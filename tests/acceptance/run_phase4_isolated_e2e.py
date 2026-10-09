"""在项目内临时根目录运行一次独立的stateful MCP + registry lifecycle验收。

执行方式：uv run python tests/acceptance/run_phase4_isolated_e2e.py
子进程复用项目的本地模型和Qdrant，但registry、ACL、BM25副本、manifest与原文均放在
artifacts/phase4-isolated-e2e/runtime-root；finally关闭进程并删除该临时root。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "phase4-isolated-e2e"
RUNTIME_ROOT = OUTPUT_DIR / "runtime-root"
ROUTE_RELATIVE = Path("configs/indexes/multicorpus-candidates-003.json")
REGISTRY_RELATIVE = Path("data/indexes/index-registry.json")
CLIENT_ID = "phase4-e2e-client"
MCP_PATH = "/mcp"
QUESTIONS_RELATIVE = Path(
    "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl"
)
QUESTIONS_SHA256 = "8c69891fddf0002210b9446150d35d4acc71217d58f401033ca0e2fef8595162"
TARGET_QUESTION_IDS = (
    "supplemental-3dtiles-longline-003",
    "supplemental-3dtiles-longline-005",
    "supplemental-3dtiles-longline-017",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def registry_snapshot(root: Path) -> dict[str, Any]:
    raw = (root / REGISTRY_RELATIVE).read_bytes()
    data = json.loads(raw)
    return {
        "sha256": sha256(raw),
        "revision": data["revision"],
        "active_target_id": data["active"]["target_id"],
        "history_target_ids": [x["target_id"] for x in data["history"]],
        "events": data.get("events", []),
    }


def copy_runtime_assets(root: Path) -> dict[str, Any]:
    """只复制route/registry所需文件；source目录依据manifest白名单逐文件复制。"""
    from wenjin.index_registry import IndexRegistryStore, IndexTarget, resolve_project_path

    route = json.loads((PROJECT_ROOT / ROUTE_RELATIVE).read_text(encoding="utf-8-sig"))
    active = IndexRegistryStore(PROJECT_ROOT / REGISTRY_RELATIVE).active()
    targets = [active, *(IndexTarget.from_dict(item) for item in route["candidates"])]
    unique = {target.target_id: target for target in targets}
    copied: dict[str, str] = {}
    trusted_raw_root = (PROJECT_ROOT / "raw_data").resolve()

    # route 和全部target config供启动准入及语料目录工具使用。
    route_path = root / ROUTE_RELATIVE
    route_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / ROUTE_RELATIVE, route_path)
    config_paths = set()
    for target in unique.values():
        matching = next((
            path for path in (PROJECT_ROOT / "configs/indexes").glob("*.json")
            if path.name != ROUTE_RELATIVE.name
            and json.loads(path.read_text(encoding="utf-8-sig")).get("target_id")
            == target.target_id
        ), None)
        if matching is None:
            raise FileNotFoundError(f"target config not found for {target.target_id}")
        config_paths.add(matching)
        manifest = resolve_project_path(PROJECT_ROOT, target.manifest_path)
        manifest_copy = resolve_project_path(root, target.manifest_path)
        manifest_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest, manifest_copy)
        copied[target.manifest_path] = sha256(manifest.read_bytes())
        parsed = json.loads(manifest.read_text(encoding="utf-8-sig"))
        subdir = str(parsed.get("raw_data_subdir", "")).strip("/\\")
        for entry in parsed.get("files", []):
            if not (entry.get("retrieval_eligible") or entry.get("indexable")):
                continue
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("manifest source path escapes its corpus")
            source = PROJECT_ROOT / "raw_data" / subdir / relative
            if not source.resolve().is_relative_to(trusted_raw_root):
                raise ValueError("manifest source resolves outside raw_data")
            destination = root / "raw_data" / subdir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied[destination.relative_to(root).as_posix()] = sha256(source.read_bytes())

    for config_path in config_paths:
        if not config_path.is_file():
            raise FileNotFoundError(f"target config not found: {config_path.name}")
        destination = root / "configs/indexes" / config_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config_path, destination)
        copied[destination.relative_to(root).as_posix()] = sha256(config_path.read_bytes())

    # 唯一索引资产：当前active、route五target和其BM25库。
    for target in unique.values():
        source = resolve_project_path(PROJECT_ROOT, target.bm25_path)
        destination = resolve_project_path(root, target.bm25_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied[target.bm25_path] = sha256(source.read_bytes())

    # source directories are discovered from the manifests; catalog only needs valid manifests
    # and target config references, all already copied above.
    return {
        "route_sha256": sha256((root / ROUTE_RELATIVE).read_bytes()),
        "copied_file_count": len(copied) + 1,
        "copied_bytes": sum((root / item).stat().st_size for item in copied),
        "copied_sha256": copied,
        "target_ids": sorted(unique),
    }


def initialize_isolated_registry(root: Path) -> dict[str, Any]:
    from wenjin.index_registry import IndexRegistryStore, IndexTarget

    active = IndexRegistryStore(PROJECT_ROOT / REGISTRY_RELATIVE).active()
    candidate = IndexTarget.from_dict(json.loads(
        (root / "configs/indexes/ogc-3dtiles-1.1-blocks-split-candidate-003.json")
        .read_text(encoding="utf-8-sig")
    ))
    store = IndexRegistryStore(root / REGISTRY_RELATIVE)
    store.initialize(active, reason="isolated phase4 lifecycle rehearsal")

    # 隔离策略：OSG公开，3D Tiles仅授权测试客户端；其他target默认拒绝。
    from wenjin.access import AccessPolicy, write_policy

    access_dir = root / "configs/access"
    write_policy(access_dir, AccessPolicy("osg-3.6.5", "public", ("*",), "isolated test"))
    write_policy(access_dir, AccessPolicy(
        "ogc-3dtiles-1.1", "internal", (CLIENT_ID,), "isolated test"
    ))
    return {"store": store, "candidate": candidate}


def server_worker(args: argparse.Namespace) -> None:
    """隔离服务进程入口；凭证只来自父进程内存环境，不写入文件或日志。"""
    from starlette.middleware import Middleware

    from wenjin.auth import BearerAuthMiddleware
    from wenjin.mcp_server import EXPECTED_PROTOCOL_VERSION, build_mcp_server

    if EXPECTED_PROTOCOL_VERSION != "2025-11-25":
        raise RuntimeError("unexpected MCP protocol version")
    api_key = os.environ["WENJIN_MCP_API_KEY"]
    metrics_path = Path(args.project_root) / "phase4-server-metrics.json"
    run_started = time.perf_counter()
    stop_metrics = threading.Event()

    def sample_metrics() -> None:
        # 只读取当前worker里已加载的Python/torch状态，不触发模型/CUDA初始化。
        metrics_file = Path(__file__).resolve().parents[1] / "support" / "phase4_process_metrics.py"
        spec = importlib.util.spec_from_file_location("phase4_process_metrics_worker", metrics_file)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        collect_process_metrics = module.collect_process_metrics

        while not stop_metrics.is_set():
            temporary = metrics_path.with_name(".phase4-server-metrics.tmp")
            temporary.write_text(json.dumps(
                collect_process_metrics(run_started), ensure_ascii=False
            ), encoding="utf-8")
            temporary.replace(metrics_path)
            stop_metrics.wait(0.5)

    threading.Thread(target=sample_metrics, daemon=True).start()
    app = build_mcp_server(project_root=Path(args.project_root), bearer_enabled=True)
    app.run(
        transport="streamable-http", show_banner=False,
        host="127.0.0.1", port=args.port, path=MCP_PATH,
        stateless_http=False, json_response=False,
        host_origin_protection=True,
        allowed_hosts=[f"127.0.0.1:{args.port}"],
        middleware=[Middleware(
            BearerAuthMiddleware, api_key=api_key, client_id=CLIENT_ID
        )],
    )


class ServerProcess:
    def __init__(self, root: Path, port: int, api_key: str, log_path: Path) -> None:
        self.root = root
        self.port = port
        self.api_key = api_key
        self.log_path = log_path
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self._log_handle = None

    def start(self, timeout: float = 300) -> dict[str, Any]:
        env = os.environ.copy()
        env.update({"WENJIN_MCP_API_KEY": self.api_key})
        command = [
            sys.executable, str(Path(__file__).resolve()), "--server-worker",
            "--project-root", str(self.root), "--port", str(self.port),
        ]
        self._log_handle = self.log_path.open("wb")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.started_at = time.perf_counter()
        self.process = subprocess.Popen(
            command, cwd=PROJECT_ROOT, env=env, stdout=self._log_handle,
            stderr=subprocess.STDOUT, creationflags=flags,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"MCP server exited during startup: {self.process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return {"pid": self.process.pid, "port": self.port}
            except OSError:
                time.sleep(0.25)
        raise TimeoutError("MCP server did not open its local port")

    def stop(self) -> dict[str, Any]:
        if self.process is None:
            return {"stopped": True, "exit_code": None}
        process = self.process
        process.terminate()
        try:
            code = process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=10)
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None
        self.process = None
        closed_deadline = time.monotonic() + 5
        port_closed = False
        while time.monotonic() < closed_deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    time.sleep(0.1)
            except OSError:
                port_closed = True
                break
        return {
            "stopped": process.poll() is not None,
            "exit_code": code,
            "pid": process.pid,
            "port_closed": port_closed,
        }


def peak_working_set_bytes(pid: int) -> int | None:
    """留接口兼容报告字段；真实数值由worker自己的metrics模块采样。"""
    del pid
    return None


def gpu_memory_sample() -> list[int] | None:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=True,
        )
        return [int(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def structured(result: Any) -> dict[str, Any]:
    value = getattr(result, "structuredContent", None)
    if isinstance(value, dict):
        return value
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text:
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                pass
    raise RuntimeError("MCP tool returned no structured JSON object")


async def mcp_call(port: int, api_key: str, tool: str, arguments: dict) -> tuple[dict, dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    notifications: list[str] = []

    class RecordingClientSession(ClientSession):
        async def send_notification(self, notification, related_request_id=None):
            envelope = getattr(notification, "root", notification)
            method = getattr(envelope, "method", None)
            if method:
                notifications.append(str(method))
            return await super().send_notification(notification, related_request_id)

    async with streamablehttp_client(
        f"http://127.0.0.1:{port}{MCP_PATH}", headers={"Authorization": f"Bearer {api_key}"}
    ) as (read_stream, write_stream, get_session_id):
        async with RecordingClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            session_id = get_session_id()
            if initialized.protocolVersion != "2025-11-25":
                raise RuntimeError("MCP negotiated an unexpected protocol version")
            if "notifications/initialized" not in notifications:
                raise RuntimeError("MCP client did not send notifications/initialized")
            listed = await session.list_tools()
            tool_names = [item.name for item in listed.tools]
            if tool not in tool_names:
                raise RuntimeError(f"negotiated server does not expose {tool}")
            call = await session.call_tool(tool, arguments)
            result = structured(call)
            protocol = getattr(initialized, "protocolVersion", None)
            return result, {
                "protocol_version": protocol,
                "notifications": notifications,
                "server_capabilities": initialized.capabilities.model_dump(mode="json"),
                "tools_list_count": len(tool_names),
                "requested_tool_visible": tool in tool_names,
                "session_id_present": bool(session_id),
                "session_id_sha256": sha256(session_id.encode()) if session_id else None,
                "is_error": bool(getattr(call, "isError", False)),
            }


async def mcp_call_when_ready(
    port: int, api_key: str, tool: str, arguments: dict, process: subprocess.Popen,
    timeout: float = 300,
) -> tuple[dict, dict]:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"MCP worker exited before ready: {process.returncode}")
        try:
            result, lifecycle = await mcp_call(port, api_key, tool, arguments)
            if result.get("error") in {
                "source_integrity_failure", "unavailable", "invalid_target", "forbidden"
            }:
                raise RuntimeError(
                    f"MCP became reachable but returned unexpected {result['error']}"
                )
            return result, lifecycle
        except Exception as exc:  # server socket can open before model/lifespan startup ends
            import httpx

            def is_connect_error(error: BaseException) -> bool:
                if isinstance(error, httpx.ConnectError):
                    return True
                return isinstance(error, BaseExceptionGroup) and any(
                    is_connect_error(child) for child in error.exceptions
                )

            if not is_connect_error(exc):
                raise
            last_error = type(exc).__name__
            await asyncio.sleep(1)
    raise TimeoutError(f"MCP worker did not become ready; last error={last_error}")


async def mcp_session_continuity(
    port: int, api_key: str, queries: list[dict[str, str]], *, library: str | None = None
) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    notifications: list[str] = []

    class RecordingClientSession(ClientSession):
        async def send_notification(self, notification, related_request_id=None):
            envelope = getattr(notification, "root", notification)
            method = getattr(envelope, "method", None)
            if method:
                notifications.append(str(method))
            return await super().send_notification(notification, related_request_id)

    async with streamablehttp_client(
        f"http://127.0.0.1:{port}{MCP_PATH}", headers={"Authorization": f"Bearer {api_key}"}
    ) as (read_stream, write_stream, get_session_id):
        async with RecordingClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            session_id = get_session_id()
            if initialized.protocolVersion != "2025-11-25":
                raise RuntimeError("MCP negotiated an unexpected protocol version")
            if "notifications/initialized" not in notifications:
                raise RuntimeError("MCP client did not send notifications/initialized")
            listed = await session.list_tools()
            if "search_docs" not in {item.name for item in listed.tools}:
                raise RuntimeError("search_docs missing after negotiated tools/list")
            results = []
            for question in queries:
                arguments: dict[str, Any] = {
                    "query": question["query"], "top_k": 5, "context_char_budget": 8000,
                }
                if library:
                    arguments.update(library=library, version="1.1")
                result = structured(await session.call_tool("search_docs", arguments))
                results.append({
                    "question_id": question["id"],
                    "answer_rubric": question["answer_rubric"],
                    "evidence_status": result.get("evidence_status"),
                    "error": result.get("error"),
                    "evidence": result.get("evidence", []),
                    "source_context_diagnostics": result.get("source_context_diagnostics"),
                })
            return {
                "protocol_version": initialized.protocolVersion,
                "server_capabilities": initialized.capabilities.model_dump(mode="json"),
                "notifications": notifications,
                "tools_list_count": len(listed.tools),
                "same_session_two_calls": len(results) == 2 and bool(session_id),
                "same_session_call_count": len(results),
                "session_id_sha256": sha256(session_id.encode()) if session_id else None,
                "results": results,
            }


def load_locked_queries() -> list[dict[str, str]]:
    raw = (PROJECT_ROOT / QUESTIONS_RELATIVE).read_bytes()
    if sha256(raw) != QUESTIONS_SHA256:
        raise RuntimeError("locked supplemental-validation question SHA mismatch")
    wanted = set(TARGET_QUESTION_IDS)
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    selected = [{
        "id": row["id"], "query": row["query"],
        "answer_rubric": row["answer_rubric"],
        "expected_status": row["expected_status"],
    } for row in rows if row.get("id") in wanted]
    if {row["id"] for row in selected} != wanted or len(selected) != len(wanted):
        raise RuntimeError("locked query IDs are missing or duplicated")
    return selected


def server_metrics(root: Path) -> dict[str, Any] | None:
    path = root / "phase4-server-metrics.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def warm_searches(
    port: int, api_key: str, queries: list[dict[str, str]], repetitions: int
) -> dict:
    samples = []
    outputs = []
    for repeat in range(repetitions):
        for question in queries:
            query = question["query"]
            started = time.perf_counter()
            result, lifecycle = await mcp_call(port, api_key, "search_docs", {
                "query": query, "top_k": 5, "context_char_budget": 8000,
            })
            elapsed_ms = (time.perf_counter() - started) * 1000
            samples.append(elapsed_ms)
            evidence = result.get("evidence", [])
            outputs.append({
                "repeat": repeat + 1,
                "question_id": question["id"],
                "answer_rubric": question["answer_rubric"],
                "expected_status": question["expected_status"],
                "query_sha256": sha256(query.encode()),
                "elapsed_ms": round(elapsed_ms, 2),
                "status": result.get("evidence_status"),
                "error": result.get("error"),
                "evidence_count": len(evidence),
                "evidence": [
                    {k: item.get(k) for k in (
                        "rank", "source_path", "start_line", "end_line", "page_number",
                        "chunk_id", "text", "download_path", "source_context_status",
                    )}
                    for item in evidence
                ],
                "context_chars": result.get("context_chars"),
                "context_char_budget": result.get("context_char_budget"),
                "source_context_diagnostics": result.get("source_context_diagnostics"),
                "protocol": lifecycle,
            })
    ordered = sorted(samples)
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999) - 1))
    return {
        "n": len(samples), "latency_ms": samples,
        "p50_ms": round(median(samples), 2), "p95_ms": round(ordered[p95_index], 2),
        "timing_scope": "warm-server end-to-end per-call; each sample includes HTTP connect, initialize/initialized, tools/list, search_docs and session close",
        "requests": outputs,
    }


def manifest_raw_hash(root: Path, corpus_id: str, evidence: dict) -> tuple[Path, str]:
    from wenjin.catalog import find_corpus
    entry = find_corpus(root, corpus_id)
    if entry is None:
        raise RuntimeError("isolated corpus is not registered")
    manifest = json.loads((root / entry.manifest_path).read_text(encoding="utf-8-sig"))
    wanted = evidence["source_path"]
    normalized = wanted.removeprefix(f"raw_data/{manifest.get('raw_data_subdir', '').strip('/')}/")
    record = next((item for item in manifest["files"] if item["path"] == normalized), None)
    if record is None:
        # OSG-style source paths are already manifest-relative.
        record = next((item for item in manifest["files"] if item["path"] == wanted), None)
    if record is None:
        raise RuntimeError("selected source path is not in the isolated manifest")
    relative = Path("raw_data") / manifest.get("raw_data_subdir", "") / record["path"]
    return root / relative, str(record.get("raw_sha256") or record.get("sha256"))


async def run_e2e(args: argparse.Namespace) -> dict[str, Any]:
    from wenjin.access import AccessPolicy, write_policy
    from wenjin.index_audit import admit_index
    from wenjin.qdrant_connection import create_qdrant_client
    from wenjin.settings import Settings

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if RUNTIME_ROOT.exists():
        resolved = RUNTIME_ROOT.resolve()
        if not resolved.is_relative_to(OUTPUT_DIR.resolve()) or resolved.name != "runtime-root":
            raise RuntimeError("refusing to clear unexpected runtime-root")
        shutil.rmtree(resolved)
    RUNTIME_ROOT.mkdir(parents=True)

    report: dict[str, Any] = {
        "report": "phase4 isolated real MCP and registry lifecycle rehearsal",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_target": "2025-11-25",
        "project_root_not_modified": True,
        "active_registry_before": registry_snapshot(PROJECT_ROOT),
        "steps": [],
        "limitations": [
            "This is a local Windows-host rehearsal using a copied isolated registry and ACL; it is not production IAM or Linux-host acceptance.",
            "GPU memory sampling is nvidia-smi whole-device memory, not per-process VRAM attribution.",
            "Warm latency samples are sequential MCP calls, not a throughput or concurrency benchmark.",
        ],
    }
    api_key = "phase4-" + os.urandom(32).hex()
    port = args.port
    server: ServerProcess | None = None
    all_gpu_samples: list[list[int]] = []
    try:
        assets = copy_runtime_assets(RUNTIME_ROOT)
        registry = initialize_isolated_registry(RUNTIME_ROOT)
        store = registry["store"]
        candidate = registry["candidate"]
        report["isolated_assets"] = assets
        report["registry_states"] = {"initialized": registry_snapshot(RUNTIME_ROOT)}
        report["runtime_root_resolved"] = str(RUNTIME_ROOT.resolve())
        report["python"] = sys.version.split()[0]
        report["platform"] = sys.platform
        report["gpu_before"] = gpu_memory_sample()
        settings = Settings()
        qdrant = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        try:
            admit_started = time.perf_counter()
            admission = admit_index(candidate, root=RUNTIME_ROOT, qdrant_client=qdrant)
            report["candidate_pre_switch_admission"] = {
                "status": admission["status"],
                "elapsed_seconds": round(time.perf_counter() - admit_started, 3),
                "target_id": candidate.target_id,
                "manifest_sha256": admission.get("manifest_sha256"),
                "canonical_corpus_sha256": candidate.canonical_corpus_sha256,
            }
        finally:
            qdrant.close()

        locked_queries = load_locked_queries()
        report["locked_question_set"] = {
            "path": QUESTIONS_RELATIVE.as_posix(),
            "sha256": QUESTIONS_SHA256,
            "question_ids": [item["id"] for item in locked_queries],
            "queries_and_rubrics": locked_queries,
        }
        server = ServerProcess(RUNTIME_ROOT, port, api_key, OUTPUT_DIR / "server-osg.log")
        start = server.start()
        report["steps"].append({"action": "start_osg", **start})
        osg_cold, protocol = await mcp_call_when_ready(port, api_key, "search_docs", {
            "query": "How does a scene graph organize nodes?", "top_k": 5,
            "context_char_budget": 8000,
        }, server.process)
        report["steps"][-1]["server_process_metrics"] = server_metrics(RUNTIME_ROOT)
        report["steps"][-1].update({
            "cold_start_to_first_success_seconds_n1": round(
                time.perf_counter() - server.started_at, 3
            ),
            "protocol": protocol,
            "active_target_id": "osg-3.6.5-blocks-dualhash-003",
            "default_query_status": osg_cold.get("evidence_status"),
            "default_evidence_count": len(osg_cold.get("evidence", [])),
        })
        explicit_started = time.perf_counter()
        routed, routed_protocol = await mcp_call_when_ready(
            port, api_key, "search_docs", {
                "query": locked_queries[0]["query"], "library": "ogc-3dtiles",
                "version": "1.1", "top_k": 5, "context_char_budget": 8000,
            }, server.process,
        )
        report["steps"][0]["explicit_candidate003_route"] = {
            "question_id": locked_queries[0]["id"],
            "answer_rubric": locked_queries[0]["answer_rubric"],
            "elapsed_ms_includes_protocol_handshake": round(
                (time.perf_counter() - explicit_started) * 1000, 2
            ),
            "lifecycle": routed_protocol,
            "evidence_status": routed.get("evidence_status"),
            "error": routed.get("error"),
            "evidence": routed.get("evidence", []),
            "source_context_diagnostics": routed.get("source_context_diagnostics"),
        }
        report["steps"][0]["same_session_lifecycle_probe"] = await mcp_session_continuity(
            port, api_key, locked_queries[:2], library="ogc-3dtiles"
        )
        all_gpu_samples.append(gpu_memory_sample())
        report["registry_states"]["before_switch"] = registry_snapshot(RUNTIME_ROOT)

        switch = store.switch(candidate, reason="phase4 isolated candidate 003 restart test")
        report["registry_states"]["after_switch"] = registry_snapshot(RUNTIME_ROOT)
        report["steps"].append({"action": "switch_to_candidate003", "revision": switch["revision"]})
        stop_result = server.stop()
        if not stop_result["stopped"] or not stop_result["port_closed"]:
            raise RuntimeError("OSG server process/port was not fully stopped before switch")
        report["steps"][-1]["prior_server_stop"] = stop_result
        server = ServerProcess(RUNTIME_ROOT, port, api_key, OUTPUT_DIR / "server-3dtiles.log")
        start = server.start()
        report["steps"][-1]["restart"] = start
        first_query_started = time.perf_counter()
        first_query, first_protocol = await mcp_call_when_ready(
            port, api_key, "search_docs", {
                "query": locked_queries[0]["query"], "top_k": 5,
                "context_char_budget": 8000,
            }, server.process,
        )
        report["steps"][-1]["cold_start_to_first_success_seconds_n1"] = round(
            time.perf_counter() - server.started_at, 3
        )
        report["steps"][-1]["first_success"] = {
            "question_id": locked_queries[0]["id"],
            "answer_rubric": locked_queries[0]["answer_rubric"],
            "elapsed_ms_includes_protocol_handshake": round(
                (time.perf_counter() - first_query_started) * 1000, 2
            ),
            "protocol": first_protocol,
            "evidence_status": first_query.get("evidence_status"),
            "error": first_query.get("error"),
            "evidence": first_query.get("evidence", []),
            "source_context_diagnostics": first_query.get("source_context_diagnostics"),
        }

        # default active走单库候选深度；route003语义证据独立记录，不混称为同一ranker路径。
        phase3 = await warm_searches(port, api_key, locked_queries, args.repetitions)
        report["steps"][-1]["default_active_queries"] = phase3
        report["steps"][-1]["server_process_metrics"] = server_metrics(RUNTIME_ROOT)
        all_gpu_samples.append(gpu_memory_sample())

        # 用实际返回证据做一次允许取件：验证原文raw SHA，不输出Bearer。
        selected = next((
            item for request in phase3["requests"] for item in request["evidence"]
            if item.get("download_path")
        ), None)
        if selected is None:
            raise RuntimeError("candidate003 did not return a downloadable evidence item")
        source_path, expected_raw_hash = manifest_raw_hash(
            RUNTIME_ROOT, "ogc-3dtiles-1.1", selected
        )
        import httpx

        get_started = time.perf_counter()
        get_response = httpx.get(
            f"http://127.0.0.1:{port}{selected['download_path']}",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30,
        )
        response_hash = sha256(get_response.content)
        report["steps"][-1]["allowed_file_get"] = {
            "status_code": get_response.status_code,
            "bytes": len(get_response.content),
            "response_raw_sha256": response_hash,
            "manifest_raw_sha256": expected_raw_hash,
            "matches_manifest": response_hash == expected_raw_hash,
            "equals_isolated_file": get_response.content == source_path.read_bytes(),
            "elapsed_ms": round((time.perf_counter() - get_started) * 1000, 2),
            "source_path_sha256": sha256(selected["source_path"].encode()),
        }
        if get_response.status_code != 200 or response_hash != expected_raw_hash:
            raise RuntimeError("allowed /files response did not match manifest raw hash")

        # 收回临时ACL后验证search与/files都被拒绝；然后恢复授权完成registry回退。
        write_policy(RUNTIME_ROOT / "configs/access", AccessPolicy(
            "ogc-3dtiles-1.1", "internal", (), "isolated deny check"
        ))
        denied_search, _ = await mcp_call(port, api_key, "search_docs", {
            "query": locked_queries[1]["query"], "top_k": 5,
            "context_char_budget": 8000,
        })
        denied_file = httpx.get(
            f"http://127.0.0.1:{port}{selected['download_path']}",
            headers={"Authorization": f"Bearer {api_key}"}, timeout=15,
        )
        report["steps"][-1]["denied_checks"] = {
            "search_error": denied_search.get("error"),
            "search_evidence_count": len(denied_search.get("evidence", [])),
            "file_status_code": denied_file.status_code,
            "file_error": denied_file.json().get("error") if denied_file.headers.get(
                "content-type", ""
            ).startswith("application/json") else None,
        }
        if denied_search.get("error") != "forbidden" or denied_search.get("evidence"):
            raise RuntimeError("ACL-denied query was not rejected before retrieval")
        if denied_file.status_code != 403:
            raise RuntimeError("ACL-denied /files request was not rejected")
        write_policy(RUNTIME_ROOT / "configs/access", AccessPolicy(
            "ogc-3dtiles-1.1", "internal", (CLIENT_ID,), "isolated test"
        ))
        report["registry_states"]["before_rollback"] = registry_snapshot(RUNTIME_ROOT)

        stop_result = server.stop()
        if not stop_result["stopped"] or not stop_result["port_closed"]:
            raise RuntimeError("candidate003 server process/port was not fully stopped before rollback")
        rollback = store.rollback(reason="phase4 isolated rollback and restart test")
        report["registry_states"]["after_rollback"] = registry_snapshot(RUNTIME_ROOT)
        report["steps"].append({
            "action": "rollback_to_osg_and_restart",
            "rollback_revision": rollback["revision"],
            "prior_server_stop": stop_result,
        })
        server = ServerProcess(RUNTIME_ROOT, port, api_key, OUTPUT_DIR / "server-osg-rollback.log")
        restart = server.start()
        result, lifecycle = await mcp_call_when_ready(port, api_key, "search_docs", {
            "query": "How does a scene graph organize nodes?", "top_k": 5,
            "context_char_budget": 8000,
        }, server.process)
        report["steps"][-1]["server_process_metrics"] = server_metrics(RUNTIME_ROOT)
        report["steps"][-1].update({
            "restart": restart,
            "cold_start_to_first_success_seconds_n1": round(
                time.perf_counter() - server.started_at, 3
            ),
            "active_target_id": "osg-3.6.5-blocks-dualhash-003",
            "protocol": lifecycle,
            "default_query_status": result.get("evidence_status"),
            "default_evidence_count": len(result.get("evidence", [])),
        })
        all_gpu_samples.append(gpu_memory_sample())
        report["server_process_peak_working_set_bytes"] = {
            "osg_first": report["steps"][0].get("server_process_metrics", {}).get(
                "process_memory", {}
            ).get("peak_working_set_bytes"),
            "3dtiles": report["steps"][1].get("server_process_metrics", {}).get(
                "process_memory", {}
            ).get("peak_working_set_bytes"),
            "osg_rollback": report["steps"][2].get("server_process_metrics", {}).get(
                "process_memory", {}
            ).get("peak_working_set_bytes"),
            "method": "worker-side Windows GetProcessMemoryInfo sampled during service lifetime",
        }
        report["gpu_memory_mib_samples_whole_device"] = all_gpu_samples
        report["runtime_file_cleanup"] = "pending"
        report["isolated_final_registry"] = registry_snapshot(RUNTIME_ROOT)
        report["active_registry_after"] = registry_snapshot(PROJECT_ROOT)
        if report["active_registry_after"] != report["active_registry_before"]:
            raise RuntimeError("original active registry changed during isolated rehearsal")
        if report["isolated_final_registry"]["active_target_id"] != \
                "osg-3.6.5-blocks-dualhash-003":
            raise RuntimeError("isolated rollback did not restore original target")
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc).replace(api_key, "<redacted>").replace(
                str(RUNTIME_ROOT), "<isolated-root>"
            ),
        }
        raise
    finally:
        if server is not None:
            try:
                report.setdefault("cleanup", []).append(server.stop())
            except Exception as exc:  # noqa: BLE001 - ensure child cleanup is attempted
                report.setdefault("cleanup_errors", []).append(type(exc).__name__)
        try:
            if RUNTIME_ROOT.exists():
                resolved = RUNTIME_ROOT.resolve()
                if not resolved.is_relative_to(OUTPUT_DIR.resolve()) or resolved.name != "runtime-root":
                    raise RuntimeError("refusing cleanup outside phase4 runtime-root")
                shutil.rmtree(resolved)
                report["runtime_file_cleanup"] = "runtime-root removed after registry snapshots saved"
        except Exception as exc:  # noqa: BLE001
            report.setdefault("cleanup_errors", []).append(type(exc).__name__)
        if RUNTIME_ROOT.exists():
            report.setdefault("cleanup_errors", []).append("runtime_root_still_exists")
        for log_path in OUTPUT_DIR.glob("server-*.log"):
            try:
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                log_text = log_text.replace(api_key, "<redacted>").replace(
                    str(RUNTIME_ROOT), "<isolated-root>"
                )
                log_path.write_text(log_text, encoding="utf-8")
            except OSError:
                report.setdefault("cleanup_errors", []).append("log_redaction_failed")
        report["client_session_delete_responses"] = {
            log_path.name: sum(
                1 for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if '"DELETE /mcp HTTP/1.1" 200' in line
            )
            for log_path in OUTPUT_DIR.glob("server-*.log")
        }
        if report.get("cleanup_errors"):
            report["status"] = "failed"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["active_registry_after"] = registry_snapshot(PROJECT_ROOT)
        json_write(OUTPUT_DIR / "report.json", report)
        write_markdown_report(report, OUTPUT_DIR / "report.md")
    return report


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Phase 4 isolated MCP E2E and registry lifecycle",
        "",
        f"Status: **{report.get('status', 'failed')}**",
        f"Protocol target: `{report.get('protocol_target')}`",
        f"Platform: `{report.get('platform')}`; Python `{report.get('python')}`",
        f"Temporary root: `{report.get('runtime_root_resolved')}` (removed in finally)",
        "",
        "## Evidence",
        "",
        f"- Candidate 003 pre-switch admission: `{report.get('candidate_pre_switch_admission')}`",
        f"- Copied isolated file count/bytes: {report.get('isolated_assets', {}).get('copied_file_count')} / {report.get('isolated_assets', {}).get('copied_bytes')}",
        f"- Registry transitions: `{json.dumps(report.get('registry_states', {}), ensure_ascii=False)}`",
        f"- Steps: `{json.dumps(report.get('steps', []), ensure_ascii=False)}`",
        f"- Process peak working set: `{report.get('server_process_peak_working_set_bytes')}`",
        f"- Whole-device GPU samples (MiB): `{report.get('gpu_memory_mib_samples_whole_device')}`",
        f"- Original registry unchanged: `{report.get('active_registry_after') == report.get('active_registry_before')}`",
        "",
        "## Limits",
        "",
    ]
    lines.extend(f"- {item}" for item in report.get("limitations", []))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-worker", action="store_true")
    parser.add_argument("--project-root", default=str(RUNTIME_ROOT))
    parser.add_argument("--port", type=int, default=8879)
    parser.add_argument("--repetitions", type=int, default=2)
    args = parser.parse_args()
    if args.server_worker:
        server_worker(args)
    else:
        result = asyncio.run(run_e2e(args))
        print(json.dumps({
            "status": result.get("status", "failed"),
            "report": str(OUTPUT_DIR / "report.md"),
            "runtime_file_cleanup": result.get("runtime_file_cleanup"),
        }, ensure_ascii=False))
        if result.get("status") != "passed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
