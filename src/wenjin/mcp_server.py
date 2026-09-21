"""问津RAG的状态化MCP服务。

协议按2025-11-25生命周期运行：客户端先initialize并发送initialized通知，HTTP会话可使用
MCP-Session-Id；启动时显式禁止stateless HTTP。

身份与授权有三条硬规则，改动前请先读：
1. **注册表只读一次**：正常运行路径上只由 `create_runtime()` 读一次，得到的`IndexTarget`
   同时用于索引校验、服务构造和授权身份（`RuntimeIdentity`），三者出自同一次读取；
   请求期间不再读注册表，否则"检查的语料"会和"实际检索的索引"漂移。
   仅当测试注入`service`且未给`identity`时，才在构造时补读一次。
2. **客户身份只在装了Bearer中间件时才可信**：没有中间件时 `x-wenjin-authenticated-client`
   完全由客户端控制，采信它等于允许冒充。
3. **访问策略每请求取一份完整快照**：请求中途改策略不会让同一次响应出现混合判定；
   快照加载失败一律拒绝，不回退到上一次成功的快照。
"""

import argparse
import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.responses import Response as StarletteResponse

from wenjin.access import AccessDenied, AccessRegistry, AccessSnapshotError, load_snapshot
from wenjin.audit import SearchAudit
from wenjin.auth import TRUSTED_CLIENT_HEADER_NAME, BearerAuthMiddleware
from wenjin.catalog import corpus_catalog, find_corpus, index_target_configs
from wenjin.corpus_identity import CorpusIdentityError
from wenjin.index_audit import preflight_target
from wenjin.index_registry import (
    IndexRegistryStore,
    IndexTarget,
    resolve_corpus_identity,
    resolve_project_path,
)
from wenjin.retrieval.service import RetrievalService, SearchConfig, load_source_urls
from wenjin.serving import (
    DOWNLOAD_PREFIX,
    CorpusSources,
    SourceModified,
    SourceNotFound,
    SourceTooLarge,
    SourceUnverified,
    attach_download_paths,
)

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PROTOCOL_VERSION = "2025-11-25"
# 同时在处理的取件请求数上限；配合 serving.MAX_SERVE_BYTES 给内存占用设上界。
MAX_CONCURRENT_DOWNLOADS = 2


@dataclass(frozen=True)
class RuntimeIdentity:
    """服务启动时确定的索引身份；请求期间不再读注册表。"""

    corpus_id: str
    corpus_id_source: str
    target_id: str


class SlotHoldingResponse(StarletteResponse):
    """把并发槽位持有到**正文发送结束**的响应包装。

    只包住"读取文件+构造响应"是不够的：函数一返回槽位就被释放，而框架才开始发送正文，
    此时那个已经读进内存的文件缓冲区还被慢客户端占着，新请求却能继续读取——内存上界
    就不成立了。所以槽位必须留到发送真正结束。

    `finally` 保证正常发送完、发送抛异常、客户端断开（发送协程被取消）都会通知槽位，
    不会有请求永久占着槽位。
    """

    def __init__(self, inner: StarletteResponse, on_settled: Callable[[], None]) -> None:
        super().__init__(status_code=inner.status_code)
        self._inner = inner
        self._on_settled = on_settled

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self._inner(scope, receive, send)
        finally:
            self._on_settled()


class DownloadSlot:
    """一次取件对并发槽位的持有权。

    槽位必须**同时**满足两件事才释放：

    1. **后台读取线程已经结束**。请求协程被取消不会停掉执行器里的线程，线程还占着刚读进
       内存的文件缓冲区；如果这时就释放槽位，后续请求会开始新的读取，声明的并发与内存
       上界就不成立（实测取消两次后读取并发峰值达到3，而上限是2）。
    2. **正文已经发完**，或确定不会有正文（读取抛异常/被取消）。

    两个标记由不同来源设置：线程结束由**独立工作任务**的 `finally` 设置（该任务不随请求
    协程取消，否则会在线程还在读时就被标记为已结束——这正是第一版的错误），正文结束由
    响应发送或异常分支设置。最后满足条件的那个负责释放，且只释放一次。
    """

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._thread_finished = False
        self._body_settled = False
        self._released = False

    def mark_thread_finished(self) -> None:
        self._thread_finished = True
        self._maybe_release()

    def mark_body_sent(self) -> None:
        self._body_settled = True
        self._maybe_release()

    def mark_no_body(self) -> None:
        """确定不会发送正文：读取抛了异常，或请求在被取消的状态下结束。"""
        self._body_settled = True
        self._maybe_release()

    def _maybe_release(self) -> None:
        if self._thread_finished and self._body_settled and not self._released:
            self._released = True
            self._release()


def create_runtime(
    *, use_reranker: bool = True, root: Path | None = None
) -> tuple[RetrievalService, RuntimeIdentity]:
    """加载Qdrant客户端和两个本地0.6B模型，并返回绑定在一起的索引身份。

    注册表在这里读一次，得到的`IndexTarget`同时用于索引校验、服务构造和授权身份——
    三者出自同一次读取，不会出现"检查的语料"与"实际检索的索引"不一致。

    **MCP服务用这个函数**；只需要service的调用方用`create_default_service()`。
    """
    import torch
    from qdrant_client import QdrantClient

    from wenjin.index_audit import admit_index
    from wenjin.retrieval.rerank import Qwen3Reranker
    from wenjin.settings import Settings

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    project_root = root or ROOT
    settings = Settings()
    target = IndexRegistryStore(project_root / "data/indexes/index-registry.json").active()
    identity = RuntimeIdentity(
        corpus_id=resolve_corpus_identity(target, root=project_root).corpus_id,
        corpus_id_source="registry",
        target_id=target.target_id,
    )
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
        # 启动也走完整准入：缺页、越界位置、非白名单文件在启动时就要拦住。
        admit_index(target, root=project_root, qdrant_client=client)
        reranker = (
            Qwen3Reranker(
                settings.reranker_model_path,
                device="cuda" if torch.cuda.is_available() else "cpu",
                max_length=1024,
                batch_size=2,
            )
            if use_reranker
            else None
        )
        service = RetrievalService(
            qdrant_client=client,
            embedder=settings.embedding(),
            reranker=reranker,
            bm25_path=resolve_project_path(project_root, target.bm25_path),
            source_urls=load_source_urls(
                resolve_project_path(project_root, target.manifest_path)
            ),
            collection_name=target.dense_collection,
            supported_version=target.version,
            index_target_id=target.target_id,
        )
        return service, identity
    except Exception:
        if reranker is not None:
            reranker.close()
        client.close()
        raise


def create_default_service(
    *, use_reranker: bool = True, root: Path | None = None
) -> RetrievalService:
    """只需要检索服务的调用方用这个（CLI、评测脚本、演示脚本）。

    返回值是`RetrievalService`本身，**不是元组**——保持既有调用方的契约不变；
    需要索引身份的MCP服务请用`create_runtime()`。
    """
    return create_runtime(use_reranker=use_reranker, root=root)[0]


def create_router(
    *,
    root: Path,
    qdrant_client: Any,
    embedder: Any,
    reranker: Any | None = None,
) -> Any | None:
    """为多语料候选建路由；没配置候选清单时返回 None。

    **复用调用方已有的 Qdrant 客户端与两个模型**：`create_runtime` 已经加载过它们，
    这里再建一份会让显存和启动时间都翻倍。

    建路由会对每个候选跑**完整准入**（实测 5 个候选约 2.8 秒）。放在启动而不是
    第一次调用：坏资产要在启动时就拦住，而不是等某个客户端恰好查到那个库才炸。

    `reranker` 与 `RetrievalService` 共用同一个实例，因此本函数返回的路由
    **不能**再 `close()`——释放由 service 负责，重复关闭会把共用的模型一起关掉。
    """
    from wenjin.retrieval.router import MultiCorpusRouter

    route_path = root / "configs/indexes/multicorpus-candidates-002.json"
    if not route_path.is_file():
        return None
    return MultiCorpusRouter.from_route_file(
        route_path,
        root=root,
        qdrant_client=qdrant_client,
        embedder=embedder,
        reranker=reranker,
    )


def identity_from_registry(root: Path) -> RuntimeIdentity | None:
    """按需构造身份时用一次；无法确定就返回None，由调用方按拒绝处理。"""
    registry = root / "data/indexes/index-registry.json"
    if not registry.is_file():
        return None
    try:
        target: IndexTarget = IndexRegistryStore(registry).active()
        identity = resolve_corpus_identity(target, root=root)
    except (KeyError, ValueError, OSError, json.JSONDecodeError):
        return None
    return RuntimeIdentity(identity.corpus_id, identity.source, target.target_id)


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


def build_mcp_server(
    service: RetrievalService | None = None,
    *,
    audit: SearchAudit | None = None,
    access: AccessRegistry | None = None,
    raw_root: Path | None = None,
    project_root: Path | None = None,
    bearer_enabled: bool = False,
    identity: RuntimeIdentity | None = None,
    corpus_router: Any | None = None,
) -> FastMCP:
    """创建只读的MCP服务（检索、语料目录、原文取件）。

    根目录、审计、访问策略和索引身份都可注入：测试用临时目录，部署用项目目录，
    模块里不写死。注入`service`时若没给`identity`，会**在构造时读一次**注册表。

    `corpus_router` 是多语料候选的路由（见 `create_router`）。不给它时
    `search_docs` 只能查 OSG active，指定 library 会明确报"未配置"而不是悄悄查错库。
    """
    if LATEST_PROTOCOL_VERSION != EXPECTED_PROTOCOL_VERSION:
        raise RuntimeError(
            f"MCP SDK协议版本为{LATEST_PROTOCOL_VERSION}，项目要求{EXPECTED_PROTOCOL_VERSION}"
        )

    root = project_root or ROOT
    audit = audit or SearchAudit.from_environment(root)
    raw_root_path = raw_root or (root / "raw_data")
    access_dir = root / "configs/access"

    # 只有在"注入了service"这条测试路径上才需要在这里解析身份。正常部署走lifespan，
    # 由create_runtime()连同检索服务一起确定——那时这里再读一次注册表是多余的，
    # 也会让"只读一次"这句话变成假的。
    if identity is None and service is not None:
        identity = identity_from_registry(root)

    def current_access() -> AccessRegistry:
        """每个请求开始取一份完整策略快照；注入的注册表直接沿用。"""
        return access if access is not None else load_snapshot(access_dir)

    def sources_for(corpus_id: str) -> CorpusSources:
        """按语料身份找manifest，建立登记记录视图。"""
        entry = find_corpus(root, corpus_id)
        if entry is None:
            raise SourceNotFound(f"语料不存在：{corpus_id}")
        return CorpusSources.from_manifest(
            resolve_project_path(root, entry.manifest_path),
            raw_root=raw_root_path,
            expected_corpus_id=corpus_id,
        )

    def current_service(ctx: Context | None):
        """本次请求的检索服务；不在这里新建，也不读注册表。"""
        runtime = service
        if runtime is None:
            if ctx is None:
                raise RuntimeError("MCP Context不可用")
            runtime = ctx.lifespan_context["retrieval_service"]
        return runtime

    def current_router(ctx: Context | None):
        """本次请求的多语料路由；未配置时返回 None。"""
        if corpus_router is not None:
            return corpus_router
        if ctx is None:
            return None
        return ctx.lifespan_context.get("corpus_router")

    def _index_state(entry, *, ctx: Context | None) -> dict:
        """这份语料的索引现在能不能用。

        `indexed` 只表示"有配置提到它"；配置在而 collection 被删、BM25 缺失或
        manifest 换过时，旧实现照样报 true，实际一条都检索不出来。
        逐 target 做一次廉价预检（见 `preflight_target`），把两种状态分开报。
        """
        if not entry.index_targets:
            return {"index_state": "no_index_config", "index_usable_targets": [],
                    "index_failures": []}
        configs = index_target_configs(root)
        try:
            client = current_service(ctx).qdrant_client
        except Exception:  # noqa: BLE001 - 拿不到客户端时如实报"不可用"，不猜
            client = None
        usable, failures = [], []
        for target_id in entry.index_targets:
            raw = configs.get(target_id)
            if raw is None:
                failures.append({"target_id": target_id, "reason": "config_missing"})
                continue
            try:
                target = IndexTarget.from_dict(raw)
            except (TypeError, ValueError):
                failures.append({"target_id": target_id, "reason": "config_invalid"})
                continue
            if client is None:
                failures.append({"target_id": target_id, "reason": "qdrant_unavailable"})
                continue
            result = preflight_target(target, root=root, qdrant_client=client)
            if result["usable"]:
                usable.append(target_id)
            else:
                failures.append({"target_id": target_id, "reason": result["reason"]})
        return {
            "index_state": "usable" if usable else "unusable",
            "index_usable_targets": usable,
            "index_failures": failures,
        }

    # 并发取件上限。单个文件最多MAX_SERVE_BYTES在内存里，两个并发就是两倍；
    # 限制住才能让"内存占用有上界"这句话成立。这是每个服务实例一份。
    download_slots = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    # 取件工作任务的强引用集合；任务结束后自动移除。
    background_tasks: set[asyncio.Task] = set()

    if service is None:

        @asynccontextmanager
        async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
            runtime, runtime_identity = create_runtime(root=root)
            try:
                # 复用 service 已加载的客户端与模型，不重复加载。
                router = create_router(
                    root=root,
                    qdrant_client=runtime.qdrant_client,
                    embedder=runtime.embedder,
                    reranker=runtime.reranker,
                )
                yield {
                    "retrieval_service": runtime,
                    "identity": runtime_identity,
                    "corpus_router": router,
                }
            finally:
                # router 与 service 共用模型，只由 service 释放。
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

    def request_identity(ctx: Context | None) -> RuntimeIdentity | None:
        """本次请求对应的索引身份；不在这里读注册表。"""
        if service is not None or ctx is None:
            return identity
        return ctx.lifespan_context.get("identity", identity)

    def forbidden(detail: str, *, corpus_id: str | None, query: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "error": "forbidden",
            "detail": detail,
            "corpus_id": corpus_id,
            "evidence": [],
            "evidence_status": "not_retrieved",
            "answer_status": "not_generated",
            "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
        }
        if query is not None:
            payload["query"] = query
        return payload

    def _search_routed(
        *, query: str, library: str | None, version: str | None, target_id: str | None,
        top_k: int, context_char_budget: int, ctx: Context | None,
        client_id: str | None, session_id: str | None, started: float,
    ) -> dict:
        """多语料检索：按 library(+version) 或唯一 target_id 寻址，并按权限过滤。

        **先按权限过滤、再解析目标**。顺序反过来会有两处问题：一是会去实际查询无权读取
        的索引（既浪费又在时序上泄露信息），二是"无权"和"不存在"会给出不同的报错，
        等于告诉调用方"这个语料存在但你读不了"。过滤之后两者都表现为同一个解析失败。
        """
        from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig

        def denied(detail: str) -> dict:
            audit.record(
                query=query, version=version or "routed", top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id, elapsed_seconds=time.perf_counter() - started,
                error=AccessDenied(detail),
            )
            return forbidden(detail, corpus_id=None, query=query)

        router = current_router(ctx)
        if router is None:
            return denied("本服务未配置多语料索引，只能用默认的 OSG 索引检索")

        try:
            registry = current_access()
        except AccessSnapshotError as exc:
            return denied(f"访问策略不可用：{exc}")

        allowed = [
            index for index in router.indices
            if registry.check(index.corpus_identity, client_id)
        ]
        # 被权限挡掉的数量只进服务端审计：语料名本身就不该给无权方看到。
        if len(allowed) != len(router.indices):
            audit.record_dropped_evidence_details(
                query=query,
                dropped=[{"code": "not_authorized",
                          "corpus_id": index.corpus_identity} for index in router.indices
                         if index not in allowed],
                client_id=client_id,
            )
        if not allowed:
            return denied("没有可检索的语料")

        # 只带授权子集的路由视图：构造本身无 I/O，共用同一套客户端与模型。
        scoped = MultiCorpusRouter(
            qdrant_client=router.qdrant_client,
            embedder=router.embedder,
            indices=allowed,
            reranker=router.reranker,
            dense_search=router.dense_search,
            keyword_search=router.keyword_search,
        )
        try:
            result = scoped.search(
                query, library=library, version=version, target_id=target_id,
                config=RouteConfig(
                    candidate_k=20, top_k=top_k,
                    context_char_budget=context_char_budget,
                ),
            )
        except ValueError as exc:
            # 目标解析失败（库/版本不存在、版本有歧义、target 不存在）：这是调用方
            # 用法问题，如实回报。因为已按权限过滤过，这里不会泄露无权语料的存在。
            return {
                "error": "invalid_target",
                "detail": str(exc),
                "query": query,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }

        # 明细只进服务端审计，绝不能回给客户端：它含 corpus_id / source_path / chunk_id，
        # "某个语料里有个叫 X 的文件"本身就是机密。与 OSG 单库路径同一口径。
        details = result.pop("evidence_dropped_details", [])
        if details:
            audit.record_dropped_evidence_details(
                query=query, dropped=details, client_id=client_id
            )

        request_id = audit.record(
            query=query, version=version or "routed", top_k=top_k,
            context_char_budget=context_char_budget, session_id=session_id,
            client_id=client_id, elapsed_seconds=time.perf_counter() - started,
            result=result,
        )
        result["request_id"] = request_id
        result["mcp_protocol"] = EXPECTED_PROTOCOL_VERSION
        return result

    @mcp.tool(
        name="search_docs",
        description=(
            "Search the knowledge base and return ranked evidence with file paths, line ranges "
            "(or PDF page numbers), upstream URLs, and raw text. This tool does not generate an "
            "answer. By default it searches the OSG 3.6.5 active index; pass `library` (and "
            "`version` if that library has several) or `target_id` to search another corpus — "
            "call list_corpora first to see what is indexed and usable."
        ),
        run_in_thread=True,
    )
    def search_docs(
        query: str,
        top_k: int = 5,
        context_char_budget: int = 8000,
        version: str | None = None,
        library: str | None = None,
        target_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """检索证据；ctx只用于取得服务生命周期对象，不充当每请求协议身份。

        两条路径：
        - **不指定 library/target_id**：走 OSG active 的单库服务，行为与之前完全一致；
        - **指定了**：走多语料路由，按 library(+version) 或唯一 target_id 寻址，
          并且**只搜索调用方有权读取的语料**。
        """
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

        if library is not None or target_id is not None:
            return _search_routed(
                query=query, library=library, version=version, target_id=target_id,
                top_k=top_k, context_char_budget=context_char_budget,
                ctx=ctx, client_id=client_id, session_id=session_id, started=started,
            )

        # 未指定 library 时保持 OSG active 的原有语义（含默认版本）。
        active_version = getattr(runtime, "supported_version", "3.6.5")
        if version is None:
            version = "3.6.5"
        elif version != active_version:
            # 只给 version 不给 library，多半是想查别的语料。这里直接给结构化错误，
            # 而不是让 runtime 抛 ValueError——服务端开了错误屏蔽，抛异常客户端只会看到
            # "Error calling tool"，分不清是参数写错还是服务坏了。
            return {
                "error": "invalid_target",
                "detail": (
                    f"不带 library 时只检索 OSG {active_version}。要查其他版本请同时给出 "
                    f"library（例如 library=\"gdal\", version=\"3.13.1\"）；"
                    f"先用 list_corpora 可以看到有哪些库和版本。"
                ),
                "query": query,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
        current = request_identity(ctx)
        corpus_id = current.corpus_id if current else None

        # 检索路径同样过访问策略：只锁下载是没用的，能拿到chunk正文就等于看到了内容。
        # 被拒绝时返回结构化的forbidden结果而不是抛异常：服务端开了错误屏蔽，
        # 抛异常会让客户端只看到"调用失败"，分不清是被拒还是服务坏了。
        try:
            if current is None:
                raise AccessDenied("无法确定服务对应的索引身份，拒绝检索")
            current_access().require(corpus_id, client_id)
        except (AccessDenied, AccessSnapshotError) as exc:
            audit.record(
                query=query, version=version, top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id,
                elapsed_seconds=time.perf_counter() - started, error=exc,
            )
            return forbidden(str(exc), corpus_id=corpus_id, query=query)

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

        # 补取件链接；映射不到本语料的证据会被丢弃并单独报告，绝不重标成当前语料。
        attachment = attach_download_paths(result["evidence"], sources_for(corpus_id))
        result["evidence"] = attachment.evidence
        # 只给客户端数量和原因码：完整明细含语料名、路径和chunk ID，
        # "某个私有语料里有个叫X的文件"本身就是机密。明细进服务端审计。
        result["evidence_dropped"] = attachment.dropped_summary()
        if attachment.dropped:
            audit.record_dropped_evidence_details(
                query=query, dropped=attachment.dropped, client_id=client_id
            )
            audit.record(
                query=query, version=version, top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id,
                elapsed_seconds=time.perf_counter() - started,
                error=RuntimeError(
                    f"{len(attachment.dropped)}条证据不属于当前语料，已丢弃"
                ),
            )
        request_id = audit.record(
            query=query, version=version, top_k=top_k,
            context_char_budget=context_char_budget, session_id=session_id,
            client_id=client_id,
            elapsed_seconds=time.perf_counter() - started, result=result,
        )
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
        """返回调用方有权查看的语料清单；无权限的语料不会出现，也不暴露其存在。

        **"有索引配置"不等于"索引能用"**：配置文件在而 collection 被删、BM25 缺失或
        manifest 换过，语料在旧实现里照样报 indexed=true，但它一条都检索不出来。
        因此每条语料额外给 `index_state`，由预检得出。
        """
        client_id = client_identity(bearer_enabled=bearer_enabled)
        try:
            registry = current_access()
        except AccessSnapshotError as exc:
            return {"corpora": [], "count": 0, "error": "unavailable", "detail": str(exc),
                    "mcp_protocol": EXPECTED_PROTOCOL_VERSION}
        entries = []
        for entry in corpus_catalog(root):
            if not registry.check(entry.corpus_id, client_id):
                continue
            payload = entry.to_dict(include_files=include_files)
            payload.update(_index_state(entry, ctx=ctx))
            entries.append(payload)
        usable = sum(1 for item in entries if item["index_state"] == "usable")
        return {
            "corpora": entries,
            "count": len(entries),
            "usable_count": usable,
            "index_state_note": (
                "'usable' means a preflight check passed (manifest bytes, BM25, collection "
                "and point count all match). It is NOT full admission — run "
                "scripts/verify_candidate_indexes.py for that. 'no_index_config' means no "
                "index target references this corpus yet."
            ),
            "note": (
                "Only corpora this client is allowed to read are listed. "
                "Retrieval scores are ranking signals, not answer confidence."
            ),
            "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
        }

    @mcp.custom_route(f"{DOWNLOAD_PREFIX}/{{corpus_id}}/{{file_path:path}}", methods=["GET"])
    async def download_source(request: Request) -> Response:
        """按语料内相对路径返回原文；Bearer由中间件校验，这里只管授权、登记与字节校验。

        取件要读文件、算Hash，可能几十MB，全是同步阻塞操作。放在事件循环里会卡住
        同一进程的其他连接（实测一次200ms读取会把本该20ms的定时任务推迟到203ms），
        所以丢到有界线程池执行；同时用信号量限制并发，避免多个大文件同时占满内存。
        """
        client_id = client_identity(bearer_enabled=bearer_enabled)
        # 路径参数在事件循环里取好，线程里不再碰请求对象。
        corpus_id = request.path_params["corpus_id"]
        relative_path = request.path_params["file_path"]

        # 手动acquire而不是async with：槽位要留到读取线程结束且正文发完，见DownloadSlot。
        await download_slots.acquire()
        slot = DownloadSlot(download_slots.release)

        async def run_work() -> StarletteResponse:
            try:
                return await run_in_threadpool(
                    partial(
                        serve_download,
                        corpus_id=corpus_id,
                        relative_path=relative_path,
                        client_id=client_id,
                        current_access=current_access,
                        sources_for=sources_for,
                        audit=audit,
                    )
                )
            finally:
                # 只有线程真正结束才会走到这里。run_work跑在独立任务里，
                # 不随请求协程取消——否则会在线程还在读时就把槽位标记为已结束。
                slot.mark_thread_finished()

        # 独立任务 + shield：请求被取消时工作继续跑完，槽位由它的 finally 释放。
        work = asyncio.create_task(run_work())
        background_tasks.add(work)  # 持强引用，避免任务被GC提前回收
        work.add_done_callback(background_tasks.discard)
        try:
            response = await asyncio.shield(work)
        except BaseException:
            # 取消或异常都**不**在这里释放槽位：线程可能还在读。只声明"不会有正文"，
            # 由 mark_thread_finished 与它共同决定何时真正释放。
            slot.mark_no_body()
            raise
        return SlotHoldingResponse(response, slot.mark_body_sent)

    return mcp


def serve_download(
    *,
    corpus_id: str,
    relative_path: str,
    client_id: str | None,
    current_access: Callable[[], AccessRegistry],
    sources_for: Callable[[str], CorpusSources],
    audit: SearchAudit,
) -> Response:
    """取件的完整判定，独立成函数以便直接测试和审计。

    四个条件必须同时满足：调用方获准访问该语料、文件登记在manifest中、
    路径位于该语料可信目录内、文件字节符合登记的raw_sha256。
    """
    started = time.perf_counter()

    def finish(outcome: str, status: int, payload: dict, *, bytes_served: int | None = None,
               detail: str | None = None, response: Response | None = None) -> Response:
        audit.record_download(
            corpus_id=corpus_id, relative_path=relative_path, client_id=client_id,
            outcome=outcome, elapsed_seconds=time.perf_counter() - started,
            bytes_served=bytes_served, detail=detail,
        )
        return response if response is not None else JSONResponse(payload, status_code=status)

    try:
        registry = current_access()
    except AccessSnapshotError as exc:
        return finish("error", 503, {"error": "unavailable", "detail": str(exc)})
    try:
        registry.require(corpus_id, client_id)
    except AccessDenied as exc:
        # 按项目约定：明确返回403，而不是伪装成404。
        return finish("denied", 403, {"error": "forbidden", "detail": str(exc)})

    # 缺Hash可能在**两个阶段**暴露：加载manifest时（content_hash_of校验每条登记记录）
    # 和读取文件时。两处都要给出结构化拒绝并留下审计——只捕获后一处的话，
    # manifest阶段的异常会变成500且不留任何记录。
    unverified_detail = "语料的原文校验信息不完整，拒绝返回内容"

    try:
        sources = sources_for(corpus_id)
    except SourceNotFound as exc:
        return finish("not_found", 404, {"error": "not_found", "detail": str(exc)})
    except SourceUnverified as exc:
        # 部署问题：给客户端通用说明，具体缺哪条记录只进服务端审计。
        return finish("unverified", 409,
                      {"error": "source_unverified", "detail": unverified_detail},
                      detail=str(exc))
    except (CorpusIdentityError, ValueError):
        # 身份冲突或manifest损坏属于部署错误：拒绝，且不向客户端透露细节。
        return finish("unavailable", 503, {"error": "unavailable",
                                           "detail": "语料配置无效，请联系管理员"})

    try:
        verified = sources.read_verified(relative_path)
    except SourceNotFound as exc:
        return finish("not_found", 404, {"error": "not_found", "detail": str(exc)})
    except SourceModified as exc:
        # 语料被改动过：宁可拒绝，也不能把"和登记Hash不符"的内容当原文送出去。
        return finish("hash_mismatch", 409, {"error": "source_modified", "detail": str(exc)})
    except SourceUnverified as exc:
        # 没有可用Hash就无法证明取回的是被引用的那份原文，只能拒绝。
        return finish("unverified", 409,
                      {"error": "source_unverified", "detail": unverified_detail},
                      detail=str(exc))
    except SourceTooLarge as exc:
        return finish("too_large", 413, {"error": "too_large", "detail": str(exc)})

    return finish(
        "served", 200, {},
        bytes_served=len(verified.content),
        response=StarletteResponse(
            content=verified.content,
            media_type=verified.media_type,
            headers=verified.response_headers(),
        ),
    )


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
