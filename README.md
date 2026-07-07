# xsiam-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
**Palo Alto Cortex XSIAM** REST API.

It exposes the *entire* XSIAM REST API (129 operations across 26 categories —
Incidents, Alerts, Endpoints, Response Actions, XQL, Scripts, IOCs/BIOCs,
Assets, Datasets, Dashboards, Playbooks, Correlation Rules, and more) as MCP
tools, auto-generated from the official OpenAPI spec so coverage stays complete
and accurate. On top of that it adds a few **composite tools** designed for how
AI agents actually work.

- **No Docker.** Pure Python, launched over stdio by your MCP client.
- **Full coverage.** One tool per API operation, with resolved JSON Schemas and
  a ready-to-use request example baked into every tool description.
- **Agent-friendly.** `xsiam_health` to sanity-check auth, `xsiam_list_operations`
  to discover tools by category, and `xql_query` that runs an XQL query and
  polls to completion in a single call.

## Install

Using [`uv`](https://docs.astral.sh/uv/) (recommended — zero global install):

```bash
git clone <this-repo> xsiam-mcp && cd xsiam-mcp
uv venv && uv pip install -e .
```

Or with pip:

```bash
pip install -e .
```

Verify the tool catalog was generated (no credentials needed):

```bash
uv run xsiam-mcp --count      # e.g. "129 generated XSIAM tools (+3 composite)."
uv run xsiam-mcp --list       # print all tools grouped by category
```

## Configuration

All configuration is via environment variables.

| Variable | Required | Description |
|----------|----------|-------------|
| `XSIAM_API_KEY` | ✅ | The API key **secret**. |
| `XSIAM_API_KEY_ID` | ✅ | The numeric key ID (sent as `x-xdr-auth-id`). |
| `XSIAM_FQDN` | ✅* | Tenant FQDN, e.g. `myco.xdr.us.paloaltonetworks.com`. Base URL becomes `https://api-<fqdn>`. |
| `XSIAM_BASE_URL` | ✅* | Full API gateway URL; overrides `XSIAM_FQDN`. |
| `XSIAM_AUTH_TYPE` | | `standard` (default) or `advanced`. |
| `XSIAM_TOOLSETS` | | Comma-separated tag names to expose (default: all). e.g. `Incidents,XQL query,Response Action`. |
| `XSIAM_TIMEOUT` | | Per-request timeout in seconds (default `60`). |
| `XSIAM_VERIFY_TLS` | | `false` to disable TLS verification (default `true`). |

\* Provide either `XSIAM_FQDN` **or** `XSIAM_BASE_URL`.

### Getting credentials

In the XSIAM console go to **Settings → Configurations → Integrations → API Keys**,
click **New Key**, choose **Standard** or **Advanced**, assign a role, and save.
Copy the **API Key** (secret) and its **ID**, and grab the **FQDN** from the same page.

- **Standard key** → set `XSIAM_AUTH_TYPE=standard` (default). The key is sent
  directly in the `Authorization` header.
- **Advanced key** → set `XSIAM_AUTH_TYPE=advanced`. The server generates a
  nonce + timestamp and sends `Authorization` as the SHA-256 of
  `api_key + nonce + timestamp`, per XSIAM's advanced auth scheme.

> The API key's assigned **role** determines which operations succeed. A read-only
> key will get `403` on write operations — that's expected.

## MCP client configuration

### Claude Desktop / Claude Code (`claude_desktop_config.json` or `.mcp.json`)

```json
{
  "mcpServers": {
    "xsiam": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/xsiam-mcp", "run", "xsiam-mcp"],
      "env": {
        "XSIAM_API_KEY": "your-api-key-secret",
        "XSIAM_API_KEY_ID": "42",
        "XSIAM_FQDN": "myco.xdr.us.paloaltonetworks.com",
        "XSIAM_AUTH_TYPE": "standard"
      }
    }
  }
}
```

To scope the toolset down (fewer tools = easier for the agent), add e.g.
`"XSIAM_TOOLSETS": "Incidents,Response Action,XQL query"` to `env`.

> `XSIAM_TOOLSETS` is a **visibility** filter — it controls which tools are
> advertised to the agent to reduce noise. It is *not* a security boundary: the
> API key's assigned **role** is what authorises each call server-side. Scope
> permissions with the key's role, not this variable.

## Usage tips for agents

1. Call **`xsiam_health`** first to confirm connectivity and auth.
2. Use **`xsiam_list_operations`** (optionally with a `tag` filter like
   `"incident"` or `"endpoint"`) to find the right tool.
3. For data investigation, prefer **`xql_query`** — it starts the query and
   polls for results in one shot.
4. Every generated tool's description contains a concrete example body. Most
   list/read endpoints accept `{"request_data": {}}` to return everything.

## Development

```bash
uv pip install -e ".[dev]"
uv run pytest -q
```

The API surface is defined by `src/xsiam_mcp/data/openapi.json`. To update
coverage when the XSIAM spec changes, drop in a new spec file — tools regenerate
automatically, no code changes required.
