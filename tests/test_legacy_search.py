from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models

from wenjin.file_management.legacy_search import LegacySearchService, _qdrant_fingerprint
from wenjin.file_management.models import StaleSnapshotError
from wenjin.index_registry import IndexTarget
from wenjin.indexing.bm25 import build_bm25_index
from wenjin.ingestion.chunking import Chunk


def test_document_catalog_includes_excluded_files_and_keeps_prefixed_index_path_internal(tmp_path: Path):
    corpus_id = "demo-1.0"
    manifest_dir = tmp_path / "corpora" / corpus_id
    manifest_dir.mkdir(parents=True)
    files = [
        {
            "path": "docs/a%_guide.txt",
            "raw_sha256": hashlib.sha256(b"a").hexdigest(),
            "retrieval_eligible": True,
            "indexable": True,
        },
        {
            "path": "archive/old.txt",
            "raw_sha256": hashlib.sha256(b"b").hexdigest(),
            "retrieval_eligible": False,
            "indexable": False,
        },
    ]
    (manifest_dir / "source-manifest.json").write_text(
        json.dumps(
            {
                "corpus_id": corpus_id,
                "library": "demo",
                "version": "1.0",
                "raw_data_subdir": "demo/1.0",
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    bm25_path = tmp_path / "data" / "demo.sqlite"
    build_bm25_index(
        bm25_path,
        [
            Chunk(
                "indexed-doc",
                "raw_data/demo/1.0/docs/a%_guide.txt",
                "1.0",
                1,
                1,
                "amberwidget exact document",
                "blocks",
            )
        ],
    )
    entry = SimpleNamespace(
        corpus_id=corpus_id,
        manifest_path=f"corpora/{corpus_id}/source-manifest.json",
    )
    target = SimpleNamespace(bm25_path="data/demo.sqlite")
    service = LegacySearchService(tmp_path, qdrant_client=None, embedder=None)

    public_documents = service._documents(entry, target)
    assert len(public_documents) == 2
    by_path = {item["source_path"]: item for item in public_documents}
    eligible = by_path["docs/a%_guide.txt"]
    excluded = by_path["archive/old.txt"]
    assert eligible["source_path"] == "docs/a%_guide.txt"
    assert eligible["searchable"] is True
    assert "index_source_path" not in eligible
    assert excluded["source_path"] == "archive/old.txt"
    assert excluded["searchable"] is False

    internal_documents = service._documents(entry, target, include_index_path=True)
    internal_by_path = {item["source_path"]: item for item in internal_documents}
    assert internal_by_path["docs/a%_guide.txt"]["index_source_path"] == (
        "raw_data/demo/1.0/docs/a%_guide.txt"
    )
    assert internal_by_path["archive/old.txt"]["index_source_path"] is None

    missing = SimpleNamespace(bm25_path="data/missing.sqlite")
    with pytest.raises(Exception):
        service._documents(entry, missing)
    assert not (tmp_path / "data" / "missing.sqlite").exists()


def test_shared_snapshot_changes_for_same_count_payload_and_rejects_changed_raw_source(tmp_path: Path):
    qdrant = QdrantClient(":memory:")
    qdrant.create_collection(
        "demo_collection",
        vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE),
    )
    point_id = "00000000-0000-0000-0000-000000000001"
    qdrant.upsert(
        "demo_collection",
        points=[models.PointStruct(id=point_id, vector=[1.0, 0.0], payload={"source_path": "a.md"})],
    )
    bm25_path = tmp_path / "data" / "bm25.sqlite"
    build_bm25_index(
        bm25_path,
        [Chunk("one", "a.md", "1.0", 1, 1, "amberwidget", "blocks")],
    )
    source_file = tmp_path / "raw_data" / "demo" / "1.0" / "a.md"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("source", encoding="utf-8")
    manifest_path = tmp_path / "corpora" / "demo-1.0" / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "corpus_id": "demo-1.0",
        "library": "demo",
        "version": "1.0",
        "raw_data_subdir": "demo/1.0",
        "files": [
            {
                "path": "a.md",
                "raw_sha256": hashlib.sha256(b"source").hexdigest(),
                "retrieval_eligible": True,
                "indexable": True,
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    target = IndexTarget(
        target_id="demo-target",
        library="demo",
        version="1.0",
        strategy="blocks",
        dense_collection="demo_collection",
        bm25_path="data/bm25.sqlite",
        manifest_path="corpora/demo-1.0/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=1,
        vector_dimension=2,
        embedding_model="test",
        corpus_id="demo-1.0",
    )
    service = LegacySearchService(tmp_path, qdrant, embedder=None)
    before = service._snapshot_id(target, force=True)
    qdrant.set_payload("demo_collection", payload={"source_path": "a.md", "body": "changed"}, points=[point_id])
    assert qdrant.get_collection("demo_collection").points_count == 1
    after = service._snapshot_id(target, force=True)
    assert before != after
    source_file.write_text("changed source", encoding="utf-8")
    with pytest.raises(StaleSnapshotError, match="原文缺失或已变化"):
        service._snapshot_id(target, force=True)
    qdrant.close()


def test_cosine_snapshot_stays_stable_during_local_queries_and_detects_direction_changes():
    """真实local查询会再次归一化float32向量，不能将末位抖动误报为资产发布。"""
    client = QdrantClient(":memory:")
    try:
        client.create_collection(
            "cosine", vectors_config=models.VectorParams(size=3, distance=models.Distance.COSINE)
        )
        point_id = "00000000-0000-0000-0000-000000000002"
        client.upsert("cosine", points=[models.PointStruct(
            id=point_id, vector=[1.0, 1.0, 2.0], payload={"text": "unchanged"}
        )])
        original = _qdrant_fingerprint(client, "cosine")
        for _ in range(8):
            client.query_points("cosine", query=[1.0, 0.0, 0.0], limit=1)
            assert _qdrant_fingerprint(client, "cosine") == original
        client.upsert("cosine", points=[models.PointStruct(
            id=point_id, vector=[1.0, 2.0, 1.0], payload={"text": "unchanged"}
        )])
        assert _qdrant_fingerprint(client, "cosine") != original
    finally:
        client.close()


def test_dot_snapshot_keeps_vector_magnitude():
    client = QdrantClient(":memory:")
    try:
        client.create_collection(
            "dot", vectors_config=models.VectorParams(size=2, distance=models.Distance.DOT)
        )
        point_id = "00000000-0000-0000-0000-000000000003"
        client.upsert("dot", points=[models.PointStruct(id=point_id, vector=[1.0, 2.0])])
        original = _qdrant_fingerprint(client, "dot")
        client.upsert("dot", points=[models.PointStruct(id=point_id, vector=[2.0, 4.0])])
        assert _qdrant_fingerprint(client, "dot") != original
    finally:
        client.close()
