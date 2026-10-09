"""MCP Streamable HTTP 服务装配与进程入口。"""

from __future__ import annotations

import argparse
import ipaddress
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.middleware import Middleware

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.auth import BearerAuthMiddleware
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.mcp import runtime
from wenjin.mcp.download import (
    register_download_route,
)
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import EXPECTED_PROTOCOL_VERSION, ROOT, RuntimeIdentity
from wenjin.mcp.tools import register_tools
from wenjin.retrieval.service import RetrievalService


def build_mcp_server(
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
) -> FastMCP:
    """创建只读 MCP 服务，并组合工具、runtime 和下载路由。"""
    if LATEST_PROTOCOL_VERSION != EXPECTED_PROTOCOL_VERSION:
        raise RuntimeError(
            f"MCP SDK协议版本为{LATEST_PROTOCOL_VERSION}，项目要求{EXPECTED_PROTOCOL_VERSION}"
        )

    root = project_root or ROOT
    if identity_store is not None and managed_bridge is not None \
            and not getattr(managed_bridge, "account_mode", False):
        raise ValueError("账户模式MCP必须使用逐请求个人凭据的受限managed bridge")
    audit = audit or SearchAudit.from_environment(root)
    raw_root_path = raw_root or (root / "raw_data")
    access_dir = access_dir or (root / "configs/access")
    release_holder = {"value": release_resolver}
    managed_bridge_holder = {"value": managed_bridge}

    # 注入 service 的测试路径最多补读一次身份；生产路径从同一次 runtime 构建取得。
    if identity is None and service is not None:
        identity = runtime.identity_from_registry(root)

    # 主 service 与多库路由复用同一模型锁，避免模型并发调用绕过原互斥域。
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
            retrieval_service, runtime_identity = runtime.create_runtime(root=root)
            from wenjin.retrieval.search_policy import SearchPolicy

            search_policy_holder["value"] = getattr(
                retrieval_service, "search_policy", SearchPolicy()
            )
            file_bridge = None
            resolver = None
            try:
                file_bridge = (
                    ManagedFileHTTPClient.from_environment(account_mode=identity_store is not None)
                    if managed_bridge is None
                    and (identity_store is not None or os.getenv("WENJIN_FILE_ADMIN_TOKEN"))
                    else None
                )
                managed_bridge_holder["value"] = managed_bridge or file_bridge
                # 多库路由共享 runtime 中已经加载的客户端、向量器和重排器。
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

        mcp = FastMCP(
            "wenjin-rag",
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
        mcp = FastMCP(
            "wenjin-rag",
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

    search_policy_holder = {
        "value": getattr(service, "search_policy", None)
    }
    current_access, sources_for = register_tools(
        mcp,
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
        mcp,
        bearer_enabled=bearer_enabled,
        audit=audit,
        current_access=current_access,
        sources_for=sources_for,
        identity_store=identity_store,
        root=root,
        release_resolver=lambda: release_holder["value"],
        managed_bridge=lambda: managed_bridge_holder["value"],
    )
    return mcp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("WENJIN_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WENJIN_MCP_PORT", "8765")))
    parser.add_argument("--path", default=os.getenv("WENJIN_MCP_PATH", "/mcp"))
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--identity-db", default=os.getenv("WENJIN_IDENTITY_DB"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        raise ValueError("port必须在1到65535之间")
    if not args.path.startswith("/"):
        raise ValueError("MCP path必须以/开头")
    configured_hosts = [
        item.strip() for item in os.getenv("WENJIN_MCP_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    allowed_hosts = args.allowed_host or configured_hosts or [
        f"127.0.0.1:{args.port}",
        f"localhost:{args.port}",
    ]
    api_key = os.getenv("WENJIN_MCP_API_KEY")
    identity_store = None
    if args.identity_db:
        from wenjin.identity.store import IdentityStore

        identity_store = IdentityStore(Path(args.identity_db).expanduser().resolve())
        identity_store.initialize()
    server = build_mcp_server(
        bearer_enabled=bool(api_key) and identity_store is None,
        identity_store=identity_store,
    )
    client_id = os.getenv("WENJIN_MCP_CLIENT_ID", "default-client")
    if identity_store is not None:
        http_middleware = [Middleware(McpIdentityMiddleware, identity_store=identity_store)]
    else:
        http_middleware = (
            [Middleware(BearerAuthMiddleware, api_key=api_key, client_id=client_id)]
            if api_key else []
        )
    cert_file = os.getenv("WENJIN_TLS_CERT_FILE")
    key_file = os.getenv("WENJIN_TLS_KEY_FILE")
    if bool(cert_file) != bool(key_file):
        raise ValueError("TLS证书和私钥必须同时配置")
    if identity_store is not None:
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
    # Cloud TLS terminates at the private Caddy container. Trust only the explicit
    # proxy IP supplied by the deployment; never accept arbitrary forwarded headers.
    forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "127.0.0.1").strip()
    trusted_forwarders = [item.strip() for item in forwarded_allow_ips.split(",") if item.strip()]
    if not trusted_forwarders or "*" in trusted_forwarders:
        raise ValueError("FORWARDED_ALLOW_IPS must list trusted proxy addresses; wildcard is forbidden")
    uvicorn_config = {
        "proxy_headers": True,
        "forwarded_allow_ips": forwarded_allow_ips,
    }
    if cert_file and key_file:
        uvicorn_config.update({"ssl_certfile": cert_file, "ssl_keyfile": key_file})
    try:
        server.run(
            transport="streamable-http",
            show_banner=False,
            host=args.host,
            port=args.port,
            path=args.path,
            stateless_http=False,
            json_response=False,
            host_origin_protection=True,
            allowed_hosts=allowed_hosts,
            middleware=http_middleware,
            uvicorn_config=uvicorn_config,
        )
    except KeyboardInterrupt:
        pass
