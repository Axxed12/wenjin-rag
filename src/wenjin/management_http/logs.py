"""管理 HTTP 调用日志的过滤与分页路由。"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from wenjin.management_http.http_common import _query_integer


async def _call_logs(request: Request):
    call_type = request.query_params.get("type", "all")
    status = request.query_params.get("status", "all")
    target = request.query_params.get("target")
    retrieval_target = request.query_params.get("retrieval_target")
    limit = _query_integer(request, "limit", 50)
    offset = _query_integer(request, "offset", 0)
    try:
        result = await run_in_threadpool(
            request.app.state.call_logs.list_calls,
            call_type=call_type,
            status=status,
            target=target,
            retrieval_target=retrieval_target,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return JSONResponse(result)
