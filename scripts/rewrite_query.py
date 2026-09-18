"""调用配置在 .env 中的 OpenAI 兼容模型，并展示 query 改写结果。"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from wenjin.retrieval.query_rewrite import (
        OpenAIQueryRewriter,
        QueryRewriteSettings,
        rewrite_or_fallback,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="需要改写的原始问题")
    parser.add_argument(
        "--corpus-hint",
        default="OpenSceneGraph 3.6.5 English C++ headers, implementations, and examples",
        help="告诉模型目标语料的语言和类型，不提供答案",
    )
    parser.add_argument("--max-subqueries", type=int, default=4)
    args = parser.parse_args()

    settings = QueryRewriteSettings()
    rewriter = OpenAIQueryRewriter(settings)
    result, error_type = rewrite_or_fallback(
        rewriter,
        args.query,
        corpus_hint=args.corpus_hint,
        max_subqueries=args.max_subqueries,
    )
    # 只展示模型名、query 和错误类型；不输出地址或 API key。
    output = {
        "model": settings.query_rewrite_model,
        "original_query": result.original_query,
        "english_query": result.english_query,
        "subqueries": list(result.subqueries),
        "retrieval_queries": result.retrieval_queries(),
        "fallback_error_type": error_type,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
