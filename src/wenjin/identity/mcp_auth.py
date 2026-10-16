"""面向 MCP Streamable HTTP 的逐请求身份与受管桥接凭据辅助。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from wenjin.identity.policy import AuthenticationFailed, IdentityError, Principal


@dataclass(frozen=True)
class RequestIdentity:
    """单个 HTTP 请求的已验证身份；bearer仅供服务端桥接，不应输出或持久化。"""

    principal: Principal
    bearer: str = field(repr=False)
    access_snapshot: Any | None = field(default=None, repr=False)


class McpIdentityMiddleware:
    """给已完成MCP initialize/initialized生命周期的每个HTTP请求注入当前身份。

    MCP-Session-Id只用于阻止不同账号接管同一协议会话；绝不据此复用Principal
    或ACL。POST、GET SSE及DELETE都会重新检查个人MCP bearer。
    """

    def __init__(self, app, *, identity_store) -> None:
        self.app = app
        self.identity_store = identity_store
        self._session_users: dict[str, tuple[str, float]] = {}
        self._session_lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        authorization_values = [
            value for key, value in scope.get("headers", []) if key.lower() == b"authorization"
        ]
        if len(authorization_values) != 1:
            await self._unauthorized(send)
            return
        authorization = authorization_values[0]
        prefix = b"Bearer "
        if not authorization.startswith(prefix):
            await self._unauthorized(send)
            return
        try:
            bearer = authorization[len(prefix):].decode("ascii")
            remote_authenticate = getattr(self.identity_store, "authenticate_request", None)
            if callable(remote_authenticate):
                principal, access_snapshot = await asyncio.to_thread(remote_authenticate, bearer)
            else:
                principal = await asyncio.to_thread(
                    self.identity_store.resolve_mcp_credential, bearer
                )
                access_snapshot = None
        except (UnicodeDecodeError, IdentityError, ValueError):
            await self._unauthorized(send)
            return
        except Exception as exc:
            from wenjin.mcp.remote_identity import RemoteIdentityUnavailable

            if not isinstance(exc, RemoteIdentityUnavailable):
                raise
            await self._unavailable(send)
            return
        session_values = [
            value for key, value in scope.get("headers", [])
            if key.lower() == b"mcp-session-id"
        ]
        if len(session_values) > 1:
            await self._unauthorized(send)
            return
        try:
            session_id = session_values[0].decode("ascii") if session_values else ""
        except UnicodeDecodeError:
            await self._unauthorized(send)
            return
        with self._session_lock:
            now = time.monotonic()
            expired = [key for key, (_user, seen) in self._session_users.items()
                       if now - seen > 86_400]
            for key in expired:
                self._session_users.pop(key, None)
        if session_id:
            with self._session_lock:
                now = time.monotonic()
                binding = self._session_users.get(session_id)
                bound_user = binding[0] if binding is not None else None
                if binding is not None and bound_user == principal.user_id:
                    self._session_users[session_id] = (principal.user_id, now)
            # Unknown/expired IDs must re-initialize without a session header; never
            # let a client self-assert an old session and acquire a fresh binding.
            if bound_user != principal.user_id:
                await self._unauthorized(send)
                return

        # 删掉所有客户端可伪造的旧可信身份头；account mode只用state里的Principal。
        scope["headers"] = [
            (key, value) for key, value in scope.get("headers", [])
            if not key.lower().startswith(b"x-wenjin-") and key.lower() != b"authorization"
        ] + [(b"authorization", authorization)]
        state = scope.setdefault("state", {})
        state["wenjin_identity"] = RequestIdentity(
            principal=principal, bearer=bearer, access_snapshot=access_snapshot
        )
        state["principal"] = principal

        async def capture_session(response_message) -> None:
            if response_message.get("type") != "http.response.start":
                await send(response_message)
                return
            response_session = next(
                (value.decode("ascii", errors="ignore") for key, value in response_message.get("headers", [])
                 if key.lower() == b"mcp-session-id"),
                "",
            )
            status = int(response_message.get("status", 500))
            if response_session and 200 <= status < 300:
                with self._session_lock:
                    binding = self._session_users.get(response_session)
                    other_user = binding[0] if binding is not None else None
                    if other_user is None or other_user == principal.user_id:
                        self._session_users[response_session] = (principal.user_id, time.monotonic())
            if session_id and scope.get("method") == "DELETE" and 200 <= status < 300:
                with self._session_lock:
                    binding = self._session_users.get(session_id)
                    if binding is not None and binding[0] == principal.user_id:
                        self._session_users.pop(session_id, None)
            await send(response_message)

        await self.app(scope, receive, capture_session)

    @staticmethod
    async def _unauthorized(send) -> None:
        body = json.dumps({"error": "unauthorized"}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _unavailable(send) -> None:
        body = json.dumps({"error": "identity_authority_unavailable"}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def identity_from_scope(scope: dict[str, Any]) -> RequestIdentity:
    """工具层从可信ASGI request scope取身份；不读工具JSON中的user_id。"""
    identity = scope.get("state", {}).get("wenjin_identity")
    if not isinstance(identity, RequestIdentity):
        raise AuthenticationFailed("当前MCP请求没有已验证的个人身份")
    return identity


def identity_from_context(ctx: Any) -> RequestIdentity:
    """FastMCP工具中的ASGI身份入口，依赖当前HTTP请求scope而不依赖session state。"""
    from fastmcp.server.dependencies import get_http_request

    del ctx  # Context不是身份来源；只用于表明此helper从工具调用点使用。
    return identity_from_scope(get_http_request().scope)


def managed_bridge_headers(scope: dict[str, Any]) -> dict[str, str]:
    """为当前工具HTTP调用生成purpose-limited bridge Authorization头。

    需在每次工具调用时传入当前request scope调用，不能把返回头缓存到
    ManagedFileHTTPClient实例，否则不同用户共享MCP进程时会串用身份。
    """
    identity = identity_from_scope(scope)
    return {"Authorization": f"Bearer {identity.bearer}"}
