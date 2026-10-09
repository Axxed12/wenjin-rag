"""定向身份时限与评测授权HTTP合同，全部状态位于pytest临时目录。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from wenjin.evaluation_tasks.service import (
    EvaluationContext,
    EvaluationJobStore,
    EvaluationSpec,
)
from wenjin.management_http.app import create_app
from wenjin.retrieval.strategy_experiment import (
    checkpoint_record_hash,
    run_identity_sha256,
)

pytestmark = pytest.mark.integration

_BOOTSTRAP = "admin-auth-eval-synthetic-bootstrap"
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


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _origin(csrf: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def _bootstrap(client: TestClient) -> None:
    response = client.post(
        "/api/auth/bootstrap",
        headers={**_origin(), "Authorization": f"Bearer {_BOOTSTRAP}"},
        json={"username": "synthetic-admin", "password": _ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text


def _login(client: TestClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login", headers=_origin(),
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_forced_password_change_csrf_and_real_session_deadlines(tmp_path, monkeypatch):
    root = tmp_path / "auth-root"
    root.mkdir()
    app = create_app(
        root, admin_token=_BOOTSTRAP, manager=_Manager(),
        identity_db=root / "data" / "identity.sqlite",
    )

    with TestClient(app, client=("127.0.0.1", 52431)) as client:
        _bootstrap(client)
        admin_login = _login(client, "synthetic-admin", _ADMIN_PASSWORD)
        create = client.post(
            "/api/admin/users",
            headers=_origin(admin_login["csrf_token"]),
            json={
                "username": "synthetic-reader",
                "password": _READER_TEMP_PASSWORD,
                "role": "reader",
            },
        )
        assert create.status_code == 201, create.text

        clock = _Clock(datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc))
        store = app.state.identity_store
        monkeypatch.setattr(store, "_now", clock.now)
        login = _login(client, "synthetic-reader", _READER_TEMP_PASSWORD)
        assert login["must_change_password"] is True
        assert client.get("/api/admin/users").status_code == 403

        change_body = {
            "old_password": _READER_TEMP_PASSWORD,
            "new_password": _READER_PASSWORD,
        }
        missing_origin = client.post("/api/auth/password", json=change_body)
        assert missing_origin.status_code == 403
        wrong_origin = client.post(
            "/api/auth/password",
            headers={"Origin": "https://foreign.invalid", "X-CSRF-Token": login["csrf_token"]},
            json=change_body,
        )
        assert wrong_origin.status_code == 403
        missing_csrf = client.post(
            "/api/auth/password", headers={"Origin": "http://testserver"}, json=change_body,
        )
        assert missing_csrf.status_code == 403
        bad_csrf = client.post(
            "/api/auth/password", headers=_origin("wrong-synthetic-csrf-token-000000"),
            json=change_body,
        )
        assert bad_csrf.status_code == 403

        wrong_old = client.post(
            "/api/auth/password",
            headers=_origin(login["csrf_token"]),
            json={"old_password": "Incorrect_Synthetic_Old_Password_2026!",
                  "new_password": _READER_PASSWORD},
        )
        assert wrong_old.status_code == 422
        assert wrong_old.json() == {
            "error": "current_password_invalid",
            "detail": "当前密码不正确，请重新输入。",
        }
        assert client.get("/api/auth/me").status_code == 200, "失败的改密不得撤销当前会话"

        changed = client.post(
            "/api/auth/password", headers=_origin(login["csrf_token"]), json=change_body,
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["must_change_password"] is False
        assert client.cookies.get("wenjin_session") is None, "成功改密应撤销并清除旧session cookie"
        assert client.get("/api/auth/me").status_code == 401

        relogin = _login(client, "synthetic-reader", _READER_PASSWORD)
        assert relogin["must_change_password"] is False
        session_cookie = client.cookies.get("wenjin_session")
        absolute_seconds = store.session_absolute_seconds
        idle_seconds = store.session_idle_seconds
        assert idle_seconds == 30 * 60
        assert absolute_seconds == 12 * 60 * 60
        assert relogin["expires_at"] == (clock.value + timedelta(seconds=absolute_seconds)).isoformat(timespec="microseconds")
        cookie = client.cookies.jar
        session_cookie_entry = next(item for item in cookie if item.name == "wenjin_session")
        assert session_cookie_entry.expires is not None
        set_cookie = client.get("/api/auth/mode")  # unrelated public request does not rotate auth cookie
        assert set_cookie.status_code == 200
        assert client.cookies.get("wenjin_session") == session_cookie

        # 活跃请求滑动last_seen；空闲超过30分钟后认证失效。
        clock.advance(idle_seconds - 1)
        assert client.get("/api/auth/me").status_code == 200
        clock.advance(idle_seconds)
        idle_expired = client.get("/api/auth/me")
        assert idle_expired.status_code == 401

        # 新登录后以可控时钟跨过12小时；期间每次请求都在idle窗口内。
        clock.advance(1)
        absolute_login = _login(client, "synthetic-reader", _READER_PASSWORD)
        fixed_expiry = absolute_login["expires_at"]
        total = absolute_seconds - 1
        elapsed = 0
        while elapsed < total:
            step = min(1700, total - elapsed)
            clock.advance(step)
            elapsed += step
            response = client.get("/api/auth/me")
            assert response.status_code == 200, f"活跃session应在绝对期限前有效，elapsed={elapsed}"
        assert absolute_login["expires_at"] == fixed_expiry
        clock.advance(1)
        absolute_expired = client.get("/api/auth/me")
        assert absolute_expired.status_code == 401


def _install_synthetic_evaluation_service(app, root: Path):
    service = app.state.evaluation_service
    spec = EvaluationSpec(
        experiment_id="synthetic_auth_eval",
        title="合成授权合同",
        dataset_id="synthetic-dataset",
        profiles=("baseline",),
        max_cases=1,
        required_libraries=("synthetic-eval-library",),
    )
    assets = {"fixture": hashlib.sha256(b"synthetic-evaluation-assets").hexdigest()}
    case_identity = {"case_id": "case-1", "query": "synthetic marker query"}
    run_identity = {
        "experiment_id": spec.experiment_id,
        "profiles": ["baseline"],
        "assets": assets,
        "case_identities": {"case-1": case_identity},
    }
    input_snapshot = {
        "dataset_id": spec.dataset_id,
        "dataset_case_count": 1,
        "resource_ids": ["synthetic-eval-library"],
        "profiles": ["baseline"],
    }

    def binding(_spec, _job_id, _actor):
        return input_snapshot, run_identity

    class _Router:
        @staticmethod
        def verify_run_identity():
            return assets

    def context_factory(_job, _job_dir):
        return EvaluationContext(
            rows=[{"case_id": "case-1", "query": "synthetic marker query"}],
            router=_Router(), config={}, rewriter=None,
            input_snapshot=input_snapshot, run_identity=run_identity,
        )

    def runner(*, rows, checkpoint_dir, emit_case, progress, run_identity, **_kwargs):
        assert len(rows) == 1
        checkpoint = {
            "schema_version": 1,
            "case_id": "case-1",
            "run_identity_sha256": run_identity_sha256(run_identity),
            "case_identity": run_identity["case_identities"]["case-1"],
            "profiles": {
                "baseline": {
                    "selected": [], "ranked_candidates": [],
                    "timing_seconds": {"total": 0.0},
                },
            },
        }
        checkpoint["record_sha256"] = checkpoint_record_hash(checkpoint)
        emit_case(checkpoint)
        progress(1, 1, "case-1", "synthetic_complete")
        return {
            "status": "completed",
            "valid_case_ids": ["case-1"],
            "case_count": 1,
            "profiles": {
                "baseline": {
                    "cases": 1, "selected_evidence": 0,
                    "candidate_count": 0, "mean_total_seconds": 0.0,
                },
            },
            "assets_before": assets,
            "assets_after": assets,
            "assets_unchanged": True,
        }

    service.specs = {spec.experiment_id: spec}
    service.binding_factory = binding
    service.context_factory = context_factory
    service.runner = runner
    return service


def test_evaluation_submit_requires_real_library_grant_and_completes_synthetic_job(tmp_path):
    root = tmp_path / "eval-grant-root"
    root.mkdir()
    manager = _Manager()
    app = create_app(
        root, admin_token=_BOOTSTRAP, manager=manager,
        identity_db=root / "data" / "identity.sqlite",
    )

    with TestClient(app, client=("127.0.0.1", 52432)) as client:
        _bootstrap(client)
        admin = _login(client, "synthetic-admin", _ADMIN_PASSWORD)
        service = _install_synthetic_evaluation_service(app, root)
        identity = app.state.identity_store
        identity.synchronize_resource_catalog({("library", "synthetic-eval-library")})

        submit = {
            "experiment_id": "synthetic_auth_eval",
        }
        denied = client.post(
            "/api/admin/evaluations", headers=_origin(admin["csrf_token"]), json=submit,
        )
        assert denied.status_code == 403
        assert denied.json() == {
            "error": "evaluation_library_access_required",
            "reason_code": "library_read_grant_missing",
            "detail": "当前账号缺少此模板所需资料库的读取授权（1项）。请到“身份与授权”逐库授权后重试。",
            "missing_library_count": 1,
            "discoverable_missing_libraries": ["synthetic-eval-library"],
        }
        jobs, total = service.store.list(limit=20, offset=0)
        assert total == 0 and jobs == [], "缺少读取授权时不得创建任务记录"

        grant = client.post(
            "/api/admin/grants",
            headers=_origin(admin["csrf_token"]),
            json={
                "resource_kind": "library",
                "resource_id": "synthetic-eval-library",
                "user_id": admin["user"]["user_id"],
                "allowed": True,
                "expected_revision": 0,
            },
        )
        assert grant.status_code == 200, grant.text

        accepted = client.post(
            "/api/admin/evaluations", headers=_origin(admin["csrf_token"]), json=submit,
        )
        assert accepted.status_code == 202, accepted.text
        job_id = accepted.json()["job_id"]
        deadline = time.monotonic() + 3
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/admin/evaluations/{job_id}")
            assert status.status_code == 200, status.text
            if status.json()["status"] in {"completed", "failed", "integrity_failure"}:
                break
            time.sleep(0.01)
        assert status is not None
        assert status.json()["status"] == "completed", status.text
        result = client.get(f"/api/admin/evaluations/{job_id}/result?file=summary.json")
        assert result.status_code == 200, result.text
        assert result.json()["valid_case_ids"] == ["case-1"]

    assert manager.closed == 1
