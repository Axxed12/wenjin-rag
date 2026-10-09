"""阶段一来源逐字核验的独立合成契约测试。

所有索引、manifest、HTML、PDF 均在 tmp_path 中创建；不连接正式索引或模型。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin import index_audit as audit_module
from wenjin import manifest as manifest_module
from wenjin.content_hashing import (
    BINARY_IDENTITY_V1,
    TEXT_NEWLINE_LF_V1,
    hash_content,
)
from wenjin.index_audit import audit_index
from wenjin.index_registry import IndexTarget


class FakeQdrant:
    def __init__(self, payloads: list[dict]):
        self.payloads = [dict(item) for item in payloads]

    def get_collection(self, _name):
        return SimpleNamespace(
            status=SimpleNamespace(value="green"),
            points_count=len(self.payloads),
            config=SimpleNamespace(params=SimpleNamespace(vectors=SimpleNamespace(size=3))),
        )

    def count(self, _name, *, count_filter, exact):
        return SimpleNamespace(count=len(self.payloads))

    def scroll(self, _name, *, limit, offset, with_payload, with_vectors):
        return ([SimpleNamespace(payload=dict(row)) for row in self.payloads], None)


def _write_bm25(path: Path, payloads: list[dict], *, legacy: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as db:
        marker_columns = "" if legacy else ", parser, ocr_derived"
        db.execute(
            "CREATE TABLE chunks (chunk_id, source_path, version, start_line, end_line, "
            "strategy, content_sha256, source_raw_sha256, source_canonical_sha256, "
            "normalization, page_number, logical_document_id" + marker_columns
            + ", retrieval_text, raw_text)"
        )
        for row in payloads:
            values = (
                row["chunk_id"], row["source_path"], row["version"],
                row["start_line"], row["end_line"], row["strategy"],
                row["content_sha256"], row["source_raw_sha256"],
                row["source_canonical_sha256"], row["normalization"],
                row.get("page_number"), row["logical_document_id"],
            )
            if not legacy:
                values += (row.get("parser"), int(bool(row.get("ocr_derived", False))))
            values += ("", row["text"])
            db.execute("INSERT INTO chunks VALUES (" + ",".join("?" for _ in values) + ")", values)
        db.commit()


def _make_assets(
    root: Path,
    *,
    raw: bytes,
    suffix: str,
    parser: str,
    rows: list[dict],
    binary: bool = False,
    legacy: bool = False,
    provenance: dict | None = None,
) -> dict:
    corpus_id, library, version = "fixture-1.0", "fixture", "1.0"
    relative = f"spec{suffix}"
    source_path = f"raw_data/{library}/{version}/{relative}"
    source_file = root / source_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(raw)

    normalization = BINARY_IDENTITY_V1 if binary else TEXT_NEWLINE_LF_V1
    encoding = None if binary else "utf-8"
    digests = hash_content(raw, normalization=normalization, encoding=encoding)
    entry = {
        "path": relative,
        "raw_sha256": digests.raw_sha256,
        "canonical_sha256": digests.canonical_sha256,
        "normalization": normalization,
        "encoding": encoding,
        "logical_document_id": f"{corpus_id}:{relative}",
        "parser": parser,
        "indexable": not binary,
    }
    entry["indexable" if legacy else "retrieval_eligible"] = True
    if legacy:
        entry.pop("parser")
    if provenance is not None:
        entry["provenance"] = provenance
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
    manifest_path = root / "corpora" / corpus_id / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    payloads = []
    for index, source_row in enumerate(rows):
        text = source_row["text"]
        row = {
            "chunk_id": f"1.0:{relative}:blocks:{index:04d}",
            "source_path": source_path,
            "version": version,
            "start_line": source_row.get("start_line", 1),
            "end_line": source_row.get("end_line", source_row.get("start_line", 1)),
            "strategy": "blocks",
            "text": text,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "source_raw_sha256": digests.raw_sha256,
            "source_canonical_sha256": digests.canonical_sha256,
            "normalization": normalization,
            "page_number": source_row.get("page_number"),
            "logical_document_id": f"{corpus_id}:{relative}",
            "parser": source_row.get("unit_parser", parser),
            "ocr_derived": source_row.get("ocr_derived", False),
        }
        if legacy:
            row.pop("parser")
            row.pop("ocr_derived")
        payloads.append(row)

    bm25_path = root / "candidate.sqlite"
    _write_bm25(bm25_path, payloads, legacy=legacy)
    target = IndexTarget(
        target_id=f"{corpus_id}-blocks-test",
        corpus_id=corpus_id,
        library=library,
        version=version,
        strategy="blocks",
        dense_collection="phase1_fixture",
        bm25_path="candidate.sqlite",
        manifest_path=f"corpora/{corpus_id}/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=len(payloads),
        vector_dimension=3,
        embedding_model="synthetic-test",
        canonical_corpus_sha256=manifest["retrieval_content_sha256"],
        normalization_profile=normalization,
    )
    return {
        "root": root,
        "source_file": source_file,
        "manifest_path": manifest_path,
        "bm25_path": bm25_path,
        "target": target,
        "payloads": payloads,
        "manifest": manifest,
    }


def _audit(assets: dict, payloads: list[dict] | None = None) -> dict:
    return audit_index(
        assets["target"],
        root=assets["root"],
        qdrant_client=FakeQdrant(payloads or assets["payloads"]),
    )


def _replace_both_sides(assets: dict, payloads: list[dict]) -> dict:
    _write_bm25(assets["bm25_path"], payloads, legacy="parser" not in payloads[0])
    return _audit(assets, payloads)


def test_large_html_is_replayed_once_from_hash_snapshot_and_temp_is_removed(tmp_path, monkeypatch):
    import wenjin.ingestion.multiformat as multiformat

    # 十个独立源码行使文件超过旧8MiB门槛；chunk仍只引用两个短的可见HTML行。
    filler = b"<!--" + b"x" * (1024 * 1024) + b"-->\n"
    raw = b"<p>first visible</p>\n" + filler * 9 + b"<p>last visible</p>\n"
    assert len(raw) > 8 * 1024 * 1024
    rows = [
        {"text": "first visible\n", "start_line": 1, "end_line": 1},
        {"text": "last visible\n", "start_line": 11, "end_line": 11},
    ]
    assets = _make_assets(
        tmp_path, raw=raw, suffix=".html", parser="html-line-v1", rows=rows,
    )

    parse_calls = 0
    original_parse = multiformat.parse_line_mapped

    def count_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(multiformat, "parse_line_mapped", count_parse)
    original_hash = audit_module._stream_content_hashes
    replaced_source = b"<p>tampered after snapshot</p>\n" + filler * 9

    def hash_then_replace(path, normalization, encoding, *, snapshot_path=None):
        hashes = original_hash(path, normalization, encoding, snapshot_path=snapshot_path)
        if path.resolve() == assets["source_file"].resolve():
            path.write_bytes(replaced_source)
        return hashes

    monkeypatch.setattr(audit_module, "_stream_content_hashes", hash_then_replace)
    import tempfile

    original_tempdir = tempfile.TemporaryDirectory
    temp_paths: list[Path] = []

    def track_tempdir(*args, **kwargs):
        directory = original_tempdir(*args, **kwargs)
        temp_paths.append(Path(directory.name))
        return directory

    monkeypatch.setattr(audit_module.tempfile, "TemporaryDirectory", track_tempdir)
    report = _audit(assets)

    assert report["status"] == "PASS", json.dumps(report, ensure_ascii=False, indent=2)
    audit = report["source_text_audit"]
    assert audit["verified_chunks"] == 2
    assert audit["sources_considered"] == 1
    assert audit["sources_parser_replayed"] == 1
    assert parse_calls == 1, "同一HTML源的多个chunk不得重复解析"
    assert assets["source_file"].read_bytes() == replaced_source
    assert temp_paths and all(not path.exists() for path in temp_paths)


def test_large_legacy_html_keeps_historical_canonical_text_and_final_line(tmp_path):
    raw = b"<p>literal legacy text</p>\n" + b"x" * (8 * 1024 * 1024) + b"\nlegacy final line"
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".html",
        parser="html-line-v1",
        rows=[{"text": "legacy final line", "start_line": 3, "end_line": 3}],
        legacy=True,
    )
    report = _audit(assets)
    assert report["status"] == "PASS", report["problems"]
    assert report["source_text_audit"]["verified_chunks"] == 1


def _pdf_fixture(tmp_path: Path, *, forged_page: int | None = None, bad_range=None) -> dict:
    import pymupdf

    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 72), "first page evidence")
    document.new_page()  # 空白物理页必须保留，后续页码不可前移
    last = document.new_page()
    last.insert_text((72, 72), "third page evidence")
    last.insert_text((72, 96), "third page second line")
    raw = document.tobytes()
    document.close()

    from wenjin.ingestion.multiformat import parse_pdf_pages_bytes

    units = parse_pdf_pages_bytes(raw, "spec.pdf")
    assert [unit.page_number for unit in units] == [1, 2, 3]
    rows = [
        {"text": units[0].text, "page_number": 1, "start_line": 1, "end_line": 1},
        {
            "text": units[2].text if forged_page is None else units[0].text,
            "page_number": 3 if forged_page is None else forged_page,
            "start_line": 1 if bad_range is None else bad_range[0],
            "end_line": (
                len(units[2].text.splitlines(keepends=True))
                if bad_range is None else bad_range[1]
            ),
        },
    ]
    return _make_assets(
        tmp_path,
        raw=raw,
        suffix=".pdf",
        parser="pymupdf-page-v1",
        rows=rows,
        binary=True,
    )


def test_pdf_page_replay_checks_page_content_and_reuses_source_parse(tmp_path, monkeypatch):
    import pymupdf

    assets = _pdf_fixture(tmp_path)
    original_open = pymupdf.open
    opened = []

    class TrackedDocument:
        def __init__(self, *args, **kwargs):
            self.inner = original_open(*args, **kwargs)
            self.closed = False
            opened.append(self)

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            try:
                return self.inner.__exit__(*args)
            finally:
                self.closed = True

        def __iter__(self):
            return iter(self.inner)

        def __len__(self):
            return len(self.inner)

    monkeypatch.setattr(pymupdf, "open", TrackedDocument)
    report = _audit(assets)
    assert report["status"] == "PASS", json.dumps(report, ensure_ascii=False, indent=2)
    assert report["source_text_audit"]["verified_chunks"] == 2
    assert report["source_text_audit"]["sources_considered"] == 1
    assert report["source_text_audit"]["sources_parser_replayed"] == 1
    assert opened and all(document.closed for document in opened)


def test_pdf_valid_but_wrong_page_body_is_rejected(tmp_path):
    assets = _pdf_fixture(tmp_path, forged_page=3)
    report = _audit(assets)
    assert report["status"] == "FAIL"
    assert report["problems"].get("source_text_mismatch")
    assert report["source_text_audit"]["verified_chunks"] == 1


@pytest.mark.parametrize("mode", ["drop-last", "empty"])
def test_pdf_parser_cannot_self_certify_missing_physical_pages(tmp_path, monkeypatch, mode):
    import wenjin.ingestion.multiformat as multiformat

    assets = _pdf_fixture(tmp_path)
    original = multiformat.parse_pdf_pages_bytes

    def incomplete_parse(content: bytes, source_path: str):
        units = original(content, source_path)
        return units[:-1] if mode == "drop-last" else []

    monkeypatch.setattr(multiformat, "parse_pdf_pages_bytes", incomplete_parse)
    report = _audit(assets)
    assert report["status"] == "FAIL", report["problems"]
    assert report["problems"].get("source_content_invalid")
    assert report["source_text_audit"]["verified_chunks"] == 0


@pytest.mark.parametrize("line_range", [(0, 1), (2, 1), (4, 5), (1, 99)])
def test_pdf_invalid_page_line_range_fails_admission(tmp_path, line_range):
    assets = _pdf_fixture(tmp_path, bad_range=line_range)
    report = _audit(assets)
    assert report["status"] == "FAIL", report["problems"]
    assert report["problems"].get("position_mismatch")
    assert report["source_text_audit"]["verified_chunks"] == 1


def test_synchronized_pdf_body_and_hash_forgery_is_rejected(tmp_path):
    assets = _pdf_fixture(tmp_path)
    forged_text = "fabricated evidence\n"
    payloads = [dict(row) for row in assets["payloads"]]
    payloads[1]["text"] = forged_text
    payloads[1]["content_sha256"] = hashlib.sha256(forged_text.encode()).hexdigest()
    report = _replace_both_sides(assets, payloads)
    assert report["status"] == "FAIL"
    assert report["problems"].get("source_text_mismatch")
    assert report["source_text_audit"]["verified_chunks"] == 1


@pytest.mark.parametrize("defect", ["parser", "one-side-line-position"])
def test_problem_example_truncation_never_counts_bad_chunks_as_verified(tmp_path, defect):
    lines = [f"trusted line {index}\n" for index in range(1, 22)]
    raw = "".join(lines).encode("utf-8")
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".txt",
        parser="text-line-v1",
        rows=[
            {"text": text, "start_line": index, "end_line": index}
            for index, text in enumerate(lines, start=1)
        ],
    )
    altered = [dict(row) for row in assets["payloads"]]
    if defect == "parser":
        for row in altered:
            row["parser"] = "forged-parser-v999"
    else:
        for row in altered:
            row["start_line"] += 1
            row["end_line"] += 1

    report = _audit(assets, altered)
    expected_problem = "parser_mismatch" if defect == "parser" else "position_mismatch"
    assert report["status"] == "FAIL"
    assert report["problems"].get(expected_problem)
    assert len(report["problems"][expected_problem]) <= 20
    assert report["source_text_audit"]["total_chunks"] == 21
    assert report["source_text_audit"]["verified_chunks"] == 0


@pytest.mark.parametrize("field", ["start_line", "end_line"])
def test_boolean_is_not_accepted_as_text_line_number(tmp_path, field):
    assets = _make_assets(
        tmp_path,
        raw=b"trusted one line\n",
        suffix=".txt",
        parser="text-line-v1",
        rows=[{"text": "trusted one line\n", "start_line": 1, "end_line": 1}],
    )
    payloads = [dict(row) for row in assets["payloads"]]
    payloads[0][field] = True
    report = _audit(assets, payloads)
    assert report["status"] == "FAIL"
    assert report["problems"].get("position_mismatch")
    assert report["source_text_audit"]["verified_chunks"] == 0


def test_copytree_isolated_pdf_mutation_does_not_touch_seed_assets(tmp_path):
    seed_root = tmp_path / "seed"
    seed = _pdf_fixture(seed_root)
    seed_source_hash = hashlib.sha256(seed["source_file"].read_bytes()).hexdigest()
    seed_bm25_bytes = seed["bm25_path"].read_bytes()

    copy_root = tmp_path / "mutated-copy"
    shutil.copytree(seed_root, copy_root)
    assert all(not path.is_symlink() for path in copy_root.rglob("*")), (
        "mutation fixture must be a real copytree, not symlink/junction aliases"
    )
    copied = {
        **seed,
        "root": copy_root,
        "source_file": copy_root / seed["source_file"].relative_to(seed_root),
        "manifest_path": copy_root / seed["manifest_path"].relative_to(seed_root),
        "bm25_path": copy_root / seed["bm25_path"].relative_to(seed_root),
    }
    forged = "copied fixture forgery\n"
    payloads = [dict(row) for row in seed["payloads"]]
    payloads[1]["text"] = forged
    payloads[1]["content_sha256"] = hashlib.sha256(forged.encode()).hexdigest()
    report = _replace_both_sides(copied, payloads)

    assert report["status"] == "FAIL"
    assert report["problems"].get("source_text_mismatch")
    assert hashlib.sha256(seed["source_file"].read_bytes()).hexdigest() == seed_source_hash
    assert seed["bm25_path"].read_bytes() == seed_bm25_bytes


@pytest.mark.parametrize("scan_pages", [[True], [0], [-1], [2], [1, 1], ["1"], None])
def test_hybrid_scan_page_plan_invalidity_fails_closed(tmp_path, scan_pages):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "text-layer page")
    raw = document.tobytes()
    document.close()
    provenance = None if scan_pages is None else {
        "parse_report": {
            "pages": 1, "text_pages": 1, "no_text_pages": [],
            "scan_pages": scan_pages,
        }
    }
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".pdf",
        parser="mixed-text-ocr-page-v1",
        rows=[{
            "text": "text-layer page\n", "page_number": 1,
            "start_line": 1, "end_line": 1,
            "unit_parser": "pymupdf-page-v1", "ocr_derived": False,
        }],
        binary=True,
        provenance=provenance,
    )
    report = _audit(assets)
    assert report["status"] == "FAIL", report["problems"]
    assert (
        report["problems"].get("parser_mismatch")
        or report["problems"].get("source_content_invalid")
    )
    assert report["source_text_audit"]["verified_chunks"] == 0
    assert report["source_text_audit"]["unverifiable_chunks"] == 1


def test_hybrid_scan_pages_only_is_compatible_but_classification_is_unverified(tmp_path):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "synthetic scan page")
    raw = document.tobytes()
    document.close()
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".pdf",
        parser="mixed-text-ocr-page-v1",
        rows=[{
            "text": "recognized scan text\n", "page_number": 1,
            "start_line": 1, "end_line": 1,
            "unit_parser": "mineru-ocr-page-v1", "ocr_derived": True,
        }],
        binary=True,
        provenance={"parse_report": {"scan_pages": [1]}},
    )
    report = _audit(assets)
    assert report["status"] == "PASS", report["problems"]
    assert report["source_text_audit"]["hybrid_classification_unverified_sources"] == 1
    assert report["source_text_audit"]["verified_chunks"] == 0
    assert report["source_text_audit"]["unverifiable_chunks"] == 1


@pytest.mark.parametrize(
    "classification",
    [
        {"pages": True},
        {"pages": 0},
        {"text_pages": True},
        {"no_text_pages": [True]},
        {"no_text_pages": [2]},
        {"text_pages": 0, "no_text_pages": []},
    ],
)
def test_hybrid_optional_classification_fields_validate_when_present(
    tmp_path, classification,
):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "ordinary text page")
    raw = document.tobytes()
    document.close()
    report_data = {"scan_pages": [], **classification}
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".pdf",
        parser="mixed-text-ocr-page-v1",
        rows=[{
            "text": "ordinary text page\n", "page_number": 1,
            "start_line": 1, "end_line": 1,
            "unit_parser": "pymupdf-page-v1", "ocr_derived": False,
        }],
        binary=True,
        provenance={"parse_report": report_data},
    )
    report = _audit(assets)
    assert report["status"] == "FAIL", report["problems"]
    assert report["problems"].get("source_content_invalid")


def test_ocr_page_is_provenance_checked_but_not_counted_as_exact_source_text(tmp_path):
    import pymupdf

    document = pymupdf.open()
    document.new_page()
    raw = document.tobytes()
    document.close()
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".pdf",
        parser="mineru-ocr-page-v1",
        rows=[{
            "text": "recognized words\n", "page_number": 1,
            "start_line": 1, "end_line": 1,
            "unit_parser": "mineru-ocr-page-v1", "ocr_derived": True,
        }],
        binary=True,
    )
    report = _audit(assets)
    audit = report["source_text_audit"]
    assert audit["verified_chunks"] == 0
    assert audit["unverifiable_chunks"] == 1
    assert audit["page_level_unchecked"] == 1
    assert audit["limits_by_reason"]["ocr_derived_text"] == 1


def test_hash_valid_non_pdf_with_ocr_parser_is_rejected(tmp_path):
    # 二进制raw/canonical hash自洽，不代表扩展名为PDF的文件真的可解析。
    raw = b"not a PDF, but faithfully hashed\x00\xff"
    assets = _make_assets(
        tmp_path,
        raw=raw,
        suffix=".pdf",
        parser="mineru-ocr-page-v1",
        rows=[{
            "text": "untrusted OCR result\n", "page_number": 1,
            "start_line": 1, "end_line": 1,
            "unit_parser": "mineru-ocr-page-v1", "ocr_derived": True,
        }],
        binary=True,
    )
    report = _audit(assets)
    assert report["status"] == "FAIL", report["problems"]
    assert report["problems"].get("source_content_invalid")
    assert report["source_text_audit"]["verified_chunks"] == 0


def test_stream_hash_matches_canonical_hash_when_crlf_crosses_block_boundary(tmp_path):
    # _stream_content_hashes 使用1MiB读取块；CR与LF恰好分居两块，须与常规Hash一致。
    raw = b"a" * (1024 * 1024 - 1) + b"\r\nend\r\n"
    path = tmp_path / "boundary.txt"
    path.write_bytes(raw)
    expected = hash_content(raw, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8")
    raw_hash, canonical_hash = audit_module._stream_content_hashes(
        path, TEXT_NEWLINE_LF_V1, "utf-8"
    )
    assert raw_hash == expected.raw_sha256
    assert canonical_hash == expected.canonical_sha256
