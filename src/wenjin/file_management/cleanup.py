"""导入失败残留清理、删除账本执行和manager-owned路径约束。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from wenjin.file_management.clock import utc_now_iso


class CleanupLifecycle:
    """导入失败残留清理、删除账本执行和manager-owned路径约束。"""

    def __init__(
        self,
        root: Path,
        base: Path,
        index_adapter,
        connect: Callable,
        update_job: Callable,
        hook: Callable,
        finalize_delete: Callable,
        managed_raw_data_root: Path,
    ):
        self.root = root
        self.managed_raw_data_root = Path(managed_raw_data_root)
        self.base = base
        self.index_adapter = index_adapter
        self._connect = connect
        self._update_job = update_job
        self._hook = hook
        self._finalize_delete = finalize_delete

    def _cleanup_failed_import(self, job_id: str, version_id: str) -> None:
        """重试前按账本删除上次半成品，同时保留owned staging输入。"""

        with self._connect() as db:
            job = db.execute("SELECT resources FROM jobs WHERE job_id=?", (job_id,)).fetchone()

            version = db.execute(
                "SELECT * FROM versions WHERE version_id=?", (version_id,)
            ).fetchone()

        resources = json.loads(job["resources"])

        if resources.get("collection") and self.index_adapter.exists(resources["collection"]):
            self.index_adapter.delete(resources["collection"])

        for key in ("version_dir", "raw_root", "bm25"):
            relative = resources.get(key)

            if relative:
                self._remove_owned_path(
                    self.root / relative,
                    version["file_id"],
                    {version_id},
                    set(),
                    {version["version"]},
                )

        resources = {"staging": resources.get("staging")}

        self._update_job(job_id, phase="queued", resources=resources)

    def _run_delete(self, job_id: str) -> None:

        with self._connect() as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

        resources = json.loads(job["resources"])

        self._update_job(job_id, status="running", phase="deleting", progress=5)

        items = []

        with self._connect() as db:
            versions = [
                dict(item)
                for item in db.execute("SELECT * FROM versions WHERE file_id=?", (job["file_id"],))
            ]

            file_jobs = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM jobs WHERE file_id=? AND kind='import' AND job_id!=?",
                    (job["file_id"], job_id),
                )
            ]

        resources["versions_to_delete"] = [
            {"version_id": item["version_id"], "version": item["version"]} for item in versions
        ]

        self._update_job(job_id, phase="deleting", progress=5, resources=resources)

        for version in versions:
            expected_manifest = (
                self.base
                / "versions"
                / job["file_id"]
                / version["version_id"]
                / "source-manifest.json"
            )

            expected_raw = (
                self.managed_raw_data_root / f"file-management-{job['file_id']}" / version["version"]
            )
            if not expected_raw.resolve().is_relative_to(self.root.resolve()):
                raise ValueError("拒绝清理项目root之外的managed原文资源")

            expected_bm25 = (
                self.base / "indexes" / job["file_id"] / f"{version['version_id']}.sqlite"
            )

            expected_collection = f"fm_{job['file_id'][:12]}_{version['version_id'][:12]}"

            for key, expected in (
                ("manifest_path", expected_manifest),
                ("raw_root", expected_raw),
                ("bm25_path", expected_bm25),
            ):
                actual = version.get(key)

                if actual and Path(actual).resolve() != expected.resolve():
                    raise ValueError(f"拒绝清理非managed版本资源：{key}")

            if (
                version.get("dense_collection")
                and version["dense_collection"] != expected_collection
            ):
                raise ValueError("拒绝清理名称不符合managed身份的Qdrant collection")

            for key in ("manifest_path", "raw_root"):
                value = version.get(key)

                if value:
                    resource_path = Path(value)

                    if key == "manifest_path":
                        value = str(resource_path.parent)

                    items.append(("path", value))

            if version.get("dense_collection"):
                items.append(("collection", version["dense_collection"]))

        # 所有版本共用这个manager-owned索引目录；删除文件即删除整个目录。

        index_dir = self.base / "indexes" / job["file_id"]

        items.append(("path", str(index_dir)))

        # 所有历史导入job的staging都属owned范围，完成后stage清理失败也能补删。

        staging_job_ids = {pending["job_id"] for pending in file_jobs}

        for pending in file_jobs:
            stage = self.base / "staging" / pending["job_id"]

            items.append(("path", str(stage)))

        with self._connect() as db:
            for kind, value in items:
                db.execute(
                    "INSERT OR IGNORE INTO cleanup_items(job_id,resource_type,resource_value,status) VALUES(?,?,?,'pending')",
                    (job_id, kind, value),
                )

        total = max(1, len(items))

        for index, (kind, value) in enumerate(items, start=1):
            self._hook("deleting", {"job_id": job_id, "resource_type": kind, "resource": value})

            if kind == "path":
                version_ids = {item["version_id"] for item in versions}

                version_names = {item["version"] for item in versions}

                self._remove_owned_path(
                    Path(value), job["file_id"], version_ids, staging_job_ids, version_names
                )

                exists = Path(value).exists()

            else:
                self.index_adapter.delete(value)

                exists = self.index_adapter.exists(value)

            if exists:
                raise OSError(f"资源清理后仍存在：{kind} {value}")

            with self._connect() as db:
                db.execute(
                    "UPDATE cleanup_items SET status='deleted',detail='已核验不存在' WHERE job_id=? AND resource_type=? AND resource_value=?",
                    (job_id, kind, value),
                )

            self._update_job(job_id, phase="deleting", progress=5 + int(index / total * 85))

        self._hook("delete_registration", {"job_id": job_id})

        self._finalize_delete(job_id=job_id, file_id=job["file_id"], resources=resources)

        for pending in file_jobs:
            with self._connect() as db:
                db.execute(
                    "UPDATE jobs SET status='cancelled',phase='cancelled',progress=100,retryable=0,reason='文件正在删除',updated_at=? WHERE job_id=? AND status IN ('queued','failed','running')",
                    (utc_now_iso(), pending["job_id"]),
                )

    def _remove_owned_path(
        self,
        path: Path,
        file_id: str,
        version_ids: set[str],
        staging_job_ids: set[str],
        raw_version_names: set[str],
    ) -> None:

        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError("删除目标超出项目root")

        allowed = {
            (self.base / "versions" / file_id / version_id).resolve() for version_id in version_ids
        }

        allowed.update(
            {
                (self.managed_raw_data_root / f"file-management-{file_id}" / version_name).resolve()
                for version_name in raw_version_names
            }
        )

        allowed.add((self.base / "indexes" / file_id).resolve())

        allowed.update({(self.base / "staging" / job_id).resolve() for job_id in staging_job_ids})

        # BM25文件及sidecar只允许这个file/version专属文件名前缀。

        allowed.update(
            {
                (self.base / "indexes" / file_id / f"{version_id}.sqlite").resolve()
                for version_id in version_ids
            }
        )

        if resolved not in allowed:
            raise ValueError(f"删除目标不属于manager-owned目录：{path}")

        if resolved.is_dir():
            shutil.rmtree(resolved)

        else:
            resolved.unlink(missing_ok=True)
