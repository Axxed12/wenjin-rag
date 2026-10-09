"""账号态HTTP到生产评测Factory/runner的单题合成闭环。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.evaluation_tasks.runtime import EvaluationRuntimeFactory
from wenjin.management_http.app import create_app

_BOOTSTRAP_TOKEN = "evaluation-runtime-e2e-bootstrap-token"
_PASSWORD = "Synthetic_Admin_Password_2026!"

pytestmark = pytest.mark.integration


class _Admin:
    def __init__(self, *, qdrant, embedder):
        self.qdrant_client = qdrant
        self.embedder = embedder
        self.embedding_model = "synthetic-marker-embedder-v1"
        self._model_lock = threading.Lock()
        self.closed = False

    def resume_pending_jobs(self):
        return []

    def close(self):
        self.closed = True


class _SyntheticReranker:
    instances = []

    def __init__(self, _path, *, device, max_length, batch_size):
        assert (device, max_length, batch_size) == ("cpu", 1024, 2)
        self.closed = False
        type(self).instances.append(self)

    def score(self, _query, documents):
        return [1.0 - index / max(1, len(documents)) for index, _ in enumerate(documents)]

    def close(self):
        self.closed = True


def _origin(csrf: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def test_create_app_account_login_submit_poll_and_read_synthetic_evaluation(tmp_path, monkeypatch):
    root = tmp_path / "evaluation-http-runtime-root"
    root.mkdir()
    assets = build_two_version_release_fixture(root=root, library_id="synthetic-lib")
    qdrant = assets["qdrant"]
    try:
        target = assets["targets"]["A"]
        route_path = root / "configs" / "indexes" / "multicorpus-candidates-003.json"
        route_path.parent.mkdir(parents=True)
        route_path.write_text(
            json.dumps({"schema_version": 1, "candidates": [target.__dict__]}),
            encoding="utf-8",
        )
        dataset_path = root / "evaluation" / "datasets" / "developer-validation-2026-10-04.jsonl"
        dataset_path.parent.mkdir(parents=True)
        row = {
            "case_id": "dv-gdal-003",
            "query": "release marker A runtime fixture",
            "library": "synthetic-lib",
            "version": "1.0",
        }
        raw_dataset = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        dataset_path.write_bytes(raw_dataset)
        dataset_path.with_suffix(".sha256").write_text(
            hashlib.sha256(raw_dataset).hexdigest() + "  developer-validation-2026-10-04.jsonl\n",
            encoding="ascii",
        )
        for name in ("embedding", "reranker"):
            model_dir = root / "models" / name
            model_dir.mkdir(parents=True)
            (model_dir / "synthetic-model-marker.bin").write_bytes(f"synthetic-{name}".encode())

        settings = SimpleNamespace(
            embedding_model_path=Path("models/embedding"),
            reranker_model_path=Path("models/reranker"),
        )
        manager = _Admin(qdrant=qdrant, embedder=assets["embedder"])
        monkeypatch.setattr(
            EvaluationRuntimeFactory, "_runtime_environment",
            staticmethod(lambda: {"device": "cpu", "runtime": "synthetic-http-test"}),
        )
        monkeypatch.setattr("wenjin.retrieval.rerank.Qwen3Reranker", _SyntheticReranker)
        _SyntheticReranker.instances.clear()

        app = create_app(
            root, admin_token=_BOOTSTRAP_TOKEN, manager=manager, settings=settings,
            identity_db=root / "data" / "identity.sqlite",
        )
        with TestClient(app, client=("127.0.0.1", 52342)) as client:
            mode = client.get("/api/auth/mode")
            assert mode.json() == {"mode": "accounts", "bootstrap_open": True}
            bootstrap = client.post(
                "/api/auth/bootstrap",
                headers={**_origin(), "Authorization": f"Bearer {_BOOTSTRAP_TOKEN}"},
                json={"username": "runtime-admin", "password": _PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            login = client.post(
                "/api/auth/login", headers=_origin(),
                json={"username": "runtime-admin", "password": _PASSWORD},
            )
            assert login.status_code == 200, login.text
            csrf = login.json()["csrf_token"]
            actor = app.state.identity_store.resolve_session(client.cookies["wenjin_session"])
            app.state.identity_store.synchronize_resource_catalog({("library", "synthetic-lib")})
            granted = client.post(
                "/api/admin/grants", headers=_origin(csrf),
                json={
                    "resource_kind": "library", "resource_id": "synthetic-lib",
                    "user_id": actor.user_id, "allowed": True, "expected_revision": 0,
                },
            )
            assert granted.status_code == 200, granted.text

            templates = client.get("/api/admin/evaluations/templates")
            assert templates.status_code == 200, templates.text
            submitted = client.post(
                "/api/admin/evaluations", headers=_origin(csrf),
                json={"experiment_id": "gdal_dstalpha_diagnostic_v1"},
            )
            assert submitted.status_code == 202, submitted.text
            job_id = submitted.json()["job_id"]

            deadline = time.monotonic() + 10
            status = None
            while time.monotonic() < deadline:
                response = client.get(f"/api/admin/evaluations/{job_id}")
                assert response.status_code == 200, response.text
                status = response.json()
                if status["status"] in {"completed", "failed", "integrity_failure", "cancelled"}:
                    break
                time.sleep(0.01)
            assert status is not None and status["status"] == "completed", status
            assert status["completed_cases"] == status["total_cases"] == 1

            summary_response = client.get(
                f"/api/admin/evaluations/{job_id}/result?file=summary.json"
            )
            assert summary_response.status_code == 200, summary_response.text
            summary = summary_response.json()
            assert summary["status"] == "completed"
            assert summary["case_count"] == 1
            assert summary["profiles"]["baseline"]["cases"] == 1
            case_response = client.get(
                f"/api/admin/evaluations/{job_id}/result?case_id=dv-gdal-003"
            )
            assert case_response.status_code == 200, case_response.text
            selected = case_response.json()["profiles"]["baseline"]["selected"]
            assert any("release_marker_A" in item["text"] for item in selected)
            assert manager.closed is False

        assert manager.closed is True
        assert all(item.closed for item in _SyntheticReranker.instances)
    finally:
        qdrant.close()
