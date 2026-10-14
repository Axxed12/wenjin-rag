from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import time
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

REPO = Path(r"E:\日常任务\rag-project")
ITERATION = REPO / "artifacts/iterations/2026-10-12-model-admission"
FREEZE = ITERATION / "frozen-source-final"
SOURCE = FREEZE / "src"
EXPECTED_SOURCE_SHA256 = "c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674"
RUNNER_PATH = Path(__file__).resolve()
EMBEDDING_PATH = Path(r"E:\Models\Qwen3-Embedding-0.6B")
RERANKER_PATH = Path(r"E:\Models\Qwen3-Reranker-0.6B")
BASELINE_PATH = ITERATION / "run_concurrency_baseline.frozen.py"


def source_hash(root: Path = SOURCE) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_baseline():
    spec = importlib.util.spec_from_file_location("frozen_concurrency_baseline", BASELINE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("frozen_baseline_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.ARTIFACT_ROOT = ITERATION
    module.source_tree_hash = source_hash
    module.HTTP_TIMEOUT_SECONDS = 90
    original_overlay = module.create_overlay

    def frozen_overlay(path, ports, runtime_root):
        original_overlay(path, ports, runtime_root)
        content = path.read_text(encoding="utf-8")
        live_source = (REPO / "src").as_posix()
        content = content.replace(live_source, SOURCE.as_posix())
        path.write_text(content, encoding="utf-8")

    module.create_overlay = frozen_overlay
    compose_dir = FREEZE / "deploy"

    def frozen_compose_args(state):
        return [
            "docker", "compose", "--project-directory", str(REPO),
            "--env-file", str(Path(state["env_file"])), "-p", state["project"],
            "-f", str(compose_dir / "compose.base.yaml"),
            "-f", str(compose_dir / "compose.local.yaml"),
            "-f", str(state["overlay_file"]),
        ]

    module.compose_args = frozen_compose_args
    return module


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def main() -> int:
    if source_hash() != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("frozen_source_hash_mismatch_before_prepare")
    baseline = load_baseline()
    state = baseline.prepare(Namespace(embedding_path=EMBEDDING_PATH, reranker_path=RERANKER_PATH))
    output = Path(state["evidence_dir"])
    env_file = Path(state["env_file"])
    with env_file.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write('WENJIN_MODEL_API_TIMEOUT_SECONDS="90"\n')
        stream.write('WENJIN_MODEL_API_MAX_WAITING="8"\n')
        stream.write('WENJIN_MODEL_API_MAX_WAIT_SECONDS="30"\n')
        stream.write('WENJIN_FILE_MANAGEMENT_READ_TIMEOUT_SECONDS="210"\n')
    compose = baseline.compose_args(state)
    sampler = baseline.Sampler(state, output / "resources.jsonl")
    stop_event = threading.Event()
    monitor = threading.Thread(target=lambda: baseline.monitor_stop(sampler, stop_event), daemon=True)
    benchmark = None
    admin = None
    reader = None
    reader_b = None
    admin_password = "A9!" + baseline.uuid.uuid4().hex + "Z"
    model_token = Path(state["model_secret_path"]).read_text(encoding="ascii").strip()
    admin_token = Path(state["admin_secret_path"]).read_text(encoding="ascii").strip()
    outcome = {
        "run_id": state["run_id"], "scenario": "targeted_model_admission_postfix",
        "passed": False, "source_tree_sha256": EXPECTED_SOURCE_SHA256,
        "runner_sha256": baseline.sha256_file(RUNNER_PATH), "scenarios": {},
    }
    cleanup = {"attempted": False}
    try:
        sampler.start()
        monitor.start()
        baseline.bootstrap_isolated_identity_db(
            Path(state["runtime_root"]) / "identity" / "identity.sqlite", admin_password
        )
        baseline.run_command(
            compose + ["up", "-d", "--no-build", "--pull", "never", "qdrant", "model-node"],
            timeout=180,
        )
        model_identity = baseline.wait_http(
            f"http://127.0.0.1:{state['ports']['model']}/v1/health",
            headers={"Authorization": f"Bearer {model_token}"}, expect_json=True,
            timeout_seconds=900, stop_event=stop_event,
        )
        del model_identity
        model_identity = baseline.wait_http(
            f"http://127.0.0.1:{state['ports']['model']}/v1/identity",
            headers={"Authorization": f"Bearer {model_token}"}, expect_json=True,
            timeout_seconds=15, stop_event=stop_event,
        )
        admission = model_identity.get("admission", {})
        if admission != {"active": 1, "max_waiting": 8, "max_wait_seconds": 30.0}:
            raise RuntimeError("model_admission_identity_mismatch")
        if model_identity.get("embedding", {}).get("model_id") != baseline.EMBEDDING_ID:
            raise RuntimeError("embedding_model_identity_mismatch")
        if model_identity.get("reranker", {}).get("model_id") != baseline.RERANKER_ID:
            raise RuntimeError("reranker_model_identity_mismatch")
        shared_fixture = baseline.seed_shared_fixture(state, model_token)
        if stop_event.is_set() or sampler.stop_reason:
            raise RuntimeError("resource_guard_stopped_after_shared_fixture")
        baseline.run_command(
            compose + ["up", "-d", "--no-build", "--pull", "never", "management", "mcp"],
            timeout=180,
        )
        admin_base = f"http://127.0.0.1:{state['ports']['admin']}"
        baseline.wait_http(admin_base + "/api/health", timeout_seconds=180, stop_event=stop_event)
        benchmark = baseline.Benchmark(state, output, sampler, stop_event)
        admin, admin_headers = benchmark.admin_login(admin_base, admin_password)
        reader = benchmark.create_managed_reader(admin, admin_headers)
        benchmark.reader_token = reader["mcp_token"]
        reader_b = benchmark.create_unprivileged_reader(admin, admin_headers)
        managed_fixture = benchmark.upload_managed(
            admin, admin_headers, reader, "synthetic_managed_admission_v1"
        )
        asyncio.run(benchmark.verify_default_deny(
            f"http://127.0.0.1:{state['ports']['mcp']}", reader["mcp_token"], managed_fixture["file_id"]
        ))
        managed_fixture["grant_results"] = benchmark.grant_resources(
            admin, admin_headers, reader, managed_fixture["file_id"], shared_fixture["library"]
        )
        identity_probe = asyncio.run(benchmark.verify_unprivileged_identity(
            f"http://127.0.0.1:{state['ports']['mcp']}", reader_b["mcp_token"],
            shared_fixture["target_id"], managed_fixture["file_id"],
            shared_fixture["marker"], managed_fixture["marker"],
        ))
        attestations = [baseline.attest_loaded_source(compose, service, EXPECTED_SOURCE_SHA256)
                        for service in ("management", "mcp", "model-node")]
        image_ids = {
            "backend": baseline.docker_image_id(baseline.BACKEND_IMAGE),
            "model_node": baseline.docker_image_id(baseline.MODEL_IMAGE),
        }
        manifest = {
            "run_id": state["run_id"], "project": state["project"], "started_utc": utc_now(),
            "source_tree_sha256": EXPECTED_SOURCE_SHA256,
            "runner_sha256": outcome["runner_sha256"], "compose_file_sha256": json.loads(
                (ITERATION / "final-source-freeze.json").read_text(encoding="utf-8")
            )["compose_files"],
            "images": image_ids, "runtime_source_attestation": attestations,
            "model_identity": {
                "embedding_model_id": model_identity["embedding"]["model_id"],
                "embedding_asset_sha256": model_identity["embedding"]["asset_sha256"],
                "reranker_model_id": model_identity["reranker"]["model_id"],
                "reranker_asset_sha256": model_identity["reranker"]["asset_sha256"],
                "implementation_sha256": model_identity["implementation_sha256"],
                "admission": admission,
            },
            "settings": {"model_read_seconds": 90, "queue_waiting": 8,
                         "queue_wait_seconds": 30, "management_bridge_read_seconds": 210},
            "weights_read_only": True, "one_gpu_model_node": True,
            "fixture": {"shared": shared_fixture,
                        "managed": {k: v for k, v in managed_fixture.items() if k != "mcp_token"}},
            "protocol": baseline.PROTOCOL,
        }
        write_json(output / "run-manifest.json", manifest)
        async def protocol_scenarios():
            shared = await benchmark.open_sessions(reader["mcp_token"], "target-shared", 1)
            managed = await benchmark.open_sessions(reader["mcp_token"], "target-managed", 1)
            shared_args = {"query": "synthetic concurrency evidence", "target_id": shared_fixture["target_id"], "top_k": 1}
            managed_args = {"query": "synthetic managed marker", "file_id": managed_fixture["file_id"], "top_k": 1}
            single_shared = await shared[0].call("tools/call", {"name": "search_docs", "arguments": shared_args})
            single_managed = await managed[0].call("tools/call", {"name": "search_docs", "arguments": managed_args})

            def check_single(label, response, marker):
                tool = response.get("tool", {})
                evidence = tool.get("evidence", [])
                if response.get("http_status") != 200 or tool.get("_is_error") or tool.get("error") or not evidence:
                    return {"passed": False, "http_status": response.get("http_status"),
                            "mcp_is_error": tool.get("_is_error"), "error": tool.get("error"),
                            "evidence_count": len(evidence)}
                marker_ok = all(marker in str(item.get("text", "")) for item in evidence)
                if label == "shared":
                    identity_ok = all(item.get("library") == shared_fixture["library"]
                                      and item.get("version") == shared_fixture["version"]
                                      and item.get("index_target_id") == shared_fixture["target_id"]
                                      for item in evidence)
                else:
                    ident = tool.get("managed_file", {})
                    identity_ok = (ident.get("file_id") == managed_fixture["file_id"]
                                  and ident.get("version_id") == managed_fixture["version_id"]
                                  and all(item.get("file_id") == managed_fixture["file_id"]
                                          and item.get("version_id") == managed_fixture["version_id"]
                                          for item in evidence))
                return {"passed": marker_ok and identity_ok, "http_status": response.get("http_status"),
                        "mcp_is_error": tool.get("_is_error"), "evidence_count": len(evidence),
                        "marker_match": marker_ok, "identity_match": identity_ok,
                        "request_id": tool.get("request_id"), "evidence_identity": [
                            {k: item.get(k) for k in ("library", "version", "index_target_id", "file_id", "version_id") if k in item}
                            for item in evidence]}

            shared_single = check_single("shared", single_shared, shared_fixture["marker"])
            managed_single = check_single("managed", single_managed, managed_fixture["marker"])
            collision = await benchmark.run_collision(shared[0], managed[0],
                                                      shared_args | {"_expected_marker": shared_fixture["marker"]},
                                                      managed_args | {"_expected_marker": managed_fixture["marker"],
                                                                       "_expected_version_id": managed_fixture["version_id"]})
            strict_collision = collision.get("passed") and all(
                item.get("success") and item.get("http_status") == 200
                and item.get("evidence_count", 0) > 0
                and not (item.get("mcp_rpc", {}).get("result", {}).get("isError", True))
                for item in collision.get("results", [])
            )

            wrong_version = await managed[0].call("tools/call", {
                "name": "search_docs", "arguments": managed_args | {"version": "__stale_version_probe__"}
            })
            wrong_tool = wrong_version.get("tool", {})
            version_probe = {
                "passed": wrong_version.get("http_status") == 200
                          and wrong_tool.get("error") == "invalid_target"
                          and not wrong_tool.get("evidence"),
                "http_status": wrong_version.get("http_status"),
                "mcp_is_error": wrong_tool.get("_is_error"),
                "error": wrong_tool.get("error"), "evidence_count": len(wrong_tool.get("evidence", [])),
            }

            ingest_marker = "synthetic_query_ingest_competition_v1"
            ingest_content = ("Synthetic ingest competition fixture.\n" +
                              ("Deterministic temporary retrieval content for model admission.\n" * 240) +
                              f"Unique marker: {ingest_marker}.\n").encode("utf-8")
            ingest_started = threading.Event()
            ingest_accepted = threading.Event()
            ingest_interval = {}

            def ingest_worker():
                ingest_started.set()
                ingest_interval["start_monotonic"] = time.monotonic()
                ingest_interval["started_utc"] = utc_now()
                upload = admin.post(
                    f"/api/files?library_id={quote(reader['library_id'], safe='')}",
                    headers=admin_headers | {"Content-Type": "application/octet-stream",
                                             "X-File-Name": "ingest-competition.txt"},
                    content=ingest_content,
                )
                if upload.status_code != 202:
                    raise RuntimeError(f"competition_upload_http_{upload.status_code}")
                ingest_interval["job_id"] = upload.json()["job_id"]
                ingest_interval["accepted_utc"] = utc_now()
                ingest_accepted.set()
                job = benchmark.wait_job(admin, ingest_interval["job_id"])
                ingest_interval["end_monotonic"] = time.monotonic()
                ingest_interval["status"] = job.get("status")
                ingest_interval["file_id"] = job.get("file_id")
                return job

            ingest_task = asyncio.create_task(asyncio.to_thread(ingest_worker))
            if not await asyncio.to_thread(ingest_started.wait, 10):
                raise RuntimeError("ingest_worker_start_timeout")
            if not await asyncio.to_thread(ingest_accepted.wait, 30):
                raise RuntimeError("ingest_job_accept_timeout")
            query_start = time.monotonic()
            query_start_utc = utc_now()
            query_response = await shared[0].call("tools/call", {
                "name": "search_docs", "arguments": shared_args,
            })
            query_end = time.monotonic()
            query_end_utc = utc_now()
            ingest_job = await ingest_task
            query_tool = query_response.get("tool", {})
            query_evidence = query_tool.get("evidence", [])
            ingest_file_id = ingest_interval.get("file_id")
            file_info_response = admin.get(f"/api/files/{quote(ingest_file_id, safe='')}")
            if file_info_response.status_code != 200:
                raise RuntimeError(f"ingest_file_info_http_{file_info_response.status_code}")
            ingest_version_id = file_info_response.json().get("active_version_id")
            if not ingest_file_id or not ingest_version_id:
                raise RuntimeError("ingest_file_or_active_version_missing")
            resources_response = admin.get("/api/admin/resources")
            if resources_response.status_code != 200:
                raise RuntimeError(f"ingest_resource_sync_http_{resources_response.status_code}")
            ingest_grant = admin.post(
                "/api/admin/grants", headers=admin_headers,
                json={"user_id": reader["user_id"], "resource_kind": "managed_file",
                      "resource_id": ingest_file_id, "allowed": True, "expected_revision": 0},
            )
            if ingest_grant.status_code != 200:
                raise RuntimeError(f"ingest_file_grant_http_{ingest_grant.status_code}")
            ingest_session = (await benchmark.open_sessions(
                reader["mcp_token"], "target-ingest-readback", 1
            ))[0]
            ingest_readback = await ingest_session.call("tools/call", {
                "name": "search_docs",
                "arguments": {"file_id": ingest_file_id,
                              "query": "synthetic query ingest marker", "top_k": 1},
            })
            ingest_tool = ingest_readback.get("tool", {})
            ingest_evidence = ingest_tool.get("evidence", [])
            ingest_identity = ingest_tool.get("managed_file", {})
            ingest_marker_match = any(
                ingest_marker in str(item.get("text", "")) for item in ingest_evidence
            )
            ingest_identity_match = (
                ingest_identity.get("file_id") == ingest_file_id
                and ingest_identity.get("version_id") == ingest_version_id
                and all(item.get("file_id") == ingest_file_id
                        and item.get("version_id") == ingest_version_id for item in ingest_evidence)
            )
            ingest_readback_result = {
                "passed": ingest_job.get("status") == "completed"
                          and ingest_readback.get("http_status") == 200
                          and not ingest_tool.get("_is_error") and not ingest_tool.get("error")
                          and ingest_marker_match and ingest_identity_match,
                "http_status": ingest_readback.get("http_status"),
                "mcp_is_error": ingest_tool.get("_is_error"),
                "error": ingest_tool.get("error"),
                "marker_match": ingest_marker_match,
                "identity_match": ingest_identity_match,
                "evidence_count": len(ingest_evidence),
                "file_id": ingest_file_id, "active_version_id": ingest_version_id,
                "tool_file_id": ingest_identity.get("file_id"),
                "tool_version_id": ingest_identity.get("version_id"),
                "evidence_identity": [
                    {key: item.get(key) for key in ("file_id", "version_id")}
                    for item in ingest_evidence
                ],
                "request_id": ingest_tool.get("request_id"),
            }
            query_ingest = {
                "passed": ingest_interval.get("status") == "completed"
                          and query_response.get("http_status") == 200
                          and not query_tool.get("_is_error") and not query_tool.get("error")
                          and any(shared_fixture["marker"] in str(item.get("text", "")) for item in query_evidence)
                          and ingest_readback_result["passed"],
                "ingest_status": ingest_interval.get("status"),
                "ingest_started_utc": ingest_interval.get("started_utc"),
                "ingest_accepted_utc": ingest_interval.get("accepted_utc"),
                "query_started_utc": query_start_utc,
                "query_ended_utc": query_end_utc,
                "query_http_status": query_response.get("http_status"),
                "query_mcp_is_error": query_tool.get("_is_error"),
                "query_evidence_count": len(query_evidence),
                "ingest_request_and_query_were_concurrently_scheduled": True,
                "interpretation": "client scheduling/HTTP operation overlap only; model API queue wait and actual inference competition were not observed",
                "model_queue_competition_observed": False,
                "model_queue_wait_seconds_observed": False,
                "ingest_job_file_id": ingest_file_id,
                "ingest_active_version_id": ingest_version_id,
                "ingest_content_sha256": hashlib.sha256(ingest_content).hexdigest(),
                "client_operation_interval_overlap_seconds": round(max(0.0, min(ingest_interval.get("end_monotonic", query_start), query_end)
                                                                 - max(ingest_interval.get("start_monotonic", query_start), query_start)), 6),
                "mcp_readback": ingest_readback_result,
            }
            managed_ingest_result = {
                "job_status": ingest_job.get("status"), "file_id": ingest_file_id,
                "active_version_id": ingest_version_id, "marker": ingest_marker,
                "mcp_readback_passed": ingest_readback_result["passed"],
            }
            for session in benchmark.sessions:
                await session.close()
            return {
                "single_shared": shared_single, "single_managed": managed_single,
                "mixed_collision": {"passed": bool(strict_collision),
                                    "results": collision.get("results"),
                                    "interpretation": "one simultaneous shared+managed pair; both must return positive correct evidence"},
                "stale_version": version_probe,
                "query_ingest": query_ingest,
                "query_ingest_fixture": managed_ingest_result,
            }

        scenarios = asyncio.run(protocol_scenarios())
        outcome["scenarios"] = scenarios
        outcome["identity_probe"] = identity_probe
        outcome["passed"] = (
            scenarios["single_shared"].get("passed") is True
            and scenarios["single_managed"].get("passed") is True
            and scenarios["mixed_collision"].get("passed") is True
            and scenarios["stale_version"].get("passed") is True
            and scenarios["query_ingest"].get("passed") is True
            and identity_probe.get("passed") is True
            and not sampler.stop_reason
        )
        write_json(output / "targeted-results.json", outcome)
        return 0 if outcome["passed"] else 2
    except Exception as exc:
        outcome["failure"] = {"error_category": type(exc).__name__, "message_omitted": True}
        write_json(output / "targeted-results.json", outcome)
        return 2
    finally:
        if admin is not None:
            admin.close()
        if benchmark is not None:
            try:
                asyncio.run(benchmark.close_sessions())
            except RuntimeError:
                pass
        if sampler.thread:
            sampler.stop()
        baseline.redact_logs_and_remove_secrets(
            state, output, model_token, admin_token,
            reader.get("mcp_token", "") if reader else "",
            reader_b.get("mcp_token", "") if reader_b else "",
            reader.get("reader_password", "") if reader else "",
            reader_b.get("reader_password", "") if reader_b else "",
            admin_password,
        )
        cleanup["attempted"] = True
        try:
            baseline.run_command(compose + ["down", "--volumes", "--remove-orphans", "--timeout", "30"], timeout=90)
            cleanup["compose_down"] = "completed"
        except Exception as exc:
            cleanup["compose_down"] = f"failed:{type(exc).__name__}"
        cleanup.update(baseline.verify_no_project_resources(state["project"]))
        if cleanup.get("compose_down") == "completed" and cleanup.get("resources_remaining") is False:
            try:
                import shutil
                temp_parent = (Path(tempfile.gettempdir()) / "wenjin-concurrency-baseline").resolve()
                runtime_root = Path(state["runtime_root"]).resolve()
                state_file = Path(state["state_file"]).resolve()
                overlay_file = Path(state["overlay_file"]).resolve()
                if (runtime_root.parent != temp_parent
                        or runtime_root.name != state["project"]
                        or not runtime_root.name.startswith("wenjin-concurrency-")
                        or state_file.parent != ITERATION.resolve()
                        or overlay_file.parent != ITERATION.resolve()
                        or not state_file.name.startswith("run-state.")
                        or not overlay_file.name.startswith("compose.concurrency.")):
                    raise RuntimeError("temporary_root_outside_expected_project_boundary")
                shutil.rmtree(runtime_root)
                cleanup["temporary_root_removed"] = True
                state_file.unlink(missing_ok=True)
                overlay_file.unlink(missing_ok=True)
                cleanup["credentials_removed"] = True
            except (OSError, RuntimeError) as exc:
                cleanup["temporary_root_removed"] = False
                cleanup["credentials_removed"] = False
                cleanup["temporary_cleanup_error"] = type(exc).__name__
        else:
            cleanup["temporary_root_removed"] = False
            cleanup["recovery_state_file"] = state["state_file"]
            cleanup["recovery_runtime_root"] = state["runtime_root"]
            cleanup["credentials_removed"] = False
        cleanup["source_hash_matches_freeze_after_run"] = source_hash() == EXPECTED_SOURCE_SHA256
        write_json(output / "cleanup.json", cleanup)
        outcome["cleanup"] = cleanup
        write_json(output / "targeted-results.json", outcome)


if __name__ == "__main__":
    raise SystemExit(main())
