"""Independent contracts for remote MCP identity failure and token boundaries."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.testclient import TestClient

from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.management_http.app import create_app
from wenjin.mcp.remote_identity import RemoteIdentityStore

_ADMIN_PASSWORD = "Bridge_Validation_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Bridge_Validation_Reader_Temp_2026!"
_READER_PASSWORD = "Bridge_Validation_Reader_Ready_2026!"
pytestmark = pytest.mark.integration


class _EmptyManager:
    def resume_pending_jobs(self):
        return []

    def list_files(self):
        return []


def test_two_credentials_for_one_account_are_checked_independently_and_cookie_is_not_fallback(tmp_path):
    identity = IdentityStore(tmp_path / "data" / "identity.sqlite")
    identity.initialize()
    identity.bootstrap_first_admin("bridge-admin", _ADMIN_PASSWORD)
    admin_session = identity.login("bridge-admin", _ADMIN_PASSWORD)
    admin = admin_session.principal
    identity.create_user(admin, "bridge-reader", _READER_TEMP_PASSWORD, role="reader")
    first_login = identity.login("bridge-reader", _READER_TEMP_PASSWORD)
    identity.change_password(first_login.token, _READER_TEMP_PASSWORD, _READER_PASSWORD)
    reader_session = identity.login("bridge-reader", _READER_PASSWORD)
    reader = reader_session.principal
    first = identity.issue_mcp_credential(reader, label="first")
    second = identity.issue_mcp_credential(reader, label="second")
    identity.synchronize_resource("library", "bridge-library", active=True)
    identity.set_grant(
        admin,
        resource_kind="library",
        resource_id="bridge-library",
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )

    app = create_app(tmp_path, manager=_EmptyManager(), identity_db=identity.db_path)
    with TestClient(app) as management:
        # Even a valid administrator browser session is not accepted by the MCP bridge.
        cookie_only = management.get(
            "/api/mcp/identity/snapshot",
            cookies={"wenjin_session": admin_session.token},
        )
        assert cookie_only.status_code == 401

        def relay(request: httpx.Request) -> httpx.Response:
            response = management.get(
                str(request.url).removeprefix("http://management.test"),
                headers=dict(request.headers),
            )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
                request=request,
            )

        client = httpx.Client(transport=httpx.MockTransport(relay), follow_redirects=False)
        remote = RemoteIdentityStore(
            "http://management.test",
            allow_private_http=True,
            client=client,
        )
        assert remote.authenticate_request(first.token)[1].allows("library", "bridge-library")
        assert remote.authenticate_request(second.token)[1].allows("library", "bridge-library")

        # Revoking one token does not accidentally revoke or cache by user ID for the other.
        identity.revoke_mcp_credential(reader, first.credential_id)
        from wenjin.identity.policy import AuthenticationFailed

        with pytest.raises(AuthenticationFailed):
            remote.authenticate_request(first.token)
        assert remote.authenticate_request(second.token)[1].allows("library", "bridge-library")
        remote.close()


@pytest.mark.parametrize(("status", "expected"), [(401, 401), (500, 503)])
def test_remote_authority_failures_stop_request_without_local_fallback(status, expected):
    token = "wjm_" + "b" * 43
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, request=request, json={"error": "opaque"})
        ),
        follow_redirects=False,
    )
    remote = RemoteIdentityStore("http://management.test", allow_private_http=True, client=client)
    reached_downstream = []

    async def downstream(scope, receive, send):
        del scope, receive
        reached_downstream.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = McpIdentityMiddleware(downstream, identity_store=remote)

    async def run_request():
        sent = []
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
            "state": {},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        return sent

    try:
        response = asyncio.run(run_request())
        assert response[0]["status"] == expected
        assert not reached_downstream
        body = response[1].get("body", b"")
        assert token.encode("ascii") not in body
    finally:
        remote.close()


def test_remote_cli_rejects_static_admin_bearer_fallback(monkeypatch):
    from wenjin.mcp.stateless_server import main_stateless

    monkeypatch.setattr("sys.argv", ["wenjin-mcp-stateless"])
    monkeypatch.setenv("WENJIN_MCP_IDENTITY_MODE", "remote")
    monkeypatch.delenv("WENJIN_IDENTITY_DB", raising=False)
    monkeypatch.setenv("WENJIN_MCP_API_KEY", "fixed-admin-fallback")
    with pytest.raises(ValueError, match="不能同时配置本地身份数据库或静态API key"):
        main_stateless()
