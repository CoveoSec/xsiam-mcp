"""The MCP server: exposes every XSIAM operation as a tool, plus a few
high-value composite tools tuned for AI-agent ergonomics.

Transport is stdio, so any MCP client (Claude Desktop, Claude Code, etc.) can
launch it directly — no Docker, no network server to manage.

Design notes
------------
* ``self._ops_by_name`` is the single name→Operation index, built once over all
  operations. Dispatch always resolves through it.
* ``XSIAM_TOOLSETS`` is a *visibility* filter only: it controls which tools are
  advertised by ``list_tools`` to reduce the agent's cognitive load. It is not a
  security boundary — the API key's assigned role is what actually authorises
  each call server-side. An explicitly-named hidden tool (and the composite
  tools) therefore still execute.
"""

from __future__ import annotations

import asyncio
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from .client import XsiamClient, XsiamError
from .config import Config, ConfigError
from .parsing import extract_execution_id, loads_json, reply_of
from .spec import Operation, build_operations, filter_operations

SERVER_NAME = "xsiam-mcp"

# ---- composite / meta tool schemas ----------------------------------------

_HEALTH_TOOL = types.Tool(
    name="xsiam_health",
    description=(
        "Check connectivity and credentials against the XSIAM tenant. "
        "Calls the tenant-info endpoint and reports whether auth works. "
        "Run this first to confirm the server is configured correctly."
    ),
    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
)

_LIST_TOOL = types.Tool(
    name="xsiam_list_operations",
    description=(
        "Discovery tool. Returns the catalog of all available XSIAM operations "
        "grouped by category (OpenAPI tag), with each tool's name and summary. "
        "Use this to find the right tool before calling it. Optionally filter by "
        "a tag substring."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "tag": {
                "type": "string",
                "description": "Optional case-insensitive substring to filter categories, e.g. 'incident', 'xql', 'endpoint'.",
            }
        },
        "additionalProperties": False,
    },
)

_XQL_TOOL = types.Tool(
    name="xql_query",
    description=(
        "Run an XQL query end-to-end in a single call: starts the query, polls "
        "until results are ready, and returns them. This wraps "
        "xql/start_xql_query + xql/get_query_results so you don't have to poll "
        "manually. Prefer this for ad-hoc data investigation.\n\n"
        "Example: {\"query\": \"dataset = xdr_data | fields event_id, event_type "
        "| limit 10\", \"time_frame\": {\"relativeTime\": 86400000}}"
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The XQL query string to execute.",
            },
            "time_frame": {
                "type": "object",
                "description": "Optional time frame, e.g. {\"relativeTime\": 86400000} for the last 24h (ms), or {\"from\": <epoch_ms>, \"to\": <epoch_ms>}.",
            },
            "tenant_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of tenant IDs (for multi-tenant / MSSP). Sent as the XSIAM 'tenants' field.",
            },
            "max_wait_seconds": {
                "type": "integer",
                "description": "Max seconds to poll for results before returning the pending status (default 60).",
                "default": 60,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)

_COMPOSITE_TOOLS = [_HEALTH_TOOL, _LIST_TOOL, _XQL_TOOL]


class XsiamMCP:
    """Holds server state: the operation registry and a lazily-built client."""

    def __init__(self) -> None:
        self.server: Server = Server(SERVER_NAME)
        self._config: Config | None = None
        self._client: XsiamClient | None = None
        # Registry + name index are built once from the bundled spec (no creds).
        self.operations: list[Operation] = build_operations()
        self._ops_by_name: dict[str, Operation] = {op.name: op for op in self.operations}
        self._tools_cache: list[types.Tool] | None = None
        self._register_handlers()

    # ---- lazy config/client (so `--list` works without credentials) -------

    def config(self) -> Config:
        if self._config is None:
            self._config = Config.from_env()
        return self._config

    async def client(self) -> XsiamClient:
        if self._client is None:
            self._client = XsiamClient(self.config())
        return self._client

    # ---- visibility (listing only, never dispatch) ------------------------

    def _visible_operations(self) -> list[Operation]:
        """Operations to advertise, filtered by XSIAM_TOOLSETS (all if unset or
        if credentials aren't configured yet)."""
        try:
            toolsets = self.config().toolsets
        except ConfigError:
            toolsets = ()
        return filter_operations(self.operations, toolsets)

    def _tools(self) -> list[types.Tool]:
        """Build (once) and cache the advertised tool list. Config is immutable
        after first resolution, so the visible set never changes at runtime."""
        if self._tools_cache is None:
            generated = [
                types.Tool(
                    name=op.name,
                    description=op.tool_description(),
                    inputSchema=op.input_schema,
                )
                for op in self._visible_operations()
            ]
            # Composite tools first — they are the friendliest entry points.
            self._tools_cache = [*_COMPOSITE_TOOLS, *generated]
        return self._tools_cache

    # ---- handlers ---------------------------------------------------------

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            return self._tools()

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
            arguments = arguments or {}
            try:
                if name == _HEALTH_TOOL.name:
                    text = await self._health()
                elif name == _LIST_TOOL.name:
                    text = self._list_operations(arguments.get("tag"))
                elif name == _XQL_TOOL.name:
                    text = await self._xql_query(arguments)
                else:
                    text = await self._call_operation(name, arguments)
            except ConfigError as exc:
                text = f"Configuration error: {exc}"
            except XsiamError as exc:
                text = str(exc)
            except Exception as exc:  # pragma: no cover - defensive
                text = f"Unexpected error in {name}: {exc!r}"
            return [types.TextContent(type="text", text=text)]

    # ---- generic operation dispatch ---------------------------------------

    async def _call_operation(self, name: str, arguments: dict[str, Any]) -> str:
        op = self._ops_by_name.get(name)
        if op is None:
            return (
                f"Unknown tool: {name}. Call xsiam_list_operations to see available tools."
            )
        client = await self.client()
        return await client.call(op, arguments)

    # ---- composite tools --------------------------------------------------

    async def _health(self) -> str:
        try:
            self.config()
        except ConfigError as exc:
            return f"Not configured: {exc}"
        op = self._ops_by_name.get("system_get_tenant_info") or self._ops_by_name.get(
            "healthcheck"
        )
        if op is None:
            return "Could not find a health endpoint in the spec."
        client = await self.client()
        body: dict[str, Any] = {} if op.method == "get" else {"request_data": {}}
        try:
            result = await client.call(op, body)
            return f"OK — connected to {self.config().base_url}.\n{result}"
        except XsiamError as exc:
            return f"Connectivity/auth check FAILED against {self.config().base_url}.\n{exc}"

    def _list_operations(self, tag_filter: str | None) -> str:
        ops = self._visible_operations()
        by_tag: dict[str, list[Operation]] = {}
        for op in ops:
            by_tag.setdefault(op.tags[0] if op.tags else "Other", []).append(op)

        needle = (tag_filter or "").lower()
        lines: list[str] = [
            f"{len(ops)} XSIAM operations available"
            + (f" (filter: '{tag_filter}')" if tag_filter else "")
            + ". Plus composite tools: xsiam_health, xql_query.",
            "",
        ]
        for tag in sorted(by_tag):
            if needle and needle not in tag.lower():
                continue
            lines.append(f"## {tag}")
            for op in sorted(by_tag[tag], key=lambda o: o.name):
                summary = op.summary or op.description.split("\n", 1)[0]
                lines.append(f"  - {op.name}: {summary}")
            lines.append("")
        return "\n".join(lines).rstrip()

    async def _xql_query(self, arguments: dict[str, Any]) -> str:
        start_op = self._ops_by_name.get("xql_start_xql_query")
        results_op = self._ops_by_name.get("xql_get_query_results")
        if start_op is None or results_op is None:
            return "XQL operations are not present in the bundled spec."

        client = await self.client()
        request_data: dict[str, Any] = {"query": arguments["query"]}
        if arguments.get("time_frame"):
            request_data["timeframe"] = arguments["time_frame"]
        if arguments.get("tenant_ids"):
            request_data["tenants"] = arguments["tenant_ids"]

        start_raw = await client.call(start_op, {"request_data": request_data})
        execution_id = extract_execution_id(reply_of(loads_json(start_raw)))
        if not execution_id:
            return f"Could not start XQL query or extract its execution id:\n{start_raw}"

        max_wait = int(arguments.get("max_wait_seconds", 60))
        delay = 1.0
        elapsed = 0.0
        last = ""
        while elapsed <= max_wait:
            last = await client.call(
                results_op, {"request_data": {"query_id": execution_id, "pending_flag": True}}
            )
            reply = reply_of(loads_json(last))
            status = reply.get("status") if isinstance(reply, dict) else None
            if status and status != "PENDING":
                return last
            await asyncio.sleep(delay)
            elapsed += delay
            delay = min(delay * 1.5, 5.0)

        return (
            f"XQL query {execution_id} still PENDING after {max_wait}s. "
            f"Call xql_get_query_results with query_id={execution_id} to fetch results later.\n{last}"
        )

    # ---- run --------------------------------------------------------------

    async def run(self) -> None:
        async with stdio_server() as (read, write):
            await self.server.run(
                read, write, self.server.create_initialization_options()
            )


async def async_main() -> None:
    await XsiamMCP().run()


def main() -> None:
    asyncio.run(async_main())
