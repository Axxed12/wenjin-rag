import pytest

from wenjin.retrieval.fusion import interleave_unique, rrf_fuse


def item(chunk_id: str, score: float) -> dict:
    return {"chunk_id": chunk_id, "score": score, "text": chunk_id}


def test_rrf_rewards_candidate_ranked_by_both_retrievers():
    result = rrf_fuse(
        {
            "dense": [item("only-dense", 0.99), item("both", 0.5)],
            "bm25": [item("both", 100.0), item("only-bm25", 90.0)],
        },
        k=60,
    )
    assert result[0]["chunk_id"] == "both"
    assert result[0]["ranks"] == {"dense": 2, "bm25": 1}
    assert result[0]["original_scores"] == {"dense": 0.5, "bm25": 100.0}


def test_rrf_rejects_negative_k():
    with pytest.raises(ValueError):
        rrf_fuse({}, k=-1)


def test_interleave_unique_preserves_subquery_coverage():
    merged = interleave_unique(
        {
            "inheritance": [
                {"chunk_id": "shared", "text": "same"},
                {"chunk_id": "base", "text": "base class"},
            ],
            "method": [
                {"chunk_id": "method", "text": "addChild"},
                {"chunk_id": "shared", "text": "same"},
            ],
        }
    )
    assert [item["chunk_id"] for item in merged] == ["shared", "method", "base"]
    assert merged[0]["rewrite_routes"] == {"inheritance": 1, "method": 2}
