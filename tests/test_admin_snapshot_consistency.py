"""共享资料检索期间的路由和源文件并发变化必须阻止返回证据。"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from qdrant_client import QdrantClient

from wenjin.content_hashing import hash_content, manifest_content_fingerprint
from wenjin.file_management.legacy_search import LegacySearchService
from wenjin.file_management.models import StaleSnapshotError
from wenjin.index_registry import IndexRegistryStore
from wenjin.indexing.build import build_candidate_index


class HookEmbedder:
    """确定性小向量器；可在真实检索开始时模拟外部并发写入。"""

    def __init__(self):
        self.on_query = None

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def get_query_embedding(self, query):
        if self.on_query is not None:
            self.on_query()
        return [1.0, 0.0, 0.0, 0.0]


def _build_shared_assets(root, qdrant):
    raw = root / "raw_data" / "toy" / "1.0"
    raw.mkdir(parents=True)
    source = raw / "guide.txt"
    source.write_text("# Amber widget\nThe amberwidget starts safely.\n", encoding="utf-8")
    hashed = hash_content(source.read_bytes(), normalization="text-newline-lf-v1", encoding="utf-8")
    manifest = {
        "schema_version": 3,
        "corpus_id": "toy-1.0",
        "library": "toy",
        "version": "1.0",
        "raw_data_subdir": "toy/1.0",
        "files": [{
            "path": "guide.txt",
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "normalization": "text-newline-lf-v1",
            "encoding": "utf-8",
            "bytes": source.stat().st_size,
            "retrieval_eligible": True,
            "indexable": True,
            "parser": "text-line-v1",
            "source_url": "urn:synthetic:guide",
            "logical_document_id": "toy-1.0:guide.txt",
        }],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest_path = root / "corpora" / "toy-1.0" / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    targets = []
    for suffix in ("old", "new"):
        targets.append(build_candidate_index(
            manifest_path=manifest_path,
            target_id=f"toy-1.0-{suffix}",
            strategy="blocks",
            dense_collection=f"toy_1_0_{suffix}",
            bm25_path=root / "data" / "indexes" / f"toy_{suffix}.sqlite",
            qdrant_client=qdrant,
            embedder=HookEmbedder(),
            embedding_model="isolated-four-dimensional-test",
            project_root=root,
        ))
    return source, targets


def _write_route(root, targets):
    route_path = root / "configs" / "indexes" / "multicorpus-candidates-003.json"
    route_path.parent.mkdir(parents=True, exist_ok=True)
    route_path.write_text(json.dumps({"schema_version": 1, "candidates": [asdict(item) for item in targets]}), encoding="utf-8")


@pytest.mark.parametrize("switch_source", ["route", "registry"])
def test_search_rejects_target_removed_mid_query_even_when_old_assets_remain(tmp_path, switch_source):
    qdrant = QdrantClient(":memory:")
    embedder = HookEmbedder()
    source, (old_target, new_target) = _build_shared_assets(tmp_path, qdrant)
    if switch_source == "route":
        _write_route(tmp_path, [old_target])
    else:
        _write_route(tmp_path, [])
        registry = IndexRegistryStore(tmp_path / "data" / "indexes" / "index-registry.json")
        registry.initialize(old_target, reason="synthetic test setup")
    service = LegacySearchService(tmp_path, qdrant, embedder)
    try:
        catalog_result = service.catalog()
        group = next(row for row in catalog_result["libraries"] if row["library_id"] == "toy")
        catalog = group["sources"][0]
        assert catalog["index_target_id"] == old_target.target_id
        assert source.is_file()
        switched = False

        def switch_route_or_registry():
            nonlocal switched
            if switch_source == "route":
                _write_route(tmp_path, [new_target])
            else:
                IndexRegistryStore(tmp_path / "data" / "indexes" / "index-registry.json").switch(
                    new_target, reason="synthetic concurrent route switch"
                )
            switched = True

        embedder.on_query = switch_route_or_registry
        with pytest.raises(StaleSnapshotError):
            service.search(
                library_id="toy",
                corpus_id="toy-1.0",
                version_id=catalog["version_id"],
                query="amberwidget",
                index_target_id=old_target.target_id,
            )

        assert switched, "目标切换必须发生在真实检索开始后"
        assert qdrant.collection_exists(old_target.dense_collection)
        assert (tmp_path / old_target.bm25_path).is_file()
    finally:
        qdrant.close()


def test_search_rejects_source_change_after_admission_instead_of_returning_old_evidence(tmp_path):
    qdrant = QdrantClient(":memory:")
    embedder = HookEmbedder()
    source, (target, _other_target) = _build_shared_assets(tmp_path, qdrant)
    _write_route(tmp_path, [target])
    service = LegacySearchService(tmp_path, qdrant, embedder)
    try:
        catalog_result = service.catalog()
        group = next(row for row in catalog_result["libraries"] if row["library_id"] == "toy")
        catalog = group["sources"][0]
        original_text = source.read_text(encoding="utf-8")
        changed = False

        def change_raw_source():
            nonlocal changed
            source.write_text(
                "# Changed during retrieval\nA newer raw source replaces the old one.\n",
                encoding="utf-8",
            )
            changed = True

        embedder.on_query = change_raw_source

        with pytest.raises(StaleSnapshotError):
            service.search(
                library_id="toy",
                corpus_id="toy-1.0",
                version_id=catalog["version_id"],
                query="amberwidget",
                index_target_id=target.target_id,
            )

        assert changed and source.read_text(encoding="utf-8") != original_text
    finally:
        qdrant.close()
