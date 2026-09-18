"""定位开发题检索失败：先检查原文证据是否入库，再检查候选排序。

运行：uv run python evaluation/inspect_misses.py
此诊断不改变索引，也不把缺证据误归因于生成模型。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def evidence_in_corpus(chunks: list, span: dict) -> dict:
    """判断完整索引中的 chunk 能否联合覆盖指定原文证据范围。"""
    from evaluation.run_retrieval_eval import covers_span

    candidates = [
        {
            "chunk_id": chunk.chunk_id,
            "source_path": chunk.source_path,
            "start_line": chunk.start_line,
            "end_line": chunk.end_line,
        }
        for chunk in chunks
        if chunk.source_path == span["path"]
        and chunk.start_line <= span["end_line"]
        and chunk.end_line >= span["start_line"]
    ]
    return {"fully_coverable": covers_span(candidates, span), "overlapping_chunks": candidates}


def main() -> None:
    from qdrant_client import QdrantClient

    from evaluation.index_assets import active_index_assets
    from ingest import load_documents
    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query
    from wenjin.ingestion.chunking import block_chunks
    from wenjin.settings import Settings

    seeds = [
        json.loads(line)
        for line in (ROOT / "corpora/osg-3.6.5/dev-seed.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    target = {seed["id"]: seed for seed in seeds if seed["id"] in {"osg-dev-002", "osg-dev-008"}}
    chunks = [
        chunk
        for document in load_documents(ROOT / "corpora/osg-3.6.5/source-manifest.json")
        for chunk in block_chunks(document, max_chars=1600)
    ]
    settings = Settings()
    active, database = active_index_assets(ROOT)
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    result = {}
    try:
        for seed_id, seed in target.items():
            dense = dense_query(
                client, embedder, active.dense_collection, seed["query"], top_k=20
            )
            terms = query_terms(seed["query"])
            bm25 = (
                bm25_query(
                    database,
                    seed["query"],
                    top_k=20,
                )
                if terms else []
            )
            result[seed_id] = {
                "query": seed["query"],
                "gold": [
                    {"span": span, "corpus": evidence_in_corpus(chunks, span)}
                    for span in seed["evidence"]
                ],
                "bm25_terms": terms,
                "dense_top20": [
                    {
                        "rank": rank,
                        "chunk_id": point.payload["chunk_id"],
                        "source_path": point.payload["source_path"],
                        "lines": [point.payload["start_line"], point.payload["end_line"]],
                        "score": round(point.score, 5),
                    }
                    for rank, point in enumerate(dense, start=1)
                ],
                "bm25_top20": [
                    {
                        "rank": rank,
                        "chunk_id": item["chunk_id"],
                        "source_path": item["source_path"],
                        "lines": [item["start_line"], item["end_line"]],
                        "score": round(item["score"], 5),
                    }
                    for rank, item in enumerate(bm25, start=1)
                ],
            }
    finally:
        client.close()
    output = ROOT / "artifacts/runs/retrieval-misses-blocks-dev.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for seed_id, item in result.items():
        print(seed_id, "bm25_terms=", item["bm25_terms"])
        for gold in item["gold"]:
            span = gold["span"]
            print(
                "  gold",
                span["path"],
                span["start_line"],
                span["end_line"],
                "corpus_coverable=",
                gold["corpus"]["fully_coverable"],
            )
        print("  dense_top_paths=", [entry["source_path"] for entry in item["dense_top20"][:5]])
        print("  bm25_top_paths=", [entry["source_path"] for entry in item["bm25_top20"][:5]])
    print("报告：", output)


if __name__ == "__main__":
    main()
