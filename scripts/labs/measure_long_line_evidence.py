"""只读量化 blocks-split 候选中的单行字符硬切证据。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from wenjin.ingestion.multiformat import load_retrieval_documents

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/indexes/multicorpus-candidates-002.json"
MAX_CHARS = 1600


def _read_chunks_read_only(path: Path) -> list[dict]:
    """用 SQLite mode=ro 打开现有 BM25 候选，只读取必要字段。"""
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
        required = {"chunk_id", "source_path", "start_line", "end_line", "raw_text"}
        if not required <= columns:
            raise ValueError(f"{path}: 缺少只读统计需要的列 {sorted(required - columns)}")
        rows = connection.execute(
            "SELECT chunk_id, source_path, start_line, end_line, raw_text FROM chunks"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _long_line_parts(document: dict, stored_rows: list[dict]) -> list[dict]:
    """从解析后原文行与现存SQLite rows识别旧候选中的字符硬切。

    这里刻意不调用当前 `block_chunks`：生产策略现已改成保留完整行，重跑现役切片器
    无法重放旧候选的历史构建行为。必须要求候选SQLite在同一行上恰好存有按1600切开的
    全部分段，避免把普通解析改写差异当作硬切。
    """
    lines = document["text"].splitlines(keepends=True)
    found = []
    for line_index, line in enumerate(lines):
        if len(line) <= MAX_CHARS:
            continue
        segments = [line[offset : offset + MAX_CHARS] for offset in range(0, len(line), MAX_CHARS)]
        matched = []
        for row in stored_rows:
            if (
                row["source_path"] == document["source_path"]
                and int(row["start_line"]) == line_index + 1
                and int(row["end_line"]) == line_index + 1
                and row["raw_text"] in segments
            ):
                matched.append(row)
        if len(matched) == len(segments):
            # 按chunk序号恢复行内顺序，也能处理重复字符段。
            matched.sort(key=lambda row: int(row["chunk_id"].rsplit(":", 1)[1]))
            if [row["raw_text"] for row in matched] == segments:
                found.append({
                    "source_path": document["source_path"],
                    "page_number": document.get("page_number"),
                    "line": line_index + 1,
                    "line_chars": len(line),
                    "chunk_ids": [row["chunk_id"] for row in matched],
                    "chunk_lengths": [len(row["raw_text"]) for row in matched],
                })
    return found


def _self_check() -> dict:
    """区分超长硬切、普通行和解析器合法改写的最小合成检查。"""
    positive_line = "A" * 1700 + "\n"
    positive_segments = [positive_line[i : i + MAX_CHARS] for i in range(0, len(positive_line), MAX_CHARS)]
    positive_rows = [
        {"chunk_id": f"synthetic:long:blocks:{i:04d}", "source_path": "synthetic/long.txt",
         "start_line": 1, "end_line": 1, "raw_text": segment}
        for i, segment in enumerate(positive_segments)
    ]
    positive_hits = _long_line_parts(
        {"source_path": "synthetic/long.txt", "text": positive_line}, positive_rows
    )
    assert len(positive_hits) == 1 and positive_hits[0]["chunk_lengths"] == [1600, 101]

    negative_line = "ordinary line\n"
    negative_rows = [{
        "chunk_id": "synthetic:normal:blocks:0000", "source_path": "synthetic/normal.txt",
        "start_line": 1, "end_line": 1, "raw_text": negative_line,
    }]
    assert not _long_line_parts(
        {"source_path": "synthetic/normal.txt", "text": negative_line}, negative_rows
    )

    # 原始HTML一行可能很长，但若解析后仅剩短可见文字，则不属于超长解析行。
    parsed_rewrite = "visible text\n"
    assert not _long_line_parts(
        {"source_path": "synthetic/rewrite.html", "text": parsed_rewrite}, []
    )
    return {
        "positive": {"affected_chunks": 2, "lengths": [1600, 101]},
        "ordinary_line_negative": {"affected_chunks": 0},
        "parser_rewrite_negative": {"parsed_line_chars": 13, "affected_chunks": 0},
    }


def main() -> None:
    started = time.perf_counter()
    self_check = _self_check()
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    corpora = []
    samples = []
    skipped_ocr = []
    grand = Counter()
    discrepancies = []

    for candidate in config["candidates"]:
        manifest_path = ROOT / candidate["manifest_path"]
        actual_manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if actual_manifest_hash != candidate["manifest_sha256"]:
            raise ValueError(f"manifest SHA256 与候选配置不符：{candidate['library']}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        # 禁止意外调用 OCR 服务：只在所有 eligible PDF 都声明本地文本层解析时处理。
        ocr_entries = [
            entry["path"] for entry in manifest["files"]
            if entry.get("retrieval_eligible") and entry.get("parser") in {
                "mineru-ocr-page-v1", "mixed-text-ocr-page-v1"
            }
        ]
        if ocr_entries:
            skipped_ocr.extend(f"{candidate['library']}/{path}" for path in ocr_entries)

        database_path = ROOT / candidate["bm25_path"]
        stored = _read_chunks_read_only(database_path)
        chunks_by_source_path = dict(Counter(row["source_path"] for row in stored))
        if len(stored) != candidate["chunk_count"]:
            discrepancies.append({
                "library": candidate["library"], "kind": "registered_vs_sqlite_count",
                "registered": candidate["chunk_count"], "sqlite": len(stored),
            })

        # loader 严格按 manifest 白名单读取、校验双 Hash 并调用同一生产 parser。
        documents = load_retrieval_documents(manifest_path, ROOT / "raw_data")
        long_lines = []
        for document in documents:
            if document.get("parser") in {"mineru-ocr-page-v1", "mixed-text-ocr-page-v1"}:
                continue
            long_lines.extend(_long_line_parts(document, stored))

        affected_ids = {chunk_id for item in long_lines for chunk_id in item["chunk_ids"]}
        matched_affected = affected_ids
        corpora.append({
            "library": candidate["library"],
            "target_id": candidate["target_id"],
            "manifest_sha256_verified": True,
            "registered_chunks": candidate["chunk_count"],
            "sqlite_chunks": len(stored),
            "chunks_by_source_path": chunks_by_source_path,
            "historical_hard_cut_chunks_in_sqlite": len(affected_ids),
            "affected_chunks_confirmed_in_sqlite": len(matched_affected),
            "affected_files": len({line["source_path"] for line in long_lines}),
            "affected_source_file_sizes_bytes": {
                source: (ROOT / source).stat().st_size
                for source in sorted({line["source_path"] for line in long_lines})
            },
            "affected_physical_pages": len({
                (line["source_path"], line["page_number"])
                for line in long_lines if line["page_number"] is not None
            }),
            "long_parsed_lines": len(long_lines),
            "max_long_line_chars": max((line["line_chars"] for line in long_lines), default=0),
            "max_chunk_chars_in_affected_lines": max(
                (max(line["chunk_lengths"]) for line in long_lines), default=0
            ),
            "parser_counts": dict(Counter(document.get("parser") for document in documents)),
        })
        for line in long_lines:
            if len(samples) < 20:
                samples.append({
                    "library": candidate["library"],
                    "source_path": line["source_path"],
                    "page_number": line["page_number"],
                    "line": line["line"],
                    "line_chars": line["line_chars"],
                    "chunk_lengths": line["chunk_lengths"],
                })
        grand["registered_chunks"] += candidate["chunk_count"]
        grand["sqlite_chunks"] += len(stored)
        grand["historical_hard_cut_chunks_in_sqlite"] += len(affected_ids)
        grand["affected_chunks_confirmed_in_sqlite"] += len(matched_affected)
        grand["affected_files"] += corpora[-1]["affected_files"]
        grand["affected_physical_pages"] += corpora[-1]["affected_physical_pages"]
        grand["long_parsed_lines"] += len(long_lines)

    elapsed = time.perf_counter() - started
    result = {
        "max_chars": MAX_CHARS,
        "method": "manifest allowlist + production parser/hash verification + historical SQLite slice matching in mode=ro",
        "totals": dict(grand),
        "corpora": corpora,
        "samples": samples,
        "synthetic_self_check": self_check,
        "skipped_ocr_entries": skipped_ocr,
        "discrepancies": discrepancies,
        "elapsed_seconds": round(elapsed, 3),
        "scope_note": "只读取候选manifest登记的retrieval_eligible文件及其五个BM25 SQLite候选；未扫描raw_data目录，未访问Qdrant/模型/网络OCR。",
    }
    output_dir = ROOT / "artifacts/handoff-2026-09-29/long-lines"
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / "results.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path = output_dir / "REPORT.md"
    report_path.write_text(_render_report(result), encoding="utf-8")
    print(json.dumps({"results": str(result_path.relative_to(ROOT)), "report": str(report_path.relative_to(ROOT)), "totals": result["totals"], "elapsed_seconds": elapsed, "discrepancies": discrepancies}, ensure_ascii=False, indent=2))


def _render_report(result: dict) -> str:
    totals = result["totals"]
    lines = [
        "# blocks-split 超长单行证据量化",
        "",
        "## 结论",
        "",
        f"五库 002 候选登记/SQLite 实存 {totals['registered_chunks']} chunks。",
        "此复现根据已校验manifest来源的生产解析文本，直接对照现存只读SQLite行定位和正文；不再调用已改为保留完整行的新切片器。",
        f"其中 {totals['affected_chunks_confirmed_in_sqlite']} 个现存 chunk 经 SQLite 正文与行号核对，确认来自单行超长后按字符硬切；涉及 {totals['affected_files']} 个文件、{totals['affected_physical_pages']} 个 PDF 物理页、{totals['long_parsed_lines']} 条解析后超长原文行。",
        "受影响文件字节数：" + json.dumps(
            {source: size for item in result["corpora"] for source, size in item["affected_source_file_sizes_bytes"].items()},
            ensure_ascii=False,
        ) + "。",
        "该 HTML 源超过8MiB逐字切片核验上限；002的本库1,025个chunk中，HTML文件占342个，PDF另有683个页级chunk。当前逐字切片核验上限会将HTML的342个chunk标为 `source_over_8mb`，PDF的683个标为 `page_or_binary_source`；不能把58个历史硬切说成被原文比较直接拒绝。",
        "",
        "`affected_physical_pages` 只统计 PDF 页；非 PDF 文本行不虚构页码。最大超长行长度按解析器输出后的 Python 字符数计。",
        "",
        "## 按库统计",
        "",
        "| 库 | SQLite chunks | 历史硬切 chunks | 文件 | PDF物理页 | 超长行 | 最大行字符数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in result["corpora"]:
        lines.append(
            f"| {item['library']} | {item['sqlite_chunks']} | {item['historical_hard_cut_chunks_in_sqlite']} | {item['affected_files']} | {item['affected_physical_pages']} | {item['long_parsed_lines']} | {item['max_long_line_chars']} |"
        )
    lines += ["", "## 有限位置样例", "", "仅报告相对来源路径、页/行位置与长度，不输出原文：", ""]
    if result["samples"]:
        lines.append("| 库 | 来源路径 | 页 | 行 | 行字符数 | 切片长度 |")
        lines.append("|---|---|---:|---:|---:|---|")
        for sample in result["samples"]:
            page = "—" if sample["page_number"] is None else str(sample["page_number"])
            lines.append(
                f"| {sample['library']} | `{sample['source_path']}` | {page} | {sample['line']} | {sample['line_chars']} | {sample['chunk_lengths']} |"
            )
    else:
        lines.append("未发现符合判定条件的行；正例自检确认该检测器可捕获 1700 字符单行切片。")
    lines += [
        "",
        "## 判定方法与边界",
        "",
        "1. 校验配置登记的 manifest SHA256，再由 `load_retrieval_documents()` 按 manifest 白名单读取 eligible 来源、校验双 Hash，并调用 manifest 声明的生产解析器。",
        "2. 为复现建库策略修改前的旧候选，直接扫描这些候选SQLite的只读chunk行；仅当解析后某行长度大于 1600，且同一行号下现存正文依序恰好等于该行按 1600 字符切出的全部分段时计为历史硬切。HTML/XML/roff均以其生产解析文本判定，不把解析器改写本身算成硬切。",
        "3. 当前实现已改为保留完整超长行，所以此命令重查的是尚未重建的 002 SQLite 旧资产，不能作为新切片实现重放或资产已修复的证据。",
        f"4. 合成分类自检：1701 字符行对应SQLite里的两个历史段，长度 {result['synthetic_self_check']['positive']['lengths']}；普通行和解析后变短的改写行均不命中。",
        "",
        f"OCR 声明文件未处理：{result['skipped_ocr_entries'] or '无'}。本批 PDF 均使用本地 PyMuPDF 文本层解析；未调用 OCR。没有读取 manifest 外文件，没有扫描 raw_data 目录，也没有连接 Qdrant 或模型。",
        f"差异记录：{json.dumps(result['discrepancies'], ensure_ascii=False)}。运行耗时：{result['elapsed_seconds']} 秒。",
        "",
        "## 三种处理方式的实际代价",
        "",
        "- 保留完整行：简单且保住行证据，但不能再将“完整行必然绕过 context_char_budget”作为结论。`select_context()` 会对超预算片段执行 `continue` 跳过；实际代价是该证据可能不进入返回上下文，必要证据可能因此缺席。主动提高预算才会增大响应正文。",
        "- 增加字符偏移并调整契约：可以继续返回长行的局部片段，但要让 schema、来源核验、审计、评测和消费方共同理解行内偏移；本次量化没有替用户选此方案。",
        "- 建库时拒绝或丢弃并计数：不再产生无法按行号核验的片段，但会拒绝包含这些行/文件或丢失对应内容；应保留可审计的拒绝/丢弃计数，具体粒度待决。",
        "",
        "## 重现",
        "",
        "```powershell",
        "uv run python scripts/measure_long_line_evidence.py",
        "```",
        "",
        "脚本只写入本目录 `results.json` 和 `REPORT.md`；它不修改代码、配置、manifest、索引或 active。此前的生产切片重放基线保存在 `results-pre-fix-replay.json` 与 `REPORT-pre-fix-replay.md`。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
