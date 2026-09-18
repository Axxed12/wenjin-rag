"""查询 FTS5/BM25 索引；默认打印摘要，--raw 输出完整 JSON。"""

import argparse
import json
from pathlib import Path

from query_dense_index import format_human_result

from wenjin.indexing.bm25 import bm25_query

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()
    path = ROOT / "data/indexes" / f"osg_3_6_5_bm25_{args.strategy}.sqlite"
    results = bm25_query(path, args.query, top_k=args.top_k)
    if args.raw:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_human_result(args.query, args.strategy, results).replace("向量相似度排序分数", "BM25 排名分数"))


if __name__ == "__main__":
    main()
