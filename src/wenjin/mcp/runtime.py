"""建立并释放共享检索服务、索引身份与多语料路由。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wenjin.index_registry import (
    IndexRegistryStore,
    IndexTarget,
    resolve_corpus_identity,
    resolve_project_path,
    target_raw_data_root,
)
from wenjin.indexing.zh import prewarm as prewarm_zh
from wenjin.releases.catalog import ReleaseCatalog
from wenjin.releases.resolver import LazyRequestReleaseResolver, release_catalog_path
from wenjin.releases.service import ReleaseService
from wenjin.retrieval.service import RetrievalService, load_source_urls
from wenjin.serving import CorpusSources

ROOT = Path(__file__).resolve().parents[3]
EXPECTED_PROTOCOL_VERSION = "2025-11-25"


@dataclass(frozen=True)
class RuntimeIdentity:
    """服务启动时确定的索引身份；请求期间不再读注册表。"""

    corpus_id: str
    corpus_id_source: str
    target_id: str
    embedding_model: str | None = None
    model_provider: str = "local"
    model_identity: dict[str, Any] | None = None


def create_runtime(
    *,
    use_reranker: bool = True,
    root: Path | None = None,
    settings=None,
    read_only_release_catalog: bool = False,
) -> tuple[RetrievalService, RuntimeIdentity]:
    """加载Qdrant客户端和两个本地0.6B模型，并返回绑定在一起的索引身份。

    注册表在这里读一次，得到的`IndexTarget`同时用于索引校验、服务构造和授权身份——
    三者出自同一次读取，不会出现"检查的语料"与"实际检索的索引"不一致。

    **MCP服务用这个函数**；只需要service的调用方用`create_default_service()`。
    """
    from wenjin.index_audit import admit_index
    from wenjin.qdrant_connection import create_qdrant_client
    from wenjin.retrieval.lazy_reranker import build_shared_reranker
    from wenjin.retrieval.search_policy import SearchPolicy
    from wenjin.retrieval.source_context import (
        VerifiedSourceContextCache,
        reviewed_profile_for_target,
    )
    from wenjin.settings import Settings

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    project_root = Path(root or ROOT).resolve()
    settings = settings or Settings.for_project_root(project_root)
    policy = SearchPolicy.from_settings(settings)
    if not use_reranker:
        from dataclasses import replace

        policy = replace(policy, reranker_enabled=False)
    # 中文分词器要在启动时预热：jieba 建前缀词典约 0.5 s，落在请求路径上就是首个
    # 检索请求凭空慢半秒；而服务用线程池跑工具，两个请求同时首次初始化还会重复建词典
    # （prewarm 内部已加锁，这里在装配阶段单线程调一次更省事）。
    prewarm_zh()
    catalog_path = release_catalog_path(project_root)
    catalog = (
        ReleaseCatalog(catalog_path, read_only=read_only_release_catalog)
        if catalog_path.is_file()
        else None
    )
    catalog_snapshot = catalog.snapshot_all() if catalog is not None else None
    active_release = None
    if catalog_snapshot is not None:
        active_release = next(
            (
                library["active"]
                for library in catalog_snapshot["libraries"].values()
                if library.get("active") is not None
                and str(library["active"].get("target", {}).get("library", "")).strip().lower()
                == "osg"
            ),
            None,
        )
    target = (
        IndexTarget.from_dict(active_release["target"])
        if active_release is not None
        else IndexRegistryStore(project_root / "data/indexes/index-registry.json").active()
    )
    actual_embedding_model = settings.resolved_embedding_model_id
    identity = RuntimeIdentity(
        corpus_id=resolve_corpus_identity(target, root=project_root).corpus_id,
        corpus_id_source="release_catalog" if active_release is not None else "registry",
        target_id=target.target_id,
        embedding_model=actual_embedding_model,
    )
    if actual_embedding_model != target.embedding_model:
        raise RuntimeError(
            f"当前Embedding模型{actual_embedding_model}与索引{target.embedding_model}不一致"
        )
    client = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    reranker = None
    model_bindings = None
    try:
        # 启动也走完整准入：缺页、越界位置、非白名单文件在启动时就要拦住。
        if active_release is not None:
            ReleaseService(project_root, catalog, client).validate(active_release["release_id"])
        else:
            admit_index(target, root=project_root, qdrant_client=client)
        if getattr(settings, "model_provider", "local") == "remote":
            from wenjin.models.providers import build_remote_model_bindings

            model_bindings = build_remote_model_bindings(settings, root=project_root)
            embedder = model_bindings.embedder
            reranker = model_bindings.reranker if use_reranker else None
            identity = RuntimeIdentity(
                corpus_id=identity.corpus_id,
                corpus_id_source=identity.corpus_id_source,
                target_id=identity.target_id,
                embedding_model=identity.embedding_model,
                model_provider="remote",
                model_identity=model_bindings.identity,
            )
        else:
            embedder = settings.embedding(root=project_root)
            reranker = build_shared_reranker(settings, root=project_root) if use_reranker else None
        source_context_cache = None
        context_profile = reviewed_profile_for_target(target)
        if context_profile:
            source_context_cache = VerifiedSourceContextCache(
                CorpusSources.from_manifest(
                    resolve_project_path(project_root, target.manifest_path),
                    raw_root=target_raw_data_root(project_root, target),
                    expected_corpus_id=target.corpus_identity(),
                ),
                profile=context_profile,
            )
        service = RetrievalService(
            qdrant_client=client,
            embedder=embedder,
            reranker=reranker,
            bm25_path=resolve_project_path(project_root, target.bm25_path),
            source_urls=load_source_urls(resolve_project_path(project_root, target.manifest_path)),
            collection_name=target.dense_collection,
            supported_version=target.version,
            index_target_id=target.target_id,
            source_context_cache=source_context_cache,
        )
        service.search_policy = policy
        service.model_bindings = model_bindings
        return service, identity
    except Exception:
        if reranker is not None:
            reranker.close()
        if model_bindings is not None:
            model_bindings.close()
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
    search_lock: Any | None = None,
    read_only_release_catalog: bool = False,
) -> Any | None:
    """为多语料候选建路由；没配置候选清单时返回 None。

    **复用调用方已有的 Qdrant 客户端与两个模型**：`create_runtime` 已经加载过它们，
    这里再建一份会让显存和启动时间都翻倍。

    建路由会对每个候选跑**完整准入**（实测 5 个候选约 2.8 秒）。放在启动而不是
    第一次调用：坏资产要在启动时就拦住，而不是等某个客户端恰好查到那个库才炸。

    `reranker` 与 `RetrievalService` 共用同一个实例，因此本函数返回的路由
    **不能**再 `close()`——释放由 service 负责，重复关闭会把共用的模型一起关掉。
    """
    from wenjin.retrieval.router import DEFAULT_MULTI_CORPUS_ROUTE, MultiCorpusRouter

    catalog_path = release_catalog_path(root)
    migrated_libraries: set[str] = set()
    if catalog_path.is_file():
        snapshot = ReleaseCatalog(catalog_path, read_only=read_only_release_catalog).snapshot_all()
        from wenjin.retrieval.router import normalize_library

        migrated_libraries = {
            normalize_library(str(library["active"].get("target", {}).get("library", "")))
            for library in snapshot["libraries"].values()
            if library.get("active") is not None
        }
    from wenjin.retrieval.router import normalize_library

    targets: list[IndexTarget] = []
    route_path = root / DEFAULT_MULTI_CORPUS_ROUTE
    if route_path.is_file():
        try:
            route = json.loads(route_path.read_text(encoding="utf-8-sig"))
            if route.get("schema_version") != 1 or not isinstance(route.get("candidates"), list):
                raise ValueError("默认多库route配置无效")
            targets.extend(
                IndexTarget.from_dict(item)
                for item in route["candidates"]
                if normalize_library(str(item.get("library", ""))) not in migrated_libraries
            )
        except (OSError, ValueError, json.JSONDecodeError):
            if not migrated_libraries:
                raise

    # 默认OSG来自registry/单库service，不在五库route003中；catalog接管OSG后不再读取旧条目。
    if "osg" not in migrated_libraries:
        registry_path = root / "data/indexes/index-registry.json"
        if registry_path.is_file():
            default_target = IndexRegistryStore(registry_path).active()
            if normalize_library(default_target.library) not in migrated_libraries:
                targets.append(default_target)
    if not targets:
        return None
    return MultiCorpusRouter.from_targets(
        targets,
        root=root,
        qdrant_client=qdrant_client,
        embedder=embedder,
        reranker=reranker,
        search_lock=search_lock,
    )


def create_release_resolver(
    *,
    root: Path,
    qdrant_client: Any,
    embedder: Any,
    reranker: Any | None,
    legacy_router: Any | None,
    search_lock: Any,
    expected_embedding_model: str | None = None,
    read_only_release_catalog: bool = False,
) -> LazyRequestReleaseResolver:
    """为MCP请求共享当前SQLite release目录，不创建第二套模型或Qdrant客户端。"""
    return LazyRequestReleaseResolver(
        root=root,
        qdrant_client=qdrant_client,
        embedder=embedder,
        reranker=reranker,
        legacy_router=legacy_router,
        search_lock=search_lock,
        expected_embedding_model=expected_embedding_model,
        read_only_release_catalog=read_only_release_catalog,
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
