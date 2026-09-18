"""问津RAG的状态化MCP服务。

协议按2025-11-25生命周期运行：客户端先initialize并发送initialized通知，HTTP会话可使用
MCP-Session-Id；启动时显式禁止stateless HTTP。
"""

import argparse
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.middleware import Middleware

from wenjin.audit import SearchAudit
from wenjin.auth import BearerAuthMiddleware
from wenjin.index_registry import IndexRegistryStore, resolve_project_path, validate_target
from wenjin.retrieval.service import RetrievalService, SearchConfig, load_source_urls

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PROTOCOL_VERSION = "2025-11-25"


def create_default_service(*, use_reranker: bool = True) -> RetrievalService:
    """加载Qdrant客户端和两个本地0.6B模型；供常驻MCP进程复用。"""
    import torch
    from qdrant_client import QdrantClient

    from wenjin.retrieval.rerank import Qwen3Reranker
    from wenjin.settings import Settings

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    settings = Settings()
    target = IndexRegistryStore(ROOT / "data/indexes/index-registry.json").active()
    actual_embedding_model = os.getenv(
        "EMBEDDING_MODEL_ID", settings.embedding_model_path.name
    )
    if actual_embedding_model != target.embedding_model:
        raise RuntimeError(
            f"当前Embedding模型{actual_embedding_model}与索引"
            f"{target.embedding_model}不一致"
        )
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    reranker = None
    try:
        validate_target(target, root=ROOT, qdrant_client=client)
        reranker = (
            Qwen3Reranker(
                settings.reranker_model_path,
                device="cuda" if torch.cuda.is_available() else "cpu",
                max_length=1024,
                batch_size=2,
            )
            if use_reranker else None
        )
        return RetrievalService(
            qdrant_client=client,
            embedder=settings.embedding(),
            reranker=reranker,
            bm25_path=resolve_project_path(ROOT, target.bm25_path),
            source_urls=load_source_urls(resolve_project_path(ROOT, target.manifest_path)),
            collection_name=target.dense_collection,
            supported_version=target.version,
            index_target_id=target.target_id,
        )
    except Exception:
        if reranker is not None:
            reranker.close()
        client.close()
        raise


def build_mcp_server(
    service: RetrievalService | None = None, *, audit: SearchAudit | None = None
) -> FastMCP:
    """创建只有一个只读检索工具的MCP服务；测试可注入轻量service。"""
    if LATEST_PROTOCOL_VERSION != EXPECTED_PROTOCOL_VERSION:
        raise RuntimeError(
            f"MCP SDK协议版本为{LATEST_PROTOCOL_VERSION}，项目要求{EXPECTED_PROTOCOL_VERSION}"
        )

    # 不依赖HTTP访问日志：进程内和网络MCP调用走同一审计入口。
    audit = audit or SearchAudit.from_environment(ROOT)

    if service is None:

        @asynccontextmanager
        async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
            runtime = create_default_service()
            try:
                yield {"retrieval_service": runtime}
            finally:
                runtime.close()
                audit.close()

        mcp = FastMCP(
            "wenjin-rag",
            version="0.1.0",
            instructions="Search verified OSG 3.6.5 evidence. Returned scores are ranking signals.",
            lifespan=lifespan,
            mask_error_details=True,
        )
    else:
        mcp = FastMCP(
            "wenjin-rag",
            version="0.1.0",
            instructions="Search verified OSG 3.6.5 evidence. Returned scores are ranking signals.",
            mask_error_details=True,
        )

    @mcp.tool(
        name="search_docs",
        description=(
            "Search OSG 3.6.5 source documentation and return ranked evidence with file paths, "
            "line ranges, upstream URLs, and raw text. This tool does not generate an answer."
        ),
        run_in_thread=True,
    )
    def search_docs(
        query: str,
        top_k: int = 5,
        context_char_budget: int = 8000,
        version: str = "3.6.5",
        ctx: Context | None = None,
    ) -> dict:
        """检索证据；ctx只用于取得服务生命周期对象，不充当每请求协议身份。"""
        runtime = service
        if runtime is None:
            if ctx is None:
                raise RuntimeError("MCP Context不可用")
            runtime = ctx.lifespan_context["retrieval_service"]
        started = time.perf_counter()
        try:
            session_id = ctx.session_id if ctx is not None else None
        except RuntimeError:
            session_id = None
        client_id = get_http_headers(include={"x-wenjin-authenticated-client"}).get(
            "x-wenjin-authenticated-client"
        )
        try:
            result = runtime.search(
                query,
                config=SearchConfig(
                    version=version,
                    top_k=top_k,
                    context_char_budget=context_char_budget,
                ),
            )
        except Exception as exc:
            audit.record(
                query=query, version=version, top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id,
                elapsed_seconds=time.perf_counter() - started, error=exc,
            )
            raise
        request_id = audit.record(
            query=query, version=version, top_k=top_k,
            context_char_budget=context_char_budget, session_id=session_id,
            client_id=client_id,
            elapsed_seconds=time.perf_counter() - started, result=result,
        )
        result["request_id"] = request_id
        result["mcp_protocol"] = EXPECTED_PROTOCOL_VERSION
        return result

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("WENJIN_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WENJIN_MCP_PORT", "8765")))
    parser.add_argument("--path", default=os.getenv("WENJIN_MCP_PATH", "/mcp"))
    parser.add_argument("--allowed-host", action="append", default=[])
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
    server = build_mcp_server()
    api_key = os.getenv("WENJIN_MCP_API_KEY")
    client_id = os.getenv("WENJIN_MCP_CLIENT_ID", "default-client")
    http_middleware = (
        [Middleware(BearerAuthMiddleware, api_key=api_key, client_id=client_id)]
        if api_key else []
    )
    cert_file = os.getenv("WENJIN_TLS_CERT_FILE")
    key_file = os.getenv("WENJIN_TLS_KEY_FILE")
    if bool(cert_file) != bool(key_file):
        raise ValueError("TLS证书和私钥必须同时配置")
    uvicorn_config = (
        {"ssl_certfile": cert_file, "ssl_keyfile": key_file}
        if cert_file and key_file else None
    )
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
        pass  # Uvicorn已执行应用关闭；不再打印与故障无关的Ctrl+C栈。


if __name__ == "__main__":
    main()
