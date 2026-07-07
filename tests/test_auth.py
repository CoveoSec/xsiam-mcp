import hashlib

import pytest

from xsiam_mcp.config import Config, ConfigError


def _base_env(monkeypatch):
    monkeypatch.setenv("XSIAM_API_KEY", "secret-key")
    monkeypatch.setenv("XSIAM_API_KEY_ID", "42")
    monkeypatch.delenv("XSIAM_BASE_URL", raising=False)
    monkeypatch.delenv("XSIAM_AUTH_TYPE", raising=False)


def test_fqdn_becomes_base_url(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("XSIAM_FQDN", "myco.xdr.us.paloaltonetworks.com")
    cfg = Config.from_env()
    assert cfg.base_url == "https://api-myco.xdr.us.paloaltonetworks.com"


def test_base_url_overrides_fqdn(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("XSIAM_FQDN", "myco.xdr.us.paloaltonetworks.com")
    monkeypatch.setenv("XSIAM_BASE_URL", "https://api-custom.example.com/")
    cfg = Config.from_env()
    assert cfg.base_url == "https://api-custom.example.com"


def test_missing_credentials_raise_actionable_error(monkeypatch):
    monkeypatch.delenv("XSIAM_API_KEY", raising=False)
    monkeypatch.delenv("XSIAM_API_KEY_ID", raising=False)
    monkeypatch.delenv("XSIAM_FQDN", raising=False)
    monkeypatch.delenv("XSIAM_BASE_URL", raising=False)
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    msg = str(exc.value)
    assert "XSIAM_API_KEY" in msg
    assert "XSIAM_API_KEY_ID" in msg
    assert "XSIAM_FQDN" in msg


def test_standard_auth_sends_key_directly(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("XSIAM_FQDN", "t.example.com")
    cfg = Config.from_env()
    headers = cfg.auth_headers()
    assert headers["Authorization"] == "secret-key"
    assert headers["x-xdr-auth-id"] == "42"
    assert "x-xdr-nonce" not in headers


def test_advanced_auth_signature_is_sha256(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("XSIAM_FQDN", "t.example.com")
    monkeypatch.setenv("XSIAM_AUTH_TYPE", "advanced")
    cfg = Config.from_env()
    nonce = "n" * 64
    ts = 1700000000000
    headers = cfg.auth_headers(_nonce=nonce, _timestamp=ts)
    expected = hashlib.sha256(f"secret-key{nonce}{ts}".encode()).hexdigest()
    assert headers["Authorization"] == expected
    assert headers["x-xdr-nonce"] == nonce
    assert headers["x-xdr-timestamp"] == str(ts)


def test_invalid_auth_type_rejected(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("XSIAM_FQDN", "t.example.com")
    monkeypatch.setenv("XSIAM_AUTH_TYPE", "bogus")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_toolsets_parsed(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("XSIAM_FQDN", "t.example.com")
    monkeypatch.setenv("XSIAM_TOOLSETS", "Incidents, XQL query ,Response Action")
    cfg = Config.from_env()
    assert cfg.toolsets == ("Incidents", "XQL query", "Response Action")
