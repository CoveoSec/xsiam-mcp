"""Load the bundled Cortex XSIAM OpenAPI spec and turn every operation into an
MCP tool definition.

The goal is *full coverage*: one MCP tool per OpenAPI operation (~129 tools),
each with a resolved JSON Schema for its input and a description rich enough
that an AI agent can call it correctly on the first try (spec summary +
description + a concrete request example lifted from the spec).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

# Header params the transport injects itself — never expose these to the model.
_INJECTED_HEADERS = {
    "authorization",
    "x-xdr-auth-id",
    "x-xdr-nonce",
    "x-xdr-timestamp",
    "content-type",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass
class Operation:
    """A single callable XSIAM API operation, ready to expose as an MCP tool."""

    name: str
    method: str
    path: str
    summary: str
    description: str
    tags: tuple[str, ...]
    input_schema: dict[str, Any]
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    example: dict[str, Any] | None = None

    def tool_description(self) -> str:
        """Human/agent-facing description: summary + detail + a ready example."""
        parts: list[str] = []
        if self.summary:
            parts.append(self.summary)
        if self.description and self.description != self.summary:
            parts.append(self.description.strip())
        tag = self.tags[0] if self.tags else ""
        parts.append(f"[{self.method.upper()} {self.path}]" + (f" · {tag}" if tag else ""))
        if self.example is not None:
            parts.append(
                "Example request body:\n```json\n"
                + json.dumps(self.example, indent=2)
                + "\n```"
            )
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Spec loading + $ref resolution
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_spec() -> dict[str, Any]:
    """Load the bundled OpenAPI JSON document."""
    with resources.files("xsiam_mcp.data").joinpath("openapi.json").open(
        "r", encoding="utf-8"
    ) as fh:
        return json.load(fh)


class _RefResolver:
    """Resolves local ``$ref`` pointers, guarding against infinite recursion on
    self-referential schemas by substituting a shallow ``{}`` past a depth cap."""

    def __init__(self, root: dict[str, Any], max_depth: int = 40) -> None:
        self.root = root
        self.max_depth = max_depth

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return {}
        node: Any = self.root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return {}
        return node

    def resolve(self, schema: Any, _depth: int = 0, _seen: frozenset[str] = frozenset()) -> Any:
        if _depth > self.max_depth:
            return {}
        if isinstance(schema, dict):
            if "$ref" in schema:
                ref = schema["$ref"]
                if ref in _seen:
                    # Cycle — stop expanding, keep the object usable.
                    return {"type": "object"}
                target = self._lookup(ref)
                resolved = self.resolve(target, _depth + 1, _seen | {ref})
                # Merge any sibling keys alongside the $ref (rare but valid).
                extra = {k: v for k, v in schema.items() if k != "$ref"}
                if extra and isinstance(resolved, dict):
                    resolved = {**resolved, **self.resolve(extra, _depth + 1, _seen)}
                return resolved
            return {
                k: self.resolve(v, _depth + 1, _seen)
                for k, v in schema.items()
                # Drop vendor extensions we surface elsewhere / that clutter schemas.
                if k not in ("x-examples", "x-stoplight")
            }
        if isinstance(schema, list):
            return [self.resolve(item, _depth + 1, _seen) for item in schema]
        return schema


# ---------------------------------------------------------------------------
# Tool-name derivation
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^v\d+$")


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def _sanitize(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name.lower()


def _base_name(path: str, *, keep_version: bool) -> str:
    segs = _segments(path)
    if segs and segs[0] == "public_api":
        segs = segs[1:]
    if not keep_version:
        segs = [s for s in segs if not _VERSION_RE.match(s)]
    # Substitute path-template braces for a readable token.
    segs = [s.replace("{", "by_").replace("}", "") for s in segs]
    return _sanitize("_".join(segs)) or "op"


def _derive_names(paths: dict[str, Any]) -> dict[str, str]:
    """Map each path to a unique, valid, readable tool-name stem.

    Prefer the version-stripped name; only fall back to the version-qualified
    name for paths that would otherwise collide (e.g. the v1 and v2 variants of
    get_alerts_multi_events)."""
    stripped: dict[str, list[str]] = {}
    for path in paths:
        stripped.setdefault(_base_name(path, keep_version=False), []).append(path)

    names: dict[str, str] = {}
    used: set[str] = set()
    for base, group in stripped.items():
        if len(group) == 1:
            names[group[0]] = base
            used.add(base)
        else:
            for path in group:
                versioned = _base_name(path, keep_version=True)
                candidate = versioned
                i = 2
                while candidate in used:
                    candidate = f"{versioned}_{i}"
                    i += 1
                names[path] = candidate
                used.add(candidate)
    return names


# ---------------------------------------------------------------------------
# Operation extraction
# ---------------------------------------------------------------------------


def _extract_example(body_schema: dict[str, Any]) -> dict[str, Any] | None:
    """Pull a concrete request example from the operation, if the spec has one."""
    x_examples = body_schema.get("x-examples")
    if isinstance(x_examples, dict) and x_examples:
        first = next(iter(x_examples.values()))
        if isinstance(first, dict):
            return first
    example = body_schema.get("example")
    if isinstance(example, dict):
        return example
    return None


def _params_by_kind(
    params: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """Return (schema-properties, required, path_names, query_names) for
    non-injected path/query parameters."""
    props: dict[str, Any] = {}
    required: list[str] = []
    path_names: list[str] = []
    query_names: list[str] = []
    for p in params:
        if not isinstance(p, dict):
            continue
        loc = p.get("in")
        name = p.get("name", "")
        if loc == "header" or name.lower() in _INJECTED_HEADERS:
            continue
        if loc == "path":
            path_names.append(name)
        elif loc == "query":
            query_names.append(name)
        else:
            continue
        schema = dict(p.get("schema") or {"type": "string"})
        if p.get("description"):
            schema.setdefault("description", p["description"])
        props[name] = schema
        if p.get("required") or loc == "path":
            required.append(name)
    return props, required, path_names, query_names


def build_operations() -> list[Operation]:
    """Parse the bundled spec into a list of Operation objects (one per op)."""
    spec = load_spec()
    resolver = _RefResolver(spec)
    paths = spec.get("paths", {})
    names = _derive_names(paths)

    operations: list[Operation] = []
    for path, path_item in paths.items():
        shared_params = path_item.get("parameters", []) or []
        stem = names[path]
        methods = [m for m in path_item if m.lower() in _HTTP_METHODS]
        for method in methods:
            op = path_item[method]
            if not isinstance(op, dict):
                continue

            # Combine shared + operation-level params.
            all_params = list(shared_params) + list(op.get("parameters", []) or [])
            (
                param_props,
                param_required,
                path_names,
                query_names,
            ) = _params_by_kind(all_params)

            # Request body schema.
            body_schema: dict[str, Any] = {}
            example: dict[str, Any] | None = None
            request_body = op.get("requestBody")
            if isinstance(request_body, dict):
                content = request_body.get("content", {})
                json_ct = content.get("application/json", {})
                raw_schema = json_ct.get("schema", {})
                if isinstance(raw_schema, dict):
                    example = _extract_example(raw_schema)
                    body_schema = resolver.resolve(raw_schema)
                    if json_ct.get("example") and example is None and isinstance(
                        json_ct["example"], dict
                    ):
                        example = json_ct["example"]

            # Merge body schema + parameter properties into one input schema.
            if body_schema.get("type") == "object" or "properties" in body_schema:
                input_schema: dict[str, Any] = {
                    "type": "object",
                    "properties": dict(body_schema.get("properties", {})),
                }
                req = list(body_schema.get("required", []) or [])
            else:
                # Body isn't a plain object (or absent) — start empty.
                input_schema = {"type": "object", "properties": {}}
                req = []

            input_schema["properties"].update(param_props)
            req.extend(param_required)
            if req:
                input_schema["required"] = sorted(set(req))
            # Allow extra fields — the XSIAM API evolves faster than the spec.
            input_schema.setdefault("additionalProperties", True)

            # Unique tool name (append method if a path has multiple ops).
            tool_name = stem if len(methods) == 1 else f"{stem}_{method}"

            operations.append(
                Operation(
                    name=tool_name,
                    method=method.lower(),
                    path=path,
                    summary=(op.get("summary") or "").strip(),
                    description=(op.get("description") or "").strip(),
                    tags=tuple(op.get("tags", []) or []),
                    input_schema=input_schema,
                    path_params=tuple(path_names),
                    query_params=tuple(query_names),
                    example=example,
                )
            )

    # Final uniqueness guard (defensive — names should already be unique).
    seen: dict[str, int] = {}
    for op in operations:
        if op.name in seen:
            seen[op.name] += 1
            op.name = f"{op.name}_{seen[op.name]}"
        else:
            seen[op.name] = 1

    return operations


def filter_operations(
    operations: list[Operation], toolsets: tuple[str, ...]
) -> list[Operation]:
    """Restrict operations to the given OpenAPI tags (case-insensitive).

    Empty ``toolsets`` means expose everything."""
    if not toolsets:
        return operations
    wanted = {t.lower() for t in toolsets}
    return [op for op in operations if any(tag.lower() in wanted for tag in op.tags)]
