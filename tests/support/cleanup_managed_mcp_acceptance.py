"""Delete leftover managed acceptance files through their own HTTP API."""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from qdrant_client import QdrantClient

from wenjin.qdrant_connection import create_qdrant_client
from wenjin.settings import Settings

REPO = Path(__file__).resolve().parents[2]
HELPER = Path(__file__).resolve().parents[1] / "acceptance" / "managed_mcp_acceptance_server.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    evidence = args.evidence_dir.resolve()
    expected_parent = (REPO / "artifacts/iterations/2026-10-06-osg-managed-mcp").resolve()
    if not root.is_relative_to(expected_parent) or root.name != "runtime-root":
        raise ValueError("refusing to clean a root outside the managed MCP acceptance workspace")
    if not evidence.is_relative_to(expected_parent):
        raise ValueError("cleanup evidence directory must stay inside this acceptance workspace")
    evidence.mkdir(parents=True, exist_ok=True)
    log_path = evidence / "cleanup-cases.jsonl"
    if log_path.exists():
        raise FileExistsError(log_path)
    token = "cleanup-" + secrets.token_urlsafe(32)
    host, port = "127.0.0.1", free_port()
    base = f"http://{host}:{port}"
    log_handle = (evidence / "cleanup-manager-server.log").open("wb")
    command = [sys.executable, str(HELPER), "manager", "--root", str(root),
               "--host", host, "--port", str(port), "--admin-token", token]
    process = subprocess.Popen(command, cwd=REPO, stdout=log_handle,
                               stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    client = httpx.Client(timeout=httpx.Timeout(30, connect=3), trust_env=False,
                          headers={"Authorization": f"Bearer {token}"})
    qdrant: QdrantClient | None = None
    events = []

    def record(case: str, status: str, detail: dict) -> None:
        event = {"time_utc": datetime.now(timezone.utc).isoformat(), "case": case,
                 "status": status, "detail": detail}
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            stream.flush()
        events.append(event)

    def wait_job(job_id: str) -> dict:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = client.get(f"{base}/api/jobs/{quote(job_id, safe='')}")
            response.raise_for_status()
            job = response.json()
            if job.get("status") in {"deleted", "failed", "cancelled", "completed", "duplicate"}:
                return job
            time.sleep(0.15)
        raise TimeoutError(f"cleanup job timed out: {job_id}")

    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"manager process exited with {process.returncode}")
            try:
                health = client.get(f"{base}/api/health")
                if health.status_code == 200 and health.json().get("status") == "ready":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError("manager API did not become ready")
        record("manager-api-ready", "pass", {"status": health.status_code, "pid": process.pid})
        settings = Settings()
        qdrant = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        prefix = {item.name for item in qdrant.get_collections().collections}
        listing = client.get(f"{base}/api/files")
        listing.raise_for_status()
        managed = [item for item in listing.json().get("files", [])
                   if item.get("managed") is True and item.get("file_id")]
        owned_resources: dict[str, dict] = {}
        for item in managed:
            file_id = item["file_id"]
            response = client.get(f"{base}/api/files/{quote(file_id, safe='')}/resources")
            response.raise_for_status()
            owned_resources[file_id] = response.json()
        record("manager-owned-files-discovered", "pass", {
            "managed_row_count": len(managed),
            "file_ids": [item["file_id"] for item in managed],
            "active_collections": {key: value.get("dense_collection") for key, value in owned_resources.items()},
            "action": "only these manager-owned rows in this unique root are submitted for delete",
        })
        for item in managed:
            file_id = item["file_id"]
            deleted = client.delete(f"{base}/api/files/{quote(file_id, safe='')}")
            deleted.raise_for_status()
            submitted = deleted.json()
            job = wait_job(submitted["job_id"])
            record("manager-api-cleanup", "pass" if job.get("status") == "deleted" else "fail", {
                "file_id": file_id,
                "job_id": submitted["job_id"],
                "status": job.get("status"),
                "cleanup_verified": job.get("resources", {}).get("cleanup_verified"),
                "cleanup_details": job.get("resources", {}).get("cleanup_details", []),
            })
            if job.get("status") != "deleted":
                raise RuntimeError(f"manager API cleanup did not delete {file_id}")
        remaining = client.get(f"{base}/api/files")
        remaining.raise_for_status()
        remaining_managed = [item for item in remaining.json().get("files", [])
                             if item.get("managed") is True and item.get("status") != "deleted"]
        now = {item.name for item in qdrant.get_collections().collections}
        removed_new = sorted(prefix - now)
        new_collections = sorted(now - prefix)
        resource_paths = {
            file_id: {
                "raw_root_exists": (root / "raw_data" / f"file-management-{file_id}").exists(),
                "version_root_exists": (root / "data/file-management/versions" / file_id).exists(),
                "index_root_exists": (root / "data/file-management/indexes" / file_id).exists(),
                "collection_exists": bool(collection and qdrant.collection_exists(collection)),
            }
            for file_id, collection in ((key, value.get("dense_collection"))
                                        for key, value in owned_resources.items())
        }
        no_owned_residuals = all(
            not any(paths.values()) for paths in resource_paths.values()
        )
        cleanup_pass = not remaining_managed and no_owned_residuals
        record("cleanup-postconditions", "pass" if cleanup_pass else "fail", {
            "remaining_active_managed_file_ids": [item.get("file_id") for item in remaining_managed],
            "collections_removed_during_cleanup": removed_new,
            "new_collections_during_cleanup": new_collections,
            "owned_resource_paths": resource_paths,
            "runtime_root_preserved": root.exists(),
        })
        return 0 if cleanup_pass else 2
    finally:
        client.close()
        if qdrant is not None:
            qdrant.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        log_handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
