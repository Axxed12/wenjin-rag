"""Live acceptance: active OSG routing plus HTTP-managed files through MCP.

Run from the repository root with:
  uv --cache-dir .uv-cache run --no-sync python tests/run_managed_mcp_acceptance.py

The helper uses a fresh project root and unique managed collections. It reads the
real OSG index but does not modify its registry, corpus, indexes, or access files.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from qdrant_client import QdrantClient

from wenjin.index_registry import IndexRegistryStore, resolve_corpus_identity, resolve_project_path
from wenjin.qdrant_connection import create_qdrant_client
from wenjin.settings import Settings

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "artifacts/iterations/2026-10-06-osg-managed-mcp"
HELPER = Path(__file__).with_name("managed_mcp_acceptance_server.py")
MCP_PATH = "/mcp"
AUTHORIZED = "managed-mcp-authorized"
QUERY_OSG = "How does a scene graph organize nodes?"
QUERY_3DTILES = "在 Class Property 表格中，extras 属性用于存放哪类内容？"
MARKER_V1 = "quartzwidget-v1-only-active"
MARKER_V2 = "obsidianwidget-v2-only-active"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def structured(result: Any) -> dict:
    value = getattr(result, "structuredContent", None)
    if isinstance(value, dict):
        return value
    for block in getattr(result, "content", []):
        block_text = getattr(block, "text", None)
        if block_text:
            try:
                decoded = json.loads(block_text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise RuntimeError("MCP tool returned no structured JSON object")


class Acceptance:
    def __init__(self) -> None:
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
        self.output = EVIDENCE / f"live-{self.run_id}"
        self.runtime_root = self.output / "runtime-root"
        self.access_dir = self.output / "isolated-access"
        self.input_dir = self.output / "inputs"
        self.control_path = self.output / "manager-fault-control.json"
        self.log_path = self.output / "acceptance-cases.jsonl"
        self.output.mkdir(parents=True, exist_ok=False)
        self.runtime_root.mkdir()
        self.access_dir.mkdir()
        self.input_dir.mkdir()
        self.events: list[dict] = []
        self.manager: subprocess.Popen | None = None
        self.mcp: subprocess.Popen | None = None
        self.manager_log = None
        self.mcp_log = None
        self.manager_token = "fm-" + secrets.token_urlsafe(32)
        self.mcp_key = "mcp-" + secrets.token_urlsafe(32)
        self.http = httpx.Client(
            timeout=httpx.Timeout(30, connect=3),
            trust_env=False,
            headers={"Authorization": f"Bearer {self.manager_token}"},
        )
        self.manager_port = free_port()
        self.mcp_port = free_port()
        self.manager_url = f"http://127.0.0.1:{self.manager_port}"
        self.qdrant: QdrantClient | None = None
        self.initial_collections: set[str] = set()
        self.active: dict[str, Any] = {}
        self.lifecycle: dict[str, Any] = {}
        self.tool_names: list[str] = []
        self.client_session = None
        self.client_stack: AsyncExitStack | None = None
        self.created_file_ids: set[str] = set()

    def record(self, case: str, status: str, detail: dict) -> None:
        event = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "case": case,
            "status": status,
            "detail": detail,
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)

    def check(self, case: str, condition: bool, detail: dict) -> None:
        self.record(case, "pass" if condition else "fail", detail)
        if not condition:
            raise AssertionError(f"acceptance failed at {case}: {detail}")

    def baseline(self) -> None:
        target = IndexRegistryStore(REPO / "data/indexes/index-registry.json").active()
        settings = Settings()
        self.qdrant = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        collections = self.qdrant.get_collections().collections
        self.initial_collections = {item.name for item in collections}
        bm25 = resolve_project_path(REPO, target.bm25_path)
        manifest = resolve_project_path(REPO, target.manifest_path)
        collection = self.qdrant.get_collection(target.dense_collection)
        self.active = {
            "target_id": target.target_id,
            "corpus_id": resolve_corpus_identity(target, root=REPO).corpus_id,
            "library": target.library,
            "version": target.version,
            "collection": target.dense_collection,
            "points": collection.points_count,
            "bm25_sha256": sha256(bm25.read_bytes()),
            "manifest_sha256": sha256(manifest.read_bytes()),
            "registry_sha256": sha256((REPO / "data/indexes/index-registry.json").read_bytes()),
            "collection_count": len(collections),
            "managed_collection_count": sum(item.name.startswith("fm_") for item in collections),
        }
        self.record("readonly-live-baseline", "pass", self.active)

    def write_policy(self, corpus_id: str, visibility: str, clients: list[str]) -> None:
        path = self.access_dir / f"{corpus_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "schema_version": 1,
            "corpus_id": corpus_id,
            "visibility": visibility,
            "allowed_clients": clients,
            "note": "isolated MCP acceptance policy",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    def start_process(self, mode: str) -> tuple[subprocess.Popen, int]:
        if mode == "manager":
            port = self.manager_port
            command = [sys.executable, str(HELPER), "manager", "--root", str(self.runtime_root),
                       "--host", "127.0.0.1", "--port", str(port), "--admin-token", self.manager_token,
                       "--fault-control", str(self.control_path)]
            env = os.environ.copy()
            env["WENJIN_FILE_ADMIN_TOKEN"] = self.manager_token
            self.manager_log = (self.output / "manager-server.log").open("wb")
            self.manager = subprocess.Popen(command, cwd=REPO, env=env, stdout=self.manager_log,
                                            stderr=subprocess.STDOUT,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return self.manager, port

        port = self.mcp_port
        command = [sys.executable, str(HELPER), "mcp", "--project-root", str(REPO),
                   "--access-dir", str(self.access_dir), "--host", "127.0.0.1", "--port", str(port),
                   "--client-id", AUTHORIZED, "--mcp-api-key", self.mcp_key]
        env = os.environ.copy()
        env["WENJIN_AUDIT_SINK"] = "off"
        env["WENJIN_FILE_MANAGEMENT_URL"] = self.manager_url
        env["WENJIN_FILE_ADMIN_TOKEN"] = self.manager_token
        self.mcp_log = (self.output / "mcp-server.log").open("wb")
        self.mcp = subprocess.Popen(command, cwd=REPO, env=env, stdout=self.mcp_log,
                                    stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return self.mcp, port

    async def wait_manager(self) -> None:
        process, _ = self.start_process("manager")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"managed HTTP server exited early: {process.returncode}")
            try:
                response = self.http.get(f"{self.manager_url}/api/health")
                if response.status_code == 200 and response.json().get("status") == "ready":
                    self.check("manager-http-singleton-ready", True, {
                        "pid": process.pid, "status": response.status_code,
                        "auth_required": response.status_code == 200,
                        "runtime_root_relative": self.runtime_root.relative_to(EVIDENCE).as_posix(),
                    })
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError("managed HTTP server did not become ready")

    async def connect_mcp(self, *, mode: str) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        process, port = self.start_process("mcp")
        url = f"http://127.0.0.1:{port}{MCP_PATH}"
        deadline = time.monotonic() + 240
        last_error = None
        # FastMCP binds its socket before Uvicorn finishes lifespan startup (model load,
        # admission and route assembly). A TCP connect alone is not readiness. Probe the
        # HTTP route until the app itself responds, then create exactly one MCP session.
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"MCP server exited during startup: {process.returncode}")
            try:
                response = self.http.get(
                    url, headers={"Authorization": f"Bearer {self.mcp_key}"}, timeout=2
                )
                # GET is not an MCP request on this endpoint; any HTTP response proves
                # Uvicorn has completed startup and is dispatching requests.
                if response.status_code >= 100:
                    break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(0.4)
        else:
            raise TimeoutError(f"MCP HTTP app did not become ready: {last_error}")

        stack = AsyncExitStack()
        try:
            read_stream, write_stream, get_session_id = await stack.enter_async_context(
                streamablehttp_client(url, headers={"Authorization": f"Bearer {self.mcp_key}"})
            )
            notifications: list[str] = []

            class RecordingSession(ClientSession):
                async def send_notification(inner, notification, related_request_id=None):
                    envelope = getattr(notification, "root", notification)
                    method = getattr(envelope, "method", None)
                    if method:
                        notifications.append(str(method))
                    return await super(RecordingSession, inner).send_notification(
                        notification, related_request_id
                    )

            client = await stack.enter_async_context(RecordingSession(read_stream, write_stream))
            initialized = await client.initialize()
            if initialized.protocolVersion != "2025-11-25":
                raise RuntimeError(f"wrong MCP protocol: {initialized.protocolVersion}")
            if "notifications/initialized" not in notifications:
                raise RuntimeError("client did not send notifications/initialized")
            listed = await client.list_tools()
            names = [item.name for item in listed.tools]
            if "search_docs" not in names:
                raise RuntimeError("search_docs missing from tools/list")
            session_id = get_session_id()
            self.client_stack = stack
            self.client_session = client
            self.tool_names = names
            self.lifecycle = {
                "mode": mode,
                "protocol_version": initialized.protocolVersion,
                "notifications": notifications,
                "server_capabilities": initialized.capabilities.model_dump(mode="json"),
                "session_id_present": bool(session_id),
                "session_id_sha256": sha256(session_id.encode()) if session_id else None,
                "tools": names,
                "pid": process.pid,
                "mcp_process_start_count": 1,
                "readiness_probe_status": response.status_code,
            }
            self.check(f"{mode}-stateful-mcp-initialized", True, self.lifecycle)
        except Exception:
            await stack.aclose()
            raise

    async def mcp_call(self, tool: str, arguments: dict) -> dict:
        if self.client_session is None:
            raise RuntimeError("MCP session is not connected")
        result = await self.client_session.call_tool(tool, arguments)
        payload = structured(result)
        return payload

    async def close_mcp_session(self) -> None:
        if self.client_stack is not None:
            await self.client_stack.aclose()
            self.client_stack = None
            self.client_session = None

    def wait_job(self, job_id: str, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        observed = []
        while time.monotonic() < deadline:
            response = self.http.get(f"{self.manager_url}/api/jobs/{quote(job_id, safe='')}")
            response.raise_for_status()
            job = response.json()
            stamp = (job.get("status"), job.get("phase"), job.get("progress"))
            if not observed or observed[-1] != stamp:
                observed.append(stamp)
            if job.get("status") in {"completed", "failed", "deleted", "duplicate", "cancelled"}:
                return {"job": job, "observed": observed}
            time.sleep(0.15)
        raise TimeoutError(f"job {job_id} timed out; recent states: {observed[-6:]}")

    def upload(self, payload: bytes, name: str, *, replace_file_id: str | None = None) -> dict:
        headers = {"X-File-Name": quote(name, safe=""), "Content-Type": "application/octet-stream"}
        if replace_file_id:
            response = self.http.put(f"{self.manager_url}/api/files/{quote(replace_file_id, safe='')}",
                                     content=payload, headers=headers)
        else:
            response = self.http.post(f"{self.manager_url}/api/files", content=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    def write_fault(self, phase: str) -> None:
        temporary = self.control_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"phase": phase, "action": "raise"}), encoding="utf-8")
        os.replace(temporary, self.control_path)

    @staticmethod
    def ids(payload: dict) -> dict:
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        return {
            "evidence_count": len(evidence),
            "evidence_ids": [item.get("chunk_id") for item in evidence],
            "evidence_target_ids": sorted({item.get("target_id") or item.get("index_target_id")
                                            for item in evidence
                                            if item.get("target_id") or item.get("index_target_id")}),
            "managed_version_ids": sorted({item.get("version_id") for item in evidence
                                             if item.get("version_id")}),
            "managed_source_sha256s": sorted({item.get("source_sha256") for item in evidence
                                                if item.get("source_sha256")}),
            "text_contains_v1": any(MARKER_V1 in str(item.get("text", "")) for item in evidence),
            "text_contains_v2": any(MARKER_V2 in str(item.get("text", "")) for item in evidence),
        }

    async def run_osg_batch(self) -> None:
        self.write_policy(self.active["corpus_id"], "internal", [AUTHORIZED])
        self.write_policy("ogc-3dtiles-1.1", "public", ["*"])
        await self.connect_mcp(mode="osg")
        self.check("osg-three-tools-present",
                   {"search_docs", "list_corpora"}.issubset(self.tool_names),
                   {"tools": self.tool_names})
        requests = [
            ("default", {"query": QUERY_OSG, "top_k": 5}),
            ("active-library-alias", {"query": QUERY_OSG, "library": "osg", "top_k": 5}),
            ("active-target-id", {"query": QUERY_OSG, "target_id": self.active["target_id"], "top_k": 5}),
        ]
        results = {}
        for label, args in requests:
            result = await self.mcp_call("search_docs", args)
            info = self.ids(result)
            results[label] = result
            self.check(f"osg-route-{label}-returns-active-evidence",
                       result.get("error") is None and info["evidence_count"] > 0,
                       {"args": args, **info, "evidence_status": result.get("evidence_status"),
                        "protocol": result.get("mcp_protocol")})
        keys = [
            [(item.get("target_id") or self.active["target_id"], item.get("chunk_id"))
             for item in results[label].get("evidence", [])]
            for label in ("default", "active-library-alias", "active-target-id")
        ]
        self.check("osg-alias-target-id-evidence-identity-equal",
                   keys[0] == keys[1] == keys[2],
                   {"target_ids": [self.active["target_id"]] * 3,
                    "evidence_identity_sequences": keys})

        wrong_version_cases = [
            ("default", {"query": QUERY_OSG, "version": "9.99"}),
            ("library", {"query": QUERY_OSG, "library": "osg", "version": "9.99"}),
            ("target_id", {"query": QUERY_OSG, "target_id": self.active["target_id"], "version": "9.99"}),
        ]
        for label, args in wrong_version_cases:
            result = await self.mcp_call("search_docs", args)
            self.check(f"wrong-version-{label}-invalid-target-no-evidence",
                       result.get("error") == "invalid_target" and not result.get("evidence"),
                       {"args": args, "error": result.get("error"),
                        "evidence_count": len(result.get("evidence", [])),
                        "evidence_status": result.get("evidence_status")})

        osg_policy = self.access_dir / f"{self.active['corpus_id']}.json"
        self.write_policy(self.active["corpus_id"], "internal", [])
        denied = await self.mcp_call("search_docs", {"query": QUERY_OSG})
        self.check("osg-acl-denied-no-evidence",
                   denied.get("error") == "forbidden" and not denied.get("evidence"),
                   {"error": denied.get("error"), "evidence_count": len(denied.get("evidence", [])),
                    "policy_sha256": sha256(osg_policy.read_bytes())})
        directory = await self.mcp_call("list_corpora", {})
        corpus_ids = [item.get("corpus_id") for item in directory.get("corpora", [])]
        self.check("osg-acl-denied-hidden-from-corpus-list", self.active["corpus_id"] not in corpus_ids,
                   {"count": directory.get("count"), "visible_corpus_ids": corpus_ids})

        other = await self.mcp_call("search_docs", {
            "query": QUERY_3DTILES, "library": "ogc-3dtiles", "version": "1.1", "top_k": 5,
        })
        other_info = self.ids(other)
        expected_other = "ogc-3dtiles-1.1-blocks-split-candidate-003"
        self.check("other-library-remains-searchable-after-osg-denial",
                   other.get("error") is None and other_info["evidence_count"] > 0
                   and set(other_info["evidence_target_ids"]) == {expected_other},
                   {**other_info, "error": other.get("error"), "expected_target_id": expected_other})
        await self.close_mcp_session()
        self.stop_mcp()

    async def run_managed_batch(self) -> None:
        await self.wait_manager()
        # OSG stays authorized for this isolated process; managed files are hidden until
        # a group or file-specific policy is written after the HTTP upload.
        self.write_policy(self.active["corpus_id"], "public", ["*"])
        await self.connect_mcp(mode="managed")
        self.check("managed-tools-present",
                   {"list_managed_files", "search_docs"}.issubset(self.tool_names),
                   {"tools": self.tool_names})
        initial = await self.mcp_call("list_managed_files", {})
        self.check("managed-list-empty-before-upload", not initial.get("files"),
                   {"files": initial.get("files", []), "count": initial.get("count")})

        v1 = f"QuartzWidget installation guide. {MARKER_V1}. Current state belongs to release one.\n".encode()
        v2 = f"ObsidianWidget migration guide. {MARKER_V2}. Current state belongs to release two.\n".encode()
        (self.input_dir / "guide-v1.txt").write_bytes(v1)
        (self.input_dir / "guide-v2.txt").write_bytes(v2)
        submitted = self.upload(v1, "managed-guide.txt")
        file_id = submitted["file_id"]
        self.created_file_ids.add(file_id)
        job1 = self.wait_job(submitted["job_id"])
        self.check("managed-http-upload-completed", job1["job"]["status"] == "completed",
                   {"job_id": submitted["job_id"], "status": job1["job"].get("status"),
                    "phase": job1["job"].get("phase"), "progress": job1["job"].get("progress"),
                    "collection": job1["job"].get("resources", {}).get("collection")})
        record1 = self.http.get(f"{self.manager_url}/api/files/{file_id}").json()
        version1 = record1["version"]
        version_id1 = record1["active_version_id"]
        resources1 = self.http.get(f"{self.manager_url}/api/files/{file_id}/resources").json()
        self.check("managed-upload-production-dense-bm25-hash", resources1.get("consistent") is True
                   and resources1.get("source_sha256_matches") is True
                   and resources1.get("bm25_rows") == resources1.get("dense_points") == resources1.get("chunk_count"),
                   {key: resources1.get(key) for key in ("source_sha256_matches", "bm25_rows", "dense_points",
                                                           "chunk_count", "consistent", "version_id")})

        hidden = await self.mcp_call("list_managed_files", {})
        self.check("managed-file-hidden-without-policy", all(item.get("file_id") != file_id
                   for item in hidden.get("files", [])),
                   {"count": hidden.get("count"), "file_id_present": any(
                       item.get("file_id") == file_id for item in hidden.get("files", []))})
        self.write_policy("managed-files", "internal", [AUTHORIZED])
        visible = await self.mcp_call("list_managed_files", {})
        names = [(item.get("file_id"), item.get("version"), item.get("version_id"))
                 for item in visible.get("files", [])]
        self.check("managed-http-upload-dynamically-discovered-without-mcp-restart",
                   any(item.get("file_id") == file_id and item.get("version_id") == version_id1
                       for item in visible.get("files", [])),
                   {"mcp_pid": self.lifecycle.get("pid"), "mcp_process_start_count": 1,
                    "managed_file_rows": names})
        found_v1 = await self.mcp_call("search_docs", {
            "query": "QuartzWidget installation release one", "file_id": file_id,
        })
        info_v1 = self.ids(found_v1)
        self.check("managed-mcp-search-v1-live-http-retrieval",
                   found_v1.get("error") is None and info_v1["evidence_count"] > 0
                   and info_v1["text_contains_v1"] and info_v1["managed_version_ids"] == [version_id1]
                   and info_v1["managed_source_sha256s"] == [sha256(v1)],
                   {**info_v1, "managed_file": found_v1.get("managed_file"),
                    "expected_version_id": version_id1, "expected_source_sha256": sha256(v1)})

        group_only_content = b"StarlightWidget group-reader guide, available to the managed-files group.\n"
        (self.input_dir / "group-only.txt").write_bytes(group_only_content)
        group_upload = self.upload(group_only_content, "group-only.txt")
        group_file_id = group_upload["file_id"]
        self.created_file_ids.add(group_file_id)
        group_job = self.wait_job(group_upload["job_id"])
        self.check("managed-second-group-file-upload-completed",
                   group_job["job"].get("status") == "completed",
                   {"job_id": group_upload["job_id"], "status": group_job["job"].get("status")})
        group_search = await self.mcp_call("search_docs", {
            "query": "StarlightWidget group-reader guide", "file_id": group_file_id,
        })
        group_search_info = self.ids(group_search)
        self.check("managed-group-allow-applies-to-second-file",
                   group_search.get("error") is None and group_search_info["evidence_count"] > 0,
                   {"file_id": group_file_id, **group_search_info})

        # Inject a real manager job failure after build/verification but before active switch.
        self.write_fault("activating")
        failed_submit = self.upload(v2, "managed-guide-v2.txt", replace_file_id=file_id)
        failed = self.wait_job(failed_submit["job_id"])
        self.check("managed-replace-failed-before-active-switch", failed["job"]["status"] == "failed",
                   {"job_id": failed_submit["job_id"], "status": failed["job"].get("status"),
                    "phase": failed["job"].get("phase"), "retryable": failed["job"].get("retryable"),
                    "reason_code_only": bool(failed["job"].get("reason"))})
        after_failed = self.http.get(f"{self.manager_url}/api/files/{file_id}").json()
        fail_search = await self.mcp_call("search_docs", {
            "query": "QuartzWidget installation release one", "file_id": file_id,
        })
        fail_info = self.ids(fail_search)
        self.check("failed-replace-retains-old-version-and-search-evidence",
                   after_failed.get("active_version_id") == version_id1 and fail_info["text_contains_v1"]
                   and not fail_info["text_contains_v2"]
                   and fail_info["managed_version_ids"] == [version_id1],
                   {"active_version_id": after_failed.get("active_version_id"),
                    "expected_version_id": version_id1, **fail_info})

        succeeded_submit = self.upload(v2, "managed-guide-v2.txt", replace_file_id=file_id)
        succeeded = self.wait_job(succeeded_submit["job_id"])
        self.check("managed-replace-v2-completed", succeeded["job"]["status"] == "completed",
                   {"job_id": succeeded_submit["job_id"], "status": succeeded["job"].get("status"),
                    "phase": succeeded["job"].get("phase"),
                    "collection": succeeded["job"].get("resources", {}).get("collection")})
        active2 = self.http.get(f"{self.manager_url}/api/files/{file_id}").json()
        version_id2 = active2["active_version_id"]
        v2_result = await self.mcp_call("search_docs", {
            "query": "ObsidianWidget migration release two", "file_id": file_id,
        })
        info_v2 = self.ids(v2_result)
        self.check("managed-mcp-search-uses-only-successful-active-v2",
                   active2.get("version") != version1 and version_id2 != version_id1
                   and info_v2["evidence_count"] > 0 and info_v2["text_contains_v2"]
                   and not info_v2["text_contains_v1"] and info_v2["managed_version_ids"] == [version_id2]
                   and info_v2["managed_source_sha256s"] == [sha256(v2)],
                   {"active_version": active2.get("version"), "active_version_id": version_id2,
                    "previous_version": version1, "previous_version_id": version_id1,
                    "expected_sha256": sha256(v2), **info_v2})
        old_version = await self.mcp_call("search_docs", {
            "query": "QuartzWidget installation release one", "file_id": file_id,
            "version": version1,
        })
        self.check("old-managed-version-request-rejected-without-evidence",
                   old_version.get("error") == "invalid_target"
                   and not old_version.get("evidence"),
                   {"error": old_version.get("error"), "evidence_count": len(old_version.get("evidence", [])),
                    "managed_file": old_version.get("managed_file"), "requested_version": version1})

        exact_id = f"managed-file-{file_id}"
        self.write_policy(exact_id, "internal", [])
        denied_list = await self.mcp_call("list_managed_files", {})
        denied_search = await self.mcp_call("search_docs", {
            "query": "ObsidianWidget migration release two", "file_id": file_id,
        })
        combined_denial = json.dumps({"list": denied_list, "search": denied_search}, ensure_ascii=False)
        self.check("per-file-deny-overrides-group-allow-and-hides-internal-details",
                   all(item.get("file_id") != file_id for item in denied_list.get("files", []))
                   and any(item.get("file_id") == group_file_id for item in denied_list.get("files", []))
                   and denied_search.get("error") == "file_not_available"
                   and not denied_search.get("evidence")
                   and "managed-guide.txt" not in combined_denial
                   and MARKER_V2 not in combined_denial,
                   {"list_count": denied_list.get("count"),
                    "visible_file_ids": [item.get("file_id") for item in denied_list.get("files", [])],
                    "expected_surviving_group_file_id": group_file_id,
                    "search_error": denied_search.get("error"),
                    "search_evidence_count": len(denied_search.get("evidence", [])),
                    "display_name_leaked": "managed-guide.txt" in combined_denial,
                    "body_leaked": MARKER_V2 in combined_denial})
        (self.access_dir / f"{exact_id}.json").unlink(missing_ok=True)
        self.write_policy("managed-files", "internal", [])
        group_denied = await self.mcp_call("search_docs", {
            "query": "ObsidianWidget migration release two", "file_id": file_id,
        })
        self.check("managed-group-policy-revocation-dynamic-without-restart",
                   group_denied.get("error") == "file_not_available"
                   and not group_denied.get("evidence"),
                   {"error": group_denied.get("error"), "evidence_count": len(group_denied.get("evidence", [])),
                    "mcp_pid": self.lifecycle.get("pid"), "mcp_process_start_count": 1})
        # Restore the group grant, then verify malformed policy snapshots deny the full request.
        self.write_policy("managed-files", "internal", [AUTHORIZED])
        (self.access_dir / "managed-files.json").write_text("{broken", encoding="utf-8")
        corrupt_list = await self.mcp_call("list_managed_files", {})
        corrupt_search = await self.mcp_call("search_docs", {
            "query": "ObsidianWidget migration release two", "file_id": file_id,
        })
        self.check("corrupt-acl-snapshot-fails-closed-no-evidence",
                   corrupt_list.get("error") == "unavailable"
                   and corrupt_search.get("error") == "file_not_available"
                   and not corrupt_search.get("evidence"),
                   {"list_error": corrupt_list.get("error"), "search_error": corrupt_search.get("error"),
                    "search_evidence_count": len(corrupt_search.get("evidence", []))})
        self.write_policy("managed-files", "internal", [AUTHORIZED])

        delete_response = self.http.delete(f"{self.manager_url}/api/files/{quote(file_id, safe='')}")
        delete_response.raise_for_status()
        delete_submit = delete_response.json()
        deleted = self.wait_job(delete_submit["job_id"])
        self.check("managed-http-delete-completed", deleted["job"]["status"] == "deleted",
                   {"job_id": delete_submit["job_id"], "status": deleted["job"].get("status"),
                    "cleanup_verified": deleted["job"].get("resources", {}).get("cleanup_verified"),
                    "cleanup_details": deleted["job"].get("resources", {}).get("cleanup_details", [])})
        after_delete_list = await self.mcp_call("list_managed_files", {})
        after_delete_search = await self.mcp_call("search_docs", {
            "query": "ObsidianWidget migration release two", "file_id": file_id,
        })
        self.check("deleted-file-unavailable-through-mcp-without-evidence",
                   all(item.get("file_id") != file_id for item in after_delete_list.get("files", []))
                   and after_delete_search.get("error") == "file_not_available"
                   and not after_delete_search.get("evidence"),
                   {"list_contains_file": any(item.get("file_id") == file_id
                                                for item in after_delete_list.get("files", [])),
                    "authorized_sibling_still_visible": any(
                        item.get("file_id") == group_file_id for item in after_delete_list.get("files", [])),
                    "search_error": after_delete_search.get("error"),
                    "search_evidence_count": len(after_delete_search.get("evidence", []))})

        self.delete_managed_file(group_file_id)
        details = deleted["job"].get("resources", {}).get("cleanup_details", [])
        residual_paths = [item["resource"] for item in details if item.get("type") == "path"
                          and (self.runtime_root / item["resource"]).exists()]
        residual_collections = [item["resource"] for item in details if item.get("type") == "collection"
                                and self.qdrant.collection_exists(item["resource"])]
        self.check("management-api-delete-leaves-no-owned-resource-residuals",
                   not residual_paths and not residual_collections
                   and deleted["job"].get("resources", {}).get("cleanup_verified", 0) > 0,
                   {"residual_owned_paths": residual_paths,
                    "residual_owned_collections": residual_collections,
                    "cleanup_verified": deleted["job"].get("resources", {}).get("cleanup_verified"),
                    "sibling_file_deleted_through_management_api": group_file_id})
        resources_response = self.http.get(f"{self.manager_url}/api/files/{file_id}/resources")
        self.check("deleted-resources-endpoint-refuses-tombstone", resources_response.status_code == 404,
                   {"status": resources_response.status_code})
        await self.close_mcp_session()
        self.stop_mcp()
        self.stop_manager()

    def delete_managed_file(self, file_id: str) -> dict:
        response = self.http.delete(f"{self.manager_url}/api/files/{quote(file_id, safe='')}")
        response.raise_for_status()
        submitted = response.json()
        done = self.wait_job(submitted["job_id"])
        self.check("managed-http-delete-job-completed", done["job"].get("status") == "deleted",
                   {"file_id": file_id, "job_id": submitted["job_id"],
                    "status": done["job"].get("status"),
                    "cleanup_verified": done["job"].get("resources", {}).get("cleanup_verified")})
        return done["job"]

    def stop_process(self, mode: str) -> dict:
        process = self.mcp if mode == "mcp" else self.manager
        if process is None:
            return {"stopped": True, "port_closed": True}
        port = self.mcp_port if mode == "mcp" else self.manager_port
        process.terminate()
        try:
            code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=10)
        log_handle = self.mcp_log if mode == "mcp" else self.manager_log
        if log_handle:
            log_handle.close()
        closed = False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    time.sleep(0.1)
            except OSError:
                closed = True
                break
        return {"stopped": process.poll() is not None, "exit_code": code,
                "pid": process.pid, "port_closed": closed}

    def stop_mcp(self) -> None:
        result = self.stop_process("mcp")
        self.mcp = None
        self.record("mcp-process-stopped", "pass" if result["stopped"] and result["port_closed"] else "fail", result)
        if not result["stopped"] or not result["port_closed"]:
            raise RuntimeError(f"MCP cleanup failed: {result}")

    def stop_manager(self) -> None:
        result = self.stop_process("manager")
        self.manager = None
        self.record("manager-process-stopped", "pass" if result["stopped"] and result["port_closed"] else "fail", result)
        if not result["stopped"] or not result["port_closed"]:
            raise RuntimeError(f"manager cleanup failed: {result}")

    def verify_project_assets_unchanged(self) -> None:
        registry_sha = sha256((REPO / "data/indexes/index-registry.json").read_bytes())
        bm25 = resolve_project_path(REPO, IndexRegistryStore(REPO / "data/indexes/index-registry.json").active().bm25_path)
        bm25_sha = sha256(bm25.read_bytes())
        collections = self.qdrant.get_collections().collections
        remaining_new = sorted(item.name for item in collections
                               if item.name not in self.initial_collections)
        self.check("cleanup-and-project-fingerprint-final", registry_sha == self.active["registry_sha256"]
                   and bm25_sha == self.active["bm25_sha256"]
                   and not remaining_new,
                   {"registry_sha256_before": self.active["registry_sha256"],
                    "registry_sha256_after": registry_sha,
                    "bm25_sha256_before": self.active["bm25_sha256"],
                    "bm25_sha256_after": bm25_sha,
                    "remaining_new_qdrant_collections": remaining_new,
                    "runtime_root_retained_for_logs": self.runtime_root.exists()})

    def write_summary(self) -> None:
        passed = sum(item["status"] == "pass" for item in self.events)
        failed = sum(item["status"] == "fail" for item in self.events)
        summary = {
            "title": "OSG routing and managed files through stateful MCP",
            "run_id": self.run_id,
            "protocol_target": "2025-11-25",
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "project_root": str(REPO),
            "isolated_runtime_root": str(self.runtime_root),
            "initial_active": self.active,
            "checks_passed": passed,
            "checks_failed": failed,
            "acceptance_jsonl": str(self.log_path),
            "limitations": [
                "OSG used the real active Qdrant collection, BM25 database, local embedding and retrieval service; optional reranking was disabled to bound model load.",
                "MCP calls used actual Streamable HTTP clients and separate local service processes; this is a Windows-host local rehearsal, not production IAM or deployment acceptance.",
                "Managed upload, replace and delete used a unique isolated root and real Qdrant/local embedding; no default registry, corpus, indexes, or configs/access policy was changed.",
                "Search-result body semantics were checked only for unique test markers and evidence identity; this is not general retrieval-quality or answer-generation evaluation.",
            ],
        }
        (self.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                                       encoding="utf-8")

    def close(self) -> None:
        self.http.close()
        if self.qdrant is not None:
            self.qdrant.close()

    def cleanup_managed_resources(self) -> None:
        """Delete test-owned managed files through the live management HTTP API."""
        if self.manager is None or self.manager.poll() is not None:
            return
        try:
            response = self.http.get(f"{self.manager_url}/api/files")
            response.raise_for_status()
            rows = response.json().get("files", [])
            for row in rows:
                file_id = row.get("file_id")
                if file_id not in self.created_file_ids or row.get("managed") is not True:
                    continue
                if row.get("status") == "deleted":
                    continue
                deleted = self.http.delete(
                    f"{self.manager_url}/api/files/{quote(file_id, safe='')}"
                )
                deleted.raise_for_status()
                submit = deleted.json()
                completed = self.wait_job(submit["job_id"])
                self.check("finally-cleanup-through-management-api",
                           completed["job"].get("status") == "deleted",
                           {"job_id": submit["job_id"], "status": completed["job"].get("status"),
                            "cleanup_verified": completed["job"].get("resources", {}).get("cleanup_verified")})
        except Exception as exc:
            self.record("finally-cleanup-through-management-api", "fail",
                        {"error_type": type(exc).__name__})


async def run(args: argparse.Namespace) -> int:
    acceptance = Acceptance()
    try:
        acceptance.baseline()
        if args.batch in {"all", "osg"}:
            await acceptance.run_osg_batch()
        if args.batch in {"all", "managed"}:
            await acceptance.run_managed_batch()
        acceptance.verify_project_assets_unchanged()
        acceptance.write_summary()
        return 0
    finally:
        await acceptance.close_mcp_session()
        acceptance.cleanup_managed_resources()
        if acceptance.mcp is not None:
            try:
                acceptance.stop_mcp()
            except Exception:
                pass
        if acceptance.manager is not None:
            try:
                acceptance.stop_manager()
            except Exception:
                pass
        acceptance.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", choices=("all", "osg", "managed"), default="all")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
