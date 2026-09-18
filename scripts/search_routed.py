"""通过跨库路由查询5个新语料候选索引，只打印证据，不生成答案。

这是本地只读检查入口；MCP的search_docs仍只服务OSG active索引。
运行：uv run python scripts/search_routed.py "projsync 支持哪些参数" --library PROJ
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="跨库路由检索（只读，不生成答案）")
    parser.add_argument("query", nargs="?", help="检索问题；--catalog时不使用")
    parser.add_argument("--library", help="显式指定library；省略则受预算约束跨库扇出")
    parser.add_argument("--version")
    parser.add_argument("--route", type=Path,
                        default=ROOT / "configs/indexes/multicorpus-candidates-002.json")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-k", type=int, default=10)
    parser.add_argument("--max-libraries", type=int, default=5)
    parser.add_argument("--context-char-budget", type=int, default=8000)
    parser.add_argument("--no-reranker", action="store_true")
    parser.add_argument("--catalog", action="store_true", help="只列出路由中的库和版本")
    parser.add_argument("--raw", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from qdrant_client import QdrantClient

    from wenjin.retrieval.rerank import Qwen3Reranker
    from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig
    from wenjin.settings import Settings

    settings = Settings()
    # 一个进程内只加载一次模型和客户端。
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    reranker = None
    router = None
    try:
        if not args.no_reranker:
            reranker = Qwen3Reranker(
                settings.reranker_model_path,
                device="cuda" if torch.cuda.is_available() else "cpu",
                max_length=1024,
                batch_size=2,
            )
        router = MultiCorpusRouter.from_route_file(
            args.route.resolve(),
            root=ROOT,
            qdrant_client=client,
            embedder=settings.embedding(),
            reranker=reranker,
        )
        if args.catalog:
            print(json.dumps(router.catalog(), ensure_ascii=False, indent=2))
            return
        if not args.query:
            parser.error("必须提供检索问题（或使用 --catalog）")
        result = router.search(
            args.query,
            library=args.library,
            version=args.version,
            config=RouteConfig(
                candidate_k=args.candidate_k,
                top_k=args.top_k,
                max_libraries=args.max_libraries,
                context_char_budget=args.context_char_budget,
            ),
        )
    finally:
        if router is not None:
            router.close()
        else:
            client.close()
            if reranker is not None:
                reranker.close()

    if args.raw:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"问题：{result['query']}")
    print(f"路由：{[item['library'] + ' ' + item['version'] for item in result['routed_libraries']]}")
    if result["skipped_libraries"]:
        print(f"因预算跳过的库：{result['skipped_libraries']}")
    print(f"证据：{len(result['evidence'])} 条；reranker={result['retrieval']['reranker_used']}")
    print("说明：分数只用于排序；本入口不判断证据是否充分。")
    for evidence in result["evidence"]:
        location = (
            f"page {evidence['page_number']}" if evidence["page_number"]
            else f"{evidence['start_line']}-{evidence['end_line']}"
        )
        print(f"\n[{evidence['rank']}] {evidence['library']} {evidence['version']} "
              f"{evidence['source_path']}:{location}")
        print(f"chunk_id：{evidence['chunk_id']}")
        print(f"来源：{evidence['source_url']}")
        print(f"正文预览：{' '.join(evidence['text'].split())[:320]}...")


if __name__ == "__main__":
    main()
