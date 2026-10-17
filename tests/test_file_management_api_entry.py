from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from wenjin.file_management.__main__ import _parser
from wenjin.file_management.api import create_app, validate_listen_security


@dataclass(frozen=True)
class FakeJob:
    job_id: str
    file_id: str
    status: str
    resources: dict


class FakeManager:
    """API入口测试替身；只模拟已持久提交后的service边界。"""

    def __init__(self, root: Path, *, block=False, fail_resume=False):
        self.root = root
        self.base = root / "data" / "file-management"
        self.base.mkdir(parents=True)
        self.jobs = {}
        self.paths = {}
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail_resume = fail_resume
        self.closed = False
        if not block:
            self.release.set()

    def list_files(self):
        return []

    def get_file(self, file_id):
        return {"file_id": file_id}

    def submit_import(
        self, source, *, display_name=None, replace_file_id=None, original_filename=None
    ):
        job_id = f"job-{len(self.jobs) + 1}"
        owned = self.base / "staging" / job_id / source.name
        owned.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, owned)
        self.paths[job_id] = owned
        job = FakeJob(job_id=job_id, file_id="managed-1", status="queued", resources={})
        self.jobs[job_id] = job
        return job

    def submit_adopt(self, file_id):
        raise KeyError(file_id)

    def submit_delete(self, file_id):
        return self.submit_import(Path("unused"))

    def submit_retry(self, job_id):
        return self.jobs[job_id]

    def get_job(self, job_id):
        return self.jobs[job_id]

    def resume_pending_jobs(self):
        if self.fail_resume:
            raise RuntimeError("resume failed")
        return []

    def close(self):
        self.closed = True

    def run_job(self, job_id):
        with self.lock:
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.started.set()
        try:
            self.release.wait(timeout=5)
            time.sleep(0.03)
        finally:
            with self.lock:
                self.running -= 1

    def search(self, file_id, query):
        return {"file_id": file_id, "query": query}

    def get_original(self, file_id, relative_path=None):
        return b"original"

    def inspect_resources(self, file_id):
        return {"file_id": file_id, "consistent": True}


def test_listen_security_and_cli_args():
    validate_listen_security("127.0.0.1", None)
    validate_listen_security("localhost", None)
    with pytest.raises(ValueError, match="非loopback"):
        validate_listen_security("0.0.0.0", None)
    validate_listen_security("0.0.0.0", "admin-token")

    download = _parser().parse_args(["download", "file-1", "out.bin"])
    assert download.file_id == "file-1"
    assert download.output == Path("out.bin")
    status = _parser().parse_args(["job", "job-1"])
    assert status.job_id == "job-1"


def test_upload_limit_cleans_temporary_file(tmp_path):
    manager = FakeManager(tmp_path)
    app = create_app(tmp_path, manager=manager, max_upload_bytes=4)
    with TestClient(app) as client:
        response = client.post(
            "/api/files", content=b"12345", headers={"X-File-Name": "doc.txt"}
        )
        assert response.status_code == 413
    incoming = manager.base / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_upload_temp_cleanup_and_serial_worker_does_not_block_status(tmp_path):
    manager = FakeManager(tmp_path, block=True)
    app = create_app(tmp_path, manager=manager)
    with TestClient(app) as client:
        first = client.post(
            "/api/files", content=b"one", headers={"X-File-Name": "one.txt"}
        )
        second = client.post(
            "/api/files", content=b"two", headers={"X-File-Name": "two.txt"}
        )
        assert first.status_code == second.status_code == 202
        assert manager.started.wait(timeout=2)
        # job在后台等待时，查询路由仍然处理，不跟着计算线程阻塞。
        started = time.perf_counter()
        assert client.get("/api/files").status_code == 200
        assert time.perf_counter() - started < 1
        incoming = manager.base / ".incoming"
        assert not incoming.exists() or list(incoming.iterdir()) == []
        manager.release.set()
    assert manager.max_running == 1


def test_http_scheduler_records_queued_job_wait_behind_single_worker(tmp_path):
    """Observe accepted-upload and actual run_job-entry times around a worker barrier."""
    from wenjin.management_http.app import create_app as create_management_app

    class TimelineManager(FakeManager):
        def __init__(self, root):
            super().__init__(root, block=True)
            self.entry_times = []
            self.entry_events = [threading.Event(), threading.Event()]

        def run_job(self, job_id):
            with self.lock:
                self.entry_times.append((job_id, time.monotonic()))
                index = len(self.entry_times) - 1
                if index < len(self.entry_events):
                    self.entry_events[index].set()
            return super().run_job(job_id)

    manager = TimelineManager(tmp_path)
    token = "SyntheticManagementBearer_QueueWait_20261017"
    app = create_management_app(tmp_path, manager=manager, admin_token=token)
    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        a_post_started = time.monotonic()
        first = client.post("/api/files", content=b"synthetic A", headers=headers | {"X-File-Name": "a.txt"})
        a_accepted = time.monotonic()
        assert first.status_code == 202
        assert manager.entry_events[0].wait(2), "job A never entered the single worker"
        a_entered = manager.entry_times[0][1]

        b_post_started = time.monotonic()
        second = client.post("/api/files", content=b"synthetic B", headers=headers | {"X-File-Name": "b.txt"})
        b_accepted = time.monotonic()
        assert second.status_code == 202
        assert manager.entry_events[1].wait(0.1) is False, "job B entered while job A held the only worker"
        assert len(manager.entry_times) == 1

        release_a = time.monotonic()
        manager.release.set()
        assert manager.entry_events[1].wait(3), "job B did not enter after the worker became free"
        b_entered = manager.entry_times[1][1]
        assert manager.entry_times[0][0] == first.json()["job_id"]
        assert manager.entry_times[1][0] == second.json()["job_id"]
        # Job A may enter the background worker before its upload response is flushed.
        assert a_post_started <= a_entered
        assert b_post_started <= b_accepted < release_a <= b_entered
        timeline = {
            "job_a": {"post_started_monotonic": a_post_started, "accepted_monotonic": a_accepted, "run_job_entered_monotonic": a_entered},
            "job_b": {"post_started_monotonic": b_post_started, "accepted_monotonic": b_accepted, "run_job_entered_monotonic": b_entered},
            "worker_a_release_monotonic": release_a,
            "http_queue_wait_from_accept_seconds": b_entered - b_accepted,
            "worker_capacity": 1,
            "measurement": "synthetic HTTP scheduling only; not model/GPU latency",
        }
        print("HTTP_QUEUE_WAIT_TIMELINE=" + json.dumps(timeline, sort_keys=True))
    assert manager.max_running == 1


def test_bearer_protects_management_routes(tmp_path):
    app = create_app(tmp_path, manager=FakeManager(tmp_path), admin_token="secret")
    with TestClient(app) as client:
        assert client.get("/api/files").status_code == 401
        assert client.get("/api/files", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_startup_failure_closes_manager(tmp_path):
    manager = FakeManager(tmp_path, fail_resume=True)
    app = create_app(tmp_path, manager=manager)
    with pytest.raises(RuntimeError, match="resume failed"):
        with TestClient(app):
            pass
    assert manager.closed
