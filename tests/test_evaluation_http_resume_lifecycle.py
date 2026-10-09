"""Synthetic HTTP coverage for evaluation recovery across app lifespans.

This contract exercises create_app's default EvaluationRuntimeFactory,
EvaluationTaskService, and production retrieval runner with a temporary root,
in-memory Qdrant collections, deterministic embeddings, and a fake reranker.
It does not load local models or inspect the real project data root.
"""

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
from wenjin.evaluation_tasks.service import EvaluationJobStore
from wenjin.file_management import FileManagementService
from wenjin.management_http.app import create_app


pytestmark = pytest.mark.integration

_BOOTSTRAP = "temporary-evaluation-resume-bootstrap"
_PASSWORD = "Synthetic_Resume_Admin_Password_2026!"
_LIBRARY = "synthetic-eval-library"
_CASE = "dv-gdal-003"


class _CountingEmbedder:
    def __init__(self, delegate):
        self.delegate = delegate
        self.query_calls = 0

    def get_query_embedding(self, query: str):
        self.query_calls += 1
        return self.delegate.get_query_embedding(query)

    def get_text_embedding_batch(self, texts, *, show_progress=False):
        return self.delegate.get_text_embedding_batch(texts, show_progress=show_progress)


class _Manager(FileManagementService):
    """Use the real temporary-root manager and count its app-owned close."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        super().close()


class _SyntheticReranker:
    instances = []

    def __init__(self, _path, *, device, max_length, batch_size):
        assert (device, max_length, batch_size) == ("cpu", 1024, 2)
        self.score_calls = 0
        self.closed = False
        type(self).instances.append(self)

    def score(self, _query, documents):
        self.score_calls += 1
        return [1.0 - index / max(1, len(documents)) for index, _ in enumerate(documents)]

    def close(self):
        self.closed = True


class _Gate:
    def __init__(self):
        self.case_saved = threading.Event()
        self.release_runner = threading.Event()
        self.shutdown_started = threading.Event()


def _headers(csrf: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def _write_runtime_inputs(root: Path, assets) -> SimpleNamespace:
    target = assets["targets"]["A"]
    route_path = root / "configs" / "indexes" / "multicorpus-candidates-003.json"
    route_path.parent.mkdir(parents=True)
    route_path.write_text(
        json.dumps({"schema_version": 1, "route_id": "synthetic-eval", "candidates": [target.__dict__]}),
        encoding="utf-8",
    )
    dataset = root / "evaluation" / "datasets" / "developer-validation-2026-10-04.jsonl"
    dataset.parent.mkdir(parents=True)
    row = {
        "case_id": _CASE,
        "query": "release marker A runtime fixture",
        "library": _LIBRARY,
        "version": "1.0",
    }
    raw = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    dataset.write_bytes(raw)
    dataset.with_suffix(".sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "  developer-validation-2026-10-04.jsonl\n",
        encoding="ascii",
    )
    for name in ("embedding", "reranker"):
        model_dir = root / "models" / name
        model_dir.mkdir(parents=True)
        (model_dir / "synthetic-model-marker.bin").write_bytes(f"synthetic-{name}".encode())
    return SimpleNamespace(
        embedding_model_path=Path("models/embedding"),
        reranker_model_path=Path("models/reranker"),
        search_reranker_enabled=True,
        reranker_max_length=1024,
        reranker_batch_size=2,
        resolved_embedding_model_id="synthetic-marker-embedder-v1",
        resolved_reranker_model_id="synthetic-marker-reranker-v1",
    )


def _build_app(root: Path, qdrant, embedder, settings):
    manager = _Manager(
        root=root, qdrant_client=qdrant, embedder=embedder,
        embedding_model=settings.resolved_embedding_model_id,
    )
    app = create_app(
        root, admin_token=_BOOTSTRAP, manager=manager, settings=settings,
        identity_db=root / "data" / "identity.sqlite",
    )
    return app, manager


def _bootstrap_login(client: TestClient, *, bootstrap: bool) -> str:
    if bootstrap:
        response = client.post(
            "/api/auth/bootstrap",
            headers={**_headers(), "Authorization": f"Bearer {_BOOTSTRAP}"},
            json={"username": "resume-admin", "password": _PASSWORD},
        )
        assert response.status_code == 201, response.text
    response = client.post(
        "/api/auth/login", headers=_headers(),
        json={"username": "resume-admin", "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _set_grant(
    client: TestClient, app, csrf: str, *, allowed: bool, revision: int,
    resource_id: str = _LIBRARY,
) -> None:
    actor = app.state.identity_store.resolve_session(client.cookies["wenjin_session"])
    response = client.post(
        "/api/admin/grants", headers=_headers(csrf),
        json={
            "resource_kind": "library", "resource_id": resource_id,
            "user_id": actor.user_id, "allowed": allowed,
            "expected_revision": revision,
        },
    )
    assert response.status_code == 200, response.text


def _wait(client: TestClient, job_id: str, *, terminal: set[str] | None = None) -> dict:
    terminal = terminal or {"completed", "cancelled", "interrupted", "failed", "integrity_failure"}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/api/admin/evaluations/{job_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in terminal:
            return payload
        time.sleep(0.02)
    raise AssertionError("synthetic evaluation did not reach a terminal state")


def _enter_shutdown(client: TestClient, gate: _Gate):
    failures: list[BaseException] = []
    done = threading.Event()

    def close_app():
        try:
            client.__exit__(None, None, None)
        except BaseException as error:
            failures.append(error)
        finally:
            done.set()

    worker = threading.Thread(target=close_app, name="synthetic-app-shutdown", daemon=True)
    worker.start()
    assert gate.shutdown_started.wait(5), "app shutdown was not observed"
    gate.release_runner.set()
    assert done.wait(10), "app shutdown did not finish"
    worker.join()
    assert not failures, failures


@pytest.fixture
def runtime_harness(tmp_path, monkeypatch):
    root = tmp_path / "evaluation-resume-http-root"
    root.mkdir()
    assets = build_two_version_release_fixture(root=root, library_id=_LIBRARY)
    embedder = _CountingEmbedder(assets["embedder"])
    settings = _write_runtime_inputs(root, assets)
    gates: list[_Gate] = []
    runner_calls: list[dict] = []
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)
    monkeypatch.setattr("wenjin.retrieval.rerank.Qwen3Reranker", _SyntheticReranker)
    monkeypatch.setattr(
        EvaluationRuntimeFactory, "_runtime_environment",
        staticmethod(lambda: {"device": "cpu", "runtime": "synthetic-resume-contract"}),
    )
    _SyntheticReranker.instances.clear()

    original_create_service = EvaluationRuntimeFactory.create_service

    def instrumented_create_service(factory, identity_store, task_store):
        service = original_create_service(factory, identity_store, task_store)
        production_runner = service.runner

        def runner(**kwargs):
            runner_calls.append(dict(kwargs))
            gate = gates[0] if gates else None
            if gate is None:
                return production_runner(**kwargs)
            emit_case = kwargs["emit_case"]

            def held_emit(record):
                emit_case(record)
                gate.case_saved.set()
                if not gate.release_runner.wait(10):
                    raise TimeoutError("synthetic runner gate timed out")

            kwargs["emit_case"] = held_emit
            return production_runner(**kwargs)

        service.runner = runner
        original_shutdown = service.shutdown

        async def observed_shutdown():
            if gates:
                gates[0].shutdown_started.set()
            await original_shutdown()

        service.shutdown = observed_shutdown
        return service

    monkeypatch.setattr(EvaluationRuntimeFactory, "create_service", instrumented_create_service)
    try:
        yield root, assets, embedder, settings, gates, runner_calls
    finally:
        assets["qdrant"].close()


def _open_first_app(root, assets, embedder, settings):
    app, manager = _build_app(root, assets["qdrant"], embedder, settings)
    client = TestClient(app, client=("127.0.0.1", 52342))
    client.__enter__()
    csrf = _bootstrap_login(client, bootstrap=app.state.identity_store.bootstrap_is_open())
    app.state.identity_store.synchronize_resource_catalog({
        ("library", _LIBRARY), ("library", "gdal"),
    })
    _set_grant(client, app, csrf, allowed=True, revision=0)
    _set_grant(client, app, csrf, allowed=True, revision=0, resource_id="gdal")
    return app, manager, client, csrf


def test_default_app_resume_rechecks_grant_and_reuses_valid_checkpoint(runtime_harness):
    root, assets, embedder, settings, gates, runner_calls = runtime_harness
    gate = _Gate()
    gates.append(gate)
    app1, manager1, client1, _csrf1 = _open_first_app(root, assets, embedder, settings)
    submitted = client1.post(
        "/api/admin/evaluations", headers=_headers(_csrf1),
        json={"experiment_id": "gdal_dstalpha_diagnostic_v1"},
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    assert gate.case_saved.wait(8), "production runner did not persist its synthetic case"
    job_store = app1.state.evaluation_job_store
    checkpoint = root / "artifacts" / "evaluations" / job_id / "cases" / f"{_CASE}.json"
    assert checkpoint.is_file()
    original_checkpoint = checkpoint.read_bytes()

    _enter_shutdown(client1, gate)
    interrupted = job_store.get(job_id)
    assert interrupted.status == "interrupted"
    assert interrupted.completed_cases == interrupted.total_cases == 1
    assert manager1.close_calls == 1
    gates.clear()

    app2, manager2 = _build_app(root, assets["qdrant"], embedder, settings)
    with TestClient(app2, client=("127.0.0.1", 52343)) as client2:
        csrf2 = _bootstrap_login(client2, bootstrap=False)
        before = client2.get(f"/api/admin/evaluations/{job_id}")
        assert before.status_code == 200 and before.json()["status"] == "interrupted"
        _set_grant(client2, app2, csrf2, allowed=False, revision=1)
        denied = client2.post(
            f"/api/admin/evaluations/{job_id}/resume", headers=_headers(csrf2), json={},
        )
        assert denied.status_code == 403, denied.text
        assert app2.state.evaluation_job_store.get(job_id).status == "interrupted"

        _set_grant(client2, app2, csrf2, allowed=True, revision=2)
        embed_calls_before = embedder.query_calls
        rerank_calls_before = sum(item.score_calls for item in _SyntheticReranker.instances)
        resumed = client2.post(
            f"/api/admin/evaluations/{job_id}/resume", headers=_headers(csrf2), json={},
        )
        assert resumed.status_code == 202, resumed.text
        completed = _wait(client2, job_id)
        assert completed["status"] == "completed"
        assert completed["completed_cases"] == completed["total_cases"] == 1
        assert checkpoint.read_bytes() == original_checkpoint
        assert embedder.query_calls == embed_calls_before
        assert sum(item.score_calls for item in _SyntheticReranker.instances) == rerank_calls_before
        assert len(runner_calls) == 2  # initial run + resume validation/reuse
        case = client2.get(
            f"/api/admin/evaluations/{job_id}/result?case_id={_CASE}"
        )
        assert case.status_code == 200
        assert any("release_marker_A" in item["text"]
                   for item in case.json()["profiles"]["baseline"]["selected"])
        assert manager2.close_calls == 0


def test_default_app_resume_rejects_checkpoint_hash_tampering(runtime_harness):
    root, assets, embedder, settings, gates, runner_calls = runtime_harness
    gate = _Gate()
    gates.append(gate)
    app1, _manager1, client1, csrf1 = _open_first_app(root, assets, embedder, settings)
    submitted = client1.post(
        "/api/admin/evaluations", headers=_headers(csrf1),
        json={"experiment_id": "gdal_dstalpha_diagnostic_v1"},
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["job_id"]
    assert gate.case_saved.wait(8), "production runner did not persist its synthetic case"
    checkpoint = root / "artifacts" / "evaluations" / job_id / "cases" / f"{_CASE}.json"
    _enter_shutdown(client1, gate)
    assert app1.state.evaluation_job_store.get(job_id).status == "interrupted"
    record = json.loads(checkpoint.read_text(encoding="utf-8"))
    record["profiles"]["baseline"]["selected"].append({"text": "tampered"})
    checkpoint.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    gates.clear()

    app2, _manager2 = _build_app(root, assets["qdrant"], embedder, settings)
    with TestClient(app2, client=("127.0.0.1", 52344)) as client2:
        csrf2 = _bootstrap_login(client2, bootstrap=False)
        resume = client2.post(
            f"/api/admin/evaluations/{job_id}/resume", headers=_headers(csrf2), json={},
        )
        assert resume.status_code == 202, resume.text
        failed = _wait(client2, job_id)
        assert failed["status"] == "integrity_failure"
        assert failed["error_code"] == "EvaluationIntegrityError"
        assert len(runner_calls) == 1  # corrupted checkpoint rejected before runner call
