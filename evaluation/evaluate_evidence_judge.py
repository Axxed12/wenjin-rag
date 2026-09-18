"""用外接模型审核正式检索证据，并与人工预期状态比较。"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from evaluation.run_retrieval_eval import metrics, overlaps
    from wenjin.evidence import OpenAIEvidenceJudge
    from wenjin.mcp_server import create_default_service
    from wenjin.retrieval.query_rewrite import QueryRewriteSettings
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
    model_settings = QueryRewriteSettings()
    judge = OpenAIEvidenceJudge(model_settings)
    service = create_default_service()
    rows = []
    try:
        for item in positives + negatives:
            result = service.search(item["query"], config=SearchConfig(top_k=5))
            if item["id"].startswith("osg-test-"):
                simple = [
                    {"source_path": evidence["source_path"],
                     "start_line": evidence["start_line"],
                     "end_line": evidence["end_line"]}
                    for evidence in result["evidence"]
                ]
                coverage = metrics(simple, item["evidence"], k=5)
                touches = any(
                    overlaps(candidate, span)
                    for candidate in simple for span in item["evidence"]
                )
                expected = (
                    "supported" if coverage["all_spans_hit"]
                    else "partially_supported" if touches
                    else "insufficient"
                )
                group = "frozen_answerable"
            else:
                expected = item["expected_status"]
                group = "negative_calibration"
            started = time.perf_counter()
            error_type = None
            try:
                assessment = judge.assess(item["query"], result["evidence"])
                actual = assessment.status
                assessment_data = {
                    "status": assessment.status,
                    "citations": list(assessment.citations),
                    "supported_claims": list(assessment.supported_claims),
                    "missing_aspects": list(assessment.missing_aspects),
                    "reason": assessment.reason,
                }
            except Exception as exc:
                actual = "judge_error"
                error_type = type(exc).__name__
                assessment_data = None
            rows.append({
                "id": item["id"],
                "group": group,
                "query": item["query"],
                "expected_status": expected,
                "actual_status": actual,
                "correct": actual == expected,
                "safe_binary_correct": (actual == "supported") == (expected == "supported"),
                "judge_seconds": round(time.perf_counter() - started, 3),
                "judge_error_type": error_type,
                "assessment": assessment_data,
                "evidence_ids": [entry["chunk_id"] for entry in result["evidence"]],
            })
    finally:
        service.close()

    report = {
        "run_id": "evidence-judge-001",
        "model": model_settings.query_rewrite_model,
        "questions": len(rows),
        "three_class_accuracy": sum(row["correct"] for row in rows) / len(rows),
        "safe_supported_vs_not_accuracy": (
            sum(row["safe_binary_correct"] for row in rows) / len(rows)
        ),
        "judge_errors": sum(row["actual_status"] == "judge_error" for row in rows),
        "mean_judge_seconds": sum(row["judge_seconds"] for row in rows) / len(rows),
        "rows": rows,
    }
    output = ROOT / "artifacts/runs/evidence-judge-001.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"},
                     ensure_ascii=False, indent=2))
    print(f"报告：{output}")


if __name__ == "__main__":
    main()
