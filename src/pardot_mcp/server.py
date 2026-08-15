"""FastMCP server exposing the Account Engagement (Pardot) v5 API.

Design principles (mirroring google-slides-mcp):
* **Low-level first.** Generic query/get/create/update/delete tools cover every
  v5 object, and ``pardot_api_request`` is the raw escape hatch; the registry
  supplies safe default fields so common objects work with zero ceremony.
* **Bounded cost.** Every tool is a single API call with paged, trimmed output.
* **Actionable errors.** API errors come back with hints (bad object name, bad
  fields, wrong business unit, auth) so an LLM can self-correct.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any, NoReturn, Optional

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import registry
from .auth import AuthError, Settings, make_token_manager
from .client import PardotClient, PardotError

mcp = FastMCP(
    "pardot_mcp",
    instructions=(
        "Tools for the Salesforce Account Engagement (Pardot) v5 API. Start "
        "with pardot_list_object_types to see known objects, their default "
        "fields, and filters; pardot_query is the main read tool."
    ),
)

_client: Optional[PardotClient] = None


def get_client() -> PardotClient:
    """Build the API client lazily so listing tools never needs credentials."""
    global _client
    if _client is None:
        settings = Settings.from_env()
        if not settings.business_unit_id:
            raise ToolError(
                "PARDOT_BUSINESS_UNIT_ID is not set. Find it in Salesforce "
                "Setup -> Account Engagement -> Business Unit Setup (an "
                "18-char id starting with 0Uv) and add it to the server's "
                "environment in your MCP client config."
            )
        http = httpx.AsyncClient(timeout=30.0)
        _client = PardotClient(
            base_url=settings.base_url,
            business_unit_id=settings.business_unit_id,
            token_manager=make_token_manager(settings, http),
            http=http,
        )
    return _client


def _raise_tool_error(e: Exception) -> NoReturn:
    if isinstance(e, ToolError):
        raise e
    if isinstance(e, (PardotError, AuthError)):
        raise ToolError(str(e)) from e
    if isinstance(e, httpx.TimeoutException):
        raise ToolError("Request to the Pardot API timed out. Try again, or reduce `limit`.") from e
    if isinstance(e, httpx.HTTPError):
        raise ToolError(f"Network error talking to the Pardot API: {e}") from e
    raise ToolError(f"Unexpected error: {type(e).__name__}: {e}") from e


def _resolve_fields(object_type: str, fields: Optional[list[str]]) -> list[str]:
    resolved = fields or registry.default_fields(object_type)
    if not resolved:
        raise ToolError(
            f"'{registry.normalize(object_type)}' is not in the built-in object "
            "registry, and the v5 API requires an explicit fields list — pass "
            "`fields` (always include 'id'). pardot_list_object_types shows "
            "known objects."
        )
    return resolved


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@mcp.tool(
    annotations={
        "title": "List Pardot object types",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
def pardot_list_object_types() -> dict[str, Any]:
    """List known Account Engagement v5 objects with default fields and filters. (No API call.)

    Returns the built-in registry: for each object, the default `fields` used
    when a query omits them, object-specific filter params, and usage notes.
    `common_filters` apply to most objects. Objects not listed here may still
    exist in the API — query them with an explicit `fields` list.

    Returns:
        dict with `api`, `common_filters`, `objects` (name -> {default_fields,
        extra_filters, notes}), and a `note` on naming conventions.
    """
    return {
        "api": "Account Engagement (Pardot) v5 — GET/POST /api/v5/objects/{object}",
        "common_filters": registry.COMMON_FILTERS,
        "filter_value_notes": (
            "Timestamps use ISO 8601 (e.g. 2025-01-31T00:00:00Z). `id` accepts "
            "a comma-separated list. Booleans are true/false."
        ),
        "objects": {
            name: {
                "default_fields": list(s.fields),
                "extra_filters": list(s.extra_filters),
                "notes": s.notes,
            }
            for name, s in registry.OBJECTS.items()
        },
        "note": (
            "Object names are kebab-case plurals. This registry is a "
            "convenience: other objects and fields may exist in your org — "
            "pass explicit `fields` to query them, and request custom "
            "prospect fields by their field name."
        ),
    }


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
@mcp.tool(
    annotations={
        "title": "Query Pardot records",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def pardot_query(
    object_type: Annotated[str, Field(description="v5 object name, kebab-case plural, e.g. 'prospects', 'lists', 'visitor-activities'.")],
    fields: Annotated[Optional[list[str]], Field(description="Fields to return. Omit to use the registry defaults for known objects.")] = None,
    filters: Annotated[Optional[dict[str, Any]], Field(description="Filter query params, e.g. {\"email\": \"a@b.com\", \"createdAtAfter\": \"2025-01-01T00:00:00Z\", \"deleted\": false}.")] = None,
    order_by: Annotated[Optional[str], Field(description="Sort expression, e.g. 'createdAt desc' or 'id'.")] = None,
    limit: Annotated[int, Field(ge=1, le=1000, description="Max records per page (API max 1000).")] = 100,
    next_page_token: Annotated[Optional[str], Field(description="`nextPageToken` from a previous response to fetch the next page.")] = None,
) -> dict[str, Any]:
    """List/filter records of any Account Engagement object. (1 API call.)

    The main read tool. Common recipes:

    * Prospect by email: `object_type="prospects", filters={"email": "a@b.com"}`.
    * Recently updated prospects: `filters={"updatedAtAfter": "2025-06-01T00:00:00Z"}, order_by="updatedAt desc"`.
    * A prospect's activity: `object_type="visitor-activities", filters={"prospectId": 123}`.
    * Members of a list: `object_type="list-memberships", filters={"listId": 456}`.

    Returns:
        dict: {"object": str, "count": int, "values": [records...],
        "nextPageToken": str | None} — pass `nextPageToken` back in to page.
    """
    try:
        obj = registry.normalize(object_type)
        params: dict[str, Any] = dict(filters or {})
        params["fields"] = _resolve_fields(obj, fields)
        params["limit"] = limit
        params["orderBy"] = order_by
        params["nextPageToken"] = next_page_token
        data = await get_client().request("GET", f"objects/{obj}", params=params)
        values = data.get("values", [])
        return {
            "object": obj,
            "count": len(values),
            "values": values,
            "nextPageToken": data.get("nextPageToken"),
        }
    except Exception as e:  # noqa: BLE001 — shaped into actionable ToolErrors
        _raise_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Get a Pardot record",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def pardot_get_record(
    object_type: Annotated[str, Field(description="v5 object name, e.g. 'prospects'.")],
    record_id: Annotated[int, Field(description="The record's Pardot id.")],
    fields: Annotated[Optional[list[str]], Field(description="Fields to return. Omit to use the registry defaults.")] = None,
) -> dict[str, Any]:
    """Read a single record by id. (1 API call.)

    Returns:
        dict: the record with the requested fields.
    """
    try:
        obj = registry.normalize(object_type)
        return await get_client().request(
            "GET",
            f"objects/{obj}/{record_id}",
            params={"fields": _resolve_fields(obj, fields)},
        )
    except Exception as e:  # noqa: BLE001
        _raise_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Get prospect by email",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def pardot_get_prospect_by_email(
    email: Annotated[str, Field(description="The prospect's email address (exact match).")],
    fields: Annotated[Optional[list[str]], Field(description="Fields to return. Omit for the default prospect fields.")] = None,
) -> dict[str, Any]:
    """Look up prospects by exact email address. (1 API call.)

    Convenience wrapper over `pardot_query`. Orgs that allow multiple prospects
    per email can return more than one record.

    Returns:
        dict: {"count": int, "values": [prospects...]} — empty `values` means
        no prospect has that email.
    """
    try:
        obj = "prospects"
        data = await get_client().request(
            "GET",
            f"objects/{obj}",
            params={"email": email, "fields": _resolve_fields(obj, fields), "limit": 10},
        )
        values = data.get("values", [])
        return {"count": len(values), "values": values}
    except Exception as e:  # noqa: BLE001
        _raise_tool_error(e)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
@mcp.tool(
    annotations={
        "title": "Create a Pardot record",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def pardot_create_record(
    object_type: Annotated[str, Field(description="v5 object name, e.g. 'prospects', 'lists', 'list-memberships'.")],
    data: Annotated[dict[str, Any], Field(description="Field values, e.g. {\"email\": \"a@b.com\", \"firstName\": \"Ada\"} or {\"listId\": 1, \"prospectId\": 2}.")],
) -> dict[str, Any]:
    """Create a record. (1 API call.)

    Examples: create a prospect (`data={"email": ...}`), add a prospect to a
    static list (`object_type="list-memberships", data={"listId": ..., "prospectId": ...}`).
    Some objects are read-only via the API (see pardot_list_object_types notes);
    those return an error explaining so.

    Returns:
        dict: the created record (including its new `id`).
    """
    try:
        obj = registry.normalize(object_type)
        return await get_client().request("POST", f"objects/{obj}", json_body=data)
    except Exception as e:  # noqa: BLE001
        _raise_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Update a Pardot record",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def pardot_update_record(
    object_type: Annotated[str, Field(description="v5 object name, e.g. 'prospects'.")],
    record_id: Annotated[int, Field(description="The record's Pardot id.")],
    data: Annotated[dict[str, Any], Field(description="Fields to change, e.g. {\"jobTitle\": \"CTO\"}. Unmentioned fields keep their values.")],
) -> dict[str, Any]:
    """Partially update a record by id (PATCH semantics). (1 API call.)

    Returns:
        dict: the updated record.
    """
    try:
        obj = registry.normalize(object_type)
        return await get_client().request("PATCH", f"objects/{obj}/{record_id}", json_body=data)
    except Exception as e:  # noqa: BLE001
        _raise_tool_error(e)


@mcp.tool(
    annotations={
        "title": "Delete a Pardot record",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def pardot_delete_record(
    object_type: Annotated[str, Field(description="v5 object name, e.g. 'prospects', 'list-memberships'.")],
    record_id: Annotated[int, Field(description="The record's Pardot id.")],
) -> dict[str, Any]:
    """Delete a record by id. (1 API call.)

    Most Account Engagement deletes are soft deletes (recoverable from the
    recycle bin), but treat this as destructive.

    Returns:
        dict: {"status": "ok"} on success (the API returns 204 No Content).
    """
    try:
        obj = registry.normalize(object_type)
        return await get_client().request("DELETE", f"objects/{obj}/{record_id}")
    except Exception as e:  # noqa: BLE001
        _raise_tool_error(e)


# --------------------------------------------------------------------------- #
# Escape hatch
# --------------------------------------------------------------------------- #
@mcp.tool(
    annotations={
        "title": "Raw Pardot v5 API request",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def pardot_api_request(
    method: Annotated[str, Field(description="HTTP method: GET, POST, PATCH, or DELETE.")],
    path: Annotated[str, Field(description="Path relative to /api/v5/, e.g. 'objects/prospects' or 'objects/prospects/123'.")],
    params: Annotated[Optional[dict[str, Any]], Field(description="Query params. Lists are comma-joined; booleans become true/false.")] = None,
    body: Annotated[Optional[dict[str, Any]], Field(description="JSON body for POST/PATCH.")] = None,
) -> Any:
    """Raw escape hatch for anything the other tools don't cover. (1 API call.)

    Auth and the Pardot-Business-Unit-Id header are added automatically. Use it
    for endpoints beyond the object CRUD surface (e.g. the async export API
    under 'exports') or objects missing from the registry.

    Returns:
        The API's JSON response as-is.
    """
    try:
        allowed = {"GET", "POST", "PATCH", "DELETE"}
        if method.upper() not in allowed:
            raise ToolError(f"Unsupported method '{method}'. Use one of: {sorted(allowed)}.")
        return await get_client().request(method, path, params=params, json_body=body)
    except Exception as e:  # noqa: BLE001
        _raise_tool_error(e)


def main() -> None:
    """Console entry point: run the MCP server over stdio."""
    print("pardot-mcp: starting MCP server on stdio", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
