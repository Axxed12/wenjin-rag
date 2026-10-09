"""MCP 检索、目录和受管文件工具及其请求级授权逻辑。"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from typing import Any
from urllib.parse import urlencode

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers, get_http_request

from wenjin.access import AccessDenied, AccessRegistry, AccessSnapshotError, load_snapshot
from wenjin.auth import TRUSTED_CLIENT_HEADER_NAME
from wenjin.catalog import corpus_catalog_report, find_corpus, index_target_configs
from wenjin.corpus_identity import CorpusIdentityError
from wenjin.identity.mcp_auth import identity_from_context, managed_bridge_headers
from wenjin.index_audit import preflight_target
from wenjin.index_registry import IndexTarget, resolve_project_path
from wenjin.mcp.managed_client import (
    ManagedFileConflict,
    ManagedFileHTTPClient,
    ManagedFileServiceError,
    require_managed_file_access,
    visible_managed_files,
)
from wenjin.mcp.runtime import EXPECTED_PROTOCOL_VERSION, RuntimeIdentity
from wenjin.releases.resolver import release_corpus_entry
from wenjin.retrieval.router import normalize_library
from wenjin.retrieval.search_policy import SearchPolicy
from wenjin.serving import DOWNLOAD_PREFIX, CorpusSources, SourceNotFound, attach_download_paths

catalog_logger = logging.getLogger("wenjin.catalog")
catalog_logger.setLevel(logging.WARNING)
catalog_logger.propagate = False
if not catalog_logger.handlers:
    _catalog_handler = logging.StreamHandler(sys.stdout)
    _catalog_handler.setFormatter(logging.Formatter("%(message)s"))
    catalog_logger.addHandler(_catalog_handler)

security_logger = logging.getLogger("wenjin.security")

def log_identity_problems(report) -> None:
    """把被隔离的目录写进服务端日志；这是网络接口**不**外发它们之后的可见性出口。

    逐条包 try：运维日志失败不能把一次成功的工具调用变成工具错误。这里用宽泛的
    `except Exception` 是刻意的——handler 的错误类型不可穷举（格式化、编码、I/O 都可能），
    而这段代码的契约就是"绝不向外抛"（logging 只在 handler 自己调 handleError 时才吞异常）。
    """
    for problem in report.problems:
        try:
            catalog_logger.warning(
                "语料身份无法确定，已隔离：%s reason=%s visibility=%s declared_id=%s "
                "detail=%s",
                problem.location, problem.reason, problem.visibility,
                problem.declared_id, problem.detail,
            )
        except Exception:  # noqa: BLE001 - 日志失败不影响工具可用性，见 docstring
            pass

def policy_unavailable(exc: Exception, *, tool: str) -> str:
    """记下策略快照加载失败，返回**可以外发**的客户端说明。

    `AccessSnapshotError` 的正文是 `访问策略快照加载失败：<内层异常>`，内层异常带着
    与文件名不一致的**声明语料身份**或策略文件路径。整体拒绝（fail closed）是对的，
    但把它原样回给客户端，等于让没有该语料权限的调用方靠一句报错就知道"内网里有这么
    一份语料"——`corpora-private/` 底下的也算。与 `log_identity_problems` 同一口径：
    被隔离的身份只走服务端日志。
    """
    try:
        security_logger.warning(
            "访问策略快照加载失败，已整体拒绝：tool=%s error=%s", tool, exc
        )
    except Exception:  # noqa: BLE001 - 日志失败不能把一次拒绝变成崩溃
        pass
    return "访问策略不可用，已拒绝本次请求"

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

def register_tools(
    mcp: FastMCP, *, root, raw_root_path, audit, access, access_dir,
    bearer_enabled, identity, service, corpus_router, release_resolver, managed_bridge,
    identity_store=None, search_policy_holder=None,
) -> tuple[Any, Any]:
    search_policy_holder = search_policy_holder or {
        "value": getattr(service, "search_policy", None) or SearchPolicy(
            reranker_enabled=getattr(service, "reranker", None) is not None
        )
    }
    def current_access() -> AccessRegistry:
        """每个请求开始取一份完整策略快照；注入的注册表直接沿用。"""
        return access if access is not None else load_snapshot(access_dir)


    def current_release_resolver(ctx: Context | None = None):
        resolver = release_resolver() if callable(release_resolver) else release_resolver
        if resolver is None and ctx is not None:
            try:
                resolver = ctx.lifespan_context.get("release_resolver")
            except RuntimeError:
                resolver = None
        return resolver


    def current_release_request(ctx: Context | None = None):
        resolver = current_release_resolver(ctx)
        return resolver.begin_request() if resolver is not None else None


    def sources_for(corpus_id: str, *, release_id: str | None = None) -> CorpusSources:
        """按语料身份找manifest，建立登记记录视图。"""
        resolver = current_release_resolver()
        if resolver is not None:
            return resolver.sources_for(corpus_id, release_id=release_id)
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


    def current_managed_bridge(ctx: Context | None) -> ManagedFileHTTPClient | None:
        if managed_bridge is not None:
            return managed_bridge
        if ctx is None:
            return None
        return ctx.lifespan_context.get("managed_file_bridge")


    def request_principal(ctx: Context | None):
        if identity_store is None:
            return None
        if ctx is None:
            raise RuntimeError("account MCP请求缺少Context")
        return identity_from_context(ctx).principal


    def request_bridge_authorization(ctx: Context | None) -> str | None:
        if identity_store is None:
            return None
        if ctx is None:
            raise RuntimeError("account MCP请求缺少Context")
        return managed_bridge_headers(get_http_request().scope)["Authorization"]


    def authorized_library_ids(ctx: Context | None) -> set[str]:
        """通过当前个人凭据同步授权目录，并按本地最新grant筛选稳定library。"""
        if identity_store is None:
            return set()
        bridge = current_managed_bridge(ctx)
        if bridge is None:
            return set()
        principal = request_principal(ctx)
        catalog = bridge.list_libraries(authorization=request_bridge_authorization(ctx))
        visible = set()
        for item in catalog:
            library_id = item.get("library_id")
            if (isinstance(library_id, str)
                    and identity_store.can_read(principal, "library", library_id)):
                visible.add(normalize_library(library_id))
        return visible


    def effective_client_id(ctx: Context | None) -> str | None:
        principal = request_principal(ctx)
        return principal.user_id if principal is not None else client_identity(bearer_enabled=bearer_enabled)


    def audit_session_id(ctx: Context | None) -> str | None:
        try:
            session_id = ctx.session_id if ctx is not None else None
        except RuntimeError:
            session_id = None
        if session_id and identity_store is not None:
            return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return session_id


    def managed_unavailable(query: str, *, error: str = "file_not_available") -> dict:
        return {
            "error": error,
            "detail": (
                "该文件不可用或当前客户端无权访问"
                if error == "file_not_available"
                else "managed文件检索暂不可用"
            ),
            "query": query,
            "evidence": [],
            "evidence_status": "not_retrieved",
            "answer_status": "not_generated",
            "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
        }


    def search_managed_file(
        *, file_id: str, query: str, version: str | None, top_k: int,
        context_char_budget: int, ctx: Context | None,
    ) -> dict:
        started = time.perf_counter()
        session_id = audit_session_id(ctx)
        client_id = effective_client_id(ctx)
        audit_version = version or "managed-active"

        def finish(result: dict, *, error: Exception | None = None) -> dict:
            request_id = audit.record(
                query=query, version=audit_version, top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id, elapsed_seconds=time.perf_counter() - started,
                result=result if error is None else None, error=error,
            )
            result["request_id"] = request_id
            result["mcp_protocol"] = EXPECTED_PROTOCOL_VERSION
            return result

        if not query.strip() or len(query) > 2000:
            return finish(
                {**managed_unavailable(query, error="invalid_query"),
                 "detail": "query长度必须在1到2000字符之间"},
                error=ValueError("invalid managed query"),
            )
        if identity_store is None:
            try:
                registry = current_access()
                require_managed_file_access(registry, file_id, client_id)
            except (AccessDenied, AccessSnapshotError, ValueError) as exc:
                # file id、策略拒绝与损坏策略均不向客户端暴露；不触碰管理服务。
                return finish(managed_unavailable(query), error=exc)

        bridge = current_managed_bridge(ctx)
        if bridge is None:
            return finish(
                managed_unavailable(query, error="unavailable"),
                error=ManagedFileServiceError("managed bridge is not configured"),
            )
        try:
            result = bridge.search_file(
                file_id,
                query,
                version=version,
                top_k=top_k,
                context_char_budget=context_char_budget,
                authorization=request_bridge_authorization(ctx),
            )
        except (KeyError, AccessDenied) as exc:
            return finish(managed_unavailable(query), error=exc)
        except ManagedFileConflict as exc:
            return finish(
                {**managed_unavailable(query, error="invalid_target"),
                 "detail": "请求版本不是当前active版本，或检索参数无效"},
                error=exc,
            )
        except (ManagedFileServiceError, OSError) as exc:
            return finish(
                managed_unavailable(query, error="unavailable"), error=exc
            )
        managed_identity = result.get("managed_file", {})
        actual_version = managed_identity.get("version")
        version_id = managed_identity.get("version_id")
        if isinstance(actual_version, str):
            audit_version = actual_version
        if isinstance(version_id, str) and re.fullmatch(r"[0-9a-f]{32,64}", version_id):
            download_path = (
                f"{DOWNLOAD_PREFIX}/managed/{file_id}?"
                f"{urlencode({'version_id': version_id})}"
            )
            for evidence in result.get("evidence", []):
                if isinstance(evidence, dict):
                    evidence["download_path"] = download_path
        return finish(result)


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
        router_override=None,
        release_resolver_override=None,
    ) -> dict:
        """多语料检索：按 library(+version) 或唯一 target_id 寻址，并按权限过滤。

        **先按权限过滤、再解析目标**。顺序反过来会有两处问题：一是会去实际查询无权读取
        的索引（既浪费又在时序上泄露信息），二是"无权"和"不存在"会给出不同的报错，
        等于告诉调用方"这个语料存在但你读不了"。过滤之后两者都表现为同一个解析失败。
        """
        from wenjin.releases.service import ReleaseValidationError
        from wenjin.retrieval.router import MultiCorpusRouter
        from wenjin.retrieval.source_context import SourceContextIntegrityError

        def denied(detail: str, *, error: Exception | None = None) -> dict:
            # 默认把"拒绝"本身记成 AccessDenied；策略快照加载失败要记成真实的异常类型，
            # 否则审计里"快照坏了"会伪装成一次普通的无权访问。`error` 只取类型名入行，
            # 正文不外发也不入日志行。
            audit.record(
                query=query, version=version or "routed", top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id, elapsed_seconds=time.perf_counter() - started,
                error=error or AccessDenied(detail),
            )
            return forbidden(detail, corpus_id=None, query=query)

        router = router_override or current_router(ctx)
        if router is None:
            return denied("本服务未配置多语料索引，只能用默认的 OSG 索引检索")

        if identity_store is not None:
            try:
                visible_libraries = authorized_library_ids(ctx)
                principal = request_principal(ctx)
                allowed = [
                    index for index in router.indices
                    if normalize_library(index.library) in visible_libraries
                    and identity_store.can_read(
                        principal, "library", normalize_library(index.library)
                    )
                ]
            except Exception as exc:
                return denied("当前资料库授权不可用，已拒绝本次检索", error=exc)
        else:
            try:
                registry = current_access()
            except AccessSnapshotError as exc:
                return denied(policy_unavailable(exc, tool="search_docs"), error=exc)
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
        active_policy = (
            search_policy_holder["value"]
            or getattr(service, "search_policy", None)
            or SearchPolicy(reranker_enabled=router.reranker is not None)
        )
        if active_policy.reranker_enabled != (router.reranker is not None):
            raise RuntimeError("共享检索配置与当前reranker实例状态不一致")

        # 只带授权子集的路由视图：构造本身无 I/O，共用同一套客户端与模型。
        scoped = MultiCorpusRouter(
            qdrant_client=router.qdrant_client,
            embedder=router.embedder,
            indices=allowed,
            reranker=router.reranker,
            dense_search=router.dense_search,
            keyword_search=router.keyword_search,
            search_lock=router.search_lock,
        )
        try:
            if release_resolver_override is not None:
                selected_indices = scoped._resolve_request(library, version, target_id)
                materialized = release_resolver_override.materialize_indices(selected_indices)
                replacements = {
                    index.target_id: index for index in materialized
                }
                scoped = MultiCorpusRouter(
                    qdrant_client=router.qdrant_client,
                    embedder=router.embedder,
                    indices=[replacements.get(index.target_id, index) for index in allowed],
                    reranker=router.reranker,
                    dense_search=router.dense_search,
                    keyword_search=router.keyword_search,
                    search_lock=router.search_lock,
                )
            result = scoped.search(
                query, library=library, version=version, target_id=target_id,
                config=active_policy.route_config(
                    top_k, context_char_budget=context_char_budget
                ),
            )
            result["retrieval_config"] = active_policy.public_config(
                top_k, context_char_budget=context_char_budget
            )
        except SourceContextIntegrityError as exc:
            # 来源信任失败是服务端安全问题，不是调用方目标用法错误；绝不回传路径/Hash详情。
            audit.record(
                query=query, version=version or "routed", top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id, elapsed_seconds=time.perf_counter() - started,
                error=exc,
            )
            return {
                "error": "source_integrity_failure",
                "detail": "登记来源完整性检查失败，本次未返回检索结果",
                "query": query,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
        except ReleaseValidationError as exc:
            audit.record(
                query=query, version=version or "routed", top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id, elapsed_seconds=time.perf_counter() - started,
                error=exc,
            )
            return {
                "error": "source_integrity_failure",
                "detail": "active资料库完整性检查失败，本次未返回检索结果",
                "query": query,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
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
            "answer. By default it searches the current active OSG index; pass `library` (and "
            "`version` if that library has several) or `target_id` to search another corpus. "
            "Use `file_id` from list_managed_files to search one active uploaded file."
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
        file_id: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """检索证据；ctx只用于取得服务生命周期对象，不充当每请求协议身份。

        默认和显式指定 active OSG走active单库服务；library/target_id走多库路由；
        file_id走受管文件HTTP服务。后两者都按当前client身份执行ACL。
        """
        if file_id is not None:
            if library is not None or target_id is not None:
                return {
                    "error": "invalid_target",
                    "detail": "file_id不能与library或target_id同时使用",
                    "query": query,
                    "evidence": [],
                    "evidence_status": "not_retrieved",
                    "answer_status": "not_generated",
                    "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
                }
            return search_managed_file(
                file_id=file_id,
                query=query,
                version=version,
                top_k=top_k,
                context_char_budget=context_char_budget,
                ctx=ctx,
            )

        runtime = service
        if runtime is None:
            if ctx is None:
                raise RuntimeError("MCP Context不可用")
            runtime = ctx.lifespan_context["retrieval_service"]
        active_policy = (
            search_policy_holder["value"]
            or getattr(runtime, "search_policy", None)
            or SearchPolicy(reranker_enabled=getattr(runtime, "reranker", None) is not None)
        )
        started = time.perf_counter()
        session_id = audit_session_id(ctx)
        client_id = effective_client_id(ctx)

        try:
            request_release = current_release_request(ctx)
        except Exception as exc:
            audit.record(
                query=query, version=version or "release-catalog", top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id, elapsed_seconds=time.perf_counter() - started,
                error=exc,
            )
            return {
                "error": "unavailable",
                "detail": "active资料库快照不可用，已拒绝本次检索",
                "query": query,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }

        current = request_identity(ctx)
        active_version = getattr(runtime, "supported_version", None)
        active_target_id = current.target_id if current is not None else getattr(
            runtime, "index_target_id", None
        )
        active_library = None
        if current is not None:
            try:
                active_entry = find_corpus(root, current.corpus_id)
            except (CorpusIdentityError, OSError, ValueError):
                active_entry = None
            if active_entry is not None:
                active_library = active_entry.library
                if active_version is None:
                    active_version = active_entry.version

        def invalid_target(detail: str) -> dict:
            return {
                "error": "invalid_target",
                "detail": detail,
                "query": query,
                "evidence": [],
                "evidence_status": "not_retrieved",
                "answer_status": "not_generated",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }

        if request_release is not None:
            selected_library = library
            if selected_library is None and target_id is None:
                selected_library = request_release.default_library
            if selected_library is None and target_id is None:
                return invalid_target("无法确定默认active资料库，拒绝执行检索。")
            return _search_routed(
                query=query,
                library=selected_library,
                version=version,
                target_id=target_id,
                top_k=top_k,
                context_char_budget=context_char_budget,
                ctx=ctx,
                client_id=client_id,
                session_id=session_id,
                started=started,
                router_override=request_release.router,
                release_resolver_override=current_release_resolver(ctx),
            )

        # active target 随服务启动时的 registry identity 确定，不能写死某个 target_id。
        active_target_requested = target_id is not None and target_id == active_target_id
        active_library_requested = False
        if library is not None and active_library is not None:
            active_library_requested = (
                normalize_library(library) == normalize_library(active_library)
            )

        if active_target_requested and library is not None and not active_library_requested:
            return invalid_target(
                "target_id 指向当前 active 索引时，library 必须与该索引所属资料库一致。"
            )
        if active_library_requested and target_id is not None and not active_target_requested:
            return invalid_target(
                "library 与 target_id 不能同时用于选择不同的索引目标。"
            )
        if not active_target_requested and not active_library_requested \
                and (library is not None or target_id is not None):
            return _search_routed(
                query=query, library=library, version=version, target_id=target_id,
                top_k=top_k, context_char_budget=context_char_budget,
                ctx=ctx, client_id=client_id, session_id=session_id, started=started,
            )

        # 默认或显式选择当前 active 时都走单库服务，版本动态绑定到当前运行时。
        if active_version is None:
            return invalid_target("无法确定当前 active 索引版本，拒绝执行检索。")
        if version is None:
            version = active_version
        elif version != active_version:
            return invalid_target(
                f"当前 active 索引只支持 {active_version}。要查询其他资料库或版本，"
                "请使用 library（以及 version）或 target_id；先调用 list_corpora 查看可用目标。"
            )
        corpus_id = current.corpus_id if current else None

        # 检索路径同样过访问策略：只锁下载是没用的，能拿到chunk正文就等于看到了内容。
        # 被拒绝时返回结构化的forbidden结果而不是抛异常：服务端开了错误屏蔽，
        # 抛异常会让客户端只看到"调用失败"，分不清是被拒还是服务坏了。
        try:
            if current is None:
                raise AccessDenied("无法确定服务对应的索引身份，拒绝检索")
            if identity_store is not None:
                visible_libraries = authorized_library_ids(ctx)
                if (active_library is None
                        or normalize_library(active_library) not in visible_libraries):
                    raise AccessDenied("当前账号没有该资料库的读取授权")
            else:
                current_access().require(corpus_id, client_id)
        except (AccessDenied, AccessSnapshotError) as exc:
            audit.record(
                query=query, version=version, top_k=top_k,
                context_char_budget=context_char_budget, session_id=session_id,
                client_id=client_id,
                elapsed_seconds=time.perf_counter() - started, error=exc,
            )
            # AccessDenied 说的是**本服务绑定**的那个语料，调用方本来就在跟它打交道，
            # 原样回；策略快照的异常带着别的语料身份，只能给通用说明。
            detail = (
                policy_unavailable(exc, tool="search_docs")
                if isinstance(exc, AccessSnapshotError) else str(exc)
            )
            return forbidden(detail, corpus_id=corpus_id, query=query)

        try:
            if active_policy.reranker_enabled != (runtime.reranker is not None):
                raise RuntimeError("共享检索配置与默认reranker实例状态不一致")
            result = runtime.search(
                query,
                config=active_policy.single_config(
                    version, top_k, context_char_budget=context_char_budget
                ),
            )
            # 兼容单库runtime的身份来自启动时已准入的RuntimeIdentity；请求参数不能
            # 覆盖它。给证据补上同一target，和共享路由入口保持一致的可审计身份。
            if active_target_id is not None:
                result["index_target_id"] = active_target_id
                for evidence_item in result.get("evidence", []):
                    evidence_item["index_target_id"] = active_target_id
            result["retrieval_config"] = active_policy.public_config(
                top_k, context_char_budget=context_char_budget
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

        身份有问题的目录**不在本接口输出**：它们不进 `corpora`，也不能被检索或取件；
        到底是哪些目录、为什么被隔离，只进服务端日志和本地CLI。按ACL过滤在这里救不了场
        ——隔离目录没有策略，`registry.check` 必然 default-deny，过滤后永远是空的，
        反而给出"一切正常"的假信号。
        """
        client_id = effective_client_id(ctx)
        visible_libraries = authorized_library_ids(ctx) if identity_store is not None else None
        registry = None
        if identity_store is None:
            try:
                registry = current_access()
            except AccessSnapshotError as exc:
                return {"corpora": [], "count": 0, "error": "unavailable",
                        "detail": policy_unavailable(exc, tool="list_corpora"),
                        "mcp_protocol": EXPECTED_PROTOCOL_VERSION}
        report = corpus_catalog_report(root)
        log_identity_problems(report)
        request_release = current_release_request(ctx)
        entry_by_corpus = {entry.corpus_id: entry for entry in report.entries}
        release_by_corpus = {}
        if identity_store is not None and request_release is not None:
            # MCP readers see only targets present in this request's active router;
            # old manifests for a migrated library are not historical browse access.
            active_corpus_ids = {
                index.corpus_identity
                for index in (request_release.router.indices if request_release.router else [])
            }
            entry_by_corpus = {
                corpus_id: entry for corpus_id, entry in entry_by_corpus.items()
                if corpus_id in active_corpus_ids
            }
        if request_release is not None:
            for library_state in request_release.catalog_snapshot["libraries"].values():
                record = library_state.get("active")
                if record is None:
                    continue
                target = IndexTarget.from_dict(record["target"])
                if identity_store is not None:
                    allowed = normalize_library(target.library) in visible_libraries
                else:
                    allowed = registry.check(target.corpus_identity(), client_id)
                if not allowed:
                    continue
                old_entry = entry_by_corpus.get(target.corpus_identity())
                entry = release_corpus_entry(root, record, existing=old_entry)
                entry_by_corpus[entry.corpus_id] = entry
                release_by_corpus[entry.corpus_id] = record
        entries = []
        for entry in entry_by_corpus.values():
            if identity_store is not None:
                allowed = normalize_library(entry.library) in visible_libraries
            else:
                allowed = registry.check(entry.corpus_id, client_id)
            if not allowed:
                continue
            payload = entry.to_dict(include_files=include_files)
            release_record = release_by_corpus.get(entry.corpus_id)
            if release_record is None:
                payload.update(_index_state(entry, ctx=ctx))
            else:
                try:
                    index = request_release.router.resolve_target(
                        str(release_record["target"]["target_id"])
                    )
                    current_release_resolver(ctx).materialize_indices([index])
                    payload.update({
                        "index_state": "usable",
                        "index_usable_targets": [index.target_id],
                        "index_failures": [],
                    })
                except Exception:
                    payload.update({
                        "index_state": "unusable",
                        "index_usable_targets": [],
                        "index_failures": [{
                            "target_id": release_record["target"]["target_id"],
                            "reason": "release_validation_failed",
                        }],
                    })
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
                "Directories quarantined for an unresolvable corpus identity are NOT "
                "reported here (naming them would leak directory names past the access "
                "check): they are recorded in the server log and listed by "
                "scripts/manage_access.py --list. If a corpus you expect is missing, ask "
                "an operator to check for quarantined directories. "
                "Retrieval scores are ranking signals, not answer confidence."
            ),
            "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
        }


    @mcp.tool(
        name="list_managed_files",
        description=(
            "List active uploaded managed files this client may read. Returns stable file_id, "
            "active version and chunk count. Use file_id with search_docs to search one file."
        ),
        run_in_thread=True,
    )
    def list_managed_files(ctx: Context | None = None) -> dict:
        """只列active且通过managed-file ACL授权的文件，不包含legacy或待处理任务。"""
        client_id = effective_client_id(ctx)
        bridge = current_managed_bridge(ctx)
        if bridge is None:
            return {
                "files": [], "count": 0, "error": "unavailable",
                "detail": "managed文件服务暂不可用",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
        try:
            if identity_store is not None:
                files = bridge.list_files(authorization=request_bridge_authorization(ctx))
            else:
                registry = current_access()
                files = visible_managed_files(bridge, registry, client_id)
        except AccessSnapshotError as exc:
            logging.getLogger(__name__).error(
                "managed文件访问策略不可用：%s", type(exc).__name__
            )
            return {
                "files": [], "count": 0, "error": "unavailable",
                "detail": "managed文件访问策略暂不可用",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
        except ManagedFileServiceError:
            return {
                "files": [], "count": 0, "error": "unavailable",
                "detail": "managed文件服务暂不可用",
                "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
            }
        return {
            "files": [item.to_dict() for item in files],
            "count": len(files),
            "mcp_protocol": EXPECTED_PROTOCOL_VERSION,
        }

    return current_access, sources_for
