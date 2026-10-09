"""检验Reranker最高分能否区分“证据充分”和“不可完整回答”。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def classification_at_threshold(rows: list[dict], threshold: float) -> dict:
    """将top_score当二分类分数，计算小样本诊断指标。"""
    tp = sum(row["target_supported"] and row["top_score"] >= threshold for row in rows)
    fp = sum(not row["target_supported"] and row["top_score"] >= threshold for row in rows)
    fn = sum(row["target_supported"] and row["top_score"] < threshold for row in rows)
    tn = sum(not row["target_supported"] and row["top_score"] < threshold for row in rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    return {
        "threshold": threshold,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "balanced_accuracy": (recall + specificity) / 2,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
    }


def candidate_thresholds(rows: list[dict]) -> list[float]:
    """在实际分数之间取中点，并包含区间两端。"""
    scores = sorted({row["top_score"] for row in rows})
    if not scores:
        return []
    values = [0.0]
    values.extend((left + right) / 2 for left, right in zip(scores, scores[1:]))
    values.append(1.0)
    return values


def main() -> None:
    from evaluation.quality.run_retrieval_eval import metrics
    from wenjin.mcp_server import create_default_service
    from wenjin.retrieval.service import SearchConfig

    positives = [
        json.loads(line)
        for line in (ROOT / "evaluation/datasets/osg-3_6_5-frozen-001.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    negatives = [
        json.loads(line)
        for line in (ROOT / "evaluation/datasets/osg-evidence-negative-001.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    service = create_default_service()
    rows = []
    try:
        for item in positives + negatives:
            result = service.search(item["query"], config=SearchConfig(top_k=5))
            evidence = result["evidence"]
            top_score = max((entry["rerank_score"] for entry in evidence), default=0.0)
            margin = (
                evidence[0]["rerank_score"] - evidence[1]["rerank_score"]
                if len(evidence) >= 2 else top_score
            )
            if item["id"].startswith("osg-test-"):
                simple_results = [
                    {
                        "source_path": entry["source_path"],
                        "start_line": entry["start_line"],
                        "end_line": entry["end_line"],
                    }
                    for entry in evidence
                ]
                target_supported = metrics(simple_results, item["evidence"], k=5)[
                    "all_spans_hit"
                ]
                expected_status = "supported" if target_supported else "insufficient"
                group = "frozen_answerable"
            else:
                target_supported = False
                expected_status = item["expected_status"]
                group = "negative_calibration"
            rows.append({
                "id": item["id"],
                "group": group,
                "query": item["query"],
                "expected_status": expected_status,
                "target_supported": target_supported,
                "top_score": top_score,
                "top1_margin": margin,
                "top_ids": [entry["chunk_id"] for entry in evidence],
                "top_sources": [entry["source_path"] for entry in evidence],
            })
    finally:
        service.close()

    evaluations = [classification_at_threshold(rows, value) for value in candidate_thresholds(rows)]
    best = max(evaluations, key=lambda item: (item["balanced_accuracy"], item["f1"]))
    supported_scores = [row["top_score"] for row in rows if row["target_supported"]]
    unsupported_scores = [row["top_score"] for row in rows if not row["target_supported"]]
    report = {
        "run_id": "evidence-threshold-001",
        "positive_dataset": "evaluation/datasets/osg-3_6_5-frozen-001.jsonl",
        "negative_dataset": "evaluation/datasets/osg-evidence-negative-001.jsonl",
        "score": "top-1 Qwen3-Reranker-0.6B yes probability",
        "supported_score_range": [min(supported_scores), max(supported_scores)],
        "unsupported_score_range": [min(unsupported_scores), max(unsupported_scores)],
        "best_threshold_on_this_calibration_set": best,
        "decision_rule_status": "diagnostic_only_not_enabled",
        "rows": rows,
    }
    output = ROOT / "artifacts/runs/evidence-threshold-001.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
