"""运行 dense + BM25，并用 RRF 融合两个候选列表。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def qdrant_points_to_dicts(points: list) -> list[dict]:
    return [{"score": point.score, **point.payload} for point in points]


def format_results(query: str, results: list[dict]) -> str:
    lines = [
        f"查询：{query}",
        f"RRF 结果：{len(results)} 条",
        "说明：RRF 只使用名次；dense/BM25 原始分数没有直接相加。",
    ]
    for index, result in enumerate(results, start=1):
        preview = " ".join(result["text"].split())[:300]
        lines.extend(
            [
                "",
                f"[{index}] rrf={result['rrf_score']:.6f} ranks={result['ranks']}",
                f"来源：{result['source_path']}:{result['start_line']}-{result['end_line']}",
                f"chunk_id：{result['chunk_id']}",
                f"正文预览：{preview}...",
            ]
        )
    return "\n".join(lines)


def main() -> None:
    from qdrant_client import QdrantClient

    from wenjin.indexing.bm25 import bm25_query
    from wenjin.indexing.dense import dense_query
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        dense = qdrant_points_to_dicts(
            dense_query(
                client,
                settings.embedding(),
                f"osg_3_6_5_dense_{args.strategy}",
                args.query,
                top_k=args.candidate_k,
            )
        )
    finally:
        client.close()
    bm25 = bm25_query(
        ROOT / "data/indexes" / f"osg_3_6_5_bm25_{args.strategy}.sqlite",
        args.query,
        top_k=args.candidate_k,
    )
    fused = rrf_fuse({"dense": dense, "bm25": bm25}, k=args.rrf_k)[: args.top_k]
    if args.raw:
        print(json.dumps(fused, ensure_ascii=False, indent=2))
    else:
        print(format_results(args.query, fused))


if __name__ == "__main__":
    main()
