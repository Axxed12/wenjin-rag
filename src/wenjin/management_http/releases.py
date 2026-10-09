"""管理员共享资料库release API与异步prepare任务状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wenjin.catalog import index_target_configs
from wenjin.index_registry import IndexTarget
from wenjin.management_http.http_common import _jsonable
from wenjin.releases.catalog import ReleaseCatalog, ReleaseConflictError
from wenjin.releases.resolver import release_catalog_path
from wenjin.releases.service import ReleaseService
from wenjin.retrieval.router import normalize_library


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReleaseJobStore:
    """持久化prepare进度；它只记录候选工作，不拥有/清理release资源。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute(
                """CREATE TABLE IF NOT EXISTS release_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    library_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    target_json TEXT,
                    target_sha256 TEXT,
                    reason TEXT NOT NULL,
                    actor TEXT,
                    stage TEXT NOT NULL DEFAULT 'queued',
                    progress INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    result_json TEXT,
                    error_json TEXT
                )"""
            )
            columns = {row[1] for row in database.execute("PRAGMA table_info(release_jobs)")}
            if "target_json" not in columns:
                database.execute("ALTER TABLE release_jobs ADD COLUMN target_json TEXT")
            if "target_sha256" not in columns:
                database.execute("ALTER TABLE release_jobs ADD COLUMN target_sha256 TEXT")

    @contextmanager
    def _connect(self):
        database = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA busy_timeout=30000")
            yield database
        finally:
            database.close()

    def create(
        self, *, library_id: str, target: IndexTarget, reason: str, actor: str | None
    ):
        job_id = uuid4().hex
        now = _now()
        target_json = json.dumps(target.__dict__, ensure_ascii=False, sort_keys=True)
        target_sha256 = hashlib.sha256(target_json.encode("utf-8")).hexdigest()
        with self._connect() as database:
            database.execute(
                "INSERT INTO release_jobs(job_id,status,library_id,target_id,target_json,"
                "target_sha256,reason,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, "queued", library_id, target.target_id, target_json,
                    target_sha256, reason, actor, now, now,
                ),
            )
        return self.get(job_id)

    def update(
        self, job_id: str, *, status: str, stage: str | None = None,
        progress: int | None = None, result=None, error=None,
    ):
        with self._connect() as database:
            cursor = database.execute(
                "UPDATE release_jobs SET status=?,stage=COALESCE(?,stage),"
                "progress=COALESCE(?,progress),updated_at=?,result_json=?,error_json=? "
                "WHERE job_id=?",
                (
                    status,
                    stage,
                    progress,
                    _now(),
                    json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
                    json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None,
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.get(job_id)

    def get(self, job_id: str):
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM release_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        record = dict(row)
        result_json = record.pop("result_json")
        error_json = record.pop("error_json")
        record["result"] = json.loads(result_json) if result_json else None
        record["error"] = json.loads(error_json) if error_json else None
        return record

    def pending(self):
        with self._connect() as database:
            rows = database.execute(
                "SELECT job_id FROM release_jobs WHERE status IN ('queued','preparing') "
                "ORDER BY created_at"
            ).fetchall()
        return [self.get(str(row[0])) for row in rows]


def _store(request: Request) -> ReleaseJobStore:
    store = getattr(request.app.state, "release_job_store", None)
    if store is None:
        store = ReleaseJobStore(release_catalog_path(request.app.state.root).with_name("release-jobs.sqlite"))
        request.app.state.release_job_store = store
    return store


def _public_job(job: dict) -> dict:
    # target载荷和fingerprint只用于worker固定候选输入，不经管理API暴露。
    public_fields = {
        "job_id", "status", "library_id", "target_id", "reason", "actor",
        "stage", "progress", "created_at", "updated_at", "result", "error",
    }
    return {key: value for key, value in job.items() if key in public_fields}


def _release_service(request: Request) -> ReleaseService:
    root = Path(request.app.state.root).resolve()
    catalog_path = release_catalog_path(root)
    if not catalog_path.is_file():
        raise HTTPException(404, "资料库尚无发布记录")
    request.app.state.release_catalog_seen = True
    client = getattr(request.app.state, "qdrant_client", None)
    if client is None:
        client = getattr(request.app.state.manager, "qdrant_client", None)
    if client is None:
        raise HTTPException(503, "Qdrant客户端尚未就绪")
    return ReleaseService(root, ReleaseCatalog(release_catalog_path(root)), client)


def _actor(request: Request) -> str | None:
    principal = getattr(request.state, "principal", None)
    return getattr(principal, "user_id", None)


async def _payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "请求体必须是JSON对象") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是JSON对象")
    return payload


async def _run_prepare(app, job_id: str) -> None:
    store = app.state.release_job_store
    try:
        async with app.state.release_job_semaphore:
            job = store.update(
                job_id, status="preparing", stage="validating_and_sealing", progress=5
            )
            root = Path(app.state.root).resolve()
            target_payload = job.get("target_json")
            if not target_payload:
                raise ValueError("该旧任务没有固定候选快照，请新建prepare任务")
            target_data = json.loads(target_payload)
            target = IndexTarget.from_dict(target_data)
            target_sha256 = hashlib.sha256(
                json.dumps(target.__dict__, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if target_sha256 != job.get("target_sha256"):
                raise ValueError("任务候选快照指纹不匹配")
            config = index_target_configs(root).get(job["target_id"])
            if config is None:
                raise ValueError("候选target不存在或未登记")
            registered = IndexTarget.from_dict(config)
            registered_sha256 = hashlib.sha256(
                json.dumps(registered.__dict__, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if registered_sha256 != target_sha256:
                raise ValueError("候选配置在任务排队期间已变化，请新建prepare任务")
            if normalize_library(target.library) != normalize_library(job["library_id"]):
                raise ValueError("候选target不属于所选资料库")
            service = _release_service_for_app(app)
            record = await run_in_threadpool(
                service.prepare,
                job["library_id"],
                target,
                reason=job["reason"],
                actor=job["actor"],
            )
            store.update(
                job_id,
                status="ready",
                stage="ready",
                progress=100,
                result={"release_id": record["release_id"], "status": record["status"]},
            )
    except Exception as exc:
        store.update(
            job_id,
            status="failed",
            stage="failed",
            error={"type": type(exc).__name__, "detail": str(exc)[:500]},
        )
    finally:
        app.state.running_release_jobs.pop(job_id, None)


def _release_service_for_app(app) -> ReleaseService:
    root = Path(app.state.root).resolve()
    client = getattr(app.state, "qdrant_client", None)
    if client is None:
        client = getattr(app.state.manager, "qdrant_client", None)
    if client is None:
        raise RuntimeError("Qdrant客户端尚未就绪")
    app.state.release_catalog_seen = True
    return ReleaseService(root, ReleaseCatalog(release_catalog_path(root)), client)


async def _schedule_prepare(request: Request, job_id: str) -> None:
    running = request.app.state.running_release_jobs
    task = running.get(job_id)
    if task is None or task.done():
        running[job_id] = asyncio.create_task(_run_prepare(request.app, job_id))


async def _release_list(request: Request):
    library_id = request.path_params["library_id"]
    path = release_catalog_path(request.app.state.root)
    if not path.is_file() and getattr(request.app.state, "release_catalog_seen", False):
        raise HTTPException(503, "release catalog运行期间丢失，拒绝显示旧目录")
    if path.is_file():
        request.app.state.release_catalog_seen = True
    snapshot = (
        ReleaseCatalog(path).snapshot(library_id)
        if path.is_file()
        else {
            "revision": 0,
            "library_id": library_id,
            "active_release_id": None,
            "active": None,
            "releases": [],
            "history": [],
        }
    )
    return JSONResponse(_jsonable(snapshot))


async def _release_targets(request: Request):
    library_id = request.query_params.get("library_id", "").strip()
    root = Path(request.app.state.root).resolve()
    targets = []
    for config in index_target_configs(root).values():
        try:
            target = IndexTarget.from_dict(config)
        except Exception:
            continue
        normalized_library = normalize_library(target.library)
        if library_id and normalized_library != normalize_library(library_id):
            continue
        targets.append({
            "target_id": target.target_id,
            "library_id": normalized_library,
            "library": target.library,
            "version": target.version,
            "corpus_id": target.corpus_identity(),
            "chunk_count": target.chunk_count,
        })
    libraries = {
        item["library_id"]: {"library_id": item["library_id"], "library": item["library"]}
        for item in targets
    }
    return JSONResponse({"libraries": sorted(libraries.values(), key=lambda item: item["library_id"]), "targets": targets})


async def _release_prepare(request: Request):
    payload = await _payload(request)
    library_id = payload.get("library_id")
    target_id = payload.get("target_id")
    reason = payload.get("reason", "")
    if not isinstance(library_id, str) or not library_id.strip():
        raise HTTPException(422, "library_id不能为空")
    if not isinstance(target_id, str) or not target_id.strip():
        raise HTTPException(422, "target_id不能为空")
    if not isinstance(reason, str) or len(reason) > 1000:
        raise HTTPException(422, "reason必须是长度不超过1000的字符串")
    library_id = normalize_library(library_id.strip())
    target_id = target_id.strip()
    root = Path(request.app.state.root).resolve()
    config = index_target_configs(root).get(target_id)
    if config is None:
        raise HTTPException(404, "候选target不存在或未登记")
    try:
        target = IndexTarget.from_dict(config)
    except Exception as exc:
        raise HTTPException(422, "候选target配置无效") from exc
    if normalize_library(target.library) != library_id:
        raise HTTPException(422, "候选target不属于指定资料库")
    job = _store(request).create(
        library_id=library_id,
        target=target,
        reason=reason,
        actor=_actor(request),
    )
    await _schedule_prepare(request, job["job_id"])
    return JSONResponse({"job": _jsonable(_public_job(job))}, status_code=202)


async def _release_job_status(request: Request):
    job = _store(request).get(request.path_params["job_id"])
    return JSONResponse(_jsonable(_public_job(job)))


async def _release_activate(request: Request):
    payload = await _payload(request)
    release_id = payload.get("release_id")
    expected_revision = payload.get("expected_revision")
    reason = payload.get("reason")
    if not isinstance(release_id, str) or not release_id:
        raise HTTPException(422, "release_id不能为空")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise HTTPException(422, "expected_revision必须是非负整数")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise HTTPException(422, "reason必须是1到1000字符")
    try:
        result = await run_in_threadpool(
            _release_service(request).activate,
            request.path_params["library_id"],
            release_id,
            expected_revision=expected_revision,
            reason=reason,
            actor=_actor(request),
            expected_embedding_model=getattr(
                request.app.state.manager, "embedding_model", None
            ),
        )
    except ReleaseConflictError as exc:
        return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=409)
    except RuntimeError as exc:
        from wenjin.releases.service import ReleaseValidationError

        if not isinstance(exc, ReleaseValidationError):
            raise
        return JSONResponse({"error": exc.code, "detail": "release校验失败，未切换active"}, status_code=409)
    return JSONResponse(_jsonable(result))


async def _release_rollback(request: Request):
    payload = await _payload(request)
    expected_revision = payload.get("expected_revision")
    reason = payload.get("reason")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise HTTPException(422, "expected_revision必须是非负整数")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise HTTPException(422, "reason必须是1到1000字符")
    try:
        result = await run_in_threadpool(
            _release_service(request).rollback,
            request.path_params["library_id"],
            expected_revision=expected_revision,
            reason=reason,
            actor=_actor(request),
            expected_embedding_model=getattr(
                request.app.state.manager, "embedding_model", None
            ),
        )
    except ReleaseConflictError as exc:
        return JSONResponse({"error": exc.code, "detail": str(exc)}, status_code=409)
    except RuntimeError as exc:
        from wenjin.releases.service import ReleaseValidationError

        if not isinstance(exc, ReleaseValidationError):
            raise
        return JSONResponse({"error": exc.code, "detail": "release校验失败，active未变化"}, status_code=409)
    return JSONResponse(_jsonable(result))


async def _release_validate(request: Request):
    from wenjin.releases.service import ReleaseValidationError

    try:
        report = await run_in_threadpool(
            _release_service(request).validate, request.path_params["release_id"]
        )
    except ReleaseValidationError:
        return JSONResponse(
            {"error": "release_validation_failed", "detail": "release校验失败"},
            status_code=409,
        )
    return JSONResponse(_jsonable(report))


def initialize_release_jobs(app) -> None:
    """供管理app lifespan调用：创建任务状态并恢复queued/preparing工作。"""
    app.state.release_job_store = ReleaseJobStore(
        release_catalog_path(app.state.root).with_name("release-jobs.sqlite")
    )
    app.state.running_release_jobs = {}
    app.state.release_job_semaphore = asyncio.Semaphore(1)
    app.state.release_catalog_seen = release_catalog_path(app.state.root).is_file()


async def resume_release_jobs(app) -> None:
    for job in app.state.release_job_store.pending():
        if job["status"] == "preparing":
            app.state.release_job_store.update(
                job["job_id"], status="failed", stage="interrupted_after_restart", progress=5,
                error={
                    "type": "RestartRecovery",
                    "detail": "prepare中断；资源可能已有部分创建但未接管。请确认旧owned资源后新建任务。",
                },
            )
            continue
        await _schedule_prepare_for_app(app, job["job_id"])


async def _schedule_prepare_for_app(app, job_id: str) -> None:
    running = app.state.running_release_jobs
    if job_id not in running or running[job_id].done():
        running[job_id] = asyncio.create_task(_run_prepare(app, job_id))


def release_routes(guard):
    """返回由管理app传入统一鉴权wrapper的release routes。"""
    return [
        Route("/api/admin/release-targets", guard(_release_targets), methods=["GET"]),
        Route("/api/admin/releases/prepare", guard(_release_prepare), methods=["POST"]),
        Route("/api/admin/releases/{release_id:str}/validate", guard(_release_validate), methods=["POST"]),
        Route("/api/admin/libraries/{library_id:str}/releases", guard(_release_list), methods=["GET"]),
        Route("/api/admin/libraries/{library_id:str}/activate", guard(_release_activate), methods=["POST"]),
        Route("/api/admin/libraries/{library_id:str}/rollback", guard(_release_rollback), methods=["POST"]),
        Route("/api/admin/release-jobs/{job_id:str}", guard(_release_job_status), methods=["GET"]),
    ]
