"""比较原 query、英文改写和子问题拆解对检索的影响。

默认 fixture 是针对已知失败编写的诊断输入，只能验证链路潜力。填好 .env 后使用
``--backend api`` 才能测指定模型；之后还必须在冻结测试集上复验。
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CORPUS_HINT = (
    "OpenSceneGraph 3.6.5 English C++ headers, implementations, and official examples"
)


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def retrieve_one_query(
    query: str,
    *,
    route_name: str,
    client,
    embedder,
    collection: str,
    database: Path,
    candidate_k: int,
    source_prefixes: list[str],
) -> tuple[list[dict], list[dict], dict]:
    """对一条 query 做 dense+BM25；中文无英文词时，BM25 分路为空而非报错。"""
    from scripts.query_hybrid import qdrant_points_to_dicts
    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query
    from wenjin.retrieval.fusion import rrf_fuse

    dense = qdrant_points_to_dicts(
        dense_query(
            client,
            embedder,
            collection,
            query,
            top_k=candidate_k,
        )
    )
    terms = query_terms(query)
    bm25 = bm25_query(database, query, top_k=candidate_k) if terms else []
    base_fused = rrf_fuse(
        {f"{route_name}_dense": dense, f"{route_name}_bm25": bm25},
        k=60,
    )
    routed_bm25 = {
        f"{route_name}_source_{index}": bm25_query(
            database,
            query,
            top_k=candidate_k,
            terms_override=terms,
            source_prefix=prefix,
        )
        for index, prefix in enumerate(source_prefixes)
        if terms
    }
    routed_fused = rrf_fuse(
        {
            f"{route_name}_dense": dense,
            f"{route_name}_bm25": bm25,
            **routed_bm25,
        },
        k=60,
    )
    trace = {
        "query": query,
        "bm25_terms": terms,
        "top_ids": [item["chunk_id"] for item in base_fused[:5]],
        "routed_top_ids": [item["chunk_id"] for item in routed_fused[:5]],
    }
    return base_fused, routed_fused, trace


def fixture_rewrite(seed: dict, fixtures: dict[str, dict]):
    from wenjin.retrieval.query_rewrite import QueryRewrite

    fixture = fixtures.get(seed["id"])
    if fixture is None:
        raise KeyError(f"fixture 中没有 {seed['id']}")
    return QueryRewrite(
        original_query=seed["query"],
        english_query=fixture["english_query"],
        subqueries=tuple(fixture["subqueries"]),
    )


def main() -> None:
    from qdrant_client import QdrantClient

    from evaluation.index_assets import active_index_assets
    from evaluation.run_retrieval_eval import metrics, summarize
    from wenjin.retrieval.fusion import interleave_unique, rrf_fuse
    from wenjin.retrieval.query_expansion import source_routes
    from wenjin.retrieval.query_rewrite import (
        OpenAIQueryRewriter,
        QueryRewriteSettings,
        rewrite_or_fallback,
    )
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["fixture", "api"], default="fixture")
    parser.add_argument("--ids", nargs="+", default=["osg-dev-002", "osg-dev-008"])
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument(
        "--run-id",
        help="可复现实验编号；指定后用于输出文件名，避免重复实验互相覆盖",
    )
    args = parser.parse_args()
    suffix = "api" if args.backend == "api" else "fixture"
    run_id = args.run_id or f"dev-failures-{suffix}"
    if not all(character.isalnum() or character in "-_" for character in run_id):
        raise ValueError("run-id 只能包含字母、数字、连字符和下划线")

    seeds_by_id = {
        item["id"]: item for item in load_jsonl(ROOT / "corpora/osg-3.6.5/dev-seed.jsonl")
    }
    missing = [seed_id for seed_id in args.ids if seed_id not in seeds_by_id]
    if missing:
        raise ValueError(f"开发集中没有这些 ID：{missing}")
    seeds = [seeds_by_id[seed_id] for seed_id in args.ids]
    fixture_path = ROOT / "evaluation/fixtures/query-rewrite-dev-failures.jsonl"
    fixtures = {item["id"]: item for item in load_jsonl(fixture_path)}

    model_name = None
    rewriter = None
    if args.backend == "api":
        rewrite_settings = QueryRewriteSettings()
        rewriter = OpenAIQueryRewriter(rewrite_settings)
        model_name = rewrite_settings.query_rewrite_model

    settings = Settings()
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    target, database = active_index_assets(ROOT)
    details = []
    started = time.perf_counter()
    try:
        for seed in seeds:
            rewrite_started = time.perf_counter()
            rewrite_error_type = None
            if args.backend == "api":
                rewrite, rewrite_error_type = rewrite_or_fallback(
                    rewriter,
                    seed["query"],
                    corpus_hint=CORPUS_HINT,
                )
            else:
                rewrite = fixture_rewrite(seed, fixtures)
            rewrite_seconds = time.perf_counter() - rewrite_started

            per_query = {}
            per_query_routed = {}
            traces = []
            routes = source_routes(seed["query"])
            for index, query in enumerate(rewrite.retrieval_queries()):
                route_name = f"q{index}"
                result, routed_result, trace = retrieve_one_query(
                    query,
                    route_name=route_name,
                    client=client,
                    embedder=embedder,
                    collection=target.dense_collection,
                    database=database,
                    candidate_k=args.candidate_k,
                    source_prefixes=routes,
                )
                per_query[route_name] = result
                per_query_routed[route_name] = routed_result
                trace["evidence_at_20"] = metrics(result, seed["evidence"], k=20)
                traces.append({"route": route_name, **trace})

            original_fusion = per_query["q0"]
            translation_fusion = rrf_fuse(
                {name: per_query[name] for name in ("q0", "q1")},
                k=60,
            )
            decomposition_rrf = rrf_fuse(per_query, k=60)
            decomposition_interleave = interleave_unique(per_query)
            routed_decomposition_interleave = interleave_unique(per_query_routed)
            strategies = {
                "original_fusion": original_fusion,
                "translation_fusion": translation_fusion,
                "decomposition_rrf": decomposition_rrf,
                "decomposition_interleave": decomposition_interleave,
                "routed_decomposition_interleave": routed_decomposition_interleave,
            }
            details.append(
                {
                    "id": seed["id"],
                    "query": seed["query"],
                    "rewrite": {
                        "english_query": rewrite.english_query,
                        "subqueries": list(rewrite.subqueries),
                        "seconds": round(rewrite_seconds, 3),
                        "fallback_error_type": rewrite_error_type,
                    },
                    "source_routes": routes,
                    "evidence": seed["evidence"],
                    "query_traces": traces,
                    "metrics": {
                        name: {
                            "at_5": metrics(results, seed["evidence"], k=5),
                            "at_20": metrics(results, seed["evidence"], k=20),
                        }
                        for name, results in strategies.items()
                    },
                    "top_ids": {
                        name: [item["chunk_id"] for item in results[:5]]
                        for name, results in strategies.items()
                    },
                }
            )
    finally:
        client.close()

    strategy_names = (
        "original_fusion",
        "translation_fusion",
        "decomposition_rrf",
        "decomposition_interleave",
        "routed_decomposition_interleave",
    )
    report = {
        "dataset": "corpora/osg-3.6.5/dev-seed.jsonl",
        "selected_ids": args.ids,
        "dataset_role": "known development failures; not held out",
        "rewrite_backend": args.backend,
        "rewrite_model": model_name,
        "fixture_role": (
            "hand-authored diagnostic; not model output" if args.backend == "fixture" else None
        ),
        "candidate_k_per_query": args.candidate_k,
        "seconds": round(time.perf_counter() - started, 3),
        "summary": {
            name: {f"at_{k}": summarize(details, name, k) for k in (5, 20)}
            for name in strategy_names
        },
        "details": details,
    }
    report["run_id"] = run_id
    output = ROOT / f"artifacts/runs/query-rewrite-eval-{run_id}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
