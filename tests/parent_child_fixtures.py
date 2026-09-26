"""父子验证共用的合成索引夹具。

用**真实的**切片函数与 BM25 写入函数造资产，不手搓行：手搓的行只能证明"审计读得懂
我写的字段"，证明不了"索引器写出来的东西能过审计"。Qdrant 那一侧用假客户端，
payload 与 `create_dense_collection` 写入的同形。

夹具一律建在 `tmp_path` 下，不碰仓库里的 `raw_data/`、`corpora/`、`data/`。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from wenjin import manifest as manifest_module
from wenjin.index_registry import IndexTarget
from wenjin.indexing.bm25 import build_bm25_index
from wenjin.ingestion.chunking import PARENTED_STRATEGY, block_chunks_parented

CORPUS_DIR = "osg-3.6.5"
FILE_PATH = "include/osg/Parented"
PARSER = "html-line-v1"
NORMALIZATION = "text-newline-lf-v1"

# 一份超长块 + 一段尾部：超长块会被字符级硬切，于是出现"一个父块带多个 child"，
# 这正是父块区间、父子对齐与名额去重这些检查真正起作用的形态。
SOURCE_TEXT = (
    "".join(f"line-{index:03d}-{'x' * 20}\n" for index in range(1, 41))
    + "\n"
    + "".join(f"tail-{index:03d}-{'y' * 20}\n" for index in range(50, 58))
)


class FakeQdrant:
    """审计与检索只用到 get_collection / count / scroll / close。"""

    def __init__(self, payloads: list[dict], *, points: int | None = None) -> None:
        self.payloads = [dict(item) for item in payloads]
        self.points = points if points is not None else len(self.payloads)

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

    def close(self):
        return None


def chunk_payload(chunk) -> dict:
    """与 `create_dense_collection` 写入的 payload 同形。"""
    return {
        "chunk_id": chunk.chunk_id, "source_path": chunk.source_path,
        "version": chunk.version, "start_line": chunk.start_line,
        "end_line": chunk.end_line, "strategy": chunk.strategy, "text": chunk.text,
        "content_sha256": chunk.content_sha256,
        "source_raw_sha256": chunk.source_raw_sha256,
        "source_canonical_sha256": chunk.source_canonical_sha256,
        "normalization": chunk.normalization, "page_number": chunk.page_number,
        "logical_document_id": chunk.logical_document_id,
        "parser": chunk.parser, "ocr_derived": chunk.ocr_derived,
        "parent_id": chunk.parent_id,
    }


ARCHIVE_PATH = "include/osg/ArchiveOnly"
# 只归档、不检索的那份文件的内容：父块若被改到引用它，就是在给调用方送
# 「没有检索授权」的正文。
ARCHIVE_TEXT = "".join(f"archived-{index:03d}\n" for index in range(1, 21))


def build_parented_assets(
    tmp_path: Path,
    *,
    strategy: str = PARENTED_STRATEGY,
    max_chars: int = 200,
    with_archive_file: bool = False,
) -> dict:
    """造一份自洽的父子索引：语料 + manifest + BM25（含 parents 表）+ 假 Qdrant。

    `with_archive_file=True` 时额外登记一份 `indexable: False` 的文件：它**只归档、
    不检索**，用来测"父块能不能引用到检索白名单之外的内容"。
    """
    raw_file = tmp_path / "raw_data" / "osg" / "3.6.5" / FILE_PATH
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text(SOURCE_TEXT, encoding="utf-8", newline="")
    digest = hashlib.sha256(raw_file.read_bytes()).hexdigest()

    files = [{
        "path": FILE_PATH, "raw_sha256": digest, "canonical_sha256": digest,
        "normalization": NORMALIZATION, "encoding": "utf-8",
        "parser": PARSER, "indexable": True,
        "source_url": "https://example.invalid/Parented",
    }]
    if with_archive_file:
        archive_digest = hashlib.sha256(ARCHIVE_TEXT.encode()).hexdigest()
        archive_file = tmp_path / "raw_data" / "osg" / "3.6.5" / ARCHIVE_PATH
        archive_file.parent.mkdir(parents=True, exist_ok=True)
        archive_file.write_text(ARCHIVE_TEXT, encoding="utf-8", newline="")
        files.append({
            "path": ARCHIVE_PATH, "raw_sha256": archive_digest,
            "canonical_sha256": archive_digest,
            "normalization": NORMALIZATION, "encoding": "utf-8",
            "parser": PARSER, "indexable": False,
            "source_url": "https://example.invalid/ArchiveOnly",
        })
    manifest = {
        "schema_version": 3,
        "corpus_id": CORPUS_DIR,
        "library": "osg",
        "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5",
        "license": "OSGPL",
        "files": files,
    }
    manifest["canonical_corpus_sha256"] = manifest_module.canonical_fingerprint(manifest)
    corpus_dir = tmp_path / "corpora" / CORPUS_DIR
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = corpus_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    document = {
        "source_path": FILE_PATH, "version": "3.6.5", "text": SOURCE_TEXT,
        "page_number": None, "parser": PARSER,
        "raw_sha256": digest, "canonical_sha256": digest,
        "normalization": NORMALIZATION, "logical_document_id": None,
    }
    children, parents = block_chunks_parented(document, max_chars=max_chars)
    assert parents, "夹具没有产出父块，后面的检查没有意义"

    bm25_path = tmp_path / "candidate.sqlite"
    build_bm25_index(
        bm25_path, children, parents=parents if strategy == PARENTED_STRATEGY else None
    )
    target = IndexTarget(
        target_id="osg-3.6.5-parented-synth", corpus_id=CORPUS_DIR, library="osg",
        version="3.6.5", strategy=strategy, dense_collection="synth_parented",
        bm25_path="candidate.sqlite",
        manifest_path=f"corpora/{CORPUS_DIR}/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=len(children), vector_dimension=3, embedding_model="test",
        canonical_corpus_sha256=manifest["canonical_corpus_sha256"],
    )
    return {
        "root": tmp_path, "target": target, "bm25_path": bm25_path,
        "payloads": [chunk_payload(child) for child in children],
        "children": children, "parents": parents, "document": document,
    }


def edit_sqlite(path: Path, statements: list[tuple[str, tuple]]) -> None:
    with closing(sqlite3.connect(path)) as database:
        for sql, parameters in statements:
            database.execute(sql, parameters)
        database.commit()


def parent_with_children(assets: dict, *, at_least: int = 2):
    """挑一个至少带 `at_least` 个 child 的父块，连同它的 child 一起返回。"""
    for parent in assets["parents"]:
        members = [c for c in assets["children"] if c.parent_id == parent.chunk_id]
        if len(members) >= at_least:
            return parent, members
    return None, []
