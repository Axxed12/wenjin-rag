"""单文件版本化入库、双索引切换和可恢复删除。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from wenjin.file_management.backends import IndexAdapter, QdrantBm25Adapter
from wenjin.file_management.cleanup import CleanupLifecycle
from wenjin.file_management.jobs import JobLifecycle
from wenjin.file_management.models import FileRecord, FileSnapshot, JobRecord, StaleSnapshotError
from wenjin.file_management.repository import FileManagementRepository
from wenjin.file_management.versions import VersionLifecycle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_job_reason(reason: str | None) -> str | None:
    """避免失败消息把操作系统绝对路径带入批量任务响应。"""
    if reason is None:
        return None
    return re.sub(r"(?:(?:[A-Za-z]:\\|/)[^\s'\"<>]+)", "[路径已隐藏]", reason)


def _library_id(library: str) -> str:
    """稳定复用路由的大小写/空格/下划线归一规则。"""
    return re.sub(r"[\s_]+", "-", library.strip().lower())


class _ProcessLock:
    """跨平台独占锁：同一root只允许一个管理服务进程持有。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+b")
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                self.handle.write(b"\0")
                self.handle.flush()
            self.handle.seek(0)
            try:
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                self.handle.close()
                raise RuntimeError(f"该项目根目录已有文件管理服务：{path.parent.parent}") from error
        else:
            import fcntl

            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                self.handle.close()
                raise RuntimeError(f"该项目根目录已有文件管理服务：{path.parent.parent}") from error

    def close(self) -> None:
        if self.handle.closed:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


class FileManagementService:
    """以SQLite为文件/version/job登记真值，以独立索引版本实现原子切换。

    构造时不加载Embedding模型；调用者传入单例embedder。`fault_hook`只用于故障注入，
    在线服务留空即可。所有读操作和active指针变化共用一把锁，避免一次请求跨版本读取。
    """

    def __init__(
        self,
        root: Path,
        qdrant_client: Any,
        embedder: Any,
        embedding_model: str,
        *,
        parser: Callable | None = None,
        fault_hook: Callable[[str, dict], None] | None = None,
        index_adapter: IndexAdapter | None = None,
        managed_raw_data_root: Path | str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.base = self.root / "data" / "file-management"
        candidate_raw_root = (
            self.root / "raw_data" if managed_raw_data_root is None
            else Path(managed_raw_data_root)
        )
        if not candidate_raw_root.is_absolute():
            candidate_raw_root = self.root / candidate_raw_root
        self.managed_raw_data_root = candidate_raw_root.resolve()
        if not self.managed_raw_data_root.is_relative_to(self.root):
            raise ValueError("WENJIN_MANAGED_RAW_DATA_ROOT必须位于项目root内")
        self.db_path = self.base / "manager.sqlite"
        self.qdrant_client = qdrant_client
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.parser = parser
        self.fault_hook = fault_hook
        self.index_adapter = index_adapter or QdrantBm25Adapter(
            root=self.root,
            qdrant_client=qdrant_client,
            embedder=embedder,
            embedding_model=embedding_model,
            managed_raw_data_root=self.managed_raw_data_root,
        )
        self._rw_lock = threading.RLock()
        self._job_lock = threading.Lock()
        self._retry_lock = threading.Lock()
        self._model_lock = threading.RLock()
        if isinstance(self.index_adapter, QdrantBm25Adapter):
            self.index_adapter.embedding_lock = self._model_lock
        self._process_lock = _ProcessLock(self.base / "manager.lock")
        self.repository = FileManagementRepository()
        self.base.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
            self._verify_managed_raw_root_binding()
            self.cleanup = CleanupLifecycle(
                root=self.root,
                base=self.base,
                index_adapter=self.index_adapter,
                connect=self._connect,
                update_job=self._update_job,
                hook=self._hook,
                finalize_delete=self._finalize_delete,
                managed_raw_data_root=self.managed_raw_data_root,
            )
            self.versions = VersionLifecycle(
                root=self.root,
                base=self.base,
                parser=self.parser,
                index_adapter=self.index_adapter,
                model_lock=self._model_lock,
                connect=self._connect,
                update_job=self._update_job,
                hook=self._hook,
                sha256=self._sha256,
                activate=self._activate_version,
                repository=self.repository,
                managed_raw_data_root=self.managed_raw_data_root,
                cancel_requested=self._cancel_requested,
            )
            self.jobs = JobLifecycle(
                connect=self._connect,
                get_job=self.get_job,
                submit_delete=self.submit_delete,
                cleanup_failed_import=lambda job_id, version_id: self._cleanup_failed_import(
                    job_id, version_id
                ),
                cleanup_cancelled_import=lambda job_id, version_id: self._cleanup_cancelled_import(
                    job_id, version_id
                ),
                run_import=lambda job_id: self._run_import(job_id),
                run_delete=lambda job_id: self._run_delete(job_id),
                rw_lock=self._rw_lock,
                job_lock=self._job_lock,
                retry_lock=self._retry_lock,
                begin_immediate=self._begin_immediate,
            )
        except Exception:
            self._process_lock.close()
            raise

    def close(self) -> None:
        """释放项目根进程锁；Qdrant/embedder生命周期由服务入口统一管理。"""
        self._process_lock.close()

    def _verify_managed_raw_root_binding(self) -> None:
        """拒绝用变更后的配置把已有活动版本静默指向另一个原文树。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT file_id,version,raw_root FROM versions WHERE raw_root IS NOT NULL"
            ).fetchall()
        for row in rows:
            expected = (
                self.managed_raw_data_root
                / f"file-management-{row['file_id']}"
                / row["version"]
            ).resolve()
            if (not expected.is_relative_to(self.root)
                    or Path(row["raw_root"]).resolve() != expected):
                raise ValueError(
                    "WENJIN_MANAGED_RAW_DATA_ROOT与已登记文件版本不一致；拒绝改换原文位置"
                )

    @contextmanager
    def _connect(self):
        """service 唯一连接入口；在此提交或回滚并关闭连接，组件只使用此回调。"""
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            self.repository.initialize(db)

    def list_libraries(self) -> list[dict[str, str | None]]:
        """返回管理员建立的受管逻辑库；授权仍由 identity store 单独控制。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT library_id,display_name,created_by,created_at "
                "FROM managed_libraries ORDER BY display_name COLLATE NOCASE,library_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_library(self, library_id: str | None, display_name: str, *, created_by: str | None = None) -> dict[str, str | None]:
        """持久登记一个受管逻辑库，不创建索引也不授予任何用户权限。"""
        stable_id = library_id.strip() if isinstance(library_id, str) and library_id.strip() else f"managed-{uuid4().hex[:12]}"
        label = display_name.strip() if isinstance(display_name, str) else ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", stable_id):
            raise ValueError("资料库编号必须是小写字母、数字和连字符组成的稳定编号")
        if (
            not label or len(label) > 120
            or any(ord(char) < 32 or ord(char) == 127 for char in label)
        ):
            raise ValueError("资料库名称不能为空，且不能超过120个字符")
        timestamp = _now()
        try:
            with self._rw_lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if stable_id == "managed-files":
                    raise ValueError("该编号保留给历史默认分组")
                duplicate_id = db.execute(
                    "SELECT display_name FROM managed_libraries WHERE library_id=?", (stable_id,)
                ).fetchone()
                existing_group = db.execute(
                    "SELECT library_name FROM files WHERE library_id=? AND status!='deleted' LIMIT 1", (stable_id,)
                ).fetchone()
                if duplicate_id or existing_group:
                    raise ValueError("资料库编号已存在")
                duplicate_name = db.execute(
                    "SELECT library_id FROM managed_libraries WHERE display_name=? COLLATE NOCASE", (label,)
                ).fetchone()
                existing_name = db.execute(
                    "SELECT library_id FROM files WHERE library_name=? COLLATE NOCASE AND status!='deleted' LIMIT 1", (label,)
                ).fetchone()
                if duplicate_name or existing_name:
                    raise ValueError("资料库名称已存在")
                db.execute(
                    "INSERT INTO managed_libraries(library_id,display_name,created_by,created_at) VALUES(?,?,?,?)",
                    (stable_id, label, created_by, timestamp),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("资料库编号或名称已存在") from exc
        return {"library_id": stable_id, "display_name": label, "created_by": created_by, "created_at": timestamp}

    @staticmethod
    def _file_record(row: sqlite3.Row) -> FileRecord:
        return FileManagementRepository.file_record(row)

    @staticmethod
    def _job_record(row: sqlite3.Row) -> JobRecord:
        return FileManagementRepository.job_record(row)

    def _active_file_record(self, db: sqlite3.Connection, row: sqlite3.Row) -> FileRecord:
        return self.repository.active_file_record(db, row)

    def list_files(self, *, include_legacy: bool = True) -> list[FileRecord]:
        """列出managed文件，并以只读方式附加旧manifest中的unmanaged文件。"""
        with self._rw_lock, self._connect() as db:
            managed = [
                self._active_file_record(db, row)
                for row in db.execute(
                    "SELECT * FROM files WHERE status != 'deleted' ORDER BY display_name COLLATE NOCASE"
                )
            ]
        return managed + (self._list_legacy() if include_legacy else [])

    def _list_legacy(self) -> list[FileRecord]:
        """历史manifest仅作为inventory；绝不借此取得旧索引或原文所有权。"""
        from wenjin.catalog import corpus_catalog_report, index_target_configs

        catalog = corpus_catalog_report(self.root)
        configs = index_target_configs(self.root)
        target_ids = sorted(
            {target_id for corpus in catalog.entries for target_id in corpus.index_targets}
        )
        counts_by_target = {}
        for target_id in target_ids:
            target = configs.get(target_id)
            if not target:
                continue
            try:
                from wenjin.index_registry import resolve_project_path

                path = resolve_project_path(self.root, target["bm25_path"])
                if not path.is_file():
                    continue
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    counts_by_target[target_id] = dict(
                        db.execute(
                            "SELECT source_path,COUNT(*) FROM chunks GROUP BY source_path"
                        ).fetchall()
                    )
            except (OSError, sqlite3.Error, KeyError, ValueError):
                continue
        result = []
        for corpus in catalog.entries:
            manifest_path = self.root / corpus.manifest_path
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in manifest.get("files", []):
                name = str(item.get("path", ""))
                if not name:
                    continue
                legacy_id = (
                    f"legacy:{corpus.corpus_id}:{hashlib.sha256(name.encode()).hexdigest()[:16]}"
                )
                chunks, count_source = self._legacy_chunk_count(corpus, name, counts_by_target)
                result.append(
                    FileRecord(
                        file_id=legacy_id,
                        display_name=Path(name).name,
                        status="legacy_unmanaged",
                        active_version_id=None,
                        version=corpus.version,
                        chunk_count=chunks,
                        updated_at="",
                        managed=False,
                        legacy_id=legacy_id,
                        library=corpus.library,
                        library_id=_library_id(corpus.library),
                        library_name=corpus.library,
                        chunk_count_source=count_source,
                    )
                )
        return result

    @staticmethod
    def _legacy_chunk_count(
        corpus, relative: str, counts_by_target: dict
    ) -> tuple[int | None, str | None]:
        source_path = (
            f"raw_data/{corpus.raw_data_subdir}/{relative}" if corpus.raw_data_subdir else relative
        )
        for target_id in corpus.index_targets:
            if target_id in counts_by_target:
                return int(counts_by_target[target_id].get(source_path, 0)), target_id
        return None, None

    def get_file(self, file_id: str) -> FileRecord:
        with self._rw_lock, self._connect() as db:
            row = db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if row is None:
                raise KeyError(file_id)
            return self._active_file_record(db, row)

    def get_job(self, job_id: str) -> JobRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT j.*,v.version AS version,v.display_name AS version_display_name,"
                "v.library_id AS version_library_id,v.library_name AS version_library_name,"
                "v.original_filename AS version_original_filename,"
                "f.display_name,f.library_id,f.library_name,f.original_filename "
                "FROM jobs j LEFT JOIN versions v ON v.version_id=j.version_id "
                "LEFT JOIN files f ON f.file_id=j.file_id WHERE j.job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            return self._job_record(row)

    def list_jobs(self, *, limit: int = 100, offset: int = 0) -> list[JobRecord]:
        """按SQLite登记顺序分页列出任务，不读取可能含磁盘路径的payload/resources。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit必须是1到200之间的整数")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset必须是大于等于0的整数")

        # 仅选择返回给管理界面的字段，版本来自真实versions登记，不从latest文件推断。
        with self._connect() as db:
            rows = db.execute(
                "SELECT j.job_id,j.file_id,j.kind,j.status,j.phase,j.progress,j.retryable,"
                "j.reason,j.retry_count,j.created_at,j.updated_at,j.version_id,j.payload,j.cancel_requested,"
                "v.version AS version,v.display_name AS version_display_name,"
                "v.library_id AS version_library_id,v.library_name AS version_library_name,"
                "v.original_filename AS version_original_filename,"
                "f.display_name,f.library_id,f.library_name,f.original_filename "
                "FROM jobs AS j LEFT JOIN versions AS v ON v.version_id=j.version_id "
                "LEFT JOIN files AS f ON f.file_id=j.file_id "
                "ORDER BY j.created_at DESC,j.rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

        result = []
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            result.append(JobRecord(
                job_id=row["job_id"],
                file_id=row["file_id"],
                kind=row["kind"],
                status=row["status"],
                phase=row["phase"],
                progress=int(row["progress"]),
                retryable=bool(row["retryable"]),
                reason=_redact_job_reason(row["reason"]),
                retry_count=int(row["retry_count"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                # list接口不需要副本信息；payload/resources可能包含原文和索引路径。
                duplicate_of=None,
                resources=None,
                version_id=row["version_id"],
                version=row["version"],
                file_display_name=(payload.get("file_display_name") or payload.get("display_name")
                                   or row["version_display_name"]
                                   or (row["display_name"] if row["version_id"] is None else None)),
                library_id=(payload.get("library_id") or row["version_library_id"]
                            or (row["library_id"] if row["version_id"] is None else None)),
                library_display_name=(payload.get("library_display_name") or row["version_library_name"]
                                      or (row["library_name"] if row["version_id"] is None else None)),
                original_filename=(payload.get("original_filename") or row["version_original_filename"]
                                   or (row["original_filename"] if row["version_id"] is None else None)),
                cancel_requested=bool(row["cancel_requested"]),
            ))
        return result

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def submit_import(
        self,
        source: Path,
        *,
        display_name: str | None = None,
        replace_file_id: str | None = None,
        retry_of: str | None = None,
        library_id: str | None = None,
        library_name: str | None = None,
        original_filename: str | None = None,
    ) -> JobRecord:
        provided_source = Path(source)
        if provided_source.is_symlink():
            raise ValueError("入库来源不能是符号链接")
        source = provided_source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("入库来源必须是普通文件")
        original_name = (original_filename or source.name).strip()
        if (
            not original_name or len(original_name) > 240
            or Path(original_name).name != original_name or "/" in original_name
            or "\\" in original_name or ":" in original_name
            or re.search(r'[<>"|?*\x00-\x1f]', original_name)
            or original_name.endswith((".", " "))
        ):
            raise ValueError("original_filename无效")
        name = (display_name or original_name).strip()
        if (
            not name
            or len(name) > 240
            or Path(name).name != name
            or "/" in name
            or "\\" in name
            or ":" in name
            or re.search(r'[<>"|?*\x00-\x1f]', name)
            or name.endswith((".", " "))
        ):
            raise ValueError("display_name无效")
        # 上传名仅用于展示；磁盘名使用随机值和受限扩展名，避开Windows保留名及路径注入。
        suffix = Path(original_name).suffix.lower()
        if suffix and not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            raise ValueError("原文件名扩展名无效")
        selected_library_id = library_id or "managed-files"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", selected_library_id):
            raise ValueError("library_id无效")
        selected_library_name = (library_name or selected_library_id).strip() or selected_library_id
        raw_hash = self._sha256(source)
        job_id, version_id = uuid4().hex, uuid4().hex
        timestamp = _now()
        staged_name = f"source{suffix}"
        staging = self.base / "staging" / job_id / staged_name
        staging.parent.mkdir(parents=True, exist_ok=True)
        # 先复制上传内容，避免文件I/O期间持有SQLite写锁；提交前原子登记其稳定路径。
        try:
            shutil.copyfile(source, staging)
        except Exception:
            shutil.rmtree(staging.parent, ignore_errors=True)
            raise
        duplicate_job = False
        # 入库只登记新的staging版本，不切active；允许当前active检索继续读取。
        try:
            with self._connect() as db:
                # 判重、版本号分配与文件/版本/job登记必须串行化，避免并发replace拿到相同vN。
                db.execute("BEGIN IMMEDIATE")
                duplicate = db.execute(
                    "SELECT f.file_id,f.display_name,f.library_name,v.version_id,v.status AS version_status,j.job_id,j.status AS job_status "
                    "FROM files f JOIN versions v ON v.file_id=f.file_id "
                    "LEFT JOIN jobs j ON j.version_id=v.version_id AND j.kind='import' "
                    "WHERE v.raw_sha256=? AND f.status!='deleted' AND f.library_id=? "
                    "ORDER BY CASE v.status WHEN 'active' THEN 0 ELSE 1 END,j.created_at DESC LIMIT 1",
                    (raw_hash, selected_library_id),
                ).fetchone()
                if duplicate and replace_file_id is None:
                    db.execute(
                        "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,reason,payload,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            duplicate["file_id"],
                            duplicate["version_id"],
                            "import",
                            "duplicate",
                            "complete",
                            100,
                            0,
                            "内容与已登记版本相同",
                            json.dumps(
                                {
                                    "duplicate_of": duplicate["job_id"] or duplicate["version_id"],
                                    "existing_file_id": duplicate["file_id"],
                                    "existing_status": duplicate["job_status"]
                                    or duplicate["version_status"],
                                    "file_display_name": duplicate["display_name"],
                                    "library_id": selected_library_id,
                                    "library_display_name": duplicate["library_name"],
                                    "original_filename": original_name,
                                }
                            ),
                            timestamp,
                            timestamp,
                        ),
                    )
                    duplicate_job = True
                elif replace_file_id:
                    file_row = db.execute(
                        "SELECT * FROM files WHERE file_id=?", (replace_file_id,)
                    ).fetchone()
                    if file_row is None or file_row["status"] == "deleted":
                        raise KeyError(replace_file_id)
                    if file_row["status"] == "deleting":
                        raise ValueError("文件正在删除，不能替换")
                    file_id = replace_file_id
                    if library_id is None:
                        active = db.execute(
                            "SELECT library_id,library_name FROM versions WHERE version_id=? AND file_id=?",
                            (file_row["active_version_id"], file_id),
                        ).fetchone() if file_row["active_version_id"] else None
                        if file_row["active_version_id"] and (
                            active is None or not active["library_id"] or not active["library_name"]
                        ):
                            raise ValueError("活动版本资料归属无法确认；请明确指定目标资料库后再替换")
                        selected_library_id = (
                            (active["library_id"] or file_row["library_id"]) if active else file_row["library_id"]
                        )
                        selected_library_name = (
                            (active["library_name"] or selected_library_id) if active else file_row["library_name"]
                        )
                    pending = db.execute(
                        "SELECT 1 FROM jobs WHERE file_id=? AND kind='import' "
                        "AND status IN ('queued','running') LIMIT 1",
                        (file_id,),
                    ).fetchone()
                    if pending:
                        raise ValueError("该文件已有导入任务正在排队或运行，请等待完成后再替换")
                    version_no = (
                        db.execute(
                            "SELECT COUNT(*) FROM versions WHERE file_id=?", (file_id,)
                        ).fetchone()[0]
                        + 1
                    )
                elif not duplicate_job:
                    file_id = uuid4().hex
                    version_no = 1
                    db.execute(
                        "INSERT INTO files(file_id,display_name,original_filename,status,created_at,updated_at,library_id,library_name) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            file_id,
                            name,
                            original_name,
                            "importing",
                            timestamp,
                            timestamp,
                            selected_library_id,
                            selected_library_name,
                        ),
                    )
                if not duplicate_job:
                    version = f"v{version_no}"
                    db.execute(
                        "INSERT INTO versions(version_id,file_id,version,raw_sha256,source_name,library_id,library_name,display_name,original_filename,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            version_id, file_id, version, raw_hash, staged_name,
                            selected_library_id, selected_library_name, name,
                            original_name, "staging", timestamp,
                        ),
                    )
                    if replace_file_id:
                        # 新候选替代已失败的旧候选后，旧任务保留失败记录和清理账本，
                        # 但不能再重试并把活动指针切回较早版本。
                        db.execute(
                            "UPDATE jobs SET retryable=0,reason='已有较新的替换候选，此任务不可重试',updated_at=? "
                            "WHERE file_id=? AND kind='import' AND status='failed' AND retryable=1 "
                            "AND version_id IN (SELECT version_id FROM versions WHERE file_id=? "
                            "AND status='failed' AND CAST(SUBSTR(version,2) AS INTEGER)<?)",
                            (timestamp, file_id, file_id, version_no),
                        )
                    db.execute(
                        "INSERT INTO jobs(job_id,file_id,version_id,kind,status,phase,progress,retryable,payload,resources,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            job_id,
                            file_id,
                            version_id,
                            "import",
                            "queued",
                            "queued",
                            0,
                            1,
                            json.dumps(
                                {
                                    "source": str(staging),
                                    "display_name": name,
                                    "file_display_name": name,
                                    "library_id": selected_library_id,
                                    "library_display_name": selected_library_name,
                                    "original_filename": original_name,
                                    "replace": bool(replace_file_id),
                                    "retry_of": retry_of,
                                }
                            ),
                            json.dumps({"staging": str(staging.relative_to(self.root).as_posix())}),
                            timestamp,
                            timestamp,
                        ),
                    )
        except Exception:
            shutil.rmtree(staging.parent, ignore_errors=True)
            raise
        if duplicate_job:
            shutil.rmtree(staging.parent, ignore_errors=True)
        return self.get_job(job_id)

    def submit_delete(self, file_id: str) -> JobRecord:
        job_id, timestamp = uuid4().hex, _now()
        with self._rw_lock, self._connect() as db:
            self._begin_immediate(db)
            row = db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if row is None:
                raise KeyError(file_id)
            if row["status"] == "deleted":
                raise ValueError("文件已经删除")
            if row["status"] == "deleting":
                raise ValueError("该文件已有删除任务，不能重复提交")
            active = db.execute(
                "SELECT library_id,library_name,display_name,original_filename "
                "FROM versions WHERE version_id=? AND file_id=?",
                (row["active_version_id"], file_id),
            ).fetchone() if row["active_version_id"] else None
            readable = active or row
            db.execute(
                "UPDATE files SET status='deleting',updated_at=? WHERE file_id=?",
                (timestamp, file_id),
            )
            versions = [
                dict(item)
                for item in db.execute("SELECT * FROM versions WHERE file_id=?", (file_id,))
            ]
            payload = {
                "file_display_name": readable["display_name"],
                "library_id": readable["library_id"],
                "library_display_name": readable["library_name"],
                "original_filename": readable["original_filename"],
                "versions": versions,
                "staging_jobs": [
                    dict(item)
                    for item in db.execute(
                        "SELECT * FROM jobs WHERE file_id=? AND status IN ('queued','failed','running')",
                        (file_id,),
                    )
                ],
            }
            db.execute(
                "INSERT INTO jobs(job_id,file_id,kind,status,phase,progress,retryable,payload,resources,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    file_id,
                    "delete",
                    "queued",
                    "queued",
                    0,
                    1,
                    json.dumps(payload),
                    json.dumps(
                        {
                            "legacy_retained": self._legacy_artifacts_for_file(db, file_id),
                            "cleanup_scope": "本文件所有manager-owned版本原文、manifest目录、BM25文件/sidecar、Qdrant collection和import staging",
                            "owned_backup_scope": "只清理文件版本目录内的manager-owned副本；当前没有独立备份功能",
                            "external_backups": "not_checked",
                            "historical_artifacts": "retained",
                        }
                    ),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_job(job_id)

    def submit_retry(self, job_id: str) -> JobRecord:
        return self.jobs.submit_retry(job_id)

    def _cleanup_failed_import(self, job_id: str, version_id: str) -> None:
        self.cleanup._cleanup_failed_import(job_id, version_id)

    def _cancel_requested(self, job_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def _cleanup_cancelled_import(self, job_id: str, version_id: str) -> None:
        """清理取消候选的owned资源，并将终态写在资源释放之后。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT j.file_id,v.version FROM jobs j JOIN versions v ON v.version_id=j.version_id "
                "WHERE j.job_id=? AND j.version_id=? AND j.kind='import'",
                (job_id, version_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            file_id, version = row["file_id"], row["version"]
            active = db.execute(
                "SELECT active_version_id FROM files WHERE file_id=?", (file_id,)
            ).fetchone()
            if active is None or active["active_version_id"] == version_id:
                raise ValueError("拒绝清理缺失文件或当前活动版本")

        self._cleanup_failed_import(job_id, version_id)
        stage = self.base / "staging" / job_id
        self.cleanup._remove_owned_path(
            stage, file_id, {version_id}, {job_id}, {version}
        )
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE versions SET status='cancelled' WHERE version_id=? AND file_id=? AND status!='active'",
                (version_id, file_id),
            )
            db.execute(
                "UPDATE files SET status='failed',updated_at=? WHERE file_id=? "
                "AND active_version_id IS NULL AND status='importing'",
                (_now(), file_id),
            )
        self._update_job(
            job_id, status="cancelled", phase="cancelled", progress=100,
            retryable=False, reason="任务已取消，候选资源已清理",
            resources={"cancel_cleanup": "complete"},
        )

    def resume_pending_jobs(self) -> list[JobRecord]:
        return self.jobs.resume_pending_jobs()

    def request_cancel(self, job_id: str) -> JobRecord:
        return self.jobs.request_cancel(job_id)

    def run_job(self, job_id: str) -> JobRecord:
        return self.jobs.run_job(job_id)

    def _hook(self, phase: str, context: dict) -> None:
        if self.fault_hook:
            self.fault_hook(phase, context)

    def _update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str,
        progress: int | None = None,
        retryable: bool | None = None,
        reason: str | None = None,
        resources: dict | None = None,
        cancel_requested: bool | None = None,
    ) -> None:
        self.jobs._update_job(
            job_id,
            status=status,
            phase=phase,
            progress=progress,
            retryable=retryable,
            reason=reason,
            resources=resources,
            cancel_requested=cancel_requested,
        )

    @staticmethod
    def _begin_immediate(db: sqlite3.Connection) -> None:
        """由 service 协调器统一发起需要串行化的 SQLite 写事务。"""
        db.execute("BEGIN IMMEDIATE")

    def _finalize_delete(self, *, job_id: str, file_id: str, resources: dict) -> None:
        """在协调器事务内完成删除登记；沿用原删除路径的锁范围。"""
        with self._connect() as db:
            self._begin_immediate(db)
            self.repository.finalize_delete(
                db,
                job_id=job_id,
                file_id=file_id,
                resources=resources,
                root=self.root,
                timestamp=_now(),
            )

    def _activate_version(
        self,
        *,
        job_id: str,
        file_id: str,
        version_id: str,
        manifest_path: Path,
        version_dir: Path,
        raw_root: Path,
        bm25_path: Path,
        collection: str,
        chunk_count: int,
        source_path: Path,
        source_sha256: str,
    ) -> None:
        # active 指针切换的读写锁和原子事务仍由 service 协调器持有。
        with self._rw_lock, self._connect() as db:
            self._begin_immediate(db)
            cancel = db.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id=? AND version_id=?",
                (job_id, version_id),
            ).fetchone()
            if cancel is None or cancel["cancel_requested"]:
                raise RuntimeError("导入任务已请求取消，阻止候选版本激活")
            current = db.execute("SELECT status FROM files WHERE file_id=?", (file_id,)).fetchone()
            if current is None or current["status"] in {"deleting", "deleted"}:
                raise RuntimeError("文件已进入删除流程，阻止新版激活")
            prior_job = db.execute(
                "SELECT resources FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            prior_resources = json.loads(prior_job["resources"])
            resources = {
                "manifest": manifest_path.relative_to(self.root).as_posix(),
                "version_dir": version_dir.relative_to(self.root).as_posix(),
                "raw_root": raw_root.relative_to(self.root).as_posix(),
                "bm25": bm25_path.relative_to(self.root).as_posix(),
                "collection": collection,
                "chunk_count": chunk_count,
                "source_path": source_path.relative_to(self.root).as_posix(),
                "source_sha256": source_sha256,
                "legacy_retained": prior_resources.get("legacy_retained", []),
                "legacy_may_remain_searchable": bool(prior_resources.get("legacy_retained")),
            }
            self.repository.activate_version(
                db,
                job_id=job_id,
                file_id=file_id,
                version_id=version_id,
                resources=resources,
                timestamp=_now(),
            )

    def _run_import(self, job_id: str) -> None:
        self.versions._run_import(job_id)

    def _pdf_plans(self, source: Path):
        return self.versions._pdf_plans(source)

    @staticmethod
    def _atomic_json(path: Path, data: dict) -> None:
        VersionLifecycle._atomic_json(path, data)

    def _update_version_resources(
        self, version_id, manifest_path, raw_root, source_name, bm25_path, collection, chunk_count
    ) -> None:
        with self._connect() as db:
            self.repository.update_version_resources(
                db,
                version_id,
                manifest_path,
                raw_root,
                source_name,
                bm25_path,
                collection,
                chunk_count,
            )

    def _run_delete(self, job_id: str) -> None:
        self.cleanup._run_delete(job_id)

    def _remove_owned_path(
        self,
        path: Path,
        file_id: str,
        version_ids: set[str],
        staging_job_ids: set[str],
        raw_version_names: set[str],
    ) -> None:
        self.cleanup._remove_owned_path(
            path, file_id, version_ids, staging_job_ids, raw_version_names
        )

    def snapshot(self, file_id: str) -> FileSnapshot:
        with self._rw_lock, self._connect() as db:
            row = db.execute(
                "SELECT f.file_id,f.status AS file_status,f.active_version_id,"
                "v.version_id,v.version,v.display_name,v.library_id,v.library_name,v.original_filename,"
                "v.manifest_path,v.raw_root,v.source_path,v.bm25_path,v.dense_collection,"
                "v.chunk_count,v.strategy,v.raw_sha256 "
                "FROM files f JOIN versions v ON v.version_id=f.active_version_id AND v.file_id=f.file_id "
                "WHERE f.file_id=? AND f.status='active' AND v.status='active'",
                (file_id,),
            ).fetchone()
            if row is None:
                raise KeyError(file_id)
            if not row["library_id"] or not row["library_name"] or not row["display_name"]:
                raise RuntimeError("活动版本缺少资料归属或显示名称快照，拒绝访问")
            source_path = Path(row["source_path"])
            expected_manifest = (
                self.base / "versions" / file_id / row["version_id"] / "source-manifest.json"
            )
            expected_root = (
                self.managed_raw_data_root / f"file-management-{file_id}" / row["version"]
            ).resolve()
            if not expected_root.is_relative_to(self.root):
                raise RuntimeError("managed原文目录越界，拒绝访问")
            expected_bm25 = self.base / "indexes" / file_id / f"{row['version_id']}.sqlite"
            expected_collection = f"fm_{file_id[:12]}_{row['version_id'][:12]}"
            if (
                Path(row["manifest_path"]).resolve() != expected_manifest.resolve()
                or Path(row["raw_root"]).resolve() != expected_root.resolve()
                or Path(row["bm25_path"]).resolve() != expected_bm25.resolve()
                or row["dense_collection"] != expected_collection
                or not source_path.resolve().is_relative_to(expected_root.resolve())
            ):
                raise RuntimeError("active资源路径与managed file/version身份不一致，拒绝查询")
            if not source_path.is_file() or self._sha256(source_path) != row["raw_sha256"]:
                raise RuntimeError("active原文缺失或SHA256不匹配，拒绝检索")
            return FileSnapshot(
                file_id=file_id,
                version_id=row["version_id"],
                version=row["version"],
                display_name=row["display_name"],
                library_id=row["library_id"],
                library_name=row["library_name"],
                original_filename=row["original_filename"],
                manifest_path=Path(row["manifest_path"]),
                raw_root=Path(row["raw_root"]),
                bm25_path=Path(row["bm25_path"]),
                dense_collection=row["dense_collection"],
                chunk_count=row["chunk_count"],
                strategy=row["strategy"],
                source_path=source_path,
                source_raw_sha256=row["raw_sha256"],
            )

    def search(
        self,
        file_id: str,
        query: str,
        *,
        config=None,
        reranker=None,
        search_policy=None,
        version: str | None = None,
        version_id: str | None = None,
        expected_library_id: str | None = None,
        top_k: int = 5,
        context_char_budget: int = 8000,
    ) -> dict:
        from wenjin.retrieval.service import RetrievalService, SearchConfig, load_source_urls

        with self._rw_lock:
            snapshot = self.snapshot(file_id)
            if version_id is not None and version_id != snapshot.version_id:
                raise StaleSnapshotError("受管文件版本已变化，请刷新资料库清单后重试")
            if expected_library_id is not None and expected_library_id != snapshot.library_id:
                raise StaleSnapshotError("受管文件资料库归属已变化，请刷新后重试")
            service = RetrievalService(
                qdrant_client=self.qdrant_client,
                embedder=self.embedder,
                reranker=reranker,
                bm25_path=snapshot.bm25_path,
                source_urls=load_source_urls(snapshot.manifest_path),
                collection_name=snapshot.dense_collection,
                supported_version=snapshot.version,
                index_target_id=snapshot.version_id,
                search_lock=self._model_lock,
            )
            # version和active snapshot在同一把读锁内绑定；replace无法在两者之间切换active。
            if search_policy is not None:
                if search_policy.reranker_enabled != (reranker is not None):
                    raise RuntimeError("managed检索策略与共享reranker实例状态不一致")
                chosen = config or search_policy.single_config(
                    snapshot.version if version is None else version,
                    top_k,
                    context_char_budget=context_char_budget,
                )
            else:
                chosen = config or SearchConfig(
                    version=snapshot.version if version is None else version,
                    top_k=top_k,
                    context_char_budget=context_char_budget,
                )
            with self._model_lock:
                result = service.search(query, config=chosen)
            if search_policy is not None:
                result["retrieval_config"] = search_policy.public_config(
                    top_k, context_char_budget=context_char_budget
                )
            # 共享文件系统写入者不受manager读写锁控制；在返回证据前再核对原文身份。
            try:
                current = self.snapshot(file_id)
            except (OSError, RuntimeError) as error:
                raise StaleSnapshotError(
                    "受管文件原文在检索期间发生变化，请刷新资料库清单后重试"
                ) from error
            if (
                current.version_id != snapshot.version_id
                or current.source_raw_sha256 != snapshot.source_raw_sha256
            ):
                raise StaleSnapshotError("受管文件版本在检索期间发生变化，请刷新资料库清单后重试")
            # 检索与身份元数据来自同一份active snapshot；source hash已在snapshot()校验。
            result["managed_file"] = {
                "file_id": snapshot.file_id,
                "version_id": snapshot.version_id,
                "version": snapshot.version,
                "source_sha256": snapshot.source_raw_sha256,
            }
            for evidence in result.get("evidence", []):
                evidence["file_id"] = snapshot.file_id
                evidence["version_id"] = snapshot.version_id
                evidence["source_sha256"] = snapshot.source_raw_sha256
            return result

    def get_original(
        self,
        file_id: str,
        relative_path: str | None = None,
        *,
        expected_version_id: str | None = None,
        expected_library_id: str | None = None,
    ) -> bytes:
        from wenjin.manifest import file_records, load, trusted_raw_root

        with self._rw_lock:
            snapshot = self.snapshot(file_id)
            if expected_version_id is not None and snapshot.version_id != expected_version_id:
                raise StaleSnapshotError("受管文件版本已变化，请刷新资料库清单后重试")
            if expected_library_id is not None and expected_library_id != snapshot.library_id:
                raise StaleSnapshotError("受管文件资料库归属已变化，请刷新后重试")
            manifest = load(snapshot.manifest_path)
            records = file_records(manifest)
            requested = relative_path or Path(records[0]["path"]).as_posix()
            record = next((item for item in records if item["path"] == requested), None)
            if record is None:
                raise KeyError(requested)
            from wenjin.manifest import read_verified_bytes

            return read_verified_bytes(
                record, trusted_raw_root(manifest, self.managed_raw_data_root)
            )

    def create_retrieval_service(
        self, snapshot: FileSnapshot, *, qdrant_client=None, embedder=None
    ):
        """为入口或集成测试提供带版本约束的现有检索实现。"""
        from wenjin.retrieval.service import RetrievalService, load_source_urls

        return RetrievalService(
            qdrant_client=qdrant_client or self.qdrant_client,
            embedder=embedder or self.embedder,
            bm25_path=snapshot.bm25_path,
            source_urls=load_source_urls(snapshot.manifest_path),
            collection_name=snapshot.dense_collection,
            supported_version=snapshot.version,
            index_target_id=snapshot.version_id,
        )

    def _legacy_retained(self, file_id: str) -> list[dict]:
        if not file_id.startswith("legacy:"):
            return []
        pieces = file_id.split(":", 2)
        return [
            {
                "legacy_id": file_id,
                "library": pieces[1] if len(pieces) > 1 else None,
                "status": "retained_unmanaged",
                "may_remain_searchable": True,
            }
        ]

    def _legacy_artifacts_for_file(self, db: sqlite3.Connection, file_id: str) -> list[dict]:
        """按内容Hash指出仍存在于旧共享语料中的副本。"""
        hashes = {
            row[0]
            for row in db.execute(
                "SELECT DISTINCT raw_sha256 FROM versions WHERE file_id=?",
                (file_id,),
            )
        }
        if not hashes:
            return []
        from wenjin.catalog import corpus_catalog_report

        retained = []
        for corpus in corpus_catalog_report(self.root).entries:
            manifest_path = self.root / corpus.manifest_path
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in manifest.get("files", []):
                digest = item.get("raw_sha256") or item.get("sha256")
                if digest in hashes:
                    retained.append(
                        {
                            "corpus_id": corpus.corpus_id,
                            "library": corpus.library,
                            "version": corpus.version,
                            "manifest": corpus.manifest_path,
                            "source_path": item.get("path"),
                            "index_targets": list(corpus.index_targets),
                            "status": "retained_unmanaged",
                            "may_remain_searchable": True,
                        }
                    )
        return retained

    def submit_adopt(self, legacy_id: str) -> JobRecord:
        """把已登记历史文件复制为managed副本；源manifest、索引和route全部保留。"""
        record = next((item for item in self._list_legacy() if item.legacy_id == legacy_id), None)
        if record is None:
            raise KeyError(legacy_id)
        from wenjin.catalog import corpus_catalog_report
        from wenjin.manifest import file_records, load, trusted_raw_root

        corpus_id = legacy_id.split(":", 2)[1]
        corpus = next(
            (
                item
                for item in corpus_catalog_report(self.root).entries
                if item.corpus_id == corpus_id
            ),
            None,
        )
        if corpus is None:
            raise KeyError(legacy_id)
        manifest_path = self.root / corpus.manifest_path
        manifest = load(manifest_path)
        relative = next(
            (
                name
                for name in (item["path"] for item in file_records(manifest))
                if f"legacy:{corpus_id}:{hashlib.sha256(name.encode()).hexdigest()[:16]}"
                == legacy_id
            ),
            None,
        )
        if relative is None:
            raise KeyError(legacy_id)
        root = trusted_raw_root(manifest, self.managed_raw_data_root)
        source = (root / relative).resolve(strict=True)
        record_meta = next(item for item in file_records(manifest) if item["path"] == relative)
        if self._sha256(source) != record_meta["_content_hash"]:
            raise RuntimeError("legacy原文Hash不匹配，拒绝adopt")
        job = self.submit_import(source, display_name=Path(relative).name)
        if job.status == "duplicate":
            with self._connect() as db:
                resources = json.loads(
                    db.execute(
                        "SELECT resources FROM jobs WHERE job_id=?", (job.job_id,)
                    ).fetchone()["resources"]
                )
                resources["legacy_retained"] = [
                    {
                        "legacy_id": legacy_id,
                        "corpus_id": corpus_id,
                        "manifest": corpus.manifest_path,
                        "source_path": relative,
                        "legacy_may_remain_searchable": True,
                    }
                ]
                resources["legacy_may_remain_searchable"] = True
                db.execute(
                    "UPDATE jobs SET resources=? WHERE job_id=?",
                    (json.dumps(resources), job.job_id),
                )
            return self.get_job(job.job_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload,resources FROM jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            payload = json.loads(row["payload"])
            payload["adopted_from"] = {
                "legacy_id": legacy_id,
                "corpus_id": corpus_id,
                "manifest": corpus.manifest_path,
                "source_path": relative,
                "legacy_may_remain_searchable": True,
            }
            resources = json.loads(row["resources"])
            resources["legacy_retained"] = [payload["adopted_from"]]
            resources["legacy_may_remain_searchable"] = True
            db.execute(
                "UPDATE jobs SET payload=?,resources=? WHERE job_id=?",
                (json.dumps(payload), json.dumps(resources), job.job_id),
            )
        return self.get_job(job.job_id)

    def inspect_resources(self, file_id: str) -> dict:
        """只读对账快照：原文SHA、BM25行数与Qdrant点数。"""
        with self._rw_lock:
            snapshot = self.snapshot(file_id)
            with closing(sqlite3.connect(snapshot.bm25_path)) as db:
                bm25_rows = int(db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            dense_points = self.index_adapter.count(snapshot.dense_collection)
            return {
                "file_id": file_id,
                "version_id": snapshot.version_id,
                "source_exists": snapshot.source_path.is_file(),
                "source_sha256_matches": self._sha256(snapshot.source_path)
                == snapshot.source_raw_sha256,
                "manifest_exists": snapshot.manifest_path.is_file(),
                "bm25_path": snapshot.bm25_path.relative_to(self.root).as_posix(),
                "bm25_rows": bm25_rows,
                "dense_collection": snapshot.dense_collection,
                "dense_points": dense_points,
                "chunk_count": snapshot.chunk_count,
                "consistent": bm25_rows == snapshot.chunk_count == dense_points,
            }
