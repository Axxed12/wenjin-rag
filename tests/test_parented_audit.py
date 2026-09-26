"""父子化索引的准入审计：父块也要能通过，而且要能拦住坏掉的父子结构。

父块没有"另一侧"可比（它不在 FTS 表、也没有向量），所以它的真值是 manifest 登记记录
加**原文行切片**。这里逐类篡改一处，确认审计把它拦下来；放松任何一条，坏掉的父块都会
以"看起来正常"的正文进入证据。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from wenjin.content_hashing import hash_content, manifest_content_fingerprint
from wenjin.index_audit import IndexAdmissionError, admit_index, audit_index
from wenjin.indexing.build import build_candidate_index
from wenjin.ingestion.chunking import block_chunks_parented

FILE_PATH = "include/osg/Test"
NORMALIZATION = "text-newline-lf-v1"
COLLECTION = "parented_audit_dense"


class StubEmbedder:
    def get_text_embedding_batch(self, texts, show_progress=False):
        return [[0.0] * 8 for _ in texts]


def source_text() -> str:
    """两块内容夹一个"孤儿空行"：连续两个空行里有一行不属于任何child。

    这样夹具里既有"多个child合成一个父块"，也有"child之间断开"这个边界。
    """
    block = "".join(f"alpha-{index:03d}-{'x' * 70}\n" for index in range(24))
    other = "".join(f"beta-{index:03d}-{'y' * 70}\n" for index in range(24))
    return block + "\n" + "\n" + other + "tail\n" + "\n"


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    text = source_text()
    raw = root / "raw_data/osg/3.6.5/include/osg"
    raw.mkdir(parents=True)
    (raw / "Test").write_text(text, encoding="utf-8")
    # Hash 必须按**文件真实字节**算：Windows 上 write_text 会把换行写成 CRLF，
    # 拿字符串算出的 LF 字节会对不上加载器的校验。
    hashed = hash_content(
        (raw / "Test").read_bytes(), normalization=NORMALIZATION, encoding="utf-8"
    )
    manifest_dir = root / "corpora/osg-3.6.5"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "source-manifest.json"
    manifest = {
        "schema_version": 3, "corpus_id": "osg-3.6.5", "library": "osg", "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5", "license": "OSGPL",
        "files": [{
            "path": FILE_PATH, "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256, "normalization": NORMALIZATION,
            "encoding": "utf-8", "indexable": True,
            "source_url": "https://example.org/osg/Test",
            "logical_document_id": "osg-3.6.5:include/osg/Test",
        }],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def build_parented(root: Path) -> tuple:
    client = QdrantClient(":memory:")
    target = build_candidate_index(
        manifest_path=root / "corpora/osg-3.6.5/source-manifest.json",
        target_id="osg-3.6.5-blocks-split-parented-003",
        strategy="blocks-split-parented",
        dense_collection=COLLECTION,
        bm25_path=root / "data/indexes/osg_3_6_5_bm25_parented.sqlite",
        qdrant_client=client, embedder=StubEmbedder(), embedding_model="fake",
        project_root=root,
    )
    return target, client


@pytest.fixture()
def assets(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    # 建库入口按这个环境变量找原文根（与 ingest.load_documents 一致）。
    monkeypatch.setenv("WENJIN_RAW_DATA_ROOT", str(root / "raw_data"))
    target, client = build_parented(root)
    return {
        "root": root,
        "target": target,
        "client": client,
        "bm25": root / "data/indexes/osg_3_6_5_bm25_parented.sqlite",
        "text": source_text(),
    }


def audit(assets: dict) -> dict:
    return audit_index(assets["target"], root=assets["root"], qdrant_client=assets["client"])


def sql(assets: dict, statement: str, parameters: tuple = ()) -> None:
    with closing(sqlite3.connect(assets["bm25"])) as database:
        database.execute(statement, parameters)
        database.commit()


def rows(assets: dict, statement: str, parameters: tuple = ()) -> list[tuple]:
    with closing(sqlite3.connect(assets["bm25"])) as database:
        return database.execute(statement, parameters).fetchall()


def fixture_chunks(assets: dict) -> tuple:
    """夹具自己跑一遍切片，拿到期望的父子结构（审计用的索引由同一个函数产出）。"""
    from ingest import load_documents

    document = load_documents(assets["root"] / "corpora/osg-3.6.5/source-manifest.json")[0]
    return block_chunks_parented(document, max_chars=1600)


# --- 基线 ---


def test_fixture_really_has_two_parents_and_a_gap(assets):
    """夹具不成立的话，下面的检查会全变成空跑。"""
    children, parents = fixture_chunks(assets)
    assert len(parents) == 2, [parent.chunk_id for parent in parents]
    assert len(children) == 4, [child.chunk_id for child in children]
    assert [len([c for c in children if c.parent_id == p.chunk_id]) for p in parents] == [2, 2]
    assert any(
        current.start_line > previous.end_line + 1
        for previous, current in zip(children, children[1:])
    ), "夹具里没有child之间的洞"


def test_parented_index_passes_admission(assets):
    report = admit_index(assets["target"], root=assets["root"], qdrant_client=assets["client"])
    assert report["status"] == "PASS", report["problems"]
    children, parents = fixture_chunks(assets)
    summary = report["parents"]
    assert summary["table_present"] is True
    assert summary["rows"] == len(parents)
    assert summary["children_with_parent"] == len(children)
    assert summary["children_unparented"] == 0
    assert summary["dangling_parent_ids"] == 0
    # 文本来源的父块必须逐字比对过原文切片，而不是只查了行号范围。
    assert summary["line_level_checkable"] == len(parents)
    assert summary["text_slice_verified"] == len(parents)
    assert summary["page_level_unchecked"] == 0
    assert summary["verifiable"] is True


def test_chunk_count_still_counts_only_retrievable_children(assets):
    """父块不进向量集合、也不进 FTS 表：chunk_count 仍是可检索片段数。"""
    children, parents = fixture_chunks(assets)
    assert assets["target"].chunk_count == len(children)
    assert assets["client"].count(COLLECTION, exact=True).count == len(children)
    assert len(parents) > 0
    assert rows(assets, "SELECT COUNT(*) FROM parents")[0][0] == len(parents)


def test_non_parented_index_is_unaffected(assets):
    """同一套审计对普通索引的结论不变：没有父块表就如实写 0，不因为缺表而失败。"""
    from ingest import load_documents
    from wenjin.ingestion.chunking import block_chunks

    root = assets["root"]
    plain = build_candidate_index(
        manifest_path=root / "corpora/osg-3.6.5/source-manifest.json",
        target_id="osg-3.6.5-blocks-split-003", strategy="blocks-split",
        dense_collection="plain_audit_dense",
        bm25_path=root / "data/indexes/osg_3_6_5_bm25_plain.sqlite",
        qdrant_client=assets["client"], embedder=StubEmbedder(), embedding_model="fake",
        project_root=root,
    )
    report = admit_index(plain, root=root, qdrant_client=assets["client"])
    assert report["status"] == "PASS", report["problems"]
    assert report["parents"] == {
        "table_present": False, "rows": 0, "children_with_parent": 0, "children_unparented": 0,
        "dangling_parent_ids": 0, "line_level_checkable": 0, "text_slice_verified": 0,
        "text_slice_uncheckable": 0, "page_level_unchecked": 0, "verifiable": True,
    }
    # 同一切法下 child 的数量不变：父子化不改变"放进检索的是什么"。
    document = load_documents(root / "corpora/osg-3.6.5/source-manifest.json")[0]
    assert plain.chunk_count == len(block_chunks(document, max_chars=1600, split_oversized=True))
    assert plain.chunk_count == assets["target"].chunk_count


# --- 逐类篡改 ---


def test_missing_parents_table_is_rejected(assets):
    sql(assets, "DROP TABLE parents")
    report = audit(assets)
    assert report["status"] == "FAIL"
    assert report["problems"]["parent_missing"], report["problems"]
    assert report["parents"]["table_present"] is False
    with pytest.raises(IndexAdmissionError):
        admit_index(assets["target"], root=assets["root"], qdrant_client=assets["client"])


def test_child_without_a_parent_id_is_rejected(assets):
    """命中这个 child 之后取不到父块——用户会拿到半截证据。"""
    victim = rows(assets, "SELECT chunk_id FROM chunks LIMIT 1")[0][0]
    sql(assets, "UPDATE chunks SET parent_id = NULL WHERE chunk_id = ?", (victim,))
    report = audit(assets)
    assert report["status"] == "FAIL"
    assert report["parents"]["children_unparented"] == 1
    assert any("parent_id" in item["detail"] for item in report["problems"]["parent_missing"])


def test_dangling_parent_id_is_rejected(assets):
    victim = rows(assets, "SELECT chunk_id FROM chunks LIMIT 1")[0][0]
    sql(assets, "UPDATE chunks SET parent_id = 'no-such-parent' WHERE chunk_id = ?", (victim,))
    report = audit(assets)
    assert report["status"] == "FAIL"
    assert report["parents"]["dangling_parent_ids"] == 1
    assert any("不存在" in item["detail"] for item in report["problems"]["parent_missing"])


def test_parent_whose_text_is_not_the_source_slice_is_rejected(assets):
    """换个写法把正文拼出来：Hash 也一起改，所以只有"回到原文切片"这一条能拦住它。"""
    parent_id, text = rows(assets, "SELECT chunk_id, text FROM parents LIMIT 1")[0]
    tampered = text.replace("\n", "", 1)  # 逐字不同、折叠空白后也不同的拼接产物
    sql(
        assets,
        "UPDATE parents SET text = ?, content_sha256 = ? WHERE chunk_id = ?",
        (tampered, hashlib.sha256(tampered.encode("utf-8")).hexdigest(), parent_id),
    )
    report = audit(assets)
    assert report["status"] == "FAIL"
    assert report["problems"]["parent_text_mismatch"], report["problems"]
    # 内容Hash是按篡改后的正文重算的，所以它必须**不是**拦下这次篡改的那一条。
    assert not report["problems"]["content_hash_mismatch"]
    assert report["parents"]["text_slice_verified"] == len(fixture_chunks(assets)[1]) - 1
    assert report["parents"]["verifiable"] is False


def test_parent_that_does_not_cover_its_children_is_rejected(assets):
    """把第二个父块的一个child改挂到第一个父块上：child之间有洞，父块声明了没盖到的行。"""
    children, parents = fixture_chunks(assets)
    first, second = parents[0], parents[1]
    moved = [child for child in children if child.parent_id == second.chunk_id][0]
    lines = assets["text"].splitlines(keepends=True)
    widened = "".join(lines[first.start_line - 1 : moved.end_line])
    sql(
        assets,
        "UPDATE parents SET end_line = ?, text = ?, content_sha256 = ? WHERE chunk_id = ?",
        (moved.end_line, widened, hashlib.sha256(widened.encode("utf-8")).hexdigest(),
         first.chunk_id),
    )
    sql(
        assets,
        "UPDATE chunks SET parent_id = ? WHERE chunk_id = ?",
        (first.chunk_id, moved.chunk_id),
    )
    report = audit(assets)
    assert report["status"] == "FAIL"
    detail = " ".join(item["detail"] for item in report["problems"]["parent_mismatch"])
    assert "有洞" in detail, report["problems"]["parent_mismatch"]


def test_parent_id_disagreeing_between_the_two_sides_is_rejected(assets):
    """向量侧与关键词侧对"这个child属于哪个父块"说法不同：两侧会返回不同的正文。"""
    points, _ = assets["client"].scroll(COLLECTION, limit=1, with_payload=True)
    point = points[0]
    other = rows(assets, "SELECT chunk_id FROM parents WHERE chunk_id != ? LIMIT 1",
                 (point.payload["parent_id"],))[0][0]
    assets["client"].set_payload(COLLECTION, payload={"parent_id": other}, points=[point.id])
    report = audit(assets)
    assert report["status"] == "FAIL"
    assert report["problems"]["parent_mismatch"], report["problems"]
    assert any("parent_id" in item["detail"] for item in report["problems"]["parent_mismatch"])


def test_parent_page_number_must_match_its_children(assets):
    """父块与child的页码口径不同，按页取原文时会取到另一页。"""
    parent_id = rows(assets, "SELECT chunk_id FROM parents LIMIT 1")[0][0]
    sql(assets, "UPDATE parents SET page_number = 7 WHERE chunk_id = ?", (parent_id,))
    report = audit(assets)
    assert report["status"] == "FAIL"
    assert report["problems"]["parent_mismatch"] or report["problems"]["position_mismatch"]
