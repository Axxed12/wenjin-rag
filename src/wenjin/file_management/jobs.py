"""重试、恢复、执行和任务状态写入。锁实例由 FileManagementService 持有。"""

from __future__ import annotations

import json
from typing import Callable

from wenjin.file_management.clock import utc_now_iso
from wenjin.file_management.models import JobRecord


class JobLifecycle:
    """任务重试、恢复和执行；使用协调器注入的锁与连接回调。"""

    def __init__(
        self,
        connect: Callable,
        get_job: Callable,
        submit_delete: Callable,
        cleanup_failed_import: Callable,
        cleanup_cancelled_import: Callable,
        run_import: Callable,
        run_delete: Callable,
        rw_lock,
        job_lock,
        retry_lock,
        begin_immediate: Callable,
    ):
        self._connect = connect
        self.get_job = get_job
        self.submit_delete = submit_delete
        self._cleanup_failed_import = cleanup_failed_import
        self._cleanup_cancelled_import = cleanup_cancelled_import
        self._run_import = run_import
        self._run_delete = run_delete
        self._rw_lock = rw_lock
        self._job_lock = job_lock
        self._retry_lock = retry_lock
        self._begin_immediate = begin_immediate

    @staticmethod
    def _version_number(value: str | None) -> int | None:
        if not isinstance(value, str) or not value.startswith("v"):
            return None
        try:
            return int(value[1:])
        except ValueError:
            return None

    def submit_retry(self, job_id: str) -> JobRecord:
        # 同一root仅有一个service进程；本进程内先串行任务与重试，再重读SQLite状态。
        with self._retry_lock, self._job_lock:
            old = self.get_job(job_id)
            if not old.retryable or old.status not in {"failed", "deleting"}:
                raise ValueError("该任务当前不可重试")

            if old.kind == "delete":
                with self._rw_lock, self._connect() as db:
                    self._begin_immediate(db)
                    current = db.execute(
                        "SELECT status,retryable FROM jobs WHERE job_id=? AND kind='delete'",
                        (job_id,),
                    ).fetchone()
                    file_state = db.execute(
                        "SELECT status FROM files WHERE file_id=?", (old.file_id,)
                    ).fetchone()
                    if (
                        current is None
                        or current["status"] not in {"failed", "deleting"}
                        or not current["retryable"]
                        or file_state is None
                        or file_state["status"] != "deleting"
                    ):
                        raise ValueError("删除任务或文件状态已改变，不能重试")
                    # 沿用原任务和cleanup_items账本，崩溃/部分清理后幂等续跑。
                    db.execute(
                        "UPDATE jobs SET status='queued',phase='queued',progress=0,retryable=1,reason=NULL,cancel_requested=0,retry_count=retry_count+1,updated_at=? WHERE job_id=?",
                        (utc_now_iso(), job_id),
                    )
                return self.get_job(job_id)

            if old.kind != "import":
                raise ValueError("该任务类型不支持重试")

            with self._rw_lock:
                with self._connect() as db:
                    self._begin_immediate(db)
                    current = db.execute(
                    "SELECT j.status,j.retryable,j.cancel_requested,j.version_id,v.version AS candidate_version,"
                        "f.status AS file_status,f.active_version_id,av.version AS active_version "
                        "FROM jobs j JOIN files f ON f.file_id=j.file_id "
                        "LEFT JOIN versions v ON v.version_id=j.version_id "
                        "LEFT JOIN versions av ON av.version_id=f.active_version_id "
                        "WHERE j.job_id=? AND j.kind='import'",
                        (job_id,),
                    ).fetchone()
                    if current is None or current["status"] != "failed" or not current["retryable"]:
                        raise ValueError("文件或导入任务状态已改变，不能重试")
                    if current["cancel_requested"]:
                        raise ValueError("已请求取消的任务不能作为新导入重试")
                    if current["file_status"] in {"deleting", "deleted"}:
                        raise ValueError("文件正在删除或已经删除，不能重试导入")
                    version_id = current["version_id"]
                    if not version_id or current["active_version_id"] == version_id:
                        raise ValueError("活动版本不能作为失败候选清理或重试")
                    candidate_number = self._version_number(current["candidate_version"])
                    active_number = self._version_number(current["active_version"])
                    if candidate_number is None or (
                        active_number is not None and candidate_number <= active_number
                    ):
                        raise ValueError("较早的失败候选不能覆盖当前活动版本")

                # 锁内已确认该候选不是active，worker和另一次重试不能并发修改它。
                self._cleanup_failed_import(job_id, version_id)

                with self._connect() as db:
                    self._begin_immediate(db)
                    current = db.execute(
                        "SELECT j.status,j.retryable,f.status AS file_status,"
                        "f.active_version_id,v.version AS candidate_version,av.version AS active_version "
                        "FROM jobs j JOIN files f ON f.file_id=j.file_id "
                        "LEFT JOIN versions v ON v.version_id=j.version_id "
                        "LEFT JOIN versions av ON av.version_id=f.active_version_id "
                        "WHERE j.job_id=? AND j.kind='import'",
                        (job_id,),
                    ).fetchone()
                    if current is None or current["status"] != "failed" or not current["retryable"]:
                        raise ValueError("文件状态已改变，导入重试已取消")
                    if current["file_status"] in {"deleting", "deleted"}:
                        raise ValueError("文件正在删除或已经删除，导入重试已取消")
                    if current["active_version_id"] == version_id:
                        raise ValueError("活动版本不能作为失败候选清理或重试")
                    candidate_number = self._version_number(current["candidate_version"])
                    active_number = self._version_number(current["active_version"])
                    if candidate_number is None or (
                        active_number is not None and candidate_number <= active_number
                    ):
                        raise ValueError("较早的失败候选不能覆盖当前活动版本")
                    db.execute(
                        "UPDATE versions SET status='staging' WHERE version_id=? AND file_id=?",
                        (version_id, old.file_id),
                    )
                    if not current["active_version_id"]:
                        db.execute(
                            "UPDATE files SET status='importing',updated_at=? WHERE file_id=?",
                            (utc_now_iso(), old.file_id),
                        )
                    db.execute(
                        "UPDATE jobs SET status='queued',phase='queued',progress=0,retryable=1,reason=NULL,cancel_requested=0,retry_count=retry_count+1,updated_at=? WHERE job_id=?",
                        (utc_now_iso(), job_id),
                    )
            return self.get_job(job_id)

    def resume_pending_jobs(self) -> list[JobRecord]:

        with self._connect() as db:
            rows = db.execute(
                "SELECT job_id,status,phase,cancel_requested FROM jobs "
                "WHERE status='queued' OR (status='failed' AND phase='interrupted' AND retryable=1) "
                "OR (status='failed' AND cancel_requested=1) "
                "ORDER BY created_at"
            ).fetchall()

        resumed = []

        for row in rows:
            if row["status"] == "failed":
                if row["cancel_requested"]:
                    resumed.append(self._finish_requested_cancel(row["job_id"]))
                else:
                    resumed.append(self.submit_retry(row["job_id"]))

            else:
                resumed.append(self.get_job(row["job_id"]))

        return resumed

    def request_cancel(self, job_id: str) -> JobRecord:
        """Queued jobs clean immediately; running jobs only receive a cooperative flag."""
        cleanup_now = False
        with self._job_lock:
            with self._connect() as db:
                self._begin_immediate(db)
                row = db.execute(
                    "SELECT kind,status,cancel_requested FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                if row["kind"] != "import":
                    raise ValueError("只有导入任务支持取消")
                if row["status"] == "cancelled":
                    return self.get_job(job_id)
                if row["status"] == "queued":
                    db.execute(
                        "UPDATE jobs SET status='running',phase='cancel_cleanup',cancel_requested=1,"
                        "retryable=0,reason='正在清理取消的导入候选',updated_at=? WHERE job_id=? AND status='queued'",
                        (utc_now_iso(), job_id),
                    )
                    cleanup_now = True
                elif row["status"] == "running":
                    db.execute(
                        "UPDATE jobs SET cancel_requested=1,reason='已请求取消；等待当前处理阶段结束',updated_at=? "
                        "WHERE job_id=? AND status='running'",
                        (utc_now_iso(), job_id),
                    )
                elif row["status"] == "failed" and row["cancel_requested"]:
                    db.execute(
                        "UPDATE jobs SET status='running',phase='cancel_cleanup',retryable=0,"
                        "reason='正在重试取消候选清理',updated_at=? WHERE job_id=? AND status='failed'",
                        (utc_now_iso(), job_id),
                    )
                    cleanup_now = True
                else:
                    raise ValueError("该任务当前不能取消")
        if cleanup_now:
            return self._finish_requested_cancel(job_id)
        return self.get_job(job_id)

    def _finish_requested_cancel(self, job_id: str) -> JobRecord:
        with self._connect() as db:
            row = db.execute(
                "SELECT version_id FROM jobs WHERE job_id=? AND kind='import' AND cancel_requested=1",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ValueError("取消请求或候选登记已改变")
        try:
            self._cleanup_cancelled_import(job_id, row["version_id"])
        except Exception as error:
            self._update_job(
                job_id,
                status="failed",
                phase="cancel_cleanup_pending",
                progress=None,
                retryable=True,
                reason=f"取消候选清理未完成：{type(error).__name__}",
            )
        return self.get_job(job_id)

    def run_job(self, job_id: str) -> JobRecord:
        with self._job_lock:
            # 状态必须在job lock内重读；并发重复调度只允许一个worker领取queued任务。
            with self._connect() as db:
                self._begin_immediate(db)
                row = db.execute(
                    "SELECT job_id,file_id,kind,status,cancel_requested FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                if row["status"] != "queued":
                    return self.get_job(job_id)
                changed = db.execute(
                    "UPDATE jobs SET status='running',phase='starting',retryable=0,reason=NULL,updated_at=? "
                    "WHERE job_id=? AND status='queued'",
                    (utc_now_iso(), job_id),
                ).rowcount
                if changed != 1:
                    return self.get_job(job_id)
                job_file_id, job_kind = row["file_id"], row["kind"]
        try:
            if job_kind == "import":
                with self._connect() as db:
                    file_state = db.execute(
                        "SELECT status FROM files WHERE file_id=?", (job_file_id,)
                    ).fetchone()

                if file_state is None or file_state["status"] in {"deleting", "deleted"}:
                    self._update_job(
                        job_id,
                        status="cancelled",
                        phase="cancelled",
                        progress=100,
                        retryable=False,
                        reason="文件正在删除",
                    )

                    return self.get_job(job_id)

                self._run_import(job_id)

            elif job_kind == "delete":
                self._run_delete(job_id)

            else:
                raise ValueError(f"未知job类型：{job_kind}")

        except Exception as error:
            with self._connect() as db:
                latest = db.execute(
                    "SELECT status,version_id,cancel_requested FROM jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            if latest is None or latest["status"] in {"completed", "deleted", "cancelled"}:
                return self.get_job(job_id)
            if job_kind == "import" and latest["cancel_requested"]:
                return self._finish_requested_cancel(job_id)
            if job_kind == "import":
                with self._connect() as db:
                    db.execute(
                        "UPDATE versions SET status='failed' WHERE version_id=? AND status!='active'",
                        (latest["version_id"],),
                    )

                    file_row = db.execute(
                        "SELECT active_version_id,status FROM files WHERE file_id=?",
                        (job_file_id,),
                    ).fetchone()

                    if (
                        file_row
                        and not file_row["active_version_id"]
                        and file_row["status"] != "deleting"
                    ):
                        db.execute(
                            "UPDATE files SET status='failed',updated_at=? WHERE file_id=?",
                            (utc_now_iso(), job_file_id),
                        )

            self._update_job(
                job_id,
                status="failed",
                phase="failed",
                retryable=True,
                reason=f"{type(error).__name__}: {error}",
            )

        return self.get_job(job_id)

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

        assignments, values = ["phase=?", "updated_at=?"], [phase, utc_now_iso()]

        if status is not None:
            assignments.append("status=?")

            values.append(status)

        if progress is not None:
            assignments.append("progress=?")

            values.append(progress)

        if retryable is not None:
            assignments.append("retryable=?")

            values.append(int(retryable))

        assignments.append("reason=?")

        values.append(reason)

        if resources is not None:
            assignments.append("resources=?")

            values.append(json.dumps(resources, ensure_ascii=False))

        if cancel_requested is not None:
            assignments.append("cancel_requested=?")
            values.append(int(cancel_requested))

        values.append(job_id)

        with self._connect() as db:
            db.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id=?", values)
