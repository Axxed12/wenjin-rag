"""在10道开发题上比较 dense、BM25 和 RRF。

这是开发集诊断，不是冻结测试集，也不能用于宣称泛化效果。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def overlaps(result: dict, evidence: dict) -> bool:
    """判断一个候选是否触及某段证据；只用于首个相关候选的排名。"""
    return (
        result["source_path"] == evidence["path"]
        and result["start_line"] <= evidence["end_line"]
        and result["end_line"] >= evidence["start_line"]
    )


def covers_span(results: list[dict], evidence: dict) -> bool:
    """判断同一文件的多个 chunk 联合起来是否覆盖证据的每一行。"""
    intervals = sorted(
        (max(result["start_line"], evidence["start_line"]),
         min(result["end_line"], evidence["end_line"]))
        for result in results
        if overlaps(result, evidence)
    )
    next_line = evidence["start_line"]
    for start, end in intervals:
        if start > next_line:
            return False  # 中间缺了一行；只有接触不能算完整证据。
        next_line = max(next_line, end + 1)
        if next_line > evidence["end_line"]:
            return True
    return False


def metrics(results: list[dict], evidence: list[dict], *, k: int) -> dict:
    """计算 Top-K 的联合证据覆盖率与第一个接触证据的候选名次。"""
    top = results[:k]
    hit_spans = [covers_span(top, span) for span in evidence]
    full_single_spans = [
        any(
            result["source_path"] == span["path"]
            and result["start_line"] <= span["start_line"]
            and result["end_line"] >= span["end_line"]
            for result in top
        )
        for span in evidence
    ]
    first_rank = next(
        (
            rank
            for rank, result in enumerate(top, start=1)
            if any(overlaps(result, span) for span in evidence)
        ),
        None,
    )
    return {
        "returned": len(top),
        "span_recall": sum(hit_spans) / len(hit_spans),
        "single_chunk_span_rate": sum(full_single_spans) / len(full_single_spans),
        "all_spans_hit": all(hit_spans),
        "any_span_hit": any(hit_spans),
        "first_evidence_touch_rank": first_rank,
        "reciprocal_rank": 0.0 if first_rank is None else 1.0 / first_rank,
    }


def summarize(details: list[dict], retriever: str, k: int) -> dict:
    values = [item["metrics"][retriever][f"at_{k}"] for item in details]
    return {
        "questions": len(values),
        "mean_span_recall": sum(v["span_recall"] for v in values) / len(values),
        "mean_single_chunk_span_rate": sum(v["single_chunk_span_rate"] for v in values) / len(values),
        "all_spans_hit_rate": sum(v["all_spans_hit"] for v in values) / len(values),
        "any_span_hit_rate": sum(v["any_span_hit"] for v in values) / len(values),
        "first_evidence_touch_mrr": sum(v["reciprocal_rank"] for v in values) / len(values),
    }


def main() -> None:
    from qdrant_client import QdrantClient

    from evaluation.index_assets import active_index_assets
    from scripts.query_hybrid import qdrant_points_to_dicts
    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.retrieval.query_expansion import (
        code_identifier_terms,
        expand_query,
        source_routes,
    )
    from wenjin.settings import Settings

    settings = Settings()
    seeds = [
        json.loads(line)
        for line in (ROOT / "corpora/osg-3.6.5/dev-seed.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    target, database = active_index_assets(ROOT)
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    details = []
    started = time.perf_counter()
    try:
        for seed in seeds:
            dense = qdrant_points_to_dicts(
                dense_query(
                    client,
                    embedder,
                    target.dense_collection,
                    seed["query"],
                    top_k=20,
                )
            )
            terms = query_terms(seed["query"])
            bm25 = bm25_query(database, seed["query"], top_k=20) if terms else []
            fusion = rrf_fuse({"dense": dense, "bm25": bm25}, k=60)
            expanded_query, added_terms = expand_query(seed["query"])
            expanded_dense = qdrant_points_to_dicts(
                dense_query(
                    client,
                    embedder,
                    target.dense_collection,
                    expanded_query,
                    top_k=20,
                )
            )
            expanded_terms = query_terms(expanded_query)
            expanded_bm25 = (
                bm25_query(database, expanded_query, top_k=20) if expanded_terms else []
            )
            identifiers = code_identifier_terms(expanded_terms)
            symbol_bm25 = (
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
            routes = source_routes(seed["query"])
            routed_results = {
                f"route_{index}": bm25_query(
                    database,
                    expanded_query,
                    top_k=20,
                    terms_override=expanded_terms,
                    source_prefix=prefix,
                )
                for index, prefix in enumerate(routes)
                if expanded_terms
            }
            expanded_fusion = rrf_fuse(
                {
                    "dense_expanded": expanded_dense,
                    "bm25_expanded": expanded_bm25,
                    "symbol_headers": symbol_bm25,
                    **routed_results,
                },
                k=60,
            )
            details.append(
                {
                    "id": seed["id"],
                    "query": seed["query"],
                    "bm25_terms": terms,
                    "bm25_available": bool(terms),
                    "expanded_query": expanded_query,
                    "added_terms": added_terms,
                    "identifier_terms": identifiers,
                    "source_routes": routes,
                    "evidence": seed["evidence"],
                    "metrics": {
                        name: {
                            "at_5": metrics(results, seed["evidence"], k=5),
                            "at_20": metrics(results, seed["evidence"], k=20),
                        }
                        for name, results in {
                            "dense": dense,
                            "bm25": bm25,
                            "fusion": fusion,
                            "expanded_fusion": expanded_fusion,
                        }.items()
                    },
                    "top_ids": {
                        "dense": [item["chunk_id"] for item in dense[:5]],
                        "bm25": [item["chunk_id"] for item in bm25[:5]],
                        "fusion": [item["chunk_id"] for item in fusion[:5]],
                        "expanded_fusion": [item["chunk_id"] for item in expanded_fusion[:5]],
                    },
                }
            )
    finally:
        client.close()

    report = {
        "dataset": "corpora/osg-3.6.5/dev-seed.jsonl",
        "dataset_role": "development diagnostics; source-selected and not held out",
        "strategy": "blocks",
        "candidate_k": 20,
        "rrf_k": 60,
        "evidence_match": "Top-K chunks jointly cover every line in each cited source span; one-chunk containment reported separately",
        "seconds": round(time.perf_counter() - started, 3),
        "summary": {
            name: {f"at_{k}": summarize(details, name, k) for k in (5, 20)}
            for name in ("dense", "bm25", "fusion", "expanded_fusion")
        },
        "details": details,
    }
    output = ROOT / "artifacts/runs/retrieval-eval-blocks-dev.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
