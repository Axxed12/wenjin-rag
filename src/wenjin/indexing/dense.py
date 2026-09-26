"""使用 LlamaIndex 的 Embedding 适配器，把 chunk 写入 Qdrant。"""

import uuid

from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from qdrant_client import QdrantClient, models


def embed_chunks(
    embedder: HuggingFaceEmbedding, chunks: list, *, batch_size: int = 16
) -> list[list[float]]:
    """按批次计算文档向量；返回顺序与 chunks 完全一致。"""
    vectors = []
    for start in range(0, len(chunks), batch_size):
        texts = [chunk.text for chunk in chunks[start : start + batch_size]]
        vectors.extend(embedder.get_text_embedding_batch(texts, show_progress=False))
    if len(vectors) != len(chunks):
        raise RuntimeError("Embedding 数量与 chunk 数量不一致")
    return vectors


def create_dense_collection(
    client: QdrantClient,
    collection_name: str,
    chunks: list,
    vectors: list[list[float]],
    *,
    batch_size: int = 64,
) -> None:
    """创建一个独立 collection，并保存向量及可追溯 payload。"""
    if not chunks or len(chunks) != len(vectors):
        raise ValueError("chunks 与 vectors 必须非空且长度一致")
    client.recreate_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE),
    )
    for start in range(0, len(chunks), batch_size):
        points = []
        for chunk, vector in zip(chunks[start : start + batch_size], vectors[start : start + batch_size]):
            points.append(
                models.PointStruct(
                    # UUID5 让同一个 chunk 在重跑时得到稳定 ID，不依赖列表顺序。
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.chunk_id)),
                    vector=vector,
                    payload={
                        "chunk_id": chunk.chunk_id,
                        "source_path": chunk.source_path,
                        "version": chunk.version,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "strategy": chunk.strategy,
                        "text": chunk.text,
                        "content_sha256": chunk.content_sha256,
                        "source_raw_sha256": chunk.source_raw_sha256,
                        "source_canonical_sha256": chunk.source_canonical_sha256,
                        "normalization": chunk.normalization,
                        "page_number": chunk.page_number,
                        "logical_document_id": chunk.logical_document_id,
                        # OCR 标记必须随向量一起存：证据要能说明正文是识别来的。
                        "parser": chunk.parser,
                        "ocr_derived": chunk.ocr_derived,
                        # 父子化索引里命中 child 后要换成父块返回；父块 ID 必须两侧都有，
                        # 否则向量侧命中的候选换不到父块（审计也会逐字段比这一列）。
                        "parent_id": chunk.parent_id,
                    },
                )
            )
        client.upsert(collection_name=collection_name, points=points, wait=True)


def dense_query_vector(
    client: QdrantClient,
    vector: list[float],
    collection_name: str,
    *,
    top_k: int = 5,
) -> list:
    """用已编码好的 query 向量查询单个 collection；跨库扇出时只编码一次。"""
    return client.query_points(collection_name, query=vector, limit=top_k).points


def dense_query(
    client: QdrantClient,
    embedder: HuggingFaceEmbedding,
    collection_name: str,
    query: str,
    *,
    top_k: int = 5,
) -> list:
    """将 query 编码后查询 Qdrant；结果按相似度降序返回。"""
    vector = embedder.get_query_embedding(query)
    return dense_query_vector(client, vector, collection_name, top_k=top_k)
