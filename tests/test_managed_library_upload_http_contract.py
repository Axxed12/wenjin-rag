"""Isolated HTTP contract for named managed libraries and uploads."""

from __future__ import annotations

import hashlib
import re
import time
from http.cookies import SimpleCookie
from urllib.parse import quote

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from wenjin.file_management import FileManagementService
from wenjin.management_http.app import create_app

pytestmark = pytest.mark.integration

_ADMIN_TOKEN = "managed-library-bootstrap-only-test-token"
_ADMIN_PASSWORD = "Managed_Library_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Managed_Library_Reader_Temporary_2026!"
_READER_PASSWORD = "Managed_Library_Reader_Changed_2026!"


class _MarkerEmbedder:
    """Deterministic CPU vectors for the synthetic upload only."""

    dimension = 16

    def _vector(self, text: str) -> list[float]:
        return [
            int(hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:4], 16) / 65535.0
            for index in range(self.dimension)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


class _SessionClient:
    """Different account cookies over one TestClient lifespan."""

    def __init__(self, client, token: str | None = None):
        self.client = client
        self.token = token

    def request(self, method, url, **kwargs):
        previous = self.client.cookies.get("wenjin_session")
        self.client.cookies.delete("wenjin_session")
        if self.token:
            self.client.cookies.set("wenjin_session", self.token)
        try:
            response = self.client.request(method, url, **kwargs)
            cookie = SimpleCookie()
            cookie.load(response.headers.get("set-cookie", ""))
            if "wenjin_session" in cookie:
                self.token = cookie["wenjin_session"].value
            return response
        finally:
            self.client.cookies.delete("wenjin_session")
            if previous:
                self.client.cookies.set("wenjin_session", previous)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)


def _csrf(token: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": token}


def _login(client: _SessionClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _wait_job(client: _SessionClient, job_id: str) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"queued", "preparing", "running", "cancelling"}:
            return job
        time.sleep(0.05)
    raise AssertionError("synthetic upload job did not reach a terminal state")


def test_named_library_permissions_chinese_upload_and_deleted_job_name_snapshot(tmp_path):
    root = tmp_path / "isolated-project"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model="isolated-managed-library-marker-v1",
    )
    app = create_app(
        root,
        admin_token=_ADMIN_TOKEN,
        manager=manager,
        identity_db=root / "identity.sqlite",
    )

    try:
        with TestClient(app, client=("127.0.0.1", 52342)) as base:
            bootstrap = base.post(
                "/api/auth/bootstrap",
                headers={
                    "Authorization": f"Bearer {_ADMIN_TOKEN}",
                    "Origin": "http://testserver",
                },
                json={"username": "library-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            admin = _SessionClient(base)
            reader = _SessionClient(base)
            admin_session = _login(admin, "library-admin", _ADMIN_PASSWORD)
            admin_headers = _csrf(admin_session["csrf_token"])

            created = admin.post(
                "/api/admin/managed-libraries",
                headers=admin_headers,
                json={"display_name": "华东地形指南"},
            )
            assert created.status_code == 201, created.text
            library = created.json()["library"]
            library_id = library["library_id"]
            assert re.fullmatch(r"managed-[0-9a-f]{12}", library_id)
            assert library["display_name"] == "华东地形指南"
            assert library["created_by"] == bootstrap.json()["user"]["user_id"]
            assert admin.get("/api/admin/managed-libraries").json()["libraries"] == [library]
            assert admin.post(
                "/api/admin/managed-libraries",
                headers=admin_headers,
                json={"display_name": "华东地形指南"},
            ).status_code == 409, "display names are unique independent of generated IDs"
            assert admin.post(
                "/api/admin/managed-libraries",
                headers=admin_headers,
                json={"library_id": library_id, "display_name": "另一份显示名"},
            ).status_code == 409, "a stable library ID cannot be reassigned"

            create_reader = admin.post(
                "/api/admin/users",
                headers=admin_headers,
                json={
                    "username": "library-reader",
                    "password": _READER_TEMP_PASSWORD,
                    "role": "reader",
                },
            )
            assert create_reader.status_code == 201, create_reader.text
            first_reader_session = _login(reader, "library-reader", _READER_TEMP_PASSWORD)
            assert first_reader_session["must_change_password"] is True
            changed = reader.post(
                "/api/auth/password",
                headers=_csrf(first_reader_session["csrf_token"]),
                json={"old_password": _READER_TEMP_PASSWORD, "new_password": _READER_PASSWORD},
            )
            assert changed.status_code == 200, changed.text
            reader_session = _login(reader, "library-reader", _READER_PASSWORD)
            reader_headers = _csrf(reader_session["csrf_token"])

            # Registration is independent from read access; no grant is created implicitly.
            assert reader.get("/api/admin/managed-libraries").status_code == 403
            before_grant = reader.get("/api/admin/libraries")
            assert before_grant.status_code == 200, before_grant.text
            assert library_id not in {
                item["library_id"] for item in before_grant.json()["libraries"]
            }
            credential = reader.post(
                "/api/auth/credentials",
                headers=reader_headers,
                json={"label": "managed-library-test"},
            )
            assert credential.status_code == 201, credential.text
            mcp_headers = {"Authorization": f"Bearer {credential.json()['credential']['token']}"}
            assert reader.get("/api/mcp/files", headers=mcp_headers).json()["files"] == []

            source_bytes = "synthetic terrain ambermarker for parser contract\n".encode("utf-8")
            uploaded = admin.post(
                f"/api/files?library_id={library_id}",
                headers={
                    **admin_headers,
                    "Content-Type": "application/octet-stream",
                    "X-File-Name": quote("地形图.v1.txt"),
                    "X-Display-Name": quote("华东地貌说明"),
                },
                content=source_bytes,
            )
            assert uploaded.status_code == 202, uploaded.text
            upload_job = _wait_job(admin, uploaded.json()["job_id"])
            assert upload_job["status"] == "completed", upload_job
            file_id = upload_job["file_id"]
            file_record = admin.get(f"/api/files/{file_id}")
            assert file_record.status_code == 200, file_record.text
            file_data = file_record.json()
            assert file_data["library_id"] == library_id
            assert file_data["library_name"] == "华东地形指南"
            assert file_data["display_name"] == "华东地貌说明"
            assert file_data["original_filename"] == "地形图.v1.txt"
            assert file_data["version"] is not None
            assert manager.snapshot(file_id).source_path.suffix.lower() == ".txt"
            assert manager.get_original(file_id, expected_version_id=file_data["active_version_id"]) == source_bytes
            assert reader.get("/api/mcp/files", headers=mcp_headers).json()["files"] == []

            resources = admin.get("/api/admin/resources")
            assert resources.status_code == 200, resources.text
            assert any(
                item["resource_kind"] == "library" and item["resource_id"] == library_id
                for item in resources.json()["resources"]
            )
            grant = admin.post(
                "/api/admin/grants",
                headers=admin_headers,
                json={
                    "user_id": reader_session["user"]["user_id"],
                    "resource_kind": "library",
                    "resource_id": library_id,
                    "allowed": True,
                    "expected_revision": 0,
                },
            )
            assert grant.status_code == 200, grant.text
            after_grant = reader.get("/api/admin/libraries")
            assert after_grant.status_code == 200, after_grant.text
            visible = next(
                item for item in after_grant.json()["libraries"]
                if item["library_id"] == library_id
            )
            assert visible["library"] == "华东地形指南"
            mcp_files = reader.get("/api/mcp/files", headers=mcp_headers)
            assert mcp_files.status_code == 200, mcp_files.text
            assert any(item["file_id"] == file_id for item in mcp_files.json()["files"])

            deleted = admin.delete(f"/api/files/{file_id}", headers=admin_headers)
            assert deleted.status_code == 202, deleted.text
            delete_job = _wait_job(admin, deleted.json()["job_id"])
            assert delete_job["status"] == "deleted", delete_job
            detail = admin.get(f"/api/jobs/{delete_job['job_id']}")
            assert detail.status_code == 200, detail.text
            assert detail.json()["file_display_name"] == "华东地貌说明"
            assert detail.json()["library_id"] == library_id
            assert detail.json()["library_display_name"] == "华东地形指南"
            assert detail.json()["original_filename"] == "地形图.v1.txt"

            listed_jobs = admin.get("/api/jobs?limit=20").json()["jobs"]
            listed_delete = next(item for item in listed_jobs if item["job_id"] == delete_job["job_id"])
            assert listed_delete["file_display_name"] == "华东地貌说明"
            assert listed_delete["file_id"] == file_id  # stable ID remains available in API details

        # Reopening the manager on the same temporary root proves registration survives service restart.
        manager.close()
        manager = FileManagementService(
            root=root,
            qdrant_client=qdrant,
            embedder=_MarkerEmbedder(),
            embedding_model="isolated-managed-library-marker-v1",
        )
        assert manager.list_libraries() == [library]
    finally:
        manager.close()
        qdrant.close()
