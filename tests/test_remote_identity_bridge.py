"""Remote identity bridge uses the authoritative account DB on every request."""

from __future__ import annotations

import asyncio
import hashlib
import sys

import httpx
import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from wenjin.file_management import FileManagementService
from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.identity.policy import AuthenticationFailed, Principal
from wenjin.identity.tokens import token_digest
from wenjin.management_http.app import create_app
from wenjin.mcp.remote_identity import RemoteAccessSnapshot, RemoteIdentityStore
from wenjin.mcp.stateless_server import main_stateless

pytestmark = pytest.mark.integration
_ADMIN_PASSWORD = "Remote_Bridge_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Remote_Bridge_Reader_Temp_2026!"
_READER_PASSWORD = "Remote_Bridge_Reader_Ready_2026!"


class _MarkerEmbedder:
    dimension = 16

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            int(hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:4], 16) / 65535.0
            for index in range(16)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def test_remote_identity_bridge_reflects_current_credentials_and_precise_file_acl(tmp_path):
    root = tmp_path / "remote-identity-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model="synthetic-remote-identity-v1",
    )
    source = tmp_path / "remote-managed-guide.txt"
    source.write_text("Remote identity bridge synthetic file.\n", encoding="utf-8")
    job = manager.submit_import(
        source,
        display_name="remote-managed-guide.txt",
        library_id="remote-bridge-library",
        library_name="Remote Bridge Library",
    )
    assert manager.run_job(job.job_id).status == "completed"
    managed_file = manager.get_file(job.file_id)

    identity = IdentityStore(root / "data" / "identity" / "identity.sqlite")
    identity.initialize()
    identity.bootstrap_first_admin("remote-admin", _ADMIN_PASSWORD)
    admin = identity.login("remote-admin", _ADMIN_PASSWORD).principal
    reader_user = identity.create_user(admin, "remote-reader", _READER_TEMP_PASSWORD, role="reader")
    first = identity.login("remote-reader", _READER_TEMP_PASSWORD)
    identity.change_password(first.token, _READER_TEMP_PASSWORD, _READER_PASSWORD)
    reader = identity.login("remote-reader", _READER_PASSWORD).principal
    admin_credential = identity.issue_mcp_credential(admin, label="remote-admin")
    reader_credential = identity.issue_mcp_credential(reader, label="remote-reader")
    identity.synchronize_resource("library", managed_file.library_id, active=True)
    identity.synchronize_resource("managed_file", managed_file.file_id, active=True)
    identity.synchronize_resource("library", "remote-hidden-library", active=True)
    identity.set_grant(
        admin,
        resource_kind="library",
        resource_id=managed_file.library_id,
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )

    app = create_app(root, manager=manager, identity_db=identity.db_path)
    try:
        with TestClient(app) as management:
            # A logged-in browser cookie is not an identity-bridge credential.
            cookie_only = management.get(
                "/api/mcp/identity/snapshot", cookies={"wenjin_session": "some-session"}
            )
            assert cookie_only.status_code == 401

            reader_response = management.get(
                "/api/mcp/identity/snapshot",
                headers={"Authorization": f"Bearer {reader_credential.token}"},
            )
            assert reader_response.status_code == 200, reader_response.text
            assert reader_response.headers["cache-control"] == "no-store"
            payload = reader_response.json()
            assert payload["principal"]["user_id"] == reader.user_id
            assert payload["access"]["readable_resources"] == [
                {
                    "resource_kind": "library",
                    "resource_id": managed_file.library_id,
                }
            ]
            assert payload["access"]["readable_managed_files"] == [
                {
                    "library_id": managed_file.library_id,
                    "file_id": managed_file.file_id,
                }
            ]
            assert "grants" not in payload["access"]
            assert reader_user.user_id not in str(payload["access"])
            assert reader_credential.token not in reader_response.text
            assert token_digest(reader_credential.token) not in reader_response.text
            assert "remote-hidden-library" not in reader_response.text

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
            principal, snapshot = remote.authenticate_request(reader_credential.token)
            assert principal.user_id == reader.user_id
            assert snapshot.allows("library", managed_file.library_id)
            assert snapshot.allows_managed_file(managed_file.library_id, managed_file.file_id)
            assert not snapshot.allows("library", "remote-hidden-library")

            # Per-file deny overrides inherited library allow on the very next request.
            identity.set_grant(
                admin,
                resource_kind="managed_file",
                resource_id=managed_file.file_id,
                user_id=reader.user_id,
                allowed=False,
                expected_revision=0,
            )
            _, denied_snapshot = remote.authenticate_request(reader_credential.token)
            assert denied_snapshot.allows("library", managed_file.library_id)
            assert not denied_snapshot.allows_managed_file(
                managed_file.library_id, managed_file.file_id
            )

            # Revoking a grant updates the next request without restarting the adapter.
            identity.set_grant(
                admin,
                resource_kind="library",
                resource_id=managed_file.library_id,
                user_id=reader.user_id,
                allowed=False,
                expected_revision=1,
            )
            _, revoked_grant_snapshot = remote.authenticate_request(reader_credential.token)
            assert not revoked_grant_snapshot.allows("library", managed_file.library_id)
            assert managed_file.library_id not in str(
                [item for item in revoked_grant_snapshot.readable_resources]
            )

            # An admin can read the active file despite reader-specific denial.
            _, admin_snapshot = remote.authenticate_request(admin_credential.token)
            assert admin_snapshot.allows_managed_file(managed_file.library_id, managed_file.file_id)

            # Revoking the exact Bearer takes effect on the next identity-bridge call.
            identity.revoke_mcp_credential(reader, reader_credential.credential_id)
            with pytest.raises(AuthenticationFailed):
                remote.authenticate_request(reader_credential.token)

            # Disabled users and users forced to change password fail closed too.
            disabled_user = identity.create_user(
                admin, "remote-disabled", _READER_TEMP_PASSWORD, role="reader"
            )
            disabled_first = identity.login("remote-disabled", _READER_TEMP_PASSWORD)
            identity.change_password(disabled_first.token, _READER_TEMP_PASSWORD, _READER_PASSWORD)
            disabled = identity.login("remote-disabled", _READER_PASSWORD).principal
            disabled_credential = identity.issue_mcp_credential(disabled, label="disabled")
            disabled_record = next(
                item for item in identity.list_users(admin) if item.user_id == disabled_user.user_id
            )
            identity.set_user_active(
                admin, disabled_user.user_id, False, expected_revision=disabled_record.revision
            )
            with pytest.raises(AuthenticationFailed):
                remote.authenticate_request(disabled_credential.token)

            identity.create_user(admin, "remote-must-change", _READER_TEMP_PASSWORD, role="reader")
            changed_first = identity.login("remote-must-change", _READER_TEMP_PASSWORD)
            identity.change_password(changed_first.token, _READER_TEMP_PASSWORD, _READER_PASSWORD)
            changed = identity.login("remote-must-change", _READER_PASSWORD).principal
            changed_credential = identity.issue_mcp_credential(changed, label="must-change")
            with identity._transaction(write=True) as connection:
                connection.execute(
                    "UPDATE users SET must_change_password=1 WHERE user_id=?",
                    (changed.user_id,),
                )
            with pytest.raises(AuthenticationFailed):
                remote.authenticate_request(changed_credential.token)
            remote.close()
    finally:
        manager.close()
        qdrant.close()


def test_remote_identity_middleware_loads_snapshot_for_each_http_request():
    class RotatingAuthority:
        calls = 0

        def authenticate_request(self, bearer):
            assert bearer == "wjm_" + "a" * 43
            self.calls += 1
            principal = Principal("user-stable", "reader", "reader", self.calls)
            visible = frozenset({("library", f"library-{self.calls}")})
            return principal, RemoteAccessSnapshot(principal, visible, frozenset())

    authority = RotatingAuthority()
    seen = []

    async def downstream(scope, receive, send):
        del receive
        identity = scope["state"]["wenjin_identity"]
        seen.append((identity.principal.revision, identity.access_snapshot))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = McpIdentityMiddleware(downstream, identity_store=authority)

    async def request_once():
        scope = {
            "type": "http",
            "method": "POST",
            "headers": [(b"authorization", ("Bearer wjm_" + "a" * 43).encode())],
            "state": {},
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        return sent[0]["status"]

    assert asyncio.run(request_once()) == 200
    assert asyncio.run(request_once()) == 200
    assert authority.calls == 2
    assert seen[0][1].allows("library", "library-1")
    assert not seen[0][1].allows("library", "library-2")
    assert seen[1][1].allows("library", "library-2")
    assert not seen[1][1].allows("library", "library-1")


def test_stateless_cli_rejects_mixed_remote_and_local_identity_configuration(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wenjin-mcp-stateless"])
    monkeypatch.setenv("WENJIN_MCP_IDENTITY_MODE", "remote")
    monkeypatch.setenv("WENJIN_IDENTITY_DB", "C:/must-not-be-opened/identity.sqlite")
    monkeypatch.delenv("WENJIN_MCP_API_KEY", raising=False)
    with pytest.raises(ValueError, match="不能同时配置本地身份数据库"):
        main_stateless()

    monkeypatch.delenv("WENJIN_IDENTITY_DB", raising=False)
    monkeypatch.setenv("WENJIN_MCP_IDENTITY_MODE", "remote")
    monkeypatch.setattr(sys, "argv", ["wenjin-mcp-stateless", "--identity-mode", "local"])
    with pytest.raises(ValueError, match="冲突"):
        main_stateless()
