"""查询已构建的 dense collection，并打印来源行号。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def format_human_result(query: str, strategy: str, results: list[dict]) -> str:
    """把机器结果压缩成适合人先阅读的检索摘要。"""
    lines = [
        f"查询：{query}",
        f"切分策略：{strategy}",
        f"召回结果：{len(results)} 条",
        "说明：score 是向量相似度排序分数，不是答案正确率。",
    ]
    for index, result in enumerate(results, start=1):
        text = " ".join(str(result.get("text", "")).split())
        preview = text[:360] + ("..." if len(text) > 360 else "")
        lines.extend(
            [
                "",
                f"[{index}] score={result['score']:.5f}",
                f"来源：{result.get('source_path')}:{result.get('start_line')}-{result.get('end_line')}",
                f"版本：{result.get('version')}  策略：{result.get('strategy')}",
                f"chunk_id：{result.get('chunk_id')}",
                f"正文预览：{preview}",
            ]
        )
    return "\n".join(lines)

def main() -> None:
    from qdrant_client import QdrantClient

    from wenjin.indexing.dense import dense_query
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--raw", action="store_true", help="输出完整 JSON，便于程序调试")
    args = parser.parse_args()
    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        points = dense_query(client, settings.embedding(), f"osg_3_6_5_dense_{args.strategy}", args.query, top_k=args.top_k)
        result = [
            {"score": round(point.score, 5), **point.payload}
            for point in points
        ]
    finally:
        client.close()
    if args.raw:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_human_result(args.query, args.strategy, result))


if __name__ == "__main__":
    main()
