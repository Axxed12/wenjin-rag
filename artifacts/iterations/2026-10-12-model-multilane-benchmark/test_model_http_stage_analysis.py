"""CPU-only contract checks for HTTP-stage pure event/metric analysis."""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("model_http_stage.py")
SPEC = importlib.util.spec_from_file_location("model_http_stage_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
stage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stage
SPEC.loader.exec_module(stage)


def _forward(request_id: str, lane_id: int, start_ms: float, duration_ms: float) -> dict:
    return {
        "event": "forward_end",
        "request_id": request_id,
        "lane_id": lane_id,
        "model_component": "embedding",
        "cuda_forward_start_from_origin_ms": start_ms,
        "cuda_forward_interval_ms": duration_ms,
    }


def test_cuda_interval_sweep_counts_peak_distinct_lanes_not_historical_union() -> None:
    events = [
        _forward("a", 0, 0.0, 10.0),
        _forward("b", 1, 1.0, 1.0),
        _forward("c", 2, 3.0, 1.0),
    ]

    metrics = stage._analyze_server_events(events, {"a", "b", "c"})

    assert metrics["max_simultaneous_lanes_in_forward_intervals"] == 2
    assert metrics["cross_lane_cuda_interval_overlap_pair_count"] == 2
    assert metrics["same_lane_forward_interval_overlap"] is False


def test_touching_cuda_intervals_do_not_count_as_overlap() -> None:
    events = [_forward("a", 0, 0.0, 1.0), _forward("b", 1, 1.0, 1.0)]

    metrics = stage._analyze_server_events(events, {"a", "b"})

    assert metrics["max_simultaneous_lanes_in_forward_intervals"] == 1
    assert metrics["cross_lane_cuda_interval_overlap_pair_count"] == 0


def test_reference_metrics_report_embedding_norm_and_rerank_order() -> None:
    embedding = stage._error_against_reference(
        {"kind": "embedding"}, [[1.0, 0.0], [0.0, 2.0]], [[1.0, 0.0], [0.0, 2.0]]
    )
    rerank = stage._error_against_reference(
        {"kind": "rerank"}, [0.8, 0.2, 0.5], [0.8, 0.2, 0.5]
    )

    assert embedding["shape_match"] and embedding["within_tolerance"]
    assert embedding["minimum_cosine_similarity"] == 1.0
    assert embedding["maximum_l2_error"] == 0.0
    assert math.isclose(rerank["spearman_rank_correlation"], 1.0)
    assert rerank["kendall_tau_b"] == 1.0
    assert rerank["top_k_overlap_ratio"] == 1.0


def test_identity_binding_ignores_only_lane_count() -> None:
    identity = {
        "protocol": "wenjin-model-api/1",
        "embedding": {"asset_sha256": "embed"},
        "reranker": {"asset_sha256": "rerank"},
        "runtime": {"torch": "version"},
        "experiment_adapter": {"lane_count": 1, "adapter_sha256": "adapter"},
    }
    changed_lanes = {**identity, "experiment_adapter": {"lane_count": 4, "adapter_sha256": "adapter"}}
    changed_adapter = {**identity, "experiment_adapter": {"lane_count": 4, "adapter_sha256": "changed"}}

    assert stage._identity_binding(identity) == stage._identity_binding(changed_lanes)
    assert stage._identity_binding(identity) != stage._identity_binding(changed_adapter)


def test_invalid_numeric_reference_values_fail_closed_without_raising() -> None:
    invalid_values = [True, "0.5", float("nan"), float("inf"), 10**10000]

    for invalid in invalid_values:
        assert stage._finite_numbers([invalid]) is None
        embedding = stage._error_against_reference(
            {"kind": "embedding"}, [[invalid]], [[1.0]]
        )
        rerank = stage._error_against_reference(
            {"kind": "rerank"}, [invalid], [1.0]
        )
        assert embedding["shape_match"] is False
        assert embedding["within_tolerance"] is False
        assert rerank["shape_match"] is False
        assert rerank["within_tolerance"] is False


def test_extreme_finite_embedding_values_do_not_overflow_analysis() -> None:
    metrics = stage._error_against_reference(
        {"kind": "embedding"}, [[1e308]], [[1.0]]
    )

    assert metrics["within_tolerance"] is False


def test_fixed_schedule_has_separate_pure_and_mixed_request_budgets() -> None:
    workload = stage._workload()
    waves = stage._mixed_request_waves(workload)
    counts = {name: 0 for name in workload}
    for wave in waves:
        for name, _, _ in wave:
            counts[name] += 1

    assert len(waves) == 8
    assert all(len(wave) == stage.CLIENT_CONCURRENCY for wave in waves)
    assert all({job[1]["kind"] for job in wave} == {"embedding", "rerank"} for wave in waves)
    assert all(
        count == (stage.REQUESTS_PER_ROUND // 2 if workload[name]["kind"] == "embedding" else stage.REQUESTS_PER_ROUND)
        for name, count in counts.items()
    )
    pure_per_round = len(workload) * stage.REQUESTS_PER_ROUND
    mixed_per_round = len(waves) * stage.CLIENT_CONCURRENCY
    assert pure_per_round == 48
    assert mixed_per_round == 32
    assert (pure_per_round + mixed_per_round) * stage.DEFAULT_ROUNDS == 240


def test_workload_hash_binds_operation_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    original = stage._workload()
    monkeypatch.setattr(stage, "_workload", lambda: original)
    original_hash = stage._workload_sha256()
    changed = {name: dict(group) for name, group in original.items()}
    changed["embedding_query_short"]["kind"] = "rerank"
    monkeypatch.setattr(stage, "_workload", lambda: changed)

    assert stage._workload_sha256() != original_hash


def test_request_window_stats_reports_counts_latency_window_and_throughput() -> None:
    rows = [
        {
            "request_started_monotonic_ns": 1_000_000_000,
            "response_completed_monotonic_ns": 2_000_000_000,
            "duration_ms": 100.0,
            "status_code": 200,
        },
        {
            "request_started_monotonic_ns": 1_500_000_000,
            "response_completed_monotonic_ns": 3_000_000_000,
            "duration_ms": 150.0,
            "status_code": 500,
            "error": "http_error",
        },
    ]

    stats = stage._request_window_stats(rows, expected_count=3)

    assert stats["request_count"] == 2
    assert stats["success_count"] == 1
    assert stats["failure_count"] == 1
    assert stats["response_completed_count"] == 2
    assert stats["not_sent_count"] == 1
    assert stats["client_e2e_ms"] == {"sample_n": 2, "p50": 100.0, "p95": 150.0, "max": 150.0}
    assert stats["observation_window_seconds"] == 2.0
    assert stats["successful_requests_per_second"] == 0.5
    assert stats["completed_requests_per_second"] == 1.0


def test_server_http_events_measure_overlapping_embedding_and_rerank_requests() -> None:
    events = [
        {"event": "request_received", "request_id": "embed", "monotonic_ns": 10},
        {"event": "request_completed", "request_id": "embed", "monotonic_ns": 50},
        {"event": "request_received", "request_id": "rerank", "monotonic_ns": 20},
        {"event": "request_completed", "request_id": "rerank", "monotonic_ns": 40},
    ]

    metrics = stage._analyze_server_events(
        events,
        {"embed", "rerank"},
        {"embed": "embedding", "rerank": "rerank"},
    )

    assert metrics["mixed_operation_server_http_overlap_pair_count"] == 1


def test_model_api_payload_uses_and_validates_server_identity_protocol() -> None:
    identity = {
        "protocol": "wenjin-model-api/1",
        "embedding": {"model_id": "embed", "asset_sha256": "asset"},
        "implementation_sha256": "implementation",
    }
    payload = stage._request_payload(stage._workload()["embedding_query_short"], identity)

    assert payload["protocol"] == "wenjin-model-api/1"
    with pytest.raises(RuntimeError, match="model_api_protocol_mismatch"):
        stage._validate_model_api_identity({**identity, "protocol": "2025-11-25"})
