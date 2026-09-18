import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.retrieval.service import (
    RetrievalService,
    SearchConfig,
    load_source_urls,
    select_context,
)


def point(chunk_id: str, *, version: str = "3.6.5", text: str = "evidence"):
    return SimpleNamespace(
        score=0.8,
        payload={
            "chunk_id": chunk_id,
            "source_path": "include/osg/Node",
            "version": version,
            "start_line": 10,
            "end_line": 12,
            "strategy": "blocks",
            "text": text,
        },
    )


def service(tmp_path: Path, *, points: list, keyword_results: list | None = None):
    bm25_path = tmp_path / "index.sqlite"
    bm25_path.write_bytes(b"placeholder")
    keyword_calls = []

    def fake_dense(*args, **kwargs):
        return points

    def fake_keyword(*args, **kwargs):
        keyword_calls.append((args, kwargs))
        return keyword_results or []

    result = RetrievalService(
        qdrant_client=SimpleNamespace(close=lambda: None),
        embedder=object(),
        bm25_path=bm25_path,
        collection_name="test_collection",
        source_urls={"include/osg/Node": "https://example.invalid/node"},
        dense_search=fake_dense,
        keyword_search=fake_keyword,
    )
    return result, keyword_calls


def test_search_returns_traceable_evidence_without_generating_answer(tmp_path):
    retrieval, keyword_calls = service(tmp_path, points=[point("c1")])
    result = retrieval.search("节点怎样遍历？", config=SearchConfig(top_k=1))
    assert result["answer_status"] == "not_generated"
    assert result["evidence_status"] == "unverified"
    assert result["retrieval"]["bm25_returned"] == 0
    assert keyword_calls == []  # 纯中文query没有英文词项时，BM25分路安全返回空。
    assert result["evidence"][0]["source_url"] == "https://example.invalid/node"


def test_search_filters_wrong_version_and_validates_query(tmp_path):
    retrieval, _ = service(tmp_path, points=[point("wrong", version="9.9")])
    assert retrieval.search("Node API")["evidence"] == []
    with pytest.raises(ValueError, match="active索引"):
        retrieval.search("Node API", config=SearchConfig(version="4.0"))
    with pytest.raises(ValueError, match="query 长度"):
        retrieval.search("   ")


def test_context_budget_keeps_complete_chunks():
    candidates = [
        {"chunk_id": "too-large", "text": "x" * 600},
        {"chunk_id": "fits", "text": "y" * 500},
    ]
    selected = select_context(candidates, top_k=2, char_budget=500)
    assert [item["chunk_id"] for item in selected] == ["fits"]
    assert selected[0]["final_rank"] == 2


def test_source_urls_accept_indexable_and_retrieval_eligible(tmp_path):
    """OSG清单用indexable，五库新清单用retrieval_eligible；只认前者会丢source_url。"""
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({
        "raw_data_subdir": "proj/9.8.1",
        "files": [
            {"path": "osg", "indexable": True, "source_url": "urn:osg"},
            {"path": "proj", "retrieval_eligible": True, "source_url": "urn:proj"},
            {"path": "man/x.1", "retrieval_eligible": True, "source_url": "urn:man"},
            {"path": "archive", "retrieval_eligible": False, "source_url": "urn:archive"},
        ],
    }), encoding="utf-8")
    assert load_source_urls(manifest) == {
        "osg": "urn:osg",
        "proj": "urn:proj",
        "man/x.1": "urn:man",
        # multiformat读取的chunk会带raw_data前缀，两种键都要能查到。
        "raw_data/proj/9.8.1/osg": "urn:osg",
        "raw_data/proj/9.8.1/proj": "urn:proj",
        "raw_data/proj/9.8.1/man/x.1": "urn:man",
    }


def test_search_config_rejects_unbounded_inputs():
    with pytest.raises(ValueError, match="candidate_k"):
        SearchConfig(candidate_k=100).validate()
    with pytest.raises(ValueError, match="version"):
        SearchConfig(version="").validate()
