"""检查两种切分策略对已核实源码证据的覆盖情况。

**覆盖率有三种判定依据，数值不可互相比较**，报告必须把用的是哪一种写清楚：

- `line_intersection`：chunk 的行号区间与证据行区间相交。最弱——单行被硬切时，
  两侧片段都能与证据行相交，但没有任何一片包含完整证据文本；
- `page_present`：按页来源（PDF）只登记物理页、没有行号或锚点，只能判"该页有 chunk"；
- `evidence_text`：**完整行片段**的行范围并集必须盖满证据区间。完整行片段指正文与它声明的
  整段来源行逐字一致；单行被硬切产生的**半行片段不计入覆盖**（没有字符偏移量，无法证明它
  盖住了哪一段），单独报在 `partial_line_chunks` / `undecidable_lines` 里。
  **注意不是"拼起来找子串"**：重叠片段会让拼接重复文字，把完整证据割断。

本脚本同时给出 `line_intersection` 与 `evidence_text` 两个数，**用后者判断覆盖是否成立**。
两者都只说明证据进了 chunk，不说明检索时一定能召回它。

输出写到独立文件，不覆盖旧报告：`artifacts/runs/chunk-lab-001-coverage.json`（只含
行号相交口径）保持不变，便于对照口径变化带来的差异。
运行：uv run python scripts/evaluate_chunk_coverage.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wenjin.coverage import LINE_BASIS, TEXT_BASIS, evidence_coverage  # noqa: E402

SEED_PATH = ROOT / "corpora/osg-3.6.5/dev-seed.jsonl"
MANIFEST = ROOT / "corpora/osg-3.6.5/source-manifest.json"
OUTPUT = ROOT / "artifacts/runs/chunk-lab-002-coverage-line-and-text.json"


def covered(chunks: list, path: str, start: int, end: int) -> list:
    """返回与指定源码行范围相交的 chunk。"""
    return [
        chunk
        for chunk in chunks
        if chunk.source_path == path and chunk.start_line <= end and chunk.end_line >= start
    ]


def evaluate(chunks: list, sources: dict[str, str], seeds: list[dict]) -> dict:
    """逐题算两种口径：行号相交（弱）与证据文本覆盖（强）。

    `all_evidence_spans_hit` 沿用旧字段名与旧口径（行号相交），数值与
    `chunk-lab-001-coverage.json` 可直接对照；强口径单独成字段，不混进旧名字里。

    强口径由 `wenjin.coverage.evidence_coverage` 判定：按**来源位置**合并命中片段的行
    范围，再校验文本与范围一致。**不能**直接把命中片段拼起来找子串——重叠片段会把
    重叠部分重复一遍，把本来完整的证据割断（`fixed` 的相邻窗口重叠 120 字符）。
    """
    results = []
    for seed in seeds:
        spans = []
        for evidence in seed["evidence"]:
            start, end = evidence["start_line"], evidence["end_line"]
            hits = covered(chunks, evidence["path"], start, end)
            source_lines = sources[evidence["path"]].splitlines(keepends=True)
            verdict = evidence_coverage(hits, source_lines, start, end)
            spans.append({
                "evidence": evidence,
                "chunk_ids": [chunk.chunk_id for chunk in hits],
                "evidence_text_covered": verdict["covered"],
                "missing_lines": verdict["missing_lines"],
                # 只被半行片段沾到、没有完整片段覆盖的行：无法判定，不算覆盖。
                "undecidable_lines": verdict["undecidable_lines"],
                "partial_line_chunks": verdict["partial_line_chunks"],
                "text_range_conflicts": verdict["text_range_conflicts"],
            })
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
                "all_evidence_text_covered": all(
                    item["evidence_text_covered"] for item in spans
                ),
                "spans": spans,
            }
        )
    undecided = sum(
        1 for item in results for span in item["spans"] if span["undecidable_lines"]
    )
    partial = sum(
        len(span["partial_line_chunks"]) for item in results for span in item["spans"]
    )
    return {
        "question_count": len(results),
        # 半行片段与"无法判定"的行数：为 0 才说明这一轮的数字没有受硬切影响。
        "spans_with_undecidable_lines": undecided,
        "partial_line_chunks_in_hits": partial,
        "coverage_basis": {
            "all_evidence_spans_hit": LINE_BASIS,
            "all_evidence_spans_in_one_chunk": LINE_BASIS,
            "all_evidence_text_covered": TEXT_BASIS,
        },
        "all_evidence_spans_hit": sum(item["all_evidence_spans_hit"] for item in results),
        "all_evidence_spans_in_one_chunk": sum(item["all_evidence_spans_in_one_chunk"] for item in results),
        "all_evidence_text_covered": sum(
            item["all_evidence_text_covered"] for item in results
        ),
        "details": results,
    }


def main() -> None:
    from ingest import load_documents
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks

    documents = load_documents(MANIFEST)
    seeds = [json.loads(line) for line in SEED_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    strategies = {"fixed": [], "blocks": []}
    sources: dict[str, str] = {}
    for document in documents:
        # 证据文本要回到**原文**取，不能用解析后的文本：这里是行映射解析，
        # 两者一致，但显式取原文字节更能说明"比的是不是原文"。
        sources.setdefault(document["source_path"], document["text"])
        strategies["fixed"].extend(fixed_chunks(document, size=1200, overlap=120))
        strategies["blocks"].extend(block_chunks(document, max_chars=1600))

    report = {
        "schema_version": 2,
        "note": (
            "Development evidence coverage only; no query retrieval was executed. "
            "Coverage here means 'the evidence entered some chunk', not 'retrieval will find it'. "
            "Two bases are reported and are NOT comparable: line_intersection (weak) and "
            "evidence_text (strong). The earlier chunk-lab-001-coverage.json report contains "
            "only the line_intersection basis and is left untouched."
        ),
        "strategies": {
            name: evaluate(chunks, sources, seeds) for name, chunks in strategies.items()
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
