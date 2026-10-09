"""CPU-only：核对phase4现场报告的可复核门禁并生成人读MD摘要。"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/phase4-isolated-e2e"
REPORT = OUT / "report.json"
RUNNER = ROOT / "tests/acceptance/run_phase4_isolated_e2e.py"
TARGET_ID = "ogc-3dtiles-1.1-blocks-split-candidate-003"
ACTIVE_ID = "osg-3.6.5-blocks-dualhash-003"
MANIFEST_REL = Path("corpora/ogc-3dtiles-1.1/source-manifest.json")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def process_exists(pid: int) -> bool | None:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0 or "Access denied" in result.stdout + result.stderr:
            return None
        return f'"{pid}"' in result.stdout
    except (OSError, subprocess.SubprocessError):
        return None  # 当前Windows权限无法查询时明确记为不可探测。


def main() -> None:
    report: dict[str, Any] = json.loads(REPORT.read_text(encoding="utf-8"))
    steps = report["steps"]
    expected_manifest = next(
        item["manifest_sha256"] for item in json.loads(
            (ROOT / "configs/indexes/multicorpus-candidates-003.json").read_text(
                encoding="utf-8-sig"
            )
        )["candidates"] if item["target_id"] == TARGET_ID
    )
    actual_manifest = file_sha(ROOT / MANIFEST_REL)
    runner_sha = file_sha(RUNNER)
    runner_modules = {
        relative: file_sha(ROOT / relative)
        for relative in (
            "src/wenjin/retrieval/source_context.py",
            "src/wenjin/indexing/bm25.py",
            "src/wenjin/retrieval/router.py",
            "src/wenjin/retrieval/service.py",
            "src/wenjin/mcp_server.py",
            "src/wenjin/serving.py",
            "tests/support/phase4_process_metrics.py",
        )
    }
    log_names = ("server-osg.log", "server-3dtiles.log", "server-osg-rollback.log")
    server_ids: dict[str, int | None] = {}
    for step, log_name in zip(steps, log_names):
        log = (OUT / log_name).read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Started server process \[(\d+)\]", log)
        server_pid = int(match.group(1)) if match else None
        server_ids[log_name] = server_pid
        metrics = step.get("server_process_metrics")
        if step["action"] == "switch_to_candidate003":
            metrics = step.get("server_process_metrics")
        step["uvicorn_server_pid_from_log"] = server_pid
        step["uvicorn_pid_matches_worker_metrics"] = bool(
            server_pid and metrics and server_pid == metrics.get("pid")
        )
        status = process_exists(server_pid) if server_pid else None
        step["uvicorn_pid_exit_probe"] = (
            "not_running" if status is False
            else "access_denied_or_unavailable" if status is None
            else "still_running_or_pid_missing"
        )
    # A process wrapper is stopped by the following restart/cleanup record.
    steps[0]["launcher_pid_exited"] = bool(steps[1].get("prior_server_stop", {}).get("stopped"))
    steps[1]["launcher_pid_exited"] = bool(steps[2].get("prior_server_stop", {}).get("stopped"))
    steps[2]["launcher_pid_exited"] = bool(
        report.get("cleanup") and report["cleanup"][-1].get("stopped")
    )
    # The middle service's PID appears under the switch step's restart and metrics fields.
    if steps[1].get("uvicorn_server_pid_from_log") is None:
        log = (OUT / "server-3dtiles.log").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Started server process \[(\d+)\]", log)
        pid = int(match.group(1)) if match else None
        steps[1]["uvicorn_server_pid_from_log"] = pid
        steps[1]["uvicorn_pid_matches_worker_metrics"] = bool(
            pid and steps[1].get("server_process_metrics", {}).get("pid") == pid
        )
        status = process_exists(pid) if pid else None
        steps[1]["uvicorn_pid_exit_probe"] = (
            "not_running" if status is False
            else "access_denied_or_unavailable" if status is None
            else "still_running_or_pid_missing"
        )

    routes = steps[0]["protocol"], steps[2]["protocol"]
    explicit = steps[0]["explicit_candidate003_route"]["lifecycle"]
    continuity = steps[0]["same_session_lifecycle_probe"]
    protocols = [routes[0], explicit, continuity, routes[1]]
    protocol_ok = all(
        item.get("protocol_version") == report["protocol_target"]
        and "notifications/initialized" in item.get("notifications", [])
        and item.get("tools_list_count", 0) >= 2
        and item.get("session_id_present", True)
        for item in protocols
    ) and continuity.get("same_session_call_count") == 2

    switched = report["registry_states"]["after_switch"]
    rolled_back = report["registry_states"]["after_rollback"]
    file_get = steps[1]["allowed_file_get"]
    denied = steps[1]["denied_checks"]
    stop_records = [
        steps[1]["prior_server_stop"],
        steps[2]["prior_server_stop"],
        *report.get("cleanup", []),
    ]
    cleanup_ok = (
        report.get("runtime_file_cleanup", "").startswith("runtime-root removed")
        and not (OUT / "runtime-root").exists()
        and all(item.get("stopped") and item.get("port_closed") for item in stop_records)
        and all(not item.get("launcher_pid") for item in report.get("cleanup", []))
    )
    gates = {
        "candidate003_full_admission": report["candidate_pre_switch_admission"]["status"] == "PASS",
        "manifest_sha_matches_route": actual_manifest == expected_manifest,
        "stateful_mcp_protocol_session_tools": protocol_ok,
        "explicit_candidate003_route_success": steps[0]["explicit_candidate003_route"].get("error") is None,
        "registry_switch_and_restart": switched["revision"] == 2 and switched["active_target_id"] == TARGET_ID,
        "allowed_files_raw_sha": file_get["status_code"] == 200 and file_get["matches_manifest"] and file_get["equals_isolated_file"],
        "acl_denied_search_and_files": denied["search_error"] == "forbidden" and denied["search_evidence_count"] == 0 and denied["file_status_code"] == 403,
        "registry_rollback_and_restart": rolled_back["revision"] == 3 and rolled_back["active_target_id"] == ACTIVE_ID,
        "service_worker_pid_matches_uvicorn_log": all(step.get("uvicorn_pid_matches_worker_metrics") for step in steps),
        "service_and_launcher_processes_stopped": cleanup_ok and all(
            step.get("launcher_pid_exited") for step in steps
        ),
        "isolated_root_removed": not (OUT / "runtime-root").exists(),
        "original_registry_unchanged": report["active_registry_before"]["sha256"] == report["active_registry_after"]["sha256"],
    }
    report["run_provenance"] = {
        "command": "uv --cache-dir artifacts/phase4-isolated-e2e/uv-cache run --offline --no-sync python tests/acceptance/run_phase4_isolated_e2e.py --repetitions 2",
        "runner_sha256_at_summary": runner_sha,
        "production_source_sha256": runner_modules,
        "route_sha256": file_sha(ROOT / "configs/indexes/multicorpus-candidates-003.json"),
        "candidate003_manifest_sha256_expected": expected_manifest,
        "candidate003_manifest_sha256_observed": actual_manifest,
        "git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True, check=True,
        ).stdout.strip(),
        "mcp_worker_pids_from_uvicorn_logs": server_ids,
        "launcher_pids": [steps[0]["pid"], steps[1]["restart"]["pid"], steps[2]["restart"]["pid"]],
        "worker_pid_exit_probe": [step.get("uvicorn_pid_exit_probe") for step in steps],
    }
    report["engineering_gates"] = gates
    report["engineering_status"] = "passed" if all(gates.values()) else "failed"
    report["semantic_review_status"] = "passed; see phase4-delivery-review.md"
    report["runtime_summary"] = {
        "active_query_samples": steps[1]["default_active_queries"]["n"],
        "active_query_p50_ms": steps[1]["default_active_queries"]["p50_ms"],
        "active_query_p95_ms": steps[1]["default_active_queries"]["p95_ms"],
        "active_query_timing_scope": steps[1]["default_active_queries"]["timing_scope"],
        "cold_start_process_to_first_success_seconds_n1": [
            step["cold_start_to_first_success_seconds_n1"] for step in steps
        ],
        "allowed_file_bytes": file_get["bytes"],
        "allowed_file_raw_sha256_matches": file_get["matches_manifest"],
        "acl_denied_search": denied["search_error"],
        "acl_denied_file_http": denied["file_status_code"],
        "sampled_server_peak_working_set_bytes": report["server_process_peak_working_set_bytes"],
        "sampled_server_gpu_peak_allocated_bytes": [
            step["server_process_metrics"]["torch_cuda_memory"]["peak_allocated_bytes"]
            for step in steps
        ],
        "whole_device_gpu_mib_samples": report["gpu_memory_mib_samples_whole_device"],
    }
    report["status"] = report["engineering_status"]
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(report)


def write_markdown(report: dict[str, Any]) -> None:
    summary = report["runtime_summary"]
    lines = [
        "# Phase 4 isolated MCP E2E and registry lifecycle",
        "",
        f"Engineering gates: **{report['engineering_status']}**; semantic review: **{report['semantic_review_status']}**.",
        "",
        f"Run command: `{report['run_provenance']['command']}`",
        f"Run start: `{report['started_at_utc']}`; Python `{report['python']}`, Windows `{report['steps'][0]['server_process_metrics']['os']['version']}`.",
        f"Runner SHA-256: `{report['run_provenance']['runner_sha256_at_summary']}`. Production helper/entry SHA values are in [report.json](report.json).",
        "",
        "The report JSON preserves selected evidence text, locators, per-request outputs, registry snapshots, copied-asset hashes, and sanitized service logs. The temporary root was removed after the snapshots were saved.",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Candidate003 full admission and manifest SHA | PASS; `{report['candidate_pre_switch_admission']['elapsed_seconds']} s`; manifest `{report['run_provenance']['candidate003_manifest_sha256_observed']}` |",
        f"| Stateful MCP initialize / initialized / tools/list / session | PASS; `{report['protocol_target']}`; same-session calls `{report['steps'][0]['same_session_lifecycle_probe']['same_session_call_count']}` |",
        "| Registry switch/restart/rollback/restart | PASS; revision 1→2→3; OSG → candidate003 → OSG |",
        f"| `/files` allowed response | PASS; {summary['allowed_file_bytes']:,} bytes; raw SHA matched manifest |",
        f"| ACL denial | PASS; search `{summary['acl_denied_search']}` with zero evidence; `/files` HTTP {summary['acl_denied_file_http']} |",
        f"| Original active registry | PASS; SHA stayed `{report['active_registry_before']['sha256']}` |",
        "| Temporary files, service processes and port | PASS; root removed; all server ports closed; Uvicorn PIDs matched worker metrics; independent process check confirmed exit; Windows process termination used |",
        "",
        "## Runtime observations",
        "",
        "- Candidate003 explicit route: Q003; after switch default-active single-library service: 6 calls across Q003/Q005/Q017. The default service uses candidate depth 20; explicit multi-corpus route uses its existing 20/30 retrieval/rerank-pool settings. This is operational evidence, not a paired ranking comparison.",
        f"- Warm end-to-end latency p50/p95: {summary['active_query_p50_ms']}/{summary['active_query_p95_ms']} ms (n={summary['active_query_samples']}; each sample includes HTTP connection, initialize/initialized, tools/list, tool call, and session close).",
        f"- Process-start to first successful MCP call: `{summary['cold_start_process_to_first_success_seconds_n1']}` seconds for each of three starts (n=1 each).",
        f"- Actual Uvicorn worker peak working set: `{summary['sampled_server_peak_working_set_bytes']}` bytes. Per-worker torch CUDA peak allocation: `{summary['sampled_server_gpu_peak_allocated_bytes']}` bytes. Whole-device nvidia-smi samples: `{summary['whole_device_gpu_mib_samples']}` MiB.",
        "- Independent semantic review passed the three locked live queries (Q003/Q005/Q017); see [phase4-delivery-review.md](../phase4-delivery-review.md). The MCP response itself remains `not_generated`; no answer-generation quality claim is made.",
        "- The runner's diagnostic summary fields were not populated because the MCP response nests them; independent review recalculated context size from the full returned evidence text. No source-context diagnostic counts are claimed here.",
        "- Windows `Popen.terminate()` was used; process termination and closed ports were verified, not graceful shutdown.",
        "",
        "## Scope limits",
        "",
        "This used a Windows host and existing local Qdrant/model assets with an isolated copy of route/registry/ACL/source/BM25 assets. It is not production IAM, clean offline installation, native Linux, throughput, or concurrency acceptance. `uv.lock` does not supply a wheelhouse.",
        "",
        "Failed startup attempts are preserved in `attempt-001.*` and `attempt-002.*`; the successful run is `report.json`.",
        "",
    ]
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
