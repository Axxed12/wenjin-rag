import pytest

from wenjin.retrieval.rerank import (
    DEFAULT_INSTRUCTION,
    STRICT_SOURCE_INSTRUCTION,
    candidate_document,
    rank_candidates,
    tokenize_pairs,
)


def candidate(chunk_id: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "source_path": "include/osg/Geode",
        "start_line": 10,
        "end_line": 20,
        "text": f"body {chunk_id}",
    }


def test_candidate_document_keeps_source_and_text():
    text = candidate_document(candidate("a"))
    assert "include/osg/Geode" in text
    assert "Lines: 10-20" in text
    assert "body a" in text


def test_rank_candidates_is_descending_stable_and_non_mutating():
    candidates = [candidate("a"), candidate("b"), candidate("c")]
    ranked = rank_candidates(candidates, [0.2, 0.9, 0.9])
    assert [item["chunk_id"] for item in ranked] == ["b", "c", "a"]
    assert ranked[0]["pre_rerank_rank"] == 2
    assert "rerank_score" not in candidates[0]


def test_rank_candidates_rejects_length_mismatch():
    with pytest.raises(ValueError, match="数量不一致"):
        rank_candidates([candidate("a")], [])


def test_strict_instruction_requires_exact_identifiers():
    assert "exact identifiers" in STRICT_SOURCE_INSTRUCTION
    assert "similar class or method" in STRICT_SOURCE_INSTRUCTION
    assert STRICT_SOURCE_INSTRUCTION != DEFAULT_INSTRUCTION


def test_first_tokenization_defers_attention_mask_until_suffix_is_added():
    captured = {}

    class FakeTokenizer:
        def __call__(self, pairs, **kwargs):
            captured.update(kwargs)
            return {"input_ids": [[1, 2]] for _ in pairs}

    encoded = tokenize_pairs(FakeTokenizer(), ["query-document"], max_length=100)
    assert encoded == {"input_ids": [[1, 2]]}
    assert captured["return_attention_mask"] is False
