"""父子切片：结构、上限、以及"父块正文 == 原文切片"这条硬契约。

这些断言与 `tests/parent_child_probe.py` 那套独立判据分开写：两处都错成同一个样子
才会双双漏过，共用一份判定就做不到这一点。
"""

import pytest

from wenjin.ingestion.chunking import (
    PARENTED_STRATEGY,
    block_chunks,
    block_chunks_parented,
)

CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


def document(text: str, **extra) -> dict:
    return {"source_path": "include/osg/Test", "version": "3.6.5", "text": text, **extra}


def lines_text(count: int, *, width: int = 40) -> str:
    return "".join(f"line-{index:03d}-{'x' * width}\n" for index in range(count))


def source_lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def slice_of(text: str, start_line: int, end_line: int) -> str:
    return "".join(source_lines(text)[start_line - 1 : end_line])


def parented(text: str, **kwargs) -> tuple[list, list, list[tuple]]:
    """跑一次父子切片，返回 `(children, parents, [(父块, 子块列表)])`。"""
    children, parents = block_chunks_parented(document(text), **kwargs)
    by_id = {parent.chunk_id: parent for parent in parents}
    grouped: dict[str, list] = {}
    for child in children:
        assert child.parent_id in by_id, f"child 的父块取不到：{child.parent_id}"
        grouped.setdefault(child.parent_id, []).append(child)
    structure = [
        (by_id[parent_id], sorted(members, key=lambda item: (item.start_line, item.end_line)))
        for parent_id, members in grouped.items()
    ]
    return children, parents, structure


def assert_parent_contract(text: str, children, parents, structure) -> None:
    """父块与 child 的全部结构性不变量；任何一条不成立都说明父块不能作为证据。"""
    assert children and parents
    assert {child.parent_id for child in children} == {parent.chunk_id for parent in parents}
    assert all(parent.parent_id is None for parent in parents)
    assert all(chunk.strategy == PARENTED_STRATEGY for chunk in children + parents)
    for parent, members in structure:
        assert parent.text == slice_of(text, parent.start_line, parent.end_line), (
            f"父块正文不是它声明区间 {parent.start_line}-{parent.end_line} 的原文切片"
        )
        assert members[0].start_line == parent.start_line
        assert members[-1].end_line == parent.end_line
        # 子块合起来必须逐字盖满父块正文：父块声明的区间里不能有没人覆盖的内容。
        assert "".join(child.text for child in members) == parent.text
        for previous, current in zip(members, members[1:]):
            # 允许重叠（单行硬切的片段共享同一行），但不允许断开。
            assert current.start_line <= previous.end_line + 1, (
                f"父块 {parent.chunk_id} 的子块之间有洞："
                f"{previous.end_line + 1}-{current.start_line - 1}"
            )


def test_parents_merge_adjacent_children_within_max_children():
    text = lines_text(6, width=40)
    children, parents, structure = parented(
        text, max_chars=100, max_children=2, max_parent_chars=10_000
    )
    assert len(children) == 3, "夹具应当产生3个child，否则这条测不到合并"
    assert [len(members) for _, members in structure] == [2, 1]
    assert [(parent.start_line, parent.end_line) for parent in parents] == [(1, 4), (5, 6)]
    assert_parent_contract(text, children, parents, structure)


def test_max_parent_chars_binds_before_max_children():
    """字符上限一样要生效，否则父块会超出调用方的上下文预算。"""
    text = lines_text(6, width=40)
    _, roomy, _ = parented(text, max_chars=100, max_children=10, max_parent_chars=10_000)
    children, parents, structure = parented(
        text, max_chars=100, max_children=10, max_parent_chars=200
    )
    assert len(roomy) == 1, "上限放宽时应当只有一个父块，否则这条对照没有意义"
    assert len(parents) == 2, "字符上限没有生效"
    assert [len(members) for _, members in structure] == [2, 1]
    assert_parent_contract(text, children, parents, structure)


def test_children_separated_by_a_gap_start_a_new_parent():
    """连续空行里有一行不属于任何child：硬并进同一个父块就会声明没盖到的区间。"""
    text = lines_text(4, width=40) + "\n\n" + lines_text(4, width=40)
    children, parents, structure = parented(
        text, max_chars=100, max_children=10, max_parent_chars=10_000
    )
    assert any(
        current.start_line > previous.end_line + 1
        for previous, current in zip(children, children[1:])
    ), "夹具里没有出现child之间的洞，这条测不到"
    assert len(parents) > 1, "断开的child被并进了同一个父块"
    assert_parent_contract(text, children, parents, structure)


def test_pieces_of_one_long_line_stay_in_the_same_parent():
    """单行硬切的片段共享行号：拆到两个父块，两个父块会声明同一段区间。"""
    line = CHARS * 4 + "\n"
    children, parents, structure = parented(
        line, max_chars=40, max_children=2, max_parent_chars=40
    )
    assert len(children) > 2, "夹具应当产生多个硬切片段"
    assert len(parents) == 1, "同一行的片段被拆到了多个父块"
    assert parents[0].text == line
    assert (parents[0].start_line, parents[0].end_line) == (1, 1)
    assert_parent_contract(line, children, parents, structure)


def test_parented_children_are_exactly_blocks_split_pieces():
    """child 必须还是 blocks-split 的片段：换了切法就等于换了命中率。"""
    text = lines_text(6, width=40)
    children, _, _ = parented(text, max_chars=100, max_children=2, max_parent_chars=10_000)
    plain = block_chunks(document(text), max_chars=100, split_oversized=True)
    assert [child.chunk_id for child in children] == [chunk.chunk_id for chunk in plain]
    assert [child.text for child in children] == [chunk.text for chunk in plain]
    assert [(child.start_line, child.end_line) for child in children] == [
        (chunk.start_line, chunk.end_line) for chunk in plain
    ]


def test_every_non_blank_line_is_covered_by_a_child():
    """child 一个都不能丢行：丢了就是检索永远命中不到的内容（父块不参与检索）。"""
    text = lines_text(3, width=40) + "\n\n\n" + lines_text(3, width=40) + "\n"
    children, _, _ = parented(text, max_chars=100, max_children=2, max_parent_chars=200)
    covered: set[int] = set()
    for child in children:
        covered.update(range(child.start_line, child.end_line + 1))
    wanted = {number for number, line in enumerate(source_lines(text), start=1) if line.strip()}
    assert wanted - covered == set()


def test_parent_ids_do_not_collide_with_child_ids():
    text = lines_text(6, width=40)
    children, parents, _ = parented(text, max_chars=100)
    ids = [chunk.chunk_id for chunk in children + parents]
    assert len(set(ids)) == len(ids)


def test_documents_never_merge_into_one_parent():
    """每页（PDF）是独立的解析单元，跨页合并会得到一个横跨两页的区间。"""
    first = {**document(lines_text(4, width=40)), "page_number": 1}
    second = {**document(lines_text(4, width=40)), "page_number": 2}
    chunks = [
        block_chunks_parented(first, max_chars=100, max_children=10),
        block_chunks_parented(second, max_chars=100, max_children=10),
    ]
    children = chunks[0][0] + chunks[1][0]
    parents = chunks[0][1] + chunks[1][1]
    assert len(parents) == 2
    assert {parent.page_number for parent in parents} == {1, 2}
    assert {child.page_number for child in children if child.parent_id == parents[0].chunk_id} == {1}
    assert {child.page_number for child in children if child.parent_id == parents[1].chunk_id} == {2}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_chars": 0}, "max_chars"),
        ({"max_children": 0}, "max_children"),
        ({"max_parent_chars": 0}, "max_parent_chars"),
    ],
)
def test_invalid_limits_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        block_chunks_parented(document(lines_text(2)), **kwargs)
