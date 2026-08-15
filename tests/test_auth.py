import json

import httpx
import pytest

from pardot_mcp.auth import (
    AuthError,
    OAuthTokenManager,
    Settings,
    StaticTokenManager,
    load_cached_token,
    make_pkce,
    make_token_manager,
    save_cached_token,
)

ENV_VARS = [
    "PARDOT_CLIENT_ID", "SALESFORCE_CLIENT_ID",
    "PARDOT_CLIENT_SECRET", "SALESFORCE_CLIENT_SECRET",
    "PARDOT_BUSINESS_UNIT_ID", "PARDOT_ACCESS_TOKEN", "PARDOT_REFRESH_TOKEN",
    "PARDOT_TOKEN_PATH", "TOKEN_PATH", "SALESFORCE_LOGIN_URL", "PARDOT_LOGIN_URL",
    "PARDOT_BASE_URL", "PARDOT_OAUTH_REDIRECT_PORT", "PARDOT_OAUTH_SCOPES",
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PARDOT_TOKEN_PATH", str(tmp_path / "token.json"))
    return monkeypatch


def make_http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_pkce_pair_shape():
    verifier, challenge = make_pkce()
    assert len(verifier) >= 43 and "=" not in verifier
    assert len(challenge) == 43 and "=" not in challenge
    assert verifier != challenge


def test_settings_defaults(clean_env):
    settings = Settings.from_env()
    assert settings.login_url == "https://login.salesforce.com"
    assert settings.base_url == "https://pi.pardot.com"
    assert settings.redirect_port == 8721
    assert settings.client_id is None


def test_token_cache_roundtrip(tmp_path):
    path = tmp_path / "nested" / "token.json"
    save_cached_token(path, {"refresh_token": "r1"})
    assert load_cached_token(path) == {"refresh_token": "r1"}
    assert load_cached_token(tmp_path / "missing.json") is None


def test_static_token_manager_selected(clean_env):
    clean_env.setenv("PARDOT_ACCESS_TOKEN", "static-tok")
    manager = make_token_manager(Settings.from_env(), http=None)
    assert isinstance(manager, StaticTokenManager)


async def test_static_token_cannot_refresh():
    manager = StaticTokenManager("static-tok")
    assert await manager.get_access_token() == "static-tok"
    with pytest.raises(AuthError):
        await manager.get_access_token(force_refresh=True)


async def test_refresh_token_grant_preferred(clean_env):
    clean_env.setenv("PARDOT_CLIENT_ID", "cid")
    clean_env.setenv("PARDOT_CLIENT_SECRET", "csecret")
    clean_env.setenv("PARDOT_REFRESH_TOKEN", "rtok")
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["url"] = str(request.url)
        posted["data"] = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        return httpx.Response(200, json={"access_token": "fresh"})

    manager = OAuthTokenManager(Settings.from_env(), make_http(handler))
    assert await manager.get_access_token() == "fresh"
    assert posted["url"] == "https://login.salesforce.com/services/oauth2/token"
    assert posted["data"]["grant_type"] == "refresh_token"
    assert posted["data"]["refresh_token"] == "rtok"
    # Second call is served from memory — no extra token request.
    posted.clear()
    assert await manager.get_access_token() == "fresh"
    assert posted == {}


async def test_refresh_token_read_from_cache_file(clean_env, tmp_path):
    token_path = tmp_path / "token.json"
    clean_env.setenv("PARDOT_TOKEN_PATH", str(token_path))
    clean_env.setenv("PARDOT_CLIENT_ID", "cid")
    clean_env.setenv("PARDOT_CLIENT_SECRET", "csecret")
    save_cached_token(token_path, {"refresh_token": "cached-rtok"})

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body and "cached-rtok" in body
        # Salesforce rotated the refresh token: the cache must be updated.
        return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "rotated"})

    manager = OAuthTokenManager(Settings.from_env(), make_http(handler))
    assert await manager.get_access_token() == "fresh"
    assert json.loads(token_path.read_text())["refresh_token"] == "rotated"


async def test_broker_token_file_supplies_everything(clean_env, tmp_path):
    """A host-injected credential file (broker/per-user OAuth) needs no client
    id/secret/login-url env vars: refresh_token, client_id, client_secret and
    token_uri are all read from the file."""
    token_path = tmp_path / "token.json"
    clean_env.setenv("PARDOT_TOKEN_PATH", str(token_path))
    save_cached_token(
        token_path,
        {
            "refresh_token": "file-rtok",
            "client_id": "file-cid",
            "client_secret": "file-csecret",
            "token_uri": "https://acme.my.salesforce.com/services/oauth2/token",
            "token": "embedded-access-token-ignored",
            "scopes": ["pardot_api"],
        },
    )
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["url"] = str(request.url)
        posted["data"] = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        return httpx.Response(200, json={"access_token": "fresh"})

    manager = OAuthTokenManager(Settings.from_env(), make_http(handler))
    assert await manager.get_access_token() == "fresh"
    assert posted["url"] == "https://acme.my.salesforce.com/services/oauth2/token"
    assert posted["data"]["grant_type"] == "refresh_token"
    assert posted["data"]["refresh_token"] == "file-rtok"
    assert posted["data"]["client_id"] == "file-cid"
    assert posted["data"]["client_secret"] == "file-csecret"


async def test_env_wins_over_token_file(clean_env, tmp_path):
    token_path = tmp_path / "token.json"
    clean_env.setenv("PARDOT_TOKEN_PATH", str(token_path))
    clean_env.setenv("PARDOT_CLIENT_ID", "env-cid")
    clean_env.setenv("PARDOT_CLIENT_SECRET", "env-csecret")
    clean_env.setenv("SALESFORCE_LOGIN_URL", "https://test.salesforce.com")
    save_cached_token(
        token_path,
        {
            "refresh_token": "file-rtok",
            "client_id": "file-cid",
            "client_secret": "file-csecret",
            "token_uri": "https://acme.my.salesforce.com/services/oauth2/token",
        },
    )
    posted = {}

    def handler(request: httpx.Request) -> httpx.Response:
        posted["url"] = str(request.url)
        posted["data"] = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
        return httpx.Response(200, json={"access_token": "fresh"})

    manager = OAuthTokenManager(Settings.from_env(), make_http(handler))
    assert await manager.get_access_token() == "fresh"
    assert posted["url"] == "https://test.salesforce.com/services/oauth2/token"
    assert posted["data"]["client_id"] == "env-cid"
    assert posted["data"]["client_secret"] == "env-csecret"


async def test_client_credentials_fallback(clean_env):
    clean_env.setenv("PARDOT_CLIENT_ID", "cid")
    clean_env.setenv("PARDOT_CLIENT_SECRET", "csecret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "grant_type=client_credentials" in request.content.decode()
        return httpx.Response(200, json={"access_token": "cc-tok"})

    manager = OAuthTokenManager(Settings.from_env(), make_http(handler))
    assert await manager.get_access_token() == "cc-tok"


async def test_no_credentials_is_actionable(clean_env):
    manager = OAuthTokenManager(Settings.from_env(), make_http(lambda r: httpx.Response(500)))
    with pytest.raises(AuthError) as excinfo:
        await manager.get_access_token()
    assert "pardot-mcp-auth" in str(excinfo.value)


async def test_token_endpoint_failure_is_actionable(clean_env):
    clean_env.setenv("PARDOT_CLIENT_ID", "cid")
    clean_env.setenv("PARDOT_CLIENT_SECRET", "csecret")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "no client credentials user enabled"})

    manager = OAuthTokenManager(Settings.from_env(), make_http(handler))
    with pytest.raises(AuthError) as excinfo:
        await manager.get_access_token()
    message = str(excinfo.value)
    assert "invalid_grant" in message and "Client Credentials" in message
