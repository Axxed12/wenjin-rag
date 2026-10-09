"""文件管理 SQLite 映射与持久化语句。

连接、锁和事务边界都归协调器所有。Repository 的每个写方法接收已打开连接，
只执行 SQL，不自行加锁、不提交，也不决定事务何时开始。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from wenjin.file_management.clock import utc_now_iso
from wenjin.file_management.models import FileRecord, JobRecord


class FileManagementRepository:
    """SQL 和行映射，不接管连接生命周期。"""

    @staticmethod
    def _version_number(value: str | None) -> int | None:
        if not isinstance(value, str) or not value.startswith("v"):
            return None
        try:
            return int(value[1:])
        except ValueError:
            return None

    @staticmethod
    def initialize(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS files(
                file_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                original_filename TEXT,
                status TEXT NOT NULL, active_version_id TEXT,
                duplicate_of TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                library_id TEXT NOT NULL DEFAULT 'managed-files',
                library_name TEXT NOT NULL DEFAULT '未分组'
            );
            CREATE TABLE IF NOT EXISTS versions(
                version_id TEXT PRIMARY KEY, file_id TEXT NOT NULL REFERENCES files(file_id),
                version TEXT NOT NULL, raw_sha256 TEXT NOT NULL, source_name TEXT NOT NULL,
                library_id TEXT, library_name TEXT, display_name TEXT, original_filename TEXT,
                manifest_path TEXT, raw_root TEXT, source_path TEXT, bm25_path TEXT,
                dense_collection TEXT, chunk_count INTEGER NOT NULL DEFAULT 0,
                strategy TEXT NOT NULL DEFAULT 'blocks-split', status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS versions_hash_idx ON versions(raw_sha256);
            CREATE TABLE IF NOT EXISTS jobs(
                job_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, version_id TEXT,
                kind TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
                progress INTEGER NOT NULL, retryable INTEGER NOT NULL,
                reason TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}', resources TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at);
            CREATE TABLE IF NOT EXISTS cleanup_items(
                job_id TEXT NOT NULL REFERENCES jobs(job_id), resource_type TEXT NOT NULL,
                resource_value TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                detail TEXT, PRIMARY KEY(job_id, resource_type, resource_value)
            );
            CREATE TABLE IF NOT EXISTS managed_libraries(
                library_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_by TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        # 旧本地数据库继续兼容；只补缺失的默认分组列。
        file_columns = {row[1] for row in db.execute("PRAGMA table_info(files)")}
        if "library_id" not in file_columns:
            db.execute(
                "ALTER TABLE files ADD COLUMN library_id TEXT NOT NULL DEFAULT 'managed-files'"
            )
        if "library_name" not in file_columns:
            db.execute("ALTER TABLE files ADD COLUMN library_name TEXT NOT NULL DEFAULT '未分组'")
        if "original_filename" not in file_columns:
            db.execute("ALTER TABLE files ADD COLUMN original_filename TEXT")
        version_columns = {row[1] for row in db.execute("PRAGMA table_info(versions)")}
        for column in ("library_id", "library_name", "display_name", "original_filename"):
            if column not in version_columns:
                db.execute(f"ALTER TABLE versions ADD COLUMN {column} TEXT")
        FileManagementRepository._migrate_version_metadata(db)
        # 进程中断的任务标记为可重试失败，不能继续显示为运行中。
        db.execute(
            "UPDATE jobs SET status='failed',phase='interrupted',retryable=1,reason='服务进程中断，可重试',updated_at=? WHERE status='running'",
            (utc_now_iso(),),
        )

    @staticmethod
    def _migrate_version_metadata(db: sqlite3.Connection) -> None:
        """从版本自己的导入任务恢复快照；只有active仍缺时才用当前file行兜底。

        旧版本没有可核验元数据时保留NULL，不把现在的file标签冒充历史快照。
        """
        # 一个version可能还关联后来“内容重复”的duplicate任务。只有真正持有
        # staging source 的导入任务才是该版本的元数据来源。
        source_payloads: dict[str, dict] = {}
        latest_build_version_by_file: dict[str, int] = {}
        jobs = db.execute(
            "SELECT j.version_id,j.payload,v.file_id,v.version "
            "FROM jobs j JOIN versions v ON v.version_id=j.version_id "
            "WHERE j.kind='import' ORDER BY j.created_at,j.rowid"
        ).fetchall()
        for job in jobs:
            try:
                payload = json.loads(job["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not payload.get("source"):
                continue
            source_payloads.setdefault(job["version_id"], payload)
            try:
                number = int(str(job["version"]).removeprefix("v"))
            except ValueError:
                continue
            latest_build_version_by_file[job["file_id"]] = max(
                number, latest_build_version_by_file.get(job["file_id"], 0)
            )

        rows = db.execute(
            "SELECT v.version_id,v.file_id,v.version,v.created_at AS version_created_at,"
            "v.library_id,v.library_name,v.display_name,v.original_filename,"
            "f.active_version_id,f.library_id AS file_library_id,f.library_name AS file_library_name,"
            "f.display_name AS file_display_name,f.original_filename AS file_original_filename "
            "FROM versions v JOIN files f ON f.file_id=v.file_id "
            "WHERE v.library_id IS NULL OR v.library_name IS NULL OR v.display_name IS NULL "
            "OR v.original_filename IS NULL"
        ).fetchall()
        for row in rows:
            payload = source_payloads.get(row["version_id"], {})
            active = row["version_id"] == row["active_version_id"]
            # 老版本曾在replace提交时提前更新file行。只要其后出现过真正的
            # build候选，file行对这个旧active就不再是可信历史来源。
            try:
                active_number = int(str(row["version"]).removeprefix("v"))
            except ValueError:
                active_number = -1
            file_fallback_safe = (
                active
                and active_number >= 0
                and latest_build_version_by_file.get(row["file_id"], active_number) <= active_number
            )
            values = {
                "library_id": payload.get("library_id") or (row["file_library_id"] if file_fallback_safe else None),
                "library_name": payload.get("library_display_name") or (row["file_library_name"] if file_fallback_safe else None),
                "display_name": payload.get("file_display_name") or payload.get("display_name") or (row["file_display_name"] if file_fallback_safe else None),
                "original_filename": payload.get("original_filename") or (row["file_original_filename"] if file_fallback_safe else None),
            }
            db.execute(
                "UPDATE versions SET library_id=COALESCE(library_id,?),"
                "library_name=COALESCE(library_name,?),display_name=COALESCE(display_name,?),"
                "original_filename=COALESCE(original_filename,?) WHERE version_id=?",
                (values["library_id"], values["library_name"], values["display_name"], values["original_filename"], row["version_id"]),
            )

    @staticmethod
    def file_record(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            file_id=row["file_id"],
            display_name=row["display_name"],
            status=row["status"],
            active_version_id=row["active_version_id"],
            version=row["version"],
            chunk_count=int(row["chunk_count"] or 0),
            updated_at=row["updated_at"],
            duplicate_of=row["duplicate_of"],
            managed=True,
            library_id=row["library_id"] if "library_id" in row.keys() else "managed-files",
            library_name=row["library_name"] if "library_name" in row.keys() else "未分组",
            original_filename=row["original_filename"] if "original_filename" in row.keys() else None,
        )

    @staticmethod
    def job_record(row: sqlite3.Row) -> JobRecord:
        payload = json.loads(row["payload"])
        return JobRecord(
            job_id=row["job_id"],
            file_id=row["file_id"],
            kind=row["kind"],
            status=row["status"],
            phase=row["phase"],
            progress=int(row["progress"]),
            retryable=bool(row["retryable"]),
            reason=row["reason"],
            retry_count=int(row["retry_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            duplicate_of=payload.get("existing_file_id", payload.get("duplicate_of")),
            resources={
                **json.loads(row["resources"]),
                **{
                    key: payload[key]
                    for key in ("existing_status", "duplicate_of")
                    if key in payload
                },
            },
            version_id=row["version_id"],
            version=row["version"] if "version" in row.keys() else None,
            file_display_name=(payload.get("file_display_name") or payload.get("display_name")
                               or (row["version_display_name"] if "version_display_name" in row.keys() else None)
                               or (row["display_name"] if not row["version_id"] and "display_name" in row.keys() else None)),
            library_id=(payload.get("library_id")
                        or (row["version_library_id"] if "version_library_id" in row.keys() else None)
                        or (row["library_id"] if not row["version_id"] and "library_id" in row.keys() else None)),
            library_display_name=(payload.get("library_display_name")
                                  or (row["version_library_name"] if "version_library_name" in row.keys() else None)
                                  or (row["library_name"] if not row["version_id"] and "library_name" in row.keys() else None)),
            original_filename=(payload.get("original_filename")
                               or (row["version_original_filename"] if "version_original_filename" in row.keys() else None)
                               or (row["original_filename"] if not row["version_id"] and "original_filename" in row.keys() else None)),
        )

    @staticmethod
    def active_file_record(db: sqlite3.Connection, row: sqlite3.Row) -> FileRecord:
        version = None
        chunks = 0
        integrity_reason = None
        if row["status"] == "active" and not row["active_version_id"]:
            integrity_reason = "活动版本指针缺失，拒绝检索"
        if row["active_version_id"]:
            version = db.execute(
                "SELECT version,chunk_count,library_id,library_name,display_name,original_filename,status "
                "FROM versions WHERE version_id=? AND file_id=?",
                (row["active_version_id"], row["file_id"]),
            ).fetchone()
            if version:
                chunks = version["chunk_count"]
                if version["status"] != "active":
                    integrity_reason = "活动指针与版本状态不一致，拒绝检索"
                elif not all(version[key] for key in ("library_id", "library_name", "display_name")):
                    integrity_reason = "活动版本缺少资料归属快照，拒绝检索"
            else:
                integrity_reason = "活动版本登记缺失，拒绝检索"
        latest_job = db.execute(
            "SELECT job_id,status,reason,phase,progress FROM jobs WHERE file_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (row["file_id"],),
        ).fetchone()
        return FileRecord(
            file_id=row["file_id"],
            display_name=(
                "活动版本信息缺失" if integrity_reason else
                version["display_name"] if version and version["display_name"] and not integrity_reason
                else (row["display_name"] if not row["active_version_id"] else "活动版本信息缺失")
            ),
            status=("integrity_error" if integrity_reason else row["status"]),
            active_version_id=row["active_version_id"],
            version=version["version"] if version else None,
            chunk_count=int(chunks),
            updated_at=row["updated_at"],
            duplicate_of=row["duplicate_of"],
            managed=True,
            latest_job_status=latest_job["status"] if latest_job else None,
            latest_job_reason=(integrity_reason or (latest_job["reason"] if latest_job else None)),
            latest_job_id=latest_job["job_id"] if latest_job else None,
            latest_job_phase=latest_job["phase"] if latest_job else None,
            latest_job_progress=latest_job["progress"] if latest_job else None,
            library_id=(version["library_id"] if version and not integrity_reason else (row["library_id"] if not row["active_version_id"] and not integrity_reason else None)),
            library_name=(version["library_name"] if version and not integrity_reason else (row["library_name"] if not row["active_version_id"] and not integrity_reason else None)),
            original_filename=(version["original_filename"] if version and not integrity_reason else (row["original_filename"] if not row["active_version_id"] and not integrity_reason else None)),
        )

    @staticmethod
    def update_version_resources(
        db: sqlite3.Connection,
        version_id: str,
        manifest_path,
        raw_root,
        source_name: str,
        bm25_path,
        collection: str,
        chunk_count: int,
    ) -> None:
        db.execute(
            "UPDATE versions SET manifest_path=?,raw_root=?,source_path=?,bm25_path=?,dense_collection=?,chunk_count=? WHERE version_id=?",
            (
                str(manifest_path),
                str(raw_root),
                str(raw_root / source_name),
                str(bm25_path),
                collection,
                chunk_count,
                version_id,
            ),
        )

    @staticmethod
    def activate_version(
        db: sqlite3.Connection,
        *,
        job_id: str,
        file_id: str,
        version_id: str,
        resources: dict,
        timestamp: str,
    ) -> None:
        selected = db.execute(
            "SELECT version,library_id,library_name,display_name,original_filename,status FROM versions WHERE version_id=? AND file_id=?",
            (version_id, file_id),
        ).fetchone()
        if selected is None:
            raise KeyError(version_id)
        if any(not selected[key] for key in ("library_id", "library_name", "display_name", "original_filename")):
            raise ValueError("候选版本缺少资料归属快照，拒绝激活")
        old = db.execute(
            "SELECT f.active_version_id,v.version AS active_version FROM files f "
            "LEFT JOIN versions v ON v.version_id=f.active_version_id AND v.file_id=f.file_id "
            "WHERE f.file_id=?", (file_id,)
        ).fetchone()
        if old is None:
            raise KeyError(file_id)
        candidate_number = FileManagementRepository._version_number(selected["version"])
        active_number = FileManagementRepository._version_number(old["active_version"])
        if candidate_number is None or (active_number is not None and candidate_number <= active_number):
            raise ValueError("较早的失败候选不能覆盖当前活动版本")
        if old and old["active_version_id"]:
            db.execute(
                "UPDATE versions SET status='retained' WHERE version_id=?",
                (old["active_version_id"],),
            )
        db.execute("UPDATE versions SET status='active' WHERE version_id=?", (version_id,))
        db.execute(
            "UPDATE files SET active_version_id=?,status='active',library_id=?,library_name=?,display_name=?,original_filename=?,updated_at=? WHERE file_id=?",
            (version_id, selected["library_id"], selected["library_name"], selected["display_name"], selected["original_filename"], timestamp, file_id),
        )
        db.execute(
            "UPDATE jobs SET status='completed',phase='complete',progress=100,retryable=0,reason=NULL,resources=?,updated_at=? WHERE job_id=?",
            (json.dumps(resources), timestamp, job_id),
        )

    @staticmethod
    def finalize_delete(
        db: sqlite3.Connection,
        *,
        job_id: str,
        file_id: str,
        resources: dict,
        root,
        timestamp: str,
    ) -> None:
        """在调用方事务内登记删除完成，并保存清理证据。"""
        db.execute("DELETE FROM versions WHERE file_id=?", (file_id,))
        db.execute(
            "UPDATE files SET status='deleted',active_version_id=NULL,updated_at=? WHERE file_id=?",
            (timestamp, file_id),
        )
        db.execute(
            "UPDATE jobs SET status='deleted',phase='complete',progress=100,retryable=0,reason=NULL,updated_at=? WHERE job_id=?",
            (timestamp, job_id),
        )
        rows = db.execute(
            "SELECT resource_type,resource_value,status FROM cleanup_items WHERE job_id=?",
            (job_id,),
        ).fetchall()
        resources["cleanup_verified"] = len(rows)
        resources["versions_cleaned"] = len(resources.get("versions_to_delete", []))
        resources["cleanup_details"] = [
            {
                "type": item["resource_type"],
                "resource": (
                    Path(item["resource_value"]).resolve().relative_to(root).as_posix()
                    if item["resource_type"] == "path"
                    else item["resource_value"]
                ),
                "status": item["status"],
            }
            for item in rows
        ]
        resources["legacy_retained"] = resources.get("legacy_retained", [])
        resources["legacy_may_remain_searchable"] = bool(resources["legacy_retained"])
        db.execute(
            "UPDATE jobs SET resources=? WHERE job_id=?",
            (json.dumps(resources), job_id),
        )
