"""MCP连接入口管理API。"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wenjin.management_http.identity_http import _payload
from wenjin.mcp_control.management import ERROR_DETAILS, MCPControlError


def _error_response(exc: MCPControlError):
    return JSONResponse(
        {"error": exc.code, "detail": ERROR_DETAILS.get(exc.code, "MCP入口管理请求失败。")},
        status_code=exc.status,
    )


async def _service(request: Request):
    service = getattr(request.app.state, "mcp_control_service", None)
    if service is None:
        raise MCPControlError("mcp_control_unavailable", 503)
    return service


async def entries(request: Request):
    try:
        return JSONResponse(await run_in_threadpool((await _service(request)).entries))
    except MCPControlError as exc:
        return _error_response(exc)


async def connection_config(request: Request):
    try:
        values = request.query_params.getlist("entry_id")
        if len(values) > 1:
            raise MCPControlError("mcp_request_invalid", 422)
        entry_id = values[0] if values else None
        return JSONResponse(await run_in_threadpool((await _service(request)).connection_config, entry_id))
    except MCPControlError as exc:
        return _error_response(exc)


async def set_default(request: Request):
    try:
        payload = await _payload(request, max_bytes=8192)
        if set(payload) != {"entry_id", "expected_revision"} or not isinstance(payload.get("entry_id"), str):
            raise MCPControlError("mcp_request_invalid", 422)
        result = await run_in_threadpool(
            (await _service(request)).set_default,
            payload["entry_id"], payload["expected_revision"],
        )
        return JSONResponse(result)
    except MCPControlError as exc:
        return _error_response(exc)


async def action(request: Request):
    try:
        payload = await _payload(request, max_bytes=8192)
        if set(payload) - {"action", "expected_generation"} or not isinstance(payload.get("action"), str):
            raise MCPControlError("mcp_request_invalid", 422)
        result = await run_in_threadpool(
            (await _service(request)).action,
            request.path_params["entry_id"], payload,
        )
        return JSONResponse(result, status_code=202)
    except MCPControlError as exc:
        return _error_response(exc)


async def operation(request: Request):
    try:
        result = await run_in_threadpool(
            (await _service(request)).operation,
            request.path_params["operation_id"],
        )
        return JSONResponse(result)
    except MCPControlError as exc:
        return _error_response(exc)


def routes(route_factory):
    return [
        Route("/api/admin/mcp/entries", route_factory(entries, access="admin_session"), methods=["GET"]),
        Route("/api/admin/mcp/connection-config", route_factory(connection_config, access="admin_session"), methods=["GET"]),
        Route("/api/admin/mcp/default-entry", route_factory(set_default, access="admin_session"), methods=["PUT"]),
        Route("/api/admin/mcp/entries/{entry_id:str}/actions", route_factory(action, access="admin_session"), methods=["POST"]),
        Route("/api/admin/mcp/operations/{operation_id:str}", route_factory(operation, access="admin_session"), methods=["GET"]),
    ]
