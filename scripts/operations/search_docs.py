"""通过正式 RetrievalService 查询 OSG 文档并打印简短证据。"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def format_search_result(result: dict) -> str:
    lines = [
        f"问题：{result['query']}",
        f"版本：{result['version']}；证据：{len(result['evidence'])} 条",
        f"分路：dense={result['retrieval']['dense_returned']}，"
        f"BM25={result['retrieval']['bm25_returned']}；"
        f"reranker={result['retrieval']['reranker_used']}",
        "说明：检索分数只用于排序；当前入口不生成最终答案。",
    ]
    for evidence in result["evidence"]:
        preview = " ".join(evidence["text"].split())[:320]
        lines.extend([
            "",
            f"[{evidence['rank']}] {evidence['source_path']}:"
            f"{evidence['start_line']}-{evidence['end_line']}",
            f"chunk_id：{evidence['chunk_id']}",
            f"来源：{evidence['source_url']}",
            f"RRF={evidence['rrf_score']:.6f}；"
            f"Reranker={evidence['rerank_score'] if evidence['rerank_score'] is not None else '未启用'}",
            f"正文预览：{preview}...",
        ])
    return "\n".join(lines)


def main() -> None:
    from wenjin.mcp_server import create_default_service
    from wenjin.retrieval.service import SearchConfig

    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--context-char-budget", type=int, default=8000)
    parser.add_argument("--no-reranker", action="store_true")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    service = create_default_service(use_reranker=not args.no_reranker)
    try:
        result = service.search(
            args.query,
            config=SearchConfig(
                top_k=args.top_k,
                context_char_budget=args.context_char_budget,
            ),
        )
    finally:
        service.close()
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.raw else format_search_result(result))


if __name__ == "__main__":
    main()
