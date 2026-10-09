"""从任意已校验manifest全量构建隔离的候选dense与BM25索引。"""

import hashlib
import json
from pathlib import Path

from wenjin.content_hashing import manifest_content_fingerprint
from wenjin.index_registry import IndexTarget
from wenjin.indexing.bm25 import build_bm25_index
from wenjin.indexing.dense import create_dense_collection, embed_chunks
from wenjin.ingestion.chunking import block_chunks, fixed_chunks
from wenjin.ingestion.manifest_documents import load_documents
from wenjin.ingestion.multiformat import load_retrieval_documents


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
    raw_data_root: Path | None = None,
) -> IndexTarget:
    """拒绝覆盖现有资产；任一步失败则删除本次新建的候选。"""
    if strategy not in {"blocks", "blocks-split", "fixed"}:
        raise ValueError("strategy必须为blocks、blocks-split或fixed")
    if qdrant_client.collection_exists(dense_collection):
        raise FileExistsError(f"候选collection已存在：{dense_collection}")
    if bm25_path.exists():
        raise FileExistsError(f"候选BM25已存在：{bm25_path}")
    manifest_path = manifest_path.resolve()
    project_root = project_root.resolve()
    trusted_raw_data_root = Path(raw_data_root or (project_root / "raw_data")).resolve()
    if not trusted_raw_data_root.is_relative_to(project_root):
        raise ValueError("原文存储root必须位于项目目录内")
    if not manifest_path.is_relative_to(project_root) or not bm25_path.resolve().is_relative_to(
        project_root
    ):
        raise ValueError("manifest和BM25输出必须位于项目目录内")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    uses_retrieval_scope = any("retrieval_eligible" in entry for entry in manifest["files"])
    if uses_retrieval_scope:
        documents = load_retrieval_documents(manifest_path, trusted_raw_data_root)
    else:
        documents = load_documents(
            manifest_path, raw_data_root=trusted_raw_data_root
        )
    chunks = []
    for document in documents:
        if strategy == "fixed":
            chunks.extend(fixed_chunks(document, size=1200, overlap=120))
        else:
            # blocks-split 按行拆分超长块；过长单行会完整保留，允许超过 max_chars。
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
        build_bm25_index(bm25_path, chunks)
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
        raw_snapshot_root=(
            str(trusted_raw_data_root.relative_to(project_root)).replace("\\", "/")
            if trusted_raw_data_root != (project_root / "raw_data").resolve() else None
        ),
    )
