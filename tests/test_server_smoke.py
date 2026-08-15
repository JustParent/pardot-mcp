"""End-to-end smoke tests: drive the FastMCP server through an in-memory MCP
client, with the Pardot API replaced by an httpx.MockTransport."""

import json

import httpx
import pytest
from fastmcp import Client

import pardot_mcp.server as server_mod
from pardot_mcp.client import PardotClient

EXPECTED_TOOLS = {
    "pardot_list_object_types",
    "pardot_query",
    "pardot_get_record",
    "pardot_get_prospect_by_email",
    "pardot_create_record",
    "pardot_update_record",
    "pardot_delete_record",
    "pardot_api_request",
}


class FakeTokenManager:
    async def get_access_token(self, force_refresh: bool = False) -> str:
        return "tok"


@pytest.fixture
def mock_api(monkeypatch):
    """Point the server at a fake Pardot API; returns the request log."""
    log = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        if request.method == "GET" and request.url.path == "/api/v5/objects/prospects":
            return httpx.Response(
                200,
                json={
                    "values": [{"id": 7, "email": "ada@example.com"}],
                    "nextPageToken": "tok123",
                },
            )
        if request.method == "POST" and request.url.path == "/api/v5/objects/lists":
            return httpx.Response(201, json={"id": 55, "name": json.loads(request.content)["name"]})
        return httpx.Response(404, json={"code": 109, "message": "not found"})

    client = PardotClient(
        base_url="https://pi.pardot.com",
        business_unit_id="0Uv000000000001EAA",
        token_manager=FakeTokenManager(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(server_mod, "_client", client)
    return log


def _payload(result):
    """Extract the JSON payload from a tool result across fastmcp 2.x minors."""
    blocks = result.content if hasattr(result, "content") else result
    return json.loads(blocks[0].text)


async def test_all_tools_listed():
    async with Client(server_mod.mcp) as client:
        tools = {tool.name for tool in await client.list_tools()}
    assert EXPECTED_TOOLS <= tools


async def test_list_object_types_needs_no_credentials(monkeypatch):
    monkeypatch.setattr(server_mod, "_client", None)
    async with Client(server_mod.mcp) as client:
        payload = _payload(await client.call_tool("pardot_list_object_types", {}))
    assert "prospects" in payload["objects"]
    assert "email" in payload["objects"]["prospects"]["default_fields"]


async def test_query_end_to_end(mock_api):
    async with Client(server_mod.mcp) as client:
        payload = _payload(
            await client.call_tool(
                "pardot_query",
                {"object_type": "prospects", "filters": {"email": "ada@example.com"}},
            )
        )
    assert payload["count"] == 1
    assert payload["values"][0]["email"] == "ada@example.com"
    assert payload["nextPageToken"] == "tok123"
    url = str(mock_api[0].url)
    assert "email=ada%40example.com" in url
    assert "fields=" in url  # registry defaults were applied
    assert mock_api[0].headers["Pardot-Business-Unit-Id"] == "0Uv000000000001EAA"


async def test_create_end_to_end(mock_api):
    async with Client(server_mod.mcp) as client:
        payload = _payload(
            await client.call_tool(
                "pardot_create_record",
                {"object_type": "lists", "data": {"name": "My List"}},
            )
        )
    assert payload == {"id": 55, "name": "My List"}


async def test_unknown_object_without_fields_is_actionable(mock_api):
    async with Client(server_mod.mcp) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("pardot_query", {"object_type": "wombats"})
    assert "fields" in str(excinfo.value)


async def test_api_error_surfaces_hint(mock_api):
    async with Client(server_mod.mcp) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool(
                "pardot_query",
                {"object_type": "campaigns", "fields": ["id", "name"]},
            )
    message = str(excinfo.value)
    assert "HTTP 404" in message and "kebab-case" in message


async def test_missing_business_unit_is_actionable(monkeypatch):
    monkeypatch.setattr(server_mod, "_client", None)
    for var in ("PARDOT_BUSINESS_UNIT_ID", "PARDOT_ACCESS_TOKEN", "PARDOT_CLIENT_ID", "PARDOT_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    async with Client(server_mod.mcp) as client:
        with pytest.raises(Exception) as excinfo:
            await client.call_tool("pardot_query", {"object_type": "prospects"})
    assert "PARDOT_BUSINESS_UNIT_ID" in str(excinfo.value)
