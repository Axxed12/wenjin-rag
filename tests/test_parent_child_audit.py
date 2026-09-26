import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.parent_child_fixtures import (
    ARCHIVE_PATH,
    FILE_PATH,
    SOURCE_TEXT,
    FakeQdrant,
    build_parented_assets,
    edit_sqlite,
)
from wenjin.index_audit import audit_index


def run_audit(assets: dict) -> dict:
    return audit_index(
        assets["target"], root=assets["root"],
        qdrant_client=FakeQdrant(assets["payloads"]),
    )


@pytest.fixture()
def assets(tmp_path):
    return build_parented_assets(tmp_path)


# --- 基线：未篡改必须 PASS，否则后面的变异测试没有意义 ---


def test_synthetic_parented_index_passes_admission(assets):
    """父块**靠现有规则**就能过准入：来源绑定、位置范围、正文切片逐字比对全绿。

    如果这条开始失败，先怀疑审计被收紧到父块过不去，再看是不是夹具坏了。
    """
    report = run_audit(assets)
    assert report["status"] == "PASS", report["problems"]


def test_admission_report_actually_verified_the_parents(assets):
    """通过不等于查过：报告必须说明父块被逐条核验到了什么程度。

    `text_slice_verified == line_level_checkable` 才说明"每个可做切片比对的父块都
    真的比过"。只看 `status=PASS` 会把"根本没查"读成"查了没问题"。
    """
    report = run_audit(assets)["parents"]
    assert report["table_present"] is True
    assert report["rows"] == len(assets["parents"]), "审计读到的父块数与夹具不一致"
    assert report["line_level_checkable"] == report["rows"], "有父块没做正文切片比对"
    assert report["text_slice_verified"] == report["rows"]
    assert report["verifiable"] is True
    assert report["dangling_parent_ids"] == 0
    assert report["children_unparented"] == 0


def test_fixture_really_has_a_parent_with_several_children(assets):
    """夹具本身要够狠：至少一个父块带多个 child，否则父子对齐检查是空跑。"""
    multi = [
        parent for parent in assets["parents"]
        if sum(1 for child in assets["children"] if child.parent_id == parent.chunk_id) > 1
    ]
    assert multi, "夹具里没有多 child 的父块，父子对齐与无洞检查没有被触发"


# --- 逐类篡改：父块侧的每一处都必须被拦下 ---


def test_parent_text_that_is_not_the_source_slice_is_caught(assets):
    """父块正文与它声明的行区间不再逐字一致——拼接、重排、任何编辑都会落到这里。

    这是父块最核心的契约（证据正文必须是原文切片）。改动一个字符也必须报出来。
    """
    parent = assets["parents"][0]
    text = parent.text.replace("\n", "", 1)
    assert text != parent.text, "夹具的父块正文里没有换行，改不动"
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET text = ? WHERE chunk_id = ?", (text, parent.chunk_id)),
    ])
    report = run_audit(assets)
    assert report["problems"]["parent_text_mismatch"], "父块正文被改了却没有报出来"
    assert report["status"] == "FAIL"


def test_parent_content_hash_mismatch_is_caught(assets):
    """父块正文的 content_sha256 必须与正文对得上，不能由写入方自己声明。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET content_sha256 = ? WHERE chunk_id = ?",
         ("0" * 64, parent.chunk_id)),
    ])
    report = run_audit(assets)
    assert report["problems"]["content_hash_mismatch"], "父块正文 Hash 对不上却没有报出来"


def test_parent_range_widened_by_one_line_is_caught(assets):
    """父块多声明一行 = 虚报原文：正文与新区间不再逐字相等，子块也不再两端对齐。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET end_line = end_line + 1 WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    problems = report["problems"]
    assert problems["parent_text_mismatch"] or problems["parent_mismatch"], \
        "父块把区间加宽了一行却没有被拦下"
    assert report["status"] == "FAIL"


def test_parent_range_beyond_the_source_file_is_caught(assets):
    """父块行号超出来源文件实际行数：位置范围检查必须仍然生效。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET end_line = 99999 WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    assert report["problems"]["position_mismatch"], "父块行号越界没有被拦下"


def test_missing_parent_row_is_caught(assets):
    """删掉一个父块行：引用它的 child 变成悬空引用，命中后取不到父块。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("DELETE FROM parents WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    assert report["problems"]["parent_missing"], "父块行被删却没有被拦下"
    assert report["parents"]["dangling_parent_ids"] > 0, "报告没有说明悬空引用的数量"


def test_orphan_parent_without_children_is_caught(assets):
    """父块没有任何 child 引用：它永远不可能作为证据返回，却看着像覆盖了那段原文。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE chunks SET parent_id = NULL WHERE parent_id = ?", (parent.chunk_id,)),
    ])
    for payload in assets["payloads"]:
        if payload["parent_id"] == parent.chunk_id:
            payload["parent_id"] = None
    report = run_audit(assets)
    problems = report["problems"]
    assert problems["parent_missing"], "没有 child 的父块没有被报出来"
    assert report["parents"]["children_unparented"] > 0, \
        "父子化索引里出现无父块的 child，报告必须计数"


def test_parent_id_disagreeing_between_the_two_sides_is_caught(assets):
    """两侧对"这个 child 属于哪个父块"必须一致。

    不一致时，向量侧和关键词侧命中同一个 child 会返回**两段不同的正文**，
    去重按父块身份做，于是同一条证据以两个身份各占一个名额。
    """
    saved = assets["payloads"][0]["parent_id"]
    assets["payloads"][0]["parent_id"] = assets["parents"][-1].chunk_id
    report = run_audit(assets)
    assert report["problems"]["parent_mismatch"], "两侧 parent_id 不一致却没有报出来"
    assert saved is not None, "夹具的第一个 child 本来就没有 parent_id"


def test_parent_strategy_mismatch_is_caught(assets):
    """父块行也必须与目标声明的切片策略一致——它同样进了索引文件。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET strategy = 'blocks-split' WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    assert report["problems"]["strategy_mismatch"], "父块策略与目标不符却没有报出来"


# --- 父子结构声明与实际的对应：两个方向都要拦 ---


def test_parented_target_without_a_parents_table_is_caught(assets):
    """声明了父子策略、索引里却没有 parents 表：命中 child 后取不到父块。

    表被删掉时 `read_bm25_parents` 返回 None（"没有父子结构"）而不是空列表，
    这两件事必须分开：前者在父子策略下是缺陷，后者在非父子策略下是正常的。
    """
    edit_sqlite(assets["bm25_path"], [("DROP TABLE parents", ())])
    report = run_audit(assets)
    assert report["problems"]["parent_missing"], "声明父子却没有父块表，没有被拦下"
    assert report["parents"]["table_present"] is False


def test_declared_parented_but_zero_parent_rows_is_caught(assets):
    """对照：表在但一行父块都没有——与"表不存在"是两件事，也必须拦下。"""
    edit_sqlite(assets["bm25_path"], [("DELETE FROM parents", ())])
    report = run_audit(assets)
    assert report["problems"]["parent_missing"], "空的父块表没有被拦下"
    assert report["parents"]["table_present"] is True
    assert report["parents"]["rows"] == 0


@pytest.mark.parametrize("drain", ["drop_table", "empty_table"], ids=["表不存在", "表为空"])
def test_zero_verified_parents_must_not_be_reported_as_verifiable(assets, drain):
    """**已报告缺陷**：一个父块都没核验到时，`parents.verifiable` 不能是 True。

    判据是 `text_slice_verified == line_level_checkable`，两侧都是 0 时相等，
    于是"什么都没查"被报成"核验通过"——这正是该字段本要防的那件事
    （实现自己的注释写着"不能让'这一项没查'混进'这一项通过了'"）。

    影响范围有限：同一份报告里 `problems["parent_missing"]` 会置位、`status` 仍是 FAIL，
    所以**索引不会被误放行**；错的只是报告里这一格的可核验性口径。
    这条断言按正确行为写，修好后自然转绿。
    """
    edit_sqlite(assets["bm25_path"], [
        ("DROP TABLE parents" if drain == "drop_table" else "DELETE FROM parents", ()),
    ])
    parents = run_audit(assets)["parents"]
    assert parents["rows"] == 0
    assert parents["verifiable"] is False, (
        f"父块行数为 0，`verifiable` 却是 {parents['verifiable']!r}："
        "没有父块可核验时不能报成核验通过"
    )


def test_non_parented_target_carrying_a_parents_table_is_caught(assets):
    """反过来：索引里留着 parents 表，目标却声明了别的策略。

    两侧的策略列必须同步改，否则 `validate_target` 会先在"一份索引只能有一种策略"
    那一关拦下，就测不到这一条了——那关本身也是要保留的（见对抗性用例）。
    """

    edit_sqlite(assets["bm25_path"], [
        ("UPDATE chunks SET strategy = 'blocks-split'", ()),
        ("UPDATE parents SET strategy = 'blocks-split'", ()),
    ])
    assets["target"] = replace(assets["target"], strategy="blocks-split")
    report = run_audit(assets)
    assert report["problems"]["parent_mismatch"], "多出的父块表没有被拦下"


def test_mutating_the_fixture_does_not_touch_the_real_corpora(assets):
    """夹具必须是真拷贝：改坏它不能波及仓库里的真实语料。

    本仓库曾因用链接冒充拷贝把真实语料写坏过，所以这里显式断言夹具落在临时目录、
    不在仓库的语料目录之内，且临时语料与真实语料不是同一个文件。
    """
    root = Path(assets["root"]).resolve()
    repo = Path(__file__).resolve().parents[1].resolve()
    assert not root.is_relative_to(repo), "夹具落在了仓库目录内，改坏它会影响真实语料"
    for guarded in (repo / "raw_data", repo / "corpora", repo / "data", repo / "configs"):
        assert not root.is_relative_to(guarded)
    copied = root / "raw_data" / "osg" / "3.6.5" / FILE_PATH
    assert copied.is_file() and copied.read_text(encoding="utf-8") == SOURCE_TEXT
    real = repo / "raw_data" / "osg" / "3.6.5" / FILE_PATH
    if real.is_file():
        assert copied.stat().st_ino != real.stat().st_ino, "夹具与真实语料指向同一个文件"


def test_audit_reads_the_fixture_from_disk_not_from_memory(assets):
    """审计读的是磁盘上的索引：改了库里的值就必须影响结论，不能是缓存的旧结果。"""
    parent = assets["parents"][0]
    before = run_audit(assets)
    assert before["status"] == "PASS"
    shutil.copy2(assets["bm25_path"], assets["bm25_path"].with_suffix(".bak"))
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET text = '被改坏了' WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    assert run_audit(assets)["status"] == "FAIL"


# --- 父块能不能绕过检索白名单或 OCR 标记核验 ---
#
# 这一组回答的是"安全判定会不会被新结构绕过去"：一个 child 在检索白名单内，
# 但它的父块引用了不在白名单里的内容时会怎样。


@pytest.fixture()
def assets_with_archive(tmp_path):
    """带一份 `indexable: False`（只归档、不检索）的文件。"""
    return build_parented_assets(tmp_path, max_chars=60, with_archive_file=True)


def test_the_archive_file_is_really_out_of_scope(assets_with_archive):
    """夹具前提：白名单里只有那份可检索文件，归档件不在其中。

    没有这条，下面的"绕过"用例可能因为归档件本来就在白名单里而失去意义。
    """
    report = run_audit(assets_with_archive)
    assert report["status"] == "PASS", report["problems"]
    assert report["retrieval_scope_files"] == 1
    assert report["manifest_records"] == 2, "归档件应当仍登记在 manifest 里"


def test_parent_pointing_outside_the_retrieval_scope_is_caught(assets_with_archive):
    """父块的 `source_path` 指向只归档不检索的文件：必须按越权拦下。

    这条比 child 那一侧更要紧：父块是**直接进证据**的正文，如果它能把白名单之外的
    内容带出来，调用方看到的就是一段没有检索授权的原文。
    """
    parent = assets_with_archive["parents"][0]
    edit_sqlite(assets_with_archive["bm25_path"], [
        ("UPDATE parents SET source_path = ? WHERE chunk_id = ?",
         (ARCHIVE_PATH, parent.chunk_id)),
    ])
    report = run_audit(assets_with_archive)
    assert report["problems"]["outside_retrieval_scope"], \
        "父块引用了检索白名单之外的文件，却没有被拦下"
    assert report["status"] == "FAIL"


def test_parent_pointing_at_an_unregistered_path_is_caught(assets):
    """父块的 `source_path` 不在 manifest 里：来源无法绑定，同样要拦。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET source_path = 'include/osg/Nowhere' WHERE chunk_id = ?",
         (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    assert report["problems"]["source_path_unmapped"], "父块来源未登记却没有被拦下"


def test_parent_cannot_claim_an_ocr_marker_its_parser_does_not_support(assets):
    """父块自称"这段正文是 OCR 来的"、parser 却是文本层：标记与解析器必须自洽。

    把识别文本洗成原文的样子，正是 OCR 标记要防的事；父块直接进证据，
    这一层如果只核 child，父块就成了绕过去的通道。
    """
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET ocr_derived = 1 WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    assert report["problems"]["ocr_marker_mismatch"], \
        "父块的 ocr_derived 与 parser 不符，却没有被拦下"


def test_parent_cannot_claim_a_parser_the_manifest_does_not_allow(assets):
    """父块声称由 OCR 解析器产出，而 manifest 声明这份文件是文本解析。"""
    parent = assets["parents"][0]
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE parents SET parser = 'mineru-ocr-page-v1', ocr_derived = 1 "
         "WHERE chunk_id = ?", (parent.chunk_id,)),
    ])
    report = run_audit(assets)
    problems = report["problems"]
    assert problems["parser_mismatch"] or problems["ocr_marker_mismatch"], \
        "父块的解析器不在 manifest 允许范围内，却没有被拦下"


def test_parent_source_must_match_its_children(assets_with_archive):
    """父块与 child 来自不同文件/不同页时拦下——否则命中的 child 与返回的正文对不上。"""
    parent = assets_with_archive["parents"][0]
    edit_sqlite(assets_with_archive["bm25_path"], [
        ("UPDATE parents SET source_path = ? WHERE chunk_id = ?",
         (ARCHIVE_PATH, parent.chunk_id)),
    ])
    report = run_audit(assets_with_archive)
    assert report["problems"]["parent_mismatch"], \
        "父块与 child 的来源不一致，却没有被 parent_mismatch 报出来"


def test_parent_range_still_cannot_cover_lines_no_child_covers(assets):
    """父块不能声明覆盖任何"没有 child 覆盖"的行——这是白名单之外内容进来的另一条路。

    白名单是**文件级**的，所以在行这一层不存在"越权内容"；真正的护栏是父子无洞：
    父块声明的每一行都必须有 child 覆盖，而每个 child 自身都要过来源绑定与白名单检查。
    """
    parent = assets["parents"][0]
    members = sorted(
        (c for c in assets["children"] if c.parent_id == parent.chunk_id),
        key=lambda item: item.start_line,
    )
    assert len(members) >= 2, "夹具需要一个多 child 的父块"
    # 把中间那个 child 的 parent_id 摘掉：父块声明的区间就出现了一段没人覆盖的行。
    edit_sqlite(assets["bm25_path"], [
        ("UPDATE chunks SET parent_id = NULL WHERE chunk_id = ?", (members[0].chunk_id,)),
    ])
    for payload in assets["payloads"]:
        if payload["chunk_id"] == members[0].chunk_id:
            payload["parent_id"] = None
    report = run_audit(assets)
    problems = report["problems"]
    assert problems["parent_mismatch"] or problems["parent_missing"], \
        "父块覆盖了没有 child 覆盖的行，却没有被拦下"
