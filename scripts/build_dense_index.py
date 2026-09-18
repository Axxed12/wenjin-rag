"""构建 OSG 文档的 dense Qdrant 索引。

示例：
    uv run python scripts/build_dense_index.py --strategy blocks
    uv run python scripts/build_dense_index.py --strategy fixed
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main() -> None:
    from qdrant_client import QdrantClient

    from ingest import load_documents
    from wenjin.index_registry import IndexRegistryStore
    from wenjin.indexing.dense import create_dense_collection, embed_chunks
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    args = parser.parse_args()
    settings = Settings()
    documents = load_documents(ROOT / "corpora/osg-3.6.5/source-manifest.json")
    chunks = []
    for document in documents:
        if args.strategy == "fixed":
            chunks.extend(fixed_chunks(document, size=1200, overlap=120))
        else:
            chunks.extend(block_chunks(document, max_chars=1600))
    active = IndexRegistryStore(ROOT / "data/indexes/index-registry.json").active()
    collection = (
        active.dense_collection
        if args.strategy == active.strategy
        else f"osg_3_6_5_dense_{args.strategy}"
    )
    started = time.perf_counter()
    embedder = settings.embedding()
    vectors = embed_chunks(embedder, chunks)
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        create_dense_collection(client, collection, chunks, vectors)
        report = {
            "collection": collection,
            "strategy": args.strategy,
            "source_version": "3.6.5",
            "index_target_id": active.target_id if args.strategy == active.strategy else None,
            "chunk_count": len(chunks),
            "vector_dimension": len(vectors[0]),
            "seconds": round(time.perf_counter() - started, 3),
            "model_path": str(settings.embedding_model_path),
            "note": "Dense index build only; no retrieval quality claim.",
        }
    finally:
        client.close()
    output = ROOT / "artifacts/runs" / f"dense-index-{args.strategy}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
