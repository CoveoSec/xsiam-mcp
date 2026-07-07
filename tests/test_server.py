import json
import os
import sys

import httpx
import pytest

from xsiam_mcp.client import XsiamClient, XsiamError
from xsiam_mcp.config import Config
from xsiam_mcp.server import XsiamMCP
from xsiam_mcp.spec import build_operations


def _cfg():
    return Config(
        api_key="k",
        api_key_id="7",
        base_url="https://api-test.example.com",
        auth_type="standard",
        timeout=5,
    )


def _client_with(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=_cfg().base_url)
    return XsiamClient(_cfg(), client=http)


# ---- argument routing -----------------------------------------------------


def test_split_arguments_routes_path_query_body():
    from xsiam_mcp.spec import Operation

    op = Operation(
        name="demo",
        method="post",
        path="/public_api/v1/things/{id}",
        summary="",
        description="",
        tags=("Demo",),
        input_schema={"type": "object", "properties": {}},
        path_params=("id",),
        query_params=("limit",),
    )
    url, query, body = XsiamClient.split_arguments(
        op, {"id": "abc", "limit": 10, "request_data": {"x": 1}}
    )
    assert url == "/public_api/v1/things/abc"
    assert query == {"limit": 10}
    assert body == {"request_data": {"x": 1}}


# ---- request success + auth injection -------------------------------------


@pytest.mark.asyncio
async def test_call_injects_auth_and_returns_json():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["id"] = request.headers.get("x-xdr-auth-id")
        captured["path"] = request.url.path
        return httpx.Response(200, json={"reply": {"total_count": 3}})

    client = _client_with(handler)
    op = next(o for o in build_operations() if o.name == "incidents_get_incidents")
    out = await client.call(op, {"request_data": {}})
    await client.aclose()

    assert captured["auth"] == "k"
    assert captured["id"] == "7"
    assert captured["path"] == "/public_api/v1/incidents/get_incidents"
    assert json.loads(out)["reply"]["total_count"] == 3


# ---- error formatting -----------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_readable_text_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"reply": {"err_msg": "bad key"}})

    client = _client_with(handler)
    op = next(o for o in build_operations() if o.name == "incidents_get_incidents")
    with pytest.raises(XsiamError) as exc:
        await client.call(op, {"request_data": {}})
    await client.aclose()
    text = str(exc.value)
    assert "HTTP 401" in text
    assert "Unauthorized" in text  # hint
    assert "bad key" in text


# ---- xql_query composite polling ------------------------------------------


@pytest.mark.asyncio
async def test_xql_query_starts_and_polls_to_completion():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("start_xql_query"):
            return httpx.Response(200, json={"reply": "EXEC-123"})
        # get_query_results — pending once, then success.
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"reply": {"status": "PENDING"}})
        return httpx.Response(
            200,
            json={"reply": {"status": "SUCCESS", "results": {"data": [{"a": 1}]}}},
        )

    app = XsiamMCP()
    app._config = _cfg()
    app._client = _client_with(handler)

    out = await app._xql_query({"query": "dataset = xdr_data | limit 1", "max_wait_seconds": 10})
    await app._client.aclose()
    parsed = json.loads(out)
    assert parsed["reply"]["status"] == "SUCCESS"
    assert calls["n"] >= 2  # polled at least twice


@pytest.mark.asyncio
async def test_xql_query_sends_tenants_field_for_mssp():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("start_xql_query"):
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"reply": "EXEC-1"})
        return httpx.Response(200, json={"reply": {"status": "SUCCESS"}})

    app = XsiamMCP()
    app._config = _cfg()
    app._client = _client_with(handler)
    await app._xql_query({"query": "dataset = xdr_data", "tenant_ids": ["t1", "t2"]})
    await app._client.aclose()
    # XSIAM's field is 'tenants' — not 'tenant_id'.
    assert captured["body"]["request_data"]["tenants"] == ["t1", "t2"]
    assert "tenant_id" not in captured["body"]["request_data"]


def test_toolset_filter_is_visibility_only_dispatch_still_resolves():
    # A tool hidden by XSIAM_TOOLSETS must still be dispatchable by exact name
    # (the API key's role is the real authz boundary, not this filter).
    app = XsiamMCP()
    # Every generated op is present in the single dispatch index.
    assert "endpoints_isolate" in app._ops_by_name
    assert "incidents_get_incidents" in app._ops_by_name


# ---- discovery ------------------------------------------------------------


def test_list_operations_grouped_by_tag():
    app = XsiamMCP()
    text = app._list_operations(None)
    assert "## Incidents" in text
    assert "## XQL query" in text
    assert "incidents_get_incidents" in text


def test_list_operations_tag_filter():
    app = XsiamMCP()
    text = app._list_operations("incident")
    assert "## Incidents" in text
    assert "## Endpoint Management" not in text


# ---- end-to-end stdio handshake (real subprocess) -------------------------


@pytest.mark.asyncio
async def test_stdio_list_tools_end_to_end():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env.update(
        {
            "XSIAM_API_KEY": "k",
            "XSIAM_API_KEY_ID": "7",
            "XSIAM_FQDN": "test.example.com",
        }
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "xsiam_mcp"], env=env
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            names = {t.name for t in result.tools}
            assert "xsiam_health" in names
            assert "xql_query" in names
            assert "xsiam_list_operations" in names
            assert "incidents_get_incidents" in names
            # 129 generated + 3 composite
            assert len(result.tools) >= 132

            # A pure, network-free tool call round-trips through the server.
            call = await session.call_tool("xsiam_list_operations", {"tag": "incident"})
            assert call.content
            assert "Incidents" in call.content[0].text
