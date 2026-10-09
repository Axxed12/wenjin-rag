"""查询已构建的 dense collection，并打印来源行号。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.support.search_display import format_human_result  # noqa: E402


def main() -> None:
    from wenjin.indexing.dense import dense_query
    from wenjin.qdrant_connection import create_qdrant_client
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--raw", action="store_true", help="输出完整 JSON，便于程序调试")
    args = parser.parse_args()
    settings = Settings()
    client = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
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
