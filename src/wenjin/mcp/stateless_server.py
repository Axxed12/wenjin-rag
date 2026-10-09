"""Isolated MCP 2026-07-28 Streamable HTTP entry point.

Run this module with the independent ``mcp-stateless`` dependency environment.
The established 2025-11-25 service remains in :mod:`wenjin.mcp.server`.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import ipaddress
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp.types import LATEST_PROTOCOL_VERSION

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.auth import BearerAuthMiddleware
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.mcp import runtime
from wenjin.mcp.download import register_download_route
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import ROOT, RuntimeIdentity
from wenjin.mcp.stateless_identity import StatelessIdentityStore
from wenjin.mcp.stateless_tools import (
    STATELESS_PROTOCOL_VERSION,
    StatelessRegistrationFacade,
)
from wenjin.mcp.tools import register_tools
from wenjin.retrieval.service import RetrievalService


def _request_body_limit() -> int:
    """Read the v2 SDK limit lazily so legacy-environment --help still works."""
    from mcp.server.transport_security import DEFAULT_MAX_REQUEST_BODY_SIZE

    return DEFAULT_MAX_REQUEST_BODY_SIZE


def _send_error(
    send, *, status: int, code: int, message: str, data: dict | None = None,
    request_id: str | int | None = None,
) -> Any:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    body = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "error": error}, separators=(",", ":")
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if status == 405:
        headers.append((b"allow", b"POST"))

    async def response() -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return response()


class StatelessEndpointMiddleware:
    """Enforce the modern endpoint shape and per-call response cache policy."""

    def __init__(self, app, *, path: str) -> None:
        self.app = app
        self.path = path.rstrip("/") or "/"

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path", "").rstrip("/") != self.path:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method != "POST":
            await _send_error(
                send, status=405, code=-32600,
                message="The stateless MCP endpoint accepts POST requests only",
            )
            return

        body_limit = _request_body_limit()

        content_lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if content_lengths:
            try:
                if len(content_lengths) != 1 or int(content_lengths[0]) > body_limit:
                    await _send_error(
                        send, status=413, code=-32600,
                        message="MCP request body exceeds the 4 MiB limit",
                    )
                    return
            except ValueError:
                await _send_error(
                    send, status=400, code=-32600,
                    message="Invalid Content-Length header",
                )
                return
        body_parts: list[bytes] = []
        body_size = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body_part = message.get("body", b"")
            body_size += len(body_part)
            if body_size > body_limit:
                await _send_error(
                    send, status=413, code=-32600,
                    message="MCP request body exceeds the 4 MiB limit",
                )
                return
            body_parts.append(body_part)
            more_body = bool(message.get("more_body", False))
        body = b"".join(body_parts)
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = None  # Let the SDK return its standard parse error.

        request_id = None
        if isinstance(request, dict):
            candidate_id = request.get("id")
            if isinstance(candidate_id, (str, int)) and not isinstance(candidate_id, bool):
                request_id = candidate_id
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            metadata = params.get("_meta")
            metadata = metadata if isinstance(metadata, dict) else {}
            body_version = metadata.get("io.modelcontextprotocol/protocolVersion")
            if (
                "io.modelcontextprotocol/protocolVersion" in metadata
                and not isinstance(body_version, str)
            ):
                await _send_error(
                    send, status=400, code=-32602,
                    message="Invalid protocolVersion request metadata",
                    request_id=request_id,
                )
                return
            if not isinstance(body_version, str):
                body_version = None
            legacy_initialize = (
                request.get("method") == "initialize"
                and isinstance(params.get("protocolVersion"), str)
            )
            if legacy_initialize:
                body_version = params["protocolVersion"]

        else:
            body_version = None

        headers = [
            value for key, value in scope.get("headers", [])
            if key.lower() == b"mcp-protocol-version"
        ]
        if len(headers) != 1:
            await _send_error(
                send, status=400, code=-32020,
                message="Missing or duplicate MCP-Protocol-Version header",
                request_id=request_id,
            )
            return
        try:
            header_version = headers[0].decode("ascii")
        except UnicodeDecodeError:
            await _send_error(
                send, status=400, code=-32020,
                message="MCP-Protocol-Version header must contain ASCII",
                request_id=request_id,
            )
            return

        if isinstance(body_version, str) and body_version != header_version:
            await _send_error(
                send, status=400, code=-32020,
                message="MCP-Protocol-Version header does not match request metadata",
                request_id=request_id,
            )
            return

        requested_version = body_version or header_version
        if header_version != STATELESS_PROTOCOL_VERSION or requested_version != STATELESS_PROTOCOL_VERSION:
            await _send_error(
                send,
                status=400,
                code=-32022,
                message="Requested MCP protocol version is not supported by this endpoint",
                request_id=request_id,
                data={
                    "supported": [STATELESS_PROTOCOL_VERSION],
                    "requested": requested_version,
                },
            )
            return

        async def replay_receive() -> dict:
            nonlocal body
            if body is not None:
                current, body = body, None
                return {"type": "http.request", "body": current, "more_body": False}
            return await receive()

        async def private_tool_response(message: dict) -> None:
            if (
                isinstance(request, dict)
                and request.get("method") == "tools/call"
                and message.get("type") == "http.response.start"
            ):
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, replay_receive, private_tool_response)


class IgnoreLegacySessionHeadersMiddleware:
    """Ignore 2025 session/resume headers before per-request identity checks."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            scope["headers"] = [
                (key, value)
                for key, value in scope.get("headers", [])
                if key.lower() not in {b"mcp-session-id", b"last-event-id"}
            ]
        await self.app(scope, receive, send)


class PrivateDownloadResponseMiddleware:
    """Prevent shared caches from retaining personal file responses."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "") if scope.get("type") == "http" else ""

        async def private_response(message: dict) -> None:
            if path.startswith("/mcp-stateless/files/") and message.get("type") == "http.response.start":
                message["headers"] = [
                    (key, value) for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, private_response)


def _validate_stateless_runtime() -> None:
    """Fail clearly when launched with the legacy dependency environment."""
    fastmcp_version = importlib.metadata.version("fastmcp")
    sdk_version = importlib.metadata.version("mcp")
    if fastmcp_version.split(".", 1)[0] != "4":
        raise RuntimeError(
            f"MCP stateless entry requires FastMCP 4.x; found {fastmcp_version}"
        )
    if sdk_version.split(".", 1)[0] != "2":
        raise RuntimeError(f"MCP stateless entry requires Python SDK 2.x; found {sdk_version}")
    if LATEST_PROTOCOL_VERSION != STATELESS_PROTOCOL_VERSION:
        raise RuntimeError(
            "MCP stateless entry requires SDK protocol "
            f"{STATELESS_PROTOCOL_VERSION}; found {LATEST_PROTOCOL_VERSION}"
        )
    if not callable(getattr(FastMCP, "http_app", None)):
        raise RuntimeError("FastMCP 4 http_app API is unavailable in this environment")


def build_stateless_mcp_server(
    service: RetrievalService | None = None,
    *,
    audit: SearchAudit | None = None,
    access: AccessRegistry | None = None,
    access_dir: Path | None = None,
    managed_bridge: ManagedFileHTTPClient | None = None,
    raw_root: Path | None = None,
    project_root: Path | None = None,
    bearer_enabled: bool = False,
    identity: RuntimeIdentity | None = None,
    corpus_router: Any | None = None,
    release_resolver: Any | None = None,
    identity_store: Any | None = None,
    settings: Any | None = None,
) -> FastMCP:
    """Build the isolated modern server while reusing the existing RAG handlers."""
    _validate_stateless_runtime()
    root = project_root or ROOT
    if identity_store is not None and managed_bridge is not None \
            and not getattr(managed_bridge, "account_mode", False):
        raise ValueError("账户模式MCP必须使用逐请求个人凭据的受限managed bridge")
    audit = audit or SearchAudit.from_environment(root)
    raw_root_path = raw_root or (root / "raw_data")
    access_dir = access_dir or (root / "configs/access")
    release_holder = {"value": release_resolver}
    managed_bridge_holder = {"value": managed_bridge}

    if identity is None and service is not None:
        identity = runtime.identity_from_registry(root)

    service_search_lock = getattr(service, "search_lock", None)
    if (
        service_search_lock is not None
        and corpus_router is not None
        and hasattr(corpus_router, "search_lock")
    ):
        corpus_router.search_lock = service_search_lock

    if service is None:

        @asynccontextmanager
        async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
            # Disable pydantic-settings' implicit project .env lookup. Process
            # environment is the default source; operators may opt into a file
            # explicitly through --env-file in main_stateless().
            from wenjin.retrieval.search_policy import SearchPolicy
            from wenjin.settings import Settings

            runtime_settings = settings or Settings(_env_file=None)
            retrieval_service, runtime_identity = runtime.create_runtime(
                root=root, settings=runtime_settings
            )
            search_policy_holder["value"] = getattr(
                retrieval_service, "search_policy", SearchPolicy()
            )
            file_bridge = None
            try:
                file_bridge = (
                    ManagedFileHTTPClient.from_environment(account_mode=identity_store is not None)
                    if managed_bridge is None
                    and (identity_store is not None or os.getenv("WENJIN_FILE_ADMIN_TOKEN"))
                    else None
                )
                managed_bridge_holder["value"] = managed_bridge or file_bridge
                router = runtime.create_router(
                    root=root,
                    qdrant_client=retrieval_service.qdrant_client,
                    embedder=retrieval_service.embedder,
                    reranker=retrieval_service.reranker,
                    search_lock=retrieval_service.search_lock,
                )
                resolver = runtime.create_release_resolver(
                    root=root,
                    qdrant_client=retrieval_service.qdrant_client,
                    embedder=retrieval_service.embedder,
                    reranker=retrieval_service.reranker,
                    legacy_router=router,
                    search_lock=retrieval_service.search_lock,
                    expected_embedding_model=runtime_identity.embedding_model,
                )
                release_holder["value"] = resolver
                yield {
                    "retrieval_service": retrieval_service,
                    "identity": runtime_identity,
                    "corpus_router": router,
                    "release_resolver": resolver,
                    "managed_file_bridge": file_bridge,
                    "identity_store": identity_store,
                }
            finally:
                if file_bridge is not None:
                    file_bridge.close()
                retrieval_service.close()
                audit.close()

        server = FastMCP(
            "wenjin-rag-stateless",
            version="0.1.0",
            instructions=(
                "Search evidence from the active OSG index or other indexed libraries with "
                "search_docs(library=...). Use list_corpora to discover those libraries. "
                "Use list_managed_files to discover authorized active uploads, then call "
                "search_docs(file_id=...) to search one uploaded file. Scores are ranking "
                "signals, not confidence."
            ),
            lifespan=lifespan,
            mask_error_details=True,
        )
    else:
        server = FastMCP(
            "wenjin-rag-stateless",
            version="0.1.0",
            instructions=(
                "Search evidence from the active OSG index or other indexed libraries with "
                "search_docs(library=...). Use list_corpora to discover those libraries. "
                "Use list_managed_files to discover authorized active uploads, then call "
                "search_docs(file_id=...) to search one uploaded file. Scores are ranking "
                "signals, not confidence."
            ),
            mask_error_details=True,
        )

    search_policy_holder = {"value": getattr(service, "search_policy", None)}
    registration = StatelessRegistrationFacade(server)
    current_access, sources_for = register_tools(
        registration,
        root=root,
        raw_root_path=raw_root_path,
        audit=audit,
        access=access,
        access_dir=access_dir,
        bearer_enabled=bearer_enabled,
        identity=identity,
        service=service,
        corpus_router=corpus_router,
        release_resolver=lambda: release_holder["value"],
        managed_bridge=managed_bridge,
        identity_store=identity_store,
        search_policy_holder=search_policy_holder,
    )
    register_download_route(
        registration,
        bearer_enabled=bearer_enabled,
        audit=audit,
        current_access=current_access,
        sources_for=sources_for,
        identity_store=identity_store,
        root=root,
        release_resolver=lambda: release_holder["value"],
        managed_bridge=lambda: managed_bridge_holder["value"],
    )
    return server


def create_stateless_app(
    *,
    path: str = "/mcp-stateless",
    server: FastMCP | None = None,
    service: RetrievalService | None = None,
    identity_store=None,
    api_key: str | None = None,
    client_id: str = "default-client",
    allowed_hosts: list[str] | None = None,
    project_root: Path | None = None,
    settings: Any | None = None,
    identity: RuntimeIdentity | None = None,
):
    """Create FastMCP 4's ASGI app with modern-protocol and per-request auth gates."""
    if identity_store is None and not api_key:
        raise ValueError("stateless MCP app requires an IdentityStore or static Bearer credential")
    if path != "/mcp-stateless":
        raise ValueError("stateless download routing is fixed at /mcp-stateless")
    server = server or build_stateless_mcp_server(
        service=service,
        identity=identity,
        identity_store=identity_store,
        project_root=project_root,
        settings=settings,
        bearer_enabled=bool(api_key) and identity_store is None,
    )
    app = server.http_app(
        path=path,
        transport="http",
        stateless_http=True,
        json_response=True,
        host_origin_protection=True,
        allowed_hosts=allowed_hosts,
    )
    app = StatelessEndpointMiddleware(app, path=path)
    app = PrivateDownloadResponseMiddleware(app)
    if identity_store is not None:
        app = McpIdentityMiddleware(app, identity_store=identity_store)
    elif api_key:
        app = BearerAuthMiddleware(app, api_key=api_key, client_id=client_id)
    return IgnoreLegacySessionHeadersMiddleware(app)


def main_stateless() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("WENJIN_MCP_STATELESS_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WENJIN_MCP_STATELESS_PORT", "18768")))
    parser.add_argument("--path", default=os.getenv("WENJIN_MCP_STATELESS_PATH", "/mcp-stateless"))
    parser.add_argument("--project-root", default=os.getenv("WENJIN_PROJECT_ROOT"))
    parser.add_argument("--env-file", default=None,
                        help="可选显式配置文件；默认只读取当前进程环境变量")
    parser.add_argument("--identity-db", default=os.getenv("WENJIN_IDENTITY_DB"))
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise ValueError("port必须在1到65535之间")
    if args.path != "/mcp-stateless":
        raise ValueError("新版MCP入口固定使用 /mcp-stateless")

    api_key = os.getenv("WENJIN_MCP_API_KEY")
    if not args.identity_db and not api_key:
        raise ValueError("无状态MCP必须配置现有身份数据库或WENJIN_MCP_API_KEY")
    identity_store = None
    if args.identity_db:
        db_path = Path(args.identity_db).expanduser().resolve()
        if not db_path.is_file():
            raise FileNotFoundError("指定的身份数据库不存在；服务不会创建或初始化账户库")
        identity_store = StatelessIdentityStore(db_path)

    project_root = Path(args.project_root).expanduser().resolve() if args.project_root else ROOT
    from wenjin.settings import Settings

    settings = Settings(_env_file=Path(args.env_file).expanduser().resolve()) \
        if args.env_file else Settings(_env_file=None)
    configured_hosts = [
        item.strip() for item in os.getenv("WENJIN_MCP_STATELESS_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    allowed_hosts = args.allowed_host or configured_hosts or [
        f"127.0.0.1:{args.port}", f"localhost:{args.port}",
    ]
    app = create_stateless_app(
        path=args.path,
        identity_store=identity_store,
        api_key=api_key,
        client_id=os.getenv("WENJIN_MCP_CLIENT_ID", "default-client"),
        allowed_hosts=allowed_hosts,
        project_root=project_root,
        settings=settings,
    )

    cert_file = os.getenv("WENJIN_TLS_CERT_FILE")
    key_file = os.getenv("WENJIN_TLS_KEY_FILE")
    if bool(cert_file) != bool(key_file):
        raise ValueError("TLS证书和私钥必须同时配置")
    if identity_store is not None or api_key:
        try:
            loopback_host = args.host.lower() == "localhost" or ipaddress.ip_address(
                args.host.strip("[]")
            ).is_loopback
        except ValueError:
            loopback_host = False
        private_network_http = os.getenv("WENJIN_MCP_ALLOW_PRIVATE_HTTP", "").strip().lower() in {
            "1", "true", "yes",
        }
        if not loopback_host and not cert_file and not private_network_http:
            raise ValueError("账户模式MCP非loopback监听必须配置TLS证书；也可由HTTPS代理转发到loopback")

    forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1").strip()
    trusted_forwarders = [item.strip() for item in forwarded_allow_ips.split(",") if item.strip()]
    if not trusted_forwarders or "*" in trusted_forwarders:
        raise ValueError("FORWARDED_ALLOW_IPS must list trusted proxy addresses; wildcard is forbidden")

    import uvicorn

    uvicorn_config: dict[str, Any] = {
        "proxy_headers": True,
        "forwarded_allow_ips": forwarded_allow_ips,
    }
    if cert_file and key_file:
        uvicorn_config.update({"ssl_certfile": cert_file, "ssl_keyfile": key_file})
    uvicorn.run(app, host=args.host, port=args.port, **uvicorn_config)


if __name__ == "__main__":
    main_stateless()
