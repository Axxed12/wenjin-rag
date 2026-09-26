"""从任意已校验manifest全量构建隔离的候选dense与BM25索引。"""

import hashlib
import json
from pathlib import Path

from ingest import load_documents
from wenjin.content_hashing import manifest_content_fingerprint
from wenjin.index_registry import IndexTarget
from wenjin.indexing.bm25 import build_bm25_index
from wenjin.indexing.dense import create_dense_collection, embed_chunks
from wenjin.ingestion.chunking import (
    PARENTED_STRATEGY,
    block_chunks,
    block_chunks_parented,
    fixed_chunks,
)
from wenjin.ingestion.multiformat import load_retrieval_documents

SUPPORTED_STRATEGIES = ("blocks", "blocks-split", PARENTED_STRATEGY, "fixed")


def build_candidate_index(
    *,
    manifest_path: Path,
    target_id: str,
    strategy: str,
    dense_collection: str,
    bm25_path: Path,
    qdrant_client,
    embedder,
    embedding_model: str,
    project_root: Path,
) -> IndexTarget:
    """拒绝覆盖现有资产；任一步失败则删除本次新建的候选。

    `blocks-split-parented` 只把 **child** 写进 dense 与 BM25（检索命中的是它们），
    父块写进 BM25 文件里的 `parents` 表（见 `chunking.block_chunks_parented`）。
    `chunk_count` 因此仍是**可检索片段数**，与 BM25 行数和 Qdrant 点数逐一对应；
    父块由准入审计按同一套来源绑定独立核验，不靠数量对账。
    """
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError("strategy必须为" + "、".join(SUPPORTED_STRATEGIES))
    if qdrant_client.collection_exists(dense_collection):
        raise FileExistsError(f"候选collection已存在：{dense_collection}")
    if bm25_path.exists():
        raise FileExistsError(f"候选BM25已存在：{bm25_path}")
    manifest_path = manifest_path.resolve()
    project_root = project_root.resolve()
    if not manifest_path.is_relative_to(project_root) or not bm25_path.resolve().is_relative_to(
        project_root
    ):
        raise ValueError("manifest和BM25输出必须位于项目目录内")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    uses_retrieval_scope = any("retrieval_eligible" in entry for entry in manifest["files"])
    documents = (
        load_retrieval_documents(manifest_path, project_root / "raw_data")
        if uses_retrieval_scope
        else load_documents(manifest_path)
    )
    chunks = []
    parents = []
    for document in documents:
        if strategy == "fixed":
            chunks.extend(fixed_chunks(document, size=1200, overlap=120))
        elif strategy == PARENTED_STRATEGY:
            children, parent_chunks = block_chunks_parented(document, max_chars=1600)
            chunks.extend(children)
            parents.extend(parent_chunks)
        else:
            # blocks-split 对超长块强制生效，chunk 文本不再超过 max_chars。
            chunks.extend(
                block_chunks(
                    document, max_chars=1600, split_oversized=strategy == "blocks-split"
                )
            )
    if not chunks:
        raise ValueError("候选语料没有生成chunk")
    try:
        vectors = embed_chunks(embedder, chunks)
        create_dense_collection(qdrant_client, dense_collection, chunks, vectors)
        build_bm25_index(
            bm25_path, chunks, parents=parents if strategy == PARENTED_STRATEGY else None
        )
    except Exception:
        if qdrant_client.collection_exists(dense_collection):
            qdrant_client.delete_collection(dense_collection)
        bm25_path.unlink(missing_ok=True)
        raise

    profiles = {chunk.normalization for chunk in chunks}
    return IndexTarget(
        target_id=target_id,
        corpus_id=manifest.get("corpus_id"),
        library=manifest.get("library", "osg"),
        version=manifest["version"],
        strategy=strategy,
        dense_collection=dense_collection,
        bm25_path=str(bm25_path.resolve().relative_to(project_root)).replace("\\", "/"),
        manifest_path=str(manifest_path.relative_to(project_root)).replace("\\", "/"),
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=len(chunks),
        vector_dimension=len(vectors[0]),
        embedding_model=embedding_model,
        canonical_corpus_sha256=(
            manifest.get("retrieval_content_sha256")
            or manifest_content_fingerprint(manifest)
        ),
        normalization_profile=(
            next(iter(profiles)) if len(profiles) == 1 else "mixed-versioned-v1"
        ),
    )
