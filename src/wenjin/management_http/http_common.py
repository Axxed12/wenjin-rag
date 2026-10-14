"""HTTP 路由共享的认证、错误与轻量运行时原语。"""

from __future__ import annotations

import hmac
import ipaddress
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from wenjin.identity.policy import (
    AuthenticationFailed,
    Conflict,
    CurrentPasswordIncorrect,
    Forbidden,
    NotFound,
)
from wenjin.models.providers import ModelProviderError, model_provider_error_detail
from wenjin.retrieval.errors import StaleSnapshotError

DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return value.name
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ModelProviderError):
        return JSONResponse(
            {
                "error": exc.code,
                "detail": model_provider_error_detail(exc.code),
                "evidence_status": "not_retrieved",
            },
            status_code=503,
        )
    if isinstance(exc, CurrentPasswordIncorrect):
        return JSONResponse(
            {"error": "current_password_invalid", "detail": "当前密码不正确，请重新输入。"},
            status_code=422,
        )
    if isinstance(exc, AuthenticationFailed):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if isinstance(exc, Forbidden):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if isinstance(exc, NotFound):
        return JSONResponse({"error": "not_found"}, status_code=404)
    if isinstance(exc, Conflict):
        return JSONResponse({"error": "conflict", "detail": str(exc)}, status_code=409)
    if isinstance(exc, StaleSnapshotError):
        return JSONResponse(
            {
                "error": "stale_snapshot",
                "detail": "资料库或索引版本已变化，请刷新资料库清单后重试",
            },
            status_code=409,
        )
    if isinstance(exc, KeyError):
        return JSONResponse({"error": "not_found"}, status_code=404)
    if isinstance(exc, ValueError):
        return JSONResponse(
            {"error": "conflict_or_invalid_state", "detail": str(exc)}, status_code=409
        )
    if isinstance(exc, (OSError, RuntimeError)):
        return JSONResponse({"error": "operation_failed", "detail": str(exc)}, status_code=503)
    return JSONResponse({"error": "internal_error"}, status_code=500)


async def _manager(request: Request):
    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        raise HTTPException(503, "文件管理服务尚未就绪")
    return manager


async def _authorize(request: Request, access: str = "admin") -> None:
    identity = getattr(request.app.state, "identity_store", None)
    if access in {"public", "bootstrap"}:
        return
    if access == "mcp":
        if identity is None:
            # 无账户模式保持既有 MCP 到管理服务桥接合同。
            expected = request.app.state.admin_token
            if expected is None:
                return
            supplied = request.headers.get("authorization", "")
            candidate = supplied.removeprefix("Bearer ") if supplied.startswith("Bearer ") else ""
            if not candidate or not hmac.compare_digest(candidate, expected):
                raise HTTPException(401, "需要有效的管理 Bearer token")
            request.state.principal = None
            return
        supplied = request.headers.get("authorization", "")
        candidate = supplied.removeprefix("Bearer ") if supplied.startswith("Bearer ") else ""
        try:
            request.state.principal = await run_in_threadpool(
                identity.resolve_mcp_credential, candidate
            )
        except Exception as exc:
            from wenjin.identity.policy import AuthenticationFailed

            if isinstance(exc, AuthenticationFailed):
                raise HTTPException(401, "需要有效的个人MCP凭据") from exc
            raise
        return
    if identity is not None:
        from wenjin.management_http.identity_http import CSRF_HEADER, SESSION_COOKIE

        token = request.cookies.get(SESSION_COOKIE, "")
        if not token:
            raise HTTPException(401, "需要登录")
        try:
            principal = await run_in_threadpool(identity.resolve_session, token)
        except Exception as exc:
            from wenjin.identity.policy import AuthenticationFailed

            if isinstance(exc, AuthenticationFailed):
                raise HTTPException(401, "会话不可用或已过期") from exc
            raise
        request.state.principal = principal
        allowed_during_password_change = {"me", "logout", "change_password", "csrf_refresh"}
        if principal.must_change_password and access not in allowed_during_password_change:
            raise HTTPException(403, "首次登录必须先修改密码")
        if access == "admin" and principal.role != "admin":
            raise HTTPException(403, "仅管理员可访问")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and access not in {
            "me",
            "csrf_refresh",
        }:
            origin = request.headers.get("origin")
            if not origin:
                raise HTTPException(403, "状态变更请求必须包含同源Origin")
            from urllib.parse import urlsplit

            try:
                parsed = urlsplit(origin)
            except ValueError as exc:
                raise HTTPException(403, "Origin无效") from exc
            if (
                parsed.scheme != request.url.scheme
                or parsed.netloc.lower() != request.headers.get("host", "").lower()
            ):
                raise HTTPException(403, "跨源状态变更已拒绝")
            csrf = request.headers.get(CSRF_HEADER, "")
            if not csrf or not identity.verify_csrf(token, csrf):
                raise HTTPException(403, "CSRF校验失败")
        return
    expected = request.app.state.admin_token
    if expected is None:
        return
    supplied = request.headers.get("authorization", "")
    candidate = supplied.removeprefix("Bearer ") if supplied.startswith("Bearer ") else ""
    if not hmac.compare_digest(candidate, expected):
        raise HTTPException(
            401, "需要有效的管理 Bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


async def _guarded(request: Request, handler, access: str = "admin"):
    try:
        await _authorize(request, access)
        return await handler(request)
    except HTTPException as exc:
        return JSONResponse(
            {"error": "request_rejected", "detail": exc.detail},
            status_code=exc.status_code,
            headers=exc.headers,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射core异常，避免路径/traceback外泄
        return _error_response(exc)


def _set_retrieval_log_target(request: Request, *, target_type: str, value: str | None) -> None:
    """只把已校验的稳定ID交给调用日志；不设置查询正文或原文路径。"""
    if not value or len(value) > 160 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        request.state.call_target = "unknown"
        return
    request.state.call_target = f"{target_type}:{value}"


def _query_integer(request: Request, name: str, default: int) -> int:
    values = request.query_params.getlist(name)
    if not values:
        return default
    if len(values) != 1 or len(values[0]) > 19 or not re.fullmatch(r"[0-9]+", values[0]):
        raise HTTPException(422, f"{name}必须是单个非负整数")
    value = int(values[0])
    if value > 9_223_372_036_854_775_807:
        raise HTTPException(422, f"{name}超出支持范围")
    return value


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_listen_security(host: str, token: str | None) -> None:
    """非loopback监听必须使用管理token，避免误把本机管理API暴露到网络。"""
    if not _is_loopback(host) and not token:
        raise ValueError("非loopback监听必须设置 WENJIN_FILE_ADMIN_TOKEN")
