"""Independent read-only comparison of frozen model benchmark runs.

The script consumes only saved run artifacts; it never starts a service or sends
requests. Latencies are direct intervals between saved per-request events and
are deliberately not inferred by subtracting one latency from another.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def percentiles(values: list[float]) -> dict[str, float | None]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return {"n": 0, "median": None, "nearest_rank_p50": None, "p95": None, "max": None}
    return {
        "n": len(ordered),
        "median": statistics.median(ordered),
        "nearest_rank_p50": ordered[max(0, math.ceil(0.50 * len(ordered)) - 1)],
        "p95": ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)],
        "max": ordered[-1],
    }


def event_interval_ms(
    by_request: dict[str, dict[str, list[dict[str, Any]]]],
    request_id: str,
    begin: str,
    end: str,
) -> float | None:
    request_events = by_request.get(request_id, {})
    starts = request_events.get(begin, [])
    ends = request_events.get(end, [])
    if not starts or not ends:
        return None
    start_ns = starts[0].get("monotonic_ns")
    end_ns = ends[-1].get("monotonic_ns")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int) or end_ns < start_ns:
        return None
    return (end_ns - start_ns) / 1_000_000


def load_run(run_dir: Path) -> dict[str, Any]:
    outcome = read_json(run_dir / "outcome.json")
    model = outcome.get("model", {})
    freeze = read_json(run_dir / "freeze.json")
    requests = read_jsonl(run_dir / "client-requests.jsonl")
    events = read_jsonl(run_dir / "model-events.jsonl")

    by_request: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        request_id = event.get("request_id")
        name = event.get("event")
        if isinstance(request_id, str) and isinstance(name, str):
            by_request[request_id][name].append(event)

    rows: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for request in requests:
        family = request.get("benchmark_family")
        group = request.get("group")
        round_index = request.get("round_index")
        if isinstance(family, str) and isinstance(group, str) and isinstance(round_index, int):
            rows[(family, group, round_index)].append(request)

    def summarize(subset: list[dict[str, Any]], family_phase: bool = False) -> dict[str, Any]:
        queue = []
        operation = []
        server_e2e = []
        client_e2e = []
        forward_counts = Counter()
        errors = 0
        successes = 0
        missing_event_requests = []
        starts = []
        completions = []
        for request in subset:
            request_id = request["request_id"]
            server = by_request.get(request_id, {})
            for begin, end, target in (
                ("admission_enqueued", "worker_dequeued", queue),
                ("operation_enter", "operation_exit", operation),
                ("request_received", "request_completed", server_e2e),
            ):
                interval = event_interval_ms(by_request, request_id, begin, end)
                if interval is not None:
                    target.append(interval)
            if isinstance(request.get("duration_ms"), (int, float)):
                client_e2e.append(float(request["duration_ms"]))
            request_forward_ends = [
                event for event in server.get("forward_end", [])
                if isinstance(event.get("cuda_forward_interval_ms"), (int, float))
            ]
            forward_counts.update(event.get("model_component", "unknown") for event in request_forward_ends)
            if request.get("status_code") == 200 and request.get("error") is None:
                successes += 1
            else:
                errors += 1
            if not all(name in server for name in ("request_received", "request_completed")):
                missing_event_requests.append(request_id)
            started = request.get("request_started_monotonic_ns")
            completed = request.get("response_completed_monotonic_ns")
            if isinstance(started, int):
                starts.append(started)
            if isinstance(completed, int):
                completions.append(completed)

        span_seconds = None
        if family_phase and starts and completions and max(completions) >= min(starts):
            span_seconds = (max(completions) - min(starts)) / 1_000_000_000
        return {
            "request_n": len(subset),
            "success_n": successes,
            "error_n": errors,
            "forward_end_n_by_component": dict(forward_counts),
            "queue_ms": percentiles(queue),
            "operation_ms": percentiles(operation),
            "server_e2e_ms": percentiles(server_e2e),
            "client_e2e_ms": percentiles(client_e2e),
            "family_phase_window_seconds": span_seconds,
            "family_phase_successful_rps": (
                successes / span_seconds if span_seconds and span_seconds > 0 else None
            ),
            "missing_request_lifecycle_events": missing_event_requests,
        }

    family_rows = {}
    for family in ("pure_embedding", "pure_rerank", "mixed"):
        subset = [row for row in requests if row.get("benchmark_family") == family]
        family_rows[family] = summarize(subset, family_phase=True)

    group_rows = {}
    for key in sorted(rows):
        family, group, round_index = key
        group_rows[f"{family}/{group}/round-{round_index + 1}"] = summarize(rows[key])

    return {
        "run_id": freeze.get("run_id"),
        "lane_count": freeze.get("lanes"),
        "source_sha256": freeze.get("source_sha256"),
        "formal_source_sha256": freeze.get("formal_source_sha256"),
        "workload_sha256": model.get("workload_sha256"),
        "request_count": len(requests),
        "event_count": len(events),
        "run_passed": outcome.get("passed") is True and model.get("passed") is True,
        "formal_source_unchanged": outcome.get("formal_source_unchanged"),
        "frozen_files_unchanged": outcome.get("frozen_files_unchanged"),
        "model_state_unchanged_after_drain": outcome.get("model_state_unchanged_after_drain"),
        "families": family_rows,
        "group_rounds": group_rows,
    }


def fmt(stats: dict[str, Any], key: str) -> str:
    values = stats[key]
    if values["n"] == 0:
        return "n=0"
    return (f"n={values['n']}; median/nr-p50/p95/max="
            f"{values['median']:.2f}/{values['nearest_rank_p50']:.2f}/"
            f"{values['p95']:.2f}/{values['max']:.2f} ms")


def make_markdown(left: dict[str, Any], right: dict[str, Any]) -> str:
    lines = [
        "# 多路模型实验独立复算对照",
        "",
        f"- API 1: `{left['run_id']}`, lanes={left['lane_count']}, pass={left['run_passed']}",
        f"- API 2: `{right['run_id']}`, lanes={right['lane_count']}, pass={right['run_passed']}",
        f"- source SHA 相同：`{left['source_sha256'] == right['source_sha256']}`；workload SHA 相同：`{left['workload_sha256'] == right['workload_sha256']}`",
        "- queue 为每请求 `admission_enqueued → worker_dequeued`；execution 为 `operation_enter → operation_exit`；server E2E 为 `request_received → request_completed`。这些是各自事件的直接时间差，不用 E2E 减 queue 推算 execution。",
        "- median为`statistics.median`（偶数样本取中间两项均值）；nearest-rank P50与P95另列。family吞吐由连续family phase最早client start至最晚client completion计算。group/round表只描述该组该轮raw请求窗口。",
        "",
        "## 连续阶段吞吐与总量",
        "",
        "| Family | API | Requests (success/error) | Forward ends by component | Phase span (s) | Success req/s | Queue median/nr-p50/p95/max | Exec median/nr-p50/p95/max | Server E2E median/nr-p50/p95/max | Client E2E median/nr-p50/p95/max |",
        "|---|---:|---:|---|---:|---:|---|---|---|---|",
    ]
    for family in ("pure_embedding", "pure_rerank", "mixed"):
        for api, result in (("1", left), ("2", right)):
            row = result["families"][family]
            lines.append(
                f"| {family} | {api} | {row['request_n']} ({row['success_n']}/{row['error_n']}) | "
                f"`{row['forward_end_n_by_component']}` | {row['family_phase_window_seconds']:.3f} | "
                f"{row['family_phase_successful_rps']:.3f} | {fmt(row, 'queue_ms')} | {fmt(row, 'operation_ms')} | "
                f"{fmt(row, 'server_e2e_ms')} | {fmt(row, 'client_e2e_ms')} |"
            )
    lines.extend(
        [
            "",
            "## Group / round 重复",
            "",
            "| Group/round | API | Requests (success/error) | Forward ends by component | Queue median/nr-p50/p95/max | Exec median/nr-p50/p95/max | Server E2E median/nr-p50/p95/max | Client E2E median/nr-p50/p95/max |",
            "|---|---:|---:|---|---|---|---|---|",
        ]
    )
    group_names = sorted(set(left["group_rounds"]) | set(right["group_rounds"]))
    for name in group_names:
        for api, result in (("1", left), ("2", right)):
            row = result["group_rounds"].get(name)
            if row is None:
                lines.append(f"| {name} | {api} | missing | - | - | - | - | - |")
                continue
            lines.append(
                f"| {name} | {api} | {row['request_n']} ({row['success_n']}/{row['error_n']}) | "
                f"`{row['forward_end_n_by_component']}` | {fmt(row, 'queue_ms')} | {fmt(row, 'operation_ms')} | "
                f"{fmt(row, 'server_e2e_ms')} | {fmt(row, 'client_e2e_ms')} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api1_run", type=Path)
    parser.add_argument("api2_run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    left, right = load_run(args.api1_run), load_run(args.api2_run)
    if left["lane_count"] != 1 or right["lane_count"] != 2:
        raise SystemExit("expected API1 lanes=1 and API2 lanes=2")
    if left["source_sha256"] != right["source_sha256"]:
        raise SystemExit("frozen source SHA mismatch")
    if left["formal_source_sha256"] != right["formal_source_sha256"]:
        raise SystemExit("formal source SHA mismatch")
    if left["workload_sha256"] != right["workload_sha256"]:
        raise SystemExit("workload SHA mismatch")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(make_markdown(left, right), encoding="utf-8")
    print(json.dumps({"api1": left, "api2": right, "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
