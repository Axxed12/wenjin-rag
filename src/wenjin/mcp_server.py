"""问津RAG的状态化MCP服务。

协议按2025-11-25生命周期运行：客户端先initialize并发送initialized通知，HTTP会话可使用
MCP-Session-Id；启动时显式禁止stateless HTTP。
"""

import argparse
import json
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
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from wenjin.access import AccessDenied, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.auth import TRUSTED_CLIENT_HEADER_NAME, BearerAuthMiddleware
from wenjin.catalog import corpus_catalog
from wenjin.index_registry import IndexRegistryStore, resolve_project_path, validate_target
from wenjin.retrieval.service import RetrievalService, SearchConfig, load_source_urls
from wenjin.serving import DOWNLOAD_PREFIX, SourceLibrary, SourceNotFound, attach_download_paths

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


def client_identity(*, bearer_enabled: bool) -> str | None:
    """取得已认证的客户端ID。

    `x-wenjin-authenticated-client` 这个头**只有装了Bearer中间件时才可信**——中间件会先剥掉
    客户端自带的同名头再注入服务端值。没装中间件（例如没配 WENJIN_MCP_API_KEY）时，
    这个头完全由客户端控制，采信它等于允许任意伪造身份访问内部语料，因此一律按匿名处理。
    匿名的结果是内部语料被拒绝（它们要求具体client_id），公开语料仍可读。
    """
    if not bearer_enabled:
        return None
    # get_http_headers 要的是str，不是ASGI scope用的bytes。
    return get_http_headers(include={TRUSTED_CLIENT_HEADER_NAME}).get(
        TRUSTED_CLIENT_HEADER_NAME
    )


def active_corpus_id(root: Path | None = None) -> str | None:
    """active索引对应哪个语料；从manifest路径的目录名取，和语料目录命名一致。"""
    registry_path = (root or ROOT) / "data/indexes/index-registry.json"
    if not registry_path.is_file():
        return None
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        manifest = Path(data["active"]["manifest_path"])
    except (KeyError, json.JSONDecodeError, OSError):
        return None
    return manifest.parent.name


def build_mcp_server(
    service: RetrievalService | None = None,
    *,
    audit: SearchAudit | None = None,
    access: AccessRegistry | None = None,
    raw_root: Path | None = None,
    project_root: Path | None = None,
    bearer_enabled: bool = False,
) -> FastMCP:
    """创建只读的MCP服务（检索、语料目录、原文取件）。

    根目录、审计和访问策略都可注入：测试用临时目录，部署用项目目录，模块里不写死。
    """
    if LATEST_PROTOCOL_VERSION != EXPECTED_PROTOCOL_VERSION:
        raise RuntimeError(
            f"MCP SDK协议版本为{LATEST_PROTOCOL_VERSION}，项目要求{EXPECTED_PROTOCOL_VERSION}"
        )

    root = project_root or ROOT

    def resolved_corpus_id(ctx: Context | None) -> str | None:
        """检索服务启动时确定的语料身份。

        正常部署走lifespan（与索引同一次读注册表，不会漂移）；测试注入service时没有
        lifespan，退回按需读取，仅用于测试。
        """
        if service is not None:
            return active_corpus_id(root)
        if ctx is None:
            return None
        return ctx.lifespan_context.get("corpus_id")

    # 不依赖HTTP访问日志：进程内和网络MCP调用走同一审计入口。
    audit = audit or SearchAudit.from_environment(root)
    # 访问策略默认从部署目录加载；没有策略文件的语料一律拒绝。
    access = access if access is not None else AccessRegistry.from_directory(root / "configs/access")
    source_library = SourceLibrary(raw_root or (root / "raw_data"))

    if service is None:

        @asynccontextmanager
        async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
            runtime = create_default_service()
            # 语料身份在启动时和检索服务一起确定，请求期间不再重读注册表：
            # 运行中改了注册表的话，重读会让"检查的语料"和"实际检索的索引"漂移。
            try:
                yield {"retrieval_service": runtime, "corpus_id": active_corpus_id(root)}
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
        client_id = client_identity(bearer_enabled=bearer_enabled)
        # 检索路径同样过访问策略：只锁下载是没用的，能拿到chunk正文就等于看到了内容。
        # 被拒绝时返回结构化的forbidden结果而不是抛异常：服务端开了错误屏蔽，
        # 抛异常会让客户端只看到"调用失败"，分不清是被拒还是服务坏了。
        corpus_id = resolved_corpus_id(ctx)
        try:
            # 必须fail-closed：registry读不出来时corpus_id为None，此时不能"跳过检查继续检索"，
            # 那等于把"默认拒绝"变成"默认放行"。确定不了目标语料就拒绝。
            if not corpus_id:
                raise AccessDenied("无法从索引注册表确定目标语料，拒绝检索")
            access.require(corpus_id, client_id)
        except AccessDenied as exc:
            audit.record(
                query=query, version=version, top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id,
                elapsed_seconds=time.perf_counter() - started, error=exc,
            )
            return {
                "query": query,
                "error": "forbidden",
                "detail": str(exc),
                "corpus_id": corpus_id,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
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
        # 补上取件路径，让调用方能直接下载到原文；只拼接，鉴权在端点里做。
        result["evidence"] = attach_download_paths(result["evidence"], corpus_id)
        # 纵深防御：逐条证据再按它自己的语料核一次权限，丢弃无权读取的片段。
        # 现在search_docs只服务单库，这一步是冗余的；但等跨库路由接进来，
        # 证据会来自多个语料，没有这层过滤就会出现某个库的内容跟着另一个库的授权漏出去。
        allowed = []
        for item in result["evidence"]:
            item_corpus = item.get("corpus_id") or corpus_id
            if access.check(item_corpus, client_id):
                allowed.append(item)
        dropped = len(result["evidence"]) - len(allowed)
        result["evidence"] = allowed
        result["evidence_dropped_by_access"] = dropped
        result["request_id"] = request_id
        result["mcp_protocol"] = EXPECTED_PROTOCOL_VERSION
        return result

    @mcp.tool(
        name="list_corpora",
        description=(
            "List the corpora available in the knowledge base: library, version, license, "
            "file counts and whether an index exists. Only corpora this client may read are "
            "returned. Use this before searching to learn what is indexed."
        ),
        run_in_thread=True,
    )
    def list_corpora(
        include_files: bool = False,
        ctx: Context | None = None,
    ) -> dict:
        """返回调用方有权查看的语料清单；无权限的语料不会出现，也不暴露其存在。"""
        client_id = client_identity(bearer_enabled=bearer_enabled)
        entries = [
            entry.to_dict(include_files=include_files)
            for entry in corpus_catalog(root)
            if access.check(entry.corpus_id, client_id)
        ]
        return {
            "corpora": entries,
            "count": len(entries),
            "note": (
                "Only corpora this client is allowed to read are listed. "
                "Retrieval scores are ranking signals, not answer confidence."
            ),
            "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
        }

    @mcp.custom_route(f"{DOWNLOAD_PREFIX}/{{corpus_id}}/{{file_path:path}}", methods=["GET"])
    async def download_source(request: Request) -> Response:
        """按语料内相对路径返回原文文件；Bearer由中间件校验，这里只管授权和路径安全。"""
        # 不读请求头：只有Bearer中间件注入的身份才可信，没装中间件时一律匿名。
        client_id = client_identity(bearer_enabled=bearer_enabled)
        corpus_id = request.path_params["corpus_id"]
        file_path = request.path_params["file_path"]
        try:
            access.require(corpus_id, client_id)
        except AccessDenied as exc:
            # 按项目约定：明确返回403，而不是伪装成404。
            return JSONResponse({"error": "forbidden", "detail": str(exc)}, status_code=403)
        entry = next(
            (item for item in corpus_catalog(root) if item.corpus_id == corpus_id), None
        )
        if entry is None or not entry.raw_data_subdir:
            return JSONResponse({"error": "not_found", "detail": "语料不存在"}, status_code=404)
        try:
            served = source_library.resolve_in_corpus(entry.raw_data_subdir, file_path)
        except SourceNotFound as exc:
            return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404)
        return FileResponse(
            served.path,
            media_type=served.media_type,
            headers=served.response_headers(),
        )

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
    api_key = os.getenv("WENJIN_MCP_API_KEY")
    # 没配密钥就没有中间件，此时客户端身份头不可信，必须让服务知道。
    server = build_mcp_server(bearer_enabled=bool(api_key))
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
