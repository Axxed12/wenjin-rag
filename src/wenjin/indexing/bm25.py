"""SQLite FTS5 关键词索引与 BM25 查询。

当前是可解释的英文/代码标识符基线。中文 query 中的英文类名、方法名会参与检索；
中文语义本身主要交给 dense 路径，后续再评估是否需要中文分词或查询改写。
"""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

ASCII_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def query_terms(query: str) -> list[str]:
    """提取适合当前 OSG 英文源码的检索词，并保持出现顺序去重。"""
    terms = []
    seen = set()
    for term in ASCII_TERM.findall(query):
        lowered = term.lower()
        if lowered not in seen:
            seen.add(lowered)
            terms.append(term)
    return terms


def fts_query(query: str) -> str:
    """构造 OR 查询以保留召回；每个词用双引号避免被当成 FTS 语法。"""
    terms = query_terms(query)
    if not terms:
        raise ValueError("当前 BM25 基线没有从 query 中提取到英文或代码标识符")
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def build_bm25_index(path: Path, chunks: list) -> None:
    """通过临时数据库完整重建，再替换正式文件，避免半成品索引。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    # sqlite3.Connection 的上下文管理器只提交/回滚事务，并不关闭连接；
    # Windows 上未关闭的文件句柄会阻止 temporary.replace(path)。
    with closing(sqlite3.connect(temporary)) as database:
        database.execute(
            """
            CREATE VIRTUAL TABLE chunks USING fts5(
                chunk_id UNINDEXED,
                source_path UNINDEXED,
                version UNINDEXED,
                start_line UNINDEXED,
                end_line UNINDEXED,
                strategy UNINDEXED,
                content_sha256 UNINDEXED,
                source_raw_sha256 UNINDEXED,
                source_canonical_sha256 UNINDEXED,
                normalization UNINDEXED,
                page_number UNINDEXED,
                logical_document_id UNINDEXED,
                retrieval_text,
                raw_text UNINDEXED,
                tokenize='unicode61'
            )
            """
        )
        rows = [
            (
                chunk.chunk_id,
                chunk.source_path,
                chunk.version,
                chunk.start_line,
                chunk.end_line,
                chunk.strategy,
                chunk.content_sha256,
                chunk.source_raw_sha256,
                chunk.source_canonical_sha256,
                chunk.normalization,
                chunk.page_number,
                chunk.logical_document_id,
                # 路径也进入检索表示，让类/文件名可以参与关键词召回。
                f"{chunk.source_path}\n{chunk.text}",
                chunk.text,
            )
            for chunk in chunks
        ]
        database.executemany(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        database.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
        database.commit()
    temporary.replace(path)


def bm25_query(
    path: Path,
    query: str,
    *,
    top_k: int = 5,
    terms_override: list[str] | None = None,
    source_prefix: str | None = None,
) -> list[dict]:
    """返回关键词候选；SQLite 原生 bm25 越小越好，展示时转为 higher-is-better。"""
    terms = query_terms(query) if terms_override is None else terms_override
    if not terms:
        raise ValueError("当前 BM25 基线没有从 query 中提取到英文或代码标识符")
    expression = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        columns = {row[1] for row in database.execute("PRAGMA table_info(chunks)")}
        page_select = "page_number" if "page_number" in columns else "NULL AS page_number"
        logical_select = (
            "logical_document_id"
            if "logical_document_id" in columns
            else "NULL AS logical_document_id"
        )
        source_clause = " AND source_path LIKE ?" if source_prefix else ""
        parameters = (expression, source_prefix, top_k) if source_prefix else (expression, top_k)
        rows = database.execute(
            f"""
            SELECT chunk_id, source_path, version, start_line, end_line, strategy,
                   {page_select}, {logical_select},
                   raw_text, bm25(chunks) AS raw_bm25
            FROM chunks
            WHERE chunks MATCH ?
            {source_clause}
            ORDER BY raw_bm25 ASC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [
        {
            "score": -row["raw_bm25"],
            "raw_bm25": row["raw_bm25"],
            "chunk_id": row["chunk_id"],
            "source_path": row["source_path"],
            "version": row["version"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "strategy": row["strategy"],
            "page_number": row["page_number"],
            "logical_document_id": row["logical_document_id"],
            "text": row["raw_text"],
            "matched_terms": terms,
        }
        for row in rows
    ]
