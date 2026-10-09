"""Remove only residual managed files from named isolated acceptance roots.

All deletions go through the real HTTP delete job and are reported with
independent Qdrant collection checks. The allowlist is deliberately fixed to
the earlier disposable run directories from this acceptance iteration.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from qdrant_client import QdrantClient

from wenjin.settings import Settings

ROOTS = (
    "live-20261006-011943-root",
    "live-20261006-012212-root",
    "live-20261006-012341-root",
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    artifact = repo / "artifacts/iterations/2026-10-06-file-management"
    evidence = artifact / "isolated-attempt-cleanup"
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "cleanup-cases.jsonl"
    if result_path.exists():
        raise FileExistsError(f"refusing to overwrite {result_path}")
    settings = Settings()
    qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    helper = Path(__file__).resolve().parents[1] / "acceptance" / "file_management_acceptance_server.py"

    def record(case: str, status: str, detail: dict) -> None:
        event = {"time_utc": datetime.now(timezone.utc).isoformat(), "case": case,
                 "status": status, "detail": detail}
        with result_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    try:
        for root_name in ROOTS:
            root = artifact / root_name
            if not root.is_dir():
                record(root_name, "skip", {"reason": "allowlisted root does not exist"})
                continue
            known_collections = set()
            run_evidence = artifact / root_name.replace("-root", "") / "acceptance-cases.jsonl"
            if run_evidence.is_file():
                known_collections.update(re.findall(r'fm_[0-9a-f]{12}_[0-9a-f]{12}',
                                                    run_evidence.read_text(encoding="utf-8")))
            port, host = free_port(), "127.0.0.1"
            base = f"http://{host}:{port}"
            command = [sys.executable, str(helper), "--root", str(root),
                       "--host", host, "--port", str(port)]
            log_path = evidence / f"server-{root_name}.log"
            log = log_path.open("ab")
            process = subprocess.Popen(command, cwd=evidence, stdout=log, stderr=subprocess.STDOUT)
            client = httpx.Client(timeout=httpx.Timeout(20, connect=3), trust_env=False)
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"server exited {process.returncode}: {log_path}")
                    try:
                        health = client.get(f"{base}/api/health")
                        if health.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    raise TimeoutError(f"server did not start: {log_path}")
                before = client.get(f"{base}/api/files")
                before.raise_for_status()
                managed = [row for row in before.json()["files"] if row.get("managed") is not False]
                cleanup = []
                failed = []
                for row in managed:
                    response = client.delete(f"{base}/api/files/{row['file_id']}")
                    if response.status_code not in (200, 202):
                        failed.append({"file_id": row["file_id"], "status": response.status_code,
                                       "body": response.text[:500]})
                        continue
                    job = response.json()
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        job_response = client.get(f"{base}/api/jobs/{job['job_id']}")
                        job_response.raise_for_status()
                        job = job_response.json()
                        if job.get("status") in {"deleted", "failed"}:
                            break
                        time.sleep(0.1)
                    cleanup.append({"file_id": row["file_id"], "job_id": job["job_id"],
                                    "status": job.get("status"),
                                    "cleanup_verified": job.get("resources", {}).get("cleanup_verified")})
                    if job.get("status") != "deleted":
                        failed.append({"file_id": row["file_id"], "job": job})
                after = client.get(f"{base}/api/files")
                after.raise_for_status()
                remaining_managed = [row for row in after.json()["files"] if row.get("managed") is not False]
                present_known_collections = sorted(
                    known_collections & {row.name for row in qdrant.get_collections().collections})
                ok = not failed and not remaining_managed and not present_known_collections
                record(root_name, "pass" if ok else "fail", {
                    "pid": process.pid, "command": command, "cwd": str(evidence),
                    "legacy_rows_retained": sum(row.get("managed") is False for row in after.json()["files"]),
                    "managed_rows_before": len(managed), "cleanup_jobs": cleanup,
                    "failed": failed, "remaining_managed": remaining_managed,
                    "run_collections_seen": sorted(known_collections),
                    "run_collections_still_present": present_known_collections,
                })
                if not ok:
                    return 2
            finally:
                client.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=30)
                log.close()
        return 0
    finally:
        qdrant.close()


if __name__ == "__main__":
    raise SystemExit(main())
