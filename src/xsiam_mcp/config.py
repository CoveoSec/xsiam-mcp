"""Configuration and authentication for the XSIAM MCP server.

All settings are read from environment variables so the server can be launched
by any MCP client (Claude Desktop, Claude Code, etc.) without code changes and
without Docker.

Required:
    XSIAM_API_KEY       The API key *secret* (the long string).
    XSIAM_API_KEY_ID    The numeric key ID (sent as the x-xdr-auth-id header).
    XSIAM_FQDN          Your tenant FQDN, e.g. "myco.xdr.us.paloaltonetworks.com".
                        The base URL becomes https://api-<fqdn>.
                        (Alternatively set XSIAM_BASE_URL to the full gateway URL.)

Optional:
    XSIAM_BASE_URL      Full API gateway URL; overrides XSIAM_FQDN when set.
    XSIAM_AUTH_TYPE     "standard" (default) or "advanced".
    XSIAM_TIMEOUT       Per-request timeout in seconds (default 60).
    XSIAM_TOOLSETS      Comma-separated OpenAPI tag names to expose (default: all).
                        e.g. "Incidents,XQL query,Response Action".
    XSIAM_VERIFY_TLS    "false" to disable TLS verification (default: true).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        value = value.strip()
    return value if value else default


@dataclass
class Config:
    """Resolved runtime configuration."""

    api_key: str
    api_key_id: str
    base_url: str
    auth_type: str = "standard"
    timeout: float = 60.0
    toolsets: tuple[str, ...] = field(default_factory=tuple)
    verify_tls: bool = True

    # ---- construction -----------------------------------------------------

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config from the environment, raising ConfigError with an
        actionable message if anything required is missing."""
        api_key = _env("XSIAM_API_KEY")
        api_key_id = _env("XSIAM_API_KEY_ID")

        base_url = _env("XSIAM_BASE_URL")
        fqdn = _env("XSIAM_FQDN")
        if not base_url and fqdn:
            fqdn = fqdn.removeprefix("https://").removeprefix("http://").strip("/")
            # If the user pasted the api- gateway host directly, keep it as-is.
            base_url = fqdn if fqdn.startswith("api-") else f"api-{fqdn}"
            base_url = f"https://{base_url}"

        missing = []
        if not api_key:
            missing.append("XSIAM_API_KEY")
        if not api_key_id:
            missing.append("XSIAM_API_KEY_ID")
        if not base_url:
            missing.append("XSIAM_FQDN (or XSIAM_BASE_URL)")
        if missing:
            raise ConfigError(
                "Missing required XSIAM configuration: "
                + ", ".join(missing)
                + ". Set these environment variables in your MCP client config. "
                "See the README for how to obtain them from "
                "Settings → Configurations → Integrations → API Keys."
            )

        auth_type = (_env("XSIAM_AUTH_TYPE", "standard") or "standard").lower()
        if auth_type not in ("standard", "advanced"):
            raise ConfigError(
                f"XSIAM_AUTH_TYPE must be 'standard' or 'advanced', got {auth_type!r}."
            )

        try:
            timeout = float(_env("XSIAM_TIMEOUT", "60") or "60")
        except ValueError as exc:
            raise ConfigError(f"XSIAM_TIMEOUT must be a number: {exc}") from exc

        raw_toolsets = _env("XSIAM_TOOLSETS", "") or ""
        toolsets = tuple(
            t.strip() for t in raw_toolsets.split(",") if t.strip()
        )

        verify_tls = (_env("XSIAM_VERIFY_TLS", "true") or "true").lower() not in (
            "false",
            "0",
            "no",
        )

        return cls(
            api_key=api_key,
            api_key_id=api_key_id,
            base_url=base_url.rstrip("/"),
            auth_type=auth_type,
            timeout=timeout,
            toolsets=toolsets,
            verify_tls=verify_tls,
        )

    # ---- auth -------------------------------------------------------------

    def auth_headers(self, *, _nonce: str | None = None, _timestamp: int | None = None) -> dict[str, str]:
        """Return the HTTP auth headers for a request.

        Standard keys send the API key directly in Authorization.
        Advanced keys send a SHA-256 hex digest of api_key + nonce + timestamp,
        with the nonce and timestamp in their own headers.

        The private ``_nonce`` / ``_timestamp`` params exist only so tests can
        assert a deterministic signature; production calls generate fresh ones.
        """
        headers = {
            "x-xdr-auth-id": str(self.api_key_id),
            "Content-Type": "application/json",
        }

        if self.auth_type == "advanced":
            nonce = _nonce or secrets.token_hex(32)
            timestamp = _timestamp if _timestamp is not None else int(round(time.time() * 1000))
            payload = f"{self.api_key}{nonce}{timestamp}".encode("utf-8")
            signature = hashlib.sha256(payload).hexdigest()
            headers["Authorization"] = signature
            headers["x-xdr-nonce"] = nonce
            headers["x-xdr-timestamp"] = str(timestamp)
        else:
            headers["Authorization"] = self.api_key

        return headers
