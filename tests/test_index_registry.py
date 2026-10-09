import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.index_registry import (
    IndexRegistryStore,
    IndexTarget,
    resolve_project_path,
    validate_target,
)


def target(tmp_path: Path, target_id: str = "a") -> IndexTarget:
    # 用真实形态的manifest：validate_target会执行manifest契约校验
    # （标识符、文件路径、每条记录的内容Hash），空对象会被正确拒绝。
    corpus_dir = tmp_path / "corpora" / "osg-3.6.5"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest = corpus_dir / "source-manifest.json"
    manifest.write_text(json.dumps({
        "library": "osg", "version": "3.6.5", "raw_data_subdir": "osg/3.6.5",
        "files": [{
            "path": "README.md",
            "raw_sha256": "0" * 64,
            "canonical_sha256": "0" * 64,
            "normalization": "text-newline-lf-v1",
            "logical_document_id": "osg-3.6.5:README.md",
            "indexable": True,
        }],
    }), encoding="utf-8")
    database = tmp_path / "index.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS chunks(version, strategy)")
            if connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0:
                connection.executemany(
                    "INSERT INTO chunks VALUES (?, ?)", [("3.6.5", "blocks")] * 2
                )
    return IndexTarget(
        target_id=target_id,
        library="osg",
        version="3.6.5",
        strategy="blocks",
        dense_collection=f"collection_{target_id}",
        bm25_path="index.sqlite",
        manifest_path="corpora/osg-3.6.5/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        chunk_count=2,
        vector_dimension=3,
        embedding_model="test",
    )


class FakeQdrant:
    def get_collection(self, name):
        return SimpleNamespace(
            status=SimpleNamespace(value="green"),
            points_count=2,
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=3))
            ),
        )


def test_validate_target_checks_all_three_assets(tmp_path):
    result = validate_target(target(tmp_path), root=tmp_path, qdrant_client=FakeQdrant())
    assert result["valid"] is True
    assert result["bm25_rows"] == result["qdrant_points"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows会阻止删除仍被SQLite句柄打开的文件")
def test_validate_target_closes_bm25_handle_for_managed_retry(tmp_path):
    """Windows重试/删除候选索引前，准入审计必须释放BM25文件句柄。"""
    config = target(tmp_path)
    validate_target(config, root=tmp_path, qdrant_client=FakeQdrant())
    bm25_path = resolve_project_path(tmp_path, config.bm25_path)
    bm25_path.unlink()
    assert not bm25_path.exists()


def test_registry_switch_and_rollback_are_revisioned(tmp_path):
    store = IndexRegistryStore(tmp_path / "registry.json")
    first = target(tmp_path, "first")
    second = target(tmp_path, "second")
    store.initialize(first, reason="bootstrap")
    assert store.switch(second, reason="candidate passed")["revision"] == 2
    assert store.active().target_id == "second"
    state = store.rollback(reason="smoke failed")
    assert state["revision"] == 3
    assert store.active().target_id == "first"
    assert state["history"] == []
    assert [event["action"] for event in state["events"]] == [
        "initialize", "switch", "rollback"
    ]


def test_registry_rejects_path_escape_and_duplicate_switch(tmp_path):
    with pytest.raises(ValueError, match="超出项目目录"):
        resolve_project_path(tmp_path, "../secret")
    store = IndexRegistryStore(tmp_path / "registry.json")
    first = target(tmp_path, "first")
    store.initialize(first, reason="bootstrap")
    with pytest.raises(ValueError, match="已经是active"):
        store.switch(first, reason="duplicate")


def test_atomic_write_leaves_no_temporary_file(tmp_path):
    store = IndexRegistryStore(tmp_path / "registry.json")
    store.initialize(target(tmp_path), reason="bootstrap")
    json.loads(store.path.read_text(encoding="utf-8"))
    assert list(tmp_path.glob(".*.tmp")) == []
