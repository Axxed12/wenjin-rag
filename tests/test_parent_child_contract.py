"""父子片段的**契约**检查：只看 `Chunk` 的公共字段和块间的行区间关系。

这套检查的意义在于**它们现在就该全绿**：契约是"每条证据的 `text` 逐字等于它声明的
`[start_line, end_line]` 原文切片"，父子结构只是在这条契约之上多一层关系，
不应该把它放松。所以本文件的每一类断言都分两半：

- **无条件的部分**（原文切片、区间合法、页码口径）在改动前后都必须通过；
- **父子相关的部分**在样本里出现行区间包含关系（或显式 parent 字段）时才执行，
  否则 `skip` 并写明"没测到"——不写清楚的话，跳过会被误读成通过。

实现交付后，`tests/parent_child_probe.discovered_producers()` 会自动发现新的切片函数，
这里无需改动即可把它纳入。若父子结构是**另外的**产出路径（不在切片模块里），
请把该模块加进 `PRODUCER_MODULES`。

验证方不修改实现：本文件只报告缺陷。
"""

import pytest

from tests.parent_child_probe import (
    call_producer,
    call_producer_parts,
    declared_parent_violations,
    discovered_producers,
    document_report,
    explicit_parent_children,
    explicit_parent_coverage_violations,
    explicit_parent_map,
    parent_coverage_violations,
    parent_field_names,
    parent_self_reference_violations,
    producer_variants,
    source_lines,
    structural_parent_map,
    text_violations,
    unresolvable_parent_links,
)

# 构造出的文档覆盖三类边界：多块可合并、只有一块（父块==子块）、首尾空行。
SINGLE_BLOCK = "只有一块内容，没有空行分隔。\n"
MULTI_BLOCK = (
    "第一块：头文件声明。\nvoid addDrawable();\n"
    "\n"
    "第二块：说明文字，稍长一些，用来把合并预算吃掉。\n"
    "\n"
    "第三块：结尾。\n"
)
LEADING_TRAILING_BLANK = "\n\n中间才是内容\n\n"
# 单行超长：触发 blocks-split 的字符级硬切，半行片段就出现在这里。
ONE_LONG_LINE = ("abcdefghij" * 40) + "\n"

FIXTURES = {
    "single_block": SINGLE_BLOCK,
    "multi_block": MULTI_BLOCK,
    "leading_trailing_blank": LEADING_TRAILING_BLANK,
    "one_long_line": ONE_LONG_LINE,
}


def document(text: str) -> dict:
    return {"source_path": "include/osg/Test", "version": "3.6.5", "text": text}


def producer_cases() -> list[tuple[str, object, dict]]:
    """`(说明, 生产者, 参数变体)` 的组合；实现新增切片函数后自动进入。"""
    cases = []
    for name, member in discovered_producers():
        for variant in producer_variants(member):
            label = name + ("".join(f"[{key}={value}]" for key, value in variant.items()))
            cases.append((label, member, variant))
    return cases


PRODUCERS = producer_cases()


def test_producers_were_actually_discovered():
    """发现机制本身要有效：一个都没发现的话，下面全是空跑。"""
    assert PRODUCERS, "切片模块里没有发现 `f(document, ...)` 形态的函数，检查未生效"


@pytest.mark.parametrize(("label", "member", "variant"), PRODUCERS,
                         ids=[case[0] for case in PRODUCERS])
@pytest.mark.parametrize("fixture", sorted(FIXTURES), ids=sorted(FIXTURES))
def test_chunk_text_equals_its_declared_source_slice(label, member, variant, fixture):
    """硬契约：`text` **逐字**等于 `[start_line, end_line]` 的原文切片。

    行号 1-based、左闭右闭（`_make_chunk` 用 `start_line=start+1, end_line=end`）。
    单行硬切产生的半行片段是唯一允许的例外，它被单列（见 `text_violations`），
    不计入覆盖——`Chunk` 没有字符偏移量，无法证明它盖住了整行。
    """
    chunks = call_producer(member, document(FIXTURES[fixture]), variant, max_chars=60)
    assert chunks, f"{label} 在 {fixture} 上没有产出 chunk"
    violations = text_violations(document(FIXTURES[fixture]), chunks)
    assert violations["text_conflict"] == [], (
        f"{label}/{fixture} 的 text 不是它声明行区间的原文切片：{violations['text_conflict']}"
    )
    assert violations["text_whitespace_drift"] == [], (
        f"{label}/{fixture} 的 text 只在折叠空白后才等于原文，逐字不等："
        f"{violations['text_whitespace_drift']}"
    )


@pytest.mark.parametrize(("label", "member", "variant"), PRODUCERS,
                         ids=[case[0] for case in PRODUCERS])
@pytest.mark.parametrize("fixture", sorted(FIXTURES), ids=sorted(FIXTURES))
def test_chunk_ranges_and_page_convention_hold(label, member, variant, fixture):
    """行号区间合法、落在原文范围内；页码口径与所在解析单元一致。"""
    text = FIXTURES[fixture]
    chunks = call_producer(member, document(text), variant, max_chars=60)
    report = document_report(document(text), chunks)
    for kind in ("range_invalid", "source_kind_inconsistent"):
        assert report[kind] == [], f"{label}/{fixture} {kind}：{report[kind]}"


@pytest.mark.parametrize(("label", "member", "variant"), PRODUCERS,
                         ids=[case[0] for case in PRODUCERS])
@pytest.mark.parametrize("fixture", sorted(FIXTURES), ids=sorted(FIXTURES))
def test_children_reconstruct_every_non_blank_line(label, member, variant, fixture):
    """子块（无父子结构时就是全部块）合起来必须盖住原文的**每一个非空行**。

    空行不承载内容，切片按空行分块时不会产出只含空行的块——这是正确行为。
    但**有内容**的行一个都不能丢，丢了就是检索永远命中不到的内容。
    """
    text = FIXTURES[fixture]
    chunks = call_producer(member, document(text), variant, max_chars=60)
    lines = source_lines(document(text))
    wanted = {number for number, line in enumerate(lines, start=1) if line.strip()}
    covered: set[int] = set()
    for chunk in chunks:
        covered.update(range(chunk.start_line, chunk.end_line + 1))
    missing = sorted(wanted - covered)
    assert missing == [], f"{label}/{fixture} 有内容的行没被任何块覆盖：{missing}"


# --- 父子关系：出现结构时才检查，否则明确报"没测到" ---


def sample_with_parents():
    """找一个真的产出父子结构的样本；找不到就返回 None。"""
    for label, member, variant in PRODUCERS:
        for fixture, text in FIXTURES.items():
            chunks = call_producer(member, document(text), variant, max_chars=60)
            if structural_parent_map(chunks):
                return label, fixture, chunks
    return None


PARENT_SAMPLE = sample_with_parents()


def test_parent_structure_exists_or_report_not_measured():
    """行区间包含关系是父子的**结构性判据**：任何实现都逃不过它。

    没有这种结构就说清"未测到"，不能让它静默变成一句"通过"。
    """
    if PARENT_SAMPLE is None:
        pytest.skip(
            "未测到：当前切片产出里没有任何行区间包含关系，"
            "无法从结构上判定父子合并是否正确（父子结构可能尚未实现，"
            "或它在别的产出路径上，需要把该模块加进 PRODUCER_MODULES）"
        )
    label, fixture, chunks = PARENT_SAMPLE
    parents = structural_parent_map(chunks)
    assert parents, f"{label}/{fixture} 应当有父块"


@pytest.mark.parametrize("case", [0], ids=["parent_structure"])
def test_parent_range_is_hole_free_and_aligns_with_its_children(case):
    """父块的行区间必须被它的子块**无洞**盖满，且两端严格对齐。

    父块若只是"相邻 child 合并"，合并规则漏掉中间的空行块、或把不同来源的块并进来，
    就会声明一段它其实没盖住的范围——下游按行号取原文时会拿到不存在的证据。
    允许子块之间**重叠**（`fixed` 策略本就重叠），但不允许断开。
    """
    if PARENT_SAMPLE is None:
        pytest.skip("未测到：没有父子结构可查（见 test_parent_structure_exists_or_report_not_measured）")
    label, fixture, _ = PARENT_SAMPLE
    chunks = call_producer(
        next(member for name, member, _ in PRODUCERS if name == label),
        document(FIXTURES[fixture]), {}, max_chars=60,
    )
    problems = parent_coverage_violations(chunks)
    assert problems == [], f"{label}/{fixture} 父块行区间有洞或未对齐：{problems}"


def test_parent_text_is_the_source_slice_not_a_concatenation():
    """父块的 `text` 必须是原文切片，**不是**子块文本的拼接产物。

    两者在有被跳过的空行时就会分叉：拼接结果少一个换行，逐字比对能抓到，
    折叠空白后却一样——所以这里查的是逐字相等。`text_violations` 已在
    `test_chunk_text_equals_its_declared_source_slice` 里覆盖全部块，这里只把
    父块单独再点一次，确保父块不会被"只检查子块"的写法漏掉。
    """
    if PARENT_SAMPLE is None:
        pytest.skip("未测到：没有父子结构可查")
    label, fixture, chunks = PARENT_SAMPLE
    parents = structural_parent_map(chunks)
    parent_chunks = [chunk for chunk in chunks if chunk.chunk_id in parents]
    assert parent_chunks, "结构判定说有父块，取出来却是空的"
    violations = text_violations(document(FIXTURES[fixture]), parent_chunks)
    assert violations["text_conflict"] == [], f"{label}/{fixture} 父块文本不是原文切片：" \
                                              f"{violations['text_conflict']}"
    assert violations["text_whitespace_drift"] == [], \
        f"{label}/{fixture} 父块文本只在折叠空白后相等：{violations['text_whitespace_drift']}"


def parented_cases() -> list[tuple[str, object, dict]]:
    """产出里真的带 parent_id 的组合。"""
    cases = []
    for label, member, variant in PRODUCERS:
        for fixture, text in FIXTURES.items():
            chunks = call_producer(member, document(text), variant, max_chars=60)
            if explicit_parent_children(chunks):
                cases.append((label, member, variant))
                break
    return cases


PARENTED = parented_cases()
PARENTED_IDS = [f"{label}{variant}" for label, _, variant in PARENTED]


@pytest.mark.parametrize(("label", "member", "variant"), PARENTED, ids=PARENTED_IDS)
@pytest.mark.parametrize("fixture", sorted(FIXTURES), ids=sorted(FIXTURES))
def test_parent_range_equals_the_union_of_its_children(label, member, variant, fixture):
    """父块区间必须**恰好等于**它所有 child 的并集，child 之间不能断开。

    这是显式 `parent_id` 判据，比行区间包含关系更强：文档只有一个 child 时
    父块与子块区间完全相同，结构判据认不出来，只有这条能查。
    父块多声明一行 = 虚报原文；少声明一行 = 丢内容；中间断开 = 声称覆盖了没人覆盖的行。
    """
    chunks = call_producer(member, document(FIXTURES[fixture]), variant, max_chars=60)
    problems = explicit_parent_coverage_violations(chunks)
    assert problems == [], f"{label}/{fixture} 父块区间与子块并集不一致：{problems}"


@pytest.mark.parametrize(("label", "member", "variant"), PARENTED, ids=PARENTED_IDS)
@pytest.mark.parametrize("fixture", sorted(FIXTURES), ids=sorted(FIXTURES))
def test_no_child_is_left_without_a_parent(label, member, variant, fixture):
    """声明了父块的产出里，不能有漏挂 `parent_id` 的 child。

    漏挂的 child 命中后会以 child 身份返回：同一段正文既能以父块、又能以 child
    进入证据，去重就漏掉了它。
    """
    chunks, declared = call_producer_parts(
        member, document(FIXTURES[fixture]), variant, max_chars=60,
    )
    if not declared:
        pytest.skip("未测到：该生产者没有单独交出父块列表，无法区分父块与普通块")
    problems = declared_parent_violations(chunks, declared)
    assert problems == [], f"{label}/{fixture} 父子挂载不自洽：{problems}"


def test_single_child_document_gets_a_parent_equal_to_its_child():
    """边界：文档只有一个 child 时，父块区间**等于**子块区间。

    这是本设计最容易被结构判据漏掉的一格——两者区间一模一样。所以这里用生产者
    自己交出的父块列表来判断，而不是行区间包含关系。
    """
    if not PARENTED:
        pytest.skip("未测到：没有任何生产者产出父子结构")
    single = "只有一块内容，没有空行分隔。\n"
    seen_any = False
    for label, member, variant in PARENTED:
        chunks, declared = call_producer_parts(
            member, document(single), variant, max_chars=60,
        )
        if not declared:
            continue
        seen_any = True
        assert len(declared) == 1, f"{label} 单块文档产出了 {len(declared)} 个父块"
        parent = declared[0]
        children = explicit_parent_children(chunks).get(parent.chunk_id, [])
        assert children, f"{label} 单块文档的父块没有任何 child"
        assert (parent.start_line, parent.end_line) == \
               (children[0].start_line, children[-1].end_line), \
            f"{label} 单 child 的父块区间与 child 不一致"
        violations = text_violations(document(single), [parent])
        assert violations["text_conflict"] == [] and violations["text_whitespace_drift"] == [], \
            f"{label} 单 child 的父块正文不是原文切片：{violations}"
    if not seen_any:
        pytest.skip("未测到：生产者不交出父块列表")


def test_every_child_parent_reference_resolves():
    """每个 child 的 parent 引用都必须落在真实存在的父块上。

    悬空引用 = 命中子块后取不到父块，用户拿到半截证据。
    没有显式 parent 字段时报"未测到"，而不是假装通过。
    """
    if not parent_field_names():
        pytest.skip(
            "未测到：`Chunk` 上没有任何名字含 parent 的字段，"
            "无法检查子块到父块的引用是否可解析（父子结构可能尚未实现）"
        )
    checked = 0
    for label, member, variant in PRODUCERS:
        for fixture, text in FIXTURES.items():
            chunks = call_producer(member, document(text), variant, max_chars=60)
            dangling = unresolvable_parent_links(chunks)
            assert dangling == [], f"{label}/{fixture} 存在悬空的 parent 引用：{dangling}"
            problems = parent_self_reference_violations(chunks)
            assert problems == [], f"{label}/{fixture} parent 引用自指或多层：{problems}"
            assert explicit_parent_map(chunks) or True  # 有字段也可能全为 None
            checked += 1
    assert checked, "没有任何样本被检查到"
