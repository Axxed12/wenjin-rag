"""父子片段的**对抗性用例**：针对这个设计最可能出错的地方设计反例。

分三组：

1. **检测器判别力**——故意构造坏结果，确认判定会说"坏"。一个永远说"通过"的检查
   比没有检查更糟：它把"没测到"伪装成"没问题"。每个坏例都配一个对照的好例。
2. **边界**——只有一块的文档、首块/末块、父块跨越 `blocks-split` 硬切的地方。
3. **审计准入**——父块必须能靠**现有**规则通过准入。如果为了让它过去而放宽了
   行号范围、页码或"一份索引只能有一种切片策略"的检查，那就是安全问题，
   不是实现细节：本组断言会在放宽时失败。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.parent_child_probe import (
    Chunk,
    call_producer,
    declared_parent_violations,
    discovered_producers,
    document_report,
    evidence_violations,
    explicit_parent_coverage_violations,
    parent_coverage_violations,
    source_lines,
    text_violations,
)
from wenjin.index_audit import _position_problems
from wenjin.index_registry import IndexTarget, validate_target

PATH = "include/osg/Test"
# 中间隔着两个空行：拼接子块文本会少掉它们，逐字比对能抓到，折叠空白后抓不到。
GAP_TEXT = "AA\n\n\nBB\n"
WITH_GAP = {"source_path": PATH, "version": "3.6.5", "text": GAP_TEXT}


def chunk(chunk_id: str, start: int, end: int, text: str, **extra) -> Chunk:
    return Chunk(chunk_id, PATH, "3.6.5", start, end, text, "blocks", **extra)


# --- 1. 检测器判别力 ---


def test_text_check_catches_parent_text_built_by_concatenating_children():
    """父块文本若由子块拼接而来，中间被跳过的空行就丢了——必须报出来。

    拼接结果 `"AA\\nBB\\n"` 与原文切片 `"AA\\n\\n\\nBB\\n"` 折叠空白后相同，
    所以判定不能只看归一化结果：
    """
    parent = chunk("parent", 1, 4, "AA\nBB\n")
    violations = text_violations(WITH_GAP, [parent])
    assert violations["text_conflict"] == []            # 不是冲突：它是原文的子串
    assert violations["text_whitespace_drift"] == [       # 但逐字不等
        {"chunk_id": "parent", "start_line": 1, "end_line": 4}
    ], "拼接产物被当成合法父块文本放过了"


def test_text_check_catches_parent_text_from_another_range():
    """对照的另一半：文本压根不是这段来源 → 判冲突，不是"逐字漂移"。"""
    parent = chunk("parent", 1, 4, "完全无关的文字\n")
    violations = text_violations(WITH_GAP, [parent])
    assert violations["text_conflict"], "取自别的区间的父块文本没有被报成冲突"


def test_text_check_accepts_the_true_slice():
    """对照组：真正的原文切片必须通过，否则判定过于严苛、会淹没真缺陷。"""
    correct = chunk("parent", 1, 4, GAP_TEXT)
    violations = text_violations(WITH_GAP, [correct])
    assert violations == {
        "text_conflict": [], "text_whitespace_drift": [], "partial_line_piece": [],
    }


def test_parent_coverage_catches_a_hole_between_children():
    """父块声明 1-4，子块只盖住 1-2 和 4 —— 第 3 行是洞。"""
    chunks = [
        chunk("parent", 1, 4, GAP_TEXT),
        chunk("a", 1, 2, "AA\n\n"),
        chunk("b", 4, 4, "BB\n"),
    ]
    problems = parent_coverage_violations(chunks)
    assert problems, "父块与子块之间有洞却没有报出来"
    assert "洞" in problems[0]["detail"]


def test_parent_coverage_catches_children_not_aligned_with_parent():
    """父块声明 1-6，子块只到第 4 行：父块多声明的两行是虚报。"""
    chunks = [
        chunk("parent", 1, 6, "AAAA\nBBBB\n"),
        chunk("a", 1, 2, "AA\n"),
        chunk("b", 3, 4, "BB\n"),
    ]
    problems = parent_coverage_violations(chunks)
    assert problems, "子块未与父块两端对齐，却没有报出来"


def test_parent_coverage_accepts_contiguous_children_and_allows_overlap():
    """对照组：连续子块通过；`fixed` 那种重叠也必须通过，重叠不是洞。"""
    contiguous = [
        chunk("parent", 1, 4, GAP_TEXT),
        chunk("a", 1, 2, "AA\n\n"),
        chunk("b", 3, 4, "\nBB\n"),
    ]
    assert parent_coverage_violations(contiguous) == []
    overlapping = [
        chunk("parent", 1, 4, GAP_TEXT),
        chunk("a", 1, 3, "AA\n\n\n"),
        chunk("b", 3, 4, "\nBB\n"),
    ]
    assert parent_coverage_violations(overlapping) == [], "重叠被误判成洞"


def test_explicit_parent_check_catches_a_parent_claiming_one_extra_line():
    """父块多声明一行（虚报原文）必须被抓到——结构判据也会抓到，这里走显式 `parent_id`。"""
    chunks = [
        chunk("parent-x", 1, 4, "AA\n\n\nBB\n"),
        chunk("a", 1, 2, "AA\n\n", parent_id="parent-x"),
        chunk("b", 3, 4, "\nBB\n", parent_id="parent-x"),
    ]
    assert explicit_parent_coverage_violations(chunks) == []
    over = [chunks[0]] + [
        Chunk("a", PATH, "3.6.5", 1, 2, "AA\n\n", "blocks", parent_id="parent-x"),
        Chunk("b", PATH, "3.6.5", 3, 4, "\nBB\n", "blocks", parent_id="parent-x"),
    ]
    over[0] = Chunk("parent-x", PATH, "3.6.5", 1, 5, "AA\n\n\nBB\n更多\n", "blocks")
    assert explicit_parent_coverage_violations(over), "父块多声明的行没有被报出来"


def test_explicit_parent_check_catches_children_that_break_apart():
    """子块之间断开，父块却声明覆盖了那一段：下游按行号取原文会拿到没人覆盖的行。"""
    chunks = [
        chunk("parent-x", 1, 6, "六个行\n"),
        chunk("a", 1, 2, "AA\n", parent_id="parent-x"),
        chunk("b", 5, 6, "BB\n", parent_id="parent-x"),
    ]
    problems = explicit_parent_coverage_violations(chunks)
    assert problems and "断开" in problems[0]["detail"], "断开没有被报出来"


def test_declared_parent_check_catches_an_orphan_parent():
    """声明了却没有 child 引用的父块：检索永远命不中它，只是看着像覆盖到了。"""
    declared = [chunk("parent-x", 1, 4, "AA\n\n\nBB\n")]
    children = [chunk("a", 1, 2, "AA\n\n", parent_id="parent-x")]
    assert declared_parent_violations(children + declared, declared) == []
    orphan = declared + [chunk("parent-y", 1, 4, "AA\n\n\nBB\n")]
    problems = declared_parent_violations(children + orphan, orphan)
    assert problems and "没有任何 child 引用" in problems[0]["detail"]


def test_declared_parent_check_catches_a_child_without_a_parent():
    """漏挂 `parent_id` 的 child 会以 child 身份返回，去重漏掉它。"""
    declared = [chunk("parent-x", 1, 4, "AA\n\n\nBB\n")]
    children = [chunk("a", 1, 2, "AA\n\n", parent_id="parent-x"), chunk("b", 3, 4, "\nBB\n")]
    problems = declared_parent_violations(children + declared, declared)
    assert problems and "没有 parent_id" in problems[0]["detail"]


def evidence(*spans) -> list[dict]:
    return [
        {"rank": index, "chunk_id": f"c{index}", "source_path": PATH,
         "start_line": start, "end_line": end, "text": text}
        for index, (start, end, text) in enumerate(spans, start=1)
    ]


def test_evidence_check_catches_the_same_parent_returned_twice():
    """同一个父块被多个 child 命中时返回两次：白占名额，必须报出来。"""
    duplicated = [
        {"rank": 1, "chunk_id": "p", "source_path": PATH,
         "start_line": 1, "end_line": 40, "text": "整段父块正文"},
        {"rank": 2, "chunk_id": "p2", "source_path": PATH,
         "start_line": 1, "end_line": 40, "text": "整段父块正文"},
    ]
    violations = evidence_violations(duplicated)
    assert violations["duplicate_evidence"], "同一个父块占了两条证据，却没有报出来"


def test_evidence_check_catches_a_parent_and_its_hit_child_sharing_top5():
    """父块与命中的那个子块同时出现：父块已经包含它，子块这一条是多余的。"""
    both = evidence((10, 20, "子块正文"), (1, 40, "父块正文"))
    violations = evidence_violations(both)
    assert violations["containment_within_result"], "父块与子块同时占了名额，却没有报出来"


def test_evidence_check_does_not_mistake_hard_split_siblings_for_a_duplicate():
    """同一区间、不同正文 = 单行硬切的同胞片段，不是父子去重失败。

    把它们报成"重复证据"会让检查在 3D Tiles 那种长行文档上产生成片假阳性——
    真实索引里这类片段有 58 个。
    """
    siblings = evidence((7, 7, "前半行"), (7, 7, "后半行"))
    violations = evidence_violations(siblings)
    assert violations["duplicate_evidence"] == []
    assert violations["same_position_different_text"], "同胞片段应被单独记录，不能静默"


def test_evidence_check_catches_a_broken_rank_sequence():
    """名次必须连续且从 1 开始：`select_context` 跳过超预算片段时最容易出错。"""
    broken = evidence((1, 2, "a"), (3, 4, "b"))
    broken[1]["rank"] = 3
    assert evidence_violations(broken)["rank_sequence_broken"]


def test_evidence_check_passes_a_correct_result():
    """对照组：互不包含、名次连续的正确结果必须零违规。"""
    good = evidence((1, 5, "a"), (10, 14, "b"), (30, 33, "c"))
    assert evidence_violations(good) == {
        "duplicate_evidence": [], "same_position_different_text": [],
        "containment_within_result": [], "rank_sequence_broken": [],
    }


# --- 2. 边界 ---


def block_producers():
    return [(name, member) for name, member in discovered_producers() if "block" in name]


def test_block_producers_are_present_for_boundary_cases():
    assert block_producers(), "没有发现按块切片的生产者，边界用例无从构造"


@pytest.mark.parametrize("fixture,text", [
    ("single_block", "只有一块内容，没有空行分隔。\n"),
    ("leading_trailing_blank", "\n\n中间才是内容\n\n"),
    ("oversized_single_block", "".join(f"line-{n:03d}\n" for n in range(30))),
])
def test_first_and_last_chunk_still_span_the_whole_document(fixture, text):
    """首块不能从第一个**有内容**的行之后开始，末块不能提前结束。

    只在文档只有一块、或首尾是空行时最容易出错——父块合并的边界判断若写错，
    会把文档开头或结尾整段丢掉。
    """
    document = {"source_path": PATH, "version": "3.6.5", "text": text}
    lines = source_lines(document)
    non_blank = [n for n, line in enumerate(lines, start=1) if line.strip()]
    assert non_blank, f"{fixture} 夹具本身没有内容行"
    for name, member in block_producers():
        chunks = call_producer(member, document, {"split_oversized": True}, max_chars=120)
        assert chunks, f"{name}/{fixture} 没有产出块"
        assert chunks[0].start_line <= non_blank[0], (
            f"{name}/{fixture} 首块从第 {chunks[0].start_line} 行开始，"
            f"漏掉了第 {non_blank[0]} 行"
        )
        assert chunks[-1].end_line >= non_blank[-1], (
            f"{name}/{fixture} 末块到第 {chunks[-1].end_line} 行结束，"
            f"漏掉了第 {non_blank[-1]} 行"
        )


def test_parent_cannot_claim_more_lines_than_the_source_has():
    """父块跨越 `blocks-split` 硬切点时，最容易把行号算到原文之外。

    硬切产生的多个片段共享同一原始行号，父块若按"片段数 × 行数"估范围就会越界。
    """
    text = "".join(f"line-{n:03d}-{'z' * 30}\n" for n in range(20))
    document = {"source_path": PATH, "version": "3.6.5", "text": text}
    total = len(source_lines(document))
    for name, member in block_producers():
        chunks = call_producer(member, document, {"split_oversized": True}, max_chars=150)
        report = document_report(document, chunks)
        assert report["range_invalid"] == [], f"{name} 有块声明的行号超出原文（共{total}行）"
        assert report["text_conflict"] == [], f"{name} 有块的 text 对不上它的行区间"
        # 每个块的行区间都必须真的落在原文里，父块也不例外。
        for item in chunks:
            assert 1 <= item.start_line <= item.end_line <= total, (
                f"{name} 的块 {item.chunk_id} 声明了越界区间 "
                f"{item.start_line}-{item.end_line}（原文共 {total} 行）"
            )


# --- 3. 审计准入：父块必须靠现有规则通过，不允许为它放宽 ---


def write_source(root: Path, relative: str, line_count: int) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {n}\n" for n in range(1, line_count + 1)), encoding="utf-8")


def test_position_check_still_accepts_a_contiguous_parent_range(tmp_path):
    """父子合并出的连续区间**本来就能**通过位置检查，不需要放宽任何规则。

    这条是"准入没必要放松"的证据：父块是相邻子块并成的连续行区间，
    与普通块在审计眼里没有区别。
    """
    write_source(tmp_path, "docs/guide.md", 6)
    record = {"parser": "html-line-v1"}
    row = {"start_line": 1, "end_line": 6, "page_number": None}
    assert _position_problems(row, record, "docs/guide.md", tmp_path, {}, {}) is None


def test_position_check_still_rejects_a_range_beyond_the_source(tmp_path):
    """行号超出源文件行数必须仍然被拒——放宽它就是放开"引用不存在的原文"。"""
    write_source(tmp_path, "docs/guide.md", 6)
    record = {"parser": "html-line-v1"}
    row = {"start_line": 1, "end_line": 999, "page_number": None}
    detail = _position_problems(row, record, "docs/guide.md", tmp_path, {}, {})
    assert detail and "超出来源文件行数" in detail, "行号越界被放过了"


def test_position_check_still_rejects_an_illegal_range(tmp_path):
    """start > end 或 start < 1 必须仍然被拒。"""
    write_source(tmp_path, "docs/guide.md", 6)
    record = {"parser": "html-line-v1"}
    for row in ({"start_line": 5, "end_line": 2, "page_number": None},
                {"start_line": 0, "end_line": 3, "page_number": None},
                {"start_line": None, "end_line": 3, "page_number": None}):
        assert _position_problems(row, record, "docs/guide.md", tmp_path, {}, {}), \
            f"非法区间 {row} 被放过了"


def test_position_check_still_requires_a_page_number_for_page_sources(tmp_path):
    """按页来源必须带物理页码；这一条若为了父块而放宽，OCR 页会失去位置锚点。"""
    record = {"parser": "pymupdf-page-v1"}
    row = {"start_line": 1, "end_line": 10, "page_number": None}
    detail = _position_problems(row, record, "docs/report.pdf", tmp_path, {}, {})
    assert detail and "物理页码" in detail, "按页来源缺页码被放过了"


def build_index_assets(root: Path, strategies: list[str]) -> dict:
    """造一份最小的自洽资产：manifest + 一个装着若干 chunk 行的 BM25。"""
    corpus_dir = root / "corpora" / "demo-1.0"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    text = "alpha\nbeta\n"
    raw_sha256 = hashlib.sha256(text.encode()).hexdigest()
    manifest = {
        "schema_version": 3,
        "corpus_id": "demo-1.0",
        "library": "demo",
        "version": "1.0",
        "raw_data_subdir": "demo/1.0",
        "license": "test",
        "files": [{
            "path": "doc.txt", "raw_sha256": raw_sha256, "canonical_sha256": raw_sha256,
            "normalization": "text-newline-lf-v1", "logical_document_id": "demo-1.0:doc.txt",
            "encoding": "utf-8", "indexable": True,
        }],
    }
    manifest_path = corpus_dir / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    write_source(root / "raw_data", "demo/1.0/doc.txt", 2)

    bm25_path = root / "candidate.sqlite"
    with closing(sqlite3.connect(bm25_path)) as database:
        database.execute("""
            CREATE TABLE chunks(
                chunk_id, source_path, version, start_line, end_line, strategy,
                content_sha256, source_raw_sha256, source_canonical_sha256,
                normalization, page_number, logical_document_id, parser,
                ocr_derived, retrieval_text, raw_text
            )""")
        for index, strategy in enumerate(strategies):
            database.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"1.0:doc.txt:{strategy}:{index:04d}", "doc.txt", "1.0", 1, 2, strategy,
                 hashlib.sha256(text.encode()).hexdigest(), raw_sha256, raw_sha256,
                 "text-newline-lf-v1", None, "demo-1.0:doc.txt", None, None, "", text),
            )
        database.commit()
    target = IndexTarget(
        target_id="demo-1.0-blocks", corpus_id="demo-1.0", library="demo", version="1.0",
        strategy="blocks", dense_collection="demo_collection",
        bm25_path="candidate.sqlite", manifest_path="corpora/demo-1.0/source-manifest.json",
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        chunk_count=len(strategies), vector_dimension=3, embedding_model="test",
    )
    return {"target": target, "bm25_path": bm25_path}


def test_admission_still_rejects_two_strategies_in_one_index(tmp_path):
    """一份索引里只能有一种切片策略——**父子块并存最可能撞上的就是这一条**。

    父块若用另一个策略名入库（比如 `blocks-split-parent`），`validate_target` 现在会拒。
    为了让父块通过而放宽这条检查，等于允许"索引里混着两种口径的块却不告诉调用方",
    那是准入放松，属于安全问题。这条断言就是探测器。
    """
    assets = build_index_assets(tmp_path, ["blocks", "blocks-split"])
    with pytest.raises(ValueError, match="版本或切片策略"):
        validate_target(assets["target"], root=tmp_path, qdrant_client=SimpleNamespace())


def test_admission_accepts_a_single_strategy(tmp_path):
    """对照组：单一策略能走到 Qdrant 校验那一步，说明上一条不是因为别的原因失败的。"""
    assets = build_index_assets(tmp_path, ["blocks"])
    with pytest.raises(Exception) as error:
        validate_target(assets["target"], root=tmp_path, qdrant_client=SimpleNamespace())
    # 失败原因必须不再是策略问题——它已经越过策略检查，死在 Qdrant 那一步。
    assert "版本或切片策略" not in str(error.value)
