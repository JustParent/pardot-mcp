"""Salesforce OAuth2 authentication for the Pardot (Account Engagement) MCP server.

Three ways to authenticate, checked in this order at server startup:

1. **Static access token** — set ``PARDOT_ACCESS_TOKEN``. Handy for a quick
   test, but Salesforce access tokens expire and cannot be refreshed, so calls
   will start failing after the session times out.
2. **Refresh token** — set ``PARDOT_REFRESH_TOKEN`` (plus client id/secret), or
   log in once in a browser with the ``pardot-mcp-auth`` command, which caches
   a refresh token at ``PARDOT_TOKEN_PATH``
   (default ``~/.config/pardot-mcp/token.json``).

   The token file is also the integration point for **hosting platforms that
   broker per-user OAuth** (each end user connects their own Salesforce
   account, the host injects a credential file per session and points
   ``TOKEN_PATH`` at it). Recognized keys: ``refresh_token`` (required),
   plus optional ``client_id`` / ``client_secret`` (used when the env vars are
   absent) and ``token_uri`` (full token-endpoint URL, used when
   ``SALESFORCE_LOGIN_URL`` is not explicitly set). This is a superset of the
   Google-style ``authorized_user`` JSON shape; unknown keys are ignored, and
   a fresh access token is always minted via the refresh grant.
3. **Client credentials** — set only ``PARDOT_CLIENT_ID`` /
   ``PARDOT_CLIENT_SECRET`` and enable the *Client Credentials Flow* on the
   connected app (with a run-as user that has Account Engagement access).
   Recommended for headless / single-user setups: no browser involved.

Environment variables:

* ``PARDOT_CLIENT_ID`` / ``PARDOT_CLIENT_SECRET`` — connected app consumer
  key/secret (aliases: ``SALESFORCE_CLIENT_ID`` / ``SALESFORCE_CLIENT_SECRET``).
* ``PARDOT_BUSINESS_UNIT_ID`` — 18-char Account Engagement business unit id
  (starts with ``0Uv``); sent as the ``Pardot-Business-Unit-Id`` header.
* ``PARDOT_REFRESH_TOKEN`` / ``PARDOT_ACCESS_TOKEN`` — optional, see above.
* ``PARDOT_TOKEN_PATH`` — token cache written by ``pardot-mcp-auth``.
* ``SALESFORCE_LOGIN_URL`` — default ``https://login.salesforce.com``; use
  ``https://test.salesforce.com`` for sandboxes, or your My Domain URL.
* ``PARDOT_BASE_URL`` — default ``https://pi.pardot.com``; use
  ``https://pi.demo.pardot.com`` for Account Engagement sandbox/demo orgs.
* ``PARDOT_OAUTH_REDIRECT_PORT`` — local callback port for ``pardot-mcp-auth``
  (default 8721; the connected app must list
  ``http://localhost:<port>/callback`` as a callback URL).
* ``PARDOT_OAUTH_SCOPES`` — optional scope override for ``pardot-mcp-auth``;
  omitted by default so all scopes granted to the connected app apply.

All diagnostics are written to stderr: stdout carries the MCP stdio JSON-RPC
channel and must stay clean.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

DEFAULT_LOGIN_URL = "https://login.salesforce.com"
DEFAULT_BASE_URL = "https://pi.pardot.com"
DEFAULT_TOKEN_PATH = "~/.config/pardot-mcp/token.json"
DEFAULT_REDIRECT_PORT = 8721

SETUP_HELP = (
    "No usable credentials found. Configure one of: "
    "(a) PARDOT_CLIENT_ID + PARDOT_CLIENT_SECRET with the connected app's "
    "Client Credentials Flow enabled (simplest, no browser); "
    "(b) a refresh token — run `pardot-mcp-auth` once to log in via browser, "
    "or set PARDOT_REFRESH_TOKEN; "
    "(c) PARDOT_ACCESS_TOKEN for a short-lived test. "
    "See the README for the Salesforce connected app setup."
)


class AuthError(Exception):
    """Raised when credentials are missing or a token request fails."""


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


@dataclass
class Settings:
    """Runtime configuration resolved from environment variables."""

    client_id: Optional[str]
    client_secret: Optional[str]
    business_unit_id: Optional[str]
    login_url: str
    base_url: str
    access_token: Optional[str]
    refresh_token: Optional[str]
    token_path: Path
    redirect_port: int
    scopes: Optional[str]
    # True when the login URL came from the environment (as opposed to the
    # default): an explicit env value beats a token_uri found in the token file.
    login_url_overridden: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        raw_login_url = _env("SALESFORCE_LOGIN_URL", "PARDOT_LOGIN_URL")
        return cls(
            client_id=_env("PARDOT_CLIENT_ID", "SALESFORCE_CLIENT_ID"),
            client_secret=_env("PARDOT_CLIENT_SECRET", "SALESFORCE_CLIENT_SECRET"),
            business_unit_id=_env("PARDOT_BUSINESS_UNIT_ID"),
            login_url=(raw_login_url or DEFAULT_LOGIN_URL).rstrip("/"),
            base_url=(_env("PARDOT_BASE_URL", default=DEFAULT_BASE_URL) or "").rstrip("/"),
            access_token=_env("PARDOT_ACCESS_TOKEN"),
            refresh_token=_env("PARDOT_REFRESH_TOKEN"),
            token_path=Path(_env("PARDOT_TOKEN_PATH", "TOKEN_PATH", default=DEFAULT_TOKEN_PATH) or DEFAULT_TOKEN_PATH).expanduser(),
            redirect_port=int(_env("PARDOT_OAUTH_REDIRECT_PORT", default=str(DEFAULT_REDIRECT_PORT)) or DEFAULT_REDIRECT_PORT),
            scopes=_env("PARDOT_OAUTH_SCOPES"),
            login_url_overridden=raw_login_url is not None,
        )


# --------------------------------------------------------------------------- #
# Token cache
# --------------------------------------------------------------------------- #
def load_cached_token(path: Path) -> Optional[dict[str, Any]]:
    """Read the token cache written by ``pardot-mcp-auth``; None if absent/bad."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save_cached_token(path: Path, token: dict[str, Any]) -> None:
    """Write the token cache with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Token managers
# --------------------------------------------------------------------------- #
class StaticTokenManager:
    """Serves a fixed access token from PARDOT_ACCESS_TOKEN."""

    def __init__(self, access_token: str):
        self._access_token = access_token

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if force_refresh:
            raise AuthError(
                "The Pardot API rejected the static PARDOT_ACCESS_TOKEN and it "
                "cannot be refreshed. Mint a new one, or switch to the refresh "
                "token or client credentials flow (see README)."
            )
        return self._access_token


class OAuthTokenManager:
    """Acquires and caches an access token via Salesforce OAuth.

    Prefers the refresh-token grant, then the client-credentials grant. Each
    credential resolves env-first with the token file as fallback, so both a
    local ``pardot-mcp-auth`` login and a host-injected per-user credential
    file (refresh_token + client_id/client_secret + token_uri) work unchanged.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self._settings = settings
        self._http = http
        self._access_token: Optional[str] = None
        self._lock = asyncio.Lock()

    async def get_access_token(self, force_refresh: bool = False) -> str:
        if self._access_token and not force_refresh:
            return self._access_token
        async with self._lock:
            if self._access_token and not force_refresh:
                return self._access_token
            self._access_token = await self._acquire()
            return self._access_token

    def _token_endpoint(self, cached: dict[str, Any]) -> str:
        s = self._settings
        if not s.login_url_overridden:
            token_uri = cached.get("token_uri")
            if isinstance(token_uri, str) and token_uri.startswith("https://"):
                return token_uri
        return f"{s.login_url}/services/oauth2/token"

    async def _acquire(self) -> str:
        s = self._settings
        cached = load_cached_token(s.token_path) or {}
        refresh_token = s.refresh_token or cached.get("refresh_token")
        refresh_from_cache = not s.refresh_token and bool(cached.get("refresh_token"))
        client_id = s.client_id or cached.get("client_id")
        client_secret = s.client_secret or cached.get("client_secret")
        token_endpoint = self._token_endpoint(cached)

        if refresh_token:
            if not client_id:
                raise AuthError(
                    "A refresh token is configured but no client id was found; "
                    "the refresh-token grant needs the connected app's consumer "
                    "key (and usually its secret). Set PARDOT_CLIENT_ID / "
                    "PARDOT_CLIENT_SECRET, or include client_id / client_secret "
                    "in the token file."
                )
            data = {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
            if client_secret:
                data["client_secret"] = client_secret
            token = await self._fetch_token(token_endpoint, data)
            # Salesforce can rotate refresh tokens; keep the cache current.
            new_refresh = token.get("refresh_token")
            if refresh_from_cache and new_refresh and new_refresh != refresh_token:
                updated = dict(cached)
                updated["refresh_token"] = new_refresh
                save_cached_token(s.token_path, updated)
            return token["access_token"]

        if client_id and client_secret:
            token = await self._fetch_token(
                token_endpoint,
                {
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            return token["access_token"]

        raise AuthError(SETUP_HELP)

    async def _fetch_token(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        response = await self._http.post(url, data=data)
        if response.status_code != 200:
            raise AuthError(_describe_token_failure(response, data["grant_type"]))
        return response.json()


def _describe_token_failure(response: httpx.Response, grant_type: str) -> str:
    try:
        body = response.json()
        detail = f"{body.get('error')}: {body.get('error_description')}"
    except ValueError:
        detail = response.text[:300]
    hint = ""
    if grant_type == "client_credentials":
        hint = (
            " Hint: the connected app must have 'Enable Client Credentials "
            "Flow' switched on with a run-as user that has Account Engagement "
            "access, and SALESFORCE_LOGIN_URL must match the org (sandboxes "
            "use https://test.salesforce.com)."
        )
    elif grant_type == "refresh_token":
        hint = (
            " Hint: refresh tokens are revoked when the connected app's OAuth "
            "policies change or the user revokes access — re-run "
            "`pardot-mcp-auth` to log in again."
        )
    return f"Salesforce token request failed ({response.status_code}): {detail}.{hint}"


def make_token_manager(settings: Settings, http: httpx.AsyncClient):
    """Pick the token manager implied by the configured environment."""
    if settings.access_token:
        return StaticTokenManager(settings.access_token)
    return OAuthTokenManager(settings, http)


# --------------------------------------------------------------------------- #
# Interactive login (`pardot-mcp-auth`)
# --------------------------------------------------------------------------- #
def make_pkce() -> tuple[str, str]:
    """Return a PKCE (code_verifier, S256 code_challenge) pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _wait_for_callback(port: int, auth_url: str, timeout_seconds: int = 300) -> dict[str, str]:
    """Serve http://localhost:<port>/callback until Salesforce redirects back."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (BaseHTTPRequestHandler API)
            parsed = urlsplit(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            self.server.oauth_result = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>pardot-mcp: login complete.</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )

        def log_message(self, *args):  # silence request logging
            pass

    server = HTTPServer(("localhost", port), Handler)
    server.oauth_result = None  # type: ignore[attr-defined]
    server.timeout = 1
    try:
        webbrowser.open(auth_url)
        deadline = time.monotonic() + timeout_seconds
        while server.oauth_result is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
            server.handle_request()
    finally:
        server.server_close()
    if server.oauth_result is None:  # type: ignore[attr-defined]
        print("Timed out waiting for the OAuth callback (5 minutes).", file=sys.stderr)
        sys.exit(1)
    return server.oauth_result  # type: ignore[attr-defined]


def auth_main() -> None:
    """Console entry point: interactive browser login that caches a refresh token."""
    settings = Settings.from_env()
    if not settings.client_id or not settings.client_secret:
        print(
            "Set PARDOT_CLIENT_ID and PARDOT_CLIENT_SECRET (the connected app's\n"
            "consumer key and secret) before running pardot-mcp-auth.\n"
            "See the README for the one-time Salesforce setup.",
            file=sys.stderr,
        )
        sys.exit(1)

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{settings.redirect_port}/callback"
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if settings.scopes:
        params["scope"] = " ".join(re.split(r"[,\s]+", settings.scopes.strip()))
    auth_url = f"{settings.login_url}/services/oauth2/authorize?{urlencode(params)}"

    print(
        f"Opening a browser for Salesforce login ({settings.login_url}).\n"
        f"If nothing opens, visit:\n\n  {auth_url}\n",
        file=sys.stderr,
    )
    result = _wait_for_callback(settings.redirect_port, auth_url)

    if "error" in result:
        print(
            f"Salesforce returned an error: {result['error']}: "
            f"{result.get('error_description', '(no description)')}",
            file=sys.stderr,
        )
        sys.exit(1)
    if result.get("state") != state:
        print("OAuth state mismatch — aborting (possible CSRF).", file=sys.stderr)
        sys.exit(1)
    code = result.get("code")
    if not code:
        print("No authorization code in the callback — aborting.", file=sys.stderr)
        sys.exit(1)

    response = httpx.post(
        f"{settings.login_url}/services/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if response.status_code != 200:
        print(_describe_token_failure(response, "authorization_code"), file=sys.stderr)
        sys.exit(1)
    token = response.json()

    save_cached_token(
        settings.token_path,
        {
            "refresh_token": token.get("refresh_token"),
            "access_token": token.get("access_token"),
            "instance_url": token.get("instance_url"),
            "login_url": settings.login_url,
            "obtained_at": int(time.time()),
        },
    )
    print(f"Token cached at {settings.token_path}", file=sys.stderr)
    if not token.get("refresh_token"):
        print(
            "WARNING: no refresh token was returned. Add the 'Perform requests "
            "at any time (refresh_token, offline_access)' scope to the "
            "connected app and log in again, or the login will expire.",
            file=sys.stderr,
        )
    if not settings.business_unit_id:
        print(
            "Reminder: also set PARDOT_BUSINESS_UNIT_ID (Salesforce Setup -> "
            "Account Engagement -> Business Unit Setup, an 18-char id starting "
            "with 0Uv) in your MCP client config.",
            file=sys.stderr,
        )
    print("Login complete.", file=sys.stderr)
