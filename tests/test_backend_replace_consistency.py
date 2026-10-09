"""Cross-layer replace visibility and recovery contracts using isolated assets."""

from __future__ import annotations

import hashlib
import asyncio
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from qdrant_client import QdrantClient
from starlette.requests import Request
from starlette.testclient import TestClient

from wenjin.file_management import FileManagementService
from wenjin.management_http.app import create_app
from wenjin.management_http.identity_http import mcp_file_list

pytestmark = pytest.mark.integration

_BOOTSTRAP_TOKEN = "backend-consistency-test-bootstrap-token"
_ADMIN_PASSWORD = "Backend_Consistency_Admin_2026!"
_READER_TEMP_PASSWORD = "Backend_Consistency_Reader_Temporary_2026!"
_READER_PASSWORD = "Backend_Consistency_Reader_Changed_2026!"


class _MarkerEmbedder:
    dimension = 32

    def _vector(self, text: str) -> list[float]:
        return [
            int(hashlib.sha256(f"{i}:{text}".encode()).hexdigest()[:4], 16) / 65535
            for i in range(self.dimension)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


class _Session:
    """Independent cookie jars over one real app lifespan."""

    def __init__(self, client):
        self.client = client
        self.token = None
        self.csrf_token = None

    def request(self, method, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and self.csrf_token:
            headers.setdefault("Origin", "http://testserver")
            headers.setdefault("X-CSRF-Token", self.csrf_token)
        kwargs["headers"] = headers
        previous = self.client.cookies.get("wenjin_session")
        self.client.cookies.delete("wenjin_session")
        if self.token:
            self.client.cookies.set("wenjin_session", self.token)
        try:
            response = self.client.request(method, path, **kwargs)
            cookie = SimpleCookie()
            cookie.load(response.headers.get("set-cookie", ""))
            if "wenjin_session" in cookie:
                self.token = cookie["wenjin_session"].value
            return response
        finally:
            self.client.cookies.delete("wenjin_session")
            if previous:
                self.client.cookies.set("wenjin_session", previous)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self.request("PUT", path, **kwargs)


def _csrf(token: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": token}


def _login(session: _Session, username: str, password: str) -> dict:
    result = session.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    session.csrf_token = body["csrf_token"]
    return body


def _create_reader(admin: _Session, shared: _Session, headers: dict, username: str) -> dict:
    created = admin.post(
        "/api/admin/users",
        headers=headers,
        json={"username": username, "password": _READER_TEMP_PASSWORD, "role": "reader"},
    )
    assert created.status_code == 201, created.text
    temporary = _login(shared, username, _READER_TEMP_PASSWORD)
    changed = shared.post(
        "/api/auth/password",
        headers=_csrf(temporary["csrf_token"]),
        json={"old_password": _READER_TEMP_PASSWORD, "new_password": _READER_PASSWORD},
    )
    assert changed.status_code == 200, changed.text
    return _login(shared, username, _READER_PASSWORD)


def _wait_job(session: _Session, job_id: str, timeout: float = 20) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        response = session.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"queued", "preparing", "running", "cancelling"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach terminal state")


def _managed_search(session: _Session, library_id: str, file_id: str, version_id: str, query: str):
    return session.post(
        "/api/admin/search",
        json={
            "library_id": library_id,
            "source_type": "managed",
            "file_id": file_id,
            "version_id": version_id,
            "query": query,
        },
    )


def _library_files(session: _Session, library_id: str) -> list[dict]:
    response = session.get("/api/admin/libraries")
    assert response.status_code == 200, response.text
    library = next(
        (item for item in response.json()["libraries"] if item["library_id"] == library_id),
        None,
    )
    return [] if library is None else library["sources"]


def test_replace_metadata_and_authorization_change_only_with_active_version(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    activation_entered = threading.Event()
    allow_activation_to_fail = threading.Event()
    hook_state = {"armed": False}

    def pause_then_fail_once(phase: str, context: dict) -> None:
        if phase == "activating" and hook_state["armed"]:
            hook_state["armed"] = False
            activation_entered.set()
            if not allow_activation_to_fail.wait(timeout=15):
                raise TimeoutError("test did not release the synthetic activation gate")
            raise OSError("synthetic activation fault")

    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model="backend-consistency-synthetic-v1",
        fault_hook=pause_then_fail_once,
    )
    app = create_app(
        root,
        admin_token=_BOOTSTRAP_TOKEN,
        manager=manager,
        identity_db=root / "identity.sqlite",
    )

    try:
        with TestClient(app, client=("127.0.0.1", 52437)) as client:
            bootstrap = client.post(
                "/api/auth/bootstrap",
                headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}", "Origin": "http://testserver"},
                json={"username": "consistency-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            admin, shared = _Session(client), _Session(client)
            admin_login = _login(admin, "consistency-admin", _ADMIN_PASSWORD)
            admin_headers = _csrf(admin_login["csrf_token"])

            library_a = admin.post(
                "/api/admin/managed-libraries", headers=admin_headers,
                json={"display_name": "A旧资料库"},
            )
            library_b = admin.post(
                "/api/admin/managed-libraries", headers=admin_headers,
                json={"display_name": "B新资料库"},
            )
            assert library_a.status_code == library_b.status_code == 201
            a_id = library_a.json()["library"]["library_id"]
            b_id = library_b.json()["library"]["library_id"]

            reader_a_login = _create_reader(admin, shared, admin_headers, "reader-library-a")
            reader_b_login = _create_reader(admin, shared, admin_headers, "reader-library-b")
            reader_a, reader_b = _Session(client), _Session(client)
            _login(reader_a, "reader-library-a", _READER_PASSWORD)
            _login(reader_b, "reader-library-b", _READER_PASSWORD)
            resources = admin.get("/api/admin/resources")
            assert resources.status_code == 200, resources.text
            for reader_login, library_id in ((reader_a_login, a_id), (reader_b_login, b_id)):
                granted = admin.post(
                    "/api/admin/grants",
                    headers=admin_headers,
                    json={
                        "user_id": reader_login["user"]["user_id"],
                        "resource_kind": "library", "resource_id": library_id,
                        "allowed": True, "expected_revision": 0,
                    },
                )
                assert granted.status_code == 200, granted.text

            v1_bytes = b"synthetic amberstone old-v1 morphology guide\n"
            first = admin.post(
                f"/api/files?library_id={a_id}",
                headers={
                    **admin_headers, "Content-Type": "application/octet-stream",
                    "X-File-Name": quote("地貌v1.txt"), "X-Display-Name": quote("A旧地貌说明"),
                },
                content=v1_bytes,
            )
            assert first.status_code == 202, first.text
            first_job = _wait_job(admin, first.json()["job_id"])
            assert first_job["status"] == "completed", (
                f"status={first_job.get('status')} phase={first_job.get('phase')} "
                f"reason={first_job.get('reason')}"
            )
            file_id = first_job["file_id"]
            old = admin.get(f"/api/files/{file_id}").json()
            assert (old["library_id"], old["display_name"], old["original_filename"]) == (
                a_id, "A旧地貌说明", "地貌v1.txt",
            )
            old_version_id = old["active_version_id"]
            assert _library_files(reader_a, a_id)[0]["file_id"] == file_id
            assert _library_files(reader_b, b_id) == []
            old_search = _managed_search(reader_a, a_id, file_id, old_version_id, "amberstone")
            assert old_search.status_code == 200, old_search.text
            assert "amberstone" in str(old_search.json())
            old_download = reader_a.get(
                f"/api/files/{file_id}/download?version_id={old_version_id}&name=地貌v1.txt"
            )
            assert old_download.status_code == 200, old_download.text
            assert old_download.content == v1_bytes

            v2_bytes = b"synthetic cobaltbay new-v2 coastal geology handbook\n"
            hook_state["armed"] = True
            replacement = admin.put(
                f"/api/files/{file_id}?library_id={b_id}",
                headers={
                    **admin_headers, "Content-Type": "application/octet-stream",
                    "X-File-Name": quote("海岸v2.txt"), "X-Display-Name": quote("B新海岸指南"),
                },
                content=v2_bytes,
            )
            assert replacement.status_code == 202, replacement.text
            job_id = replacement.json()["job_id"]
            assert activation_entered.wait(timeout=15), "synthetic worker did not reach activation gate"

            during = admin.get(f"/api/files/{file_id}").json()
            assert (during["library_id"], during["display_name"], during["original_filename"],
                    during["active_version_id"]) == (a_id, "A旧地貌说明", "地貌v1.txt", old_version_id)
            assert [item["file_id"] for item in _library_files(reader_a, a_id)] == [file_id]
            assert _library_files(reader_b, b_id) == []
            assert _managed_search(reader_a, a_id, file_id, old_version_id, "amberstone").status_code == 200
            assert _managed_search(reader_b, b_id, file_id, old_version_id, "cobaltbay").status_code in {403, 409}
            assert reader_b.get(f"/api/files/{file_id}/download?version_id={old_version_id}").status_code == 403

            allow_activation_to_fail.set()
            failed = _wait_job(admin, job_id)
            assert failed["status"] == "failed" and failed["retryable"] is True, failed
            after_failure = admin.get(f"/api/files/{file_id}").json()
            assert (after_failure["library_id"], after_failure["display_name"],
                    after_failure["original_filename"], after_failure["active_version_id"]) == (
                a_id, "A旧地貌说明", "地貌v1.txt", old_version_id,
            )
            assert _managed_search(reader_a, a_id, file_id, old_version_id, "amberstone").status_code == 200
            assert _library_files(reader_b, b_id) == []

            retried = admin.post(f"/api/jobs/{job_id}/retry", headers=admin_headers)
            assert retried.status_code == 202, retried.text
            assert retried.json()["version_id"] == failed["version_id"], "retry must reuse the prepared version identity"
            completed = _wait_job(admin, job_id)
            assert completed["status"] == "completed", completed
            final = admin.get(f"/api/files/{file_id}").json()
            assert (final["library_id"], final["library_name"], final["display_name"],
                    final["original_filename"], final["version"]) == (
                b_id, "B新资料库", "B新海岸指南", "海岸v2.txt", "v2",
            )
            assert final["active_version_id"] == failed["version_id"]
            assert _library_files(reader_a, a_id) == []
            b_sources = _library_files(reader_b, b_id)
            assert len(b_sources) == 1 and b_sources[0]["file_id"] == file_id
            new_search = _managed_search(reader_b, b_id, file_id, final["active_version_id"], "cobaltbay")
            assert new_search.status_code == 200, new_search.text
            assert "cobaltbay" in str(new_search.json())
            old_grant_search = _managed_search(
                reader_a, a_id, file_id, final["active_version_id"], "cobaltbay"
            )
            assert old_grant_search.status_code in {403, 409}, old_grant_search.text
            new_download = reader_b.get(
                f"/api/files/{file_id}/download?version_id={final['active_version_id']}&name=海岸v2.txt"
            )
            assert new_download.status_code == 200, new_download.text
            assert new_download.content == v2_bytes
            assert reader_a.get(f"/api/files/{file_id}/download?version_id={old_version_id}").status_code == 403
    finally:
        allow_activation_to_fail.set()
        manager.close()
        qdrant.close()


def test_mcp_search_and_source_reject_cross_library_activation_race(tmp_path):
    root = tmp_path / "mcp-race-root"
    root.mkdir()
    activation_entered = threading.Event()
    release_activation = threading.Event()
    job_done = threading.Event()
    gate_state = {"armed": False, "job": None}

    def pause_at_activation(phase: str, context: dict) -> None:
        del context
        if phase == "activating" and gate_state["armed"]:
            gate_state["armed"] = False
            activation_entered.set()
            if not release_activation.wait(timeout=15):
                raise TimeoutError("synthetic MCP race activation gate was not released")

    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root, qdrant_client=qdrant, embedder=_MarkerEmbedder(),
        embedding_model="synthetic-mcp-version-race-v1", fault_hook=pause_at_activation,
    )
    app = create_app(root, admin_token=_BOOTSTRAP_TOKEN, manager=manager,
                     identity_db=root / "identity.sqlite")
    workers: list[threading.Thread] = []
    try:
        with TestClient(app, client=("127.0.0.1", 52439)) as client:
            bootstrapped = client.post(
                "/api/auth/bootstrap",
                headers={"Authorization": f"Bearer {_BOOTSTRAP_TOKEN}", "Origin": "http://testserver"},
                json={"username": "mcp-race-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrapped.status_code == 201, bootstrapped.text
            admin, shared = _Session(client), _Session(client)
            login = _login(admin, "mcp-race-admin", _ADMIN_PASSWORD)
            admin_headers = _csrf(login["csrf_token"])
            lib_a = admin.post("/api/admin/managed-libraries", headers=admin_headers,
                               json={"display_name": "竞争资料库A"})
            lib_b = admin.post("/api/admin/managed-libraries", headers=admin_headers,
                               json={"display_name": "竞争资料库B"})
            assert lib_a.status_code == lib_b.status_code == 201
            a_id, b_id = lib_a.json()["library"]["library_id"], lib_b.json()["library"]["library_id"]
            a_user = _create_reader(admin, shared, admin_headers, "mcp-race-reader-a")
            b_user = _create_reader(admin, shared, admin_headers, "mcp-race-reader-b")
            resources = admin.get("/api/admin/resources")
            assert resources.status_code == 200, resources.text
            for user, library_id in ((a_user, a_id), (b_user, b_id)):
                granted = admin.post(
                    "/api/admin/grants", headers=admin_headers,
                    json={"user_id": user["user"]["user_id"], "resource_kind": "library",
                          "resource_id": library_id, "allowed": True, "expected_revision": 0},
                )
                assert granted.status_code == 200, granted.text

            source_a = tmp_path / "mcp-race-a.txt"
            source_a.write_text("synthetic amberridge version A original body", encoding="utf-8")
            first = manager.submit_import(source_a, library_id=a_id, library_name="竞争资料库A",
                                          display_name="A正文", original_filename="a.txt")
            assert manager.run_job(first.job_id).status == "completed"
            active = manager.get_file(first.file_id)
            store = app.state.identity_store
            reader_a, reader_b = _Session(client), _Session(client)
            _login(reader_a, "mcp-race-reader-a", _READER_PASSWORD)
            _login(reader_b, "mcp-race-reader-b", _READER_PASSWORD)
            principal_a = store.resolve_session(reader_a.token)
            principal_b = store.resolve_session(reader_b.token)
            token_a = store.issue_mcp_credential(principal_a, label="race-reader-a").token
            token_b = store.issue_mcp_credential(principal_b, label="race-reader-b").token

            original_acl = store.can_read_managed_file
            race_state = {"armed": False}

            def activate_after_authorization(principal, library_id: str, file_id: str) -> bool:
                allowed = original_acl(principal, library_id, file_id)
                if race_state["armed"]:
                    race_state["armed"] = False
                    release_activation.set()
                    assert job_done.wait(timeout=15), "activation did not finish inside ACL hook"
                return allowed

            store.can_read_managed_file = activate_after_authorization

            def start_candidate(library_id: str, library_name: str, label: str, filename: str):
                nonlocal active
                source = tmp_path / filename
                source.write_text(f"synthetic {filename} unique body", encoding="utf-8")
                candidate = manager.submit_import(
                    source, replace_file_id=active.file_id, library_id=library_id,
                    library_name=library_name, display_name=label, original_filename=filename,
                )
                activation_entered.clear()
                release_activation.clear()
                job_done.clear()
                gate_state["armed"] = True
                result = {}

                def run():
                    try:
                        result["job"] = manager.run_job(candidate.job_id)
                    finally:
                        job_done.set()

                worker = threading.Thread(target=run, daemon=True)
                workers.append(worker)
                worker.start()
                assert activation_entered.wait(timeout=15), "candidate did not reach activation gate"
                race_state["armed"] = True
                return candidate, result

            # A reader's authorization is checked against A, then the active snapshot
            # changes to B before manager.search takes its own request snapshot.
            candidate_b, result_b = start_candidate(b_id, "竞争资料库B", "B正文", "b-version.txt")
            search = client.post(
                "/api/mcp/search", headers={"Authorization": f"Bearer {token_a}"},
                json={"file_id": active.file_id, "version_id": active.active_version_id,
                      "query": "amberridge"},
            )
            assert search.status_code == 409, search.text
            assert "synthetic b-version" not in search.text
            workers[-1].join(timeout=15)
            assert not workers[-1].is_alive()
            assert result_b["job"].status == "completed"
            active = manager.get_file(active.file_id)

            # Repeat the interleaving for the source endpoint: B is authorized from
            # the pre-switch record, then the active file moves back to A before read.
            candidate_a, result_a = start_candidate(a_id, "竞争资料库A", "A新正文", "a-version-2.txt")
            download = client.get(
                f"/api/mcp/files/{active.file_id}/source?version_id={active.active_version_id}",
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert download.status_code == 409, download.text
            assert b"synthetic a-version-2" not in download.content
            workers[-1].join(timeout=15)
            assert not workers[-1].is_alive()
            assert result_a["job"].status == "completed"
            assert manager.get_file(active.file_id).active_version_id == candidate_a.version_id
    finally:
        release_activation.set()
        for worker in workers:
            worker.join(timeout=1)
        manager.close()
        qdrant.close()


def test_opening_legacy_database_restores_only_version_owned_metadata(tmp_path):
    root = tmp_path / "legacy-root"
    base = root / "data" / "file-management"
    base.mkdir(parents=True)
    database = base / "manager.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE files(
                file_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, original_filename TEXT,
                status TEXT NOT NULL, active_version_id TEXT, duplicate_of TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                library_id TEXT NOT NULL DEFAULT 'managed-files',
                library_name TEXT NOT NULL DEFAULT '未分组'
            );
            CREATE TABLE versions(
                version_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, version TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL, source_name TEXT NOT NULL, manifest_path TEXT,
                raw_root TEXT, source_path TEXT, bm25_path TEXT, dense_collection TEXT,
                chunk_count INTEGER NOT NULL DEFAULT 0, strategy TEXT NOT NULL DEFAULT 'blocks-split',
                status TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE jobs(
                job_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, version_id TEXT,
                kind TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
                progress INTEGER NOT NULL, retryable INTEGER NOT NULL, reason TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL DEFAULT '{}',
                resources TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("file-synthetic", "B新名", "海岸v2.txt", "active", "version-v2", None,
             "2026-10-08T00:00:00Z", "2026-10-08T00:00:00Z", "library-b", "B库"),
        )
        db.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("file-ambiguous", "失败候选污染名", "failed-v2.txt", "active", "ambiguous-v1", None,
             "2026-10-08T00:00:00Z", "2026-10-08T00:00:00Z", "library-b", "B库"),
        )
        db.execute(
            "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("file-first-pending", "首次上传名称", "待导入.txt", "processing", None, None,
             "2026-10-08T00:00:00Z", "2026-10-08T00:00:00Z", "library-a", "A库"),
        )
        for version_id, version, status in (
            ("version-v1", "v1", "retained"),
            ("version-v2", "v2", "active"),
            ("version-unknown", "v0", "retained"),
        ):
            db.execute(
                "INSERT INTO versions(version_id,file_id,version,raw_sha256,source_name,status,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (version_id, "file-synthetic", version, hashlib.sha256(version_id.encode()).hexdigest(),
                 f"{version}.txt", status, "2026-10-08T00:00:00Z"),
            )
        for version_id, version, status in (
            ("ambiguous-v1", "v1", "active"),
            ("ambiguous-v2", "v2", "failed"),
        ):
            db.execute(
                "INSERT INTO versions(version_id,file_id,version,raw_sha256,source_name,status,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (version_id, "file-ambiguous", version, hashlib.sha256(version_id.encode()).hexdigest(),
                 f"{version}.txt", status, "2026-10-08T00:00:00Z"),
            )
        for version_id, metadata in (
            ("version-v1", {"library_id": "library-a", "library_display_name": "A库",
                             "file_display_name": "A旧名", "original_filename": "terrain-v1.txt",
                             "source": "staging/job-v1/source.txt"}),
            ("version-v2", {"library_id": "library-b", "library_display_name": "B库",
                             "file_display_name": "B新名", "original_filename": "海岸v2.txt",
                             "source": "staging/job-v2/source.txt"}),
            ("version-unknown", {}),
        ):
            db.execute(
                "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,payload,created_at,updated_at) "
                "VALUES(?,?,?,'import','completed','complete',100,0,?,?,?)",
                (f"job-{version_id}", "file-synthetic", version_id, json.dumps(metadata, ensure_ascii=False),
                 "2026-10-08T00:00:00Z", "2026-10-08T00:00:00Z"),
            )
        db.execute(
            "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,payload,created_at,updated_at) "
            "VALUES('job-v2-duplicate','file-synthetic','version-v2','import','duplicate','complete',100,0,?,?,?)",
            (json.dumps({"file_display_name": "误导的重复名称", "library_id": "library-b",
                         "library_display_name": "B库", "original_filename": "wrong-duplicate-name.txt"},
                        ensure_ascii=False), "2026-10-08T00:01:00Z", "2026-10-08T00:01:00Z"),
        )
        db.execute(
            "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,payload,created_at,updated_at) "
            "VALUES('job-ambiguous-original','file-ambiguous','ambiguous-v1','import','completed','complete',100,0,'{}',?,?)",
            ("2026-10-08T00:00:00Z", "2026-10-08T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,payload,created_at,updated_at) "
            "VALUES('job-ambiguous-failed-replace','file-ambiguous','ambiguous-v2','import','failed','failed',90,1,?,?,?)",
            (json.dumps({"replace": True, "library_id": "library-b", "library_display_name": "B库",
                         "file_display_name": "失败候选污染名", "original_filename": "failed-v2.txt",
                         "source": "staging/failed-v2/source.txt"},
                        ensure_ascii=False), "2026-10-08T00:02:00Z", "2026-10-08T00:02:00Z"),
        )
        db.execute(
            "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,payload,created_at,updated_at) "
            "VALUES('job-first-pending','file-first-pending',NULL,'import','queued','queued',5,1,?,?,?)",
            (json.dumps({"library_id": "library-a", "library_display_name": "A库",
                         "file_display_name": "首次上传名称", "original_filename": "待导入.txt",
                         "source": "staging/first/source.txt"}, ensure_ascii=False),
             "2026-10-08T00:00:00Z", "2026-10-08T00:00:00Z"),
        )

    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root, qdrant_client=qdrant, embedder=_MarkerEmbedder(),
        embedding_model="legacy-migration-synthetic-v1",
    )
    try:
        active = manager.get_file("file-synthetic")
        assert (active.library_id, active.library_name, active.display_name,
                active.original_filename, active.version) == (
            "library-b", "B库", "B新名", "海岸v2.txt", "v2",
        )
        with manager._connect() as db:
            history = {
                row["version_id"]: (row["library_id"], row["library_name"],
                                    row["display_name"], row["original_filename"])
                for row in db.execute(
                    "SELECT version_id,library_id,library_name,display_name,original_filename FROM versions"
                )
            }
        assert history["version-v1"] == ("library-a", "A库", "A旧名", "terrain-v1.txt")
        assert history["version-v2"] == ("library-b", "B库", "B新名", "海岸v2.txt")
        assert history["version-unknown"] == (None, None, None, None), (
            "migration must not copy the current file row into an unidentifiable old version"
        )
        assert history["ambiguous-v1"] == (None, None, None, None), (
            "when an old failed replacement may have polluted files, do not trust its row for active history"
        )
        assert history["ambiguous-v2"] == ("library-b", "B库", "失败候选污染名", "failed-v2.txt")
        ambiguous = manager.get_file("file-ambiguous")
        assert ambiguous.status == "integrity_error"
        assert "缺失" in ambiguous.display_name
        assert ambiguous.library_id is None and ambiguous.library_name is None
        assert ambiguous.display_name != "失败候选污染名"
        with pytest.raises(RuntimeError, match="拒绝访问"):
            manager.search("file-ambiguous", "synthetic old marker")
        store = SimpleNamespace(
            synchronize_resource=lambda *args, **kwargs: None,
            can_read_managed_file=lambda *args, **kwargs: True,
        )
        app = SimpleNamespace(state=SimpleNamespace(manager=manager, identity_store=store))
        request = Request({
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/api/mcp/files",
            "raw_path": b"/api/mcp/files", "query_string": b"", "headers": [],
            "client": ("testclient", 50000), "server": ("testserver", 80),
            "app": app, "state": {"principal": object()},
        })
        mcp_response = asyncio.run(mcp_file_list(request))
        mcp_payload = json.loads(mcp_response.body)
        assert "file-ambiguous" not in {item["file_id"] for item in mcp_payload["files"]}
        initial = manager.get_file("file-first-pending")
        assert (initial.status, initial.active_version_id, initial.display_name,
                initial.library_id, initial.library_name, initial.original_filename) == (
            "processing", None, "首次上传名称", "library-a", "A库", "待导入.txt",
        )
        assert any(
            item.file_id == "file-first-pending" and item.display_name == "首次上传名称"
            and item.library_id == "library-a"
            for item in manager.list_files()
        )
    finally:
        manager.close()
        qdrant.close()


def test_concurrent_run_job_claim_executes_import_once(tmp_path, monkeypatch):
    root = tmp_path / "concurrent-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root, qdrant_client=qdrant, embedder=_MarkerEmbedder(),
        embedding_model="concurrent-run-synthetic-v1",
    )
    source = root / "same-job.txt"
    source.write_text("synthetic one-claim marker", encoding="utf-8")
    queued = manager.submit_import(source, display_name="一次导入")

    read_barrier = threading.Barrier(2)
    read_lock = threading.Lock()
    initial_reads = {"count": 0}
    original_get_job = manager.jobs.get_job
    original_run_import = manager._run_import
    run_count = {"count": 0}
    run_lock = threading.Lock()

    def synchronized_get_job(job_id):
        result = original_get_job(job_id)
        if job_id == queued.job_id:
            with read_lock:
                should_wait = initial_reads["count"] < 2
                if should_wait:
                    initial_reads["count"] += 1
            if should_wait:
                read_barrier.wait(timeout=20)
        return result

    def count_run(job_id):
        with run_lock:
            run_count["count"] += 1
        return original_run_import(job_id)

    monkeypatch.setattr(manager.jobs, "get_job", synchronized_get_job)
    monkeypatch.setattr(manager, "_run_import", count_run)
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(manager.run_job, queued.job_id) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        assert run_count["count"] == 1, "the queued job must have a single worker claim"
        assert all(item.status == "completed" for item in results)
        assert manager.get_file(queued.file_id).active_version_id == queued.version_id
        snapshot = manager.snapshot(queued.file_id)
        assert qdrant.count(collection_name=snapshot.dense_collection, exact=True).count == snapshot.chunk_count
    finally:
        manager.close()
        qdrant.close()


def test_failed_old_replacement_cannot_be_retried_after_a_newer_version_activates(tmp_path):
    root = tmp_path / "superseded-retry-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    fault = {"armed": False}

    def fail_v2_activation(phase: str, context: dict) -> None:
        if phase == "activating" and fault["armed"]:
            fault["armed"] = False
            raise OSError("synthetic v2 activation failure")

    manager = FileManagementService(
        root=root, qdrant_client=qdrant, embedder=_MarkerEmbedder(),
        embedding_model="superseded-retry-synthetic-v1", fault_hook=fail_v2_activation,
    )
    source_v1 = root / "v1.txt"
    source_v1.write_text("ambertrail active v1", encoding="utf-8")
    first = manager.submit_import(source_v1, library_id="library-a", library_name="A库",
                                  display_name="A名", original_filename="a-v1.txt")
    assert manager.run_job(first.job_id).status == "completed"
    source_v2 = root / "v2.txt"
    source_v2.write_text("failed v2 retry candidate", encoding="utf-8")
    fault["armed"] = True
    failed_v2 = manager.submit_import(
        source_v2, replace_file_id=first.file_id, library_id="library-b",
        library_name="B库", display_name="B失败名", original_filename="b-v2.txt",
    )
    assert manager.run_job(failed_v2.job_id).status == "failed"
    assert manager.get_file(first.file_id).version == "v1"

    source_v3 = root / "v3.txt"
    source_v3.write_text("cobaltridge active v3 only", encoding="utf-8")
    active_v3_job = manager.submit_import(
        source_v3, replace_file_id=first.file_id, library_id="library-c",
        library_name="C库", display_name="C成功名", original_filename="c-v3.txt",
    )
    active_v3 = manager.run_job(active_v3_job.job_id)
    assert active_v3.status == "completed", active_v3.reason
    assert manager.get_job(failed_v2.job_id).retryable is False
    current = manager.get_file(first.file_id)
    assert (current.version, current.library_id, current.display_name, current.original_filename) == (
        "v3", "library-c", "C成功名", "c-v3.txt",
    )

    with pytest.raises(ValueError, match="重试|状态|版本|active|活动"):
        manager.submit_retry(failed_v2.job_id)
    after_rejected_retry = manager.get_file(first.file_id)
    assert (after_rejected_retry.version, after_rejected_retry.active_version_id) == (
        "v3", active_v3.version_id,
    )
    assert manager.search(first.file_id, "cobaltridge", version_id=active_v3.version_id)["evidence"]
    failed_marker_evidence = manager.search(
        first.file_id, "failed v2 retry candidate", version_id=active_v3.version_id
    )["evidence"]
    assert all("failed v2 retry candidate" not in item["text"] for item in failed_marker_evidence)
    manager.close()
    qdrant.close()


def test_running_import_recovers_after_worker_process_exit(tmp_path):
    root = tmp_path / "crash-root"
    root.mkdir()
    source = root / "interrupted.txt"
    source.write_text("synthetic interrupted import recovery marker", encoding="utf-8")
    job_file = root / "job-id.txt"
    script = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        from qdrant_client import QdrantClient
        from wenjin.file_management import FileManagementService

        class Embedder:
            dimension = 16
            def _v(self, text): return [float((sum(text.encode()) + i) % 17 + 1) for i in range(self.dimension)]
            def get_text_embedding_batch(self, texts, show_progress=False): return [self._v(t) for t in texts]
            def get_query_embedding(self, query): return self._v(query)

        root, source, job_path = map(Path, sys.argv[1:4])
        client = QdrantClient(':memory:')
        def crash(phase, context):
            if phase == 'staging':
                job_path.write_text(context['job_id'], encoding='utf-8')
                os._exit(73)
        service = FileManagementService(root, client, Embedder(), 'subprocess-synthetic-v1', fault_hook=crash)
        job = service.submit_import(source, display_name='中断恢复样例')
        job_path.write_text(job.job_id, encoding='utf-8')
        service.run_job(job.job_id)
        os._exit(74)
        """
    )
    child = subprocess.run(
        [sys.executable, "-c", script, str(root), str(source), str(job_file)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert child.returncode == 73, (
        f"child did not exit from the persisted running/staging hook; code={child.returncode} "
        f"stderr={child.stderr[-1000:]}"
    )
    job_id = job_file.read_text(encoding="utf-8")

    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root, qdrant_client=qdrant, embedder=_MarkerEmbedder(),
        embedding_model="restart-synthetic-v1",
    )
    try:
        pending = manager.resume_pending_jobs()
        assert len(pending) == 1 and pending[0].job_id == job_id
        assert pending[0].status == "queued" and pending[0].retry_count == 1
        recovered = manager.run_job(job_id)
        assert recovered.status == "completed", recovered.reason
        active = manager.get_file(recovered.file_id)
        assert active.status == "active" and active.display_name == "中断恢复样例"
        assert manager.search(recovered.file_id, "interrupted", version_id=active.active_version_id)["evidence"]
    finally:
        manager.close()
        qdrant.close()
