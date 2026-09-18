"""检查两种切分策略对已核实源码证据的覆盖情况。

覆盖只说明证据行进入了 chunk，不说明检索时一定能召回它。
运行：uv run python scripts/evaluate_chunk_coverage.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEED_PATH = ROOT / "corpora/osg-3.6.5/dev-seed.jsonl"
MANIFEST = ROOT / "corpora/osg-3.6.5/source-manifest.json"
OUTPUT = ROOT / "artifacts/runs/chunk-lab-001-coverage.json"


def covered(chunks: list, path: str, start: int, end: int) -> list:
    """返回与指定源码行范围相交的 chunk。"""
    return [
        chunk
        for chunk in chunks
        if chunk.source_path == path and chunk.start_line <= end and chunk.end_line >= start
    ]


def evaluate(chunks: list, seeds: list[dict]) -> dict:
    results = []
    for seed in seeds:
        spans = []
        for evidence in seed["evidence"]:
            hits = covered(chunks, evidence["path"], evidence["start_line"], evidence["end_line"])
            spans.append({"evidence": evidence, "chunk_ids": [chunk.chunk_id for chunk in hits]})
        results.append(
            {
                "id": seed["id"],
                "all_evidence_spans_hit": all(item["chunk_ids"] for item in spans),
                "all_evidence_spans_in_one_chunk": any(
                    all(
                        chunk.source_path == evidence["path"]
                        and chunk.start_line <= evidence["start_line"]
                        and chunk.end_line >= evidence["end_line"]
                        for evidence in seed["evidence"]
                    )
                    for chunk in chunks
                ),
                "spans": spans,
            }
        )
    return {
        "question_count": len(results),
        "all_evidence_spans_hit": sum(item["all_evidence_spans_hit"] for item in results),
        "all_evidence_spans_in_one_chunk": sum(item["all_evidence_spans_in_one_chunk"] for item in results),
        "details": results,
    }


def main() -> None:
    from ingest import load_documents
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks

    documents = load_documents(MANIFEST)
    seeds = [json.loads(line) for line in SEED_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    strategies = {"fixed": [], "blocks": []}
    for document in documents:
        strategies["fixed"].extend(fixed_chunks(document, size=1200, overlap=120))
        strategies["blocks"].extend(block_chunks(document, max_chars=1600))

    report = {
        "note": "Development evidence coverage only; no query retrieval was executed.",
        "strategies": {name: evaluate(chunks, seeds) for name, chunks in strategies.items()},
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
