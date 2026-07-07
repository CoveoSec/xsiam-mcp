import json
import keyword

from xsiam_mcp.spec import build_operations, filter_operations


def test_full_coverage_at_least_129_tools():
    ops = build_operations()
    assert len(ops) >= 129


def test_tool_names_unique_and_valid():
    ops = build_operations()
    names = [o.name for o in ops]
    assert len(set(names)) == len(names), "duplicate tool names"
    for n in names:
        assert n.isidentifier(), f"{n} is not a valid identifier"
        assert not keyword.iskeyword(n)


def test_no_unresolved_refs():
    ops = build_operations()
    blob = json.dumps([o.input_schema for o in ops])
    assert "$ref" not in blob


def test_examples_embedded_in_description():
    ops = build_operations()
    with_example = [o for o in ops if o.example is not None]
    assert len(with_example) > 50
    sample = with_example[0]
    assert "Example request body" in sample.tool_description()


def test_summary_in_description():
    ops = build_operations()
    incidents = next(o for o in ops if o.name == "incidents_get_incidents")
    assert incidents.summary
    assert incidents.summary in incidents.tool_description()


def test_v1_v2_collision_resolved():
    ops = build_operations()
    names = {o.name for o in ops}
    assert "v1_alerts_get_alerts_multi_events" in names
    assert "v2_alerts_get_alerts_multi_events" in names


def test_injected_auth_headers_never_exposed():
    # The transport injects auth headers; they must never appear as tool inputs.
    ops = build_operations()
    forbidden = {"authorization", "x-xdr-auth-id", "x-xdr-nonce", "x-xdr-timestamp"}
    for o in ops:
        for prop in o.input_schema.get("properties", {}):
            assert prop.lower() not in forbidden, f"{o.name} exposes {prop}"


def test_param_folding_helper_routes_path_and_query():
    # This XSIAM spec has no path/query params, but the folding logic must
    # still route them correctly if a future spec revision adds them.
    from xsiam_mcp.spec import _params_by_kind

    params = [
        {"in": "path", "name": "id", "schema": {"type": "string"}},
        {"in": "query", "name": "limit", "schema": {"type": "integer"}, "required": True},
        {"in": "header", "name": "Authorization", "schema": {"type": "string"}},
    ]
    props, required, path_names, query_names = _params_by_kind(params)
    assert path_names == ["id"]
    assert query_names == ["limit"]
    assert "Authorization" not in props
    assert "id" in required and "limit" in required


def test_toolset_filter():
    ops = build_operations()
    only_incidents = filter_operations(ops, ("Incidents",))
    assert only_incidents
    assert all("Incidents" in o.tags for o in only_incidents)
    assert len(only_incidents) < len(ops)


def test_empty_toolset_returns_all():
    ops = build_operations()
    assert filter_operations(ops, ()) == ops
