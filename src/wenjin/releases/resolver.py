"""每个检索请求读取一次active release快照，并固定其索引/原文视图。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any

from wenjin.catalog import CorpusEntry
from wenjin.index_audit import admit_index
from wenjin.index_fingerprint import file_sha256, qdrant_fingerprint
from wenjin.index_registry import IndexTarget, resolve_project_path, target_raw_data_root
from wenjin.manifest import LEGACY_SCOPE_FIELD, RETRIEVAL_SCOPE_FIELD, scope_files
from wenjin.releases.catalog import ReleaseCatalog
from wenjin.releases.service import ReleaseService
from wenjin.retrieval.router import MultiCorpusRouter, RoutedIndex, normalize_library
from wenjin.retrieval.service import load_source_urls
from wenjin.retrieval.source_context import (
    VerifiedSourceContextCache,
    sealed_profile_for_target,
)
from wenjin.serving import CorpusSources


def release_catalog_path(root: Path) -> Path:
    """共享catalog路径；HTTP/MCP进程通过同一SQLite文件观察active指针。"""
    return Path(root) / "data" / "releases" / "release-catalog.sqlite"


def release_corpus_entry(
    root: Path, record: dict[str, Any], *, existing: CorpusEntry | None = None
) -> CorpusEntry:
    """从sealed release manifest构造目录项，原legacy目录失效时仍可服务。"""
    target = IndexTarget.from_dict(record["target"])
    manifest_path = resolve_project_path(root, target.manifest_path)
    import json

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    files = manifest.get("files", [])
    scope = tuple(scope_files(manifest))
    uses_retrieval = any(RETRIEVAL_SCOPE_FIELD in item for item in files)
    source_manifest = str(record.get("source_manifest_path", ""))
    visibility = (
        existing.visibility if existing is not None
        else "private" if source_manifest.startswith("corpora-private/")
        else "public"
    )
    return CorpusEntry(
        corpus_id=target.corpus_identity(),
        library=str(manifest.get("library", target.library)),
        version=str(manifest.get("version", target.version)),
        visibility=visibility,
        license=str(manifest.get("license", existing.license if existing else "unknown")),
        source_url=manifest.get("source_url", existing.source_url if existing else None),
        raw_data_subdir=str(manifest.get("raw_data_subdir", "")),
        files=len(files),
        retrieval_eligible_files=len(scope),
        retrieval_scope_files=scope,
        manifest_path=target.manifest_path,
        index_targets=(target.target_id,),
        scope_field=RETRIEVAL_SCOPE_FIELD if uses_retrieval else LEGACY_SCOPE_FIELD,
    )


@dataclass(frozen=True)
class RequestReleaseSnapshot:
    """单个请求持有的active目录快照及按该快照构造的路由视图。"""

    revision: int
    catalog_snapshot: dict[str, Any]
    router: MultiCorpusRouter | None
    default_library: str | None
    default_target_id: str | None


class PinnedEvaluationRouter(MultiCorpusRouter):
    """评测只关闭自己的路由视图，不关闭HTTP/MCP共享模型或Qdrant client。"""

    def __init__(self, *, run_identity: dict[str, Any], root: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.run_identity = run_identity
        self._fingerprint_root = Path(root).resolve()

    def close(self) -> None:
        # embedder、reranker、Qdrant client跟随app生命周期，不能由评测context二次关闭。
        return None

    def verify_run_identity(self) -> dict[str, Any]:
        return evaluation_asset_fingerprints(
            self.indices, root=self._fingerprint_root, qdrant_client=self.qdrant_client
        )


def _identity_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluation_asset_fingerprints(
    indices: list[RoutedIndex], *, root: Path, qdrant_client: Any
) -> dict[str, Any]:
    """重准入并绑定每个target的来源、BM25和dense资产指纹。"""
    assets: dict[str, Any] = {}
    root = Path(root).resolve()
    for index in indices:
        target = index.target
        # admit_index会重新核对manifest登记原文、BM25记录、Qdrant payload/vector。
        admission = admit_index(target, root=root, qdrant_client=qdrant_client)
        manifest_path = resolve_project_path(root, target.manifest_path)
        bm25_path = resolve_project_path(root, target.bm25_path)
        source_entries = []
        if index.sources is not None:
            source_entries = [
                {"path": record.relative, "raw_sha256": record.raw_sha256,
                 "canonical_sha256": record.canonical_sha256, "size": record.size}
                for record in sorted(index.sources.records.values(), key=lambda item: item.relative)
            ]
        assets[target.target_id] = {
            "library": target.library,
            "version": target.version,
            "release_id": index.release_id,
            "admission_sha256": _identity_digest(admission),
            "manifest_sha256": file_sha256(manifest_path),
            "raw_sources_sha256": _identity_digest(source_entries),
            "bm25_sha256": file_sha256(bm25_path),
            "dense_sha256": qdrant_fingerprint(qdrant_client, target.dense_collection),
        }
    return assets


def build_pinned_evaluation_router(
    *,
    resolver: Any,
    request_snapshot: RequestReleaseSnapshot,
    rows: list[dict[str, Any]],
    qdrant_client: Any,
    embedder: Any,
    reranker: Any | None,
    search_lock: Any,
    expected_embedding_model: str | None,
    profiles: tuple[str, ...],
) -> tuple[PinnedEvaluationRouter, dict[str, Any]]:
    """从单一request snapshot只装配题集涉及的targets并绑定可恢复身份。"""
    if request_snapshot.router is None:
        raise ValueError("评测快照没有可用索引")
    selected: dict[str, RoutedIndex] = {}
    for row in rows:
        library = normalize_library(str(row.get("library", "")))
        version = str(row.get("version", ""))
        if not library or not version:
            raise ValueError("评测题缺少library/version")
        matches = [
            index for index in request_snapshot.router.indices
            if index.library == library and index.version == version
        ]
        target_id = row.get("target_id")
        if target_id:
            matches = [index for index in matches if index.target_id == target_id]
        elif len(matches) > 1 and row.get("source_manifest_sha256"):
            matches = [index for index in matches
                       if index.target.manifest_sha256 == row["source_manifest_sha256"]]
        if len(matches) != 1:
            raise ValueError(f"评测题未唯一匹配索引目标：{library}/{version}")
        selected[matches[0].target_id] = matches[0]
    indices = list(selected.values())
    # route/registry在evaluation resolver中只作为target目录读取，目标相关校验延迟到此处，
    # 避免一个无关library损坏阻断当前模板。
    legacy = [index for index in indices if index.release_id is None]
    if legacy:
        from wenjin.retrieval.source_context import (
            VerifiedSourceContextCache,
            reviewed_profile_for_target,
        )
        from wenjin.serving import CorpusSources

        validated = []
        for index in legacy:
            target = index.target
            admit_index(target, root=resolver.root, qdrant_client=qdrant_client)
            manifest_path = resolve_project_path(resolver.root, target.manifest_path)
            sources = CorpusSources.from_manifest(
                manifest_path,
                raw_root=target_raw_data_root(resolver.root, target),
                expected_corpus_id=target.corpus_identity(),
            )
            profile = reviewed_profile_for_target(target)
            validated.append(replace(
                index,
                bm25_path=resolve_project_path(resolver.root, target.bm25_path),
                source_urls=load_source_urls(manifest_path),
                sources=sources,
                source_context_cache=(
                    VerifiedSourceContextCache(sources, profile=profile) if profile else None
                ),
            ))
        by_target = {index.target_id: index for index in validated}
        indices = [by_target.get(index.target_id, index) for index in indices]
    materialize = getattr(resolver, "materialize_indices", None)
    release_indices = [index for index in indices if index.release_id is not None]
    if callable(materialize) and release_indices:
        materialized = materialize(release_indices)
        by_target = {index.target_id: index for index in materialized}
        indices = [by_target.get(index.target_id, index) for index in indices]
    if any(index.target.embedding_model != expected_embedding_model for index in indices):
        raise ValueError("评测target声明的Embedding模型与manager不一致")
    assets = evaluation_asset_fingerprints(indices, root=resolver.root, qdrant_client=qdrant_client)
    retrieval_dir = Path(__file__).resolve().parents[1] / "retrieval"
    code_sha = _identity_digest({
        "resolver_sha256": file_sha256(Path(__file__).resolve()),
        "router_sha256": file_sha256(retrieval_dir / "router.py"),
        "strategy_sha256": file_sha256(retrieval_dir / "strategy_experiment.py"),
    })
    from wenjin.retrieval.strategy_experiment import case_identity_bindings, run_identity_sha256

    identity = {
        "schema_version": 1,
        "snapshot_revision": request_snapshot.revision,
        "profiles": list(profiles),
        "embedding_model": expected_embedding_model,
        "code_sha256": code_sha,
        "case_identities": case_identity_bindings(rows),
        "assets": assets,
    }
    identity["run_identity_sha256"] = run_identity_sha256(identity)
    router = PinnedEvaluationRouter(
        root=resolver.root, run_identity=identity,
        qdrant_client=qdrant_client, embedder=embedder,
        indices=indices, reranker=reranker, search_lock=search_lock,
    )
    return router, identity


def create_evaluation_release_resolver(
    *, root: Path, qdrant_client: Any, embedder: Any, reranker: Any | None,
    search_lock: Any, expected_embedding_model: str | None,
) -> LazyRequestReleaseResolver:
    """建立不预准入全route的lazy provider；仅题集选中的legacy target会完整校验。"""
    from wenjin.index_registry import IndexRegistryStore
    from wenjin.retrieval.router import DEFAULT_MULTI_CORPUS_ROUTE

    root = Path(root).resolve()
    targets: list[IndexTarget] = []
    route_path = root / DEFAULT_MULTI_CORPUS_ROUTE
    if route_path.is_file():
        route = json.loads(route_path.read_text(encoding="utf-8-sig"))
        if route.get("schema_version") != 1 or not isinstance(route.get("candidates"), list):
            raise ValueError("评测route配置无效")
        targets.extend(IndexTarget.from_dict(item) for item in route["candidates"])
    registry_path = root / "data" / "indexes" / "index-registry.json"
    if registry_path.is_file():
        default = IndexRegistryStore(registry_path).active()
        if all(target.target_id != default.target_id for target in targets):
            targets.append(default)
    # 仅构建不读取manifest、BM25和Qdrant的轻量target目录。
    legacy_indices = [
        RoutedIndex(
            target=target, collection=target.dense_collection,
            bm25_path=resolve_project_path(root, target.bm25_path),
            source_urls={}, sources=None,
        )
        for target in targets
    ]
    legacy_router = MultiCorpusRouter(
        qdrant_client=qdrant_client, embedder=embedder, indices=legacy_indices,
        reranker=reranker, search_lock=search_lock,
    ) if legacy_indices else None
    return LazyRequestReleaseResolver(
        root=root,
        qdrant_client=qdrant_client,
        embedder=embedder,
        reranker=reranker,
        legacy_router=legacy_router,
        search_lock=search_lock,
        expected_embedding_model=expected_embedding_model,
    )


class RequestReleaseResolver:
    """在请求开始读取CAS目录；runtime仅按不可变release_id缓存。"""

    def __init__(
        self,
        *,
        root: Path,
        catalog: ReleaseCatalog,
        release_service: ReleaseService,
        qdrant_client: Any,
        embedder: Any,
        reranker: Any | None,
        legacy_router: MultiCorpusRouter | None,
        search_lock: Any,
        expected_embedding_model: str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.catalog = catalog
        self.release_service = release_service
        self.qdrant_client = qdrant_client
        self.embedder = embedder
        self.reranker = reranker
        self.legacy_router = legacy_router
        self.search_lock = search_lock
        self.expected_embedding_model = expected_embedding_model
        self._release_indices: dict[str, RoutedIndex] = {}
        self._validated_releases: set[str] = set()
        self._cache_lock = RLock()

    @staticmethod
    def _active_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            library["active"]
            for library in snapshot["libraries"].values()
            if library.get("active") is not None
        ]

    def _release_stub(self, record: dict[str, Any]) -> RoutedIndex:
        release_id = str(record["release_id"])
        target = IndexTarget.from_dict(record["target"])
        return RoutedIndex(
            target=target,
            collection=target.dense_collection,
            bm25_path=resolve_project_path(self.root, target.bm25_path),
            source_urls={},
            sources=None,
            release_id=release_id,
        )

    def materialize_indices(self, indices: list[RoutedIndex]) -> list[RoutedIndex]:
        """授权和目标选择后才读取目标release的manifest、BM25、原文root。"""
        output = []
        with self._cache_lock:
            for index in indices:
                release_id = index.release_id
                if release_id is None:
                    output.append(index)
                    continue
                cached = self._release_indices.get(release_id)
                if cached is None:
                    try:
                        self.release_service.validate(release_id)
                        target = index.target
                        if (
                            not self.expected_embedding_model
                            or target.embedding_model != self.expected_embedding_model
                        ):
                            raise ValueError("release Embedding模型与当前服务不一致")
                        record = self.catalog.get_release(release_id)
                        manifest_path = resolve_project_path(self.root, target.manifest_path)
                        sources = CorpusSources.from_manifest(
                            manifest_path,
                            raw_root=target_raw_data_root(self.root, target),
                            expected_corpus_id=target.corpus_identity(),
                        )
                        profile = sealed_profile_for_target(
                            target, record.get("serving_profile")
                        )
                        cached = replace(
                            index,
                            source_urls=load_source_urls(manifest_path),
                            sources=sources,
                            source_context_cache=(
                                VerifiedSourceContextCache(sources, profile=profile)
                                if profile else None
                            ),
                        )
                    except Exception as error:
                        from wenjin.releases.service import ReleaseValidationError

                        if isinstance(error, ReleaseValidationError):
                            raise
                        raise ReleaseValidationError(
                            release_id,
                            f"active release无法构造请求视图：{type(error).__name__}",
                        ) from error
                    self._release_indices[release_id] = cached
                    self._validated_releases.add(release_id)
                output.append(cached)
        return output

    def validate_indices(self, indices: list[RoutedIndex]) -> None:
        """授权和目标解析后，首次使用每个immutable release时校验完整封存指纹。"""
        with self._cache_lock:
            for index in indices:
                release_id = index.release_id
                if release_id is None or release_id in self._validated_releases:
                    continue
                self.release_service.validate(release_id)
                self._validated_releases.add(release_id)

    def begin_request(self) -> RequestReleaseSnapshot:
        """原子读取revision及所有active指针，并据此固定本次路由集合。"""
        snapshot = self.catalog.snapshot_all()
        active = self._active_records(snapshot)
        migrated_libraries = {
            normalize_library(str(record["target"]["library"])) for record in active
        }
        migrated_libraries.update(
            normalize_library(str(library_id))
            for library_id, library in snapshot["libraries"].items()
            if library.get("active") is None and library.get("ever_activated")
        )
        indices = [self._release_stub(record) for record in active]
        if self.legacy_router is not None:
            indices.extend(
                index for index in self.legacy_router.indices
                if index.library not in migrated_libraries
            )

        router = None
        if indices:
            router = MultiCorpusRouter(
                qdrant_client=self.qdrant_client,
                embedder=self.embedder,
                indices=indices,
                reranker=self.reranker,
                search_lock=self.search_lock,
            )

        default_library = None
        default_target_id = None
        osg = next((item for item in indices if item.library == "osg"), None)
        if osg is not None:
            default_library = osg.library
            default_target_id = osg.target_id
        return RequestReleaseSnapshot(
            revision=int(snapshot["revision"]),
            catalog_snapshot=snapshot,
            router=router,
            default_library=default_library,
            default_target_id=default_target_id,
        )

    @staticmethod
    def resolve(
        request: RequestReleaseSnapshot,
        *,
        library: str | None = None,
        version: str | None = None,
        target_id: str | None = None,
        default_library: str | None = None,
    ) -> RoutedIndex:
        """仅解析本次快照中的active release和未迁移legacy目标。"""
        router = request.router
        if router is None:
            raise ValueError("没有可检索的active资料库")
        if target_id is not None:
            if library is not None or version is not None:
                raise ValueError("target_id不能与library/version同时使用")
            return router.resolve_target(target_id)
        selected_library = library
        if selected_library is None and (version is None or default_library is not None):
            selected_library = default_library or request.default_library
        matches = router.resolve(selected_library, version)
        if len(matches) != 1:
            raise ValueError("该请求没有唯一的active索引目标")
        return matches[0]

    def sources_for(
        self, corpus_id: str, *, release_id: str | None = None
    ) -> CorpusSources:
        """release下载链接固定到对应sealed manifest/raw snapshot。"""
        if release_id:
            record = self.catalog.get_release(release_id)
            if record.get("status") not in {"active", "retained"}:
                raise ValueError("release不是可读取的active/retained版本")
            target = IndexTarget.from_dict(record["target"])
            if target.corpus_identity() != corpus_id:
                raise ValueError("release与请求corpus_id不匹配")
            manifest_path = resolve_project_path(self.root, target.manifest_path)
            return CorpusSources.from_manifest(
                manifest_path,
                raw_root=target_raw_data_root(self.root, target),
                expected_corpus_id=corpus_id,
            )

        from wenjin.catalog import find_corpus

        entry = find_corpus(self.root, corpus_id)
        if entry is None:
            raise ValueError(f"语料不存在：{corpus_id}")
        return CorpusSources.from_manifest(
            resolve_project_path(self.root, entry.manifest_path),
            raw_root=self.root / "raw_data",
            expected_corpus_id=corpus_id,
        )


class LazyRequestReleaseResolver:
    """服务先于首个发布启动时，下一次请求无需重启即可发现新catalog。"""

    def __init__(self, *, root: Path, **runtime: Any) -> None:
        self.root = Path(root).resolve()
        self.catalog_path = release_catalog_path(self.root)
        self.runtime = runtime
        self._delegate: RequestReleaseResolver | None = None
        self._lock = RLock()
        self._catalog_seen = False

    def _current(self) -> RequestReleaseResolver | None:
        if not self.catalog_path.is_file():
            if self._catalog_seen:
                raise RuntimeError("release catalog在运行期间丢失，拒绝回退到legacy索引")
            return None
        with self._lock:
            if self._delegate is None:
                catalog = ReleaseCatalog(self.catalog_path)
                self._delegate = RequestReleaseResolver(
                    root=self.root,
                    catalog=catalog,
                    release_service=ReleaseService(
                        self.root, catalog, self.runtime["qdrant_client"]
                    ),
                    **self.runtime,
                )
            self._catalog_seen = True
            return self._delegate

    def begin_request(self) -> RequestReleaseSnapshot:
        delegate = self._current()
        if delegate is not None:
            return delegate.begin_request()
        legacy_router = self.runtime["legacy_router"]
        osg = next(
            (item for item in legacy_router.indices if item.library == "osg"),
            None,
        ) if legacy_router is not None else None
        return RequestReleaseSnapshot(
            revision=0,
            catalog_snapshot={"revision": 0, "libraries": {}},
            router=legacy_router,
            default_library=osg.library if osg else None,
            default_target_id=osg.target_id if osg else None,
        )

    def materialize_indices(self, indices: list[RoutedIndex]) -> list[RoutedIndex]:
        delegate = self._current()
        return delegate.materialize_indices(indices) if delegate is not None else indices

    def sources_for(
        self, corpus_id: str, *, release_id: str | None = None
    ) -> CorpusSources:
        delegate = self._current()
        if delegate is None:
            if release_id:
                raise ValueError("release catalog当前不可用")
            from wenjin.catalog import find_corpus

            entry = find_corpus(self.root, corpus_id)
            if entry is None:
                raise ValueError(f"语料不存在：{corpus_id}")
            return CorpusSources.from_manifest(
                resolve_project_path(self.root, entry.manifest_path),
                raw_root=self.root / "raw_data",
                expected_corpus_id=corpus_id,
            )
        return delegate.sources_for(corpus_id, release_id=release_id)
