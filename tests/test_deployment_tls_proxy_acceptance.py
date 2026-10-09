"""Optional live acceptance through an isolated local TLS reverse proxy.

Only runs when deployment-owner-provisioned synthetic endpoints and credentials are
explicitly supplied via environment. The host check prevents accidental WAN tests.
"""

from __future__ import annotations

import json
import os
import ssl
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.live]

_BASE_URL = os.getenv("WENJIN_PROXY_TEST_URL", "").rstrip("/")
_CA_FILE = os.getenv("WENJIN_PROXY_TEST_CA", "")
_USERNAME = os.getenv("WENJIN_PROXY_TEST_USERNAME", "")
_PASSWORD = os.getenv("WENJIN_PROXY_TEST_PASSWORD", "")
_READY = bool(_BASE_URL and _CA_FILE and _USERNAME and _PASSWORD)


def _same_origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _rpc_result(response: httpx.Response) -> dict:
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError("MCP响应未包含JSON-RPC消息")


@pytest.mark.skipif(not _READY, reason="需要部署owner提供的隔离TLS代理环境变量")
def test_tls_proxy_preserves_origin_secure_cookie_and_stateful_mcp_session():
    parsed = urlsplit(_BASE_URL)
    assert parsed.scheme == "https"
    assert parsed.hostname in {"localhost", "127.0.0.1", "::1"}, "仅允许本机隔离代理"
    assert os.path.isfile(_CA_FILE)

    origin = _same_origin(_BASE_URL)
    verify = ssl.create_default_context(cafile=_CA_FILE)
    with httpx.Client(base_url=_BASE_URL, verify=verify, timeout=10.0, trust_env=False) as client:
        wrong_origin = client.post(
            "/api/auth/login",
            headers={"Origin": f"http://{parsed.netloc}"},
            json={"username": _USERNAME, "password": _PASSWORD},
        )
        assert wrong_origin.status_code == 403
        assert "wenjin_session" not in client.cookies

        login = client.post(
            "/api/auth/login",
            headers={"Origin": origin},
            json={"username": _USERNAME, "password": _PASSWORD},
        )
        assert login.status_code == 200
        set_cookie = login.headers.get("set-cookie", "").lower()
        assert "wenjin_session=" in set_cookie
        assert "secure" in set_cookie
        assert "httponly" in set_cookie
        assert client.cookies.get("wenjin_session")
        csrf = login.json()["csrf_token"]

        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["mode"] == "accounts"

        issued = client.post(
            "/api/auth/credentials",
            headers={"Origin": origin, "X-CSRF-Token": csrf},
            json={"label": "isolated proxy acceptance", "expires_in_seconds": 3600},
        )
        assert issued.status_code == 201
        bearer = issued.json()["credential"]["token"]
        assert isinstance(bearer, str) and bearer

        rpc_headers = {
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        initialize = client.post(
            "/mcp",
            headers=rpc_headers,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "proxy-contract", "version": "1"},
                },
            },
        )
        assert initialize.status_code == 200
        session_id = initialize.headers.get("mcp-session-id")
        assert session_id
        init_result = _rpc_result(initialize)
        assert init_result["result"]["protocolVersion"] == "2025-11-25"

        session_headers = {**rpc_headers, "MCP-Session-Id": session_id}
        initialized = client.post(
            "/mcp", headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized.status_code in {200, 202}

        tools = client.post(
            "/mcp", headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert tools.status_code == 200
        result = _rpc_result(tools)
        assert result["id"] == 2
        assert isinstance(result["result"]["tools"], list)
