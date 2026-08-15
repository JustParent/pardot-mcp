"""Thin async client for the Account Engagement (Pardot) v5 REST API.

Every call goes to ``{PARDOT_BASE_URL}/api/v5/...`` with a Bearer token and the
``Pardot-Business-Unit-Id`` header. A 401 triggers one forced token refresh and
a single retry, so an expired access token heals transparently.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

import httpx

API_PREFIX = "api/v5"

_STATUS_HINTS = {
    400: (
        "The API message usually names the invalid parameter or field — "
        "adjust `fields`/`filters` accordingly. pardot_list_object_types "
        "shows known objects and safe default fields."
    ),
    401: (
        "Authentication failed even after refreshing the token. Check the "
        "client id/secret and refresh token, and that the connected app user "
        "has Account Engagement access."
    ),
    403: (
        "Check PARDOT_BUSINESS_UNIT_ID (Salesforce Setup -> Account "
        "Engagement -> Business Unit Setup, 18-char id starting with 0Uv) and "
        "that the authenticated user can access that business unit."
    ),
    404: (
        "Check the object name and record id: v5 object names are kebab-case "
        "plurals (e.g. 'prospects', 'list-memberships'). "
        "pardot_list_object_types lists known objects."
    ),
    429: "Rate limit exceeded — wait before retrying, or reduce request volume.",
}


class TokenManager(Protocol):
    async def get_access_token(self, force_refresh: bool = False) -> str: ...


class PardotError(Exception):
    """A non-2xx response from the Pardot API, with an actionable message."""

    def __init__(self, status: int, code: Optional[int], message: str):
        self.status = status
        self.code = code
        self.message = message
        code_part = f", code {code}" if code is not None else ""
        hint = _STATUS_HINTS.get(status)
        hint_part = f" Hint: {hint}" if hint else ""
        super().__init__(f"Pardot API error (HTTP {status}{code_part}): {message}.{hint_part}")


def encode_params(params: dict[str, Any]) -> dict[str, str]:
    """Encode query params the way the v5 API expects.

    ``None`` values are dropped, booleans become ``true``/``false``, and
    lists/tuples are comma-joined (e.g. ``fields=id,email``).
    """
    encoded: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            encoded[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            encoded[key] = ",".join(str(v) for v in value)
        else:
            encoded[key] = str(value)
    return encoded


class PardotClient:
    def __init__(
        self,
        base_url: str,
        business_unit_id: str,
        token_manager: TokenManager,
        http: Optional[httpx.AsyncClient] = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._business_unit_id = business_unit_id
        self._token_manager = token_manager
        self._http = http or httpx.AsyncClient(timeout=30.0)

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        # Be forgiving if a caller already included the /api/v5 prefix.
        if path.startswith("api/"):
            return f"{self._base_url}/{path}"
        return f"{self._base_url}/{API_PREFIX}/{path}"

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = self._url(path)
        encoded = encode_params(params or {})
        response: httpx.Response
        for attempt in (1, 2):
            token = await self._token_manager.get_access_token(force_refresh=(attempt == 2))
            response = await self._http.request(
                method.upper(),
                url,
                params=encoded,
                json=json_body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Pardot-Business-Unit-Id": self._business_unit_id,
                    "Accept": "application/json",
                },
            )
            if response.status_code == 401 and attempt == 1:
                continue  # token likely expired: force a refresh and retry once
            break
        if response.status_code >= 400:
            raise _shape_error(response)
        if response.status_code == 204 or not response.content:
            return {"status": "ok"}
        return response.json()


def _shape_error(response: httpx.Response) -> PardotError:
    code: Optional[int] = None
    message: str
    try:
        body = response.json()
        code = body.get("code")
        message = body.get("message") or str(body)
    except ValueError:
        message = response.text[:300] or "(empty response body)"
    return PardotError(response.status_code, code, message)
