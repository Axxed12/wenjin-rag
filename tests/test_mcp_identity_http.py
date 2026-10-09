"""Stateful MCP identity and managed bridge contracts on temp-only assets."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from qdrant_client import QdrantClient
from starlette.middleware import Middleware
from starlette.testclient import TestClient

import wenjin.identity.mcp_auth as mcp_auth_module
from wenjin.audit import SearchAudit
from wenjin.file_management import FileManagementService
from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.server import build_mcp_server

pytestmark = pytest.mark.integration
_ADMIN_PASSWORD = "Mcp_Http_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Mcp_Http_Reader_Temp_2026!"
_READER_PASSWORD = "Mcp_Http_Reader_Ready_2026!"


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


class _UnusedSearchService:
    supported_version = "0.1"
    index_target_id = "unused-mcp-identity-service"

    def search(self, *args, **kwargs):
        raise AssertionError("managed-file tools must not use the shared search service")


def _json_response(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://synthetic-management/"),
    )


def _rpc_json(response) -> dict:
    assert response.status_code == 200, response.text
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    events = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events, response.text
    return json.loads(events[-1])


def _call_tool(client, session_id: str, token: str, request_id: int, name: str, arguments: dict):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "MCP-Session-Id": session_id,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    return response, _rpc_json(response) if response.status_code == 200 else None


def _structured_tool_result(envelope: dict) -> dict:
    result = envelope["result"]
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                parsed = json.loads(block["text"])
            except (KeyError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError(f"MCP tool result has no structured object: {result}")


def test_stateful_mcp_identity_managed_search_acl_and_session_binding(tmp_path):
    root = tmp_path / "mcp-identity-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model="synthetic-mcp-identity-marker-v1",
    )
    source = tmp_path / "mcp-managed-guide.txt"
    source.write_text(
        "Synthetic MCP managed document with mcp_bridge_marker.\n",
        encoding="utf-8",
    )
    job = manager.submit_import(
        source,
        display_name="mcp-managed-guide.txt",
        library_id="mcp-identity-library",
        library_name="MCP Identity Library",
    )
    assert manager.run_job(job.job_id).status == "completed"
    file_record = manager.get_file(job.file_id)

    store = IdentityStore(root / "data" / "identity" / "identity.sqlite")
    store.initialize()
    store.bootstrap_first_admin("mcp-admin", _ADMIN_PASSWORD)
    admin = store.login("mcp-admin", _ADMIN_PASSWORD).principal
    store.create_user(admin, "mcp-reader", _READER_TEMP_PASSWORD, role="reader")
    first = store.login("mcp-reader", _READER_TEMP_PASSWORD)
    store.change_password(first.token, _READER_TEMP_PASSWORD, _READER_PASSWORD)
    reader = store.login("mcp-reader", _READER_PASSWORD).principal
    store.create_user(admin, "mcp-other", _READER_TEMP_PASSWORD, role="reader")
    other_first = store.login("mcp-other", _READER_TEMP_PASSWORD)
    store.change_password(other_first.token, _READER_TEMP_PASSWORD, _READER_PASSWORD)
    other = store.login("mcp-other", _READER_PASSWORD).principal
    credential = store.issue_mcp_credential(reader, label="mcp-stateful-reader")
    other_credential = store.issue_mcp_credential(other, label="mcp-stateful-other")
    store.synchronize_resource("library", file_record.library_id, active=True)
    store.synchronize_resource("managed_file", file_record.file_id, active=True)
    store.set_grant(
        admin,
        resource_kind="library",
        resource_id=file_record.library_id,
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )

    bridge_requests = []

    def management_handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        bridge_requests.append((request.method, urlsplit(str(request.url)).path, auth))
        assert auth.startswith("Bearer wjm_"), "bridge must receive current request's personal MCP token"
        principal = store.resolve_mcp_credential(auth.removeprefix("Bearer "))
        if request.url.path == "/api/mcp/libraries":
            allowed = store.can_read(principal, "library", file_record.library_id)
            return _json_response({
                "libraries": ([{
                    "library_id": file_record.library_id,
                    "library": "MCP Identity Library",
                    "sources": [],
                }] if allowed else []),
            })
        if request.url.path == "/api/mcp/files" and request.method == "GET":
            allowed = store.can_read_managed_file(
                principal, file_record.library_id, file_record.file_id
            )
            record = manager.get_file(file_record.file_id)
            files = [{
                "file_id": record.file_id,
                "library_id": record.library_id,
                "managed": True,
                "status": record.status,
                "display_name": record.display_name,
                "version": record.version,
                "active_version_id": record.active_version_id,
                "chunk_count": record.chunk_count,
                "updated_at": record.updated_at,
            }] if allowed else []
            return _json_response({"files": files})
        if request.url.path == "/api/mcp/search" and request.method == "POST":
            payload = json.loads(request.content)
            allowed = store.can_read_managed_file(
                principal, file_record.library_id, file_record.file_id
            )
            if not allowed:
                return _json_response({"error": "forbidden"}, status=403)
            result = manager.search(
                payload["file_id"],
                payload["query"],
                version_id=payload["version_id"],
                top_k=payload.get("top_k", 5),
                context_char_budget=payload.get("context_char_budget", 8000),
            )
            return _json_response(result)
        raise AssertionError(f"unexpected managed bridge request: {request.method} {request.url}")

    bridge_http = httpx.Client(transport=httpx.MockTransport(management_handler))
    bridge = ManagedFileHTTPClient(
        "https://synthetic-management", "", account_mode=True, client=bridge_http
    )
    audit = SearchAudit(root / "artifacts" / "audit" / "mcp-identity.jsonl")
    server = build_mcp_server(
        service=_UnusedSearchService(),
        audit=audit,
        project_root=root,
        identity=RuntimeIdentity("synthetic-corpus", "test-fixture", "synthetic-target"),
        managed_bridge=bridge,
        identity_store=store,
    )
    app = server.http_app(
        path="/mcp",
        middleware=[Middleware(McpIdentityMiddleware, identity_store=store)],
        stateless_http=False,
        json_response=True,
        transport="streamable-http",
    )

    try:
        with TestClient(app) as client:
            auth = {"Authorization": f"Bearer {credential.token}"}
            initialized = client.post(
                "/mcp",
                headers={**auth, "Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic-test", "version": "1"},
                    },
                },
            )
            initialize_envelope = _rpc_json(initialized)
            assert initialize_envelope["result"]["protocolVersion"] == "2025-11-25"
            session_id = initialized.headers.get("mcp-session-id")
            assert session_id

            notification = client.post(
                "/mcp",
                headers={
                    **auth,
                    "MCP-Session-Id": session_id,
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notification.status_code in {200, 202}, notification.text

            listed = client.post(
                "/mcp",
                headers={
                    **auth,
                    "MCP-Session-Id": session_id,
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            list_envelope = _rpc_json(listed)
            assert "list_managed_files" in {
                tool["name"] for tool in list_envelope["result"]["tools"]
            }

            files_response, files_envelope = _call_tool(
                client, session_id, credential.token, 3, "list_managed_files", {}
            )
            files = _structured_tool_result(files_envelope)
            assert files_response.status_code == 200
            assert [item["file_id"] for item in files["files"]] == [file_record.file_id]

            search_response, search_envelope = _call_tool(
                client,
                session_id,
                credential.token,
                4,
                "search_docs",
                {"file_id": file_record.file_id, "query": "mcp_bridge_marker", "top_k": 3},
            )
            search_result = _structured_tool_result(search_envelope)
            assert search_response.status_code == 200
            assert any("mcp_bridge_marker" in item.get("text", "") for item in search_result["evidence"])

            # New grants are read during each bridge request; the same session loses list/search immediately.
            store.set_grant(
                admin,
                resource_kind="library",
                resource_id=file_record.library_id,
                user_id=reader.user_id,
                allowed=False,
                expected_revision=1,
            )
            denied_list_response, denied_list_envelope = _call_tool(
                client, session_id, credential.token, 5, "list_managed_files", {}
            )
            denied_list = _structured_tool_result(denied_list_envelope)
            assert denied_list_response.status_code == 200
            assert denied_list["files"] == []
            denied_search_response, denied_search_envelope = _call_tool(
                client,
                session_id,
                credential.token,
                6,
                "search_docs",
                {"file_id": file_record.file_id, "query": "mcp_bridge_marker"},
            )
            denied_search = _structured_tool_result(denied_search_envelope)
            assert denied_search_response.status_code == 200
            assert denied_search["error"] == "file_not_available"
            assert denied_search["evidence"] == []

            # A different valid account cannot take over an already-bound MCP session.
            switched = client.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {other_credential.token}",
                    "MCP-Session-Id": session_id,
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}},
            )
            assert switched.status_code == 401, switched.text

            store.revoke_mcp_credential(reader, credential.credential_id)
            revoked = client.post(
                "/mcp",
                headers={
                    **auth,
                    "MCP-Session-Id": session_id,
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
            )
            assert revoked.status_code == 401, revoked.text
            assert bridge_requests
            assert all(request[2].startswith("Bearer wjm_") for request in bridge_requests)
    finally:
        bridge_http.close()
        qdrant.close()


def test_mcp_identity_middleware_spoof_duplicate_ttl_and_delete_lifecycle(
    tmp_path, monkeypatch
):
    store = IdentityStore(tmp_path / "middleware-identity.sqlite")
    store.initialize()
    store.bootstrap_first_admin("middleware-user", _ADMIN_PASSWORD)
    principal = store.login("middleware-user", _ADMIN_PASSWORD).principal
    token = store.issue_mcp_credential(principal, label="middleware-test").token

    fake_now = [100.0]
    monkeypatch.setattr(
        mcp_auth_module,
        "time",
        SimpleNamespace(monotonic=lambda: fake_now[0]),
    )
    next_session = ["session-initial"]
    delete_status = [500]
    downstream_scopes = []

    async def downstream(scope, receive, send):
        del receive
        downstream_scopes.append(scope)
        headers = []
        if scope["method"] == "initialize":
            headers.append((b"mcp-session-id", next_session[0].encode("ascii")))
            status = 200
        elif scope["method"] == "DELETE":
            status = delete_status[0]
        else:
            status = 200
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    middleware = McpIdentityMiddleware(downstream, identity_store=store)

    async def invoke(method, header_pairs):
        scope = {
            "type": "http",
            "method": method,
            "headers": list(header_pairs),
            "state": {},
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        return scope, sent[0]["status"], sent

    bearer = f"Bearer {token}".encode("ascii")
    initial_scope, status, _ = asyncio.run(
        invoke("initialize", [(b"authorization", bearer)])
    )
    assert status == 200
    assert next_session[0] in middleware._session_users
    assert initial_scope["state"]["principal"].user_id == principal.user_id

    spoof_scope, spoof_status, _ = asyncio.run(invoke(
        "POST",
        [
            (b"authorization", bearer),
            (b"mcp-session-id", b"session-initial"),
            (b"x-wenjin-user-id", b"forged-admin"),
            (b"x-wenjin-role", b"admin"),
        ],
    ))
    assert spoof_status == 200
    assert spoof_scope["state"]["principal"].user_id == principal.user_id
    assert all(not key.lower().startswith(b"x-wenjin-") for key, _ in spoof_scope["headers"])

    _, duplicate_status, _ = asyncio.run(invoke(
        "POST",
        [
            (b"authorization", bearer),
            (b"authorization", bearer),
            (b"mcp-session-id", b"session-initial"),
        ],
    ))
    assert duplicate_status == 401
    _, unknown_status, _ = asyncio.run(invoke(
        "POST",
        [(b"authorization", bearer), (b"mcp-session-id", b"unknown-session")],
    ))
    assert unknown_status == 401

    fake_now[0] += 86_401
    _, expired_status, _ = asyncio.run(invoke(
        "POST",
        [(b"authorization", bearer), (b"mcp-session-id", b"session-initial")],
    ))
    assert expired_status == 401
    assert "session-initial" not in middleware._session_users

    next_session[0] = "session-delete"
    _, init_delete_status, _ = asyncio.run(
        invoke("initialize", [(b"authorization", bearer)])
    )
    assert init_delete_status == 200
    delete_headers = [
        (b"authorization", bearer), (b"mcp-session-id", b"session-delete")
    ]
    _, failed_delete_status, _ = asyncio.run(invoke("DELETE", delete_headers))
    assert failed_delete_status == 500
    assert "session-delete" in middleware._session_users
    _, after_failed_delete_status, _ = asyncio.run(invoke("POST", delete_headers))
    assert after_failed_delete_status == 200

    delete_status[0] = 204
    _, success_delete_status, _ = asyncio.run(invoke("DELETE", delete_headers))
    assert success_delete_status == 204
    assert "session-delete" not in middleware._session_users
    _, after_success_delete_status, _ = asyncio.run(invoke("POST", delete_headers))
    assert after_success_delete_status == 401
