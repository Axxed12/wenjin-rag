"""Direct HTTP workload and event analysis for the isolated model-node experiment.

This module does not start Docker. The parent runner owns frozen source/config,
resource gates, service startup, and cleanup; this code only sends fixed model
API requests and analyzes the server's request-correlated event stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import statistics
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

MODEL_API_PROTOCOL = "wenjin-model-api/1"
CLIENT_CONCURRENCY = 4
REQUESTS_PER_ROUND = 8
DEFAULT_ROUNDS = 3
EMBEDDING_MAX_ABS_TOLERANCE = 1e-4
RERANK_MAX_ABS_TOLERANCE = 1e-3
HTTP_TIMEOUT_SECONDS = 240.0


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workload_sha256() -> str:
    workload = _workload()
    return _sha256_text(
        json.dumps(
            {
                "groups": {
                    name: group for name, group in workload.items()
                },
                "request_schedule": {
                    "name": "pure-phases-then-mixed-2plus2-v2",
                    "phase_order": ["pure_embedding", "pure_rerank", "mixed"],
                    "requests_per_group_per_round": REQUESTS_PER_ROUND,
                    "rounds": DEFAULT_ROUNDS,
                    "concurrency": CLIENT_CONCURRENCY,
                    "mixed_waves_per_round": 8,
                    "mixed_wave_embedding_requests": 2,
                    "mixed_wave_rerank_requests": 2,
                    "mixed_assignment": (
                        "embedding groups in workload order take four jobs each; rerank groups "
                        "in workload order take eight jobs each; pair two contiguous jobs from "
                        "each operation queue per wave"
                    ),
                },
            },
            sort_keys=True,
        )
    )


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _persist_raw_stage_output(target: Path, requests: list[dict[str, Any]]) -> None:
    _write_text_atomic(
        target / "client-requests.jsonl",
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in requests
        ),
    )
    _write_text_atomic(
        target / "response-vectors-and-scores.json",
        json.dumps(
            {row["request_id"]: row.get("values") for row in requests if "values" in row}, indent=2
        ),
    )


def _persist_preflight(target: Path, report: dict[str, Any]) -> None:
    _write_text_atomic(target / "preflight.json", json.dumps(report, indent=2))


def _persist_warmup(target: Path, rows: list[dict[str, Any]], report: dict[str, Any]) -> None:
    _write_text_atomic(
        target / "warmup-requests.jsonl",
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    )
    _write_text_atomic(
        target / "warmup-vectors-and-scores.json",
        json.dumps({row["request_id"]: row.get("values") for row in rows if "values" in row}, indent=2),
    )
    _write_text_atomic(target / "warmup-summary.json", json.dumps(report, indent=2))


def _repeat(seed: str, size: int) -> str:
    """Build deterministic synthetic text without embedding real project data."""
    unit = f"{seed} API contract, queue admission, model execution, retrieval evidence. "
    return (unit * (size // len(unit) + 1))[:size]


def _workload() -> dict[str, dict[str, Any]]:
    short_query = [
        "How does the admission queue handle cancellation?",
        "Where is the model API health route defined?",
        "Which identity fields bind a checkpoint?",
        "How are reranker candidates scored?",
    ]
    long_query = [_repeat(f"long-query-{index}", 1800) for index in range(4)]
    short_document = [
        "The service authenticates the bearer token before parsing model requests.",
        "The admission queue bounds waiting jobs independently from active lanes.",
        "Reranker scores preserve the input candidate order.",
        "The model identity includes the local checkpoint asset fingerprint.",
    ]
    long_document = [_repeat(f"long-document-{index}", 2400) for index in range(4)]

    def rerank_documents(count: int) -> list[str]:
        lengths = [96, 320, 720, 1500, 180, 560, 1080, 2200]
        return [_repeat(f"candidate-{index}", lengths[index]) for index in range(count)]

    return {
        "embedding_query_short": {
            "kind": "embedding",
            "input_kind": "query",
            "inputs": short_query,
        },
        "embedding_query_long": {"kind": "embedding", "input_kind": "query", "inputs": long_query},
        "embedding_document_short": {
            "kind": "embedding",
            "input_kind": "document",
            "inputs": short_document,
        },
        "embedding_document_long": {
            "kind": "embedding",
            "input_kind": "document",
            "inputs": long_document,
        },
        "rerank_4_mixed": {
            "kind": "rerank",
            "query": "Find evidence for queue lifecycle and model identity.",
            "documents": rerank_documents(4),
        },
        "rerank_8_mixed": {
            "kind": "rerank",
            "query": "Find evidence for queue lifecycle and model identity.",
            "documents": rerank_documents(8),
        },
    }


def _mixed_request_waves(
    workload: dict[str, dict[str, Any]],
) -> list[list[tuple[str, dict[str, Any], int]]]:
    """Build eight independent 2-embedding/2-rerank waves for one round."""
    embedding_jobs = [
        (name, group, item_index)
        for name, group in workload.items()
        if group["kind"] == "embedding"
        for item_index in range(REQUESTS_PER_ROUND // 2)
    ]
    rerank_jobs = [
        (name, group, item_index)
        for name, group in workload.items()
        if group["kind"] == "rerank"
        for item_index in range(REQUESTS_PER_ROUND)
    ]
    waves = [
        embedding_jobs[offset : offset + 2] + rerank_jobs[offset : offset + 2]
        for offset in range(0, 16, 2)
    ]
    if len(waves) != 8 or any(len(wave) != CLIENT_CONCURRENCY for wave in waves):
        raise RuntimeError("mixed_schedule_shape_invalid")
    return waves


def _validate_model_api_identity(identity: dict[str, Any]) -> str:
    if not isinstance(identity, dict) or identity.get("protocol") != MODEL_API_PROTOCOL:
        raise RuntimeError("model_api_protocol_mismatch")
    return identity["protocol"]


def _request_payload(group: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    common = {"protocol": _validate_model_api_identity(identity)}
    if group["kind"] == "embedding":
        model = identity["embedding"]
        return {
            **common,
            "model_id": model["model_id"],
            "asset_sha256": model["asset_sha256"],
            "implementation_sha256": identity["implementation_sha256"],
            "input_kind": group["input_kind"],
            "inputs": group["inputs"],
        }
    model = identity["reranker"]
    return {
        **common,
        "model_id": model["model_id"],
        "asset_sha256": model["asset_sha256"],
        "implementation_sha256": identity["implementation_sha256"],
        "query": group["query"],
        "documents": group["documents"],
        "max_length": model["max_length"],
        "batch_size": model["batch_size"],
    }


def _identity_binding(identity: dict[str, Any]) -> dict[str, Any]:
    adapter = dict(identity.get("experiment_adapter", {}))
    # This is the one intentional difference across lanes; the same warmed model,
    # config, source implementation, assets, and dependency set must be compared.
    adapter.pop("lane_count", None)
    return {
        "protocol": _validate_model_api_identity(identity),
        "implementation_sha256": identity.get("implementation_sha256"),
        "embedding": identity.get("embedding"),
        "reranker": identity.get("reranker"),
        "runtime": identity.get("runtime"),
        "experiment_adapter": adapter,
    }


def _result_values(group: dict[str, Any], body: dict[str, Any]) -> list[Any]:
    if group["kind"] == "embedding":
        return body.get("data", [])
    return body.get("scores", [])


def _finite_numbers(values: Any) -> list[float] | None:
    """Return finite numeric values, rejecting booleans and malformed scalars."""
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in values
    ):
        return None
    try:
        converted = [float(value) for value in values]
    except (OverflowError, TypeError, ValueError):
        return None
    return converted if all(math.isfinite(value) for value in converted) else None


def _finite_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    # Scaling before summing keeps a valid mean finite when individual values
    # are very large but the unscaled total would overflow.
    return math.fsum(value / len(values) for value in values)


def _error_against_reference(
    group: dict[str, Any], actual: list[Any], expected: list[Any]
) -> dict[str, Any]:
    flat_actual: list[float] = []
    flat_expected: list[float] = []
    if group["kind"] == "embedding":
        if (
            not isinstance(actual, list)
            or not isinstance(expected, list)
            or len(actual) != len(expected)
        ):
            return {"shape_match": False, "max_abs_error": None, "within_tolerance": False}
        if any(not isinstance(a, list) or not isinstance(e, list) or len(a) != len(e)
               for a, e in zip(actual, expected)):
            return {"shape_match": False, "max_abs_error": None, "within_tolerance": False}
        actual_vectors = [_finite_numbers(vector) for vector in actual]
        expected_vectors = [_finite_numbers(vector) for vector in expected]
        if any(vector is None for vector in actual_vectors + expected_vectors):
            return {
                "shape_match": False,
                "max_abs_error": None,
                "within_tolerance": False,
                "numeric_values_valid": False,
            }
        actual_vectors = [vector for vector in actual_vectors if vector is not None]
        expected_vectors = [vector for vector in expected_vectors if vector is not None]
        for actual_vector, expected_vector in zip(actual_vectors, expected_vectors):
            flat_actual.extend(actual_vector)
            flat_expected.extend(expected_vector)
        cosine_values = []
        l2_values = []
        for actual_vector, expected_vector in zip(actual_vectors, expected_vectors):
            actual_norm = math.hypot(*actual_vector)
            expected_norm = math.hypot(*expected_vector)
            cosine = (
                math.fsum((a / actual_norm) * (b / expected_norm)
                          for a, b in zip(actual_vector, expected_vector))
                if actual_norm and expected_norm
                else None
            )
            l2_error = math.hypot(
                *(a - e for a, e in zip(actual_vector, expected_vector))
            )
            if (cosine is not None and not math.isfinite(cosine)) or not math.isfinite(l2_error):
                return {
                    "shape_match": False,
                    "max_abs_error": None,
                    "within_tolerance": False,
                    "numeric_values_valid": False,
                }
            cosine_values.append(
                cosine
            )
            l2_values.append(l2_error)
        tolerance = EMBEDDING_MAX_ABS_TOLERANCE
    else:
        if (
            not isinstance(actual, list)
            or not isinstance(expected, list)
            or len(actual) != len(expected)
        ):
            return {"shape_match": False, "max_abs_error": None, "within_tolerance": False}
        flat_actual = _finite_numbers(actual)
        flat_expected = _finite_numbers(expected)
        if flat_actual is None or flat_expected is None:
            return {
                "shape_match": False,
                "max_abs_error": None,
                "within_tolerance": False,
                "numeric_values_valid": False,
            }
        tolerance = RERANK_MAX_ABS_TOLERANCE
    differences = [abs(a - e) for a, e in zip(flat_actual, flat_expected)]
    if not all(math.isfinite(value) for value in differences):
        return {
            "shape_match": False,
            "max_abs_error": None,
            "within_tolerance": False,
            "numeric_values_valid": False,
        }
    maximum = max(differences, default=0.0)
    metrics: dict[str, Any] = {
        "shape_match": True,
        "max_abs_error": maximum,
        "mean_abs_error": _finite_mean(differences),
        "within_tolerance": math.isfinite(maximum) and maximum <= tolerance,
        "tolerance": tolerance,
        "compared_values": len(differences),
    }
    if group["kind"] == "embedding":
        metrics["cosine_similarity_per_vector"] = cosine_values
        metrics["minimum_cosine_similarity"] = min(
            (value for value in cosine_values if value is not None), default=None
        )
        metrics["l2_error_per_vector"] = l2_values
        metrics["maximum_l2_error"] = max(l2_values, default=0.0)
    else:

        def ranks(values: list[float]) -> list[float]:
            order = sorted(range(len(values)), key=lambda index: (-values[index], index))
            result = [0.0] * len(values)
            for rank, index in enumerate(order, start=1):
                result[index] = float(rank)
            return result

        actual_ranks, expected_ranks = ranks(flat_actual), ranks(flat_expected)
        actual_rank_mean = statistics.fmean(actual_ranks) if actual_ranks else 0.0
        expected_rank_mean = statistics.fmean(expected_ranks) if expected_ranks else 0.0
        numerator = sum(
            (a - actual_rank_mean) * (e - expected_rank_mean)
            for a, e in zip(actual_ranks, expected_ranks)
        )
        denom_a = math.sqrt(sum((a - actual_rank_mean) ** 2 for a in actual_ranks))
        denom_e = math.sqrt(sum((e - expected_rank_mean) ** 2 for e in expected_ranks))
        metrics["spearman_rank_correlation"] = (
            numerator / (denom_a * denom_e) if denom_a and denom_e else 1.0
        )
        concordant = discordant = tied_actual = tied_expected = 0
        for left in range(len(flat_actual)):
            for right in range(left + 1, len(flat_actual)):
                actual_delta = flat_actual[left] - flat_actual[right]
                expected_delta = flat_expected[left] - flat_expected[right]
                if actual_delta == 0:
                    tied_actual += 1
                if expected_delta == 0:
                    tied_expected += 1
                if actual_delta * expected_delta > 0:
                    concordant += 1
                elif actual_delta * expected_delta < 0:
                    discordant += 1
        pair_count = len(flat_actual) * (len(flat_actual) - 1) / 2
        tau_denominator = math.sqrt(
            max(0.0, pair_count - tied_actual) * max(0.0, pair_count - tied_expected)
        )
        metrics["kendall_tau_b"] = (
            (concordant - discordant) / tau_denominator if tau_denominator else 1.0
        )
        k = min(3, len(flat_actual))
        actual_top = set(
            sorted(range(len(flat_actual)), key=lambda index: (-flat_actual[index], index))[:k]
        )
        expected_top = set(
            sorted(range(len(flat_expected)), key=lambda index: (-flat_expected[index], index))[:k]
        )
        metrics["top_k"] = k
        metrics["top_k_overlap_count"] = len(actual_top & expected_top)
        metrics["top_k_overlap_ratio"] = len(actual_top & expected_top) / k if k else 1.0
        metrics["within_tolerance"] = (
            metrics["within_tolerance"]
            and metrics["spearman_rank_correlation"] >= 0.99
            and metrics["kendall_tau_b"] >= 0.95
            and metrics["top_k_overlap_ratio"] >= (2 / 3)
        )
    return metrics


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def nearest_rank(fraction: float) -> float:
        return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]

    return {"p50": nearest_rank(0.50), "p95": nearest_rank(0.95), "max": max(ordered)}


def _request_window_stats(
    rows: list[dict[str, Any]], expected_count: int
) -> dict[str, Any]:
    durations = [
        float(row["duration_ms"])
        for row in rows
        if isinstance(row.get("duration_ms"), (int, float))
        and math.isfinite(float(row["duration_ms"]))
    ]
    successful = [
        row for row in rows if row.get("status_code") == 200 and row.get("error") is None
    ]
    completed = [row for row in rows if isinstance(row.get("response_completed_monotonic_ns"), int)]
    failed = [row for row in rows if row.get("error") is not None]
    start_ns = [
        row["request_started_monotonic_ns"]
        for row in rows
        if isinstance(row.get("request_started_monotonic_ns"), int)
    ]
    completion_ns = [
        row["response_completed_monotonic_ns"]
        for row in rows
        if isinstance(row.get("response_completed_monotonic_ns"), int)
    ]
    span_seconds = (
        (max(completion_ns) - min(start_ns)) / 1_000_000_000
        if start_ns and completion_ns and max(completion_ns) >= min(start_ns)
        else None
    )
    return {
        "request_count": len(rows),
        "expected_request_count": expected_count,
        "success_count": len(successful),
        "failure_count": len(failed),
        "response_completed_count": len(completed),
        "not_sent_count": max(0, expected_count - len(rows)),
        "client_e2e_ms": {"sample_n": len(durations), **_percentiles(durations)},
        "observation_window_seconds": span_seconds,
        "successful_requests_per_second": (
            len(successful) / span_seconds if span_seconds and span_seconds > 0 else None
        ),
        "completed_requests_per_second": (
            len(completed) / span_seconds if span_seconds and span_seconds > 0 else None
        ),
    }


def _analyze_server_events(
    events: list[dict[str, Any]],
    request_ids: set[str],
    request_kinds: dict[str, str] | None = None,
) -> dict[str, Any]:
    by_request: dict[str, dict[str, dict[str, Any]]] = {}
    lock_events: dict[str, list[dict[str, Any]]] = {}
    forward_counts: dict[str, int] = {}
    forward_intervals: list[dict[str, Any]] = []
    request_kinds = request_kinds or {}
    for event in events:
        request_id = event.get("request_id")
        if request_id not in request_ids:
            continue
        by_request.setdefault(request_id, {})[event["event"]] = event
        if event["event"].startswith("preprocess_lock_"):
            lock_events.setdefault(request_id, []).append(event)
        if event.get("event") == "forward_end":
            forward_counts[request_id] = forward_counts.get(request_id, 0) + 1
            start = event.get("cuda_forward_start_from_origin_ms")
            duration = event.get("cuda_forward_interval_ms")
            if isinstance(start, (int, float)) and isinstance(duration, (int, float)):
                forward_intervals.append(
                    {
                        "request_id": request_id,
                        "lane_id": event.get("lane_id"),
                        "model_component": event.get("model_component"),
                        "start_ms": float(start),
                        "end_ms": float(start + duration),
                        "duration_ms": float(duration),
                    }
                )

    queue_wait_ms: list[float] = []
    operation_ms: list[float] = []
    server_e2e_ms: list[float] = []
    dispatch_wait_ms: list[float] = []
    preprocess_wait_ms: list[float] = []
    missing_event_requests: list[str] = []
    event_status_mismatches: list[dict[str, Any]] = []
    server_request_intervals: list[dict[str, Any]] = []
    for request_id in request_ids:
        row = by_request.get(request_id, {})
        enqueued, dequeued = row.get("admission_enqueued"), row.get("worker_dequeued")
        entered, exited = row.get("operation_enter"), row.get("operation_exit")
        worker_start, operation_enter = row.get("worker_start"), row.get("operation_enter")
        received, completed = row.get("request_received"), row.get("request_completed")
        if enqueued and dequeued:
            queue_wait_ms.append((dequeued["monotonic_ns"] - enqueued["monotonic_ns"]) / 1e6)
        if entered and exited:
            operation_ms.append((exited["monotonic_ns"] - entered["monotonic_ns"]) / 1e6)
        if worker_start and operation_enter:
            dispatch_wait_ms.append(
                (operation_enter["monotonic_ns"] - worker_start["monotonic_ns"]) / 1e6
            )
        if received and completed:
            server_e2e_ms.append((completed["monotonic_ns"] - received["monotonic_ns"]) / 1e6)
            server_request_intervals.append(
                {
                    "request_id": request_id,
                    "kind": request_kinds.get(request_id),
                    "start_ns": received["monotonic_ns"],
                    "end_ns": completed["monotonic_ns"],
                }
            )
        if not (
            received
            and completed
            and enqueued
            and dequeued
            and entered
            and exited
            and row.get("lane_drained")
            and forward_counts.get(request_id, 0) > 0
        ):
            missing_event_requests.append(request_id)
        if completed and completed.get("status_code") is None:
            event_status_mismatches.append(
                {"request_id": request_id, "issue": "server_status_missing"}
            )
        lock_row = lock_events.get(request_id, [])
        lock_start = next(
            (item for item in lock_row if item["event"] == "preprocess_lock_wait_start"), None
        )
        lock_acquired = next(
            (item for item in lock_row if item["event"] == "preprocess_lock_acquired"), None
        )
        if lock_start and lock_acquired:
            preprocess_wait_ms.append(
                (lock_acquired["monotonic_ns"] - lock_start["monotonic_ns"]) / 1e6
            )

    intervals = sorted(forward_intervals, key=lambda item: item["start_ms"])
    overlap_pairs: list[dict[str, Any]] = []
    mixed_cuda_overlap_pairs: set[tuple[str, str]] = set()
    sweep_events: list[tuple[float, int, Any]] = []
    for interval in intervals:
        sweep_events.append((interval["start_ms"], 1, interval["lane_id"]))
        sweep_events.append((interval["end_ms"], -1, interval["lane_id"]))
    active_by_lane: dict[Any, int] = {}
    max_simultaneous_lanes = 0
    same_lane_overlap = False
    max_forwards_per_lane = 0
    for _, delta, lane in sorted(sweep_events, key=lambda item: (item[0], item[1])):
        if delta < 0:
            remaining = active_by_lane.get(lane, 0) - 1
            if remaining > 0:
                active_by_lane[lane] = remaining
            else:
                active_by_lane.pop(lane, None)
        else:
            if active_by_lane.get(lane, 0) > 0:
                same_lane_overlap = True
            active_by_lane[lane] = active_by_lane.get(lane, 0) + 1
            max_forwards_per_lane = max(max_forwards_per_lane, active_by_lane[lane])
        max_simultaneous_lanes = max(max_simultaneous_lanes, len(active_by_lane))
    for index, left in enumerate(intervals):
        for right in intervals[index + 1 :]:
            if right["start_ms"] >= left["end_ms"]:
                break
            if left["lane_id"] != right["lane_id"]:
                overlap = min(left["end_ms"], right["end_ms"]) - max(
                    left["start_ms"], right["start_ms"]
                )
                if overlap > 0:
                    overlap_pairs.append(
                        {
                            "left_request_id": left["request_id"],
                            "left_lane": left["lane_id"],
                            "right_request_id": right["request_id"],
                            "right_lane": right["lane_id"],
                            "overlap_ms": overlap,
                        }
                    )
                    if left["model_component"] != right["model_component"]:
                        mixed_cuda_overlap_pairs.add(
                            tuple(sorted((left["request_id"], right["request_id"])))
                        )
    mixed_http_overlap_pairs: set[tuple[str, str]] = set()
    for index, left in enumerate(server_request_intervals):
        for right in server_request_intervals[index + 1 :]:
            if (
                left["kind"]
                and right["kind"]
                and left["kind"] != right["kind"]
                and max(left["start_ns"], right["start_ns"])
                < min(left["end_ns"], right["end_ns"])
            ):
                mixed_http_overlap_pairs.add(
                    tuple(sorted((left["request_id"], right["request_id"])))
                )
    return {
        "request_event_count": len(by_request),
        "missing_event_requests": sorted(missing_event_requests),
        "server_status_mismatches": event_status_mismatches,
        "server_status_by_request": {
            request_id: row["request_completed"].get("status_code")
            for request_id, row in by_request.items()
            if "request_completed" in row
        },
        "queue_wait_ms": _percentiles(queue_wait_ms),
        "worker_dispatch_wait_ms": _percentiles(dispatch_wait_ms),
        "preprocess_lock_wait_ms": _percentiles(preprocess_wait_ms),
        "operation_host_ms": _percentiles(operation_ms),
        "server_http_e2e_ms": _percentiles(server_e2e_ms),
        "forward_event_count": len(forward_intervals),
        "forward_cuda_interval_intervals": intervals,
        "cross_lane_cuda_interval_overlap_pair_count": len(overlap_pairs),
        "mixed_operation_server_http_overlap_pair_count": len(mixed_http_overlap_pairs),
        "mixed_embedding_rerank_cuda_overlap_pair_count": len(mixed_cuda_overlap_pairs),
        "cross_lane_cuda_interval_overlap_ms_sum": sum(
            item["overlap_ms"] for item in overlap_pairs
        ),
        "max_simultaneous_lanes_in_forward_intervals": max_simultaneous_lanes,
        "same_lane_forward_interval_overlap": same_lane_overlap,
        "maximum_concurrent_forward_intervals_per_lane": max_forwards_per_lane,
        "overlap_pairs": overlap_pairs,
        "interpretation": (
            "CUDA event intervals bracket each model forward on its lane stream; interval overlap is evidence of "
            "overlapping stream timelines, not a kernel-level profiler trace."
        ),
    }


async def _wait_for_cancelled_work_to_drain(
    trace_path: Path, request_ids: set[str], timeout: float = 300.0
) -> None:
    """Do not return control to teardown while a disconnected request still owns a model lane."""
    if not request_ids:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = await asyncio.to_thread(trace_path.read_text, encoding="utf-8")
            events = [json.loads(line) for line in lines.splitlines() if line.strip()]
        except (OSError, json.JSONDecodeError):
            events = []
        grouped: dict[str, set[str]] = {}
        for event in events:
            request_id = event.get("request_id")
            if request_id in request_ids:
                grouped.setdefault(request_id, set()).add(event.get("event", ""))
        all_drained = True
        for request_id in request_ids:
            seen = grouped.get(request_id, set())
            if "request_received" not in seen:
                # Cancellation before receipt cannot be distinguished from a
                # request already accepted but not yet flushed to the trace.
                all_drained = False
                continue
            if "worker_start" in seen:
                if "lane_drained" not in seen:
                    all_drained = False
            elif not seen.intersection(
                {
                    "queued_cancel",
                    "queue_timeout",
                    "queue_full",
                    "shutdown_rejected",
                    "request_completed",
                }
            ):
                all_drained = False
        if all_drained:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("model_api_cancelled_work_drain_timeout")


async def _execute_requests(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    identity: dict[str, Any],
    lane_count: int,
    rounds: int,
    stop_event: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    workload = _workload()
    requests: list[dict[str, Any]] = []
    references: dict[str, list[Any]] = {}
    results_by_group: dict[str, list[tuple[str, list[Any]]]] = {}
    failures: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    mixed_waves: list[dict[str, Any]] = []
    stop_reason: str | None = None
    semaphore = asyncio.Semaphore(CLIENT_CONCURRENCY)
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=10.0)
    request_namespace = uuid.uuid4().hex[:16]

    async def wait_for_stop() -> None:
        assert stop_event is not None
        while not stop_event.is_set():
            await asyncio.sleep(0.05)

    async def send_one(
        benchmark_family: str,
        group_name: str,
        group: dict[str, Any],
        round_index: int,
        item_index: int,
        wave_index: int,
    ) -> dict[str, Any]:
        case_key = f"{benchmark_family}/{group_name}/r{round_index:02d}/n{item_index:02d}"
        request_id = hashlib.sha256(
            f"{request_namespace}:{lane_count}:{case_key}".encode("utf-8")
        ).hexdigest()[:32]
        payload = _request_payload(group, identity)
        endpoint = "/v1/embeddings" if group["kind"] == "embedding" else "/v1/rerank"
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Experiment-Request-Id": request_id,
            "Content-Type": "application/json",
        }
        started_ns = time.monotonic_ns()
        started = time.perf_counter()
        row: dict[str, Any] = {
            "request_id": request_id,
            "case_key": case_key,
            "benchmark_family": benchmark_family,
            "group": group_name,
            "operation_kind": group["kind"],
            "round_index": round_index,
            "wave_index": wave_index,
            "lane_count": lane_count,
            "request_started_monotonic_ns": started_ns,
            "input_sha256": _sha256_text(json.dumps(payload, sort_keys=True)),
        }
        async with semaphore:
            try:
                request_task = asyncio.create_task(
                    client.post(
                        base_url.rstrip("/") + endpoint,
                        json=payload,
                        headers=headers,
                        timeout=timeout,
                    )
                )
                stop_task = asyncio.create_task(wait_for_stop()) if stop_event is not None else None
                if stop_task is not None:
                    done, _ = await asyncio.wait(
                        {request_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop_task in done:
                        request_task.cancel()
                        await asyncio.gather(request_task, return_exceptions=True)
                        row.update(
                            {
                                "error": "resource_safety_stop",
                                "stop_reason": "external_resource_gate",
                                "response_completed_monotonic_ns": time.monotonic_ns(),
                            }
                        )
                        failures.append(row.copy())
                        requests.append(row)
                        return row
                    stop_task.cancel()
                    await asyncio.gather(stop_task, return_exceptions=True)
                response = await request_task
                duration_ms = (time.perf_counter() - started) * 1000
                response_completed_ns = time.monotonic_ns()
                row.update(
                    {
                        "status_code": response.status_code,
                        "duration_ms": duration_ms,
                        "response_request_id": response.headers.get("x-experiment-request-id"),
                        "response_completed_monotonic_ns": response_completed_ns,
                    }
                )
                if row["response_request_id"] != request_id:
                    row["error"] = "response_request_id_mismatch"
                try:
                    body = response.json()
                except ValueError:
                    body = {"error": "non_json_response"}
                if response.status_code != 200:
                    row["error"] = row.get("error") or (
                        body.get("error", "http_error") if isinstance(body, dict) else "http_error"
                    )
                else:
                    values = _result_values(group, body) if isinstance(body, dict) else []
                    if not isinstance(values, list):
                        row["error"] = row.get("error") or "output_values_not_list"
                        values = []
                    expected_count = (
                        len(group["inputs"])
                        if group["kind"] == "embedding"
                        else len(group["documents"])
                    )
                    if len(values) != expected_count:
                        row["error"] = row.get("error") or "output_count_mismatch"
                    if group["kind"] == "embedding":
                        dimensions = identity["embedding"]["dimensions"]
                        numeric_vectors = [_finite_numbers(vector) for vector in values]
                        finite = all(
                            isinstance(vector, list)
                            and len(vector) == dimensions
                            and numeric is not None
                            for vector, numeric in zip(values, numeric_vectors)
                        ) and len(numeric_vectors) == len(values)
                        unit_norm = (
                            all(
                                abs(math.hypot(*vector) - 1.0) <= 0.01
                                for vector in numeric_vectors
                                if vector is not None
                            )
                            if finite
                            else False
                        )
                        row["shape_valid"] = finite
                        row["normalized"] = unit_norm
                        if not finite or not unit_norm:
                            row["error"] = row.get("error") or "embedding_shape_or_norm_invalid"
                    else:
                        if _finite_numbers(values) is None:
                            row["error"] = row.get("error") or "rerank_score_non_finite"
                    results_by_group.setdefault(group_name, []).append((case_key, values))
                    row["values"] = values
                if row.get("error"):
                    failures.append(row.copy())
                requests.append(row)
                return row
            except (httpx.HTTPError, TimeoutError) as exc:
                row.update(
                    {
                        "error": type(exc).__name__,
                        "duration_ms": (time.perf_counter() - started) * 1000,
                        "response_completed_monotonic_ns": time.monotonic_ns(),
                    }
                )
                failures.append(row.copy())
                requests.append(row)
                return row

    async with client:
        health = await client.get(
            base_url.rstrip("/") + "/v1/health", headers={"Authorization": f"Bearer {token}"}
        )
        if health.status_code != 200:
            raise RuntimeError(f"model_api_health_failed:{health.status_code}")
        response = await client.get(
            base_url.rstrip("/") + "/v1/identity", headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code != 200:
            raise RuntimeError(f"model_api_identity_failed:{response.status_code}")
        current_identity = response.json()
        _validate_model_api_identity(current_identity)
        adapter = current_identity.get("experiment_adapter", {})
        if (
            adapter.get("lane_count") != lane_count
            or current_identity.get("admission", {}).get("active") != lane_count
        ):
            raise RuntimeError("model_api_experiment_lane_identity_mismatch")
        for key in ("implementation_sha256",):
            if current_identity.get(key) != identity.get(key):
                raise RuntimeError("model_api_reference_implementation_mismatch")

        # Run all six pure workload groups first, then a separately measured
        # 2-embedding/2-rerank mixed phase. Every family has fixed counts.
        stopped = False
        for operation_kind, family in (
            ("embedding", "pure_embedding"),
            ("rerank", "pure_rerank"),
        ):
            for group_name, group in workload.items():
                if group["kind"] != operation_kind:
                    continue
                for round_index in range(rounds):
                    for batch_index in range(REQUESTS_PER_ROUND // CLIENT_CONCURRENCY):
                        first_item = batch_index * CLIENT_CONCURRENCY
                        await asyncio.gather(
                            *(
                                send_one(
                                    family,
                                    group_name,
                                    group,
                                    round_index,
                                    item_index,
                                    batch_index,
                                )
                                for item_index in range(
                                    first_item, first_item + CLIENT_CONCURRENCY
                                )
                            )
                        )
                        if stop_event is not None and stop_event.is_set():
                            stop_reason = "external_resource_gate"
                            stopped = True
                            break
                    if stopped:
                        break
                if stopped:
                    break
            if stopped:
                break
            if stopped:
                break

        if not stopped:
            for round_index in range(rounds):
                for wave_index, jobs in enumerate(_mixed_request_waves(workload)):
                    wave_results = await asyncio.gather(
                        *(
                            send_one("mixed", name, group, round_index, item_index, wave_index)
                            for name, group, item_index in jobs
                        )
                    )
                    kinds = sorted({group["kind"] for _, group, _ in jobs})
                    mixed_waves.append(
                        {
                            "benchmark_family": "mixed",
                            "round_index": round_index,
                            "wave_index": wave_index,
                            "operation_kinds": kinds,
                            "request_ids": [row["request_id"] for row in wave_results],
                            "client_concurrent": len(wave_results) == CLIENT_CONCURRENCY,
                            "mixed_embedding_rerank": set(kinds) == {"embedding", "rerank"},
                        }
                    )
                    if stop_event is not None and stop_event.is_set():
                        stop_reason = "external_resource_gate"
                        stopped = True
                        break
                if stopped:
                    break

    # Each group replays identical inputs across every request/round.
    for group_name, group_results in results_by_group.items():
        if group_results:
            references[group_name] = group_results[0][1]
            for case_key, repeated in group_results:
                checks.append(
                    {
                        "case_key": case_key,
                        "comparison": "within_lane_repeat",
                        **_error_against_reference(
                            workload[group_name], repeated, group_results[0][1]
                        ),
                    }
                )
    requests_per_round = len(workload) * REQUESTS_PER_ROUND + 8 * CLIENT_CONCURRENCY
    family_counts = Counter(row.get("benchmark_family", "unknown") for row in requests)
    duration_stats_by_family_round: dict[str, dict[str, Any]] = {}
    duration_stats_by_group_round: dict[str, dict[str, Any]] = {}
    expected_per_family = {
        "pure_embedding": 4 * REQUESTS_PER_ROUND,
        "pure_rerank": 2 * REQUESTS_PER_ROUND,
        "mixed": 8 * CLIENT_CONCURRENCY,
    }
    for family in ("pure_embedding", "pure_rerank", "mixed"):
        for round_index in range(rounds):
            subset = [
                row
                for row in requests
                if row.get("benchmark_family") == family
                and row.get("round_index") == round_index
            ]
            duration_stats_by_family_round[f"{family}/round-{round_index + 1}"] = (
                _request_window_stats(subset, expected_per_family[family])
            )

    for family in ("pure_embedding", "pure_rerank", "mixed"):
        for group_name, group in workload.items():
            if family == "pure_embedding" and group["kind"] != "embedding":
                continue
            if family == "pure_rerank" and group["kind"] != "rerank":
                continue
            expected_group_count = (
                REQUESTS_PER_ROUND
                if family != "mixed" or group["kind"] == "rerank"
                else REQUESTS_PER_ROUND // 2
            )
            for round_index in range(rounds):
                subset = [
                    row
                    for row in requests
                    if row.get("benchmark_family") == family
                    and row.get("group") == group_name
                    and row.get("round_index") == round_index
                ]
                duration_stats_by_group_round[
                    f"{family}/{group_name}/round-{round_index + 1}"
                ] = _request_window_stats(subset, expected_group_count)

    outcome = {
        "lane_count": lane_count,
        "groups": list(workload),
        "requests_expected": requests_per_round * rounds,
        "requests_completed": len(requests),
        "benchmark_family_counts": dict(family_counts),
        "benchmark_family_counts_expected": {
            "pure_embedding": 4 * REQUESTS_PER_ROUND * rounds,
            "pure_rerank": 2 * REQUESTS_PER_ROUND * rounds,
            "mixed": 8 * CLIENT_CONCURRENCY * rounds,
        },
        "duration_stats_by_family_round": duration_stats_by_family_round,
        "duration_stats_by_group_round": duration_stats_by_group_round,
        "mixed_waves": mixed_waves,
        "mixed_waves_completed": sum(
            1 for wave in mixed_waves if wave["mixed_embedding_rerank"]
        ),
        "mixed_waves_expected": rounds * 8,
        "failures": failures,
        "within_lane_numerical_checks": checks,
        "reference_results": references,
        "workload_sha256": _workload_sha256(),
        "workload_notes": {
            "client_concurrency": CLIENT_CONCURRENCY,
            "requests_per_group_per_round": REQUESTS_PER_ROUND,
            "rounds": rounds,
            "requests_per_tier": requests_per_round * rounds,
            "pure_requests_per_group_per_round": REQUESTS_PER_ROUND,
            "pure_requests_per_round": len(workload) * REQUESTS_PER_ROUND,
            "mixed_requests_per_round": 8 * CLIENT_CONCURRENCY,
            "phase_order": ["pure_embedding", "pure_rerank", "mixed"],
            "schedule": "four pure embedding groups, two pure rerank groups, then eight separate 2-embedding/2-rerank waves per round",
            "mixed_assignment": "four embedding jobs per group and eight rerank jobs per group in workload order; pair two jobs per operation per wave",
            "mixed_waves_per_round": 8,
            "reranker_max_length": identity["reranker"]["max_length"],
            "reranker_batch_size": identity["reranker"]["batch_size"],
            "query_document_batch_size": 4,
        },
        "identity_binding": _identity_binding(identity),
        "model_identity": {
            "implementation_sha256": identity["implementation_sha256"],
            "embedding": identity["embedding"],
            "reranker": identity["reranker"],
            "experiment_adapter": adapter,
        },
        "stop_reason": stop_reason,
    }
    return requests, outcome


async def _run_http_warmup(
    base_url: str,
    token: str,
    identity: dict[str, Any],
    target: Path,
    trace_path: Path,
    stop_event: Any,
) -> dict[str, Any]:
    """Serially warm and validate all six fixed workload shapes before measurement."""
    workload = _workload()
    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "passed": False,
        "requests_expected": len(workload),
        "requests_completed": 0,
        "groups": list(workload),
    }
    namespace = uuid.uuid4().hex[:16]
    timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=10.0)

    def persist() -> None:
        report["requests_completed"] = sum("status_code" in row for row in rows)
        _persist_warmup(target, rows, report)

    async with httpx.AsyncClient(
        trust_env=False,
        timeout=timeout,
        limits=httpx.Limits(max_connections=1),
    ) as client:
        for group_index, (group_name, group) in enumerate(workload.items()):
            if stop_event is not None and stop_event.is_set():
                report["error"] = "external_resource_gate"
                persist()
                raise RuntimeError("model_api_warmup_stopped_by_resource_gate")
            request_id = hashlib.sha256(
                f"{namespace}:warmup:{group_index}:{group_name}".encode("utf-8")
            ).hexdigest()[:32]
            payload = _request_payload(group, identity)
            endpoint = "/v1/embeddings" if group["kind"] == "embedding" else "/v1/rerank"
            row: dict[str, Any] = {
                "request_id": request_id,
                "group": group_name,
                "operation_kind": group["kind"],
                "input_sha256": _sha256_text(json.dumps(payload, sort_keys=True)),
            }
            try:
                response = await client.post(
                    base_url.rstrip("/") + endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Experiment-Request-Id": request_id,
                    },
                )
            except httpx.HTTPError as exc:
                row["error"] = f"transport_{type(exc).__name__}"
                rows.append(row)
                report["error"] = row["error"]
                persist()
                raise RuntimeError(f"model_api_warmup_failed:{group_name}:{row['error']}") from exc

            row["status_code"] = response.status_code
            row["response_request_id"] = response.headers.get("x-experiment-request-id")
            try:
                body = response.json()
            except ValueError:
                body = {"error": "non_json_response"}
            if response.status_code != 200:
                row["error"] = f"http_status_{response.status_code}"
                row["error_body"] = body
            elif row["response_request_id"] != request_id:
                row["error"] = "response_request_id_mismatch"
            elif not isinstance(body, dict) or any(
                body.get(key) != expected
                for key, expected in (
                    ("protocol", identity["protocol"]),
                    (
                        "model_id",
                        identity["embedding"]["model_id"]
                        if group["kind"] == "embedding"
                        else identity["reranker"]["model_id"],
                    ),
                    (
                        "asset_sha256",
                        identity["embedding"]["asset_sha256"]
                        if group["kind"] == "embedding"
                        else identity["reranker"]["asset_sha256"],
                    ),
                    ("implementation_sha256", identity["implementation_sha256"]),
                )
            ):
                row["error"] = "response_model_identity_mismatch"
            else:
                values = _result_values(group, body)
                expected_count = (
                    len(group["inputs"])
                    if group["kind"] == "embedding"
                    else len(group["documents"])
                )
                if not isinstance(values, list):
                    valid = False
                elif group["kind"] == "embedding":
                    numeric_vectors = [_finite_numbers(vector) for vector in values]
                    valid = (
                        len(values) == expected_count
                        and all(
                            isinstance(vector, list)
                            and len(vector) == identity["embedding"]["dimensions"]
                            and numeric is not None
                            and abs(math.hypot(*numeric) - 1.0) <= 0.01
                            for vector, numeric in zip(values, numeric_vectors)
                        )
                        and len(numeric_vectors) == expected_count
                    )
                else:
                    valid = (
                        len(values) == expected_count
                        and _finite_numbers(values) is not None
                    )
                if not valid:
                    row["error"] = "output_shape_or_numeric_invalid"
                else:
                    row["values"] = values
            rows.append(row)
            if row.get("error"):
                report["error"] = row["error"]
                persist()
                raise RuntimeError(f"model_api_warmup_failed:{group_name}:{row['error']}")
            persist()

    try:
        events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        report["error"] = "trace_invalid"
        persist()
        raise RuntimeError("model_api_warmup_trace_invalid") from exc
    metrics = _analyze_server_events(
        events,
        {row["request_id"] for row in rows},
        {row["request_id"]: row["operation_kind"] for row in rows},
    )
    report["server_events"] = metrics
    report["passed"] = (
        len(rows) == len(workload)
        and all("error" not in row for row in rows)
        and not metrics["missing_event_requests"]
        and not metrics["server_status_mismatches"]
        and all(
            metrics["server_status_by_request"].get(row["request_id"]) == 200 for row in rows
        )
    )
    persist()
    if not report["passed"]:
        report["error"] = "warmup_server_event_validation_failed"
        persist()
        raise RuntimeError("model_api_warmup_server_events_invalid")
    return report


def run_model_http_stage(
    state: dict[str, Any],
    output: Path | str,
    *,
    model_token: str,
    lane_count: int,
    reference: Path | str | dict[str, Any] | None = None,
    rounds: int = DEFAULT_ROUNDS,
    stop_event: Any = None,
) -> dict[str, Any]:
    """Send direct model API traffic; assumes the root runner started the frozen stack."""
    if type(lane_count) is not int or lane_count not in {1, 2, 4}:
        raise ValueError("invalid_experiment_lane_count")
    if type(rounds) is not int or not 1 <= rounds <= 3:
        raise ValueError("invalid_experiment_rounds")
    base_url = state.get("model_base_url")
    if not isinstance(base_url, str) or not base_url.startswith("http://127.0.0.1:"):
        raise ValueError("model_api_must_use_isolated_loopback_url")
    if not model_token or len(model_token) < 24:
        raise ValueError("model_api_token_invalid")
    target = Path(output)
    target.mkdir(parents=True, exist_ok=True)
    trace_path = Path(state.get("trace_file", ""))
    if not trace_path.is_file():
        raise FileNotFoundError("model_api_trace_file_missing")

    # Fetch identity separately so workload body and its checkpoint binding are explicit.
    with httpx.Client(timeout=20.0, trust_env=False) as client:
        identity_response = client.get(
            base_url.rstrip("/") + "/v1/identity",
            headers={"Authorization": f"Bearer {model_token}"},
        )
    if identity_response.status_code != 200:
        raise RuntimeError(f"model_api_identity_failed:{identity_response.status_code}")
    identity = identity_response.json()
    _validate_model_api_identity(identity)
    adapter = identity.get("experiment_adapter", {})
    if adapter.get("lane_count") != lane_count:
        raise RuntimeError("model_api_experiment_lane_identity_mismatch")

    reference_obj: dict[str, Any] | None = None
    if reference is not None:
        reference_obj = (
            json.loads(Path(reference).read_text(encoding="utf-8"))
            if isinstance(reference, (Path, str))
            else reference
        )
        if reference_obj.get("workload_sha256") != _workload_sha256():
            raise RuntimeError("model_api_reference_workload_mismatch")
        if reference_obj.get("identity_binding") != _identity_binding(identity):
            raise RuntimeError("model_api_reference_identity_mismatch")

    # Serially warm all six fixed workload groups before the measured matrix.
    # Each status/schema/output/event failure stops here and is persisted.
    warmup = asyncio.run(
        _run_http_warmup(
            base_url,
            model_token,
            identity,
            target,
            trace_path,
            stop_event,
        )
    )

    requests, outcome = asyncio.run(
        _execute_requests(
            httpx.AsyncClient(
                trust_env=False, limits=httpx.Limits(max_connections=CLIENT_CONCURRENCY)
            ),
            base_url,
            model_token,
            identity,
            lane_count,
            rounds,
            stop_event,
        )
    )
    _persist_raw_stage_output(target, requests)
    cancelled_ids = {
        row["request_id"] for row in requests if row.get("error") == "resource_safety_stop"
    }
    if cancelled_ids:
        asyncio.run(_wait_for_cancelled_work_to_drain(trace_path, cancelled_ids))
    current_reference = {
        "schema_version": 1,
        "model_identity": outcome["model_identity"],
        "identity_binding": outcome["identity_binding"],
        "workload_sha256": outcome["workload_sha256"],
        "results": outcome["reference_results"],
    }
    current_reference_path = target / "reference.json"
    current_reference_path.write_text(json.dumps(current_reference, indent=2), encoding="utf-8")

    if reference is not None:
        assert reference_obj is not None
        numerical_checks = []
        for row in requests:
            if "values" not in row:
                continue
            case_key = row["case_key"]
            expected = reference_obj.get("results", {}).get(row["group"])
            if expected is None:
                numerical_checks.append(
                    {
                        "case_key": case_key,
                        "within_tolerance": False,
                        "error": "reference_case_missing",
                    }
                )
                continue
            group = _workload()[row["group"]]
            numerical_checks.append(
                {
                    "request_id": row["request_id"],
                    "case_key": case_key,
                    **_error_against_reference(group, row["values"], expected),
                }
            )
        outcome["reference_numerical_checks"] = numerical_checks
        outcome["reference_numerical_pass"] = (
            len(numerical_checks) == sum(1 for row in requests if "values" in row)
            and len(reference_obj.get("results", {})) == len(_workload())
            and all(item.get("within_tolerance") is True for item in numerical_checks)
        )
    else:
        outcome["reference_numerical_checks"] = []
        outcome["reference_numerical_pass"] = (
            lane_count == 1
            and outcome["requests_completed"] == outcome["requests_expected"]
            and not outcome["failures"]
            and set(outcome["reference_results"]) == set(_workload())
            and all(
                item.get("within_tolerance") is True
                for item in outcome["within_lane_numerical_checks"]
            )
        )

    try:
        server_events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("model_api_trace_invalid") from exc
    request_ids = {row["request_id"] for row in requests}
    metrics = _analyze_server_events(
        server_events,
        request_ids,
        {row["request_id"]: row["operation_kind"] for row in requests},
    )
    request_id_matches = [
        row
        for row in requests
        if row.get("status_code") is not None
        and row.get("response_request_id") == row["request_id"]
    ]
    status_mismatches = []
    for row in requests:
        if row.get("status_code") is None:
            continue
        server_status = metrics["server_status_by_request"].get(row["request_id"])
        if server_status != row["status_code"]:
            status_mismatches.append(
                {
                    "request_id": row["request_id"],
                    "client_status": row["status_code"],
                    "server_status": server_status,
                }
            )
    for mismatch in status_mismatches:
        requests_by_id = {row["request_id"]: row for row in requests}
        failed_row = requests_by_id.get(mismatch["request_id"])
        if failed_row is not None and not failed_row.get("error"):
            failed_row["error"] = "client_server_status_mismatch"
            outcome["failures"].append(failed_row.copy())
    metrics["client_server_request_id_match_count"] = len(request_id_matches)
    metrics["client_request_count_with_response"] = sum(
        1 for row in requests if row.get("status_code") is not None
    )
    metrics["client_server_status_mismatches"] = status_mismatches
    outcome["server_events"] = metrics
    distinct_failures = {row["request_id"]: row for row in outcome["failures"]}
    outcome["failures"] = list(distinct_failures.values())
    outcome["failure_categories"] = dict(
        Counter(row.get("error", "unknown") for row in outcome["failures"])
    )
    outcome["failure_ratio"] = len(distinct_failures) / max(1, outcome["requests_expected"])
    outcome["warmup"] = warmup
    outcome["request_id_correlation_pass"] = (
        len(request_id_matches) == outcome["requests_completed"]
    )
    outcome["all_server_events_present"] = not metrics["missing_event_requests"]
    outcome["mixed_server_http_overlap_observed"] = (
        metrics["mixed_operation_server_http_overlap_pair_count"] > 0
    )
    outcome["server_status_correlation_pass"] = not status_mismatches
    outcome["cuda_interval_overlap_observed"] = (
        metrics["max_simultaneous_lanes_in_forward_intervals"] >= lane_count
    )
    outcome["client_e2e_ms"] = _percentiles(
        [
            float(row["duration_ms"])
            for row in requests
            if isinstance(row.get("duration_ms"), (int, float))
        ]
    )
    outcome["stage_pass"] = (
        outcome["warmup"].get("passed") is True
        and outcome.get("stop_reason") is None
        and outcome["requests_completed"] == outcome["requests_expected"]
        and outcome["benchmark_family_counts"]
        == outcome["benchmark_family_counts_expected"]
        and all(
            row["request_count"] == row["expected_request_count"]
            for row in outcome["duration_stats_by_family_round"].values()
        )
        and all(
            row["request_count"] == row["expected_request_count"]
            for row in outcome["duration_stats_by_group_round"].values()
        )
        and outcome["failure_ratio"] <= 0.05
        and outcome["reference_numerical_pass"]
        and outcome["all_server_events_present"]
        and outcome["mixed_waves_completed"] == outcome["mixed_waves_expected"]
        and outcome["mixed_server_http_overlap_observed"]
        and outcome["request_id_correlation_pass"]
        and outcome["server_status_correlation_pass"]
        and not metrics["same_lane_forward_interval_overlap"]
        and (lane_count == 1 or outcome["cuda_interval_overlap_observed"])
    )
    outcome["passed"] = outcome["stage_pass"]

    _persist_raw_stage_output(target, requests)
    _write_text_atomic(target / "server-metrics.json", json.dumps(metrics, indent=2))
    _write_text_atomic(target / "summary.json", json.dumps(outcome, indent=2))
    return outcome
