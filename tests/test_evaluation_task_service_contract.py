"""评测任务协调器合同：独立SQLite、合成runner、无生产模型/题集。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.evaluation_tasks.service import (
    EvaluationContext,
    EvaluationJobStore,
    EvaluationSpec,
    EvaluationTaskService,
)
from wenjin.identity.policy import Forbidden
from wenjin.retrieval.router import RouteConfig
from wenjin.retrieval.strategy_experiment import (
    checkpoint_record_hash,
    run_identity_sha256,
    run_retrieval_experiment,
)


pytestmark = pytest.mark.integration


class FakeIdentityStore:
    def list_users(self, actor):
        assert actor.user_id == "admin-user"
        return []

    def can_read(self, actor, resource_type, resource_id):
        return actor.role == "admin" and resource_type == "library" and resource_id == "synthetic-lib"


def _actor(role: str):
    return SimpleNamespace(
        user_id=f"{role}-user", role=role, must_change_password=False,
    )


def _bindings():
    rows = [
        {"case_id": "case-a", "query": "synthetic query A", "library": "synthetic-lib", "version": "1"},
        {"case_id": "case-b", "query": "synthetic query B", "library": "synthetic-lib", "version": "1"},
    ]
    from wenjin.retrieval.strategy_experiment import case_identity

    identities = {row["case_id"]: case_identity(row) for row in rows}
    run_identity = {
        "schema_version": 1,
        "profiles": ["baseline"],
        "case_identities": identities,
        "assets": {"synthetic-target": "sha256:synthetic-assets"},
    }
    run_identity["run_identity_sha256"] = run_identity_sha256(run_identity)
    input_snapshot = {
        "dataset_id": "synthetic-dataset",
        "dataset_case_count": len(rows),
        "case_ids": [row["case_id"] for row in rows],
        "resource_ids": ["synthetic-lib"],
        "profiles": ["baseline"],
    }
    return rows, input_snapshot, run_identity


def _record(case_id: str, identity: dict) -> dict:
    record = {
        "schema_version": 1,
        "case_id": case_id,
        "case_identity": identity["case_identities"][case_id],
        "run_identity_sha256": identity["run_identity_sha256"],
        "profiles": {"baseline": {
            "status": "complete", "selected": [], "ranked_candidates": [],
            "timing_seconds": {"total": 0.0},
        }},
    }
    record["record_sha256"] = checkpoint_record_hash(record)
    return record


def _summary(case_ids: list[str], identity: dict, *, complete: bool) -> dict:
    value = {
        "status": "completed" if complete else "incomplete",
        "valid_case_ids": case_ids,
        "assets_before": identity["assets"],
        "assets_after": identity["assets"],
        "assets_unchanged": True,
    }
    if complete:
        value["case_count"] = len(case_ids)
        value["profiles"] = {
            "baseline": {
                "cases": len(case_ids), "selected_evidence": 0,
                "candidate_count": 0, "mean_total_seconds": 0.0,
            },
        }
    return value


def _context_factory(rows, snapshot, identity, close_calls, *, alternate_identity=None):
    def factory(_job, _job_dir):
        # router是借用对象；close_owned只记录本context自有资源的释放。
        current_identity = alternate_identity or identity
        current_snapshot = snapshot
        if alternate_identity is not None:
            current_snapshot = {**snapshot, "binding_revision": 2}
        borrowed_router = SimpleNamespace(
            closed=False,
            verify_run_identity=lambda: current_identity["assets"],
        )
        return EvaluationContext(
            rows=rows,
            router=borrowed_router,
            config={"synthetic": True},
            rewriter=None,
            input_snapshot=current_snapshot,
            run_identity=current_identity,
            close_owned=lambda: close_calls.append("owned-closed"),
        )
    return factory


def _service(tmp_path, runner, *, factory=None):
    rows, snapshot, identity = _bindings()
    close_calls = []
    store = EvaluationJobStore(tmp_path / "evaluation.sqlite")
    spec = EvaluationSpec(
        experiment_id="synthetic_contract_v1", title="Synthetic contract",
        dataset_id="synthetic-dataset", profiles=("baseline",), max_cases=2,
    )

    def binding(_spec, _job_id, _actor):
        return snapshot, identity

    service = EvaluationTaskService(
        root=tmp_path,
        store=store,
        identity_store=FakeIdentityStore(),
        specs={spec.experiment_id: spec},
        context_factory=factory or _context_factory(rows, snapshot, identity, close_calls),
        runner=runner,
        binding_factory=binding,
    )
    return service, store, spec, rows, snapshot, identity, close_calls


async def _wait_task(service, job_id):
    task = service._running.get(job_id)
    if task is not None:
        await asyncio.wait_for(task, timeout=8)
    return await asyncio.to_thread(service.store.get, job_id)


def test_admin_runner_persists_checkpoints_summary_and_releases_only_owned_context(tmp_path):
    rows, snapshot, identity = _bindings()
    calls = []
    returned_summaries = []

    def runner(**kwargs):
        calls.append(kwargs)
        for row in kwargs["rows"]:
            kwargs["emit_case"](_record(row["case_id"], identity))
            kwargs["progress"](rows.index(row) + 1, len(rows), row["case_id"], "case_complete")
        summary = _summary([row["case_id"] for row in rows], identity, complete=True)
        summary["comparison"] = {"synthetic": True}
        return summary

    service, store, spec, _, _, _, close_calls = _service(tmp_path, runner)
    async def scenario():
        with pytest.raises(Forbidden):
            await service.submit(_actor("reader"), spec.experiment_id)
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        completed = await _wait_task(service, submitted.job_id)
        assert completed.status == "completed"
        assert completed.completed_cases == completed.total_cases == 2
        assert completed.result_summary["valid_case_ids"] == ["case-a", "case-b"]
        case_dir = service._job_dir(completed.job_id) / "cases"
        assert sorted(path.name for path in case_dir.glob("*.json")) == ["case-a.json", "case-b.json"]
        summary_path = await service.result_path(_actor("admin"), completed.job_id, "summary.json")
        assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "completed"
        assert len(calls) == 1
        assert close_calls == ["owned-closed"]
        await service.shutdown()

    asyncio.run(scenario())


def test_shutdown_keeps_case_boundary_checkpoint_and_resume_reuses_it(tmp_path):
    rows, _snapshot, identity = _bindings()
    first_started = threading.Event()
    run_number = 0
    close_calls = []

    def runner(**kwargs):
        nonlocal run_number
        run_number += 1
        case_dir: Path = kwargs["checkpoint_dir"]
        existing = {path.stem for path in case_dir.glob("*.json")} if case_dir.exists() else set()
        if run_number == 1:
            kwargs["emit_case"](_record("case-a", identity))
            kwargs["progress"](1, len(rows), "case-a", "case_complete")
            first_started.set()
            deadline = time.monotonic() + 5
            while not kwargs["should_cancel"]() and time.monotonic() < deadline:
                time.sleep(0.01)
            completed_ids = ["case-a"]
        else:
            assert existing == {"case-a"}
            assert (case_dir / "case-a.json").is_file()
            kwargs["emit_case"](_record("case-b", identity))
            kwargs["progress"](2, len(rows), "case-b", "case_complete")
            completed_ids = ["case-a", "case-b"]
        return _summary(completed_ids, identity, complete=len(completed_ids) == len(rows))

    service, store, spec, _, _, _, _ = _service(
        tmp_path, runner, factory=_context_factory(rows, _bindings()[1], identity, close_calls),
    )

    async def scenario():
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        assert await asyncio.to_thread(first_started.wait, 4)
        await service.shutdown()
        interrupted = await asyncio.to_thread(store.get, submitted.job_id)
        assert interrupted.status == "interrupted"
        assert interrupted.completed_cases == 1
        first_checkpoint = service._job_dir(submitted.job_id) / "cases" / "case-a.json"
        original = first_checkpoint.read_bytes()

        restarted_store = EvaluationJobStore(tmp_path / "evaluation.sqlite")
        restarted = EvaluationTaskService(
            root=tmp_path, store=restarted_store, identity_store=FakeIdentityStore(),
            specs={spec.experiment_id: spec},
            context_factory=_context_factory(rows, _bindings()[1], identity, close_calls),
            runner=runner,
            binding_factory=lambda *_args: (_bindings()[1], identity),
        )
        diagnostics = []
        validate_case = restarted._validate_case_record
        validate_files = restarted._validated_checkpoints

        def capture_case(*args, **kwargs):
            try:
                return validate_case(*args, **kwargs)
            except Exception as error:
                diagnostics.append(("case", str(error)))
                raise

        def capture_files(*args, **kwargs):
            try:
                result = validate_files(*args, **kwargs)
                diagnostics.append(("files", sorted(result)))
                return result
            except Exception as error:
                diagnostics.append(("files_error", str(error)))
                raise

        restarted._validate_case_record = capture_case
        restarted._validated_checkpoints = capture_files
        queued = await restarted.resume(_actor("admin"), submitted.job_id)
        assert restarted._actors[submitted.job_id].user_id == "admin-user"
        completed = await _wait_task(restarted, submitted.job_id)
        assert queued.status == "queued"
        assert completed.status == "completed", (
            completed.error_code, run_number, close_calls, diagnostics,
        )
        assert completed.completed_cases == 2
        assert first_checkpoint.read_bytes() == original
        assert (first_checkpoint.parent / "case-b.json").is_file()
        assert run_number == 2
        assert close_calls == ["owned-closed", "owned-closed"]
        await restarted.shutdown()

    asyncio.run(scenario())


def test_user_cancel_is_persisted_at_case_boundary(tmp_path):
    rows, snapshot, identity = _bindings()
    started = threading.Event()
    close_calls = []

    def runner(**kwargs):
        kwargs["emit_case"](_record("case-a", identity))
        kwargs["progress"](1, len(rows), "case-a", "case_complete")
        started.set()
        deadline = time.monotonic() + 5
        while not kwargs["should_cancel"]() and time.monotonic() < deadline:
            time.sleep(0.01)
        return _summary(["case-a"], identity, complete=False)

    service, store, spec, *_rest = _service(
        tmp_path, runner, factory=_context_factory(rows, snapshot, identity, close_calls),
    )

    async def scenario():
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        assert await asyncio.to_thread(started.wait, 4)
        requested = await service.cancel(_actor("admin"), submitted.job_id)
        assert requested.status == "cancelling"
        assert requested.cancel_requested is True
        cancelled = await _wait_task(service, submitted.job_id)
        assert cancelled.status == "cancelled"
        assert cancelled.completed_cases == 1
        assert cancelled.cancel_requested is True
        checkpoint = service._job_dir(submitted.job_id) / "cases" / "case-a.json"
        assert checkpoint.is_file()
        assert not (service._job_dir(submitted.job_id) / "summary.json").exists()
        assert close_calls == ["owned-closed"]
        await service.shutdown()

    asyncio.run(scenario())


def test_resume_rejects_changed_run_fingerprint(tmp_path):
    rows, snapshot, identity = _bindings()
    started = threading.Event()
    calls = 0
    close_calls = []

    def runner(**kwargs):
        nonlocal calls
        calls += 1
        kwargs["emit_case"](_record("case-a", identity))
        started.set()
        deadline = time.monotonic() + 5
        while not kwargs["should_cancel"]() and time.monotonic() < deadline:
            time.sleep(0.01)
        return _summary(["case-a"], identity, complete=False)

    service, store, spec, *_rest = _service(
        tmp_path, runner, factory=_context_factory(rows, snapshot, identity, close_calls),
    )

    async def scenario():
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        assert await asyncio.to_thread(started.wait, 4)
        await service.shutdown()
        changed_identity = {**identity, "assets": {"synthetic-target": "sha256:changed"}}
        changed_identity["run_identity_sha256"] = run_identity_sha256(changed_identity)
        changed_factory = _context_factory(
            rows, snapshot, identity, close_calls, alternate_identity=changed_identity,
        )
        restarted_store = EvaluationJobStore(tmp_path / "evaluation.sqlite")
        restarted = EvaluationTaskService(
            root=tmp_path, store=restarted_store, identity_store=FakeIdentityStore(),
            specs={spec.experiment_id: spec}, context_factory=changed_factory,
            runner=runner, binding_factory=lambda *_args: (snapshot, identity),
        )
        await restarted.resume(_actor("admin"), submitted.job_id)
        failed = await _wait_task(restarted, submitted.job_id)
        assert failed.status == "integrity_failure"
        assert failed.error_code == "EvaluationIntegrityError"
        assert calls == 1
        assert close_calls == ["owned-closed", "owned-closed"]
        await restarted.shutdown()

    asyncio.run(scenario())


def test_resume_rejects_tampered_case_checkpoint(tmp_path):
    rows, snapshot, identity = _bindings()
    started = threading.Event()
    calls = 0
    close_calls = []

    def runner(**kwargs):
        nonlocal calls
        calls += 1
        kwargs["emit_case"](_record("case-a", identity))
        started.set()
        deadline = time.monotonic() + 5
        while not kwargs["should_cancel"]() and time.monotonic() < deadline:
            time.sleep(0.01)
        return _summary(["case-a"], identity, complete=False)

    service, store, spec, *_rest = _service(
        tmp_path, runner, factory=_context_factory(rows, snapshot, identity, close_calls),
    )

    async def scenario():
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        assert await asyncio.to_thread(started.wait, 4)
        await service.shutdown()
        checkpoint = service._job_dir(submitted.job_id) / "cases" / "case-a.json"
        record = __import__("json").loads(checkpoint.read_text(encoding="utf-8"))
        record["profiles"]["baseline"]["selected"] = ["tampered"]
        checkpoint.write_text(__import__("json").dumps(record), encoding="utf-8")

        restarted_store = EvaluationJobStore(tmp_path / "evaluation.sqlite")
        restarted = EvaluationTaskService(
            root=tmp_path, store=restarted_store, identity_store=FakeIdentityStore(),
            specs={spec.experiment_id: spec},
            context_factory=_context_factory(rows, snapshot, identity, close_calls),
            runner=runner, binding_factory=lambda *_args: (snapshot, identity),
        )
        await restarted.resume(_actor("admin"), submitted.job_id)
        failed = await _wait_task(restarted, submitted.job_id)
        assert failed.status == "integrity_failure"
        assert failed.error_code == "EvaluationIntegrityError"
        assert calls == 1
        await restarted.shutdown()

    asyncio.run(scenario())


def test_terminal_cleanup_releases_task_actor_even_when_owned_close_raises(tmp_path):
    rows, snapshot, identity = _bindings()
    close_calls = []

    def runner(**kwargs):
        for row in kwargs["rows"]:
            kwargs["emit_case"](_record(row["case_id"], identity))
            kwargs["progress"](kwargs["rows"].index(row) + 1, len(rows), row["case_id"], "case_complete")
        return _summary([row["case_id"] for row in rows], identity, complete=True)

    def close_owned():
        close_calls.append("attempted")
        raise RuntimeError("synthetic close failure")

    def context_factory(*_args):
        return EvaluationContext(
            rows=rows,
            router=SimpleNamespace(verify_run_identity=lambda: identity["assets"]),
            config={"synthetic": True}, rewriter=None, input_snapshot=snapshot,
            run_identity=identity,
            close_owned=close_owned,
        )

    service, store, spec, *_rest = _service(tmp_path, runner, factory=context_factory)

    async def scenario():
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        completed = await _wait_task(service, submitted.job_id)
        assert completed.status == "completed"
        assert close_calls == ["attempted"]
        assert submitted.job_id not in service._running
        assert submitted.job_id not in service._cancel
        assert submitted.job_id not in service._cancel_reason
        assert submitted.job_id not in service._actors
        await service.shutdown()

    asyncio.run(scenario())


def test_service_runs_production_retrieval_runner_on_one_synthetic_case(tmp_path):
    rows, snapshot, identity = _bindings()
    rows = rows[:1]
    identity = {**identity, "case_identities": {"case-a": identity["case_identities"]["case-a"]}}
    identity.pop("run_identity_sha256")
    identity["run_identity_sha256"] = run_identity_sha256(identity)
    snapshot = {
        **snapshot,
        "dataset_case_count": 1,
        "case_ids": ["case-a"],
        "profiles": ["baseline"],
    }
    candidate = {
        "chunk_id": "synthetic-target:raw_data/synthetic-lib/1/guide.txt:1",
        "source_path": "raw_data/synthetic-lib/1/guide.txt",
        "library": "synthetic-lib",
        "version": "1",
        "target_id": "synthetic-target",
        "start_line": 1,
        "end_line": 1,
        "text": "Synthetic runner evidence: service_checkpoint_marker.",
        "score": 1.0,
    }

    class SyntheticRouter:
        reranker = None
        indices = []

        def retrieve(self, _query, **_kwargs):
            batch = [dict(candidate)]
            return {
                "dense_candidates": [dict(item) for item in batch],
                "bm25_candidates": [dict(item) for item in batch],
                "fused_candidates": [dict(item) for item in batch],
                "ranked_candidates": [dict(item) for item in batch],
                "source_context_diagnostics": {},
                "reranker_used": False,
                "timing_seconds": {},
            }

        def verify_run_identity(self):
            return identity["assets"]

    router = SyntheticRouter()
    close_calls = []
    store = EvaluationJobStore(tmp_path / "evaluation.sqlite")
    spec = EvaluationSpec(
        experiment_id="synthetic_runner_v1", title="Synthetic production runner",
        dataset_id="synthetic-dataset", profiles=("baseline",), max_cases=1,
    )
    context = EvaluationContext(
        rows=rows, router=router,
        config=RouteConfig(candidate_k=2, top_k=1, rerank_pool=2, context_char_budget=500),
        rewriter=None, input_snapshot=snapshot, run_identity=identity,
        close_owned=lambda: close_calls.append("owned-closed"),
    )
    service = EvaluationTaskService(
        root=tmp_path, store=store, identity_store=FakeIdentityStore(),
        specs={spec.experiment_id: spec}, context_factory=lambda *_args: context,
        runner=run_retrieval_experiment,
        binding_factory=lambda *_args: (snapshot, identity),
    )

    async def scenario():
        submitted = await service.submit(_actor("admin"), spec.experiment_id)
        completed = await _wait_task(service, submitted.job_id)
        assert completed.status == "completed"
        assert completed.completed_cases == completed.total_cases == 1
        case_path = service._job_dir(submitted.job_id) / "cases" / "case-a.json"
        record = json.loads(case_path.read_text(encoding="utf-8"))
        selected = record["profiles"]["baseline"]["selected"]
        assert len(selected) == 1
        assert "service_checkpoint_marker" in selected[0]["text"]
        assert selected[0]["text_sha256"] == hashlib.sha256(
            selected[0]["text"].encode("utf-8")
        ).hexdigest()
        assert record["record_sha256"] == checkpoint_record_hash(record)
        summary_path = service._job_dir(submitted.job_id) / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["status"] == "completed"
        assert summary["case_count"] == 1
        assert summary["profiles"]["baseline"]["cases"] == 1
        assert close_calls == ["owned-closed"]
        await service.shutdown()

    asyncio.run(scenario())
