"""Loopback TCP MCP + management bridge contract on isolated release assets."""

from __future__ import annotations

import hashlib
import json
import socket
import time
from threading import RLock, Thread
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from starlette.middleware import Middleware

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.file_management import FileManagementService
from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.index_registry import IndexTarget
from wenjin.management_http.app import create_app
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import RuntimeIdentity, create_release_resolver
from wenjin.mcp.server import build_mcp_server
from wenjin.releases import ReleaseCatalog, ReleaseService
from wenjin.releases.resolver import release_catalog_path

pytestmark = pytest.mark.integration
_ADMIN_PASSWORD = "Tcp_Release_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Tcp_Release_Reader_Temporary_2026!"
_READER_PASSWORD = "Tcp_Release_Reader_Ready_2026!"


class _MarkerEmbedder:
    dimension = 8

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            int(hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:4], 16) / 65535.0
            for index in range(8)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


class _UnusedSearchService:
    supported_version = "1.0"
    index_target_id = "synthetic-tcp-release-service"

    def search(self, *args, **kwargs):
        raise AssertionError("shared release search should use RequestReleaseResolver")


def _start_asgi_tcp(app) -> tuple[uvicorn.Server, Thread, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="critical",
            access_log=False,
            lifespan="on",
        )
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("temporary loopback ASGI server did not start")
    return server, thread, f"http://127.0.0.1:{port}"


def _stop_asgi_tcp(server: uvicorn.Server, thread: Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive(), "temporary loopback ASGI server failed to stop"


def _rpc_body(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    data = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert data, response.text
    return json.loads(data[-1])


def _rpc_call(client, *, session_id: str, bearer: str, request_id: int,
              name: str, arguments: dict) -> dict:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {bearer}",
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
    envelope = _rpc_body(response)
    result = envelope["result"]
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content", []):
        if block.get("type") == "text":
            decoded = json.loads(block["text"])
            if isinstance(decoded, dict):
                return decoded
    raise AssertionError(f"MCP result had no structured data: {result}")


def _download_path(evidence: dict) -> str:
    path = evidence["download_path"]
    assert urlsplit(path).path.startswith("/files/")
    query = parse_qs(urlsplit(path).query)
    assert len(query.get("release_id", [])) == 1
    return path


def test_real_tcp_mcp_release_query_download_revoke_and_model_gate(tmp_path):
    root = tmp_path / "tcp-release-project"
    assets = build_two_version_release_fixture(root=root, library_id="osg")
    qdrant = assets["qdrant"]
    embedder = assets["embedder"]
    expected_model = assets["targets"]["A"].embedding_model
    catalog = ReleaseCatalog(release_catalog_path(root))
    release_service = ReleaseService(root, catalog, qdrant)
    release_a = release_service.prepare("osg", assets["targets"]["A"], reason="TCP A")
    release_b = release_service.prepare("osg", assets["targets"]["B"], reason="TCP B")
    wrong_model_target = IndexTarget.from_dict({
        **assets["targets"]["B"].__dict__,
        "target_id": "synthetic-osg-same-dimension-wrong-model",
        "embedding_model": "synthetic-osg-different-model-v2",
    })
    release_wrong_model = release_service.prepare(
        "osg", wrong_model_target, reason="TCP wrong model candidate"
    )
    active_a = release_service.activate(
        "osg",
        release_a["release_id"],
        expected_revision=0,
        expected_embedding_model=expected_model,
        reason="activate TCP release A",
    )

    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=embedder,
        embedding_model=expected_model,
    )
    identity_path = root / "data" / "identity" / "identity.sqlite"
    identity_store = IdentityStore(identity_path)
    identity_store.initialize()
    identity_store.bootstrap_first_admin("tcp-admin", _ADMIN_PASSWORD)
    admin = identity_store.login("tcp-admin", _ADMIN_PASSWORD).principal
    identity_store.create_user(admin, "tcp-reader", _READER_TEMP_PASSWORD, role="reader")
    temporary_reader = identity_store.login("tcp-reader", _READER_TEMP_PASSWORD)
    identity_store.change_password(
        temporary_reader.token, _READER_TEMP_PASSWORD, _READER_PASSWORD
    )
    reader = identity_store.login("tcp-reader", _READER_PASSWORD).principal
    credential = identity_store.issue_mcp_credential(reader, label="tcp-release-reader")
    identity_store.synchronize_resource("library", "osg", active=True)
    grant = identity_store.set_grant(
        admin,
        resource_kind="library",
        resource_id="osg",
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )

    management_app = create_app(
        root,
        manager=manager,
        identity_db=identity_path,
    )
    observed_bridge = []

    async def observe_management(scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").startswith("/api/mcp/"):
            authorization = next(
                (value for key, value in scope.get("headers", [])
                 if key.lower() == b"authorization"),
                b"",
            )
            observed_bridge.append(
                (scope.get("path"), authorization == f"Bearer {credential.token}".encode())
            )
        await management_app(scope, receive, send)

    management_server = management_thread = None
    mcp_server = mcp_thread = None
    bridge_http = None
    admin_http = None
    mcp_http = None
    try:
        management_server, management_thread, management_url = _start_asgi_tcp(
            observe_management
        )
        bridge_http = httpx.Client(timeout=10)
        bridge = ManagedFileHTTPClient(
            management_url, "", account_mode=True, client=bridge_http
        )
        resolver = create_release_resolver(
            root=root,
            qdrant_client=qdrant,
            embedder=embedder,
            reranker=None,
            legacy_router=None,
            search_lock=RLock(),
            expected_embedding_model=expected_model,
        )
        mcp = build_mcp_server(
            _UnusedSearchService(),
            audit=SearchAudit(root / "artifacts" / "tcp-release-audit.jsonl"),
            access=AccessRegistry(None, {}),
            managed_bridge=bridge,
            raw_root=root / "raw_data",
            project_root=root,
            identity=RuntimeIdentity(
                corpus_id="osg-1.0",
                corpus_id_source="synthetic-release-catalog",
                target_id=assets["targets"]["A"].target_id,
                embedding_model=expected_model,
            ),
            release_resolver=resolver,
            identity_store=identity_store,
        )
        mcp_app = mcp.http_app(
            path="/mcp",
            middleware=[Middleware(McpIdentityMiddleware, identity_store=identity_store)],
            stateless_http=False,
            json_response=True,
        )
        mcp_server, mcp_thread, mcp_url = _start_asgi_tcp(mcp_app)
        admin_http = httpx.Client(base_url=management_url, timeout=10)
        mcp_http = httpx.Client(base_url=mcp_url, timeout=10)

        origin = management_url
        login = admin_http.post(
            "/api/auth/login",
            headers={"Origin": origin},
            json={"username": "tcp-admin", "password": _ADMIN_PASSWORD},
        )
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]
        admin_headers = {"Origin": origin, "X-CSRF-Token": csrf}

        initialize = mcp_http.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {credential.token}",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "tcp-contract", "version": "1"},
                },
            },
        )
        assert initialize.status_code == 200, initialize.text
        session_id = initialize.headers.get("Mcp-Session-Id")
        assert session_id
        initialized = mcp_http.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized.status_code == 202, initialized.text

        # 管理员通过真实TCP上传一份仅用于此测试的managed文件。
        managed_v1 = b"Synthetic managed network document marker managed_tcp_v1.\n"
        upload_v1 = admin_http.post(
            "/api/files",
            headers={**admin_headers, "X-File-Name": "managed-tcp-guide.txt"},
            content=managed_v1,
        )
        assert upload_v1.status_code == 202, upload_v1.text
        upload_job_id = upload_v1.json()["job_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            upload_job = admin_http.get(f"/api/jobs/{upload_job_id}")
            assert upload_job.status_code == 200, upload_job.text
            if upload_job.json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert upload_job.json()["status"] == "completed", upload_job.text
        managed_file_id = upload_job.json()["file_id"]
        managed_status = admin_http.get(f"/api/files/{managed_file_id}")
        assert managed_status.status_code == 200, managed_status.text
        managed_library_id = managed_status.json()["library_id"]
        identity_store.synchronize_resource("library", managed_library_id, active=True)
        identity_store.synchronize_resource("managed_file", managed_file_id, active=True)
        managed_grant = identity_store.set_grant(
            admin,
            resource_kind="managed_file",
            resource_id=managed_file_id,
            user_id=reader.user_id,
            allowed=True,
            expected_revision=0,
        )
        listed_managed = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=10,
            name="list_managed_files",
            arguments={},
        )
        assert any(item["file_id"] == managed_file_id for item in listed_managed["files"])
        managed_result_v1 = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=11,
            name="search_docs",
            arguments={"file_id": managed_file_id, "query": "managed_tcp_v1", "top_k": 1},
        )
        assert "managed_tcp_v1" in managed_result_v1["evidence"][0]["text"]
        managed_path_v1 = managed_result_v1["evidence"][0]["download_path"]
        managed_source_v1 = mcp_http.get(
            managed_path_v1,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert managed_source_v1.status_code == 200, managed_source_v1.text
        assert b"managed_tcp_v1" in managed_source_v1.content

        managed_v2 = b"Synthetic managed network replacement marker managed_tcp_v2.\n"
        replacement = admin_http.put(
            f"/api/files/{managed_file_id}",
            headers={**admin_headers, "X-File-Name": "managed-tcp-guide.txt"},
            content=managed_v2,
        )
        assert replacement.status_code == 202, replacement.text
        replace_job_id = replacement.json()["job_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            replace_job = admin_http.get(f"/api/jobs/{replace_job_id}")
            assert replace_job.status_code == 200, replace_job.text
            if replace_job.json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert replace_job.json()["status"] == "completed", replace_job.text
        stale_managed_source = mcp_http.get(
            managed_path_v1,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert stale_managed_source.status_code == 409, stale_managed_source.text
        assert b"managed_tcp_v1" not in stale_managed_source.content
        managed_result_v2 = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=12,
            name="search_docs",
            arguments={"file_id": managed_file_id, "query": "managed_tcp_v2", "top_k": 1},
        )
        assert "managed_tcp_v2" in managed_result_v2["evidence"][0]["text"]
        managed_path_v2 = managed_result_v2["evidence"][0]["download_path"]
        managed_source_v2 = mcp_http.get(
            managed_path_v2,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert managed_source_v2.status_code == 200, managed_source_v2.text
        assert b"managed_tcp_v2" in managed_source_v2.content

        # 同一个真实HTTP协议session先检索A并取件。
        result_a = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=2,
            name="search_docs",
            arguments={"library": "osg", "query": assets["queries"]["A"], "top_k": 1},
        )
        assert assets["expected_markers"]["A"] in result_a["evidence"][0]["text"]
        path_a = _download_path(result_a["evidence"][0])
        source_a = mcp_http.get(
            path_a,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert source_a.status_code == 200, source_a.text
        assert assets["expected_markers"]["A"] in source_a.text

        switched_b = admin_http.post(
            "/api/admin/libraries/osg/activate",
            headers=admin_headers,
            json={
                "release_id": release_b["release_id"],
                "expected_revision": active_a["revision"],
                "reason": "activate B through live admin HTTP",
            },
        )
        assert switched_b.status_code == 200, switched_b.text
        assert switched_b.json()["active_release_id"] == release_b["release_id"]

        result_b = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=3,
            name="search_docs",
            arguments={"library": "osg", "query": assets["queries"]["B"], "top_k": 1},
        )
        assert assets["expected_markers"]["B"] in result_b["evidence"][0]["text"]
        path_b = _download_path(result_b["evidence"][0])
        source_b = mcp_http.get(
            path_b,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert source_b.status_code == 200, source_b.text
        assert assets["expected_markers"]["B"] in source_b.text
        # A下载路径仍锁定其release，即使B已成为active。
        retained_a = mcp_http.get(
            path_a,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert retained_a.status_code == 200, retained_a.text
        assert assets["expected_markers"]["A"] in retained_a.text

        identity_store.set_grant(
            admin,
            resource_kind="managed_file",
            resource_id=managed_file_id,
            user_id=reader.user_id,
            allowed=False,
            expected_revision=managed_grant.revision,
        )
        managed_after_revoke = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=13,
            name="list_managed_files",
            arguments={},
        )
        assert all(item["file_id"] != managed_file_id for item in managed_after_revoke["files"])
        managed_search_after_revoke = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=14,
            name="search_docs",
            arguments={"file_id": managed_file_id, "query": "managed_tcp_v2", "top_k": 1},
        )
        assert managed_search_after_revoke["evidence"] == []
        managed_source_after_revoke = mcp_http.get(
            managed_path_v2,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert managed_source_after_revoke.status_code == 403

        rejected_wrong_model = admin_http.post(
            "/api/admin/libraries/osg/activate",
            headers=admin_headers,
            json={
                "release_id": release_wrong_model["release_id"],
                "expected_revision": switched_b.json()["revision"],
                "reason": "reject same-dimension wrong model through admin gate",
            },
        )
        assert rejected_wrong_model.status_code == 409, rejected_wrong_model.text
        current = admin_http.get("/api/admin/libraries/osg/releases")
        assert current.status_code == 200, current.text
        assert current.json()["active_release_id"] == release_b["release_id"]

        rolled_back = admin_http.post(
            "/api/admin/libraries/osg/rollback",
            headers=admin_headers,
            json={
                "expected_revision": switched_b.json()["revision"],
                "reason": "rollback to A through live admin HTTP",
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["active_release_id"] == release_a["release_id"]
        result_after_rollback = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=4,
            name="search_docs",
            arguments={"library": "osg", "query": assets["queries"]["A"], "top_k": 1},
        )
        assert assets["expected_markers"]["A"] in result_after_rollback["evidence"][0]["text"]

        identity_store.set_grant(
            admin,
            resource_kind="library",
            resource_id="osg",
            user_id=reader.user_id,
            allowed=False,
        expected_revision=grant.revision,
        )
        denied = _rpc_call(
            mcp_http,
            session_id=session_id,
            bearer=credential.token,
            request_id=5,
            name="search_docs",
            arguments={"library": "osg", "query": assets["queries"]["A"], "top_k": 1},
        )
        assert denied["evidence"] == []
        denied_source = mcp_http.get(
            path_b,
            headers={
                "Authorization": f"Bearer {credential.token}",
                "MCP-Session-Id": session_id,
            },
        )
        assert denied_source.status_code == 403, denied_source.text
        assert any(path == "/api/mcp/libraries" and authorized for path, authorized in observed_bridge)
        assert all(authorized for _path, authorized in observed_bridge)
    finally:
        if mcp_http is not None:
            mcp_http.close()
        if admin_http is not None:
            admin_http.close()
        if mcp_server is not None and mcp_thread is not None:
            _stop_asgi_tcp(mcp_server, mcp_thread)
        if management_server is not None and management_thread is not None:
            _stop_asgi_tcp(management_server, management_thread)
        if bridge_http is not None:
            bridge_http.close()
        qdrant.close()
