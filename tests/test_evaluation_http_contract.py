"""评测管理API最小安全合同；只用临时身份库和合成manager。"""

from __future__ import annotations

from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from wenjin.management_http.app import create_app


pytestmark = pytest.mark.integration

_TOKEN = "evaluation-http-bootstrap-synthetic-token"
_ADMIN_PASSWORD = "Synthetic_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Synthetic_Reader_Temporary_2026!"
_READER_PASSWORD = "Synthetic_Reader_Changed_2026!"


class _Manager:
    def __init__(self):
        self.closed = 0

    def resume_pending_jobs(self):
        return []

    def close(self):
        self.closed += 1


class _Session:
    """在同一个TestClient/lifespan里为每个请求显式切换账号cookie。"""

    def __init__(self, client):
        self.client = client
        self.cookie = None

    def request(self, method, path, **kwargs):
        previous = self.client.cookies.get("wenjin_session")
        self.client.cookies.delete("wenjin_session")
        if self.cookie:
            self.client.cookies.set("wenjin_session", self.cookie)
        try:
            response = self.client.request(method, path, **kwargs)
            parsed = SimpleCookie()
            parsed.load(response.headers.get("set-cookie", ""))
            if "wenjin_session" in parsed:
                self.cookie = parsed["wenjin_session"].value
            return response
        finally:
            self.client.cookies.delete("wenjin_session")
            if previous:
                self.client.cookies.set("wenjin_session", previous)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self.request("POST", path, **kwargs)


def _origin(csrf: str | None = None):
    headers = {"Origin": "http://testserver"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def _login(session, username, password):
    response = session.post(
        "/api/auth/login", headers=_origin(),
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_evaluation_routes_are_admin_only_allowlist_payload_and_sanitize_errors(tmp_path):
    root = tmp_path / "eval-http-root"
    root.mkdir()
    manager = _Manager()
    app = create_app(
        root, admin_token=_TOKEN, manager=manager, settings=SimpleNamespace(),
        identity_db=root / "data" / "identity.sqlite",
    )

    with TestClient(app, client=("127.0.0.1", 52342)) as client:
        admin = _Session(client)
        reader = _Session(client)
        mode = client.get("/api/auth/mode")
        assert mode.json() == {"mode": "accounts", "bootstrap_open": True}
        bootstrap = client.post(
            "/api/auth/bootstrap",
            headers={**_origin(), "Authorization": f"Bearer {_TOKEN}"},
            json={"username": "eval-admin", "password": _ADMIN_PASSWORD},
        )
        assert bootstrap.status_code == 201, bootstrap.text
        admin_auth = _login(admin, "eval-admin", _ADMIN_PASSWORD)

        templates = admin.get("/api/admin/evaluations/templates")
        assert templates.status_code == 200, templates.text
        assert {item["experiment_id"] for item in templates.json()["templates"]} == {
            "gdal_dstalpha_diagnostic_v1", "developer_four_arm_v1",
        }
        extra_input = admin.post(
            "/api/admin/evaluations",
            headers=_origin(admin_auth["csrf_token"]),
            json={"experiment_id": "gdal_dstalpha_diagnostic_v1", "root": "caller-path"},
        )
        assert extra_input.status_code == 422, extra_input.text

        create_reader = admin.post(
            "/api/admin/users",
            headers=_origin(admin_auth["csrf_token"]),
            json={
                "username": "eval-reader", "password": _READER_TEMP_PASSWORD,
                "role": "reader",
            },
        )
        assert create_reader.status_code == 201, create_reader.text
        temporary = _login(reader, "eval-reader", _READER_TEMP_PASSWORD)
        changed = reader.post(
            "/api/auth/password",
            headers=_origin(temporary["csrf_token"]),
            json={"old_password": _READER_TEMP_PASSWORD, "new_password": _READER_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        _login(reader, "eval-reader", _READER_PASSWORD)
        assert reader.get("/api/admin/evaluations/templates").status_code == 403
        assert reader.post(
            "/api/admin/evaluations", headers=_origin(),
            json={"experiment_id": "gdal_dstalpha_diagnostic_v1"},
        ).status_code == 403

        # 账号态接口不接受旧式静态管理Bearer；也不触发运行时绑定。
        bearer_only = client.get(
            "/api/admin/evaluations/templates",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert bearer_only.status_code == 401

        async def fail_preflight(_actor, _experiment_id):
            raise RuntimeError("synthetic-secret /synthetic/private/model/path")

        app.state.evaluation_service.submit = fail_preflight
        rejected = admin.post(
            "/api/admin/evaluations",
            headers=_origin(admin_auth["csrf_token"]),
            json={"experiment_id": "gdal_dstalpha_diagnostic_v1"},
        )
        assert rejected.status_code == 503
        assert rejected.json() == {
            "error": "evaluation_unavailable",
            "detail": "评测当前不可用，请检查固定题集、资料版本和本机运行时配置",
        }
        assert "synthetic-secret" not in rejected.text
        assert "/synthetic/private/model/path" not in rejected.text

    assert manager.closed == 1
