from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from wenjin.management_http.app import create_app


class MinimalManager:
    def resume_pending_jobs(self):
        return []

    def close(self):
        return None


@pytest.mark.integration
def test_http_call_log_connections_close_across_lifespans_and_rollback(tmp_path, monkeypatch):
    """写读真实calls.sqlite，保留连接引用以防GC掩盖未关闭句柄。"""
    root = (tmp_path / "synthetic-app-root").resolve()
    root.mkdir()
    calls_path = root / "data" / "file-management" / "calls.sqlite"

    real_connect = sqlite3.connect
    opened_calls_connections: list[sqlite3.Connection] = []

    def tracked_connect(database, *args, **kwargs):
        connection = real_connect(database, *args, **kwargs)
        try:
            if Path(database).resolve() == calls_path:
                # Strong references ensure CPython refcount/GC cannot close a leak
                # after the HTTP operation has returned.
                opened_calls_connections.append(connection)
        except (TypeError, OSError):
            pass
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    app = create_app(root, manager=MinimalManager(), admin_token="synthetic-admin-token")
    headers = {"Authorization": "Bearer synthetic-admin-token"}

    # Re-enter the same app lifespan, as a process restart would, and prove the
    # second lifespan can read rows written during the first one.
    for expected_total in (1, 2):
        with TestClient(app) as client:
            config = client.get("/api/admin/config", headers=headers)
            assert config.status_code == 200, config.text
            listed = client.get("/api/admin/calls?type=http", headers=headers)
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == expected_total

            if expected_total == 2:
                store = app.state.call_logs
                before = store.list_calls(call_type="http")["total"]
                failed_request_id = uuid4().hex
                with pytest.raises(RuntimeError, match="synthetic transaction abort"):
                    with store._connect() as db:
                        db.execute(
                            """INSERT INTO http_calls(
                                request_id,occurred_at,target,retrieval_target,duration_ms,
                                status,status_code,error_code
                            ) VALUES(?,?,?,?,?,?,?,?)""",
                            (
                                failed_request_id,
                                datetime.now(timezone.utc).isoformat(),
                                "GET /api/synthetic-failure",
                                None,
                                0.0,
                                "success",
                                200,
                                None,
                            ),
                        )
                        raise RuntimeError("synthetic transaction abort")
                after = store.list_calls(call_type="http")
                assert after["total"] == before
                assert all(
                    row["request_id"] != failed_request_id for row in after["items"]
                )

    assert len(opened_calls_connections) >= 7
    still_open = []
    for connection in opened_calls_connections:
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            continue
        still_open.append(connection)
    # A rename followed by deletion is a direct Windows handle-release check.
    released_path = calls_path.with_name("calls-released.sqlite")
    rename_failure = None
    try:
        calls_path.replace(released_path)
    except OSError as error:
        rename_failure = f"{type(error).__name__}: winerror={getattr(error, 'winerror', None)}"
    assert not still_open and rename_failure is None, (
        f"open_handles={len(still_open)}; calls.sqlite rename failure={rename_failure}"
    )
    assert released_path.is_file()
    assert root.is_relative_to(tmp_path.resolve())
    shutil.rmtree(root)
    assert not root.exists()
