"""管理员代reader签发的个人MCP凭据必须保留reader授权边界。"""

from __future__ import annotations

import hashlib
from http.cookies import SimpleCookie

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from wenjin.file_management import FileManagementService
from wenjin.management_http.app import create_app

pytestmark = pytest.mark.integration

_ADMIN_TOKEN = "synthetic-bootstrap-only"
_ADMIN_PASSWORD = "Synthetic_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Synthetic_Reader_Temporary_2026!"
_READER_PASSWORD = "Synthetic_Reader_Changed_2026!"


class _TinyEmbedder:
    dimension = 8

    def _vector(self, text: str) -> list[float]:
        return [
            int(hashlib.sha256(f"{index}:{text}".encode()).hexdigest()[:4], 16) / 65535
            for index in range(self.dimension)
        ]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


class _CookieClient:
    """在同一TestClient lifespan中按请求切换两个隔离的session cookie。"""

    def __init__(self, client):
        self.client = client
        self.token = None

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


def _origin_csrf(session: dict) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": session["csrf_token"]}


def _login(client, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_issued_reader_credential_keeps_reader_identity_and_live_grants(tmp_path):
    """使用临时SQLite、Qdrant内存库和合成TXT验证真实HTTP认证/授权链。"""
    root = tmp_path / "isolated-admin-mcp-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_TinyEmbedder(),
        embedding_model="synthetic-admin-mcp-credential-v1",
    )
    source = tmp_path / "synthetic-reader-guide.txt"
    source.write_text("Private synthetic guide with reader_grant_marker.\n", encoding="utf-8")
    job = manager.submit_import(
        source,
        display_name="Synthetic Reader Guide",
        library_id="synthetic-reader-library",
        library_name="Synthetic Reader Library",
    )
    assert manager.run_job(job.job_id).status == "completed"
    record = manager.get_file(job.file_id)

    app = create_app(
        root,
        admin_token=_ADMIN_TOKEN,
        manager=manager,
        identity_db=root / "data" / "identity" / "identity.sqlite",
    )
    try:
        with TestClient(app, client=("127.0.0.1", 52342)) as base:
            bootstrap = base.post(
                "/api/auth/bootstrap",
                headers={
                    "Authorization": f"Bearer {_ADMIN_TOKEN}",
                    "Origin": "http://testserver",
                },
                json={"username": "synthetic-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            admin_id = bootstrap.json()["user"]["user_id"]
            admin_client = _CookieClient(base)
            reader_client = _CookieClient(base)

            admin_session = _login(admin_client, "synthetic-admin", _ADMIN_PASSWORD)
            create = admin_client.post(
                "/api/admin/users",
                headers=_origin_csrf(admin_session),
                json={
                    "username": "synthetic-reader",
                    "password": _READER_TEMP_PASSWORD,
                    "role": "reader",
                },
            )
            assert create.status_code == 201, create.text
            reader_id = create.json()["user"]["user_id"]

            # 首次改密前，即使管理员指定user_id，也不能给该账号签发MCP凭据。
            premature = admin_client.post(
                f"/api/admin/users/{reader_id}/credentials",
                headers=_origin_csrf(admin_session),
                json={"label": "premature-reader-token"},
            )
            assert premature.status_code == 409, premature.text

            first_login = _login(reader_client, "synthetic-reader", _READER_TEMP_PASSWORD)
            assert first_login["must_change_password"] is True
            changed = reader_client.post(
                "/api/auth/password",
                headers=_origin_csrf(first_login),
                json={
                    "old_password": _READER_TEMP_PASSWORD,
                    "new_password": _READER_PASSWORD,
                },
            )
            assert changed.status_code == 200, changed.text
            reader_session = _login(reader_client, "synthetic-reader", _READER_PASSWORD)
            assert reader_session["user"]["user_id"] == reader_id
            assert reader_session["user"]["role"] == "reader"

            issued = admin_client.post(
                f"/api/admin/users/{reader_id}/credentials",
                headers=_origin_csrf(admin_session),
                json={"label": "admin-issued-reader-token"},
            )
            assert issued.status_code == 201, issued.text
            token = issued.json()["credential"]["token"]
            bearer = {"Authorization": f"Bearer {token}"}
            credential_id = issued.json()["credential"]["credential_id"]
            identity = app.state.identity_store.resolve_mcp_credential(token)
            assert identity.user_id == reader_id
            assert identity.role == "reader"
            assert identity.must_change_password is False

            # 管理员的列表与reader自助列表只含安全元数据，不重显token或token hash。
            admin_list = admin_client.get(f"/api/admin/users/{reader_id}/credentials")
            assert admin_list.status_code == 200, admin_list.text
            reader_list = reader_client.get("/api/auth/credentials")
            assert reader_list.status_code == 200, reader_list.text
            for credential_list in (
                admin_list.json()["credentials"],
                reader_list.json()["credentials"],
            ):
                listed = next(
                    item for item in credential_list if item["credential_id"] == credential_id
                )
                assert listed["user_id"] == reader_id
                assert listed["token_prefix"] == issued.json()["credential"]["token_prefix"]
                assert {"created_at", "expires_at", "revoked_at", "label"} <= listed.keys()
                assert not {"token", "token_hash", "secret"} & listed.keys()
                assert token not in str(listed)

            # reader不能枚举他人凭据或撤销管理员名下的凭据。
            assert reader_client.get(f"/api/admin/users/{admin_id}/credentials").status_code == 403
            admin_own = admin_client.post(
                "/api/auth/credentials",
                headers=_origin_csrf(admin_session),
                json={"label": "synthetic-admin-owned"},
            )
            assert admin_own.status_code == 201, admin_own.text
            admin_own_credential_id = admin_own.json()["credential"]["credential_id"]
            assert (
                reader_client.request(
                    "DELETE",
                    f"/api/auth/credentials/{admin_own_credential_id}",
                    headers=_origin_csrf(reader_session),
                ).status_code
                == 403
            )
            assert (
                app.state.identity_store.resolve_mcp_credential(
                    admin_own.json()["credential"]["token"]
                ).user_id
                == admin_id
            )

            # 该Bearer只适用于MCP入口；不能用来获得管理员HTTP能力。
            assert base.get("/api/admin/users", headers=bearer).status_code == 401
            assert base.get("/api/mcp/files", headers=bearer).json() == {"files": []}
            denied_search = base.post(
                "/api/mcp/search",
                headers=bearer,
                json={"file_id": record.file_id, "query": "reader_grant_marker"},
            )
            assert denied_search.status_code == 403, denied_search.text

            # 每个新的MCP HTTP请求均实时读取授权：不授予时拒绝，授予后可见，撤销后立刻隐藏。
            admin_client.get("/api/admin/resources")
            grant = admin_client.post(
                "/api/admin/grants",
                headers=_origin_csrf(admin_session),
                json={
                    "user_id": reader_id,
                    "resource_kind": "library",
                    "resource_id": record.library_id,
                    "allowed": True,
                    "expected_revision": 0,
                },
            )
            assert grant.status_code == 200, grant.text
            assert grant.json()["grant"]["allowed"] is True
            granted = base.get("/api/mcp/files", headers=bearer)
            assert granted.status_code == 200, granted.text
            assert [item["file_id"] for item in granted.json()["files"]] == [record.file_id]
            searched = base.post(
                "/api/mcp/search",
                headers=bearer,
                json={"file_id": record.file_id, "query": "reader_grant_marker"},
            )
            assert searched.status_code == 200, searched.text
            assert any(
                "reader_grant_marker" in item.get("text", "")
                for item in searched.json()["evidence"]
            )

            revoked = admin_client.post(
                "/api/admin/grants",
                headers=_origin_csrf(admin_session),
                json={
                    "user_id": reader_id,
                    "resource_kind": "library",
                    "resource_id": record.library_id,
                    "allowed": False,
                    "expected_revision": 1,
                },
            )
            assert revoked.status_code == 200, revoked.text
            denied_after_revoke = base.get("/api/mcp/files", headers=bearer)
            assert denied_after_revoke.status_code == 200, denied_after_revoke.text
            assert denied_after_revoke.json()["files"] == []
            search_after_revoke = base.post(
                "/api/mcp/search",
                headers=bearer,
                json={"file_id": record.file_id, "query": "reader_grant_marker"},
            )
            assert search_after_revoke.status_code == 403, search_after_revoke.text

            # reader可撤销自己的token；撤销后MCP入口立即拒绝该凭据。
            self_revoked = reader_client.request(
                "DELETE",
                f"/api/auth/credentials/{credential_id}",
                headers=_origin_csrf(reader_session),
            )
            assert self_revoked.status_code == 200, self_revoked.text
            assert base.get("/api/mcp/files", headers=bearer).status_code == 401
    finally:
        qdrant.close()
