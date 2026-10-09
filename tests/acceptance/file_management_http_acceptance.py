"""三批真实HTTP文件管理验收；只访问显式给定的隔离root和独立collection。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import pymupdf as fitz
from qdrant_client import QdrantClient

from wenjin.settings import Settings


class Acceptance:
    def __init__(self, root: Path, evidence_dir: Path):
        self.root = root.resolve()
        self.evidence_dir = evidence_dir.resolve()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.input_dir = self.evidence_dir / "inputs"
        self.input_dir.mkdir(exist_ok=True)
        self.control = self.evidence_dir / "fault-control.json"
        self.ready = self.evidence_dir / "pause-ready.json"
        self.release = self.evidence_dir / "pause-release"
        self.log_path = self.evidence_dir / "acceptance-cases.jsonl"
        if self.log_path.exists():
            raise FileExistsError(f"refusing to overwrite evidence: {self.log_path}")
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError(f"managed root must be new or empty: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.legacy_id, self.legacy_source, self.legacy_manifest = self.make_legacy_fixture()
        self.host, self.port = "127.0.0.1", self._free_port()
        self.base = f"http://{self.host}:{self.port}"
        self.process: subprocess.Popen | None = None
        self.http = httpx.Client(timeout=httpx.Timeout(20, connect=3), trust_env=False)
        settings = Settings()
        self.qdrant = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        self.initial_collections = {item.name for item in self.qdrant.get_collections().collections}
        self.events: list[dict] = []

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def record(self, batch: str, case: str, status: str, detail: dict) -> None:
        event = {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "batch": batch,
            "case": case,
            "status": status,
            "detail": detail,
        }
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.events.append(event)

    def start_server(self, *, fault: bool = True) -> None:
        command = [
            sys.executable,
            str(Path(__file__).with_name("file_management_acceptance_server.py")),
            "--root", str(self.root), "--host", self.host, "--port", str(self.port),
        ]
        if fault:
            command.extend(["--fault-control", str(self.control)])
        log_stream = (self.evidence_dir / f"server-{self.port}.log").open("ab")
        # Start outside the repository root to exercise the installed package entry.
        self.process = subprocess.Popen(command, cwd=self.evidence_dir,
                                        stdout=log_stream, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("acceptance server exited before health became ready")
            try:
                response = self.http.get(f"{self.base}/api/health")
                if response.status_code == 200 and response.json().get("status") == "ready":
                    self.record("setup", "real-http-server-started", "pass", {
                        "pid": self.process.pid, "command": command,
                        "cwd": str(self.evidence_dir), "health_status": response.status_code,
                        "health": response.json(), "managed_root": str(self.root),
                    })
                    initial_list = self.http.get(f"{self.base}/api/files")
                    initial_list.raise_for_status()
                    rows = initial_list.json()["files"]
                    self.record("setup", "initial-list-read-only", "pass", {
                        "status": initial_list.status_code, "row_count": len(rows),
                        "legacy_count": sum(row.get("managed") is False for row in rows),
                    })
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError("acceptance server health did not become ready")

    def stop_server(self, *, crash: bool = False) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        if crash:
            self.process.terminate()
            self.process.wait(timeout=15)
        else:
            self.process.terminate()
            self.process.wait(timeout=30)
        self.process = None

    def control_fault(self, phase: str, action: str = "raise") -> None:
        payload = {"phase": phase, "action": action}
        if action == "pause":
            self.ready.unlink(missing_ok=True)
            self.release.unlink(missing_ok=True)
            payload.update(ready_file=str(self.ready), release_file=str(self.release))
        temporary = self.control.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, self.control)

    def wait_ready(self, timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ready.is_file():
                return json.loads(self.ready.read_text(encoding="utf-8"))
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("server exited before reaching the injected pause")
            time.sleep(0.05)
        raise TimeoutError("server did not reach injected pause")

    def wait_job(self, job_id: str, *, terminal: set[str] | None = None, timeout: float = 300) -> dict:
        terminal = terminal or {"completed", "duplicate", "failed", "deleted", "cancelled"}
        deadline = time.monotonic() + timeout
        observed = []
        while time.monotonic() < deadline:
            response = self.http.get(f"{self.base}/api/jobs/{quote(job_id, safe='')}")
            response.raise_for_status()
            job = response.json()
            stamp = (job.get("status"), job.get("phase"), job.get("progress"))
            if not observed or observed[-1] != stamp:
                observed.append(stamp)
            if job.get("status") in terminal:
                return {"job": job, "observed": observed}
            time.sleep(0.1)
        raise TimeoutError(f"job {job_id} timed out; last states={observed[-5:]}")

    def upload(self, data: bytes, name: str) -> dict:
        response = self.http.post(
            f"{self.base}/api/files", content=data,
            headers={"X-File-Name": quote(name, safe=""), "Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        return response.json()

    def replace(self, file_id: str, data: bytes, name: str) -> dict:
        response = self.http.put(
            f"{self.base}/api/files/{quote(file_id, safe='')}", content=data,
            headers={"X-File-Name": quote(name, safe=""), "Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        return response.json()

    def search(self, file_id: str, query: str) -> httpx.Response:
        return self.http.post(f"{self.base}/api/search", json={"file_id": file_id, "query": query})

    @staticmethod
    def matching_evidence(response: httpx.Response, marker: str) -> bool:
        return any(marker.casefold() in str(item.get("text", "")).casefold()
                   for item in response.json().get("evidence", []))

    def resources(self, file_id: str) -> dict:
        response = self.http.get(f"{self.base}/api/files/{quote(file_id, safe='')}/resources")
        response.raise_for_status()
        return response.json()

    def make_inputs(self) -> dict[str, bytes]:
        txt = b"amberwidget refresh token; batch one text source.\n"
        html = ("<html><body><h1>cobaltwidget</h1><p>batch one HTML source unique phrase.</p>"
                "</body></html>").encode("utf-8")
        pdf_path = self.input_dir / "zirconium-manual.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "zirconiumwidget setup procedure batch one PDF source.")
        document.save(pdf_path)
        document.close()
        pdf = pdf_path.read_bytes()
        for name, payload in (("guide-中文.txt", txt), ("guide.html", html), ("manual.pdf", pdf)):
            (self.input_dir / name).write_bytes(payload)
        return {"txt": txt, "html": html, "pdf": pdf}

    def make_legacy_fixture(self) -> tuple[str, Path, Path]:
        corpus_id = "legacy-sample-1.0"
        relative = "reference.txt"
        raw_root = self.root / "raw_data" / "legacy-sample" / "1.0"
        source = raw_root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        content = b"legacyinventorywidget remains read-only until adopted.\n"
        source.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        corpus_dir = self.root / "corpora" / corpus_id
        corpus_dir.mkdir(parents=True, exist_ok=True)
        manifest = corpus_dir / "source-manifest.json"
        manifest.write_text(json.dumps({
            "corpus_id": corpus_id,
            "library": "legacy-sample",
            "version": "1.0",
            "license": "MIT",
            "source_url": "urn:acceptance:legacy-sample",
            "raw_data_subdir": "legacy-sample/1.0",
            "files": [{"path": relative, "raw_sha256": digest,
                       "bytes": len(content), "indexable": True}],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        legacy_id = f"legacy:{corpus_id}:{hashlib.sha256(relative.encode()).hexdigest()[:16]}"
        return legacy_id, source, manifest

    def verify_active(self, file_id: str) -> dict:
        resource = self.resources(file_id)
        assert resource["source_exists"] and resource["source_sha256_matches"]
        assert resource["manifest_exists"]
        assert resource["consistent"]
        assert resource["bm25_rows"] == resource["dense_points"] == resource["chunk_count"]
        return resource

    def verify_new_collections_only(self) -> list[str]:
        now = {item.name for item in self.qdrant.get_collections().collections}
        new = now - self.initial_collections
        assert all(name.startswith("fm_") for name in new), new
        return sorted(new)

    def run_batch_one(self) -> dict:
        inputs = self.make_inputs()
        created = []
        for label, name in (("txt", "guide-中文.txt"), ("html", "guide.html"), ("pdf", "manual.pdf")):
            submitted = self.upload(inputs[label], name)
            progress = self.wait_job(submitted["job_id"])
            assert progress["job"]["status"] == "completed", progress
            file_id = submitted["file_id"]
            resource = self.verify_active(file_id)
            marker = {"txt": "amberwidget", "html": "cobaltwidget",
                      "pdf": "zirconiumwidget"}[label]
            result = self.search(file_id, marker)
            result.raise_for_status()
            retrieval = result.json()["retrieval"]
            assert retrieval["dense_returned"] > 0 and retrieval["bm25_returned"] > 0
            assert self.matching_evidence(result, marker)
            resource["marker_search"] = {
                "dense_returned": retrieval["dense_returned"],
                "bm25_returned": retrieval["bm25_returned"],
                "marker_in_evidence": True,
            }
            created.append({"file_id": file_id, "name": name, "job": progress, "resources": resource})
            if label == "txt":
                self.record("1", "txt-upload-job-progress-and-resource-counts", "pass", {
                    "submitted": submitted, "observed_states": progress["observed"],
                    "resources": resource,
                })

        before = self.verify_new_collections_only()
        duplicate = self.upload(inputs["txt"], "renamed-duplicate.txt")
        assert duplicate["status"] == "duplicate"
        assert duplicate["duplicate_of"] == created[0]["file_id"]
        assert before == self.verify_new_collections_only()

        self.control_fault("building")
        failed_import = self.upload(b"retrywidget durable retry source", "retry.txt")
        failed = self.wait_job(failed_import["job_id"])
        assert failed["job"]["status"] == "failed" and failed["job"]["retryable"]
        retry_response = self.http.post(f"{self.base}/api/jobs/{failed_import['job_id']}/retry")
        retry_response.raise_for_status()
        retried = self.wait_job(retry_response.json()["job_id"])
        assert retried["job"]["status"] == "completed", retried
        retry_resources = self.verify_active(failed_import["file_id"])

        listing = self.http.get(f"{self.base}/api/files").json()["files"]
        assert any(row["file_id"] == created[0]["file_id"] and row["display_name"] == "guide-中文.txt"
                   and row["version"] == "v1" and row["status"] == "active" and row["chunk_count"] > 0
                   for row in listing)
        legacy_rows = [row for row in listing if row.get("managed") is False]
        assert len(legacy_rows) == 1
        legacy = legacy_rows[0]
        assert legacy["file_id"] == self.legacy_id and legacy["status"] == "legacy_unmanaged"
        assert legacy["display_name"] == "reference.txt" and legacy["version"] == "1.0"
        legacy_show = self.http.get(f"{self.base}/api/files/{quote(self.legacy_id, safe='')}")
        legacy_show.raise_for_status()
        assert legacy_show.json()["managed"] is False
        legacy_delete = self.http.delete(f"{self.base}/api/files/{quote(self.legacy_id, safe='')}")
        assert legacy_delete.status_code == 409
        legacy_replace = self.http.put(
            f"{self.base}/api/files/{quote(self.legacy_id, safe='')}",
            content=b"must not replace legacy", headers={"X-File-Name": "reference.txt"},
        )
        assert legacy_replace.status_code == 409
        before_adopt_hash = hashlib.sha256(self.legacy_source.read_bytes()).hexdigest()
        before_adopt_manifest = hashlib.sha256(self.legacy_manifest.read_bytes()).hexdigest()
        adopted = self.http.post(f"{self.base}/api/files/{quote(self.legacy_id, safe='')}/adopt")
        adopted.raise_for_status()
        adopted_job = self.wait_job(adopted.json()["job_id"])
        assert adopted_job["job"]["status"] == "completed"
        adopted_file_id = adopted_job["job"]["file_id"]
        assert adopted_file_id != self.legacy_id
        assert adopted_job["job"]["resources"]["legacy_may_remain_searchable"] is True
        legacy_search = self.search(adopted_file_id, "legacyinventorywidget")
        legacy_search.raise_for_status()
        assert self.matching_evidence(legacy_search, "legacyinventorywidget")
        assert hashlib.sha256(self.legacy_source.read_bytes()).hexdigest() == before_adopt_hash
        assert hashlib.sha256(self.legacy_manifest.read_bytes()).hexdigest() == before_adopt_manifest
        adopted_delete = self.http.delete(f"{self.base}/api/files/{adopted_file_id}")
        adopted_delete.raise_for_status()
        adopted_deleted = self.wait_job(adopted_delete.json()["job_id"])
        assert adopted_deleted["job"]["status"] == "deleted"
        assert self.legacy_source.is_file() and self.legacy_manifest.is_file()
        self.record("1", "legacy-inventory-readonly-adopt-copy-boundary", "pass", {
            "legacy_row": legacy, "show_status": legacy_show.status_code,
            "delete_legacy_status": legacy_delete.status_code,
            "replace_legacy_status": legacy_replace.status_code,
            "adopt_job": adopted_job, "managed_copy_deleted": adopted_deleted,
            "legacy_source_sha256_unchanged": True,
            "legacy_manifest_sha256_unchanged": True,
        })
        report = {"created": created, "duplicate": duplicate, "failed_import": failed,
                  "retried_import": retried, "retry_resources": retry_resources,
                  "list_count": len(listing), "legacy_unmanaged_count": len(legacy_rows),
                  "new_collections": self.verify_new_collections_only()}
        self.record("1", "format-list-duplicate-progress-failure-retry", "pass", report)
        return report

    def run_batch_two(self) -> dict:
        original = b"historydeletewidget old version retained for deletion."
        imported = self.upload(original, "delete-history.txt")
        first = self.wait_job(imported["job_id"])
        assert first["job"]["status"] == "completed"
        replacement = self.replace(imported["file_id"],
                                   b"historydeletewidget newer active version.", "delete-history.txt")
        second = self.wait_job(replacement["job_id"])
        assert second["job"]["status"] == "completed"
        active = self.verify_active(imported["file_id"])

        self.control_fault("deleting")
        deletion = self.http.delete(f"{self.base}/api/files/{imported['file_id']}")
        deletion.raise_for_status()
        failed = self.wait_job(deletion.json()["job_id"])
        assert failed["job"]["status"] == "failed" and failed["job"]["retryable"]
        unreadable = self.search(imported["file_id"], "historydeletewidget")
        assert unreadable.status_code >= 400

        retry = self.http.post(f"{self.base}/api/jobs/{deletion.json()['job_id']}/retry")
        retry.raise_for_status()
        deleted = self.wait_job(retry.json()["job_id"])
        assert deleted["job"]["status"] == "deleted", deleted
        details = deleted["job"]["resources"]["cleanup_details"]
        assert details and all(item["status"] == "deleted" for item in details)
        residual_paths = [item["resource"] for item in details if item["type"] == "path"
                          and (self.root / item["resource"]).exists()]
        residual_collections = [item["resource"] for item in details if item["type"] == "collection"
                                and self.qdrant.collection_exists(item["resource"])]
        assert not residual_paths, residual_paths
        assert not residual_collections, residual_collections
        assert deleted["job"]["resources"].get("legacy_may_remain_searchable") is False
        self.record("2", "historical-dual-index-delete-fail-retry-residual-check", "pass", {
            "old_version": first["job"], "new_version": second["job"], "active_resources_before_delete": active,
            "failed_delete": failed, "retry_job": deleted, "cleanup_details": details,
            "residual_paths": residual_paths, "residual_collections": residual_collections,
        })
        return deleted

    def run_batch_three(self) -> dict:
        original = b"oldamberwidget old version remains readable before activation."
        imported = self.upload(original, "replace.txt")
        first = self.wait_job(imported["job_id"])
        assert first["job"]["status"] == "completed"
        file_id = imported["file_id"]
        old_resource = self.verify_active(file_id)
        old_search = self.search(file_id, "oldamberwidget")
        old_search.raise_for_status()
        assert old_search.json()["evidence"]

        new_payload = b"newcobaltwidget new version replaces old marker."
        self.control_fault("activating")
        failed = self.replace(file_id, new_payload, "replace.txt")
        failed_job = self.wait_job(failed["job_id"])
        assert failed_job["job"]["status"] == "failed" and failed_job["job"]["retryable"]
        still_old = self.verify_active(file_id)
        old_after_failure = self.search(file_id, "oldamberwidget")
        old_after_failure.raise_for_status()
        assert old_after_failure.json()["evidence"]
        new_after_failure = self.search(file_id, "newcobaltwidget")
        new_after_failure.raise_for_status()
        assert not self.matching_evidence(new_after_failure, "newcobaltwidget")
        self.record("3", "failed-replacement-retains-searchable-old-version", "pass", {
            "failed_replacement": failed_job, "active_resources": still_old,
            "old_marker_search": old_after_failure.json(),
            "new_marker_absent_from_evidence": True,
            "new_marker_query_retrieval": new_after_failure.json()["retrieval"],
        })

        # 挂起重试建库期间并发查询；请求必须仍读到完整旧版本。
        self.control_fault("building", action="pause")
        retry = self.http.post(f"{self.base}/api/jobs/{failed['job_id']}/retry")
        retry.raise_for_status()
        pause = self.wait_ready(timeout=120)
        with ThreadPoolExecutor(max_workers=6) as pool:
            concurrent = list(pool.map(lambda _: self.search(file_id, "oldamberwidget"), range(6)))
        assert all(response.status_code == 200 and response.json()["evidence"] for response in concurrent)
        assert all(self.matching_evidence(response, "oldamberwidget") for response in concurrent)
        self.release.write_text("continue", encoding="utf-8")
        replacement_done = self.wait_job(retry.json()["job_id"])
        assert replacement_done["job"]["status"] == "completed"
        new_resource = self.verify_active(file_id)
        assert new_resource["version_id"] != old_resource["version_id"]
        new_search = self.search(file_id, "newcobaltwidget")
        new_search.raise_for_status()
        assert new_search.json()["evidence"] and self.matching_evidence(new_search, "newcobaltwidget")
        new_retrieval = new_search.json()["retrieval"]
        assert new_retrieval["dense_returned"] > 0 and new_retrieval["bm25_returned"] > 0
        old_after_success = self.search(file_id, "oldamberwidget")
        old_after_success.raise_for_status()
        assert not self.matching_evidence(old_after_success, "oldamberwidget")
        self.record("3", "failed-replacement-old-readable-success-new-only-concurrent-search", "pass", {
            "old_resources": old_resource, "failed_replacement": failed_job,
            "resources_after_failure": still_old, "old_search_after_failure": old_after_failure.json(),
            "new_marker_query_after_failure": new_after_failure.json(),
            "building_pause": pause, "concurrent_search_statuses": [r.status_code for r in concurrent],
            "successful_replacement": replacement_done, "new_resources": new_resource,
            "new_retrieval": new_retrieval,
            "old_marker_after_success": old_after_success.json(),
            "new_search": new_search.json(),
        })
        return {"file_id": file_id, "job_id": retry.json()["job_id"]}

    def run_crash_restart(self) -> dict:
        self.control_fault("building", action="pause")
        submitted = self.upload(b"crashresume durable pending marker", "crash-resume.txt")
        pause = self.wait_ready(timeout=120)
        running = self.http.get(f"{self.base}/api/jobs/{submitted['job_id']}").json()
        assert running["status"] == "running" and running["phase"] == "building"
        self.stop_server(crash=True)
        self.control.unlink(missing_ok=True)
        self.start_server(fault=False)
        resumed = self.wait_job(submitted["job_id"], timeout=300)
        assert resumed["job"]["status"] == "completed", resumed
        resource = self.verify_active(submitted["file_id"])
        self.record("3", "running-job-crash-restart-resumes-and-persists", "pass", {
            "before_crash": running, "pause": pause, "after_restart": resumed,
            "resources": resource,
        })
        return resumed

    def cleanup_isolated_managed_files(self) -> dict:
        response = self.http.get(f"{self.base}/api/files")
        response.raise_for_status()
        files = [row for row in response.json()["files"]
                 if row.get("managed", True) and row.get("status") != "deleted"]
        cleaned = []
        for record in files:
            delete = self.http.delete(f"{self.base}/api/files/{quote(record['file_id'], safe='')}")
            delete.raise_for_status()
            outcome = self.wait_job(delete.json()["job_id"])
            assert outcome["job"]["status"] == "deleted", outcome
            details = outcome["job"]["resources"]["cleanup_details"]
            assert all(item["status"] == "deleted" for item in details)
            assert not any(item["type"] == "path" and (self.root / item["resource"]).exists()
                           for item in details)
            assert not any(item["type"] == "collection" and self.qdrant.collection_exists(item["resource"])
                           for item in details)
            cleaned.append({"file_id": record["file_id"], "job": outcome["job"]})
        remaining = self.http.get(f"{self.base}/api/files").json()["files"]
        assert all(not row.get("managed", True) for row in remaining)
        new_collections = self.verify_new_collections_only()
        assert not new_collections, new_collections
        self.record("cleanup", "isolated-managed-collections-and-files-removed", "pass", {
            "cleaned_file_count": len(cleaned), "cleanup_jobs": cleaned,
            "remaining_legacy_inventory_count": len(remaining), "remaining_new_collections": new_collections,
        })
        return {"cleaned_file_count": len(cleaned), "remaining_new_collections": new_collections}

    def close(self) -> None:
        self.stop_server()
        self.http.close()
        self.qdrant.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path, help="new isolated project root")
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    acceptance = Acceptance(args.root, args.evidence_dir)
    try:
        acceptance.start_server()
        acceptance.run_batch_one()
        acceptance.run_batch_two()
        acceptance.run_batch_three()
        acceptance.run_crash_restart()
        acceptance.cleanup_isolated_managed_files()
        print(json.dumps({"status": "pass", "cases": len(acceptance.events),
                          "root": str(acceptance.root),
                          "report": str(acceptance.log_path)}, ensure_ascii=False))
    except Exception as error:
        acceptance.record("run", "unhandled", "fail", {"type": type(error).__name__, "error": str(error)})
        raise
    finally:
        acceptance.close()


if __name__ == "__main__":
    main()
