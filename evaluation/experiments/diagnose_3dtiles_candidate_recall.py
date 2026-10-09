"""CPU-only检查3D Tiles规范目标在candidate_003 BM25全库中的位置。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PAIRED = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/paired-results.json"
QUESTIONS = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl"
QUESTION_SHA256 = "8c69891fddf0002210b9446150d35d4acc71217d58f401033ca0e2fef8595162"
BM25_INDEX = ROOT / "data/indexes/ogc_3dtiles_1_1_bm25_blocks_split_candidate_003.sqlite"
OUTPUT = ROOT / "artifacts/phase1-source-replay-validation/phase2-bm25-recall-diagnostic.json"
REPORT = ROOT / "artifacts/phase1-source-replay-validation/phase2-bm25-recall-diagnostic.md"
TARGETS = {
    "supplemental-3dtiles-longline-003": (5903, "c543f1a5a8615c7f02c8a043e13da94583ce5daaa7a4ee957aeacd57b5c69b32"),
    "supplemental-3dtiles-longline-005": (7128, "cb035d31b87daa154eb01ddae4fff82b810cd28236b77b7f18678aef7f9ee2c3"),
}


def main() -> None:
    raw_questions = QUESTIONS.read_bytes()
    actual_sha = hashlib.sha256(raw_questions).hexdigest()
    if actual_sha != QUESTION_SHA256:
        raise ValueError("固定补充题集SHA不匹配")
    questions = {
        row["id"]: row
        for row in (json.loads(line) for line in raw_questions.decode("utf-8").splitlines() if line.strip())
    }
    paired = json.loads(PAIRED.read_text(encoding="utf-8"))
    if paired.get("status") != "completed" or paired.get("dataset", {}).get("sha256") != QUESTION_SHA256:
        raise ValueError("paired结果状态或题集SHA不符")
    details = {row["input"]["id"]: row for row in paired["details"]}
    if len(details) != 24 or set(details) != set(questions):
        raise ValueError("paired与固定题集的24个题目ID不一致")

    from wenjin.indexing.bm25 import bm25_query

    rows_out = {}
    for question_id, (target_line, expected_sha) in TARGETS.items():
        frozen = questions[question_id]
        paired_input = details[question_id]["input"]
        if any(paired_input.get(key) != frozen.get(key) for key in ("id", "split", "query", "evidence")):
            raise ValueError(f"{question_id} paired输入与冻结题不一致")
        results = bm25_query(BM25_INDEX, frozen["query"], top_k=989)
        match = next(
            (
                (rank, item)
                for rank, item in enumerate(results, start=1)
                if item.get("source_path") == "raw_data/ogc-3dtiles/1.1/22-025r4.html"
                and item.get("start_line") == target_line
                and item.get("end_line") == target_line
            ),
            None,
        )
        if match is None:
            raise ValueError(f"{question_id}精确目标行未出现在BM25结果中")
        rank, item = match
        body_sha = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        if body_sha != expected_sha:
            raise ValueError(f"{question_id}目标正文与冻结行SHA不匹配")
        rows_out[question_id] = {
            "query": frozen["query"],
            "target": {
                "chunk_id": item["chunk_id"],
                "source_path": item["source_path"],
                "start_line": item["start_line"],
                "end_line": item["end_line"],
                "body_sha256": body_sha,
                "characters": len(item["text"]),
            },
            "bm25_full_index_rank": rank,
            "bm25_score": item["score"],
            "returned_at_candidate_k": {str(k): rank <= k for k in (20, 40, 50)},
            "index_result_count": len(results),
        }

    result = {
        "status": "completed",
        "method": "CPU-only SQLite FTS BM25 query over full candidate_003 index",
        "dataset": {"path": QUESTIONS.relative_to(ROOT).as_posix(), "sha256": actual_sha, "count": 24},
        "index": BM25_INDEX.relative_to(ROOT).as_posix(),
        "route_candidate_k_limit": 50,
        "targets": rows_out,
        "interpretation": "Both exact HTML targets rank just beyond the supported candidate_k ceiling in the BM25 leg; this alone cannot determine whether a deeper dense leg retrieves them.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Q003/Q005 CPU-only BM25 recall diagnostic",
        "",
        f"- Input question SHA-256: `{actual_sha}` (24 unique paired/frozen IDs verified).",
        "- Source: existing candidate_003 SQLite FTS index; no model, dense index, or GPU was loaded.",
        "- This full-index rank is diagnostic only; the API accepts candidate_k up to 50.",
        "",
        "| Question | Exact HTML target chunk | BM25 rank | k=20 | k=40 | k=50 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for question_id, record in rows_out.items():
        marks = record["returned_at_candidate_k"]
        lines.append(
            f"| {question_id} | `{record['target']['chunk_id']}` | {record['bm25_full_index_rank']} | "
            f"{'yes' if marks['20'] else 'no'} | {'yes' if marks['40'] else 'no'} | {'yes' if marks['50'] else 'no'} |"
        )
    lines.extend([
        "",
        "The exact HTML targets rank 53 and 55, so increasing the BM25 candidate depth to its supported maximum of 50 does not retrieve either target. The next paired experiment tests candidate_k=50 because the dense leg may add the target independently; it is not treated as a proven BM25 fix.",
        "",
    ])
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"JSON: {OUTPUT.relative_to(ROOT).as_posix()}")
    print(f"Report: {REPORT.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
