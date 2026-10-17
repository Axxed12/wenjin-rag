"""共享资料库管理目录与只读检索。

此处只读取现有manifest和已声明为当前来源的索引目标；不会导入、adopt或修改共享资产。
检索前重做完整准入，目录与检索都使用同一份IndexTarget绑定manifest/BM25/Qdrant。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from threading import Lock

from wenjin.catalog import CorpusEntry, corpus_catalog_report
from wenjin.index_audit import admit_index
from wenjin.index_fingerprint import (
    canonical_json as _canonical_json,
)
from wenjin.index_fingerprint import (
    file_sha256 as _file_sha256,
)
from wenjin.index_fingerprint import (
    qdrant_fingerprint as _qdrant_fingerprint,
)
from wenjin.index_registry import (
    IndexRegistryStore,
    IndexTarget,
    resolve_project_path,
    target_raw_data_root,
)
from wenjin.manifest import file_records, scope_files
from wenjin.manifest import load as load_manifest
from wenjin.releases.catalog import ReleaseCatalog
from wenjin.releases.resolver import release_catalog_path
from wenjin.retrieval.errors import StaleSnapshotError
from wenjin.retrieval.router import (
    MultiCorpusRouter,
    RoutedIndex,
    normalize_library,
)
from wenjin.retrieval.search_policy import SearchPolicy
from wenjin.retrieval.service import load_source_urls
from wenjin.retrieval.source_context import (
    VerifiedSourceContextCache,
    reviewed_profile_for_target,
    sealed_profile_for_target,
)
from wenjin.serving import CorpusSources, VerifiedSource

_DEFAULT_ROUTE = Path("configs/indexes/multicorpus-candidates-003.json")
_REGISTRY = Path("data/indexes/index-registry.json")
_SNAPSHOT_CACHE_SECONDS = 30.0


class LegacySearchService:
    """管理员调用的共享资料只读目录/检索服务。"""

    def __init__(
        self,
        root: Path,
        qdrant_client,
        embedder,
        *,
        search_lock=None,
        expected_embedding_model: str | None = None,
        reranker=None,
        search_policy: SearchPolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.qdrant_client = qdrant_client
        self.embedder = embedder
        self.expected_embedding_model = expected_embedding_model
        self.reranker = reranker
        self.search_policy = search_policy or SearchPolicy(
            reranker_enabled=reranker is not None
        )
        if self.search_policy.reranker_enabled != (self.reranker is not None):
            raise ValueError("shared search policy与reranker实例启用状态不一致")
        self._cache: dict[str, tuple[float, str, dict]] = {}
        self._cache_lock = Lock()
        self._release_catalog_seen = False
        # 管理检索和受管检索共享单模型锁，避免并发调用同一个GPU embedder。
        self._search_lock = search_lock or Lock()

    def _release_snapshot(self) -> dict:
        path = release_catalog_path(self.root)
        if not path.is_file():
            if self._release_catalog_seen:
                raise StaleSnapshotError("release catalog运行期间丢失，拒绝回退到legacy索引")
            return {"revision": 0, "libraries": {}}
        snapshot = ReleaseCatalog(path).snapshot_all()
        self._release_catalog_seen = True
        return snapshot

    def _corpus_entries(self, release_snapshot: dict | None = None) -> list[CorpusEntry]:
        """以active release target覆盖旧目录视图；必要时从sealed manifest补目录项。"""
        entries = {entry.corpus_id: entry for entry in corpus_catalog_report(self.root).entries}
        snapshot = release_snapshot if release_snapshot is not None else self._release_snapshot()
        unavailable_libraries = {
            normalize_library(str(library_id))
            for library_id, library in snapshot["libraries"].items()
            if library.get("active") is None and library.get("ever_activated")
        }
        entries = {
            corpus_id: entry for corpus_id, entry in entries.items()
            if normalize_library(entry.library) not in unavailable_libraries
        }
        for library in snapshot["libraries"].values():
            record = library.get("active")
            if record is None:
                continue
            target = IndexTarget.from_dict(record["target"])
            manifest_path = resolve_project_path(self.root, target.manifest_path)
            manifest = load_manifest(manifest_path)
            in_scope = tuple(scope_files(manifest))
            files = manifest.get("files", [])
            uses_retrieval = any("retrieval_eligible" in item for item in files)
            old = entries.get(target.corpus_identity())
            source_manifest = str(record.get("source_manifest_path", ""))
            visibility = (
                old.visibility if old is not None
                else "private" if source_manifest.startswith("corpora-private/")
                else "public"
            )
            entries[target.corpus_identity()] = CorpusEntry(
                corpus_id=target.corpus_identity(),
                library=str(manifest.get("library", target.library)),
                version=str(manifest.get("version", target.version)),
                visibility=visibility,
                license=str(manifest.get("license", old.license if old else "unknown")),
                source_url=manifest.get("source_url", old.source_url if old else None),
                raw_data_subdir=str(manifest.get("raw_data_subdir", "")),
                files=len(files),
                retrieval_eligible_files=len(in_scope),
                retrieval_scope_files=in_scope,
                manifest_path=target.manifest_path,
                index_targets=(target.target_id,),
                scope_field=("retrieval_eligible" if uses_retrieval else "indexable"),
            )
        return list(entries.values())

    def _private_config_view(self) -> tuple[dict[str, dict[str, IndexTarget]], set[str], dict[str, tuple[IndexTarget, str]]]:
        return self._private_config_view_for_root(self.root)

    @staticmethod
    def _private_config_view_for_root(
        root: Path,
    ) -> tuple[dict[str, dict[str, IndexTarget]], set[str], dict[str, tuple[IndexTarget, str]]]:
        """读取私域候选及显式route选择；裸target只作候选，绝不隐式发布。"""
        private_dir = Path(root) / "configs" / "indexes-private"
        route_targets: dict[str, dict[str, IndexTarget]] = {}
        ambiguous: set[str] = set()
        candidates: dict[str, tuple[IndexTarget, str]] = {}
        candidate_conflicts: set[str] = set()
        if not private_dir.is_dir():
            return route_targets, ambiguous, candidates

        for path in sorted(private_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            raw_items = data.get("candidates")
            if isinstance(raw_items, list):
                parsed: list[IndexTarget] = []
                try:
                    for item in raw_items:
                        if not isinstance(item, dict):
                            raise ValueError("route candidate必须是对象")
                        target = IndexTarget.from_dict(item)
                        if not target.manifest_path.startswith("corpora-private/"):
                            raise ValueError("private route只能选择私域manifest")
                        parsed.append(target)
                except (KeyError, TypeError, ValueError):
                    continue
                if data.get("schema_version") != 1 or not parsed:
                    # 格式错误的显式route可在管理员目录里检查，但不生效。
                    for target in parsed:
                        candidates[target.target_id] = (target, "needs_review")
                    continue
                for target in parsed:
                    existing_candidate = candidates.get(target.target_id)
                    if existing_candidate is not None and existing_candidate[0] != target:
                        candidate_conflicts.add(target.target_id)
                        ambiguous.add(normalize_library(target.library))
                    else:
                        candidates[target.target_id] = (target, "candidate")
                    library_id = normalize_library(target.library)
                    by_id = route_targets.setdefault(library_id, {})
                    existing = by_id.get(target.target_id)
                    if existing is not None and existing != target:
                        ambiguous.add(library_id)
                    by_id[target.target_id] = target
                continue

            # 独立target文件只供管理员查看，需另有私域route显式选择。
            if "manifest_path" in data:
                try:
                    target = IndexTarget.from_dict(data)
                except (KeyError, TypeError, ValueError):
                    continue
                if target.manifest_path.startswith("corpora-private/"):
                    existing = candidates.get(target.target_id)
                    if existing is not None and existing[0] != target:
                        candidate_conflicts.add(target.target_id)
                        ambiguous.add(normalize_library(target.library))
                    else:
                        candidates.setdefault(target.target_id, (target, "candidate"))

        # route内和独立配置中的同一target去重；真正被唯一route选中的target不再显示为候选。
        unique_by_id: dict[str, IndexTarget] = {}
        for library_id, targets in route_targets.items():
            if len(targets) > 1 or any(target_id in candidate_conflicts for target_id in targets):
                ambiguous.add(library_id)
                for target in targets.values():
                    candidates[target.target_id] = (target, "ambiguous")
            elif targets and library_id not in ambiguous:
                target = next(iter(targets.values()))
                unique_by_id[target.target_id] = target
                candidates.pop(target.target_id, None)
        return route_targets, ambiguous, candidates | {
            target_id: (target, "selected") for target_id, target in unique_by_id.items()
        }

    def _effective_targets(
        self,
        release_snapshot: dict | None = None,
        *,
        private_view: tuple[dict[str, dict[str, IndexTarget]], set[str], dict[str, tuple[IndexTarget, str]]] | None = None,
    ) -> dict[str, tuple[IndexTarget, str]]:
        """release catalog是迁移库权威；仅对未迁移库回退registry/route/private配置。"""
        selected: dict[str, tuple[IndexTarget, str]] = {}
        snapshot = release_snapshot if release_snapshot is not None else self._release_snapshot()
        migrated_libraries = set()

        for library in snapshot["libraries"].values():
            record = library.get("active")
            if record is None:
                if library.get("ever_activated"):
                    migrated_libraries.add(
                        normalize_library(str(library.get("library_id", "")))
                    )
                continue
            target = IndexTarget.from_dict(record["target"])
            migrated_libraries.add(normalize_library(target.library))
            selected[target.target_id] = (target, "release_active")

        def add(raw: dict, origin: str) -> None:
            target = IndexTarget.from_dict(raw)
            existing = selected.get(target.target_id)
            if existing is not None and existing[0] != target:
                raise ValueError(f"当前索引目标配置冲突：{target.target_id}")
            selected[target.target_id] = (target, origin)

        registry_path = self.root / _REGISTRY
        if registry_path.is_file() and "osg" not in migrated_libraries:
            target = IndexRegistryStore(registry_path).active()
            if normalize_library(target.library) not in migrated_libraries:
                add(target.__dict__, "registry_active")

        route_path = self.root / _DEFAULT_ROUTE
        if route_path.is_file():
            route = json.loads(route_path.read_text(encoding="utf-8-sig"))
            if route.get("schema_version") != 1 or not isinstance(route.get("candidates"), list):
                raise ValueError("默认多库route配置无效")
            for item in route["candidates"]:
                if isinstance(item, dict):
                    if normalize_library(str(item.get("library", ""))) not in migrated_libraries:
                        add(item, "default_route")

        routed_private, ambiguous_private, _candidates = (
            private_view if private_view is not None else self._private_config_view()
        )
        for library_id, targets_for_library in routed_private.items():
            # 多target代表没有唯一effective版本；重复声明同一target已在dict中去重。
            if library_id in ambiguous_private or len(targets_for_library) != 1:
                continue
            if library_id in migrated_libraries:
                continue
            target = next(iter(targets_for_library.values()))
            add(target.__dict__, "private_config")
        return selected

    def _entry_targets(self, entry: CorpusEntry, targets: dict[str, tuple[IndexTarget, str]]):
        matches = []
        for target, origin in targets.values():
            if target.corpus_identity() == entry.corpus_id:
                matches.append((target, origin))
        return sorted(matches, key=lambda item: item[0].target_id)

    def _quick_signature(self, target: IndexTarget) -> str:
        manifest = resolve_project_path(self.root, target.manifest_path)
        bm25 = resolve_project_path(self.root, target.bm25_path)
        collection = self.qdrant_client.get_collection(target.dense_collection)
        return hashlib.sha256(
            _canonical_json(
                {
                    "target": target.__dict__,
                    "manifest": [manifest.stat().st_size, manifest.stat().st_mtime_ns],
                    "bm25": [bm25.stat().st_size, bm25.stat().st_mtime_ns],
                    "qdrant": {
                        "points": collection.points_count,
                        "vectors": collection.indexed_vectors_count,
                        "status": str(collection.status),
                    },
                }
            )
        ).hexdigest()

    def _catalog_target_ready(self, target: IndexTarget) -> bool:
        """只做目录级廉价状态检查；逐文档Hash和双索引准入留到用户发起检索时。"""
        manifest_path = resolve_project_path(self.root, target.manifest_path)
        bm25_path = resolve_project_path(self.root, target.bm25_path)
        if not manifest_path.is_file() or not bm25_path.is_file():
            return False
        if _file_sha256(manifest_path) != target.manifest_sha256:
            return False
        collection = self.qdrant_client.get_collection(target.dense_collection)
        vectors = collection.config.params.vectors
        dimension = getattr(vectors, "size", None)
        status = getattr(collection.status, "value", str(collection.status))
        return (
            collection.points_count == target.chunk_count
            and dimension == target.vector_dimension
            and str(status).lower() == "green"
        )

    def _snapshot_id(self, target: IndexTarget, *, force: bool = False) -> str:
        quick = self._quick_signature(target)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(target.target_id)
            if (
                not force
                and cached is not None
                and now - cached[0] < _SNAPSHOT_CACHE_SECONDS
                and cached[1] == quick
            ):
                return cached[2]["version_id"]

        manifest_path = resolve_project_path(self.root, target.manifest_path)
        bm25_path = resolve_project_path(self.root, target.bm25_path)
        # manifest SHA由target绑定；原文scope hash、BM25完整字节及dense全量向量/正文
        # 一起形成身份，避免原文在manifest/index不变时被外部替换后仍返回旧证据。
        version_digest = hashlib.sha256()
        version_digest.update(_canonical_json(target.__dict__))
        version_digest.update(_file_sha256(manifest_path).encode("ascii"))
        version_digest.update(self._raw_sources_fingerprint(target, manifest_path).encode("ascii"))
        version_digest.update(_file_sha256(bm25_path).encode("ascii"))
        version_digest.update(_qdrant_fingerprint(self.qdrant_client, target.dense_collection).encode("ascii"))
        version_id = version_digest.hexdigest()
        result = {"version_id": version_id}
        with self._cache_lock:
            self._cache[target.target_id] = (now, quick, result)
        return version_id

    def _raw_sources_fingerprint(self, target: IndexTarget, manifest_path: Path) -> str:
        manifest = load_manifest(manifest_path)
        records = {record["path"]: record for record in file_records(manifest)}
        sources = CorpusSources.from_manifest(
            manifest_path,
            raw_root=target_raw_data_root(self.root, target),
            expected_corpus_id=target.corpus_identity(),
        )
        digest = hashlib.sha256()
        try:
            for relative in sorted(scope_files(manifest)):
                record = records[relative]
                actual_hash = _file_sha256(sources.locate(relative))
                if actual_hash != record["_content_hash"]:
                    raise ValueError("登记Hash与原文不一致")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(actual_hash.encode("ascii"))
                digest.update(b"\n")
        except Exception as error:  # noqa: BLE001 - 不把本机路径或manifest内容透给管理员UI。
            raise StaleSnapshotError(
                "共享资料原文缺失或已变化，请刷新资料库清单并联系管理员检查"
            ) from error
        return digest.hexdigest()

    @staticmethod
    def _document_id(corpus_id: str, relative_path: str) -> str:
        digest = hashlib.sha256(f"{corpus_id}\0{relative_path}".encode("utf-8")).hexdigest()[:24]
        return f"doc-{digest}"

    def _documents(
        self, entry: CorpusEntry, target: IndexTarget | None, *, include_index_path: bool = False
    ) -> list[dict]:
        manifest_path = (
            resolve_project_path(self.root, target.manifest_path)
            if target is not None
            else self.root / entry.manifest_path
        )
        manifest = load_manifest(manifest_path)
        eligible = set(scope_files(manifest))
        sources = CorpusSources.from_manifest(
            manifest_path,
            raw_root=(
                target_raw_data_root(self.root, target)
                if target is not None else self.root / "raw_data"
            ),
            expected_corpus_id=(target.corpus_identity() if target else entry.corpus_id),
        )
        indexed: dict[str, str] = {}
        if target is not None:
            bm25_path = resolve_project_path(self.root, target.bm25_path)
            # 管理目录必须严格只读：URI mode=ro避免sqlite在资产缺失时创建空数据库。
            with closing(sqlite3.connect(f"{bm25_path.as_uri()}?mode=ro", uri=True)) as database:
                for (stored_path,) in database.execute("SELECT DISTINCT source_path FROM chunks"):
                    try:
                        record = sources.match_chunk_path(str(stored_path))
                    except Exception:
                        continue
                    indexed.setdefault(record.relative, str(stored_path))
        output = []
        all_files = manifest.get("files", [])
        for file_record in sorted(all_files, key=lambda item: str(item.get("path", ""))):
            relative = str(file_record.get("path", ""))
            if not relative:
                continue
            index_source_path = indexed.get(relative)
            document = {
                "document_id": self._document_id(entry.corpus_id, relative),
                "display_name": Path(relative).name,
                "source_path": relative,
                "searchable": relative in eligible and index_source_path is not None,
                "replaceable": False,
                "deletable": False,
            }
            if include_index_path:
                document["index_source_path"] = index_source_path
            output.append(document)
        return output

    def catalog(self, managed_files: list | None = None) -> dict:
        release_snapshot = self._release_snapshot()
        entries = self._corpus_entries(release_snapshot)
        private_view = self._private_config_view()
        effective = self._effective_targets(release_snapshot, private_view=private_view)
        _private_routes, _ambiguous, private_candidates = private_view
        release_owned = {
            normalize_library(str(library.get("library_id", "")))
            for library in release_snapshot["libraries"].values()
            if library.get("active") is not None or library.get("ever_activated")
        }
        groups: dict[str, dict] = {}
        for entry in entries:
            library_id = normalize_library(entry.library)
            group = groups.setdefault(
                library_id,
                {"library_id": library_id, "library": entry.library, "sources": []},
            )
            targets = self._entry_targets(entry, effective)
            if not targets:
                candidates_for_entry = [
                    (target, status)
                    for target, status in private_candidates.values()
                    if target.corpus_identity() == entry.corpus_id
                    and normalize_library(target.library) not in release_owned
                    and status != "selected"
                ]
                if candidates_for_entry:
                    for target, status in candidates_for_entry:
                        publication_status = (
                            status if status in {"ambiguous", "needs_review"} else "candidate"
                        )
                        group["sources"].append({
                            "source_type": "shared",
                            "source_visibility": entry.visibility,
                            "corpus_id": entry.corpus_id,
                            "version": entry.version,
                            "version_id": None,
                            "index_target_id": target.target_id,
                            "source_scope": "private_candidate",
                            "publication_status": publication_status,
                            "is_active": False,
                            "validation_scope": "not_selected_for_service",
                            "searchable": False,
                            "availability_message": (
                                "尚未明确选定服务版本"
                                if publication_status == "candidate"
                                else "存在多个服务版本声明或配置需检查"
                            ),
                            "manageable": False,
                            "documents": self._documents(entry, None),
                        })
                else:
                    group["sources"].append(
                        {
                            "source_type": "shared",
                            "source_visibility": entry.visibility,
                            "corpus_id": entry.corpus_id,
                            "version": entry.version,
                            "version_id": None,
                            "index_target_id": None,
                            "source_scope": "not_active",
                            "publication_status": "not_selected",
                            "is_active": False,
                            "validation_scope": "manifest_only",
                            "searchable": False,
                            "manageable": False,
                            "documents": self._documents(entry, None),
                        }
                    )
            for target, origin in targets:
                version_id = None
                searchable = False
                try:
                    searchable = self._catalog_target_ready(target)
                    if origin == "release_active":
                        searchable = searchable and bool(self.expected_embedding_model) and (
                            target.embedding_model == self.expected_embedding_model
                        )
                    if not searchable:
                        raise ValueError("索引目标未通过快速状态检查")
                    version_id = self._snapshot_id(target)
                except Exception:  # noqa: BLE001 - 目录只显示状态码，不泄漏路径或内部配置。
                    searchable = False
                    version_id = None
                group["sources"].append(
                    {
                        "source_type": "shared",
                        "source_visibility": entry.visibility,
                        "corpus_id": entry.corpus_id,
                        "version": entry.version,
                        "version_id": version_id,
                        "index_target_id": target.target_id,
                        "source_scope": origin,
                        "publication_status": (
                            "published" if origin == "private_config" and searchable
                            else "needs_review" if origin == "private_config"
                            else "published" if searchable else "needs_review"
                        ),
                        # 活动标记要求声明来源明确且快速校验通过；full admission仍在检索前执行。
                        "is_active": (
                            searchable
                            and origin in {
                                "registry_active", "default_route", "release_active", "private_config"
                            }
                        ),
                        "validation_scope": "quick_target_validation; full_admission_on_search",
                        "searchable": searchable,
                        "availability_message": None if searchable else "索引或原文状态需管理员检查",
                        "manageable": False,
                        "documents": self._documents(entry, target if searchable else None),
                    }
                )

        for record in managed_files or []:
            if not record.managed or record.status == "deleted":
                continue
            library_id = record.library_id or "managed-files"
            group = groups.setdefault(
                library_id,
                {
                    "library_id": library_id,
                    "library": record.library_name or record.library or "未分组",
                    "sources": [],
                },
            )
            if record.status != "active" or not record.active_version_id:
                continue
            group["sources"].append(
                {
                    "source_type": "managed",
                    "source_visibility": "private",
                    "corpus_id": None,
                    "file_id": record.file_id,
                    "version": record.version,
                    "version_id": record.active_version_id,
                    "index_target_id": None,
                    "source_scope": "file_owned",
                    "searchable": True,
                    "manageable": True,
                    "documents": [
                        {
                            "document_id": record.file_id,
                            "display_name": record.display_name,
                            "source_path": None,
                            "searchable": True,
                            "replaceable": True,
                            "deletable": True,
                        }
                    ],
                }
            )

        groups.setdefault(
            "managed-files",
            {"library_id": "managed-files", "library": "受管文件", "sources": []},
        )
        for group in groups.values():
            group["sources"].sort(
                key=lambda source: (
                    0 if source.get("is_active") else 1,
                    source["source_type"],
                    source.get("corpus_id") or source.get("file_id") or "",
                    source.get("index_target_id") or "",
                )
            )
        return {"libraries": sorted(groups.values(), key=lambda group: group["library_id"])}

    @staticmethod
    def _serving_profile(
        target: IndexTarget, origin: str, release_snapshot: dict
    ) -> str | None:
        if origin != "release_active":
            return reviewed_profile_for_target(target)
        for library in release_snapshot["libraries"].values():
            record = library.get("active")
            if record is not None and record.get("target", {}).get("target_id") == target.target_id:
                return sealed_profile_for_target(target, record.get("serving_profile"))
        return None

    def _current_target(
        self, corpus_id: str, target_id: str, *, release_snapshot: dict | None = None
    ) -> tuple[IndexTarget, str, CorpusEntry, dict]:
        snapshot = release_snapshot if release_snapshot is not None else self._release_snapshot()
        entries = self._corpus_entries(snapshot)
        entry = next((row for row in entries if row.corpus_id == corpus_id), None)
        if entry is None:
            raise StaleSnapshotError("资料库版本已变化，请刷新资料库清单后重试")
        targets = self._effective_targets(snapshot)
        selected = targets.get(target_id)
        if selected is None or selected[0].corpus_identity() != corpus_id:
            raise StaleSnapshotError("资料库索引目标已变化，请刷新资料库清单后重试")
        return selected[0], selected[1], entry, snapshot

    def validate_selection(self, library_id: str, corpus_id: str, target_id: str) -> None:
        """先校验用户选中的稳定身份，再允许它进入调用日志target字段。"""
        _target, _origin, entry, _snapshot = self._current_target(corpus_id, target_id)
        if normalize_library(entry.library) != library_id:
            raise ValueError("corpus_id不属于所选library_id")

    def read_source(
        self,
        *,
        library_id: str,
        corpus_id: str,
        target_id: str,
        version_id: str,
        document_id: str,
    ) -> VerifiedSource:
        """按一个release快照校验身份并读取原文；HTTP层只负责授权和响应。"""
        release_snapshot = self._release_snapshot()
        target, _origin, entry, _snapshot = self._current_target(
            corpus_id, target_id, release_snapshot=release_snapshot
        )
        if normalize_library(entry.library) != normalize_library(library_id):
            raise ValueError("corpus_id不属于所选library_id")
        if self._snapshot_id(target, force=True) != version_id:
            raise StaleSnapshotError("资料库版本已变化，请刷新后重试")
        documents = self._documents(entry, target)
        document = next(
            (item for item in documents if item["document_id"] == document_id), None
        )
        if document is None or not document["searchable"]:
            raise ValueError("document_id不属于当前可检索语料版本")
        manifest_path = resolve_project_path(self.root, target.manifest_path)
        sources = CorpusSources.from_manifest(
            manifest_path,
            raw_root=target_raw_data_root(self.root, target),
            expected_corpus_id=target.corpus_identity(),
        )
        return sources.read_verified(document["source_path"])

    def search(
        self,
        *,
        library_id: str,
        corpus_id: str,
        version_id: str,
        query: str,
        index_target_id: str,
        document_id: str | None = None,
        top_k: int = 5,
        context_char_budget: int = 8000,
    ) -> dict:
        release_snapshot = self._release_snapshot()
        target, origin, entry, release_snapshot = self._current_target(
            corpus_id, index_target_id, release_snapshot=release_snapshot
        )
        if normalize_library(entry.library) != library_id:
            raise ValueError("corpus_id不属于所选library_id")
        if self.expected_embedding_model is not None and (
            target.embedding_model != self.expected_embedding_model
        ):
            raise StaleSnapshotError("当前Embedding模型与索引声明不一致")
        if origin == "release_active" and not self.expected_embedding_model:
            raise StaleSnapshotError("当前服务未提供Embedding模型身份，拒绝检索release")
        if self.search_policy.reranker_enabled and self.reranker is None:
            raise RuntimeError("共享检索配置启用了reranker，但当前服务没有可用实例")
        current_version_id = self._snapshot_id(target, force=True)
        if current_version_id != version_id:
            raise StaleSnapshotError("资料库或索引版本已变化，请刷新资料库清单后重试")
        documents = self._documents(entry, target, include_index_path=True)
        selected_document = None
        if document_id is not None:
            selected_document = next(
                (item for item in documents if item["document_id"] == document_id), None
            )
            if selected_document is None or not selected_document["searchable"]:
                raise ValueError("document_id不属于当前可检索语料版本")
        try:
            admit_index(target, root=self.root, qdrant_client=self.qdrant_client)
        except Exception as error:
            raise StaleSnapshotError(
                "资料库索引未通过当前资产校验，请刷新资料库清单并联系管理员检查"
            ) from error

        # UI使用manifest相对路径作为身份；检索需使用BM25/Qdrant里真实source_path，
        # 两者在新raw_data前缀语料中并不相同。
        source_filter = selected_document["index_source_path"] if selected_document else None

        def dense_search(client, vector, collection_name, *, top_k: int = 5):
            if source_filter is None:
                from wenjin.indexing.dense import dense_query_vector

                return dense_query_vector(client, vector, collection_name, top_k=top_k)
            from qdrant_client import models

            return client.query_points(
                collection_name,
                query=vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source_path", match=models.MatchValue(value=source_filter)
                        )
                    ]
                ),
                limit=top_k,
            ).points

        def keyword_search(path, query_text, *, top_k: int = 5):
            from wenjin.indexing.bm25 import bm25_query

            return bm25_query(path, query_text, top_k=top_k, source_path=source_filter)

        manifest_path = resolve_project_path(self.root, target.manifest_path)
        sources = CorpusSources.from_manifest(
            manifest_path,
            raw_root=target_raw_data_root(self.root, target),
            expected_corpus_id=target.corpus_identity(),
        )
        context_profile = self._serving_profile(target, origin, release_snapshot)
        routed = RoutedIndex(
            target=target,
            collection=target.dense_collection,
            bm25_path=resolve_project_path(self.root, target.bm25_path),
            source_urls=load_source_urls(manifest_path),
            sources=sources,
            source_context_cache=(
                VerifiedSourceContextCache(sources, profile=context_profile)
                if context_profile
                else None
            ),
        )
        router = MultiCorpusRouter(
            qdrant_client=self.qdrant_client,
            embedder=self.embedder,
            indices=[routed],
            reranker=self.reranker,
            dense_search=dense_search,
            keyword_search=keyword_search,
            search_lock=self._search_lock,
        )
        config = self.search_policy.route_config(
            top_k, context_char_budget=context_char_budget
        )
        result = router.search(query, target_id=target.target_id, config=config)
        # 准入之后若外部写入者替换了目标资产，本次证据也不返回。
        latest_target, latest_origin, latest_entry, _latest_snapshot = self._current_target(
            corpus_id, index_target_id
        )
        if (
            latest_target != target
            or latest_origin != origin
            or latest_entry.corpus_id != entry.corpus_id
            or self._snapshot_id(latest_target, force=True) != version_id
        ):
            raise StaleSnapshotError("检索期间资料库索引已变化，请刷新资料库清单后重试")
        result["catalog_identity"] = {
            "library_id": library_id,
            "source_type": "shared",
            "corpus_id": corpus_id,
            "file_id": None,
            "version_id": version_id,
            "index_target_id": target.target_id,
            "source_scope": origin,
        }
        result["retrieval_config"] = self.search_policy.public_config(
            top_k, context_char_budget=context_char_budget
        )
        return result
