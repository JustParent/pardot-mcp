import httpx
import pytest

from pardot_mcp.client import PardotClient, PardotError, encode_params

BU = "0Uv000000000001EAA"


class FakeTokenManager:
    def __init__(self):
        self.calls = []
        self.token = "tok1"

    async def get_access_token(self, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        if force_refresh:
            self.token = "tok2"
        return self.token


def make_client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tm = FakeTokenManager()
    client = PardotClient(base_url="https://pi.pardot.com", business_unit_id=BU, token_manager=tm, http=http)
    return client, tm


def test_encode_params():
    encoded = encode_params({"fields": ["id", "email"], "deleted": False, "limit": 5, "orderBy": None})
    assert encoded == {"fields": "id,email", "deleted": "false", "limit": "5"}


async def test_headers_url_and_params():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["bu"] = request.headers["Pardot-Business-Unit-Id"]
        return httpx.Response(200, json={"values": []})

    client, _ = make_client(handler)
    await client.request("GET", "objects/prospects", params={"fields": ["id", "email"], "deleted": False})

    assert seen["url"].startswith("https://pi.pardot.com/api/v5/objects/prospects?")
    assert "fields=id%2Cemail" in seen["url"]
    assert "deleted=false" in seen["url"]
    assert seen["auth"] == "Bearer tok1"
    assert seen["bu"] == BU


async def test_path_with_existing_prefix_not_doubled():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    client, _ = make_client(handler)
    await client.request("GET", "/api/v5/objects/lists", params={"fields": ["id"]})
    assert seen["url"].startswith("https://pi.pardot.com/api/v5/objects/lists?")


async def test_401_forces_refresh_and_retries_once():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if request.headers["Authorization"] == "Bearer tok1":
            return httpx.Response(401, json={"code": 184, "message": "access_token is invalid"})
        return httpx.Response(200, json={"values": [{"id": 1}]})

    client, tm = make_client(handler)
    data = await client.request("GET", "objects/prospects", params={"fields": ["id"]})
    assert data["values"] == [{"id": 1}]
    assert hits["n"] == 2
    assert tm.calls == [False, True]


async def test_repeated_401_raises_shaped_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 184, "message": "access_token is invalid"})

    client, _ = make_client(handler)
    with pytest.raises(PardotError) as excinfo:
        await client.request("GET", "objects/prospects", params={"fields": ["id"]})
    message = str(excinfo.value)
    assert "HTTP 401" in message and "code 184" in message and "Hint:" in message


async def test_error_shaping_includes_api_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 10000, "message": "Invalid fields: foo"})

    client, _ = make_client(handler)
    with pytest.raises(PardotError) as excinfo:
        await client.request("GET", "objects/prospects", params={"fields": ["foo"]})
    assert "Invalid fields: foo" in str(excinfo.value)
    assert excinfo.value.status == 400
    assert excinfo.value.code == 10000


async def test_delete_returns_ok_on_204():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(204)

    client, _ = make_client(handler)
    assert await client.request("DELETE", "objects/prospects/1") == {"status": "ok"}
