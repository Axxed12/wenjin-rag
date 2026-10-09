import sqlite3
from pathlib import Path

import pytest

from wenjin.indexing import bm25 as bm25_module
from wenjin.indexing.bm25 import bm25_query, build_bm25_index, fts_query, query_terms
from wenjin.ingestion.chunking import Chunk


def test_query_terms_extract_identifiers_and_chinese_words():
    """本条原名 `..._and_ignore_chinese`，断言的是"中文被忽略"。

    改成新语义是**故意的**，不是放宽断言：unicode61 把整段连续中文当成一个词元，
    旧行为下 `query_terms("如何添加对象")` 返回 `[]`，`bm25_query`/`fts_query` 随即
    抛 ValueError，**一个英文标识符都不带的**纯中文查询完全不走 BM25。
    ASCII 仍在前、中文追加在后。

    注意适用范围：带英文标识符的中文查询（"GeoTransform 怎么用"）旧行为下本来就能
    提取到词，BM25 一直在出力，那些查询不受本次改动影响。
    """
    assert query_terms("Geode 如何添加 osg::Drawable？") == [
        "Geode", "osg", "Drawable", "如何", "添加",
    ]
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


def test_bm25_exact_source_path_filter_does_not_treat_like_metacharacters_as_wildcards(tmp_path: Path):
    chunks = [
        Chunk("literal", "docs/a%_guide.txt", "1.0", 1, 1, "amberwidget marker", "blocks"),
        Chunk("neighbor", "docs/aXXguide.txt", "1.0", 1, 1, "amberwidget marker", "blocks"),
    ]
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, chunks)
    result = bm25_query(path, "amberwidget", source_path="docs/a%_guide.txt")
    assert [item["chunk_id"] for item in result] == ["literal"]


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


def test_chinese_only_query_now_reaches_bm25(tmp_path: Path):
    """中文 query 不再是"显式不支持"：它现在能提取出词，因此会真的去查索引。

    索引不存在时抛的是 sqlite 的错，而不是"提取不到词"——这正是修复生效的标志。
    """
    assert query_terms("如何添加对象") == ["如何", "添加", "对象"]
    with pytest.raises(sqlite3.OperationalError):
        bm25_query(tmp_path / "missing.sqlite", "如何添加对象")
    # 真正的空提取仍然显式报错：既无 ASCII 标识符也无中文。
    with pytest.raises(ValueError, match="没有从 query 中提取到"):
        bm25_query(tmp_path / "missing.sqlite", "？？！……")


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


ENGLISH_CHUNKS = [
    Chunk("e1", "include/osg/Geode", "3.6.5", 1, 2, "addDrawable adds a Drawable", "blocks"),
    Chunk("e2", "include/osg/Group", "3.6.5", 3, 4, "addChild adds a Node", "blocks"),
    Chunk("e3", "examples/demo.cpp", "3.6.5", 5, 6, "osg::ref_ptr<Node> node", "blocks"),
]


def test_pure_english_index_matches_unsegmented_index_exactly(tmp_path: Path, monkeypatch):
    """纯英文语料的不变式：切分前后 BM25 得分逐位相同（含浮点，直接比 ==）。

    旧行为用把 `segment_cjk` 换成恒等函数来复现——这样两次建库走的是同一份表结构
    和同一段代码，差异只剩"切不切分"，比手抄一份旧 schema 更能说明问题。
    """
    new_path = tmp_path / "new.sqlite"
    build_bm25_index(new_path, ENGLISH_CHUNKS)
    old_path = tmp_path / "old.sqlite"
    monkeypatch.setattr(bm25_module, "segment_cjk", lambda text: text)
    build_bm25_index(old_path, ENGLISH_CHUNKS)
    monkeypatch.undo()

    for query in ["addDrawable", "Drawable Node", "addChild osg"]:
        assert bm25_query(new_path, query) == bm25_query(old_path, query)


# source_path 用纯 ASCII：unicode61 会按 `/` `.` 切开路径，若路径里带中文
# （如 `docs/接口.md`），"接口" 在**未切分**的旧索引里也会是独立词元，
# 那样测出来的"旧索引召回 0"是假的。真实 OSG 语料路径全是 ASCII。
CHINESE_CHUNKS = [
    Chunk("c1", "docs/api.md", "1.0", 1, 2, "本接口用于查询用户信息", "blocks"),
    Chunk("c2", "docs/other.md", "1.0", 3, 4, "该函数返回节点数量", "blocks"),
]


def test_chinese_query_hits_segmented_index_and_misses_unsegmented_one(tmp_path: Path, monkeypatch):
    """中文召回：切分后能命中；不切分的旧索引对同样查询返回 0，证明修复真的起作用。"""
    new_path = tmp_path / "new.sqlite"
    build_bm25_index(new_path, CHINESE_CHUNKS)
    old_path = tmp_path / "old.sqlite"
    monkeypatch.setattr(bm25_module, "segment_cjk", lambda text: text)
    build_bm25_index(old_path, CHINESE_CHUNKS)
    monkeypatch.undo()

    for query in ["接口", "用户信息", "查询用户信息"]:
        assert [item["chunk_id"] for item in bm25_query(new_path, query)] == ["c1"]
        assert bm25_query(old_path, query) == []


def test_chinese_bm25_evidence_text_is_the_unsegmented_original(tmp_path: Path):
    """证据原文契约：返回的 `text` 取自 raw_text，与是否切分无关。"""
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, CHINESE_CHUNKS)
    result = bm25_query(path, "接口")[0]
    assert result["text"] == "本接口用于查询用户信息"
    assert result["text"] == CHINESE_CHUNKS[0].text


def test_mixed_query_recalls_chinese_and_english_chunks(tmp_path: Path):
    chunks = [
        Chunk("c", "docs/api.md", "1.0", 1, 2, "本接口用于查询用户信息", "blocks"),
        Chunk("e", "include/osg/Group", "3.6.5", 3, 4, "addChild adds a Node", "blocks"),
    ]
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, chunks)
    assert {item["chunk_id"] for item in bm25_query(path, "接口 addChild")} == {"c", "e"}


def test_legacy_unsegmented_index_still_opens(tmp_path: Path):
    """旧索引（写的是未切分的中文）必须仍能打开查询，不能报错。

    命中 0 条是预期的：整段中文是一个词元，切分后的查询词对不上它。
    真实旧索引要恢复中文召回必须重建，这一点写在 README 的契约里。
    """
    legacy = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy) as db:
        db.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, source_path UNINDEXED,"
            " version UNINDEXED, start_line UNINDEXED, end_line UNINDEXED, strategy UNINDEXED,"
            " content_sha256 UNINDEXED, source_raw_sha256 UNINDEXED,"
            " source_canonical_sha256 UNINDEXED, normalization UNINDEXED,"
            " retrieval_text, raw_text UNINDEXED)"
        )
        db.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("c1", "docs/api.md", "1.0", 1, 2, "blocks", "", None, None, None,
             "docs/api.md 本接口用于查询用户信息", "本接口用于查询用户信息"),
        )
    assert bm25_query(legacy, "接口") == []
    # 英文查询在两个索引上都还能用，旧索引不是"只能读不能查"。
    assert bm25_query(legacy, "docs")[0]["chunk_id"] == "c1"
