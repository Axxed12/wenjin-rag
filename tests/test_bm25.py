from pathlib import Path

import pytest

from wenjin.indexing.bm25 import bm25_query, build_bm25_index, fts_query, query_terms
from wenjin.ingestion.chunking import Chunk


def test_query_terms_extract_identifiers_and_ignore_chinese():
    assert query_terms("Geode 如何添加 osg::Drawable？") == ["Geode", "osg", "Drawable"]
    assert " OR " in fts_query("Geode Drawable")


def test_bm25_returns_exact_identifier_candidate(tmp_path: Path):
    chunks = [
        Chunk("one", "Geode", "3.6.5", 1, 2, "addDrawable adds a Drawable", "blocks"),
        Chunk("two", "Group", "3.6.5", 3, 4, "addChild adds a Node", "blocks"),
    ]
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, chunks)
    result = bm25_query(path, "addDrawable", top_k=2)
    assert result[0]["chunk_id"] == "one"
    assert result[0]["score"] > 0


def test_bm25_round_trips_page_and_logical_document(tmp_path: Path):
    chunks = [Chunk(
        "page", "standard.pdf", "1.0", 1, 3, "GetMap VERSION 1.3.0", "blocks",
        page_number=33, logical_document_id="ogc:06-042",
    )]
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, chunks)
    result = bm25_query(path, "GetMap")[0]
    assert result["page_number"] == 33
    assert result["logical_document_id"] == "ogc:06-042"


def test_bm25_reads_existing_schema_without_page_columns(tmp_path: Path):
    chunks = [Chunk("old", "old.txt", "1.0", 1, 1, "legacy token", "blocks")]
    path = tmp_path / "old.sqlite"
    build_bm25_index(path, chunks)
    import sqlite3
    # 模拟当前active索引：用旧列定义重建最小数据库。
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, source_path UNINDEXED, version UNINDEXED, start_line UNINDEXED, end_line UNINDEXED, strategy UNINDEXED, content_sha256 UNINDEXED, source_raw_sha256 UNINDEXED, source_canonical_sha256 UNINDEXED, normalization UNINDEXED, retrieval_text, raw_text UNINDEXED)")
        db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("old", "old.txt", "1.0", 1, 1, "blocks", "", None, None, None,
                    "old.txt legacy token", "legacy token"))
    result = bm25_query(legacy, "legacy")[0]
    assert result["page_number"] is None and result["logical_document_id"] is None


def test_chinese_only_query_is_explicitly_unsupported(tmp_path: Path):
    with pytest.raises(ValueError, match="没有从 query 中提取到"):
        bm25_query(tmp_path / "missing.sqlite", "如何添加对象")


def test_bm25_can_limit_identifier_branch_to_headers(tmp_path: Path):
    chunks = [
        Chunk("header", "include/osg/Group", "3.6.5", 1, 2, "addChild declaration", "blocks"),
        Chunk("example", "examples/demo.cpp", "3.6.5", 1, 2, "addChild example", "blocks"),
    ]
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, chunks)
    result = bm25_query(
        path,
        "addChild",
        terms_override=["addChild"],
        source_prefix="include/%",
    )
    assert [item["chunk_id"] for item in result] == ["header"]
