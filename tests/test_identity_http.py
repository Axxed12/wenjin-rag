"""Identity HTTP/API contracts on an isolated temp root and synthetic managed file."""

from __future__ import annotations

import hashlib
import json
from http.cookies import SimpleCookie

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.file_management import FileManagementService
from wenjin.management_http.app import create_app

pytestmark = pytest.mark.integration

_ADMIN_TOKEN = "identity-http-bootstrap-only-test-token"
_ADMIN_PASSWORD = "Http_Admin_Only_Test_Password_2026!"
_READER_TEMP_PASSWORD = "Http_Reader_Temporary_Password_2026!"
_READER_PASSWORD = "Http_Reader_Changed_Password_2026!"
_READER_RESET_PASSWORD = "Http_Reader_Reset_Password_2026!"


class _MarkerEmbedder:
    dimension = 16

    def _vector(self, text: str) -> list[float]:
        return [
            (int(hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:4], 16) / 65535.0)
            for index in range(self.dimension)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def _same_origin(csrf: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


class _SessionClient:
    """One TestClient/lifespan with explicit per-request cookie identities."""

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

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)


def test_identity_http_scopes_auth_grants_and_revocation_across_admin_and_mcp_routes(
    tmp_path,
):
    root = tmp_path / "http-project-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model="isolated-identity-http-marker-v1",
    )
    source = tmp_path / "http-managed-guide.txt"
    source.write_text(
        "Synthetic private managed source with http_private_marker.\n",
        encoding="utf-8",
    )
    job = manager.submit_import(
        source,
        display_name="http-managed-guide.txt",
        library_id="identity-contract-library",
        library_name="Identity Contract Library",
    )
    assert manager.run_job(job.job_id).status == "completed"
    file_record = manager.get_file(job.file_id)
    release_assets = build_two_version_release_fixture(
        root=root, library_id="release-candidate-only", namespace="candidate",
        qdrant_client=qdrant,
    )
    candidate_config = root / "configs" / "indexes" / "release-candidate-only.json"
    candidate_config.parent.mkdir(parents=True, exist_ok=True)
    candidate_config.write_text(
        json.dumps({
            "schema_version": 1,
            "candidates": [
                release_assets["targets"][label].__dict__ for label in ("A", "B")
            ],
        }),
        encoding="utf-8",
    )
    identity_db = root / "data" / "identity" / "identity.sqlite"
    app = create_app(
        root,
        admin_token=_ADMIN_TOKEN,
        manager=manager,
        identity_db=identity_db,
    )

    try:
        with TestClient(app, client=("127.0.0.1", 52341)) as base_client:
            # 一次性静态Bearer只用来bootstrap；之后的管理员API必须使用账号session。
            mode = base_client.get("/api/auth/mode")
            assert mode.status_code == 200
            assert mode.json() == {"mode": "accounts", "bootstrap_open": True}
            bootstrap = base_client.post(
                "/api/auth/bootstrap",
                headers={
                    "Authorization": f"Bearer {_ADMIN_TOKEN}",
                    "Origin": "http://testserver",
                },
                json={"username": "http-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            admin_user = bootstrap.json()["user"]
            assert base_client.get("/api/auth/mode").json()["bootstrap_open"] is False

            admin_client = _SessionClient(base_client)
            reader_client = _SessionClient(base_client)
            no_cookie_client = _SessionClient(base_client)

            admin_session = _login(admin_client, "http-admin", _ADMIN_PASSWORD)
            admin_headers = _same_origin(admin_session["csrf_token"])
            create_reader = admin_client.post(
                "/api/admin/users",
                headers=admin_headers,
                json={
                    "username": "http-reader",
                    "password": _READER_TEMP_PASSWORD,
                    "role": "reader",
                },
            )
            assert create_reader.status_code == 201, create_reader.text
            reader_user = create_reader.json()["user"]

            first_reader_session = _login(
                reader_client, "http-reader", _READER_TEMP_PASSWORD
            )
            assert first_reader_session["must_change_password"] is True
            assert reader_client.get("/api/admin/users").status_code == 403
            changed = reader_client.post(
                "/api/auth/password",
                headers=_same_origin(first_reader_session["csrf_token"]),
                json={
                    "old_password": _READER_TEMP_PASSWORD,
                    "new_password": _READER_PASSWORD,
                },
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["must_change_password"] is False
            reader_session = _login(reader_client, "http-reader", _READER_PASSWORD)
            reader_user = reader_session["user"]
            reader_headers = _same_origin(reader_session["csrf_token"])

            # Release-target visibility is admin catalog management, not library read access.
            admin_candidates = admin_client.get("/api/admin/release-targets")
            assert admin_candidates.status_code == 200, admin_candidates.text
            visible_target_ids = {
                item["target_id"] for item in admin_candidates.json()["targets"]
            }
            assert visible_target_ids == {
                release_assets["targets"]["A"].target_id,
                release_assets["targets"]["B"].target_id,
            }
            assert reader_client.get("/api/admin/release-targets").status_code == 403

            # Ensure the active managed library/file resources exist before grants.
            resources = admin_client.get("/api/admin/resources")
            assert resources.status_code == 200, resources.text
            resource_keys = {
                (item["resource_kind"], item["resource_id"])
                for item in resources.json()["resources"]
            }
            assert ("library", file_record.library_id) in resource_keys
            assert ("managed_file", job.file_id) in resource_keys

            library_allow = admin_client.post(
                "/api/admin/grants",
                headers=admin_headers,
                json={
                    "user_id": reader_user["user_id"],
                    "resource_kind": "library",
                    "resource_id": file_record.library_id,
                    "allowed": True,
                    "expected_revision": 0,
                },
            )
            assert library_allow.status_code == 200, library_allow.text
            assert library_allow.json()["grant"]["allowed"] is True

            issued = reader_client.post(
                "/api/auth/credentials",
                headers=reader_headers,
                json={"label": "identity-http-reader-token"},
            )
            assert issued.status_code == 201, issued.text
            mcp_token = issued.json()["credential"]["token"]
            mcp_headers = {"Authorization": f"Bearer {mcp_token}"}

            libraries_before = reader_client.get("/api/admin/libraries")
            assert libraries_before.status_code == 200, libraries_before.text
            visible_file = next(
                source_item
                for library in libraries_before.json()["libraries"]
                if library["library_id"] == file_record.library_id
                for source_item in library["sources"]
                if source_item.get("file_id") == job.file_id
            )
            assert visible_file["version_id"] == file_record.active_version_id
            mcp_files_before = no_cookie_client.get("/api/mcp/files", headers=mcp_headers)
            assert mcp_files_before.status_code == 200, mcp_files_before.text
            assert any(item["file_id"] == job.file_id for item in mcp_files_before.json()["files"])

            # Source fetch binds to the version returned with search/list evidence.
            current_source = no_cookie_client.get(
                f"/api/mcp/files/{job.file_id}/source",
                headers=mcp_headers,
                params={"version_id": file_record.active_version_id},
            )
            assert current_source.status_code == 200, current_source.text
            assert b"http_private_marker" in current_source.content

            replacement_source = tmp_path / "http-managed-guide-v2.txt"
            replacement_source.write_text(
                "Synthetic replacement source with http_replacement_marker.\n",
                encoding="utf-8",
            )
            replacement = manager.submit_import(
                replacement_source,
                display_name="http-managed-guide.txt",
                replace_file_id=job.file_id,
            )
            assert manager.run_job(replacement.job_id).status == "completed"
            replacement_record = manager.get_file(job.file_id)
            assert replacement_record.active_version_id != file_record.active_version_id
            stale_source = no_cookie_client.get(
                f"/api/mcp/files/{job.file_id}/source",
                headers=mcp_headers,
                params={"version_id": file_record.active_version_id},
            )
            assert stale_source.status_code == 409, stale_source.text
            assert b"http_private_marker" not in stale_source.content
            current_replacement = no_cookie_client.get(
                f"/api/mcp/files/{job.file_id}/source",
                headers=mcp_headers,
                params={"version_id": replacement_record.active_version_id},
            )
            assert current_replacement.status_code == 200, current_replacement.text
            assert b"http_replacement_marker" in current_replacement.content

            # The account cookie, static bootstrap token, and personal MCP credential are distinct.
            assert no_cookie_client.get(
                "/api/admin/users", headers={"Authorization": f"Bearer {_ADMIN_TOKEN}"}
            ).status_code == 401
            assert no_cookie_client.get(
                "/api/admin/users", headers=mcp_headers
            ).status_code == 401
            session_token = reader_client.token
            assert session_token
            session_bearer = no_cookie_client.get(
                "/api/mcp/files", headers={"Authorization": f"Bearer {session_token}"}
            )
            assert session_bearer.status_code == 401, session_bearer.text
            assert reader_client.get("/api/mcp/files").status_code == 401
            assert reader_client.get("/api/admin/users").status_code == 403

            file_deny = admin_client.post(
                "/api/admin/grants",
                headers=admin_headers,
                json={
                    "user_id": reader_user["user_id"],
                    "resource_kind": "managed_file",
                    "resource_id": job.file_id,
                    "allowed": False,
                    "expected_revision": 0,
                },
            )
            assert file_deny.status_code == 200, file_deny.text
            assert file_deny.json()["grant"]["allowed"] is False

            libraries_after = reader_client.get("/api/admin/libraries")
            assert libraries_after.status_code == 200, libraries_after.text
            library_view = next(
                item for item in libraries_after.json()["libraries"]
                if item["library_id"] == file_record.library_id
            )
            assert all(item.get("file_id") != job.file_id for item in library_view["sources"])
            mcp_libraries = no_cookie_client.get("/api/mcp/libraries", headers=mcp_headers)
            assert mcp_libraries.status_code == 200, mcp_libraries.text
            assert all(
                item.get("file_id") != job.file_id
                for library in mcp_libraries.json()["libraries"]
                for item in library.get("sources", [])
            )
            mcp_files_after = no_cookie_client.get("/api/mcp/files", headers=mcp_headers)
            assert mcp_files_after.status_code == 200, mcp_files_after.text
            assert all(item["file_id"] != job.file_id for item in mcp_files_after.json()["files"])

            denied_admin_search = reader_client.post(
                "/api/admin/search",
                json={
                    "library_id": file_record.library_id,
                    "source_type": "managed",
                    "file_id": job.file_id,
                    "version_id": file_record.active_version_id,
                    "query": "http_private_marker",
                },
            )
            assert denied_admin_search.status_code == 403
            denied_managed_search = reader_client.post(
                "/api/search", json={"file_id": job.file_id, "query": "http_private_marker"}
            )
            assert denied_managed_search.status_code == 403
            denied_download = reader_client.get(f"/api/files/{job.file_id}/download")
            assert denied_download.status_code == 403
            denied_mcp_search = no_cookie_client.post(
                "/api/mcp/search",
                headers=mcp_headers,
                json={
                    "library_id": file_record.library_id,
                    "source_type": "managed",
                    "file_id": job.file_id,
                    "version_id": file_record.active_version_id,
                    "query": "http_private_marker",
                },
            )
            assert denied_mcp_search.status_code == 403
            denied_mcp_download = no_cookie_client.get(
                f"/api/mcp/files/{job.file_id}/source",
                headers=mcp_headers,
                params={"version_id": replacement_record.active_version_id},
            )
            assert denied_mcp_download.status_code == 403

            audit = admin_client.get("/api/admin/identity-audit?limit=200")
            assert audit.status_code == 200, audit.text
            events = audit.json()["events"]
            grant_event = next(
                event for event in events
                if event["action"] == "resource_grant_changed"
                and event["target_id"] == job.file_id
                and event["metadata"].get("principal_id") == reader_user["user_id"]
            )
            assert grant_event["actor_user_id"] == admin_user["user_id"]
            assert grant_event["metadata"]["allowed"] is False
            assert grant_event["metadata"]["previous_allowed"] is None

            # 密码重置使原session与MCP token下一次请求失效；停用后新session也失效。
            reader_revision = next(
                item["revision"] for item in admin_client.get("/api/admin/users").json()["users"]
                if item["user_id"] == reader_user["user_id"]
            )
            reset = admin_client.post(
                f"/api/admin/users/{reader_user['user_id']}/reset-password",
                headers=admin_headers,
                json={
                    "new_password": _READER_RESET_PASSWORD,
                    "expected_revision": reader_revision,
                },
            )
            assert reset.status_code == 200, reset.text
            assert reader_client.get("/api/auth/me").status_code == 401
            assert no_cookie_client.get("/api/mcp/files", headers=mcp_headers).status_code == 401

            reset_session = _login(
                reader_client, "http-reader", _READER_RESET_PASSWORD
            )
            assert reset_session["must_change_password"] is True
            assert reader_client.get("/api/admin/users").status_code == 403
            completed_change = reader_client.post(
                "/api/auth/password",
                headers=_same_origin(reset_session["csrf_token"]),
                json={
                    "old_password": _READER_RESET_PASSWORD,
                    "new_password": _READER_PASSWORD,
                },
            )
            assert completed_change.status_code == 200, completed_change.text
            reauthenticated = _login(reader_client, "http-reader", _READER_PASSWORD)
            reauthenticated_headers = _same_origin(reauthenticated["csrf_token"])
            fresh_credential = reader_client.post(
                "/api/auth/credentials",
                headers=reauthenticated_headers,
                json={"label": "identity-http-disable-check"},
            )
            assert fresh_credential.status_code == 201, fresh_credential.text
            fresh_mcp_token = fresh_credential.json()["credential"]["token"]

            reader_revision = next(
                item["revision"]
                for item in admin_client.get("/api/admin/users").json()["users"]
                if item["user_id"] == reader_user["user_id"]
            )
            disabled = admin_client.patch(
                f"/api/admin/users/{reader_user['user_id']}",
                headers=admin_headers,
                json={"active": False, "expected_revision": reader_revision},
            )
            assert disabled.status_code == 200, disabled.text
            assert reader_client.get("/api/auth/me").status_code == 401
            assert no_cookie_client.get(
                "/api/mcp/files", headers={"Authorization": f"Bearer {fresh_mcp_token}"}
            ).status_code == 401
    finally:
        qdrant.close()
