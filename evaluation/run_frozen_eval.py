"""对已冻结OSG测试集做原query、规则扩展与0.6B重排评测。

本脚本不调用外接改写API；不会依据测试结果修改查询词表或模型指令。
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
DATASET = ROOT / "evaluation/datasets/osg-3_6_5-frozen-001.jsonl"


def load_and_validate_dataset(path: Path) -> tuple[list[dict], str]:
    """在评测前检查题目证据确实来自当前固定语料，并计算文件指纹。"""
    from ingest import load_documents

    raw = path.read_bytes()
    seeds = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    docs = {
        document["source_path"]: document
        for document in load_documents(ROOT / "corpora/osg-3.6.5/source-manifest.json")
    }
    if not seeds or len({seed["id"] for seed in seeds}) != len(seeds):
        raise ValueError("测试集不能为空，题目ID也不能重复")
    for seed in seeds:
        if seed.get("split") != "frozen" or seed.get("version") != "3.6.5":
            raise ValueError(f"{seed['id']}的split或版本不符合冻结集")
        if not seed.get("query") or not seed.get("evidence"):
            raise ValueError(f"{seed['id']}缺少问题或证据")
        for span in seed["evidence"]:
            document = docs.get(span["path"])
            if document is None:
                raise ValueError(f"{seed['id']}的证据文件未入库：{span['path']}")
            line_count = len(document["text"].splitlines())
            if not 1 <= span["start_line"] <= span["end_line"] <= line_count:
                raise ValueError(f"{seed['id']}的证据行号越界：{span}")
    return seeds, hashlib.sha256(raw).hexdigest()


def main() -> None:
    from qdrant_client import QdrantClient

    from evaluation.index_assets import active_index_assets
    from evaluation.run_query_rewrite_eval import retrieve_one_query
    from evaluation.run_reranker_eval import expanded_candidates
    from evaluation.run_retrieval_eval import metrics, summarize
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.retrieval.rerank import Qwen3Reranker, candidate_document, rank_candidates
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default="osg-frozen-001-0_6b")
    parser.add_argument("--no-reranker", action="store_true")
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument(
        "--reranker-model-path",
        type=Path,
        help="默认使用.env中的0.6B路径；4B对比时显式传入本地模型目录",
    )
    args = parser.parse_args()
    if not all(character.isalnum() or character in "-_" for character in args.run_id):
        raise ValueError("run-id只能包含字母、数字、连字符和下划线")
    seeds, dataset_hash = load_and_validate_dataset(DATASET)
    settings = Settings()
    reranker_model_path = args.reranker_model_path or settings.reranker_model_path
    if not args.no_reranker and not reranker_model_path.is_dir():
        raise FileNotFoundError(f"Reranker模型目录不存在：{reranker_model_path}")
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    target, database = active_index_assets(ROOT)
    prepared = []
    started = time.perf_counter()
    try:
        for seed in seeds:
            original, _, _ = retrieve_one_query(
                seed["query"],
                route_name="original",
                client=client,
                embedder=embedder,
                collection=target.dense_collection,
                database=database,
                candidate_k=20,
                source_prefixes=[],
            )
            baseline = original[:20]
            expanded = expanded_candidates(
                seed["query"], client=client, embedder=embedder,
                collection=target.dense_collection, database=database
            )
            combined = rrf_fuse(
                {"baseline_pool": baseline, "expanded_pool": expanded}, k=60
            )[:20]
            prepared.append({"seed": seed, "pools": {
                "baseline": baseline,
                "expanded": expanded,
                "combined": combined,
            }})
    finally:
        client.close()
    retrieval_seconds = time.perf_counter() - started
    del embedder
    gc.collect()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
    reranker = None
    if not args.no_reranker:
        reranker = Qwen3Reranker(
            reranker_model_path,
            device="cuda" if torch.cuda.is_available() else "cpu",
            max_length=args.max_length,
            batch_size=args.batch_size,
        )
    details = []
    rerank_started = time.perf_counter()
    try:
        for item in prepared:
            seed = item["seed"]
            pools = item["pools"]
            strategies = {f"{name}_raw": results for name, results in pools.items()}
            if reranker:
                unique = {
                    result["chunk_id"]: result
                    for results in pools.values() for result in results
                }
                candidates = list(unique.values())
                scores = reranker.score(
                    seed["query"],
                    [candidate_document(candidate) for candidate in candidates],
                )
                score_by_id = dict(zip(unique, scores))
                strategies.update({
                    f"{name}_reranked": rank_candidates(
                        results, [score_by_id[result["chunk_id"]] for result in results]
                    )
                    for name, results in pools.items()
                })
            details.append({
                "id": seed["id"],
                "query": seed["query"],
                "evidence": seed["evidence"],
                "metrics": {
                    name: {f"at_{k}": metrics(results, seed["evidence"], k=k)
                           for k in (5, 20)}
                    for name, results in strategies.items()
                },
                "top_ids": {
                    name: [result["chunk_id"] for result in results[:5]]
                    for name, results in strategies.items()
                },
                "candidate_ids": {
                    name: [result["chunk_id"] for result in results]
                    for name, results in pools.items()
                },
            })
    finally:
        peak_cuda_memory = (
            torch.cuda.max_memory_allocated() if torch.cuda.is_available() else None
        )
        if reranker:
            reranker.close()
    rerank_seconds = time.perf_counter() - rerank_started
    names = tuple(details[0]["metrics"])
    candidate_signature = json.dumps(
        [{"id": item["id"], "candidate_ids": item["candidate_ids"]} for item in details],
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    report = {
        "run_id": args.run_id,
        "dataset": str(DATASET.relative_to(ROOT)).replace("\\", "/"),
        "dataset_sha256": dataset_hash,
        "dataset_role": "assistant-authored source-checked frozen set; no tuning on results",
        "candidate_k": 20,
        "rrf_k": 60,
        "reranker_model": None if args.no_reranker else reranker_model_path.name,
        "max_length": args.max_length,
        "batch_size": args.batch_size,
        "candidate_pool_sha256": hashlib.sha256(candidate_signature).hexdigest(),
        "retrieval_seconds": round(retrieval_seconds, 3),
        "rerank_seconds": round(rerank_seconds, 3),
        "peak_cuda_memory_bytes": peak_cuda_memory,
        "summary": {
            name: {f"at_{k}": summarize(details, name, k) for k in (5, 20)}
            for name in names
        },
        "details": details,
    }
    output = ROOT / f"artifacts/runs/{args.run_id}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
