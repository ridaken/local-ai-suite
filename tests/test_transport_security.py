"""Tests for the /mcp DNS-rebinding allowlist builder.

Regression: the mcpo bridge connects to the gateway as `gateway:8090` over the
compose network. FastMCP's default localhost-only allowlist rejects that Host
header with HTTP 421, so the allowlist must be configurable and cover the
compose service name.
"""

import pytest

from mcp_gateway import config, server


def test_default_allowlist_covers_loopback_and_compose_service():
    hosts = config.MCP_ALLOWED_HOSTS
    assert "127.0.0.1:*" in hosts
    assert "gateway:*" in hosts  # the host mcpo uses inside the compose network


def test_transport_security_enables_protection_with_hosts(monkeypatch):
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["localhost:*", "gateway:*"])
    settings = server._transport_security()

    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["localhost:*", "gateway:*"]
    assert settings.allowed_origins == ["http://localhost:*", "http://gateway:*"]


def test_transport_security_rejects_wildcard(monkeypatch):
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["*"])
    with pytest.raises(ValueError, match="may not contain"):
        server._transport_security()


def test_gateway_host_would_be_accepted_by_middleware(monkeypatch):
    # Prove the actual middleware validation accepts gateway:8090 with our
    # settings — this is the exact check that returned 421 before the fix.
    from mcp.server.transport_security import TransportSecurityMiddleware

    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", ["127.0.0.1:*", "gateway:*"])
    mw = TransportSecurityMiddleware(server._transport_security())

    assert mw._validate_host("gateway:8090") is True
    assert mw._validate_host("127.0.0.1:8090") is True
    assert mw._validate_host("evil.example:8090") is False
