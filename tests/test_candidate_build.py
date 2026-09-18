import hashlib
import json
import sqlite3

import pytest

from wenjin.indexing.build import build_candidate_index


class FakeEmbedder:
    def get_text_embedding_batch(self, texts, show_progress=False):
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeQdrant:
    def __init__(self):
        self.collections = {}

    def collection_exists(self, name):
        return name in self.collections

    def recreate_collection(self, collection_name, vectors_config):
        self.collections[collection_name] = []

    def upsert(self, collection_name, points, wait):
        self.collections[collection_name].extend(points)

    def delete_collection(self, name):
        self.collections.pop(name, None)


def make_manifest(root):
    raw = root / "raw"
    raw.mkdir()
    source = raw / "doc.md"
    source.write_text("# API\nrefreshIndex returns bool.\n", encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "library": "demo",
        "version": "1.0",
        "raw_root": "raw",
        "files": [{
            "path": "doc.md",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "bytes": source.stat().st_size,
            "indexable": True,
            "source_url": "urn:test",
        }],
    }), encoding="utf-8")
    return manifest


def test_build_candidate_creates_isolated_dense_and_bm25(tmp_path):
    manifest = make_manifest(tmp_path)
    bm25 = tmp_path / "candidate.sqlite"
    client = FakeQdrant()
    target = build_candidate_index(
        manifest_path=manifest,
        target_id="demo-1",
        strategy="blocks",
        dense_collection="demo_collection",
        bm25_path=bm25,
        qdrant_client=client,
        embedder=FakeEmbedder(),
        embedding_model="fake",
        project_root=tmp_path,
    )
    assert target.chunk_count == 1 and target.vector_dimension == 3
    assert len(client.collections["demo_collection"]) == 1
    with sqlite3.connect(bm25) as database:
        assert database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1


def test_build_candidate_refuses_existing_assets(tmp_path):
    manifest = make_manifest(tmp_path)
    bm25 = tmp_path / "candidate.sqlite"
    bm25.write_text("occupied", encoding="utf-8")
    with pytest.raises(FileExistsError, match="BM25"):
        build_candidate_index(
            manifest_path=manifest,
            target_id="demo-1",
            strategy="blocks",
            dense_collection="demo_collection",
            bm25_path=bm25,
            qdrant_client=FakeQdrant(),
            embedder=FakeEmbedder(),
            embedding_model="fake",
            project_root=tmp_path,
        )
