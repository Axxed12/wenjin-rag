"""文件管理核心跨存储契约：SQLite登记、原文、BM25与Qdrant必须一起成立。"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from wenjin.file_management import FileManagementService


class MarkerEmbedder:
    """小型确定性向量器，仅用于隔离契约测试，不代表生产模型质量。"""

    dimension = 64

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in text.lower().split():
            index = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % self.dimension
            vector[index] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def _manager(root: Path, client: QdrantClient, *, fail=None) -> FileManagementService:
    return FileManagementService(
        root=root,
        qdrant_client=client,
        embedder=MarkerEmbedder(),
        embedding_model="isolated-marker-test-only",
        fault_hook=fail,
    )


def _upload(manager, path: Path, *, replace: str | None = None):
    return manager.submit_import(path, display_name=path.name, replace_file_id=replace)


def _dense_count(client: QdrantClient, collection: str) -> int:
    return client.count(collection_name=collection, exact=True).count


def _bm25_count(path: Path) -> int:
    with closing(sqlite3.connect(path)) as db:
        return int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])


def test_queued_job_resumes_and_duplicate_does_not_create_another_index(tmp_path):
    client = QdrantClient(":memory:")
    source = tmp_path / "guide.txt"
    source.write_text("amberwidget refresh token", encoding="utf-8")
    first = _manager(tmp_path, client)
    queued = _upload(first, source)
    assert queued.status == "queued" and queued.progress == 0

    # 关闭并重建 service，验证job从持久SQLite登记恢复，而非只存在于worker内存。
    first.close()
    resumed = _manager(tmp_path, client)
    pending = resumed.resume_pending_jobs()
    assert [item.job_id for item in pending] == [queued.job_id]
    completed = resumed.run_job(queued.job_id)
    assert completed.status == "completed" and completed.progress == 100, completed.reason

    snapshot = resumed.snapshot(queued.file_id)
    assert snapshot.chunk_count > 0
    assert snapshot.source_raw_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert snapshot.source_path.is_file()
    assert _bm25_count(snapshot.bm25_path) == snapshot.chunk_count
    assert _dense_count(client, snapshot.dense_collection) == snapshot.chunk_count

    before = {item.name for item in client.get_collections().collections}
    duplicate = _upload(resumed, source)
    after = {item.name for item in client.get_collections().collections}
    assert duplicate.status == "duplicate"
    assert duplicate.duplicate_of == queued.file_id
    assert before == after
    assert [row.file_id for row in resumed.list_files(include_legacy=False)] == [queued.file_id]
    resumed.close()
    client.close()


def test_failed_replacement_keeps_old_searchable_then_switches_to_new_version(tmp_path):
    client = QdrantClient(":memory:")
    calls = {"fail": False}

    def fail_before_activation(phase, context):
        if phase == "activating" and calls["fail"]:
            calls["fail"] = False
            raise OSError("acceptance failpoint: before activation")

    manager = _manager(tmp_path, client, fail=fail_before_activation)
    old_source = tmp_path / "manual.txt"
    old_source.write_text("oldamberwidget retired marker", encoding="utf-8")
    old_job = _upload(manager, old_source)
    initial = manager.run_job(old_job.job_id)
    assert initial.status == "completed", initial.reason
    old_snapshot = manager.snapshot(old_job.file_id)
    old_search = manager.search(old_job.file_id, "oldamberwidget")
    assert old_search["evidence"]
    assert manager.get_file(old_job.file_id).active_version_id == old_snapshot.version_id

    new_source = tmp_path / "manual-new.txt"
    new_source.write_text("newcobaltwidget current marker", encoding="utf-8")
    calls["fail"] = True
    replacement = _upload(manager, new_source, replace=old_job.file_id)
    failed = manager.run_job(replacement.job_id)
    assert failed.status == "failed" and failed.retryable
    # 激活前失败必须保持旧版本原文、Hash和检索入口可用。
    assert manager.snapshot(old_job.file_id).version_id == old_snapshot.version_id
    assert manager.search(old_job.file_id, "oldamberwidget")["evidence"]

    retry = manager.submit_retry(replacement.job_id)
    completed = manager.run_job(retry.job_id)
    assert completed.status == "completed"
    new_snapshot = manager.snapshot(old_job.file_id)
    assert new_snapshot.version_id != old_snapshot.version_id
    assert new_snapshot.source_raw_sha256 == hashlib.sha256(new_source.read_bytes()).hexdigest()
    assert manager.search(old_job.file_id, "newcobaltwidget")["evidence"]
    assert manager.get_file(old_job.file_id).active_version_id == new_snapshot.version_id
    manager.close()
    client.close()


def test_failed_delete_is_unsearchable_and_retry_removes_all_managed_resources(tmp_path):
    client = QdrantClient(":memory:")
    calls = {"fail": True}

    def fail_first_cleanup(phase, context):
        if phase == "deleting" and calls["fail"]:
            calls["fail"] = False
            raise OSError("acceptance failpoint: deletion unavailable")

    manager = _manager(tmp_path, client, fail=fail_first_cleanup)
    source = tmp_path / "owned.txt"
    source.write_text("deletable zephyrmarker", encoding="utf-8")
    imported = _upload(manager, source)
    initial = manager.run_job(imported.job_id)
    assert initial.status == "completed", initial.reason
    snapshot = manager.snapshot(imported.file_id)
    original_paths = [snapshot.source_path, snapshot.manifest_path, snapshot.bm25_path]
    assert all(path.exists() for path in original_paths)
    assert client.collection_exists(snapshot.dense_collection)

    delete = manager.submit_delete(imported.file_id)
    failed = manager.run_job(delete.job_id)
    assert failed.status == "failed" and failed.retryable
    assert manager.get_file(imported.file_id).status == "deleting"
    with pytest.raises(KeyError):
        manager.search(imported.file_id, "zephyrmarker")

    retry = manager.submit_retry(delete.job_id)
    deleted = manager.run_job(retry.job_id)
    assert deleted.status == "deleted" and deleted.progress == 100, deleted.reason
    assert manager.get_file(imported.file_id).status == "deleted"
    assert not any(path.exists() for path in original_paths)
    assert not client.collection_exists(snapshot.dense_collection)
    assert deleted.resources.get("cleanup_verified", 0) >= 4
    assert imported.file_id not in {row.file_id for row in manager.list_files(include_legacy=False)}
    manager.close()
    client.close()


def test_same_managed_root_rejects_a_second_service_process_lock(tmp_path):
    client = QdrantClient(":memory:")
    first = _manager(tmp_path, client)
    with pytest.raises(RuntimeError, match="已有文件管理服务"):
        _manager(tmp_path, client)
    assert first.list_files(include_legacy=False) == []
    first.close()
    replacement = _manager(tmp_path, client)
    assert replacement.list_files(include_legacy=False) == []
    replacement.close()
    client.close()


def test_delete_tombstone_during_build_cancels_import_and_cleans_late_resources(tmp_path):
    client = QdrantClient(":memory:")
    entered_build = threading.Event()
    release_build = threading.Event()

    def hold_at_building(phase, context):
        if phase == "building":
            entered_build.set()
            if not release_build.wait(timeout=20):
                raise TimeoutError("test did not release build barrier")

    manager = _manager(tmp_path, client, fail=hold_at_building)
    source = tmp_path / "race.txt"
    source.write_text("racedocument ambermarker", encoding="utf-8")
    imported = _upload(manager, source)
    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(manager.run_job, imported.job_id)
        assert entered_build.wait(timeout=20), "import did not reach the controlled build boundary"

        # 删除提交必须立即写入tombstone，不能等向量化和磁盘I/O结束。
        delete = manager.submit_delete(imported.file_id)
        assert manager.get_file(imported.file_id).status == "deleting"
        release_build.set()
        import_result = running.result(timeout=40)

    assert import_result.status in {"failed", "cancelled"}
    assert manager.get_file(imported.file_id).status == "deleting"
    deleted = manager.run_job(delete.job_id)
    assert deleted.status == "deleted", deleted.reason
    assert manager.get_file(imported.file_id).status == "deleted"
    # 删除账本执行时必须包含build边界之后才创建的collection与路径。
    assert deleted.resources.get("cleanup_verified", 0) >= 3
    assert all(item["status"] == "deleted" for item in deleted.resources["cleanup_details"])
    for collection in client.get_collections().collections:
        assert not collection.name.startswith(f"fm_{imported.file_id[:12]}_")
    assert manager.get_job(imported.job_id).status == "cancelled"
    assert manager.run_job(imported.job_id).status == "cancelled"
    manager.close()
    client.close()


def test_concurrent_replacement_submissions_allow_only_one_pending_candidate(tmp_path):
    client = QdrantClient(":memory:")
    manager = _manager(tmp_path, client)
    original = tmp_path / "original.txt"
    original.write_text("baseline marker", encoding="utf-8")
    imported = _upload(manager, original)
    assert manager.run_job(imported.job_id).status == "completed"

    replacements = []
    for label in ("left", "right"):
        path = tmp_path / f"replacement-{label}.txt"
        path.write_text(f"{label} unique marker", encoding="utf-8")
        replacements.append(path)
    def try_replace(path):
        try:
            return ("accepted", _upload(manager, path, replace=imported.file_id))
        except ValueError as error:
            return ("rejected", str(error))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(try_replace, path) for path in replacements]
        outcomes = [future.result(timeout=20) for future in futures]

    accepted = [value for state, value in outcomes if state == "accepted"]
    rejected = [value for state, value in outcomes if state == "rejected"]
    assert len(accepted) == len(rejected) == 1
    assert accepted[0].status == "queued" and accepted[0].version == "v2"
    assert "替换" in rejected[0]
    assert manager.get_file(imported.file_id).version == "v1"
    rows = manager.list_jobs(limit=50)
    replacements_in_ledger = [item for item in rows if item.file_id == imported.file_id and item.kind == "import"]
    assert len(replacements_in_ledger) == 2, "baseline plus one candidate; rejected submit leaves no orphan job"
    manager.close()
    client.close()


def test_tombstone_rejects_new_replacement_retry_but_allows_delete_retry(tmp_path):
    client = QdrantClient(":memory:")
    failed = {"activating": False, "deleting": False}

    def fail_once(phase, context):
        if phase in failed and failed[phase]:
            failed[phase] = False
            raise OSError(f"acceptance failpoint: {phase}")

    manager = _manager(tmp_path, client, fail=fail_once)
    original = tmp_path / "original.txt"
    original.write_text("old stable marker", encoding="utf-8")
    imported = _upload(manager, original)
    assert manager.run_job(imported.job_id).status == "completed"

    candidate = tmp_path / "candidate.txt"
    candidate.write_text("new candidate marker", encoding="utf-8")
    failed["activating"] = True
    replacement = _upload(manager, candidate, replace=imported.file_id)
    assert manager.run_job(replacement.job_id).status == "failed"

    failed["deleting"] = True
    delete = manager.submit_delete(imported.file_id)
    assert manager.run_job(delete.job_id).status == "failed"
    assert manager.get_file(imported.file_id).status == "deleting"
    with pytest.raises(ValueError, match="deleting|deleted|删除"):
        _upload(manager, candidate, replace=imported.file_id)
    with pytest.raises(ValueError, match="deleting|deleted|删除"):
        manager.submit_retry(replacement.job_id)

    retry = manager.submit_retry(delete.job_id)
    assert manager.run_job(retry.job_id).status == "deleted"
    manager.close()
    client.close()


def test_delete_waits_until_failed_import_retry_cleanup_finishes(tmp_path, monkeypatch):
    client = QdrantClient(":memory:")
    fail_activation = {"enabled": False}

    def fail_once(phase, context):
        if phase == "activating" and fail_activation["enabled"]:
            fail_activation["enabled"] = False
            raise OSError("acceptance failpoint: activation")

    manager = _manager(tmp_path, client, fail=fail_once)
    original = tmp_path / "original.txt"
    original.write_text("stable baseline", encoding="utf-8")
    imported = _upload(manager, original)
    assert manager.run_job(imported.job_id).status == "completed"
    candidate = tmp_path / "candidate.txt"
    candidate.write_text("retry cleanup candidate", encoding="utf-8")
    fail_activation["enabled"] = True
    replacement = _upload(manager, candidate, replace=imported.file_id)
    assert manager.run_job(replacement.job_id).status == "failed"

    entered_cleanup = threading.Event()
    release_cleanup = threading.Event()
    original_cleanup = manager._cleanup_failed_import

    def pause_cleanup(job_id, version_id):
        entered_cleanup.set()
        if not release_cleanup.wait(timeout=20):
            raise TimeoutError("test did not release retry cleanup barrier")
        original_cleanup(job_id, version_id)

    monkeypatch.setattr(manager, "_cleanup_failed_import", pause_cleanup)
    with ThreadPoolExecutor(max_workers=2) as executor:
        retry_future = executor.submit(manager.submit_retry, replacement.job_id)
        assert entered_cleanup.wait(timeout=20)
        delete_future = executor.submit(manager.submit_delete, imported.file_id)
        # delete must wait for retry's cleanup and state recheck under the same file lock.
        assert not delete_future.done()
        release_cleanup.set()
        retried = retry_future.result(timeout=30)
        deletion = delete_future.result(timeout=30)

    assert retried.status == "queued"
    assert deletion.status == "queued"
    assert manager.get_file(imported.file_id).status == "deleting"
    assert manager.run_job(deletion.job_id).status == "deleted"
    manager.close()
    client.close()
