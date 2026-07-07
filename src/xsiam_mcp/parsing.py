"""Small shared helpers for the XSIAM response envelope.

XSIAM wraps every successful response in a top-level ``reply`` object (which is
sometimes a bare scalar, e.g. the XQL execution id). These helpers centralise
that contract so the client and the composite tools don't each re-implement it.
"""

from __future__ import annotations

import json
from typing import Any


def loads_json(text: str) -> Any | None:
    """Parse JSON text, returning None instead of raising on invalid input."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def reply_of(parsed: Any) -> Any:
    """Return the ``reply`` payload from a parsed response, or None."""
    if isinstance(parsed, dict):
        return parsed.get("reply")
    return None


def extract_execution_id(reply: Any) -> str | None:
    """Pull an XQL execution id from a ``reply`` payload.

    XSIAM returns the id either as a bare string (``"reply": "abc_123_inv"``)
    or nested (``"reply": {"execution_id": ...}`` / ``{"query_id": ...}``)."""
    if isinstance(reply, dict):
        value = reply.get("execution_id") or reply.get("query_id")
        return str(value) if value else None
    if reply:
        return str(reply)
    return None
