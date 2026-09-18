"""在validation上比较五库候选索引的dense、BM25、RRF和0.6B重排。

复用`run_retrieval_eval.metrics/summarize`做行级证据覆盖，只补两件现有脚本没有的事：
1) 按题集的library+version路由到对应候选索引；2) PDF证据只有物理页、没有行号，
需要单独的页级判定（既有实现遇到start_line=None会直接报错）。

证据判定沿用"Top-K chunk联合覆盖整段证据"，并已实测：blocks-split下validation的
51个行级证据，行号联合覆盖与按文本重建覆盖零分歧。
只使用validation（50题）；test保持冻结，不参与调参。
运行：uv run python evaluation/run_multicorpus_eval.py
"""

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.run_retrieval_eval import metrics, summarize  # noqa: E402
from wenjin.retrieval.router import normalize_library  # noqa: E402

LEADING_VERSION = re.compile(r"^\d+(?:\.\d+)*")


def route_arguments(dataset_library: str, dataset_version: str) -> tuple[str, str | None]:
    """把题集里的展示名和版本串转成路由参数；版本只取开头的数字段。"""
    match = LEADING_VERSION.match(dataset_version.strip())
    return normalize_library(dataset_library), match.group(0) if match else None


def evidence_libraries(evidence: list[dict]) -> list[str]:
    """证据文件所在的语料库目录；跨库题用单库路由无法覆盖全部证据。"""
    return sorted({span["path"].split("/")[1] for span in evidence})


def chunks_required(bm25_path: Path, span: dict) -> int:
    """该证据在索引里跨几个chunk；>=2说明必须靠联合召回才能覆盖。"""
    with closing(sqlite3.connect(bm25_path)) as database:
        if span.get("page") is not None:
            return database.execute(
                "SELECT COUNT(*) FROM chunks WHERE source_path = ? AND page_number = ?",
                (span["path"], span["page"]),
            ).fetchone()[0]
        return database.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_path = ? AND start_line <= ? "
            "AND end_line >= ?",
            (span["path"], span["end_line"], span["start_line"]),
        ).fetchone()[0]


def page_span_hit(top: list[dict], span: dict) -> int | None:
    """返回第一个落在该物理页上的候选名次；没有则返回None。"""
    return next(
        (
            rank
            for rank, candidate in enumerate(top, start=1)
            if candidate["source_path"] == span["path"]
            and candidate.get("page_number") == span["page"]
        ),
        None,
    )


def evidence_metrics(candidates: list[dict], evidence: list[dict], *, k: int) -> dict:
    """行级证据交给既有metrics，页级证据单独判定，再合并成同一结构。"""
    top = candidates[:k]
    line_spans = [span for span in evidence if span.get("page") is None]
    page_spans = [span for span in evidence if span.get("page") is not None]
    parts = [metrics(top, [span], k=k) for span in line_spans]
    page_ranks = [page_span_hit(top, span) for span in page_spans]
    recalls = [part["span_recall"] for part in parts] + [
        float(rank is not None) for rank in page_ranks
    ]
    singles = [part["single_chunk_span_rate"] for part in parts] + [
        float(rank is not None) for rank in page_ranks
    ]
    ranks = [part["first_evidence_touch_rank"] for part in parts] + page_ranks
    touched = [rank for rank in ranks if rank is not None]
    first_rank = min(touched) if touched else None
    return {
        "returned": len(top),
        "k": k,
        "span_recall": statistics.fmean(recalls),
        "single_chunk_span_rate": statistics.fmean(singles),
        "all_spans_hit": all(recalls),
        "any_span_hit": any(recalls),
        "spans_total": len(evidence),
        "spans_covered": int(sum(recalls)),
        "page_level_spans": len(page_spans),
        "first_evidence_touch_rank": first_rank,
        "reciprocal_rank": 0.0 if first_rank is None else 1.0 / first_rank,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="五库候选索引的validation检索对照")
    parser.add_argument("--route", type=Path,
                        default=ROOT / "configs/indexes/multicorpus-candidates-002.json")
    parser.add_argument("--dataset", type=Path,
                        default=ROOT / "evaluation/datasets/rag-validation-001.jsonl")
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts/corpus-acceptance-002/validation-retrieval-eval.json")
    parser.add_argument("--no-reranker", action="store_true", help="只跑dense/BM25/RRF")
    arguments = parser.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from qdrant_client import QdrantClient

    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query_vector
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.retrieval.rerank import Qwen3Reranker, candidate_document, rank_candidates
    from wenjin.retrieval.router import MultiCorpusRouter
    from wenjin.settings import Settings

    settings = Settings()
    dataset = [
        json.loads(line)
        for line in arguments.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    embedder = settings.embedding()
    reranker = None
    routing = None
    records = []
    try:
        if not arguments.no_reranker:
            reranker = Qwen3Reranker(
                settings.reranker_model_path,
                device="cuda" if torch.cuda.is_available() else "cpu",
                max_length=1024,
                batch_size=2,
            )
        # 构造路由时会逐个校验候选索引；任一不合格就不会进入本次评测。
        routing = MultiCorpusRouter.from_route_file(
            arguments.route.resolve(),
            root=ROOT,
            qdrant_client=client,
            embedder=embedder,
            reranker=reranker,
        )
        for row in dataset:
            library, version = route_arguments(row["library"], row["version"])
            index = routing.resolve(library, version)[0]
            vector = embedder.get_query_embedding(row["query"])
            dense_started = time.perf_counter()
            dense = [
                {"library": library, "score": point.score, **point.payload}
                for point in dense_query_vector(
                    client, vector, index.collection, top_k=arguments.candidate_k
                )
            ]
            dense_seconds = time.perf_counter() - dense_started
            terms = query_terms(row["query"])
            bm25_started = time.perf_counter()
            keyword = (
                [{"library": library, **item}
                 for item in bm25_query(index.bm25_path, row["query"], top_k=arguments.candidate_k)]
                if terms else []
            )
            bm25_seconds = time.perf_counter() - bm25_started
            fusion_started = time.perf_counter()
            fused = rrf_fuse({"dense": dense, "bm25": keyword}, k=arguments.rrf_k)
            fusion_seconds = time.perf_counter() - fusion_started
            rerank_started = time.perf_counter()
            if reranker is not None and fused:
                scores = reranker.score(row["query"], [candidate_document(item) for item in fused])
                ranked = rank_candidates(fused, scores)
            else:
                ranked = fused
            rerank_seconds = time.perf_counter() - rerank_started
            retrievers = {"dense": dense, "bm25": keyword, "rrf": fused}
            if reranker is not None:
                retrievers["rerank"] = ranked
            records.append({
                "id": row["id"],
                "library": library,
                "dataset_library": row["library"],
                "version": index.version,
                "query": row["query"],
                "query_type": row.get("query_type"),
                "bm25_terms": terms,
                "evidence_spans": len(row["evidence"]),
                "max_chunks_per_span": max(
                    chunks_required(index.bm25_path, span) for span in row["evidence"]
                ),
                "evidence_libraries": evidence_libraries(row["evidence"]),
                "metrics": {
                    name: {f"at_{k}": evidence_metrics(results, row["evidence"], k=k)
                           for k in (5, 20)}
                    for name, results in retrievers.items()
                },
                "top_ids": {name: [item["chunk_id"] for item in results[:5]]
                            for name, results in retrievers.items()},
                "timing_seconds": {
                    "dense": round(dense_seconds, 6),
                    "bm25": round(bm25_seconds, 6),
                    "fusion": round(fusion_seconds, 6),
                    "rerank": round(rerank_seconds, 6),
                },
            })
    finally:
        if routing is not None:
            routing.close()
        else:
            client.close()
            if reranker is not None:
                reranker.close()

    retriever_names = ["dense", "bm25", "rrf"] + (["rerank"] if reranker is not None else [])
    libraries = sorted({record["library"] for record in records})

    def summary_for(records_subset: list[dict]) -> dict:
        return {name: {f"at_{k}": summarize(records_subset, name, k) for k in (5, 20)}
                for name in retriever_names}

    def latency_for(records_subset: list[dict]) -> dict:
        return {
            stage: {
                "mean": round(statistics.fmean(r["timing_seconds"][stage] for r in records_subset), 4),
                "stdev": round(statistics.pstdev(r["timing_seconds"][stage] for r in records_subset), 4),
            }
            for stage in ("dense", "bm25", "fusion", "rerank")
        }

    report = {
        "route_id": json.loads(arguments.route.read_text(encoding="utf-8-sig"))["route_id"],
        "dataset": str(arguments.dataset.relative_to(ROOT)).replace("\\", "/"),
        "dataset_role": "validation only; test 40题保持冻结且未参与本次调参",
        "candidate_k": arguments.candidate_k,
        "rrf_k": arguments.rrf_k,
        "reranker": None if reranker is None else {
            "model": settings.reranker_model_path.name,
            "max_length": reranker.max_length,
            "batch_size": reranker.batch_size,
        },
        "evidence_match": (
            "复用run_retrieval_eval.metrics：Top-K chunk联合覆盖整段行级证据；"
            "PDF证据只登记物理页，按页判定并单列"
        ),
        "boundary": (
            "本机小样本validation对照；不是第三方盲测，也不代表检索效果验收通过。"
            "RRF和reranker分数只是排序信号，不能当作答案置信度"
        ),
        "questions": len(records),
        "summary": {
            "overall": summary_for(records),
            "per_library": {library: summary_for(
                [r for r in records if r["library"] == library]) for library in libraries},
        },
        "latency_seconds": {
            "overall": latency_for(records),
            "per_library": {library: latency_for(
                [r for r in records if r["library"] == library]) for library in libraries},
        },
        "multi_span_questions": [
            {
                "id": record["id"],
                "library": record["library"],
                "spans": record["evidence_spans"],
                "spans_total": record["metrics"]["dense"]["at_5"]["spans_total"],
                "covered_at_5": {
                    name: record["metrics"][name]["at_5"]["spans_covered"]
                    for name in retriever_names
                },
            }
            for record in records if record["evidence_spans"] > 1
        ],
        # 单条证据跨多个chunk的题：必须靠联合召回覆盖，单独列出逐题结果。
        "joint_recall_questions": [
            {
                "id": record["id"],
                "library": record["library"],
                "max_chunks_per_span": record["max_chunks_per_span"],
                "spans_total": record["metrics"]["dense"]["at_5"]["spans_total"],
                "covered": {
                    name: {
                        f"at_{k}": record["metrics"][name][f"at_{k}"]["spans_covered"]
                        for k in (5, 20)
                    }
                    for name in retriever_names
                },
            }
            for record in records if record["max_chunks_per_span"] >= 2
        ],
        # 跨库题（证据分布在两个语料库）用单库路由必然覆盖不全，必须与排序失败分开。
        "cross_library_questions": [
            {
                "id": record["id"],
                "dataset_library": record["dataset_library"],
                "evidence_libraries": record["evidence_libraries"],
                "note": "证据跨库，单库路由无法覆盖全部证据；需要跨库扇出才能覆盖",
                "covered_at_5": {
                    name: record["metrics"][name]["at_5"]["spans_covered"]
                    for name in retriever_names
                },
                "spans_total": record["metrics"]["dense"]["at_5"]["spans_total"],
            }
            for record in records if len(record["evidence_libraries"]) > 1
        ],
        "failures_at_5": {
            name: [record["id"] for record in records
                   if not record["metrics"][name]["at_5"]["all_spans_hit"]]
            for name in retriever_names
        },
        "failure_reasons_at_5": {
            name: {
                record["id"]: (
                    "证据跨库，单库路由无法覆盖全部证据"
                    if len(record["evidence_libraries"]) > 1
                    else "单库内排序未在Top-5覆盖全部证据"
                )
                for record in records
                if not record["metrics"][name]["at_5"]["all_spans_hit"]
            }
            for name in retriever_names
        },
        "details": records,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]["overall"], ensure_ascii=False, indent=2))
    print(json.dumps(report["latency_seconds"]["overall"], ensure_ascii=False))
    print(f"报告：{arguments.output}")


if __name__ == "__main__":
    main()
