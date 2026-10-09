"""独立验证普通chunk是否逐字来自通过Hash校验的来源。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin import manifest as manifest_module
from wenjin.content_hashing import BINARY_IDENTITY_V1, TEXT_NEWLINE_LF_V1, hash_content
from wenjin.index_audit import audit_index
from wenjin.index_registry import IndexTarget
from wenjin.ingestion.multiformat import parse_line_mapped, parser_for_path


class FakeQdrant:
    """只提供 audit_index 所需的 collection 状态、点数与 payload。"""

    def __init__(self, payload: dict | list[dict]):
        payloads = payload if isinstance(payload, list) else [payload]
        self.payloads = [dict(item) for item in payloads]
        self.points = len(self.payloads)

    def get_collection(self, name):
        return SimpleNamespace(
            status=SimpleNamespace(value="green"),
            points_count=self.points,
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=3))),
        )

    def count(self, name, *, count_filter, exact):
        return SimpleNamespace(count=self.points)

    def scroll(self, name, *, limit, offset, with_payload, with_vectors):
        return ([SimpleNamespace(payload=dict(item)) for item in self.payloads], None)


def _write_bm25(path: Path, payload: dict | list[dict], *, parser_columns: bool) -> None:
    payloads = payload if isinstance(payload, list) else [payload]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as database:
        parser_columns_sql = ", parser, ocr_derived" if parser_columns else ""
        database.execute(
            "CREATE TABLE chunks (chunk_id, source_path, version, start_line, end_line, "
            "strategy, content_sha256, source_raw_sha256, source_canonical_sha256, "
            "normalization, page_number, logical_document_id" + parser_columns_sql
            + ", retrieval_text, raw_text)"
        )
        for item in payloads:
            fields = [
                item["chunk_id"], item["source_path"], item["version"],
                item["start_line"], item["end_line"], item["strategy"],
                item["content_sha256"], item["source_raw_sha256"],
                item["source_canonical_sha256"], item["normalization"],
                item.get("page_number"), item.get("logical_document_id"),
            ]
            if parser_columns:
                fields.extend([item.get("parser"), int(bool(item.get("ocr_derived", False)))])
            fields.extend(["", item["text"]])
            database.execute(
                "INSERT INTO chunks VALUES (" + ",".join("?" for _ in fields) + ")", fields
            )
        database.commit()


def make_assets(
    tmp_path: Path,
    *,
    raw_bytes: bytes,
    parser: str | None = None,
    suffix: str = ".txt",
    text: str | None = None,
    start_line: int = 1,
    end_line: int = 1,
    legacy: bool = False,
    binary: bool = False,
    page_number: int | None = None,
    ocr_derived: bool = False,
    normalization: str | None = None,
    source_encoding: str | None = None,
) -> dict:
    """写真实临时manifest、原文和SQLite；不连接任何真实索引资产。"""
    corpus_id = "sample-1.0"
    library, version = "sample", "1.0"
    relative = f"doc{suffix}"
    source_path = f"raw_data/{library}/{version}/{relative}"
    source_file = tmp_path / "raw_data" / library / version / relative
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(raw_bytes)

    profile = normalization or (BINARY_IDENTITY_V1 if binary else TEXT_NEWLINE_LF_V1)
    encoding = source_encoding if source_encoding is not None else (None if binary else "utf-8")
    try:
        hashed = hash_content(raw_bytes, normalization=profile, encoding=encoding)
    except (UnicodeDecodeError, LookupError, ValueError):
        # 为解码/profile失败负例构造结构自洽的manifest；内容核验必须随后拒绝原文。
        raw_digest = hashlib.sha256(raw_bytes).hexdigest()
        hashed = SimpleNamespace(raw_sha256=raw_digest, canonical_sha256=raw_digest)
    effective_parser = parser or (None if legacy else parser_for_path(Path(relative)))
    entry = {
        "path": relative,
        "raw_sha256": hashed.raw_sha256,
        "canonical_sha256": hashed.canonical_sha256,
        "normalization": profile,
        "encoding": encoding,
        "logical_document_id": f"{corpus_id}:{relative}",
        "parser": effective_parser,
    }
    entry["indexable" if legacy else "retrieval_eligible"] = True
    if legacy:
        entry.pop("parser")
    manifest = {
        "schema_version": 3,
        "corpus_id": corpus_id,
        "library": library,
        "version": version,
        "raw_data_subdir": f"{library}/{version}",
        "license": "synthetic-test-only",
        "files": [entry],
    }
    manifest["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(manifest)
    manifest["retrieval_content_sha256"] = manifest_module.retrieval_fingerprint(manifest)
    manifest_path = tmp_path / "corpora" / corpus_id / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    chunk_text = text if text is not None else raw_bytes.decode("utf-8")
    chunk_id = f"1.0:{relative}:blocks:0000"
    payload = {
        "chunk_id": chunk_id,
        "source_path": source_path,
        "version": version,
        "start_line": start_line,
        "end_line": end_line,
        "strategy": "blocks",
        "text": chunk_text,
        "content_sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        "source_raw_sha256": hashed.raw_sha256,
        "source_canonical_sha256": hashed.canonical_sha256,
        "normalization": profile,
        "page_number": page_number,
        "logical_document_id": entry["logical_document_id"],
        "parser": effective_parser,
        "ocr_derived": ocr_derived,
    }
    if legacy:
        # 模拟旧索引和旧Qdrant payload都没有parser/OCR标记字段。
        payload.pop("parser")
        payload.pop("ocr_derived")
    bm25_path = tmp_path / "candidate.sqlite"
    _write_bm25(bm25_path, payload, parser_columns=not legacy)
    target = IndexTarget(
        target_id="sample-1.0-blocks-synthetic",
        corpus_id=corpus_id,
        library=library,
        version=version,
        strategy="blocks",
        dense_collection="synthetic_collection",
        bm25_path="candidate.sqlite",
        manifest_path=f"corpora/{corpus_id}/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=1,
        vector_dimension=3,
        embedding_model="test",
        canonical_corpus_sha256=manifest["retrieval_content_sha256"],
        normalization_profile=profile,
    )
    return {
        "root": tmp_path,
        "source_file": source_file,
        "manifest_path": manifest_path,
        "bm25_path": bm25_path,
        "target": target,
        "payload": payload,
    }


def run_audit(assets: dict, payload: dict | list[dict] | None = None) -> dict:
    return audit_index(
        assets["target"], root=assets["root"],
        qdrant_client=FakeQdrant(payload or assets["payload"]),
    )


def _replace_bm25_payload(assets: dict, payload: dict | list[dict]) -> None:
    """让BM25与Qdrant拥有同一份篡改正文，避免先命中两侧不一致分支。"""
    with closing(sqlite3.connect(assets["bm25_path"])) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(chunks)")}
    _write_bm25(assets["bm25_path"], payload, parser_columns="parser" in columns)


def _source_report(report: dict) -> dict:
    assert "source_text_audit" in report, "审计报告必须显式列出原文核验覆盖"
    return report["source_text_audit"]


def test_synchronized_forgery_with_recomputed_hash_is_rejected(tmp_path):
    assets = make_assets(tmp_path, raw_bytes=b"trusted source line\n")
    forged = "fabricated but internally consistent\n"
    payload = {
        **assets["payload"],
        "text": forged,
        "content_sha256": hashlib.sha256(forged.encode()).hexdigest(),
    }
    _replace_bm25_payload(assets, payload)

    report = run_audit(assets, payload)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_text_mismatch"]
    audit = _source_report(report)
    assert audit["mismatched_chunks"] == 1
    assert audit["verified_chunks"] == 0
    assert audit["verifiable"] is False


def test_position_that_points_to_different_valid_lines_is_rejected(tmp_path):
    assets = make_assets(
        tmp_path, raw_bytes=b"first real line\nsecond real line\n",
        text="second real line\n", start_line=1, end_line=1,
    )

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_text_mismatch"]


def test_plain_text_correct_slice_is_verified(tmp_path):
    source = b"one\ntwo\nthree\n"
    assets = make_assets(tmp_path, raw_bytes=source, text="two\n", start_line=2, end_line=2)

    report = run_audit(assets)

    assert report["status"] == "PASS", report["problems"]
    audit = _source_report(report)
    assert audit["verified_chunks"] == 1
    assert audit["unverifiable_chunks"] == 0
    assert audit["sources_hash_verified"] == 1
    assert audit["verifiable"] is True


def test_parser_and_hash_share_the_same_cached_source_bytes(tmp_path, monkeypatch):
    source = b"trusted initial bytes\nsecond trusted line\n"
    assets = make_assets(
        tmp_path, raw_bytes=source, text="trusted initial bytes\n", start_line=1, end_line=1,
    )
    second = {
        **assets["payload"],
        "chunk_id": "1.0:doc.txt:blocks:0001",
        "start_line": 2,
        "end_line": 2,
        "text": "second trusted line\n",
    }
    second["content_sha256"] = hashlib.sha256(second["text"].encode()).hexdigest()
    _write_bm25(assets["bm25_path"], [assets["payload"], second], parser_columns=True)
    assets["payload"] = [assets["payload"], second]
    assets["target"] = replace(assets["target"], chunk_count=2)
    changed = b"different bytes after first read\n"
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    reads = 0
    source_text_reads = 0
    import wenjin.ingestion.multiformat as multiformat

    original_map = multiformat.parse_line_mapped_text
    parser_calls = 0

    def count_parser_calls(text: str, parser: str) -> str:
        nonlocal parser_calls
        parser_calls += 1
        return original_map(text, parser)

    monkeypatch.setattr(multiformat, "parse_line_mapped_text", count_parser_calls)

    def read_then_replace(path: Path) -> bytes:
        nonlocal reads
        data = original_read_bytes(path)
        if path.resolve() == assets["source_file"].resolve():
            reads += 1
            if reads == 1:
                # 模拟来源在校验读取后被并发替换；后续解析必须使用首读的缓存字节。
                path.write_bytes(changed)
        return data

    def count_source_read_text(path: Path, *args, **kwargs):
        nonlocal source_text_reads
        if path.resolve() == assets["source_file"].resolve():
            source_text_reads += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    monkeypatch.setattr(Path, "read_text", count_source_read_text)
    report = run_audit(assets)

    assert reads == 1
    assert source_text_reads == 0, "来源位置校验应复用按Hash核验的行快照"
    assert parser_calls == 1, "同一來源的多个chunk应复用一次解析结果"
    assert report["status"] == "PASS", report["problems"]
    audit = _source_report(report)
    assert audit["verified_chunks"] == 2
    assert audit["sources_considered"] == 1
    assert audit["sources_hash_verified"] == 1


@pytest.mark.parametrize(
    ("suffix", "parser", "raw"),
    [
        (".html", "html-line-v1", b"<p>visible words</p>\n"),
        (".xml", "xml-line-v1", b"<root key=\"value\">body</root>\n"),
        (".1", "man-roff-line-v1", b".SH NAME\nbody text\n"),
    ],
)
def test_supported_line_parsers_verify_their_parsed_source(tmp_path, suffix, parser, raw):
    source_file = tmp_path / "raw_data" / "sample" / "1.0" / f"doc{suffix}"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(raw)
    parsed = parse_line_mapped(source_file, f"raw_data/sample/1.0/doc{suffix}", parser)
    assets = make_assets(
        tmp_path, raw_bytes=raw, parser=parser, suffix=suffix,
        text=parsed.text, start_line=1, end_line=len(parsed.text.splitlines()),
    )

    report = run_audit(assets)

    assert report["status"] == "PASS", report["problems"]
    assert _source_report(report)["verified_chunks"] == 1


def test_legacy_missing_parser_column_still_checks_trusted_plain_source(tmp_path):
    assets = make_assets(
        tmp_path, raw_bytes=b"real legacy source\n", legacy=True,
        text="forged legacy source\n",
    )

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_text_mismatch"]
    assert _source_report(report)["mismatched_chunks"] == 1


def test_legacy_final_line_without_newline_is_verified_without_multiformat_lf(tmp_path):
    assets = make_assets(
        tmp_path, raw_bytes=b"first line\nlast line without newline",
        text="last line without newline", start_line=2, end_line=2, legacy=True,
    )

    report = run_audit(assets)

    assert report["status"] == "PASS", report["problems"]
    assert _source_report(report)["verified_chunks"] == 1


def test_missing_source_fails_closed(tmp_path):
    assets = make_assets(tmp_path, raw_bytes=b"source exists in manifest\n")
    assets["source_file"].unlink()

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_content_invalid"]
    assert _source_report(report)["sources_hash_failed"] == 1


def test_source_raw_hash_mismatch_fails_closed(tmp_path):
    assets = make_assets(tmp_path, raw_bytes=b"initial source\n")
    assets["source_file"].write_bytes(b"changed after manifest hash\n")

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_content_invalid"]
    assert _source_report(report)["sources_hash_failed"] == 1


def test_strict_decode_failure_fails_closed(tmp_path):
    assets = make_assets(
        tmp_path, raw_bytes=b"valid prefix\n\xff\xfe", parser="text-line-v1",
        text="valid prefix\n\ufffd\ufffd",
    )

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_content_invalid"]


@pytest.mark.parametrize(
    ("binary", "encoding"),
    [(True, "utf-8"), (False, "unknown-codec-v999")],
)
def test_invalid_binary_encoding_and_unknown_codec_fail_closed(tmp_path, binary, encoding):
    assets = make_assets(
        tmp_path, raw_bytes=b"ordinary text\n", binary=binary,
        source_encoding=encoding,
    )

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_content_invalid"]


def test_source_over_8mib_is_replayed_and_exactly_verified(tmp_path):
    raw = b"first line\n" + b"x" * (8 * 1024 * 1024)
    assets = make_assets(tmp_path, raw_bytes=raw, text="first line\n", end_line=1)

    report = run_audit(assets)

    audit = _source_report(report)
    assert audit["total_chunks"] == 1
    assert audit["verified_chunks"] == 1
    assert audit["unverifiable_chunks"] == 0
    assert audit["sources_parser_replayed"] == 1
    assert audit["sources_hash_verified"] == 1
    assert audit["verifiable"] is True


@pytest.mark.parametrize("source_size", [32, 8 * 1024 * 1024 + 1])
def test_unknown_normalization_fails_for_small_and_streamed_sources(tmp_path, source_size):
    raw = b"x" * source_size
    assets = make_assets(
        tmp_path, raw_bytes=raw, text="x" * min(source_size, 1),
        normalization="unknown-normalization-v999",
    )

    report = run_audit(assets)

    assert report["status"] == "FAIL"
    assert report["problems"]["source_content_invalid"]
    assert _source_report(report)["sources_hash_failed"] == 1


@pytest.mark.parametrize(
    ("parser", "ocr"),
    [("pymupdf-page-v1", False), ("mineru-ocr-page-v1", True)],
)
def test_pdf_text_layer_is_verified_but_ocr_remains_unverified(tmp_path, parser, ocr):
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "page body from parser")
    expected_text = page.get_text(sort=True)
    raw = pdf.tobytes()
    pdf.close()
    assets = make_assets(
        tmp_path, raw_bytes=raw, parser=parser, suffix=".pdf", binary=True,
        text=("OCR derived page text\n" if ocr else expected_text),
        page_number=1, ocr_derived=ocr,
    )

    report = run_audit(assets)

    audit = _source_report(report)
    if ocr:
        assert audit["verified_chunks"] == 0
        assert audit["unverifiable_chunks"] == 1
        assert audit["page_level_unchecked"] == 1
        assert audit["limits_by_reason"]["ocr_derived_text"] == 1
        assert audit["verifiable"] is False
    else:
        assert audit["verified_chunks"] == 1
        assert audit["unverifiable_chunks"] == 0
        assert audit["page_level_unchecked"] == 0
        assert audit["sources_parser_replayed"] == 1
        assert audit["verifiable"] is True


def test_unknown_parser_is_reported_as_unverifiable(tmp_path):
    assets = make_assets(
        tmp_path, raw_bytes=b"some source\n", parser="unknown-parser-v999",
    )

    report = run_audit(assets)

    audit = _source_report(report)
    assert audit["unverifiable_chunks"] == 1
    assert audit["limits_by_reason"]["unknown_parser"] == 1
    assert audit["verifiable"] is False


def test_hard_cut_inside_long_line_fails_but_full_line_succeeds(tmp_path):
    full_line = "A" * 1701 + "\n"
    assets = make_assets(
        tmp_path, raw_bytes=full_line.encode(), text=full_line[:1600],
        start_line=1, end_line=1,
    )
    split_report = run_audit(assets)
    assert split_report["status"] == "FAIL"
    assert split_report["problems"]["source_text_mismatch"]

    # 同一原文完整行是合法的行级证据；长于目标chunk预算并不使原文定位失真。
    full_assets = make_assets(
        tmp_path / "complete", raw_bytes=full_line.encode(), text=full_line,
        start_line=1, end_line=1,
    )
    full_report = run_audit(full_assets)
    assert full_report["status"] == "PASS", full_report["problems"]
    assert _source_report(full_report)["verified_chunks"] == 1
