"""构造用于共享检索入口合同的多候选合成索引。

调用方必须传入仓库外独立临时root；本夹具只在root/raw_data、root/corpora和
root/data/indexes下创建合成manifest、30个短原文、BM25 SQLite及内存Qdrant集合。
它通过正式build_candidate_index/IndexRegistryStore/admit_index生成并校验索引，
不加载Settings、不读.env、不查默认registry或真实语料。Qdrant client由调用方关闭。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient

from wenjin.content_hashing import (
    TEXT_NEWLINE_LF_V1,
    hash_content,
    manifest_content_fingerprint,
)
from wenjin.index_audit import admit_index
from wenjin.index_registry import IndexRegistryStore
from wenjin.indexing.build import build_candidate_index

_REPO_ROOT = Path(__file__).resolve().parents[2]


class SyntheticSearchEmbedder:
    """确定性小向量桩，不触发任何模型加载。"""

    dimension = 8

    @staticmethod
    def _vector(text: str) -> list[float]:
        # 用严格单位(one-hot)方向避开本地Qdrant COSINE查询对一般float32向量
        # 再归一化造成的末位漂移；检索仍使用真实Qdrant索引与查询调用。
        import hashlib

        raw = hashlib.sha256(text.encode("utf-8")).digest()
        selected = int.from_bytes(raw[:4], "big") % 8
        return [1.0 if index == selected else 0.0 for index in range(8)]

    def get_text_embedding_batch(
        self, texts: list[str], *, show_progress: bool = False
    ) -> list[list[float]]:
        del show_progress
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query: str) -> list[float]:
        return self._vector(query)


def build_shared_search_fixture(
    *,
    root: Path,
    library_id: str = "synthetic-search",
    version: str = "1.0",
    document_count: int = 30,
    qdrant_client: QdrantClient | None = None,
) -> dict[str, Any]:
    """在显式空临时root中创建多候选shared target。

    每个文件对应一个短chunk，正文包含共同检索短语与唯一marker。30条候选足以
    观察candidate_k=10/20及Top5，不保证任何检索质量分数或产品策略排名。
    返回`target`、`registry`、`qdrant`、`embedder`、manifest/raw/BM25路径和marker。
    若传入共享Qdrant，调用方仍负责关闭该client。
    """
    resolved_root = Path(root).resolve()
    if resolved_root == _REPO_ROOT or resolved_root.is_relative_to(_REPO_ROOT):
        raise ValueError("shared search fixture root必须位于仓库外的独立临时目录")
    if not library_id or any(ch in library_id for ch in "\\/\0"):
        raise ValueError("library_id必须是简单测试标识")
    if not 1 <= document_count <= 100:
        raise ValueError("document_count必须在1到100之间")

    raw_root = resolved_root / "raw_data" / library_id / version
    manifest_path = resolved_root / "corpora" / f"{library_id}-{version}" / "source-manifest.json"
    bm25_path = resolved_root / "data" / "indexes" / f"{library_id}-{version}-bm25.sqlite"
    registry_path = resolved_root / "data" / "indexes" / f"{library_id}-index-registry.json"
    collection = f"synthetic_{library_id.replace('-', '_')}_{version.replace('.', '_')}"
    expected_paths = [manifest_path, bm25_path, registry_path]
    if any(path.exists() for path in expected_paths):
        raise FileExistsError("拒绝覆盖已存在的shared search fixture文件")

    raw_root.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    markers = []
    for index in range(document_count):
        relative = f"doc-{index:03d}.txt"
        marker = f"shared_marker_{index:03d}"
        markers.append(marker)
        content = (
            f"Synthetic shared retrieval document {index:03d}.\n"
            f"Common query phrase: amber widget shared lookup.\n"
            f"Unique document marker: {marker}.\n"
        ).encode("utf-8")
        hashed = hash_content(
            content, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
        )
        path = raw_root / relative
        with path.open("xb") as output:
            output.write(content)
        entries.append(
            {
                "path": relative,
                "raw_sha256": hashed.raw_sha256,
                "canonical_sha256": hashed.canonical_sha256,
                "normalization": TEXT_NEWLINE_LF_V1,
                "encoding": "utf-8",
                "bytes": len(content),
                "indexable": True,
                "parser": "text-line-v1",
                "source_url": f"urn:synthetic:{library_id}:{version}/{relative}",
                "logical_document_id": f"{library_id}-{version}:{relative}",
            }
        )

    manifest = {
        "schema_version": 3,
        "corpus_id": f"{library_id}-{version}",
        "library": library_id,
        "version": version,
        "license": "synthetic-test-only",
        "source_url": f"urn:synthetic:{library_id}:{version}",
        "raw_data_subdir": f"{library_id}/{version}",
        "files": entries,
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    with manifest_path.open("xb") as output:
        output.write((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    owns_qdrant = qdrant_client is None
    qdrant = qdrant_client or QdrantClient(":memory:")
    if qdrant.collection_exists(collection):
        if owns_qdrant:
            qdrant.close()
        raise FileExistsError("拒绝覆盖已存在的shared search fixture collection")
    embedder = SyntheticSearchEmbedder()
    try:
        target = build_candidate_index(
            manifest_path=manifest_path,
            target_id=f"{library_id}-{version}-candidate",
            strategy="blocks-split",
            dense_collection=collection,
            bm25_path=bm25_path,
            qdrant_client=qdrant,
            embedder=embedder,
            embedding_model="synthetic-shared-search-v1",
            project_root=resolved_root,
        )
        registry = IndexRegistryStore(registry_path)
        registry.initialize(target, reason="synthetic shared search fixture")
        admit_index(target, root=resolved_root, qdrant_client=qdrant)
    except Exception:
        if qdrant.collection_exists(collection):
            qdrant.delete_collection(collection)
        if owns_qdrant:
            qdrant.close()
        raise

    return {
        "root": resolved_root,
        "qdrant": qdrant,
        "owns_qdrant": owns_qdrant,
        "embedder": embedder,
        "target": target,
        "registry": registry,
        "registry_path": registry_path,
        "manifest_path": manifest_path,
        "bm25_path": bm25_path,
        "raw_root": raw_root,
        "collection": collection,
        "document_count": document_count,
        "markers": markers,
        "query": "amber widget shared lookup",
        "classification": "synthetic-shared-target-not-retrieval-quality-validation",
    }
