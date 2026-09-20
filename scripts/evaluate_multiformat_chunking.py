"""用validation比较blocks与blocks-split的切片长度分布和证据覆盖。

覆盖判定改为按文本重建：把命中同一证据的chunk按原文顺序拼起来，检查证据原文是否
真的出现在拼接结果中。只看行号相交会在单行被硬切时高估覆盖，因此不再使用。
运行：uv run python scripts/evaluate_multiformat_chunking.py
"""

import argparse
import json
import re
import statistics
from collections import Counter
from pathlib import Path

from wenjin.ingestion.chunking import block_chunks
from wenjin.ingestion.multiformat import load_retrieval_documents

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts/corpus-acceptance-002/validation-chunking-comparison.json"
MAX_CHARS = 1600
# 覆盖率有两种判定依据，数值不可互相比较，报告里逐条标出用的是哪一种：
#   page_present —— 按页来源只登记物理页，只能判"该页有 chunk"（弱）；
#   evidence_text —— 把命中同一证据的 chunk 拼起来，证据原文确实出现在其中（强）。
PAGE_BASIS = "page_present"
TEXT_BASIS = "evidence_text_rebuild"
METRIC_SEMANTICS = {
    "page_present": "该物理页有 chunk；不校验证据内容是否落在文本里",
    "evidence_text_rebuild": "按原文顺序拼接命中 chunk，证据原文必须完整出现",
}
HISTOGRAM_BUCKETS = (
    (0, 200),
    (200, 400),
    (400, 800),
    (800, 1200),
    (1200, 1600),
    (1601, 10**9),  # 单独一档表示真正越界，恰好1600不算越界
)


def normalize(text: str) -> str:
    """折叠空白；只用于比较，不改变证据文本本身。"""
    return re.sub(r"\s+", " ", text).strip()


def length_stats(lengths: list[int]) -> dict:
    if not lengths:
        return {}
    ordered = sorted(lengths)
    return {
        "chunks": len(ordered),
        "mean": round(statistics.fmean(ordered), 2),
        "stdev": round(statistics.pstdev(ordered), 2),
        "min": ordered[0],
        "p50": ordered[len(ordered) // 2],
        "p90": ordered[int(len(ordered) * 0.90)],
        "p99": ordered[min(int(len(ordered) * 0.99), len(ordered) - 1)],
        "max": ordered[-1],
    }


def histogram(lengths: list[int]) -> dict:
    buckets = Counter()
    for length in lengths:
        for low, high in HISTOGRAM_BUCKETS:
            if low <= length < high:
                label = f"{low}-{high - 1}" if high < 10**9 else f"{low}+"
                buckets[label] += 1
                break
    return dict(buckets)


def evaluate_strategy(documents: list[dict], validation: list[dict], *, split_oversized: bool) -> dict:
    """按文档生成chunk，逐条证据做文本级覆盖判定。"""
    chunks_by_path: dict[str, list] = {}
    source_text_by_path: dict[str, str] = {}
    lengths_by_library: dict[str, list[int]] = {}
    all_chunks = []
    for document in documents:
        chunks = block_chunks(document, max_chars=MAX_CHARS, split_oversized=split_oversized)
        # PDF每个物理页是一条独立document，同一路径必须累加而不是覆盖。
        chunks_by_path.setdefault(document["source_path"], []).extend(chunks)
        source_text_by_path[document["source_path"]] = document["text"]
        lengths_by_library.setdefault(document["library"], []).extend(
            len(chunk.text) for chunk in chunks
        )
        all_chunks.extend(chunks)

    lengths = [len(chunk.text) for chunk in all_chunks]
    checks = []
    for row in validation:
        for evidence in row["evidence"]:
            chunks = chunks_by_path.get(evidence["path"], [])
            record = {
                "id": row["id"],
                "library": row["library"],
                "path": evidence["path"],
                "page": evidence.get("page"),
                "start_line": evidence.get("start_line"),
                "end_line": evidence.get("end_line"),
            }
            if evidence.get("page") is not None:
                # PDF证据只登记物理页，没有行号或锚点，因此只能判定"该页有chunk"。
                # 这是**弱口径**：它不说明证据内容真的落在切出来的文本里。
                hits = [chunk for chunk in chunks if chunk.page_number == evidence["page"]]
                record.update(
                    level="page",
                    coverage_basis=PAGE_BASIS,
                    hits=len(hits),
                    contained_in_one_chunk=bool(hits),
                    covered_by_chunk_union=bool(hits),
                    matching_chunks=[],
                )
            else:
                start, end = evidence["start_line"], evidence["end_line"]
                source_lines = source_text_by_path[evidence["path"]].splitlines(keepends=True)
                expected = normalize("".join(source_lines[start - 1 : end]))
                hits = [
                    chunk for chunk in chunks if chunk.start_line <= end and chunk.end_line >= start
                ]
                joined = normalize("".join(chunk.text for chunk in hits))
                single = [
                    chunk
                    for chunk in chunks
                    if chunk.start_line <= start and chunk.end_line >= end
                    and expected in normalize(chunk.text)
                ]
                record.update(
                    level="line",
                    coverage_basis=TEXT_BASIS,
                    hits=len(hits),
                    contained_in_one_chunk=bool(expected) and bool(single),
                    covered_by_chunk_union=bool(expected) and expected in joined,
                    matching_chunks=[chunk.chunk_id for chunk in single],
                )
            record["evidence_chars"] = (
                None if evidence.get("page") is not None else len(expected)
            )
            checks.append(record)

    failures = [item for item in checks if not item["covered_by_chunk_union"]]
    per_library = {
        library: {
            **length_stats(lengths),
            "over_max_chars": sum(length > MAX_CHARS for length in lengths),
        }
        for library, lengths in sorted(lengths_by_library.items())
    }
    return {
        "strategy": "blocks-split" if split_oversized else "blocks",
        "max_chars": MAX_CHARS,
        "documents": len(documents),
        "chunk_ids_unique": len({chunk.chunk_id for chunk in all_chunks}) == len(all_chunks),
        "length": length_stats(lengths),
        "length_histogram": histogram(lengths),
        "over_max_chars": sum(length > MAX_CHARS for length in lengths),
        # 单行chunk：可能是单行块，也可能是超长行被硬切出的片段。
        "single_line_chunks": sum(
            1 for chunk in all_chunks if chunk.start_line == chunk.end_line
        ),
        "per_library": per_library,
        "evidence_spans": len(checks),
        "page_level_spans": sum(item["level"] == "page" for item in checks),
        "evidence_hit": sum(bool(item["hits"]) for item in checks),
        "evidence_in_one_chunk": sum(item["contained_in_one_chunk"] for item in checks),
        "evidence_covered_by_chunk_union": sum(item["covered_by_chunk_union"] for item in checks),
        "multi_chunk_evidence": [
            {key: item[key] for key in ("id", "path", "start_line", "end_line", "hits", "evidence_chars")}
            for item in checks
            if item["covered_by_chunk_union"] and not item["contained_in_one_chunk"]
        ],
        "failures": failures,
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="比较两种block切片的长度分布与validation覆盖")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    manifests = sorted((ROOT / "corpora").glob("*/source-manifest.json"))
    documents = [
        document
        for manifest in manifests
        for document in load_retrieval_documents(manifest, ROOT / "raw_data")
    ]
    validation = [
        json.loads(line)
        for line in (ROOT / "evaluation/datasets/rag-validation-001.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    strategies = {
        name: evaluate_strategy(documents, validation, split_oversized=split)
        for name, split in (("blocks", False), ("blocks-split", True))
    }
    legacy, fixed = strategies["blocks"], strategies["blocks-split"]
    report = {
        "status": "PASS" if not fixed["failures"] else "FAIL",
        "dataset": "evaluation/datasets/rag-validation-001.jsonl",
        "evidence_match": (
            "命中同一证据的chunk按原文顺序拼接后必须包含证据原文（折叠空白后比较）；"
            "PDF证据只登记物理页，按页判定并单列"
        ),
        # 逐条记录都带 coverage_basis；这里再给出全表对照，避免把两种口径混着读。
        "metric_semantics": METRIC_SEMANTICS,
        "boundary": (
            "只验证切片是否覆盖证据原文，未生成Embedding，也未做任何检索排名；"
            "覆盖不等于检索时能召回"
        ),
        "strategies": strategies,
        "comparison": {
            "chunks": {"blocks": legacy["length"]["chunks"], "blocks-split": fixed["length"]["chunks"]},
            "over_max_chars": {
                "blocks": legacy["over_max_chars"],
                "blocks-split": fixed["over_max_chars"],
            },
            "longest_chunk": {"blocks": legacy["length"]["max"], "blocks-split": fixed["length"]["max"]},
            "evidence_in_one_chunk": {
                "blocks": legacy["evidence_in_one_chunk"],
                "blocks-split": fixed["evidence_in_one_chunk"],
            },
            "evidence_covered_by_chunk_union": {
                "blocks": legacy["evidence_covered_by_chunk_union"],
                "blocks-split": fixed["evidence_covered_by_chunk_union"],
            },
        },
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["comparison"], ensure_ascii=False, indent=2))
    print(f"报告：{arguments.output}")
    raise SystemExit(report["status"] != "PASS")


if __name__ == "__main__":
    main()
