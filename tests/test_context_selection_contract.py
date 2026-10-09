"""独立的context selector契约断言；不锁定预算选择算法的具体策略。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from wenjin.retrieval.service import select_context

LOCATOR_FIELDS = (
    "target_id",
    "source_path",
    "start_line",
    "end_line",
    "page_number",
)


def assert_context_selection_contract(
    ranked: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    *,
    char_budget: int = 8000,
    max_items: int = 5,
) -> None:
    """核验选择结果是输入证据的原文子集，不规定如何排序或取舍。"""
    assert isinstance(selected, list)
    assert len(selected) <= max_items
    assert sum(len(item["text"]) for item in selected) <= char_budget

    input_by_id = {item["chunk_id"]: item for item in ranked}
    assert len(input_by_id) == len(ranked), "输入chunk_id必须唯一"
    selected_ids = [item["chunk_id"] for item in selected]
    assert len(set(selected_ids)) == len(selected_ids), "输出不能重复同一chunk"
    assert all(chunk_id in input_by_id for chunk_id in selected_ids), "不能合成新证据"

    input_positions = {item["chunk_id"]: rank for rank, item in enumerate(ranked)}
    assert [input_positions[chunk_id] for chunk_id in selected_ids] == sorted(
        input_positions[chunk_id] for chunk_id in selected_ids
    ), "所选证据须保留原重排顺序"
    for result in selected:
        original = input_by_id[result["chunk_id"]]
        assert result["text"] == original["text"], "正文必须逐字保留，不能截断或拼接"
        for field in LOCATOR_FIELDS:
            assert result.get(field) == original.get(field), f"来源定位字段被改写：{field}"


def _sample_candidates() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": "a",
            "target_id": "docs-a",
            "source_path": "raw/a.html",
            "start_line": 10,
            "end_line": 10,
            "page_number": None,
            "text": "same source text",
        },
        {
            "chunk_id": "b",
            "target_id": "docs-b",
            "source_path": "raw/b.html",
            "start_line": 20,
            "end_line": 20,
            "page_number": None,
            "text": "same source text",
        },
    ]


def test_production_selector_is_verbatim_gold_blind_and_keeps_source_identity() -> None:
    ranked = _sample_candidates()
    original = deepcopy(ranked)
    selected = select_context(ranked, top_k=5, char_budget=8000)

    assert_context_selection_contract(ranked, selected)
    assert [item["chunk_id"] for item in selected] == ["a", "b"]
    assert len({item["source_path"] for item in selected}) == 2
    assert ranked == original, "selector不能修改输入候选"

    annotated = deepcopy(ranked)
    annotated[0]["_eval_gold_target"] = True
    annotated[1]["_eval_gold_target"] = False
    annotated_selection = select_context(annotated, top_k=5, char_budget=8000)
    assert [item["chunk_id"] for item in annotated_selection] == [
        item["chunk_id"] for item in selected
    ], "增加/变更评测gold标签不能改变selector输出"
    assert_context_selection_contract(annotated, annotated_selection)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rows: rows[0].update(text="shortened"), "正文必须逐字保留"),
        (lambda rows: rows[0].update(start_line=99), "来源定位字段被改写"),
        (lambda rows: rows[0].update(source_path="raw/other.html"), "来源定位字段被改写"),
    ],
)
def test_reference_contract_detects_text_or_locator_mutation(mutate, message: str) -> None:
    ranked = _sample_candidates()
    selected = deepcopy(ranked[:1])
    mutate(selected)

    with pytest.raises(AssertionError, match=message):
        assert_context_selection_contract(ranked, selected)


def test_reference_contract_rejects_budget_overflow_and_more_than_five_items() -> None:
    ranked = [
        {
            "chunk_id": str(index),
            "target_id": "docs",
            "source_path": f"raw/{index}.html",
            "start_line": index,
            "end_line": index,
            "page_number": None,
            "text": "x" * 1601,
        }
        for index in range(6)
    ]

    with pytest.raises(AssertionError):
        assert_context_selection_contract(ranked, ranked[:6])
    with pytest.raises(AssertionError):
        assert_context_selection_contract(ranked, ranked[:5], char_budget=8000)

