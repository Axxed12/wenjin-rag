"""在固定开发集候选池上评测本地 Qwen3-Reranker-0.6B。

Query 英译读取已保存报告，不重新调用外部API；这样候选变化不会和reranker变化混在一起。
"""

import argparse
import gc
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def expanded_candidates(
    query: str, *, client, embedder, collection: str, database: Path
) -> list[dict]:
    """复现现有开发集规则扩展分路，返回RRF后的Top-20。"""
    from scripts.query_hybrid import qdrant_points_to_dicts
    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.retrieval.query_expansion import (
        code_identifier_terms,
        expand_query,
        source_routes,
    )

    expanded_query, _ = expand_query(query)
    dense = qdrant_points_to_dicts(
        dense_query(
            client,
            embedder,
            collection,
            expanded_query,
            top_k=20,
        )
    )
    terms = query_terms(expanded_query)
    bm25 = bm25_query(database, expanded_query, top_k=20) if terms else []
    identifiers = code_identifier_terms(terms)
    symbol = (
        bm25_query(
            database,
            expanded_query,
            top_k=20,
            terms_override=identifiers,
            source_prefix="include/%",
        )
        if identifiers
        else []
    )
    routed = {
        f"route_{index}": bm25_query(
            database,
            expanded_query,
            top_k=20,
            terms_override=terms,
            source_prefix=prefix,
        )
        for index, prefix in enumerate(source_routes(query))
        if terms
    }
    return rrf_fuse(
        {
            "dense_expanded": dense,
            "bm25_expanded": bm25,
            "symbol_headers": symbol,
            **routed,
        },
        k=60,
    )[:20]


def main() -> None:
    from qdrant_client import QdrantClient

    from evaluation.index_assets import active_index_assets
    from evaluation.run_query_rewrite_eval import retrieve_one_query
    from evaluation.run_retrieval_eval import metrics, summarize
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.retrieval.rerank import (
        DEFAULT_INSTRUCTION,
        STRICT_SOURCE_INSTRUCTION,
        Qwen3Reranker,
        candidate_document,
        rank_candidates,
    )
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rewrite-report",
        type=Path,
        default=ROOT / "artifacts/runs/query-rewrite-eval-api-dev-10-001.json",
    )
    parser.add_argument("--run-id", default="reranker-0_6b-dev-001")
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--instruction-profile",
        choices=["default", "strict-source"],
        default="default",
    )
    args = parser.parse_args()
    if not all(character.isalnum() or character in "-_" for character in args.run_id):
        raise ValueError("run-id 只能包含字母、数字、连字符和下划线")
    if args.candidate_k != 20:
        raise ValueError("当前保存的英译实验固定candidate-k=20；本轮必须保持20")

    rewrite_bytes = args.rewrite_report.read_bytes()
    rewrite_report = json.loads(rewrite_bytes)
    rewrite_by_id = {item["id"]: item["rewrite"] for item in rewrite_report["details"]}
    seeds = load_jsonl(ROOT / "corpora/osg-3.6.5/dev-seed.jsonl")
    missing = [seed["id"] for seed in seeds if seed["id"] not in rewrite_by_id]
    if missing:
        raise ValueError(f"英译报告缺少开发题：{missing}")

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    settings = Settings()
    target, database = active_index_assets(ROOT)
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    retrieval_started = time.perf_counter()
    prepared = []
    try:
        for seed in seeds:
            original, _, _ = retrieve_one_query(
                seed["query"],
                route_name="original",
                client=client,
                embedder=embedder,
                collection=target.dense_collection,
                database=database,
                candidate_k=args.candidate_k,
                source_prefixes=[],
            )
            english_query = rewrite_by_id[seed["id"]]["english_query"]
            english, _, _ = retrieve_one_query(
                english_query,
                route_name="english",
                client=client,
                embedder=embedder,
                collection=target.dense_collection,
                database=database,
                candidate_k=args.candidate_k,
                source_prefixes=[],
            )
            baseline = original[: args.candidate_k]
            translation = rrf_fuse(
                {"original_query": original, "english_query": english},
                k=60,
            )[: args.candidate_k]
            expanded = expanded_candidates(
                seed["query"],
                client=client,
                embedder=embedder,
                collection=target.dense_collection,
                database=database,
            )
            combined = rrf_fuse(
                {
                    "baseline_pool": baseline,
                    "translation_pool": translation,
                    "expanded_pool": expanded,
                },
                k=60,
            )[: args.candidate_k]
            prepared.append(
                {
                    "seed": seed,
                    "english_query": english_query,
                    "pools": {
                        "baseline": baseline,
                        "translation": translation,
                        "expanded": expanded,
                        "combined": combined,
                    },
                }
            )
    finally:
        client.close()
    retrieval_seconds = time.perf_counter() - retrieval_started

    # 候选已全部保存在内存中；释放Embedding后再加载Reranker，避免两个模型同时占显存。
    del embedder
    gc.collect()
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()

    load_started = time.perf_counter()
    instruction = (
        STRICT_SOURCE_INSTRUCTION
        if args.instruction_profile == "strict-source"
        else DEFAULT_INSTRUCTION
    )
    reranker = Qwen3Reranker(
        settings.reranker_model_path,
        device="cuda" if torch.cuda.is_available() else "cpu",
        max_length=args.max_length,
        batch_size=args.batch_size,
        instruction=instruction,
    )
    model_load_seconds = time.perf_counter() - load_started
    details = []
    rerank_started = time.perf_counter()
    try:
        for item in prepared:
            seed = item["seed"]
            unique_candidates = {}
            for pool in item["pools"].values():
                for candidate in pool:
                    unique_candidates.setdefault(candidate["chunk_id"], candidate)
            unique_list = list(unique_candidates.values())
            question_started = time.perf_counter()
            scores = reranker.score(
                seed["query"],
                [candidate_document(candidate) for candidate in unique_list],
            )
            score_by_id = {
                candidate["chunk_id"]: score
                for candidate, score in zip(unique_list, scores)
            }
            rerank_seconds = time.perf_counter() - question_started

            strategies = {}
            for pool_name, pool in item["pools"].items():
                strategies[f"{pool_name}_raw"] = pool
                strategies[f"{pool_name}_reranked"] = rank_candidates(
                    pool,
                    [score_by_id[candidate["chunk_id"]] for candidate in pool],
                )
            details.append(
                {
                    "id": seed["id"],
                    "query": seed["query"],
                    "english_query": item["english_query"],
                    "evidence": seed["evidence"],
                    "unique_candidates_scored": len(unique_list),
                    "rerank_seconds": round(rerank_seconds, 3),
                    "metrics": {
                        name: {
                            "at_5": metrics(results, seed["evidence"], k=args.top_k),
                            "at_20": metrics(results, seed["evidence"], k=args.candidate_k),
                        }
                        for name, results in strategies.items()
                    },
                    "top_ids": {
                        name: [candidate["chunk_id"] for candidate in results[: args.top_k]]
                        for name, results in strategies.items()
                    },
                    "reranked_top_scores": {
                        name: [round(candidate["rerank_score"], 6) for candidate in results[: args.top_k]]
                        for name, results in strategies.items()
                        if name.endswith("_reranked")
                    },
                }
            )
    finally:
        peak_memory_bytes = (
            torch.cuda.max_memory_allocated() if torch.cuda.is_available() else None
        )
        reranker.close()
    rerank_seconds = time.perf_counter() - rerank_started

    strategy_names = tuple(details[0]["metrics"])
    report = {
        "run_id": args.run_id,
        "dataset": "corpora/osg-3.6.5/dev-seed.jsonl",
        "dataset_role": "development diagnostics; source-selected and not held out",
        "strategy": "blocks",
        "candidate_k": args.candidate_k,
        "top_k": args.top_k,
        "reranker": {
            "model": settings.reranker_model_path.name,
            "max_length": args.max_length,
            "batch_size": args.batch_size,
            "instruction_profile": args.instruction_profile,
            "instruction": instruction,
            "model_load_seconds": round(model_load_seconds, 3),
            "rerank_seconds": round(rerank_seconds, 3),
            "peak_cuda_memory_bytes": peak_memory_bytes,
        },
        "rewrite_source": {
            "path": str(args.rewrite_report.relative_to(ROOT)).replace("\\", "/"),
            "sha256": hashlib.sha256(rewrite_bytes).hexdigest(),
            "note": "saved English rewrites reused; no external API call in this run",
        },
        "retrieval_seconds": round(retrieval_seconds, 3),
        "summary": {
            name: {f"at_{k}": summarize(details, name, k) for k in (5, 20)}
            for name in strategy_names
        },
        "details": details,
    }
    output = ROOT / f"artifacts/runs/{args.run_id}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(json.dumps(report["reranker"], ensure_ascii=False, indent=2))
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
