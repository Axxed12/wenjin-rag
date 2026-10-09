"""Synthetic account, grant, active-version and download contracts for MCP 2026."""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from threading import RLock
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
import pytest

try:
    if version("fastmcp").split(".", 1)[0] != "4" or version("mcp").split(".", 1)[0] != "2":
        pytest.skip("stateless contracts require FastMCP 4.x and MCP SDK 2.x", allow_module_level=True)
except PackageNotFoundError:
    pytest.skip("stateless contracts require FastMCP 4.x and MCP SDK 2.x", allow_module_level=True)

import uvicorn
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.file_management import FileManagementService
from wenjin.identity import IdentityStore
from wenjin.management_http.app import create_app as create_management_app
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.stateless_server import build_stateless_mcp_server, create_stateless_app
from wenjin.mcp.runtime import create_release_resolver
from wenjin.releases.catalog import ReleaseCatalog
from wenjin.releases.resolver import release_catalog_path
from wenjin.releases.service import ReleaseService
from tests.support.library_release_fixture import build_two_version_release_fixture


pytestmark = pytest.mark.integration
PROTOCOL = "2026-07-28"
ADMIN_PASSWORD = "StatelessSyntheticAdmin_2026!"
TEMP_PASSWORD = "StatelessSyntheticTemp_2026!"
READER_PASSWORD = "StatelessSyntheticReader_2026!"
SESSION_HEADER = "old-user-a-session-value"
CLIENT_SCRIPT = Path(__file__).parent / "acceptance" / "mcp-stateless-client" / "client.mjs"
CLIENT_CWD = CLIENT_SCRIPT.parent


class MarkerEmbedder:
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


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_loopback(app) -> tuple[uvicorn.Server, threading.Thread, str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, name=f"synthetic-http-{port}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            return server, thread, f"http://127.0.0.1:{port}"
        if not thread.is_alive():
            raise RuntimeError("synthetic loopback server exited before startup")
        time.sleep(0.025)
    server.should_exit = True
    thread.join(timeout=2)
    raise TimeoutError("synthetic loopback server did not start")


def _stop_loopback(server: uvicorn.Server | None, thread: threading.Thread | None) -> None:
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("synthetic loopback server did not stop")


def _reader(store: IdentityStore, admin, username: str):
    store.create_user(admin, username, TEMP_PASSWORD, role="reader")
    temporary = store.login(username, TEMP_PASSWORD)
    store.change_password(temporary.token, TEMP_PASSWORD, READER_PASSWORD)
    principal = store.login(username, READER_PASSWORD).principal
    credential = store.issue_mcp_credential(principal, label=f"synthetic-{username}")
    return principal, credential


def _sdk_call(
    *, url: str, token: str, tool: str, arguments: dict | None = None,
    session_id: str | None = None, expect_error: bool = False,
) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "MCP_STATELESS_URL": url,
        "MCP_STATELESS_TOKEN": token,
        "MCP_STATELESS_TOOL": tool,
        "MCP_STATELESS_ARGUMENTS_JSON": json.dumps(arguments or {}, separators=(",", ":")),
        "MCP_STATELESS_EXPECT_TOOL_ERROR": "true" if expect_error else "false",
    }
    if session_id is not None:
        env["MCP_STATELESS_SESSION_ID"] = session_id
    result = subprocess.run(
        ["node", str(CLIENT_SCRIPT)],
        cwd=CLIENT_CWD,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise AssertionError(f"official TS SDK call failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


def _request(client: TestClient, method: str, payload: dict, token: str, *, session=SESSION_HEADER):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    if method == "tools/call":
        headers["Mcp-Name"] = payload["params"]["name"]
    return client.post("/mcp-stateless", headers=headers, json=payload)


def _headers(method: str, *, name: str | None = None) -> dict[str, str]:
    headers = {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
    }
    if name:
        headers["Mcp-Name"] = name
    return headers


def test_stateless_mcp_rechecks_identity_grants_and_managed_versions(tmp_path):
    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()
    qdrant = QdrantClient(":memory:")
    shared = build_two_version_release_fixture(
        root=project_root,
        library_id="synthetic-shared",
        namespace="stateless",
        qdrant_client=qdrant,
    )
    manager = FileManagementService(
        root=project_root,
        qdrant_client=qdrant,
        embedder=shared["embedder"],
        embedding_model=shared["targets"]["A"].embedding_model,
    )
    shared_catalog = ReleaseCatalog(release_catalog_path(project_root))
    shared_release_service = ReleaseService(project_root, shared_catalog, qdrant)
    shared_a = shared_release_service.prepare(
        "synthetic-shared", shared["targets"]["A"], reason="stateless synthetic A"
    )
    shared_b = shared_release_service.prepare(
        "synthetic-shared", shared["targets"]["B"], reason="stateless synthetic B"
    )
    shared_active_a = shared_release_service.activate(
        "synthetic-shared",
        shared_a["release_id"],
        expected_revision=0,
        expected_embedding_model=shared["targets"]["A"].embedding_model,
        reason="activate synthetic release A",
    )
    identity_path = project_root / "data" / "identity" / "identity.sqlite"
    store = IdentityStore(identity_path)
    store.initialize()
    store.bootstrap_first_admin("stateless-admin", ADMIN_PASSWORD)
    admin = store.login("stateless-admin", ADMIN_PASSWORD).principal
    alice, alice_credential = _reader(store, admin, "stateless-alice")
    bob, bob_credential = _reader(store, admin, "stateless-bob")

    first_source = tmp_path / "managed-v1.txt"
    first_source.write_text(
        "Synthetic managed source version one contains ambermarker_v1.\n", encoding="utf-8"
    )
    first_job = manager.submit_import(
        first_source,
        display_name="synthetic-managed-guide.txt",
        library_id="synthetic-library",
        library_name="Synthetic Library",
    )
    assert manager.run_job(first_job.job_id).status == "completed"
    file_id = first_job.file_id
    version_a = manager.snapshot(file_id)

    store.synchronize_resource("library", "synthetic-library", active=True)
    store.synchronize_resource("managed_file", file_id, active=True)
    store.synchronize_resource("library", "synthetic-shared", active=True)
    grant_a = store.set_grant(
        admin,
        resource_kind="library",
        resource_id="synthetic-library",
        user_id=alice.user_id,
        allowed=True,
        expected_revision=0,
    )
    store.set_grant(
        admin,
        resource_kind="library",
        resource_id="synthetic-shared",
        user_id=alice.user_id,
        allowed=True,
        expected_revision=0,
    )

    management_app = create_management_app(
        project_root, manager=manager, identity_db=identity_path
    )
    observed_bridge: list[tuple[str, str]] = []

    def observe_bridge(request: httpx.Request) -> None:
        authorization = request.headers.get("authorization", "")
        if authorization == f"Bearer {alice_credential.token}":
            identity = "alice"
        elif authorization == f"Bearer {bob_credential.token}":
            identity = "bob"
        else:
            identity = "unknown"
        observed_bridge.append((request.url.path, identity))

    management_server = management_thread = None
    mcp_server = mcp_thread = None
    bridge_http = None
    try:
        management_server, management_thread, management_url = _start_loopback(management_app)
        bridge_http = httpx.Client(
            timeout=10,
            trust_env=False,
            event_hooks={"request": [observe_bridge]},
        )
        bridge = ManagedFileHTTPClient(
            management_url, "", account_mode=True, client=bridge_http
        )
        shared_resolver = create_release_resolver(
            root=project_root,
            qdrant_client=qdrant,
            embedder=shared["embedder"],
            reranker=None,
            legacy_router=None,
            search_lock=RLock(),
            expected_embedding_model=shared["targets"]["A"].embedding_model,
        )
        synthetic_service = SimpleNamespace(
            supported_version="synthetic-only",
            index_target_id="synthetic-managed-service",
            reranker=None,
            search_policy=None,
        )
        server = build_stateless_mcp_server(
            service=synthetic_service,
            identity=RuntimeIdentity(
                corpus_id="synthetic-corpus",
                corpus_id_source="synthetic-test",
                target_id="synthetic-managed-service",
                embedding_model="synthetic-only",
            ),
            audit=SearchAudit(stream=io.StringIO()),
            access=AccessRegistry(project_root / "access", {}),
            access_dir=project_root / "access",
            managed_bridge=bridge,
            release_resolver=shared_resolver,
            raw_root=project_root / "raw_data",
            project_root=project_root,
            identity_store=store,
        )
        app = create_stateless_app(
            server=server,
            identity_store=store,
            allowed_hosts=["127.0.0.1"],
        )
        mcp_server, mcp_thread, mcp_url = _start_loopback(app)
        mcp_url += "/mcp-stateless"

        # Search shared immutable release A with the official SDK, then activate B.
        # Its returned download URL must remain bound to A and survive that switch.
        shared_search_a = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="search_docs",
            arguments={
                "query": shared["queries"]["A"],
                "library": "synthetic-shared",
                "top_k": 1,
            },
            session_id=SESSION_HEADER,
        )
        shared_result_a = shared_search_a["callResult"]["structuredContent"]
        assert shared_search_a["callResult"]["isError"] is False
        assert "release_marker_A" in json.dumps(shared_result_a, ensure_ascii=False)
        shared_download_path_a = shared_result_a["evidence"][0]["download_path"]
        shared_query_a = parse_qs(urlsplit(shared_download_path_a).query)
        assert shared_query_a.get("release_id") == [shared_a["release_id"]]
        shared_active_b = shared_release_service.activate(
            "synthetic-shared",
            shared_b["release_id"],
            expected_revision=shared_active_a["revision"],
            expected_embedding_model=shared["targets"]["A"].embedding_model,
            reason="switch synthetic active release to B",
        )
        shared_download_a = httpx.get(
            f"{mcp_url.rsplit('/mcp-stateless', 1)[0]}{shared_download_path_a}",
            headers={"Authorization": f"Bearer {alice_credential.token}"},
            timeout=10,
            trust_env=False,
        )
        expected_shared_a = shared["paths"]["A"]["raw"].read_bytes()
        assert shared_download_a.status_code == 200, shared_download_a.text
        assert shared_download_a.content == expected_shared_a
        assert hashlib.sha256(shared_download_a.content).hexdigest() == hashlib.sha256(
            expected_shared_a
        ).hexdigest()

        # The active search now resolves B, while an unknown release URL is rejected.
        shared_search_b = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="search_docs",
            arguments={
                "query": shared["queries"]["B"],
                "library": "synthetic-shared",
                "top_k": 1,
            },
            session_id=SESSION_HEADER,
        )
        shared_result_b = shared_search_b["callResult"]["structuredContent"]
        assert shared_search_b["callResult"]["isError"] is False
        assert "release_marker_B" in json.dumps(shared_result_b, ensure_ascii=False)
        shared_download_path_b = shared_result_b["evidence"][0]["download_path"]
        assert parse_qs(urlsplit(shared_download_path_b).query).get("release_id") == [
            shared_b["release_id"]
        ]
        wrong_shared_version = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="search_docs",
            arguments={
                "query": shared["queries"]["B"],
                "library": "synthetic-shared",
                "version": "does-not-exist",
                "top_k": 1,
            },
            session_id=SESSION_HEADER,
            expect_error=True,
        )
        assert wrong_shared_version["callResult"]["structuredContent"]["error"] == "invalid_target"
        shared_parts = urlsplit(shared_download_path_a)
        shared_query = parse_qs(shared_parts.query)
        shared_query["release_id"] = ["synthetic-unknown-release"]
        unknown_release_path = urlunsplit((
            "", "", shared_parts.path,
            urlencode({key: value[0] for key, value in shared_query.items()}), "",
        ))
        unknown_release_download = httpx.get(
            f"{mcp_url.rsplit('/mcp-stateless', 1)[0]}{unknown_release_path}",
            headers={"Authorization": f"Bearer {alice_credential.token}"},
            timeout=10,
            trust_env=False,
        )
        assert unknown_release_download.status_code == 403
        assert shared_active_b["active_release_id"] == shared_b["release_id"]

        # Official independent TypeScript Client: both identities present the same
        # legacy-looking session header, but current credential+grant control results.
        alice_list = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="list_managed_files",
            session_id=SESSION_HEADER,
        )
        bob_list = _sdk_call(
            url=mcp_url,
            token=bob_credential.token,
            tool="list_managed_files",
            session_id=SESSION_HEADER,
        )
        alice_files = alice_list["callResult"]["structuredContent"]["files"]
        bob_files = bob_list["callResult"]["structuredContent"]["files"]
        assert [item["file_id"] for item in alice_files] == [file_id]
        assert bob_files == []
        assert all(
            request["sessionHeaderPresent"]
            for output in (alice_list, bob_list)
            for request in output["observedRequests"]
        )
        assert all(
            response["sessionHeaderReturned"] is False
            for output in (alice_list, bob_list)
            for response in output["observedResponses"]
        )

        search_a = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="search_docs",
            arguments={"query": "ambermarker_v1", "file_id": file_id, "top_k": 1},
            session_id=SESSION_HEADER,
        )
        result_a = search_a["callResult"]["structuredContent"]
        assert result_a["mcp_protocol"] == PROTOCOL
        assert search_a["callResult"]["isError"] is False
        assert "ambermarker_v1" in json.dumps(result_a, ensure_ascii=False)
        assert result_a["managed_file"]["version_id"] == version_a.version_id
        evidence_a = result_a["evidence"][0]
        download_path_a = evidence_a["download_path"]
        assert download_path_a.startswith(f"/mcp-stateless/files/managed/{file_id}?")
        old_download = httpx.get(
            f"{mcp_url.rsplit('/mcp-stateless', 1)[0]}{download_path_a}",
            headers={
                "Authorization": f"Bearer {alice_credential.token}",
                "Mcp-Session-Id": SESSION_HEADER,
            },
            timeout=10,
            trust_env=False,
        )
        assert old_download.status_code == 200, old_download.text
        assert b"ambermarker_v1" in old_download.content
        assert "no-store" in old_download.headers.get("cache-control", "").lower()

        # Replacing the managed file changes the active snapshot. The previous
        # version URL must fail closed instead of returning the new source bytes.
        second_source = tmp_path / "managed-v2.txt"
        second_source.write_text(
            "Synthetic managed source version two contains cobaltmarker_v2.\n",
            encoding="utf-8",
        )
        replacement = manager.submit_import(
            second_source,
            display_name="synthetic-managed-guide.txt",
            replace_file_id=file_id,
            library_id="synthetic-library",
            library_name="Synthetic Library",
        )
        assert manager.run_job(replacement.job_id).status == "completed"
        version_b = manager.snapshot(file_id)
        assert version_b.version_id != version_a.version_id
        stale_download = httpx.get(
            f"{mcp_url.rsplit('/mcp-stateless', 1)[0]}{download_path_a}",
            headers={
                "Authorization": f"Bearer {alice_credential.token}",
                "Mcp-Session-Id": SESSION_HEADER,
            },
            timeout=10,
            trust_env=False,
        )
        assert stale_download.status_code == 409, stale_download.text

        wrong_version = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="search_docs",
            arguments={
                "query": "ambermarker_v1",
                "file_id": file_id,
                "version": version_a.version,
                "top_k": 1,
            },
            session_id=SESSION_HEADER,
            expect_error=True,
        )
        assert wrong_version["callResult"]["structuredContent"]["error"] == "invalid_target"

        search_b = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="search_docs",
            arguments={"query": "cobaltmarker_v2", "file_id": file_id, "top_k": 1},
            session_id=SESSION_HEADER,
        )
        result_b = search_b["callResult"]["structuredContent"]
        assert search_b["callResult"]["isError"] is False
        assert "cobaltmarker_v2" in json.dumps(result_b, ensure_ascii=False)
        assert result_b["managed_file"]["version_id"] == version_b.version_id
        assert result_b["evidence"][0]["version_id"] == version_b.version_id
        new_download_path = result_b["evidence"][0]["download_path"]
        new_download = httpx.get(
            f"{mcp_url.rsplit('/mcp-stateless', 1)[0]}{new_download_path}",
            headers={
                "Authorization": f"Bearer {alice_credential.token}",
                "Mcp-Session-Id": SESSION_HEADER,
            },
            timeout=10,
            trust_env=False,
        )
        assert new_download.status_code == 200, new_download.text
        assert b"cobaltmarker_v2" in new_download.content

        # A grant change is observed on the next list/search call without server restart.
        store.set_grant(
            admin,
            resource_kind="library",
            resource_id="synthetic-library",
            user_id=alice.user_id,
            allowed=False,
            expected_revision=grant_a.revision,
        )
        after_revoke = _sdk_call(
            url=mcp_url,
            token=alice_credential.token,
            tool="list_managed_files",
            session_id=SESSION_HEADER,
        )
        assert after_revoke["callResult"]["structuredContent"]["files"] == []

        # Credential revocation is checked on a new real SDK connection.
        store.revoke_mcp_credential(admin, alice_credential.credential_id)
        revoked_headers = {
            **_headers("server/discover"),
            "Authorization": f"Bearer {alice_credential.token}",
        }
        revoked_discover = httpx.post(
            mcp_url,
            headers=revoked_headers,
            json={
                "jsonrpc": "2.0",
                "id": 990,
                "method": "server/discover",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
            timeout=10,
            trust_env=False,
        )
        assert revoked_discover.status_code == 401

        # Real managed bridge requests must carry the current identity's own token.
        assert ("/api/mcp/files", "alice") in observed_bridge
        assert ("/api/mcp/files", "bob") in observed_bridge
        assert any(path == "/api/mcp/search" and actor == "alice"
                   for path, actor in observed_bridge)
        assert any(path.endswith("/source") and actor == "alice"
                   for path, actor in observed_bridge)
        assert all(actor in {"alice", "bob"} for _, actor in observed_bridge)

        evidence_path = os.getenv("MCP_STATELESS_EVIDENCE_OUTPUT")
        if evidence_path:
            evidence = {
                "classification": "synthetic-indexes-in-memory-qdrant-real-http-management-and-real-ts-mcp-client",
                "protocolVersion": PROTOCOL,
                "client": {"package": "@modelcontextprotocol/client", "version": "2.3.1"},
                "retrieval": {
                    "embedder": "synthetic deterministic marker embedder",
                    "reranker": "disabled; real reranker/model not exercised",
                },
                "sameLegacySessionHeader": {
                    "sentByOfficialClient": True,
                    "aliceVisibleFileIds": [item["file_id"] for item in alice_files],
                    "bobVisibleFileIds": [item["file_id"] for item in bob_files],
                    "serverResponsesMintSession": False,
                },
                "sharedReleases": {
                    "activeAfterFirstSearch": shared_a["release_id"],
                    "queryMarkerA": shared["queries"]["A"],
                    "aDownloadPathReleaseId": shared_query_a["release_id"][0],
                    "downloadAStatusAfterSwitchToB": shared_download_a.status_code,
                    "downloadABytesSha256": hashlib.sha256(shared_download_a.content).hexdigest(),
                    "expectedABytesSha256": hashlib.sha256(expected_shared_a).hexdigest(),
                    "activeAfterSwitch": shared_active_b["active_release_id"],
                    "queryMarkerB": shared["queries"]["B"],
                    "bDownloadPathReleaseId": parse_qs(urlsplit(shared_download_path_b).query)["release_id"][0],
                    "wrongVersionIsToolError": wrong_shared_version["callResult"]["isError"],
                    "unknownReleaseDownloadStatus": unknown_release_download.status_code,
                },
                "managedVersions": {
                    "queryMarkerA": "ambermarker_v1",
                    "versionA": version_a.version_id,
                    "downloadAStatus": old_download.status_code,
                    "downloadAContainsMarker": b"ambermarker_v1" in old_download.content,
                    "versionB": version_b.version_id,
                    "staleVersionADownloadAfterReplace": stale_download.status_code,
                    "wrongVersionIsToolError": wrong_version["callResult"]["isError"],
                    "queryMarkerB": "cobaltmarker_v2",
                    "downloadBStatus": new_download.status_code,
                    "downloadBContainsMarker": b"cobaltmarker_v2" in new_download.content,
                },
                "revocation": {
                    "filesAfterGrantRevocation": len(
                        after_revoke["callResult"]["structuredContent"]["files"]
                    ),
                    "credentialRevokedDiscoverStatus": revoked_discover.status_code,
                },
                "managementBridgeRequests": [
                    {"path": path, "authenticatedIdentity": actor}
                    for path, actor in observed_bridge
                ],
            }
            target = Path(evidence_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")

    finally:
        _stop_loopback(mcp_server, mcp_thread)
        _stop_loopback(management_server, management_thread)
        if bridge_http is not None:
            bridge_http.close()
        manager.close()
        qdrant.close()
