"""隔离多切片 MCP 补测；由冻结的主实验程序调用，不单独启动模型。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import statistics
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import quote


def fixture_text(marker: str, section_count: int = 18) -> bytes:
    # 每段重复公共身份标记，便于核验任意命中切片；不同主题减少完全相同的候选。
    themes = ["vector normalization", "version publication", "access authorization",
              "model admission queue", "document ingestion", "ranking evidence"]
    paragraphs = []
    for i in range(section_count):
        lines = [f"Section {i:02d}: {themes[i % len(themes)]}. Evidence {marker}."]
        for j in range(6):
            lines.append(f"{marker} step {i:02d}/{j}: {themes[i % len(themes)]} uses a fixed "
                         "model identity and independently verified source version. "
                         "Synthetic laboratory text for concurrency testing only.")
        paragraphs.append("\n".join(lines))
    return ("\n\n".join(paragraphs) + "\n").encode()


def seed_shared(state: dict, token: str, baseline) -> dict:
    from qdrant_client import QdrantClient

    from wenjin.content_hashing import (
        TEXT_NEWLINE_LF_V1,
        hash_content,
        manifest_content_fingerprint,
    )
    from wenjin.index_registry import IndexRegistryStore
    from wenjin.indexing.build import build_candidate_index

    root = Path(state["runtime_root"])
    library, version = "synthetic-concurrency", "1.0"
    corpus = f"{library}-{version}"
    marker = "multilane_shared_source_v1"
    content = fixture_text(marker)
    raw = root / "raw_data" / library / version / "guide.txt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(content)
    hashed = hash_content(content, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8")
    manifest = {"schema_version": 3, "corpus_id": corpus, "library": library,
                "version": version, "license": "synthetic-test-only",
                "source_url": f"urn:synthetic:{corpus}",
                "raw_data_subdir": f"{library}/{version}",
                "files": [{"path": "guide.txt", "raw_sha256": hashed.raw_sha256,
                           "canonical_sha256": hashed.canonical_sha256,
                           "normalization": TEXT_NEWLINE_LF_V1, "encoding": "utf-8",
                           "bytes": len(content), "indexable": True, "parser": "text-line-v1",
                           "source_url": f"urn:synthetic:{corpus}/guide.txt",
                           "logical_document_id": f"{corpus}:guide.txt"}]}
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    path = root / "corpora" / corpus / "source-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    qdrant = QdrantClient(url=f"http://127.0.0.1:{state['ports']['qdrant']}")
    client, embedder = baseline.remote_embedder(state, token)
    try:
        target = build_candidate_index(manifest_path=path, target_id=f"{corpus}-target",
                                       strategy="blocks-split", dense_collection="multilane_shared",
                                       bm25_path=root / "data/indexes/multilane_shared.sqlite",
                                       qdrant_client=qdrant, embedder=embedder,
                                       embedding_model=baseline.EMBEDDING_ID, project_root=root)
        if target.chunk_count < 8:
            raise RuntimeError("multislice_fixture_too_small")
        IndexRegistryStore(root / "data/indexes/index-registry.json").initialize(
            target, reason="isolated multilane benchmark fixture")
        return {"library": library, "version": version, "target_id": target.target_id,
                "marker": marker, "chunk_count": target.chunk_count,
                "content_sha256": hashed.raw_sha256, "dimensions": target.vector_dimension}
    finally:
        client.close()
        qdrant.close()


def upload(admin, headers, reader, marker: str, baseline, *, name: str, stop_event=None) -> dict:
    if stop_event is not None and stop_event.is_set():
        raise RuntimeError("resource_gate_stopped_before_upload")
    content = fixture_text(marker)
    response = admin.post(f"/api/files?library_id={quote(reader['library_id'], safe='')}",
                          headers=headers | {"Content-Type": "application/octet-stream",
                                             "X-File-Name": name}, content=content)
    if response.status_code != 202:
        raise RuntimeError(f"managed_multislice_upload_http_{response.status_code}")
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 120
    while True:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("resource_gate_stopped_ingest_poll")
        polled = admin.get(f"/api/jobs/{quote(job_id, safe='')}")
        polled.raise_for_status()
        job = polled.json()
        if job.get("status") in baseline.TERMINAL_JOBS:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("managed_multislice_ingest_poll_timeout")
        time.sleep(.05)
    if job.get("status") != "completed":
        raise RuntimeError("managed_multislice_ingest_failed")
    info_response = admin.get(f"/api/files/{quote(job['file_id'], safe='')}")
    info_response.raise_for_status()
    info = info_response.json()
    if info.get("chunk_count", 0) < 8:
        raise RuntimeError("managed_multislice_fixture_too_small")
    return {"file_id": job["file_id"], "version_id": info["active_version_id"],
            "library_id": reader["library_id"], "marker": marker,
            "content_sha256": hashlib.sha256(content).hexdigest(), "job": job, "file": info}


def latency_stats(rows: list[dict]) -> dict:
    def stats(items):
        values = sorted(row["elapsed_seconds"] for row in items)
        return {"n": len(values), "median": statistics.median(values) if values else None,
                "p95": values[max(0, math.ceil(.95 * len(values)) - 1)] if values else None,
                "max": max(values) if values else None}
    return {"all": stats(rows), "success": stats([r for r in rows if r["passed"]]),
            "failure": stats([r for r in rows if not r["passed"]])}


def _run_mcp_stage(state: dict, output: Path, baseline, stop_event: threading.Event,
                   *, model_token: str, repetitions: int = 3, opened_clients: list) -> dict:
    """Model API稳定后才调用；账号bootstrap必须早于management启动。"""
    output = Path(output)
    marker = "multilane_managed_source_v1"
    shared = seed_shared(state, model_token, baseline)
    admin_password = "A9!" + uuid.uuid4().hex + "Z"
    baseline.bootstrap_isolated_identity_db(Path(state["runtime_root"]) / "identity/identity.sqlite",
                                           admin_password)
    baseline.run_command(baseline.compose_args(state) + ["up", "-d", "--no-build", "--pull",
                                                        "never", "management", "mcp"], timeout=180)
    base = f"http://127.0.0.1:{state['ports']['admin']}"
    baseline.wait_http(base + "/api/health", timeout_seconds=120, stop_event=stop_event)
    bench = baseline.Benchmark(state, output, None, stop_event)
    admin, headers = bench.admin_login(base, admin_password)
    opened_clients.append(admin)
    reader = bench.create_managed_reader(admin, headers)
    reader_b = bench.create_unprivileged_reader(admin, headers)
    bench.reader_token = reader["mcp_token"]
    managed = upload(admin, headers, reader, marker, baseline, name="multilane-guide.txt", stop_event=stop_event)
    bench.grant_resources(admin, headers, reader, managed["file_id"], shared["library"])
    (output / "mcp-fixture-shared.txt").write_bytes(fixture_text(shared["marker"]))
    (output / "mcp-fixture-managed.txt").write_bytes(fixture_text(marker))
    summary = {"fixtures": {"shared": shared, "managed": managed}, "requests": [],
               "identities": {"A_user_id_sha256": hashlib.sha256(reader["user_id"].encode()).hexdigest(),
                              "B_user_id_sha256": hashlib.sha256(reader_b["user_id"].encode()).hexdigest()},
               "repetitions": [], "client_inflight_limit": 4, "passed": False,
               "queue_time": "未测得：需要关联模型事件，不能由MCP端到端相减"}

    async def scenario():
        sessions = await bench.open_sessions(reader["mcp_token"], "multilane-authorized", 4)
        b_sessions = await bench.open_sessions(reader_b["mcp_token"], "multilane-denied", 2)
        for session in sessions + b_sessions:
            session.client.headers["MCP-Protocol-Version"] = baseline.PROTOCOL
        args_shared = {"query": "model admission queue version publication", "target_id": shared["target_id"]}
        args_managed = {"query": "model admission queue version publication", "file_id": managed["file_id"]}

        async def one(session, label, args, *, expected=managed, denied=False, tag=""):
            start = time.perf_counter()
            started_utc = baseline.utc_now()
            try:
                task = asyncio.create_task(session.call("tools/call", {"name": "search_docs", "arguments": args}))
                while not task.done():
                    if stop_event.is_set():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        result = {"http_status": None, "tool": {"error": "ResourceGateCancelled", "evidence": []}}
                        break
                    await asyncio.wait({task}, timeout=.05)
                else:
                    result = await task
            except Exception as exc:
                result = {"http_status": None, "tool": {"error": type(exc).__name__, "evidence": []}}
            elapsed = time.perf_counter() - start
            tool = result.get("tool", {})
            evidence = tool.get("evidence", [])
            if denied:
                passed = result.get("http_status") == 200 and bool(tool.get("error") or tool.get("_is_error")) and not evidence
            elif label == "shared":
                passed = (result.get("http_status") == 200 and not tool.get("_is_error") and not tool.get("error")
                          and bool(evidence) and all(e.get("library") == shared["library"]
                          and e.get("version") == shared["version"] and e.get("index_target_id") == shared["target_id"]
                          and shared["marker"] in e.get("text", "") for e in evidence))
            else:
                identity = tool.get("managed_file", {})
                passed = (result.get("http_status") == 200 and not tool.get("_is_error") and not tool.get("error")
                          and bool(evidence) and identity.get("file_id") == expected["file_id"]
                          and identity.get("version_id") == expected["version_id"]
                          and all(e.get("file_id") == expected["file_id"] and e.get("version_id") == expected["version_id"]
                                  and expected["marker"] in e.get("text", "") for e in evidence))
            row = {"label": label, "tag": tag, "denied": denied, "arguments": args,
                   "start_utc": started_utc, "end_utc": baseline.utc_now(), "elapsed_seconds": elapsed,
                   "passed": bool(passed), "http_status": result.get("http_status"),
                   "error": tool.get("error"), "mcp_is_error": tool.get("_is_error"),
                   "session_hash": session.session_hash, "tool": tool,
                   "rpc": baseline.redact_protocol_value(result.get("rpc", result.get("mcp_rpc", {})), reader["mcp_token"])}
            summary["requests"].append(row)
            baseline.append_jsonl(output / "mcp-requests.jsonl", row)
            return row

        summary["authorization"] = await asyncio.gather(
            one(b_sessions[0], "shared", args_shared, denied=True, tag="authorization-B"),
            one(b_sessions[1], "managed", args_managed, denied=True, tag="authorization-B"))
        summary["warmup"] = [await one(sessions[0], "shared", args_shared, tag="warmup"),
                             await one(sessions[1], "managed", args_managed, tag="warmup")]
        for repetition in range(repetitions):
            rows = []
            started = time.perf_counter()
            for wave in range(2):
                if stop_event.is_set():
                    raise RuntimeError("resource_gate_stopped_mcp")
                wave_rows = await asyncio.gather(*[
                    one(sessions[i], "shared" if i % 2 == 0 else "managed",
                        args_shared if i % 2 == 0 else args_managed,
                        tag=f"mixed-round-{repetition}-wave-{wave}") for i in range(4)])
                rows.extend(wave_rows)
            duration = time.perf_counter() - started
            summary["repetitions"].append({"round": repetition, "request_count": len(rows),
                                            "success_count": sum(r["passed"] for r in rows),
                                            "duration_seconds": duration,
                                            "all_rps": len(rows) / duration,
                                            "successful_rps": sum(r["passed"] for r in rows) / duration,
                                            "latencies": latency_stats(rows)})
            if any(not r["passed"] for r in rows):
                raise RuntimeError("mcp_result_contract_failed")
        stale = await one(sessions[1], "managed", args_managed | {"version": "__stale_probe__"},
                          denied=True, tag="stale-version")
        summary["stale_version"] = stale
        competition = []
        for n in range(2):
            if stop_event.is_set():
                raise RuntimeError("resource_gate_stopped_before_competition")
            new_marker = f"multilane_ingest_source_{n}"
            begin = baseline.utc_now()
            started = time.perf_counter()
            queries, new_fixture = await asyncio.gather(
                asyncio.gather(one(sessions[0], "shared", args_shared, tag=f"query-ingest-{n}"),
                               one(sessions[1], "managed", args_managed, tag=f"query-ingest-{n}")),
                asyncio.to_thread(upload, admin, headers, reader, new_marker, baseline,
                                  name=f"ingest-{n}.txt", stop_event=stop_event))
            # 身份核验只对任务完成后新发起请求；不把上传前已开始的query当成新版读取。
            inventory = admin.get("/api/admin/resources")
            if inventory.status_code != 200:
                raise RuntimeError("ingest_readback_inventory_sync_failed")
            grant = admin.post("/api/admin/grants", headers=headers,
                               json={"user_id": reader["user_id"], "resource_kind": "managed_file",
                                     "resource_id": new_fixture["file_id"], "allowed": True,
                                     "expected_revision": 0})
            if grant.status_code != 200:
                baseline.write_json(output / "ingest-grant-failure.json", {"status": grant.status_code, "body": grant.json(), "scope": "isolated temporary file"})
                raise RuntimeError("ingest_readback_grant_failed")
            readback = await one(sessions[1], "managed", args_managed | {"file_id": new_fixture["file_id"]},
                                 expected=new_fixture, tag=f"post-ingest-{n}")
            competition.append({"begin_utc": begin, "end_utc": baseline.utc_now(),
                                "elapsed_seconds": time.perf_counter() - started,
                                "queries": queries, "ingest": new_fixture, "readback": readback,
                                "server_model_competition": "需独立模型事件确认，客户端重叠不足以证明"})
        summary["query_ingest"] = competition
        summary["passed"] = all(row["passed"] for row in summary["requests"])

    async def close_in_same_loop():
        try:
            await scenario()
        finally:
            await bench.close_sessions()

    try:
        asyncio.run(close_in_same_loop())
    finally:
        admin.close()
        rows = summary["requests"]
        summary["request_count"] = len(rows)
        summary["contract_pass_count"] = sum(r["passed"] for r in rows)
        summary["latencies_all_scenarios"] = latency_stats(rows)
        summary["error_counts"] = dict(Counter(str(r["error"] or "none") for r in rows))
        summary["http_status_counts"] = dict(Counter(str(r["http_status"]) for r in rows))
        summary["intentional_denial_count"] = sum(r["denied"] for r in rows)
        summary["unexpected_failure_count"] = sum(not r["passed"] for r in rows)
        # tokens/passwords仅存内存，summary没有账户原始回包或个人凭据。
        baseline.write_json(output / "mcp-summary.json", summary)
    return summary


def run_mcp_stage(state: dict, output: Path, baseline, stop_event: threading.Event,
                  *, model_token: str, repetitions: int = 3) -> dict:
    opened_clients = []
    try:
        return _run_mcp_stage(state, output, baseline, stop_event, model_token=model_token,
                              repetitions=repetitions, opened_clients=opened_clients)
    except BaseException as exc:
        baseline.write_json(Path(output) / "mcp-failure.json",
                            {"type": type(exc).__name__, "safe_reason": str(exc) if isinstance(exc, RuntimeError)
                             else "inspect_raw_requests_and_service_logs", "passed": False})
        raise
    finally:
        for client in opened_clients:
            client.close()
