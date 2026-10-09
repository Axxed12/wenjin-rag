"""Read-only legacy inventory check for the default project root.

This launches the real file-management HTTP app in a subprocess and never
imports, adopts, replaces, or deletes an existing corpus asset. The DELETE and
PUT probes are expected to return 409 for the selected unmanaged book row.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    evidence = repo / "artifacts/iterations/2026-10-06-file-management/default-root-readonly"
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "acceptance-cases.jsonl"
    if result_path.exists():
        raise FileExistsError(f"refusing to overwrite {result_path}")
    host, port = "127.0.0.1", free_port()
    base = f"http://{host}:{port}"
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "acceptance" / "file_management_acceptance_server.py"),
               "--root", str(repo), "--host", host, "--port", str(port)]
    log = (evidence / "server.log").open("ab")
    process = subprocess.Popen(command, cwd=evidence, stdout=log, stderr=subprocess.STDOUT)

    def record(case: str, status: str, detail: dict) -> None:
        event = {"time_utc": datetime.now(timezone.utc).isoformat(), "batch": "legacy-boundary",
                 "case": case, "status": status, "detail": detail}
        with result_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    client = httpx.Client(timeout=httpx.Timeout(15, connect=2), trust_env=False)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"server exited early with code {process.returncode}; see {evidence / 'server.log'}")
            try:
                health = client.get(f"{base}/api/health")
                if health.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise TimeoutError(f"server not ready; see {evidence / 'server.log'}")
        record("default-root-server-ready", "pass", {
            "pid": process.pid, "command": command, "cwd": str(evidence),
            "health_status": health.status_code, "project_root": str(repo),
        })

        response = client.get(f"{base}/api/files")
        response.raise_for_status()
        rows = response.json()["files"]
        legacy = [row for row in rows if row.get("managed") is False]
        candidates = [row for row in legacy if "agent" in row.get("display_name", "").casefold()
                      and row.get("display_name", "").casefold().endswith(".pdf")]
        if not candidates:
            record("default-root-legacy-inventory", "fail", {
                "http_status": response.status_code, "row_count": len(rows),
                "legacy_count": len(legacy), "candidate_count": 0,
                "legacy_pdf_names": [r.get("display_name") for r in legacy
                                     if r.get("display_name", "").casefold().endswith(".pdf")],
            })
            return 2
        target = next((row for row in candidates if row.get("chunk_count") == 600), candidates[0])
        safe_fields = {key: target.get(key) for key in (
            "file_id", "display_name", "status", "version", "chunk_count", "chunk_count_source",
            "managed", "legacy_id", "library")}
        show = client.get(f"{base}/api/files/{target['file_id']}")
        delete = client.delete(f"{base}/api/files/{target['file_id']}")
        replace = client.put(f"{base}/api/files/{target['file_id']}",
                             content=b"must-not-be-imported", headers={"X-File-Name": "probe.pdf"})
        if show.status_code != 200 or delete.status_code != 409 or replace.status_code != 409:
            record("default-root-legacy-readonly", "fail", {
                "target": safe_fields, "show_status": show.status_code,
                "delete_status": delete.status_code, "replace_status": replace.status_code,
            })
            return 3
        record("default-root-legacy-inventory-and-readonly-boundary", "pass", {
            "http_status": response.status_code, "total_rows": len(rows),
            "legacy_rows": len(legacy), "target": safe_fields,
            "show_status": show.status_code, "delete_status": delete.status_code,
            "replace_status": replace.status_code,
            "note": "Only metadata and HTTP status recorded; source text, credentials, and manifest contents omitted.",
        })
        return 0
    finally:
        client.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
