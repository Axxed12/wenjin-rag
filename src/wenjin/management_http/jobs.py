"""持久管理任务的 HTTP 查询、分页与重试路由。"""

from __future__ import annotations

import asyncio

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from wenjin.management_http.http_common import _jsonable, _manager, _query_integer


async def _run_job(app: Starlette, job_id: str) -> None:
    """后台执行已持久化任务；进程重启时由 resume_pending_jobs 恢复。"""
    try:
        async with app.state.worker_semaphore:
            await run_in_threadpool(app.state.manager.run_job, job_id)
    except Exception:
        # core 会把失败原因和 retryable 状态写入持久 JobRecord；响应端只读该记录。
        pass
    finally:
        app.state.running_jobs.pop(job_id, None)


async def _schedule_job(app: Starlette, job_id: str) -> None:
    async with app.state.job_lock:
        existing = app.state.running_jobs.get(job_id)
        if existing is None or existing.done():
            app.state.running_jobs[job_id] = asyncio.create_task(_run_job(app, job_id))

async def _job_status(request: Request):
    manager = await _manager(request)
    job = await run_in_threadpool(manager.get_job, request.path_params["job_id"])
    return JSONResponse(_jsonable(job))


async def _job_list(request: Request):
    manager = await _manager(request)
    limit = _query_integer(request, "limit", 100)
    offset = _query_integer(request, "offset", 0)
    if not 1 <= limit <= 200:
        raise HTTPException(422, "limit必须是1到200之间的整数")
    jobs = await run_in_threadpool(manager.list_jobs, limit=limit, offset=offset)
    return JSONResponse({"jobs": [_jsonable(item) for item in jobs], "limit": limit, "offset": offset})


async def _job_retry(request: Request):
    manager = await _manager(request)
    job = await run_in_threadpool(manager.submit_retry, request.path_params["job_id"])
    await _schedule_job(request.app, job.job_id)
    return JSONResponse(_jsonable(job), status_code=202)


async def _job_cancel(request: Request):
    manager = await _manager(request)
    job = await run_in_threadpool(manager.request_cancel, request.path_params["job_id"])
    return JSONResponse(_jsonable(job), status_code=202 if job.status == "running" else 200)
