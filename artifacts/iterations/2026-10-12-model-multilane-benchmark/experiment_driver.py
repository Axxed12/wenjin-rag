"""Freeze first, run one isolated lane setting, always clean its own resources."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPO = BASE.parents[2]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


helper = load_module("multilane_baseline_helper", BASE / "run_model_multilane_benchmark.py")


def source_hash(root):
    digest = hashlib.sha256()
    for p in sorted(Path(root).rglob("*.py")):
        digest.update(p.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(p.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare(args):
    helper.ARTIFACT_ROOT = BASE
    # Baseline helper prepares only isolated files and validates Compose; no services.
    state = helper.prepare(argparse.Namespace(embedding_path="E:/Models/Qwen3-Embedding-0.6B",
                                             reranker_path="E:/Models/Qwen3-Reranker-0.6B"))
    out = Path(state["evidence_dir"])
    frozen = out / "frozen"
    frozen.mkdir()
    harness = frozen / "harness"
    harness.mkdir()
    for p in BASE.glob("*.py"):
        shutil.copy2(p, harness / p.name)
    src = frozen / "src"
    shutil.copytree(BASE / "experimental-source", src,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    original = frozen / "formal-src"
    shutil.copytree(REPO / "src", original, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    config = frozen / "config"
    config.mkdir()
    for name in ("compose.base.yaml", "compose.local.yaml"):
        shutil.copy2(REPO / "deploy" / name, config / name)
    for name in ("uv.lock", "pyproject.toml"):
        shutil.copy2(REPO / name, frozen / name)
    env_file = Path(state["env_file"])
    waiters = 1 if args.phase == "boundary" else 8
    wait_seconds = .2 if args.phase == "boundary" else 30
    with env_file.open("a", encoding="utf-8") as stream:
        stream.write(f'WENJIN_MODEL_API_MAX_WAITING="{waiters}"\nWENJIN_MODEL_API_MAX_WAIT_SECONDS="{wait_seconds}"\n')
        stream.write('WENJIN_MODEL_API_TIMEOUT_SECONDS="90"\n')
        stream.write('WENJIN_FILE_MANAGEMENT_READ_TIMEOUT_SECONDS="210"\n')
    # This env file has only configuration and secret file paths, no secret values.
    shutil.copy2(env_file, config / "environment.paths.env")
    trace = out / "model-events.jsonl"
    trace.write_text("", encoding="utf-8")
    overlay = Path(state["overlay_file"])
    text = overlay.read_text(encoding="utf-8").replace((REPO / "src").as_posix(), src.as_posix())
    model_marker = "  model-node:\n"
    before, model_section = text.split(model_marker, 1)
    backend_source_sha256 = None
    if args.phase == "mcp":
        backend_src = frozen / "backend-src"
        shutil.copytree(src, backend_src)
        provider = backend_src / "wenjin/models/providers.py"
        old_provider = provider.read_text(encoding="utf-8")
        new_provider = old_provider.replace("import math\n", "import math\nimport os\n", 1)
        new_provider = new_provider.replace('            admission = identity["admission"]\n', '''            admission = identity["admission"]
            # 仅隔离benchmark client允许已明确绑定的实验lane身份。
            expected_lanes = int(os.environ.get("WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES", "1"))
            if expected_lanes not in {1, 2, 4}:
                raise ValueError
            if expected_lanes != 1 and identity.get("experiment_adapter", {}).get("lane_count") != expected_lanes:
                raise ValueError
''', 1).replace('or admission["active"] != 1', 'or admission["active"] != expected_lanes', 1)
        if old_provider == new_provider or 'or admission["active"] != expected_lanes' not in new_provider:
            raise RuntimeError("experiment_client_patch_shape_mismatch")
        provider.write_text(new_provider, encoding="utf-8")
        (frozen / "backend-client-diff.patch").write_text("".join(difflib.unified_diff(old_provider.splitlines(keepends=True), new_provider.splitlines(keepends=True), fromfile="model-source/providers.py", tofile="benchmark-client/providers.py")), encoding="utf-8")
        before = before.replace(src.as_posix(), backend_src.as_posix())
        before = before.replace("    environment:\n", f'    environment:\n      WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES: "{args.lanes}"\n')
        backend_source_sha256 = source_hash(backend_src)
    model_section = model_section.replace("    environment:\n", f'''      - type: bind
        source: {trace.as_posix()}
        target: /experiment/trace.jsonl
    environment:
      WENJIN_MODEL_API_EXPERIMENT_ENABLE: "1"
      WENJIN_MODEL_API_EXPERIMENT_LANES: "{args.lanes}"
      WENJIN_MODEL_TRACE_PATH: /experiment/trace.jsonl
      WENJIN_MODEL_API_EXPERIMENT_BOUNDARY_PROBE: "{1 if args.phase == 'boundary' else 0}"
      CUDA_VISIBLE_DEVICES: "0"
''', 1)
    model_section = model_section.replace("    image:", "    stop_grace_period: 120s\n    image:", 1)
    final_overlay = config / "overlay.yaml"
    final_overlay.write_text(before + model_marker + model_section, encoding="utf-8")
    overlay.unlink()
    state.update({"overlay_file": str(final_overlay), "lane_count": args.lanes,
                  "phase": args.phase, "rounds": args.rounds,
                  "reference_path": args.reference, "trace_file": str(trace),
                  "model_base_url": f"http://127.0.0.1:{state['ports']['model']}",
                  "frozen_root": str(frozen), "source_sha256": source_hash(src),
                  "formal_source_sha256": source_hash(original),
                  "backend_source_sha256": backend_source_sha256,
                  "compose_files": [str(config / "compose.base.yaml"), str(config / "compose.local.yaml"),
                                    str(final_overlay)]})
    state_path = Path(state["state_file"])
    helper.write_json(state_path, state)
    diff_parts = []
    files = sorted({p.relative_to(src) for p in src.rglob("*.py")} |
                   {p.relative_to(original) for p in original.rglob("*.py")})
    for name in files:
        old = (original / name).read_text(encoding="utf-8").splitlines(keepends=True) if (original / name).exists() else []
        new = (src / name).read_text(encoding="utf-8").splitlines(keepends=True) if (src / name).exists() else []
        diff_parts.extend(difflib.unified_diff(old, new, fromfile=f"formal/{name.as_posix()}",
                                             tofile=f"experimental/{name.as_posix()}"))
    (frozen / "source-diff.patch").write_text("".join(diff_parts), encoding="utf-8")
    compose = compose_args(state)
    helper.run_command(compose + ["config", "--quiet"])
    resolved = helper.run_command(compose + ["config", "--format", "json"])
    (config / "resolved-compose.json").write_text(resolved, encoding="utf-8")
    hashes = {p.relative_to(frozen).as_posix(): helper.sha256_file(p)
              for p in frozen.rglob("*") if p.is_file()}
    identity = {"run_id": state["run_id"], "lanes": args.lanes, "phase": args.phase,
                "client_inflight": 4, "rounds": args.rounds, "files_sha256": hashes,
                "source_sha256": state["source_sha256"], "formal_source_sha256": state["formal_source_sha256"],
                "image_ids": {name: helper.docker_image_id(name) for name in
                              (helper.MODEL_IMAGE, helper.BACKEND_IMAGE, "qdrant/qdrant:v1.19.1")},
                "created_before_service_start": True, "weights_read_only": True,
                "one_embedding_one_reranker": True, "reference_source": args.reference}
    if args.reference:
        identity["reference_sha256"] = helper.sha256_file(Path(args.reference))
        shutil.copy2(args.reference, frozen / "reference.json")
        state["reference_path"] = str(frozen / "reference.json")
        helper.write_json(state_path, state)
        identity["files_sha256"]["reference.json"] = helper.sha256_file(frozen / "reference.json")
    identity["state_sha256"] = helper.sha256_file(state_path)
    identity["environment_paths_sha256"] = helper.sha256_file(env_file)
    gpu_rows = helper.run_command(["nvidia-smi", "--query-gpu=index,uuid,name", "--format=csv,noheader"])
    if len(gpu_rows.splitlines()) != 1:
        raise RuntimeError("experiment_requires_one_host_gpu")
    identity["host_gpu_identity"] = gpu_rows
    helper.write_json(out / "freeze.json", identity)
    helper.write_json(out / "effective-resource-thresholds.json", {"start_gpu_free_mib": 8192, "start_host_free_mib": 6144, "stop_gpu_free_mib": 1024, "stop_host_free_mib": 3072, "container_restart_count_stop": 2, "gpu_host_nominal_sampling_seconds": .5, "docker_nominal_sampling_seconds": 2})
    print(json.dumps({"state": str(state_path), "run": state["run_id"], "phase": args.phase,
                      "lanes": args.lanes, "source_sha256": state["source_sha256"]}))


def compose_args(state):
    args = ["docker", "compose", "--project-name", state["project"], "--project-directory", str(REPO),
            "--env-file", state["env_file"]]
    for path in state["compose_files"]:
        args.extend(["-f", path])
    return args


class ResourceSampler:
    def __init__(self, state, out):
        self.state, self.out = state, out
        self.done = threading.Event()
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.threads = []

    def loop(self, kind, interval):
        while not self.done.is_set():
            row = {"channel": kind, "time_utc": helper.utc_now(), "monotonic": time.monotonic()}
            try:
                if kind == "gpu-host":
                    row.update(helper.resource_snapshot())
                    gpu = row.get("gpu")
                    free = row.get("host_memory_free_mib")
                    if gpu is None or free is None:
                        self.stop_reason = "resource_metrics_unavailable"
                    elif gpu["free_mib"] < 1024 or free < 3072:
                        self.stop_reason = "gpu_or_host_memory_threshold"
                else:
                    ids = helper.run_command(["docker", "ps", "-q", "--filter",
                                              f"label=com.docker.compose.project={self.state['project']}"], timeout=8)
                    if ids:
                        raw = helper.run_command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids.split()], timeout=8)
                        row["containers"] = [json.loads(x) for x in raw.splitlines()]
                        oom = helper.run_command(["docker", "inspect", "--format", "{{.State.OOMKilled}}", *ids.split()], timeout=8)
                        if "true" in oom.splitlines():
                            self.stop_reason = "container_oom_killed"
                        states = helper.run_command(["docker", "inspect", "--format", "{{.RestartCount}} {{.State.Status}}", *ids.split()], timeout=8)
                        if any(int(line.split()[0]) >= 2 for line in states.splitlines() if line.strip()):
                            self.stop_reason = "container_repeated_restart"
                if self.stop_reason:
                    self.stop_event.set()
                    row["stop_reason"] = self.stop_reason
            except Exception as exc:
                row["sampling_error"] = type(exc).__name__
                self.stop_reason = "resource_sampler_error"
                self.stop_event.set()
            helper.append_jsonl(self.out / f"resources-{kind}.jsonl", row)
            self.done.wait(interval)

    def start(self):
        for kind, interval in (("gpu-host", .5), ("docker", 2)):
            thread = threading.Thread(target=self.loop, args=(kind, interval), daemon=True)
            self.threads.append(thread)
            thread.start()

    def stop(self):
        self.done.set()
        for thread in self.threads:
            thread.join(timeout=30)
        if any(thread.is_alive() for thread in self.threads):
            self.stop_reason = "resource_sampler_thread_did_not_stop"
            self.stop_event.set()


def run(state_path):
    global helper
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    out = Path(state["evidence_dir"])
    frozen = Path(state["frozen_root"])
    freeze = json.loads((out / "freeze.json").read_text())
    runtime = Path(state["runtime_root"]).resolve()
    expected_parent = (Path(__import__("tempfile").gettempdir()) / "wenjin-concurrency-baseline").resolve()
    expected_compose = [frozen / "config" / p for p in ("compose.base.yaml", "compose.local.yaml", "overlay.yaml")]
    if (not re.fullmatch(r"wenjin-concurrency-[0-9a-f]{8}", state["project"])
        or runtime != expected_parent / state["project"]
        or out.resolve() != (BASE / "runs" / state["run_id"]).resolve()
        or frozen.resolve() != (out / "frozen").resolve()
        or Path(state["env_file"]).resolve() != runtime / ".env.concurrency"
        or Path(state["model_secret_path"]).resolve() != runtime / "secrets/model_api_token"
        or Path(state["admin_secret_path"]).resolve() != runtime / "secrets/file_admin_token"
        or state["model_base_url"] != f"http://127.0.0.1:{state['ports']['model']}"
        or [Path(p).resolve() for p in state["compose_files"]] != expected_compose
        or helper.sha256_file(Path(state_path)) != freeze["state_sha256"]
        or helper.sha256_file(Path(state["env_file"])) != freeze["environment_paths_sha256"]):
        raise RuntimeError("frozen_state_or_paths_mismatch")
    for name, digest in freeze["files_sha256"].items():
        if helper.sha256_file(frozen / name) != digest:
            raise RuntimeError("frozen_file_changed_before_run")
    for name, digest in freeze["image_ids"].items():
        if helper.docker_image_id(name) != digest:
            raise RuntimeError("frozen_image_changed_before_run")
    if helper.sha256_file(Path(__file__).resolve()) != freeze["files_sha256"]["harness/experiment_driver.py"]:
        raise RuntimeError("driver_changed_since_freeze")
    helper = load_module("frozen_baseline_helper", frozen / "harness/run_model_multilane_benchmark.py")
    helper.REPO = REPO
    helper.ARTIFACT_ROOT = BASE
    compose = compose_args(state)
    helper.compose_args = compose_args
    token = Path(state["model_secret_path"]).read_text().strip()
    sampler = ResourceSampler(state, out)
    outcome = {"passed": False, "phase": state["phase"], "lane_count": state["lane_count"]}
    started = time.perf_counter()
    try:
        resources = helper.resource_snapshot()
        if resources["gpu"]["free_mib"] < 8192 or resources["host_memory_free_mib"] < 6144:
            raise RuntimeError("resource_start_gate_rejected")
        sampler.start()
        helper.run_command(compose + ["up", "-d", "--no-build", "--pull", "never", "qdrant", "model-node"], timeout=180)
        model_ids = helper.run_command(compose + ["ps", "-q", "model-node"], timeout=10).splitlines()
        if len(model_ids) != 1:
            raise RuntimeError("model_container_not_singleton")
        model_image_id = helper.run_command(["docker", "inspect", "--format", "{{.Image}}", model_ids[0]], timeout=10)
        if model_image_id != freeze["image_ids"][helper.MODEL_IMAGE]:
            raise RuntimeError("model_container_image_mismatch")
        outcome["model_container"] = {"id": model_ids[0], "image": model_image_id, "count": 1}
        helper.wait_http(state["model_base_url"] + "/v1/health", headers={"Authorization": "Bearer " + token},
                         timeout_seconds=600, expect_json=True, stop_event=sampler.stop_event)
        outcome["container_to_ready_seconds"] = time.perf_counter() - started
        identity = helper.wait_http(state["model_base_url"] + "/v1/identity",
                                    headers={"Authorization": "Bearer " + token}, expect_json=True)
        helper.write_json(out / "model-identity.json", identity)
        outcome["model_attestation"] = helper.attest_loaded_source(compose, "model-node", state["source_sha256"])
        if sampler.stop_event.is_set():
            raise RuntimeError("resource_stop_before_workload")
        if state["phase"] == "model":
            stage = load_module("frozen_model_http_stage", frozen / "harness/model_http_stage.py")
            reference = json.loads(Path(state["reference_path"]).read_text()) if state["reference_path"] else None
            outcome["model"] = stage.run_model_http_stage(state, out, model_token=token,
                                                         lane_count=state["lane_count"], reference=reference,
                                                         rounds=state["rounds"], stop_event=sampler.stop_event)
            outcome["passed"] = bool(outcome["model"].get("stage_pass"))
        elif state["phase"] == "mcp":
            # fixture构建也使用冻结的隔离client，不加载主工作树实现。
            sys.path.insert(0, str(frozen / "backend-src"))
            __import__("os").environ["WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES"] = str(state["lane_count"])
            stage = load_module("frozen_mcp_stage", frozen / "harness/mcp_stage.py")
            outcome["mcp"] = stage.run_mcp_stage(state, out, helper, sampler.stop_event,
                                                  model_token=token, repetitions=state["rounds"])
            outcome["passed"] = bool(outcome["mcp"].get("passed"))
            outcome["backend_attestation"] = [helper.attest_loaded_source(compose, name, state["backend_source_sha256"])
                                               for name in ("management", "mcp")]
        else:
            stage = load_module("frozen_boundary_stage", frozen / "harness/boundary_http_stage.py")
            outcome["boundary"] = stage.run_boundary_stage(state, out, model_token=token,
                                                           lane_count=state["lane_count"], stop_event=sampler.stop_event)
            outcome["passed"] = bool(outcome["boundary"].get("passed"))
    except BaseException as exc:
        outcome["failure_type"] = type(exc).__name__
        outcome["failure_safe_code"] = str(exc) if isinstance(exc, RuntimeError) else "details_in_raw_artifacts"
    finally:
        sampler.stop()
        outcome["resource_stop_reason"] = sampler.stop_reason
        try:
            # Stop gracefully first: lifecycle traces include post-work model state fingerprint.
            helper.run_command(compose + ["stop", "-t", "120"], timeout=150)
        except Exception:
            outcome["graceful_stop_failed"] = True
            outcome["passed"] = False
        try:
            helper.redact_logs_and_remove_secrets(state, out, token)
        except Exception:
            outcome["log_collection_failed"] = True
        try:
            helper.run_command(compose + ["down", "--volumes", "--remove-orphans"], timeout=180)
            remaining = helper.verify_no_project_resources(state["project"])
            if any(remaining.get(key) != 0 for key in ("containers", "volumes", "networks")):
                helper.write_json(out / "cleanup.json", remaining)
                raise RuntimeError("cleanup_resources_remaining_or_unknown")
            runtime = Path(state["runtime_root"]).resolve()
            expected_parent = (Path(__import__("tempfile").gettempdir()) / "wenjin-concurrency-baseline").resolve()
            if runtime != expected_parent / state["project"]:
                raise RuntimeError("unsafe_cleanup_path")
            shutil.rmtree(runtime)
            Path(state_path).unlink(missing_ok=True)
            remaining["temporary_root_removed"] = not runtime.exists()
            helper.write_json(out / "cleanup.json", remaining)
        except Exception as exc:
            outcome["cleanup_error"] = type(exc).__name__
        outcome["formal_source_unchanged"] = source_hash(REPO / "src") == state["formal_source_sha256"]
        outcome["frozen_files_unchanged"] = all(helper.sha256_file(frozen / name) == digest
                                                 for name, digest in freeze["files_sha256"].items())
        try:
            events = [json.loads(line) for line in Path(state["trace_file"]).read_text().splitlines() if line.strip()]
            states = [event for event in events if event.get("event") == "model_state_check"]
            outcome["model_state_unchanged_after_drain"] = bool(states) and all(e.get("unchanged") is True for e in states)
        except Exception:
            outcome["model_state_unchanged_after_drain"] = False
        outcome["passed"] = bool(outcome["passed"] and outcome["formal_source_unchanged"]
                                  and outcome["frozen_files_unchanged"] and outcome["model_state_unchanged_after_drain"]
                                  and not sampler.stop_reason and not outcome.get("cleanup_error"))
        helper.write_json(out / "outcome.json", outcome)
        print(json.dumps({key: outcome.get(key) for key in ("phase", "lane_count", "passed", "failure_type",
                                                           "failure_safe_code", "cleanup_error",
                                                           "resource_stop_reason", "formal_source_unchanged")}))
    return 0 if outcome["passed"] and not outcome.get("cleanup_error") else 1


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--lanes", type=int, choices=[1, 2, 4], required=True)
    p.add_argument("--phase", choices=["model", "mcp", "boundary"], default="model")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--reference")
    r = sub.add_parser("run")
    r.add_argument("--state", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args)
        return 0
    return run(args.state)


if __name__ == "__main__":
    raise SystemExit(main())
