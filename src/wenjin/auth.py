"""MCP HTTP Bearer鉴权；在创建MCP会话前拒绝未授权请求。"""

import hmac
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

ASGIApp = Callable[[dict, Callable[..., Awaitable[dict]], Callable[..., Awaitable[None]]], Awaitable[None]]
TRUSTED_CLIENT_HEADER = b"x-wenjin-authenticated-client"


class BearerAuthMiddleware:
    """校验固定Bearer密钥，并注入不可由客户端伪造的认证客户端ID。"""

    def __init__(self, app: ASGIApp, *, api_key: str, client_id: str) -> None:
        if len(api_key) < 24:
            raise ValueError("MCP API key至少24字符")
        if not client_id.strip() or len(client_id) > 128:
            raise ValueError("MCP client_id不能为空且不能超过128字符")
        self.app = app
        self.api_key = api_key
        self.client_id = client_id.strip().encode("utf-8")
        self.logger = logging.getLogger("wenjin.security")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        if not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [(name.lower(), value) for name, value in scope.get("headers", [])]
        authorization = next(
            (value for name, value in headers if name == b"authorization"), b""
        )
        prefix = b"Bearer "
        supplied = authorization[len(prefix):].decode("utf-8", errors="ignore") \
            if authorization.startswith(prefix) else ""
        if not hmac.compare_digest(supplied, self.api_key):
            client = scope.get("client") or (None, None)
            self.logger.info(json.dumps({
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event": "mcp_auth_denied",
                "service": "wenjin-rag",
                "client_ip": client[0],
                "path": scope.get("path"),
                "status": 401,
            }, ensure_ascii=False, separators=(",", ":")))
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        # 删除客户端传入的同名header，再注入服务端可信值。
        scope["headers"] = [
            (name, value) for name, value in headers
            if name != TRUSTED_CLIENT_HEADER
        ] + [(TRUSTED_CLIENT_HEADER, self.client_id)]
        await self.app(scope, receive, send)
