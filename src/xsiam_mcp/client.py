"""Thin async HTTP transport for the XSIAM REST API.

Responsibilities:
  * inject auth headers on every request (standard or advanced keys),
  * build the correct URL from an Operation (path templating + query params),
  * split tool arguments into path / query / body,
  * turn HTTP + transport errors into readable text instead of tracebacks.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Config
from .parsing import loads_json, reply_of
from .spec import Operation


class XsiamError(RuntimeError):
    """A request-level failure that should be surfaced to the agent as text."""


class XsiamClient:
    def __init__(self, config: Config, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(
            base_url=config.base_url,
            timeout=config.timeout,
            verify=config.verify_tls,
        )

    async def aclose(self) -> None:
        if not self._external_client:
            await self._client.aclose()

    # ---- argument routing -------------------------------------------------

    @staticmethod
    def split_arguments(
        op: Operation, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Return (url_path, query_params, body) for the given tool arguments."""
        args = dict(arguments or {})
        query: dict[str, Any] = {}
        for name in op.query_params:
            if name in args:
                query[name] = args.pop(name)

        path = op.path
        for name in op.path_params:
            if name in args:
                value = args.pop(name)
                path = path.replace("{" + name + "}", str(value))
        return path, query, args

    # ---- request ----------------------------------------------------------

    async def call(self, op: Operation, arguments: dict[str, Any]) -> str:
        """Execute an operation and return the response body as text."""
        url_path, query, body = self.split_arguments(op, arguments)
        headers = self.config.auth_headers()

        try:
            request_kwargs: dict[str, Any] = {"headers": headers, "params": query or None}
            if op.method != "get":
                request_kwargs["json"] = body
            response = await self._client.request(op.method.upper(), url_path, **request_kwargs)
        except httpx.TimeoutException as exc:
            raise XsiamError(
                f"Request to {url_path} timed out after {self.config.timeout}s: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise XsiamError(f"Network error calling {url_path}: {exc}") from exc

        return self._format_response(response, url_path)

    @staticmethod
    def _format_response(response: httpx.Response, url_path: str) -> str:
        payload = loads_json(response.text)
        pretty = (
            json.dumps(payload, indent=2, ensure_ascii=False)
            if payload is not None
            else response.text
        )

        if response.is_success:
            return pretty

        # Error: give the agent the status, the XSIAM error message, and the body.
        hint = _STATUS_HINTS.get(response.status_code, "")
        message = ""
        if isinstance(payload, dict):
            reply = reply_of(payload)
            if isinstance(reply, dict):
                message = reply.get("err_msg") or reply.get("error") or ""
            message = message or payload.get("err_msg") or payload.get("error") or ""

        parts = [f"HTTP {response.status_code} calling {url_path}."]
        if hint:
            parts.append(hint)
        if message:
            parts.append(f"XSIAM says: {message}")
        parts.append(f"Response body:\n{pretty}")
        raise XsiamError(" ".join(parts[:2]) + "\n" + "\n".join(parts[2:]))


_STATUS_HINTS = {
    400: "Bad request — check the request_data body shape against the example.",
    401: "Unauthorized — verify XSIAM_API_KEY / XSIAM_API_KEY_ID and auth type (standard vs advanced).",
    402: "License or quota issue on the tenant.",
    403: "Forbidden — the API key's role lacks permission for this operation.",
    404: "Not found — check the FQDN/base URL and the resource id.",
    429: "Rate limited — slow down and retry.",
    500: "Internal server error on the XSIAM side.",
}
