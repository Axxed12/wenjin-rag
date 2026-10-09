"""Account-mode MCP private-network and exact proxy trust configuration."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.integration


class _Server:
    def __init__(self):
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs


def _configure(monkeypatch, identity_db):
    monkeypatch.setattr(sys, "argv", ["wenjin.mcp.server", "--host", "0.0.0.0"])
    monkeypatch.setenv("WENJIN_IDENTITY_DB", str(identity_db))
    monkeypatch.setenv("WENJIN_MCP_ALLOWED_HOSTS", "proxy.test:443")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.30.250.2")
    monkeypatch.delenv("WENJIN_MCP_ALLOW_PRIVATE_HTTP", raising=False)
    monkeypatch.delenv("WENJIN_TLS_CERT_FILE", raising=False)
    monkeypatch.delenv("WENJIN_TLS_KEY_FILE", raising=False)
    monkeypatch.delenv("WENJIN_MCP_API_KEY", raising=False)


def test_account_mcp_rejects_private_plain_http_by_default(tmp_path, monkeypatch):
    from wenjin.mcp import server

    _configure(monkeypatch, tmp_path / "identity.sqlite")
    with pytest.raises(ValueError, match="必须配置TLS证书"):
        server.main()


def test_account_mcp_private_network_http_requires_explicit_opt_in_and_exact_proxy(
    tmp_path, monkeypatch,
):
    from wenjin.mcp import server

    identity_db = tmp_path / "identity.sqlite"
    _configure(monkeypatch, identity_db)
    monkeypatch.setenv("WENJIN_MCP_ALLOW_PRIVATE_HTTP", "1")
    running = _Server()
    monkeypatch.setattr(server, "build_mcp_server", lambda **_kwargs: running)

    server.main()

    assert running.run_kwargs is not None
    assert running.run_kwargs["host"] == "0.0.0.0"
    assert running.run_kwargs["uvicorn_config"] == {
        "proxy_headers": True,
        "forwarded_allow_ips": "172.30.250.2",
    }


def test_account_mcp_private_network_http_rejects_wildcard_forwarder(tmp_path, monkeypatch):
    from wenjin.mcp import server

    _configure(monkeypatch, tmp_path / "identity.sqlite")
    monkeypatch.setenv("WENJIN_MCP_ALLOW_PRIVATE_HTTP", "1")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with pytest.raises(ValueError, match="wildcard is forbidden"):
        server.main()


def test_account_managed_bridge_needs_its_own_private_http_opt_in(monkeypatch):
    from wenjin.mcp.managed_client import ManagedFileHTTPClient, ManagedFileServiceError

    monkeypatch.setenv("WENJIN_FILE_MANAGEMENT_URL", "http://management:8777")
    monkeypatch.setenv("WENJIN_MCP_ALLOW_PRIVATE_HTTP", "1")
    monkeypatch.delenv("WENJIN_FILE_MANAGEMENT_ALLOW_PRIVATE_HTTP", raising=False)
    with pytest.raises(ManagedFileServiceError, match="桥接配置无效"):
        ManagedFileHTTPClient.from_environment(account_mode=True)

    monkeypatch.setenv("WENJIN_FILE_MANAGEMENT_ALLOW_PRIVATE_HTTP", "1")
    client = ManagedFileHTTPClient.from_environment(account_mode=True)
    try:
        assert client.base_url == "http://management:8777"
        assert client.account_mode is True
    finally:
        client.close()
