"""Admin-only persistent evaluation task domain and cooperative runner lifecycle."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from wenjin.identity.policy import Forbidden, NotFound

_EXPERIMENT_ID = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_STATUSES = frozenset({
    "queued", "preparing", "running", "cancelling", "interrupted",
    "cancelled", "failed", "completed", "integrity_failure",
})


class EvaluationIntegrityError(RuntimeError):
    """The fixed run identity or one of its checkpoints failed verification."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _safe_json(value: str | None) -> Any:
    return json.loads(value) if value else None


@dataclass(frozen=True)
class EvaluationSpec:
    """Server-owned allowlist entry; callers can select its ID but cannot supply paths."""

    experiment_id: str
    title: str
    dataset_id: str
    profiles: tuple[str, ...]
    max_cases: int
    required_libraries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _EXPERIMENT_ID.fullmatch(self.experiment_id):
            raise ValueError("评测experiment_id格式无效")
        if not self.profiles or any(p not in {"baseline", "parent_child", "multi_query", "mmr"} for p in self.profiles):
            raise ValueError("评测profile不在开发评测白名单中")
        if not 1 <= self.max_cases <= 100:
            raise ValueError("评测case预算必须在1到100之间")


@dataclass(frozen=True)
class EvaluationJob:
    job_id: str
    experiment_id: str
    title: str
    owner_user_id: str
    status: str
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    stage: str
    completed_cases: int
    total_cases: int
    cancel_requested: bool
    input_snapshot: dict[str, Any] | None
    run_identity: dict[str, Any] | None
    result_summary: dict[str, Any] | None
    error_code: str | None
    revision: int


class EvaluationRunner(Protocol):
    def __call__(
        self,
        *,
        rows: list[dict[str, Any]],
        router: Any,
        config: Any,
        rewriter: Any | None,
        checkpoint_dir: Path,
        emit_case: Callable[[dict[str, Any]], None],
        progress: Callable[[int, int, str, str], None],
        should_cancel: Callable[[], bool],
        run_identity: Mapping[str, Any],
    ) -> dict[str, Any]: ...


@dataclass
class EvaluationContext:
    """Pinned inputs and shared runtime objects prepared before model work begins."""

    rows: list[dict[str, Any]]
    router: Any
    config: Any
    rewriter: Any | None
    input_snapshot: dict[str, Any]
    run_identity: dict[str, Any]
    close_owned: Callable[[], None] | None = None

    def close(self) -> None:
        # Router/client/embedder/reranker may be borrowed from the app runtime.
        # Only the context factory can identify resources it created and owns.
        if self.close_owned is not None:
            self.close_owned()


ContextFactory = Callable[[EvaluationJob, Path], EvaluationContext]


class EvaluationJobStore:
    """Independent SQLite state for evaluation tasks, using cross-process write locks."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        else:
            if db.in_transaction:
                db.commit()
        finally:
            db.close()

    def initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS evaluation_jobs (
                    job_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    stage TEXT NOT NULL,
                    completed_cases INTEGER NOT NULL DEFAULT 0,
                    total_cases INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    input_snapshot_json TEXT,
                    run_identity_json TEXT,
                    result_summary_json TEXT,
                    error_code TEXT,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS evaluation_jobs_created_idx
                    ON evaluation_jobs(created_at DESC, job_id DESC);
                CREATE TABLE IF NOT EXISTS evaluation_task_audit (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    job_id TEXT,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evaluation_task_audit_job_idx
                    ON evaluation_task_audit(job_id, occurred_at DESC);
            """)

    def create(self, *, job_id: str, spec: EvaluationSpec, owner_user_id: str, total_cases: int,
               input_snapshot: dict[str, Any], run_identity: dict[str, Any]) -> EvaluationJob:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("评测job_id格式无效")
        now = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO evaluation_jobs(job_id,experiment_id,title,owner_user_id,status,"
                "created_at,updated_at,stage,total_cases,input_snapshot_json,run_identity_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, spec.experiment_id, spec.title, owner_user_id, "queued", now, now,
                 "queued", total_cases, _json(input_snapshot), _json(run_identity)),
            )
            self._event(db, owner_user_id, "evaluation_submitted", job_id, {"experiment_id": spec.experiment_id})
            db.commit()
        return self.get(job_id)

    def _event(self, db: sqlite3.Connection, actor: str, action: str, job_id: str | None,
               metadata: dict[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO evaluation_task_audit VALUES(?,?,?,?,?,?)",
            (uuid4().hex, _now(), actor, action, job_id, _json(metadata or {})),
        )

    def update(self, job_id: str, *, expected_status: tuple[str, ...] | None = None,
               actor_user_id: str | None = None, action: str | None = None,
               metadata: dict[str, Any] | None = None, **fields: Any) -> EvaluationJob:
        allowed = {
            "status", "started_at", "finished_at", "stage", "completed_cases", "total_cases",
            "cancel_requested", "input_snapshot_json", "run_identity_json",
            "result_summary_json", "error_code",
        }
        if not fields or set(fields) - allowed:
            raise ValueError("评测任务更新字段无效")
        if "status" in fields and fields["status"] not in _STATUSES:
            raise ValueError("评测任务状态无效")
        now = _now()
        assignments = [f"{key}=?" for key in fields]
        values: list[Any] = [int(value) if key == "cancel_requested" else value
                             for key, value in fields.items()]
        assignments.extend(["updated_at=?", "revision=revision+1"])
        values.append(now)
        where = "job_id=?"
        values.append(job_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if expected_status:
                placeholders = ",".join("?" for _ in expected_status)
                where += f" AND status IN ({placeholders})"
                values.extend(expected_status)
            cursor = db.execute(
                f"UPDATE evaluation_jobs SET {','.join(assignments)} WHERE {where}", values,
            )
            if cursor.rowcount != 1:
                db.rollback()
                raise RuntimeError("评测任务状态已变化，请刷新后重试")
            if actor_user_id and action:
                self._event(db, actor_user_id, action, job_id, metadata)
            db.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> EvaluationJob:
        with self._connection() as db:
            row = db.execute("SELECT * FROM evaluation_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("评测任务不存在")
        return self._record(row)

    def list(self, *, limit: int = 50, offset: int = 0) -> tuple[list[EvaluationJob], int]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise ValueError("评测任务分页参数无效")
        with self._connection() as db:
            total = int(db.execute("SELECT count(*) FROM evaluation_jobs").fetchone()[0])
            rows = db.execute(
                "SELECT * FROM evaluation_jobs ORDER BY created_at DESC,job_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._record(row) for row in rows], total

    def mark_interrupted(self) -> list[str]:
        now = _now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT job_id,cancel_requested FROM evaluation_jobs "
                "WHERE status IN ('queued','preparing','running','cancelling')"
            ).fetchall()
            for row in rows:
                cancelled = bool(row["cancel_requested"])
                status = "cancelled" if cancelled else "interrupted"
                db.execute(
                    "UPDATE evaluation_jobs SET status=?,stage=?,finished_at=?,"
                    "cancel_requested=?,updated_at=?,revision=revision+1 WHERE job_id=?",
                    (status, status, now if cancelled else None, int(cancelled), now, row["job_id"]),
                )
            db.commit()
        return [row["job_id"] for row in rows]

    def record_event(self, actor_user_id: str, action: str, job_id: str | None,
                     metadata: dict[str, Any] | None = None) -> None:
        if action not in {
            "evaluation_list_read", "evaluation_status_read", "evaluation_result_read",
            "evaluation_submitted", "evaluation_cancel_requested", "evaluation_resume_requested",
            "evaluation_access_revoked",
        }:
            raise ValueError("评测审计动作无效")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._event(db, actor_user_id, action, job_id, metadata)
            db.commit()

    def list_audit(self, *, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        if not 1 <= limit <= 200 or not 0 <= offset <= 100000:
            raise ValueError("评测审计分页参数无效")
        with self._connection() as db:
            total = int(db.execute("SELECT count(*) FROM evaluation_task_audit").fetchone()[0])
            rows = db.execute(
                "SELECT * FROM evaluation_task_audit ORDER BY occurred_at DESC,event_id DESC "
                "LIMIT ? OFFSET ?", (limit, offset),
            ).fetchall()
        return [{
            "event_id": row["event_id"], "occurred_at": row["occurred_at"],
            "actor_user_id": row["actor_user_id"], "action": row["action"],
            "job_id": row["job_id"], "metadata": _safe_json(row["metadata_json"]),
        } for row in rows], total

    @staticmethod
    def _record(row: sqlite3.Row) -> EvaluationJob:
        return EvaluationJob(
            job_id=row["job_id"], experiment_id=row["experiment_id"], title=row["title"],
            owner_user_id=row["owner_user_id"], status=row["status"], created_at=row["created_at"],
            updated_at=row["updated_at"], started_at=row["started_at"], finished_at=row["finished_at"],
            stage=row["stage"], completed_cases=int(row["completed_cases"]),
            total_cases=int(row["total_cases"]), cancel_requested=bool(row["cancel_requested"]),
            input_snapshot=_safe_json(row["input_snapshot_json"]),
            run_identity=_safe_json(row["run_identity_json"]),
            result_summary=_safe_json(row["result_summary_json"]), error_code=row["error_code"],
            revision=int(row["revision"]),
        )


class EvaluationTaskService:
    """Admin-authorized task lifecycle; runners receive only server-owned snapshots."""

    def __init__(self, *, root: Path, store: EvaluationJobStore, identity_store: Any,
                 specs: Mapping[str, EvaluationSpec], context_factory: ContextFactory,
                 runner: EvaluationRunner,
                 binding_factory: Callable[[EvaluationSpec, str, Any], tuple[dict[str, Any], dict[str, Any]]]) -> None:
        self.root = Path(root).resolve()
        self.store = store
        self.identity_store = identity_store
        self.specs = dict(specs)
        self.context_factory = context_factory
        self.runner = runner
        self.binding_factory = binding_factory
        self._task_lock = asyncio.Lock()
        self._worker_semaphore = asyncio.Semaphore(1)
        self._running: dict[str, asyncio.Task] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._cancel_reason: dict[str, str] = {}
        self._actors: dict[str, Any] = {}
        self._closed = False

    def _require_admin(self, actor: Any) -> None:
        if actor is None or getattr(actor, "role", None) != "admin" or getattr(actor, "must_change_password", False):
            raise Forbidden("仅完成改密的管理员可管理评测任务")
        # Revalidate actor against authoritative account storage on every API operation.
        self.identity_store.list_users(actor)

    def missing_libraries(self, actor: Any, spec: EvaluationSpec) -> tuple[str, ...]:
        """Return only the fixed template's missing grant IDs; callers may summarize, not expose them."""
        self._require_admin(actor)
        return tuple(
            library_id for library_id in spec.required_libraries
            if not self.identity_store.can_read(actor, "library", library_id)
        )

    def _require_job_resources(self, actor: Any, job: EvaluationJob) -> None:
        """评测管理员仍须拥有题目涉及资料库的当前读取授权。"""
        self._require_admin(actor)
        snapshot = job.input_snapshot or {}
        resources = snapshot.get("resource_ids")
        if not isinstance(resources, list) or not resources:
            raise Forbidden("评测任务没有可验证的资料库授权范围")
        for resource_id in resources:
            if not isinstance(resource_id, str) or not self.identity_store.can_read(
                actor, "library", resource_id
            ):
                raise Forbidden("当前管理员缺少评测资料库读取授权")

    def _job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise NotFound("评测任务不存在")
        task_root = (self.root / "artifacts" / "evaluations").resolve()
        path = (task_root / job_id).resolve()
        if (not task_root.is_relative_to(self.root)
                or not path.is_relative_to(task_root)):
            raise NotFound("评测任务不存在")
        return path

    @staticmethod
    def _identity_hash(run_identity: dict[str, Any]) -> str:
        from wenjin.retrieval.strategy_experiment import run_identity_sha256

        return run_identity_sha256(run_identity)

    def _validate_case_checkpoint(
        self, path: Path, *, expected_ids: set[str], run_identity: dict[str, Any],
    ) -> dict[str, Any]:
        if path.name.endswith(".tmp"):
            raise EvaluationIntegrityError("评测checkpoint仍处于临时写入状态")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EvaluationIntegrityError("评测checkpoint无法读取") from error
        if not isinstance(record, dict):
            raise EvaluationIntegrityError("评测checkpoint结构无效")
        return self._validate_case_record(
            record, filename=path.name, expected_ids=expected_ids, run_identity=run_identity,
        )

    def _validate_case_record(
        self, record: dict[str, Any], *, filename: str,
        expected_ids: set[str], run_identity: dict[str, Any],
    ) -> dict[str, Any]:
        case_id = record.get("case_id")
        if (record.get("schema_version") != 1 or case_id not in expected_ids
                or filename != f"{case_id}.json"):
            raise EvaluationIntegrityError("评测checkpoint不属于当前模板case集合")
        if record.get("run_identity_sha256") != self._identity_hash(run_identity):
            raise EvaluationIntegrityError("评测checkpoint运行身份不匹配")
        identities = run_identity.get("case_identities")
        if not isinstance(identities, dict) or record.get("case_identity") != identities.get(case_id):
            raise EvaluationIntegrityError("评测checkpoint case身份不匹配")
        stored_sha = record.get("record_sha256")
        from wenjin.retrieval.strategy_experiment import checkpoint_record_hash

        if not isinstance(stored_sha, str) or stored_sha != checkpoint_record_hash(record):
            raise EvaluationIntegrityError("评测checkpoint记录hash无效")
        profiles = run_identity.get("profiles")
        if not isinstance(record.get("profiles"), dict) or set(record["profiles"]) != set(profiles or ()):
            raise EvaluationIntegrityError("评测checkpoint策略臂与任务绑定不一致")
        return record

    def _validated_checkpoints(
        self, job_id: str, *, expected_ids: set[str], run_identity: dict[str, Any],
        allow_temporary: bool = True,
    ) -> dict[str, dict[str, Any]]:
        directory = self._job_dir(job_id) / "cases"
        if not directory.exists():
            return {}
        records: dict[str, dict[str, Any]] = {}
        for path in directory.iterdir():
            if path.suffix == ".tmp":
                temporary_case = path.name.removesuffix(".json.tmp")
                if (not allow_temporary or not path.name.endswith(".json.tmp")
                        or temporary_case not in expected_ids):
                    raise EvaluationIntegrityError("评测checkpoint目录包含不属于固定case的临时文件")
                continue
            if path.suffix != ".json":
                raise EvaluationIntegrityError("评测checkpoint目录包含未知文件")
            item = self._validate_case_checkpoint(
                path, expected_ids=expected_ids, run_identity=run_identity,
            )
            if item["case_id"] in records:
                raise EvaluationIntegrityError("评测checkpoint存在重复case")
            records[item["case_id"]] = item
        return records

    @staticmethod
    def _expected_profile_summary(
        checkpoints: Mapping[str, Mapping[str, Any]], profiles: list[str],
    ) -> dict[str, dict[str, float | int]]:
        expected: dict[str, dict[str, float | int]] = {}
        for profile in profiles:
            records = [case["profiles"][profile] for case in checkpoints.values()]
            elapsed = [float(item.get("timing_seconds", {}).get("total", 0.0)) for item in records]
            expected[profile] = {
                "cases": len(records),
                "selected_evidence": sum(len(item.get("selected", [])) for item in records),
                "candidate_count": sum(len(item.get("ranked_candidates", [])) for item in records),
                "mean_total_seconds": round(sum(elapsed) / max(1, len(elapsed)), 6),
            }
        return expected

    async def submit(self, actor: Any, experiment_id: str) -> EvaluationJob:
        self._require_admin(actor)
        spec = self.specs.get(experiment_id)
        if spec is None:
            raise NotFound("评测模板不存在或未开放")
        if self.missing_libraries(actor, spec):
            raise Forbidden("评测资料库读取授权不足")
        if self._closed:
            raise RuntimeError("评测服务正在关闭")
        proposed_job_id = uuid4().hex
        input_snapshot, run_identity = await asyncio.to_thread(
            self.binding_factory, spec, proposed_job_id, actor
        )
        if not isinstance(input_snapshot, dict) or not isinstance(run_identity, dict):
            raise ValueError("评测模板绑定器必须返回input_snapshot与run_identity对象")
        total_cases = input_snapshot.get("dataset_case_count")
        if (not isinstance(total_cases, int) or isinstance(total_cases, bool)
                or not 1 <= total_cases <= spec.max_cases):
            raise EvaluationIntegrityError("固定模板题目数量无效或超过预算")
        resource_ids = input_snapshot.get("resource_ids")
        if not isinstance(resource_ids, list) or not resource_ids:
            raise EvaluationIntegrityError("评测快照缺少资料库授权范围")
        for resource_id in resource_ids:
            if not isinstance(resource_id, str) or not self.identity_store.can_read(
                actor, "library", resource_id
            ):
                raise Forbidden("管理员尚未获得评测资料库读取授权")
        job = await asyncio.to_thread(
            self.store.create, job_id=proposed_job_id, spec=spec, owner_user_id=actor.user_id,
            total_cases=total_cases, input_snapshot=input_snapshot, run_identity=run_identity,
        )
        self._actors[job.job_id] = actor
        await self._schedule(job.job_id)
        return job

    async def list_jobs(self, actor: Any, *, limit: int = 50, offset: int = 0):
        self._require_admin(actor)
        await asyncio.to_thread(
            self.store.record_event, actor.user_id, "evaluation_list_read", None,
            {"limit": limit, "offset": offset},
        )
        return await asyncio.to_thread(self.store.list, limit=limit, offset=offset)

    async def get_job(self, actor: Any, job_id: str) -> EvaluationJob:
        self._require_admin(actor)
        await asyncio.to_thread(
            self.store.record_event, actor.user_id, "evaluation_status_read", job_id,
        )
        return await asyncio.to_thread(self.store.get, job_id)

    async def list_audit(self, actor: Any, *, limit: int = 100, offset: int = 0):
        self._require_admin(actor)
        await asyncio.to_thread(
            self.store.record_event, actor.user_id, "evaluation_list_read", None,
            {"kind": "audit", "limit": limit, "offset": offset},
        )
        return await asyncio.to_thread(self.store.list_audit, limit=limit, offset=offset)

    async def cancel(self, actor: Any, job_id: str) -> EvaluationJob:
        self._require_admin(actor)
        job = await asyncio.to_thread(self.store.get, job_id)
        event = self._cancel.get(job_id)
        if event is not None:
            self._cancel_reason[job_id] = "user_cancel"
            event.set()
        if job.status in {"queued", "preparing", "running"}:
            return await asyncio.to_thread(
                self.store.update, job_id, expected_status=(job.status,),
                actor_user_id=actor.user_id, action="evaluation_cancel_requested",
                cancel_requested=True, status="cancelling", stage="cancelling",
            )
        if job.status == "cancelling":
            return job
        raise RuntimeError("当前评测任务状态不可取消")

    async def resume(self, actor: Any, job_id: str) -> EvaluationJob:
        self._require_admin(actor)
        job = await asyncio.to_thread(self.store.get, job_id)
        if job.status not in {"interrupted", "failed"}:
            raise RuntimeError("只有中断或可恢复失败的任务可以恢复")
        if job.error_code == "EvaluationIntegrityError":
            raise RuntimeError("评测输入或checkpoint完整性失败，请新建任务而非恢复")
        self._require_job_resources(actor, job)
        if self._closed:
            raise RuntimeError("评测服务正在关闭")
        event = self._cancel.pop(job_id, None)
        self._cancel_reason.pop(job_id, None)
        if event is not None:
            event.clear()
        updated = await asyncio.to_thread(
            self.store.update, job_id, expected_status=(job.status,), actor_user_id=actor.user_id,
            action="evaluation_resume_requested", cancel_requested=False, status="queued",
            stage="queued", error_code=None,
        )
        self._actors[job_id] = actor
        await self._schedule(job_id)
        return updated

    async def _schedule(self, job_id: str) -> None:
        async with self._task_lock:
            current = self._running.get(job_id)
            if current is None or current.done():
                cancel = threading.Event()
                self._cancel[job_id] = cancel
                self._cancel_reason[job_id] = "none"
                self._running[job_id] = asyncio.create_task(self._execute_guarded(job_id, cancel))

    async def _execute_guarded(self, job_id: str, cancel: threading.Event) -> None:
        async with self._worker_semaphore:
            await self._execute(job_id, cancel)

    async def _execute(self, job_id: str, cancel: threading.Event) -> None:
        context: EvaluationContext | None = None
        try:
            job = await asyncio.to_thread(self.store.get, job_id)
            if cancel.is_set():
                terminal = self._stop_status(job_id)
                await asyncio.to_thread(
                    self.store.update, job_id, expected_status=(job.status,),
                    status=terminal, stage=terminal,
                    finished_at=None if terminal == "interrupted" else _now(),
                    cancel_requested=terminal == "cancelled",
                )
                return
            await asyncio.to_thread(
                self.store.update, job_id, expected_status=("queued",),
                status="preparing", stage="verifying_bound_inputs", started_at=job.started_at or _now(),
                finished_at=None, error_code=None,
            )
            job = await asyncio.to_thread(self.store.get, job_id)
            if not await asyncio.to_thread(self._check_job_access, job_id, job):
                raise InterruptedError("评测资料库授权已撤销")
            context = await asyncio.to_thread(self.context_factory, job, self._job_dir(job_id))
            if job.run_identity and job.run_identity != context.run_identity:
                raise EvaluationIntegrityError("恢复指纹不匹配，拒绝复用旧checkpoint")
            if job.input_snapshot and job.input_snapshot != context.input_snapshot:
                raise EvaluationIntegrityError("恢复输入快照不匹配，拒绝复用旧checkpoint")
            expected_case_ids = {
                str(row.get("case_id", row.get("id", ""))) for row in context.rows
            }
            if (not context.rows or len(context.rows) > self.specs[job.experiment_id].max_cases
                    or "" in expected_case_ids or len(expected_case_ids) != len(context.rows)
                    or len(expected_case_ids) != job.total_cases):
                raise EvaluationIntegrityError("评测runner输入数量不符合模板预算")
            if not await asyncio.to_thread(self._check_job_access, job_id, job):
                raise InterruptedError("评测资料库授权已撤销")
            existing = await asyncio.to_thread(
                self._validated_checkpoints, job_id, expected_ids=expected_case_ids,
                run_identity=context.run_identity,
            )
            await asyncio.to_thread(
                self.store.update, job_id, expected_status=("preparing",),
                completed_cases=len(existing),
            )
            await asyncio.to_thread(
                self.store.update, job_id, expected_status=("preparing",),
                input_snapshot_json=_json(context.input_snapshot),
                run_identity_json=_json(context.run_identity),
                stage="ready",
            )
            if cancel.is_set():
                raise InterruptedError("任务在模型阶段开始前已取消")

            def emit_case(record: dict[str, Any]) -> None:
                case_id = record.get("case_id") or record.get("id")
                if (not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id)
                        or case_id not in expected_case_ids):
                    raise EvaluationIntegrityError("runner返回的case_id格式无效")
                identity = self._validate_case_record(
                    record, filename=f"{case_id}.json", expected_ids=expected_case_ids,
                    run_identity=context.run_identity,
                )
                directory = self._job_dir(job_id) / "cases"
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / f"{case_id}.json"
                if destination.exists():
                    existing_record = self._validate_case_checkpoint(
                        destination, expected_ids=expected_case_ids,
                        run_identity=context.run_identity,
                    )
                    if existing_record != identity:
                        raise EvaluationIntegrityError("runner试图覆盖已验证的评测checkpoint")
                    return
                temporary = destination.with_suffix(".json.tmp")
                raw = _json(identity).encode("utf-8")
                temporary.write_bytes(raw + b"\n")
                temporary.replace(destination)
                completed = len(self._validated_checkpoints(
                    job_id, expected_ids=expected_case_ids, run_identity=context.run_identity,
                ))
                self.store.update(job_id, expected_status=("running", "cancelling"),
                                  completed_cases=completed)

            def progress(done: int, total: int, case_id: str, stage: str) -> None:
                if (total != len(expected_case_ids) or not 0 <= done <= total
                        or not isinstance(case_id, str) or len(case_id) > 120):
                    raise ValueError("runner进度参数无效")
                self.store.update(job_id, expected_status=("running", "cancelling"),
                                  stage=f"{stage}:{case_id}"[:160])

            def should_cancel() -> bool:
                if cancel.is_set():
                    return True
                current = self.store.get(job_id)
                return not self._check_job_access(job_id, current)

            self.store.update(job_id, expected_status=("preparing",), status="running", stage="running")
            summary = await asyncio.to_thread(
                self.runner, rows=context.rows, router=context.router, config=context.config,
                rewriter=context.rewriter, checkpoint_dir=self._job_dir(job_id) / "cases",
                emit_case=emit_case, progress=progress,
                should_cancel=should_cancel, run_identity=context.run_identity,
            )
            checkpoints = await asyncio.to_thread(
                self._validated_checkpoints, job_id, expected_ids=expected_case_ids,
                run_identity=context.run_identity, allow_temporary=False,
            )
            expected_assets = context.run_identity.get("assets")
            verify_assets = getattr(context.router, "verify_run_identity", None)
            assets_after = await asyncio.to_thread(verify_assets) if callable(verify_assets) else None
            if (not isinstance(expected_assets, dict)
                    or summary.get("assets_before") != expected_assets
                    or summary.get("assets_after") != expected_assets
                    or assets_after != expected_assets
                    or summary.get("assets_unchanged") is not True):
                raise EvaluationIntegrityError("运行前后资产指纹不一致，评测不能标记completed")
            checkpoint_ids = set(checkpoints)
            summary_ids = summary.get("valid_case_ids")
            if (not isinstance(summary_ids, list) or set(summary_ids) != checkpoint_ids
                    or len(summary_ids) != len(checkpoints)):
                raise EvaluationIntegrityError("runner summary case集合与已验证checkpoint不一致")
            if not cancel.is_set() and (
                summary.get("status") != "completed" or checkpoint_ids != expected_case_ids
            ):
                raise EvaluationIntegrityError("评测未完成全部固定case checkpoint，结果不能标记completed")
            if not cancel.is_set():
                profiles = context.run_identity.get("profiles")
                if (summary.get("case_count") != len(expected_case_ids)
                        or summary.get("profiles") != self._expected_profile_summary(
                            checkpoints, profiles if isinstance(profiles, list) else []
                        )):
                    raise EvaluationIntegrityError("评测summary未由完整case checkpoint推导")
            if not cancel.is_set():
                current_job = await asyncio.to_thread(self.store.get, job_id)
                if not await asyncio.to_thread(self._check_job_access, job_id, current_job):
                    cancel.set()
            final_status = self._stop_status(job_id) if cancel.is_set() else "completed"
            if final_status == "completed":
                if checkpoint_ids != expected_case_ids:
                    raise EvaluationIntegrityError("完成任务必须恰好包含全部固定case")
                self._write_job_json(job_id, "summary.json", summary)
                if isinstance(summary.get("comparison"), dict):
                    self._write_job_json(job_id, "comparison.json", summary["comparison"])
                self._write_job_json(job_id, "run.json", context.run_identity)
            self.store.update(
                job_id, expected_status=("running", "cancelling"), status=final_status,
                finished_at=None if final_status == "interrupted" else _now(),
                stage=final_status, result_summary_json=_json(summary),
                cancel_requested=final_status == "cancelled",
            )
        except InterruptedError:
            try:
                self.store.update(job_id, expected_status=("preparing", "running", "cancelling"),
                                  status=self._stop_status(job_id),
                                  stage=self._stop_status(job_id),
                                  finished_at=None if self._stop_status(job_id) == "interrupted" else _now(),
                                  cancel_requested=self._stop_status(job_id) == "cancelled")
            except RuntimeError:
                pass
        except Exception as error:
            try:
                stop_status = self._stop_status(job_id) if cancel.is_set() else None
                interrupted = stop_status == "interrupted"
                cancelled = stop_status == "cancelled"
                failed_status = (
                    "integrity_failure" if isinstance(error, EvaluationIntegrityError) else "failed"
                )
                self.store.update(
                    job_id, expected_status=("preparing", "running", "cancelling"),
                    status="interrupted" if interrupted else "cancelled" if cancelled else failed_status,
                    stage="interrupted" if interrupted else "cancelled" if cancelled else failed_status,
                    finished_at=None if interrupted else _now(),
                    error_code=None if interrupted or cancelled else type(error).__name__[:80],
                    cancel_requested=interrupted or cancelled,
                )
            except RuntimeError:
                pass
        finally:
            try:
                if context is not None:
                    await asyncio.to_thread(context.close)
            except Exception as error:
                # Resource cleanup failure must not retain actor/event/task references.
                try:
                    await asyncio.to_thread(
                        self.store.record_event, "system", "evaluation_context_close_failed",
                        job_id, {"error_code": type(error).__name__[:80]},
                    )
                except Exception:
                    pass
            finally:
                self._running.pop(job_id, None)
                self._cancel.pop(job_id, None)
                self._cancel_reason.pop(job_id, None)
                self._actors.pop(job_id, None)

    def _stop_status(self, job_id: str) -> str:
        # A user cancellation remains terminal even if shutdown begins afterward.
        return "interrupted" if self._cancel_reason.get(job_id) == "shutdown" else "cancelled"

    def _check_job_access(self, job_id: str, job: EvaluationJob) -> bool:
        try:
            self._require_job_resources(self._actors.get(job_id), job)
            return True
        except Exception:
            reason = self._cancel_reason.get(job_id)
            if reason not in {"shutdown", "user_cancel"}:
                self._cancel_reason[job_id] = "access_revoked"
                event = self._cancel.get(job_id)
                if event is not None:
                    event.set()
                self.store.record_event(
                    "system", "evaluation_access_revoked", job_id,
                    {"owner_user_id": job.owner_user_id},
                )
            return False

    async def shutdown(self) -> None:
        self._closed = True
        for job_id, event in self._cancel.items():
            if self._cancel_reason.get(job_id) != "user_cancel":
                self._cancel_reason[job_id] = "shutdown"
            event.set()
        tasks = list(self._running.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(self.store.mark_interrupted)

    async def result_path(self, actor: Any, job_id: str, filename: str,
                          *, case_id: str | None = None) -> Path:
        self._require_admin(actor)
        job = await asyncio.to_thread(self.store.get, job_id)
        if job.status != "completed":
            raise RuntimeError("评测结果尚未完成")
        self._require_job_resources(actor, job)
        if case_id is not None:
            if not _CASE_ID.fullmatch(case_id):
                raise NotFound("评测case不存在")
            run_identity = job.run_identity or {}
            expected_ids = set((run_identity.get("case_identities") or {}).keys())
            if case_id not in expected_ids:
                raise NotFound("评测case不存在")
            records = await asyncio.to_thread(
                self._validated_checkpoints, job_id, expected_ids=expected_ids,
                run_identity=run_identity, allow_temporary=False,
            )
            if set(records) != expected_ids or case_id not in records:
                raise EvaluationIntegrityError("已完成任务的case checkpoint集合无效")
            filename = f"{case_id}.json"
            path = self._job_dir(job_id) / "cases" / filename
            await asyncio.to_thread(
                self.store.record_event, actor.user_id, "evaluation_result_read", job_id,
                {"case_id": case_id},
            )
            return path
        if filename not in {"summary.json", "comparison.json", "run.json"}:
            raise NotFound("评测结果文件不存在")
        path = self._job_dir(job_id) / filename
        if not path.is_file():
            raise NotFound("评测结果文件不存在")
        await asyncio.to_thread(
            self.store.record_event, actor.user_id, "evaluation_result_read", job_id,
            {"filename": filename},
        )
        return path

    def _write_job_json(self, job_id: str, filename: str, value: dict[str, Any]) -> None:
        if filename not in {"summary.json", "comparison.json", "run.json"}:
            raise ValueError("评测结果文件名不在服务端白名单中")
        directory = self._job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(target)
