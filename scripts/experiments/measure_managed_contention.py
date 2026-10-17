"""Measure managed query/build contention using only temporary synthetic assets.

This runner uses an in-memory Qdrant client, the real FileManagementService,
its importer/index adapter, an instrumented RLock, and the real ModelAdmission
FIFO with a deterministic delayed embedder/reranker. It never reads project
corpora/configs, starts a network service, or loads local model assets.
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import hashlib
import inspect
import json
import math
import statistics
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from wenjin.file_management import FileManagementService
from wenjin.file_management import backends as file_backends
from wenjin.file_management import jobs as file_jobs
from wenjin.file_management import service as file_service
from wenjin.file_management import versions as file_versions
from wenjin.indexing import build as index_build
from wenjin.indexing import dense as index_dense
from wenjin.model_api.admission import ModelAdmission
from wenjin.retrieval.search_policy import SearchPolicy


class InstrumentedRLock:
    """RLock-compatible wrapper that records actual wait per operation."""

    def __init__(self, name: str, events: list[dict], events_lock: threading.Lock):
        self.name = name
        self._lock = threading.RLock()
        self._events = events
        self._events_lock = events_lock

    def acquire(self, *args, **kwargs):
        started = time.perf_counter()
        acquired = self._lock.acquire(*args, **kwargs)
        waited = time.perf_counter() - started
        if acquired:
            with self._events_lock:
                self._events.append({
                    "kind": "lock_wait",
                    "lock": self.name,
                    "operation_id": getattr(_operation_local, "operation_id", None),
                    "wait_seconds": waited,
                    "timestamp": time.perf_counter(),
                    "thread_id": threading.get_ident(),
                })
        return acquired

    def release(self):
        return self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


_operation_local = threading.local()


class DelayedModel:
    """Embedding/reranking stub whose sync API is backed by ModelAdmission."""

    def __init__(self, *, loop, admission, delay_seconds: float):
        self.loop = loop
        self.admission = admission
        self.delay_seconds = delay_seconds
        self.events: list[dict] = []
        self._events_lock = threading.Lock()
        self.build_inference_started = threading.Event()
        self._serial = 0

    def set_delay(self, value: float) -> None:
        self.delay_seconds = value

    def _record(self, row: dict) -> None:
        with self._events_lock:
            self.events.append(row)

    def _sync_call(self, kind: str, inputs: Any) -> Any:
        operation_id = getattr(_operation_local, "operation_id", None)
        with self._events_lock:
            self._serial += 1
            call_id = f"model-{self._serial:05d}"
        submitted = time.perf_counter()
        self._record({
            "kind": "model_submit",
            "call_id": call_id,
            "operation_id": operation_id,
            "model_operation": kind,
            "submitted": submitted,
            "thread_id": threading.get_ident(),
        })
        future = asyncio.run_coroutine_threadsafe(
            self.admission.submit(self._perform, call_id, operation_id, kind, inputs),
            self.loop,
        )
        return future.result()

    def _perform(self, call_id: str, operation_id: str | None, kind: str, inputs: Any):
        started = time.perf_counter()
        self._record({
            "kind": "model_start",
            "call_id": call_id,
            "operation_id": operation_id,
            "model_operation": kind,
            "started": started,
            "thread_id": threading.get_ident(),
        })
        if kind == "document_embedding":
            self.build_inference_started.set()
        time.sleep(self.delay_seconds)
        if kind == "document_embedding":
            result = [self._vector(text) for text in inputs]
        elif kind == "query_embedding":
            result = self._vector(inputs)
        else:
            query, documents = inputs
            query_terms = set(query.casefold().split())
            result = [float(len(query_terms & set(text.casefold().split()))) for text in documents]
        ended = time.perf_counter()
        self._record({
            "kind": "model_end",
            "call_id": call_id,
            "operation_id": operation_id,
            "model_operation": kind,
            "ended": ended,
            "inference_seconds": ended - started,
            "thread_id": threading.get_ident(),
        })
        return result

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.casefold().encode("utf-8")).digest()
        values = [1.0 if digest[i % len(digest)] & (1 << (i % 8)) else -1.0 for i in range(16)]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def get_text_embedding_batch(self, texts, show_progress=False):
        del show_progress
        return self._sync_call("document_embedding", list(texts))

    def get_query_embedding(self, query):
        return self._sync_call("query_embedding", str(query))

    def score(self, query, documents):
        return self._sync_call("reranker", (query, list(documents)))


class DelayedReranker:
    def __init__(self, model: DelayedModel):
        self.model = model

    def score(self, query: str, documents: list[str]):
        return self.model.score(query, documents)


def _set_operation(operation_id: str):
    previous = getattr(_operation_local, "operation_id", None)
    _operation_local.operation_id = operation_id
    return previous


def _restore_operation(previous):
    _operation_local.operation_id = previous


def _summarize(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "p50_seconds": None, "p95_seconds": None, "max_seconds": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "count": len(values),
        "p50_seconds": round(statistics.median(values), 6),
        "p95_seconds": round(ordered[p95_index], 6),
        "max_seconds": round(max(values), 6),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_fixture(path: Path, revision: int) -> None:
    paragraphs = [
        f"## Section {idx}\nSynthetic managed retrieval fixture revision {revision}. "
        f"The amberstone signal explains bounded inference scheduling and evidence identity. "
        f"Fixed marker benchslice{idx:02d} supports deterministic query checks.\n"
        for idx in range(1, 13)
    ]
    path.write_text("\n".join(paragraphs), encoding="utf-8")


async def run(args) -> dict:
    loop = asyncio.get_running_loop()
    admission = ModelAdmission(
        max_waiting=args.admission_max_waiting,
        max_wait_seconds=args.admission_max_wait_seconds,
    )
    qdrant = QdrantClient(":memory:")
    with tempfile.TemporaryDirectory(prefix="wenjin-managed-contention-") as temp_root:
        root = Path(temp_root).resolve()
        (root / "raw_data").mkdir(parents=True, exist_ok=True)
        model = DelayedModel(loop=loop, admission=admission, delay_seconds=0.0)
        manager = FileManagementService(
            root=root,
            qdrant_client=qdrant,
            embedder=model,
            embedding_model="synthetic-16d-v1",
            managed_raw_data_root=root / "raw_data",
        )
        event_lock = threading.Lock()
        lock_events: list[dict] = []
        model_lock = InstrumentedRLock("model_lock", lock_events, event_lock)
        rw_lock = InstrumentedRLock("manager_rw_lock", lock_events, event_lock)
        manager._model_lock = model_lock
        manager.versions._model_lock = model_lock
        # 新实现把模型锁缩到embedding batch，因此instrument实际adapter所持的锁。
        if hasattr(manager.index_adapter, "embedding_lock"):
            manager.index_adapter.embedding_lock = model_lock
        manager._rw_lock = rw_lock
        manager.jobs._rw_lock = rw_lock
        policy = SearchPolicy(
            candidate_k=10,
            rerank_pool=30,
            reranker_enabled=True,
            reranker_model_id="synthetic-delayed-reranker",
        )
        reranker = DelayedReranker(model)
        source = root / "initial.md"
        _build_fixture(source, 0)
        initial_job = manager.submit_import(
            source,
            library_id="synthetic-managed-contention",
            library_name="Synthetic managed contention",
            original_filename="initial.md",
        )
        # run_job invokes the synchronous provider shim, which submits back to this
        # asyncio loop. Keep it on a worker thread so ModelAdmission can make progress.
        await asyncio.to_thread(manager.run_job, initial_job.job_id)
        print("fixture_initial_build_complete", flush=True)
        active = manager.snapshot(initial_job.file_id)
        fixture_sha = manager._sha256(active.source_path)
        file_id = active.file_id
        output_dir = Path(args.output_dir).resolve() if args.output_dir else root / "run"
        # Default output stays under the destination artifact directory after temp assets clean up.
        if args.output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
        model.events.clear()
        lock_events.clear()
        model.set_delay(args.model_delay)
        executor = ThreadPoolExecutor(max_workers=args.max_concurrency)
        op_bounds: dict[str, dict[str, float]] = {}
        shared_query = "amberstone evidence identity scheduling"

        def query_once(operation_id: str, expected_version: str):
            previous = _set_operation(operation_id)
            started = time.perf_counter()
            op_bounds[operation_id] = {"start": started}
            try:
                result = manager.search(
                    file_id,
                    shared_query,
                    version_id=expected_version,
                    expected_library_id="synthetic-managed-contention",
                    top_k=3,
                    context_char_budget=8000,
                    reranker=reranker,
                    search_policy=policy,
                )
                ended = time.perf_counter()
                op_bounds[operation_id]["end"] = ended
                return {
                    "operation_id": operation_id,
                    "kind": "query",
                    "status": "ok" if result.get("evidence") else "empty_evidence",
                    "elapsed_seconds": round(ended - started, 6),
                    "managed_version_id": result.get("managed_file", {}).get("version_id"),
                    "evidence_count": len(result.get("evidence", [])),
                    "retrieval_timing_seconds": result.get("retrieval", {}).get("timing_seconds"),
                    "model_queue_wait_seconds": None,
                    "model_inference_seconds": None,
                    "job_queue_wait_seconds": None,
                    "queue_measurement_note": "model queue and inference are filled from ModelAdmission timestamps; application job queue is outside direct manager.run_job harness",
                }
            except Exception as exc:  # report stable exception class only
                ended = time.perf_counter()
                op_bounds[operation_id]["end"] = ended
                return {
                    "operation_id": operation_id,
                    "kind": "query",
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "elapsed_seconds": round(ended - started, 6),
                    "managed_version_id": None,
                    "evidence_count": 0,
                    "retrieval_timing_seconds": None,
                    "model_queue_wait_seconds": None,
                    "model_inference_seconds": None,
                    "job_queue_wait_seconds": None,
                }
            finally:
                _restore_operation(previous)

        def build_once(operation_id: str, revision: int):
            previous = _set_operation(operation_id)
            started = time.perf_counter()
            op_bounds[operation_id] = {"start": started}
            try:
                replacement = root / f"replacement-{revision}.md"
                _build_fixture(replacement, revision)
                job = manager.submit_import(
                    replacement,
                    replace_file_id=file_id,
                    library_id="synthetic-managed-contention",
                    library_name="Synthetic managed contention",
                    original_filename=replacement.name,
                )
                queued = time.perf_counter()
                manager.run_job(job.job_id)
                ended = time.perf_counter()
                op_bounds[operation_id].update({"queued": queued, "end": ended})
                current = manager.snapshot(file_id)
                return {
                    "operation_id": operation_id,
                    "kind": "build",
                    "status": "ok" if current.version_id != active.version_id else "version_unchanged",
                    "elapsed_seconds": round(ended - started, 6),
                    "job_queue_wait_seconds": None,
                    "model_queue_wait_seconds": None,
                    "model_inference_seconds": None,
                    "old_active_version_id": active.version_id,
                    "new_active_version_id": current.version_id,
                    "source_sha256": current.source_raw_sha256,
                    "dispatch_delay_seconds": round(queued - started, 6),
                    "queue_measurement_note": "direct manager.run_job call; application scheduler queue is not instantiated",
                }
            except Exception as exc:
                ended = time.perf_counter()
                op_bounds[operation_id]["end"] = ended
                return {
                    "operation_id": operation_id,
                    "kind": "build",
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "elapsed_seconds": round(ended - started, 6),
                    "job_queue_wait_seconds": None,
                    "model_queue_wait_seconds": None,
                    "model_inference_seconds": None,
                }
            finally:
                _restore_operation(previous)

        def run_queries(label: str, concurrency: int, version_id: str):
            futures = [
                executor.submit(query_once, f"{label}-q{idx:02d}", version_id)
                for idx in range(concurrency)
            ]
            return [future.result() for future in futures]

        scenarios: list[dict] = []
        try:
            # Single-query and concurrent-query slices use the same fixed active version.
            for concurrency in sorted(set([1, min(2, args.max_concurrency), args.max_concurrency])):
                for round_index in range(1, args.rounds + 1):
                    label = f"query-c{concurrency}-r{round_index}"
                    started = time.perf_counter()
                    results = await asyncio.to_thread(
                        run_queries, label, concurrency, active.version_id
                    )
                    ended = time.perf_counter()
                    print(f"scenario_complete:{label}", flush=True)
                    scenarios.append({
                        "name": label,
                        "kind": "queries",
                        "concurrency": concurrency,
                        "round": round_index,
                        "elapsed_seconds": round(ended - started, 6),
                        "results": results,
                    })

            # A build-only slice includes the actual importer and temporary Qdrant index build.
            for round_index in range(1, args.rounds + 1):
                build_id = f"build-only-r{round_index:02d}"
                started = time.perf_counter()
                build_result = await asyncio.wrap_future(
                    executor.submit(build_once, build_id, round_index)
                )
                ended = time.perf_counter()
                print(f"scenario_complete:{build_id}", flush=True)
                scenarios.append({
                    "name": build_id,
                    "kind": "build",
                    "round": round_index,
                    "concurrency": 1,
                    "elapsed_seconds": round(ended - started, 6),
                    "results": [build_result],
                })

            # Repeat the old-version barrier with a new candidate each round. The active
            # version is captured before each build, then checked on every query result.
            for round_index in range(1, args.rounds + 1):
                old_version_for_mixed = manager.snapshot(file_id).version_id
                model.build_inference_started.clear()
                model.set_delay(args.model_delay)
                mixed_build_id = f"mixed-build-r{round_index:02d}"
                mixed_started = time.perf_counter()
                mixed_build = executor.submit(build_once, mixed_build_id, args.rounds + round_index)
                barrier_ready = await asyncio.to_thread(
                    model.build_inference_started.wait, args.barrier_timeout
                )
                print(f"mixed_build_barrier:{barrier_ready}", flush=True)
                if barrier_ready:
                    barrier_query_start = time.perf_counter()
                    query_results = await asyncio.to_thread(
                        run_queries,
                        f"mixed-r{round_index}",
                        min(args.mixed_queries, args.max_concurrency),
                        old_version_for_mixed,
                    )
                    barrier_query_end = time.perf_counter()
                else:
                    barrier_query_start = barrier_query_end = None
                    query_results = []
                mixed_build_result = await asyncio.wrap_future(mixed_build)
                print(f"scenario_complete:{mixed_build_id}", flush=True)
                mixed_ended = time.perf_counter()
                scenarios.append({
                    "name": mixed_build_id,
                    "kind": "mixed",
                    "round": round_index,
                    "concurrency": 1 + len(query_results),
                    "elapsed_seconds": round(mixed_ended - mixed_started, 6),
                    "build_inference_barrier_observed": barrier_ready,
                    "query_barrier_elapsed_seconds": (
                        round(barrier_query_end - barrier_query_start, 6)
                        if barrier_query_start is not None else None
                    ),
                    "old_version_id_before_build": old_version_for_mixed,
                    "results": [mixed_build_result, *query_results],
                })
        finally:
            executor.shutdown(wait=True, cancel_futures=False)
            manager.close()
            qdrant.close()
            await admission.close()

        all_ops = {**op_bounds}
        model_submits = {row["call_id"]: row for row in model.events if row["kind"] == "model_submit"}
        model_starts = {row["call_id"]: row for row in model.events if row["kind"] == "model_start"}
        model_ends = {row["call_id"]: row for row in model.events if row["kind"] == "model_end"}
        model_by_op: dict[str, dict[str, float]] = {}
        for call_id, submitted in model_submits.items():
            started = model_starts.get(call_id)
            ended = model_ends.get(call_id)
            operation_id = submitted.get("operation_id")
            if not operation_id or not started or not ended:
                continue
            values = model_by_op.setdefault(operation_id, {"queue_wait": 0.0, "inference": 0.0})
            values["queue_wait"] += max(0.0, started["started"] - submitted["submitted"])
            values["inference"] += ended["inference_seconds"]
        lock_by_op: dict[str, dict[str, float]] = {}
        for event in lock_events:
            operation_id = event.get("operation_id")
            if not operation_id:
                continue
            values = lock_by_op.setdefault(operation_id, {})
            key = event["lock"]
            values[key] = values.get(key, 0.0) + event["wait_seconds"]
        for scenario in scenarios:
            for row in scenario["results"]:
                operation_id = row.get("operation_id")
                if not operation_id:
                    continue
                model_times = model_by_op.get(operation_id, {})
                row["model_queue_wait_seconds"] = round(model_times.get("queue_wait", 0.0), 6)
                row["model_inference_seconds"] = round(model_times.get("inference", 0.0), 6)
                row["lock_wait_seconds"] = {
                    key: round(value, 6) for key, value in lock_by_op.get(operation_id, {}).items()
                }
                bounds = all_ops.get(operation_id, {})
                row["timeline_seconds"] = {
                    key: round(value, 6) for key, value in bounds.items()
                }
        timeline = [
            {
                "operation_id": operation_id,
                "start_seconds": round(bounds["start"], 6),
                "end_seconds": round(bounds["end"], 6),
            }
            for operation_id, bounds in all_ops.items()
            if "start" in bounds and "end" in bounds
        ]
        model_calls = []
        for call_id, submitted in model_submits.items():
            started = model_starts.get(call_id)
            ended = model_ends.get(call_id)
            model_calls.append({
                "call_id": call_id,
                "operation_id": submitted.get("operation_id"),
                "model_operation": submitted.get("model_operation"),
                "queue_wait_seconds": (
                    round(max(0.0, started["started"] - submitted["submitted"]), 6)
                    if started else None
                ),
                "inference_seconds": round(ended["inference_seconds"], 6) if ended else None,
            })
        report = {
            "schema_version": 1,
            "run_id": uuid.uuid4().hex,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "environment": "temporary-directory + in-memory Qdrant + deterministic delayed model + real ModelAdmission",
            "fixture": {
                "library_id": "synthetic-managed-contention",
                "active_file_id": file_id,
                "initial_version_id": active.version_id,
                "initial_source_sha256": fixture_sha,
                "fixture_text_only": True,
            },
            "source_hashes": {
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "implementation": {
                    module.__name__: {
                        "path": str(Path(inspect.getsourcefile(module)).resolve()),
                        "sha256": hashlib.sha256(
                            Path(inspect.getsourcefile(module)).read_bytes()
                        ).hexdigest(),
                    }
                    for module in (
                        file_backends,
                        file_jobs,
                        file_service,
                        file_versions,
                        index_build,
                        index_dense,
                    )
                },
            },
            "configuration": {
                "model_delay_seconds_per_operation": args.model_delay,
                "max_concurrency": args.max_concurrency,
                "mixed_queries": min(args.mixed_queries, args.max_concurrency),
                "rounds": args.rounds,
                "candidate_k": policy.candidate_k,
                "rerank_pool": policy.rerank_pool,
                "top_k": 3,
                "model_admission": {
                    "max_waiting": args.admission_max_waiting,
                    "max_wait_seconds": args.admission_max_wait_seconds,
                    "matches_production_defaults": (
                        args.admission_max_waiting == 8
                        and args.admission_max_wait_seconds == 30
                    ),
                },
            },
            "stage_measurements": {
                "job_queue_wait_seconds": None,
                "job_queue_wait_reason": "This direct manager.run_job harness does not start the HTTP background scheduler.",
                "request_model_queue_wait": "Measured from provider submit timestamp to ModelAdmission operation start.",
                "inference": "Measured inside the deterministic provider operation.",
                "retrieval": "Copied from RetrievalService timing_seconds for each query.",
                "locks": "Instrumented manager_rw_lock and model_lock acquire waits per operation.",
                "overlap_rule": "Use per-operation timeline intervals; stage totals overlap and must not be added to request wall time.",
            },
            "scenarios": scenarios,
            "operation_timeline": timeline,
            "model_calls": model_calls,
            "lock_events": lock_events,
        }
        if args.output_dir:
            _write_json(output_dir / "managed-contention.json", report)
        return report


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-delay", type=float, default=0.04)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--admission-max-waiting", type=int, default=8)
    parser.add_argument("--admission-max-wait-seconds", type=float, default=30.0)
    parser.add_argument("--mixed-queries", type=int, default=2)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--barrier-timeout", type=float, default=10.0)
    return parser.parse_args()


async def _main():
    args = parse_args()
    if args.model_delay < 0 or args.model_delay > 10:
        raise SystemExit("--model-delay must be between 0 and 10 seconds")
    if args.max_concurrency < 1 or args.max_concurrency > 32:
        raise SystemExit("--max-concurrency must be between 1 and 32")
    if args.rounds < 1 or args.rounds > 10:
        raise SystemExit("--rounds must be between 1 and 10")
    report = await run(args)
    print(json.dumps({
        "run_id": report["run_id"],
        "output": str((args.output_dir / "managed-contention.json").resolve()),
        "scenarios": [
            {"name": row["name"], "elapsed_seconds": row["elapsed_seconds"]}
            for row in report["scenarios"]
        ],
        "barrier_observed": next(
            (row.get("build_inference_barrier_observed") for row in report["scenarios"] if row["kind"] == "mixed"),
            None,
        ),
    }, ensure_ascii=False))


if __name__ == "__main__":
    faulthandler.dump_traceback_later(20, repeat=True)
    asyncio.run(_main())

