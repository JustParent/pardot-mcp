# pardot-mcp

A `uvx`-runnable [MCP](https://modelcontextprotocol.io) server for the
**Salesforce Account Engagement (Pardot) v5 API** — pure Python, talking
straight to the REST API. No JVM, no JDBC drivers, no per-seat driver licences.

- **Read anything**: `pardot_query` lists/filters any v5 object (prospects,
  lists, campaigns, emails, forms, visitor activities, …) with paging, and a
  built-in registry supplies sensible default fields so common objects work
  with zero ceremony.
- **Write the common things**: create/update/delete records — upsert prospects,
  add them to lists, manage list memberships.
- **Escape hatch**: `pardot_api_request` issues raw v5 calls (e.g. the async
  export API) for anything the convenience tools don't cover.
- **Bounded cost**: every tool is a single API call with trimmed, paged output.
- **Actionable errors**: bad object names, bad fields, wrong business unit and
  auth problems come back with hints, so an LLM driving the server can
  self-correct.

## Tools

| Tool | Description |
| --- | --- |
| `pardot_list_object_types` | Known v5 objects with default fields, filters and notes (no API call) |
| `pardot_query` | List/filter records of any object, with ordering and paging |
| `pardot_get_record` | Read one record by id |
| `pardot_get_prospect_by_email` | Look up prospects by exact email |
| `pardot_create_record` | Create a record (prospect, list, list membership, …) |
| `pardot_update_record` | Partial update by id |
| `pardot_delete_record` | Delete by id (usually a recoverable soft delete) |
| `pardot_api_request` | Raw GET/POST/PATCH/DELETE against `/api/v5/...` |

## 1. Salesforce setup (one time)

Account Engagement's API authenticates through Salesforce OAuth via a
**connected app**:

1. In Salesforce **Setup → App Manager → New Connected App**.
2. Tick **Enable OAuth Settings** and configure:
   - **Callback URL**: `http://localhost:8721/callback` (only used by the
     optional interactive login below; any placeholder works if you'll use
     client credentials).
   - **Selected OAuth Scopes**: *Manage Pardot services (pardot_api)*, plus
     *Perform requests at any time (refresh_token, offline_access)* if you'll
     use the interactive login.
   - PKCE can stay required — the login command uses it.
3. Save, then **Manage Consumer Details** to copy the **consumer key**
   (`PARDOT_CLIENT_ID`) and **consumer secret** (`PARDOT_CLIENT_SECRET`).
4. Find your **business unit id** (`PARDOT_BUSINESS_UNIT_ID`): Setup →
   quick-find **"Business Unit Setup"** — an 18-character id starting `0Uv`.
5. Pick an auth flow:
   - **Client credentials (recommended — no browser, no token to babysit).**
     On the connected app: *Manage → Edit Policies → enable Client Credentials
     Flow* and set a **Run As** user that has Account Engagement access to
     your business unit.
   - **Interactive login (per-user).** Nothing extra — you'll run
     `pardot-mcp-auth` once (next section).

> The API user (run-as user or the person logging in) must have access to the
> Account Engagement business unit you query.

## 2. Install & first-time login

With **client credentials** there is no first-time login — skip to section 3.

With the **interactive flow**, log in once; a refresh token is cached at
`~/.config/pardot-mcp/token.json` (override with `PARDOT_TOKEN_PATH`) and the
server uses it silently from then on:

```bash
PARDOT_CLIENT_ID=... PARDOT_CLIENT_SECRET=... \
  uvx --from git+https://github.com/JustParent/pardot-mcp pardot-mcp-auth
```

A browser opens for Salesforce login; the connected app must list
`http://localhost:8721/callback` as a callback URL (port configurable via
`PARDOT_OAUTH_REDIRECT_PORT`). For sandboxes, also set
`SALESFORCE_LOGIN_URL=https://test.salesforce.com`.

## 3. Configure your MCP client

Add the server to Claude Desktop / Claude Code (`mcpServers` config):

```json
{
  "mcpServers": {
    "pardot": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/JustParent/pardot-mcp",
        "pardot-mcp"
      ],
      "env": {
        "PARDOT_CLIENT_ID": "3MVG9...",
        "PARDOT_CLIENT_SECRET": "...",
        "PARDOT_BUSINESS_UNIT_ID": "0Uv..."
      }
    }
  }
}
```

That's the whole config for the client-credentials flow. If you used the
interactive login instead, the same config works — the server finds the cached
token automatically (set `PARDOT_TOKEN_PATH` too if you overrode it).

## Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `PARDOT_CLIENT_ID` | yes* | Connected app consumer key (alias `SALESFORCE_CLIENT_ID`) |
| `PARDOT_CLIENT_SECRET` | yes* | Connected app consumer secret (alias `SALESFORCE_CLIENT_SECRET`) |
| `PARDOT_BUSINESS_UNIT_ID` | yes | 18-char business unit id (`0Uv...`), sent as the `Pardot-Business-Unit-Id` header |
| `PARDOT_REFRESH_TOKEN` | no | Refresh token, if you'd rather paste one than use `pardot-mcp-auth` |
| `PARDOT_ACCESS_TOKEN` | no | Static access token for quick tests (expires; no refresh) |
| `PARDOT_TOKEN_PATH` | no | Token file path (default `~/.config/pardot-mcp/token.json`; alias `TOKEN_PATH`) |
| `SALESFORCE_LOGIN_URL` | no | Default `https://login.salesforce.com`; sandboxes use `https://test.salesforce.com` |
| `PARDOT_BASE_URL` | no | Default `https://pi.pardot.com`; AE sandbox/demo orgs use `https://pi.demo.pardot.com` |
| `PARDOT_OAUTH_REDIRECT_PORT` | no | Local callback port for `pardot-mcp-auth` (default `8721`) |
| `PARDOT_OAUTH_SCOPES` | no | Scope override for `pardot-mcp-auth`; omit to use all app scopes |

\* not needed if you only set `PARDOT_ACCESS_TOKEN` for a quick test.

Credential resolution order at startup: `PARDOT_ACCESS_TOKEN` → refresh token
(`PARDOT_REFRESH_TOKEN`, then the token cache) → client credentials.

## Per-user OAuth via a hosting platform (token file)

The token file is also the integration point for platforms that broker
per-user OAuth — each end user connects their own Salesforce account in the
host app, and the host launches the server per user/session with an injected
credential file:

1. Run the OAuth dance host-side against
   `https://login.salesforce.com/services/oauth2/authorize` + `/token`
   (scopes: `pardot_api refresh_token`), storing the user's refresh token.
2. Per session, write a JSON credential file into the server's environment and
   point `TOKEN_PATH` (or `PARDOT_TOKEN_PATH`) at it.
3. Pass `PARDOT_BUSINESS_UNIT_ID` (and `PARDOT_BASE_URL` for sandbox orgs) as
   static env vars.

Recognized keys in the file:

| Key | Required | Meaning |
| --- | --- | --- |
| `refresh_token` | yes | The user's Salesforce refresh token |
| `client_id` / `client_secret` | no | Connected app credentials; used when the `PARDOT_CLIENT_ID`/`PARDOT_CLIENT_SECRET` env vars are absent |
| `token_uri` | no | Full token-endpoint URL (e.g. a My Domain or `test.salesforce.com` URL); used when `SALESFORCE_LOGIN_URL` is not explicitly set |

Env vars always win over file values. The shape is a superset of Google-style
`authorized_user` token JSON, so hosts that already emit that shape for other
servers work unchanged; unknown keys (`token`, `scopes`, …) are ignored — the
server always mints a fresh access token via the refresh grant, sending no
`scope` parameter (so it never narrows or re-requests scopes). If Salesforce
rotates the refresh token, the file is updated in place.

## Sandboxes / demo orgs

Set both of these for an Account Engagement sandbox:

```
SALESFORCE_LOGIN_URL=https://test.salesforce.com
PARDOT_BASE_URL=https://pi.demo.pardot.com
```

## A note on version pinning

`fastmcp` is pinned to `>=2.9,<3` on purpose: FastMCP major versions are
breaking (1.x was folded into the official `mcp` SDK; 2.x is the standalone
package this server is written against; 3.x changed APIs again). Because `uvx`
re-resolves dependencies, an unpinned dependency would eventually pick up a
breaking major on someone's machine mid-flight. Bump the bound only as a
deliberate migration.

## Development

```bash
git clone https://github.com/JustParent/pardot-mcp
cd pardot-mcp
uv venv && uv pip install -e '.[dev]'
uv run pytest
```

The tests are offline: the Pardot API is mocked with `httpx.MockTransport`,
and the smoke tests drive the real server through FastMCP's in-memory client.

To poke at the server interactively:

```bash
npx @modelcontextprotocol/inspector uvx --from . pardot-mcp
```

## License

MIT
