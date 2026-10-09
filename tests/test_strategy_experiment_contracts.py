"""策略实验的CPU级输入输出合同；仅临时合成原文和确定性向量。"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content
from wenjin.index_registry import IndexTarget
from wenjin.releases import resolver as release_resolver
from wenjin.retrieval import strategy_experiment as strategy
from wenjin.retrieval.router import RouteConfig, RoutedIndex
from wenjin.retrieval.source_context import OGC_3DTILES_PDF_PROFILE, REVIEWED_3DTILES_TARGET
from wenjin.serving import CorpusSources, SourceModified, SourceRecord

pytestmark = pytest.mark.quick


def _source(
    tmp_path: Path,
    body: str,
    *,
    relative: str = "guide.html",
    manifest_parser: str | None = None,
    multiformat_input: bool = False,
):
    raw_root = tmp_path / "raw"
    path = raw_root / "lib" / "1" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    content = body.encode("utf-8")
    path.write_bytes(content)
    hashed = hash_content(content, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8")
    record = SourceRecord(
        relative=relative,
        raw_sha256=hashed.raw_sha256,
        size=len(content),
        retrieval_eligible=True,
        indexable=True,
        encoding="utf-8",
        canonical_sha256=hashed.canonical_sha256,
        normalization=TEXT_NEWLINE_LF_V1,
        parser=manifest_parser,
        multiformat_input=multiformat_input,
    )
    sources = CorpusSources(
        corpus_id="lib-1", raw_data_subdir="lib/1", raw_root=raw_root,
        records={relative: record},
    )
    routed = SimpleNamespace(target_id="target-1", sources=sources)
    return path, SimpleNamespace(indices=[routed])


def _child(chunk_id: str, start: int, end: int, text: str = "source fragment") -> dict:
    return {
        "chunk_id": chunk_id,
        "source_path": "raw_data/lib/1/guide.html",
        "target_id": "target-1",
        "library": "lib",
        "version": "1",
        "parser": "html-line-v1",
        "start_line": start,
        "end_line": end,
        "text": text,
    }


def test_parent_expansion_uses_html_definition_spans_and_preserves_provenance(tmp_path):
    body = "\n".join([
        "<dl>",
        '<dt id="alpha">--alpha</dt>',
        "<dd>",
        "Alpha does one thing.",
        "The full description remains visible.",
        "</dd>",
        '<dt id="beta">--beta</dt>',
        "<dd>",
        "Beta does another thing.",
        "</dd>",
        "</dl>",
        *[f"extra source line {number}" for number in range(30)],
    ])
    _, router = _source(tmp_path, body)
    children = [
        _child("alpha-a", 4, 4, "Alpha does one thing."),
        _child("alpha-b", 5, 5, "The full description remains visible."),
        _child("beta-a", 9, 9, "Beta does another thing."),
    ]

    parents, diagnostics = strategy._parent_documents(
        router, children, RouteConfig(top_k=3, rerank_pool=3, context_char_budget=500),
    )

    assert len(parents) == 2
    alpha, beta = parents
    assert alpha["parent_kind"] == "html_definition_list_dt_dd"
    assert (alpha["start_line"], alpha["end_line"]) == (2, 6)
    assert "--alpha" in alpha["text"] and "Alpha does one thing." in alpha["text"]
    assert "--beta" not in alpha["text"]
    assert len(alpha["child_provenance"]) == 2
    assert alpha["source_sha256"] == router.indices[0].sources.records["guide.html"].raw_sha256
    assert alpha["text_sha256"] == hashlib.sha256(alpha["text"].encode("utf-8")).hexdigest()
    assert beta["parent_kind"] == "html_definition_list_dt_dd"
    assert (beta["start_line"], beta["end_line"]) == (7, 10)
    assert "--beta" in beta["text"] and "--alpha" not in beta["text"]
    assert diagnostics["duplicate_source"] == 1


def test_parent_expansion_uses_manifest_parser_when_candidate_parser_is_missing(tmp_path):
    body = "\n".join([
        "<dt>--alpha</dt>", "<dd>", "Visible child text.", "More source detail.", "</dd>",
        *[f"padding {number} " + "x" * 30 for number in range(20)],
    ])
    _, router = _source(
        tmp_path, body, manifest_parser="html-line-v1", multiformat_input=True,
    )
    child = _child("alpha-missing-parser", 3, 3, "Visible child text.")
    child.pop("parser")

    parents, _ = strategy._parent_documents(
        router, [child], RouteConfig(top_k=1, rerank_pool=1, context_char_budget=500),
    )

    assert len(parents) == 1
    assert parents[0]["parent_kind"] == "html_definition_list_dt_dd"
    assert (parents[0]["start_line"], parents[0]["end_line"]) == (1, 5)
    assert "--alpha" in parents[0]["text"]
    assert "Visible child text." in parents[0]["text"]


def test_parent_chunk_crossing_sibling_definitions_falls_back_to_child(tmp_path):
    body = "\n".join([
        "<dl>", "<dt>--alpha</dt>", "<dd>", "alpha body", "</dd>",
        "<dt>--beta</dt>", "<dd>", "beta body", "</dd>", "</dl>",
        *[f"padding {number} " + "x" * 30 for number in range(30)],
    ])
    _, router = _source(tmp_path, body)
    cross = _child("cross", 4, 8, "child spans across definitions")

    parents, diagnostics = strategy._parent_documents(
        router, [cross], RouteConfig(top_k=1, rerank_pool=1, context_char_budget=500),
    )

    assert len(parents) == 1
    assert parents[0]["parent_kind"] == "child_fallback"
    assert parents[0]["text"] == cross["text"]
    assert diagnostics["fallback_child"] == 1


def test_parent_expansion_fails_closed_when_verified_source_hash_changes(tmp_path):
    path, router = _source(tmp_path, "<dt>--alpha</dt>\n<dd>\nalpha body\n</dd>\n")
    path.write_text("<dt>--alpha</dt>\n<dd>\nchanged body\n</dd>\n", encoding="utf-8")

    with pytest.raises(SourceModified):
        strategy._parent_documents(
            router, [_child("alpha", 3, 3)],
            RouteConfig(top_k=1, rerank_pool=1, context_char_budget=500),
        )


def test_parent_expansion_uses_markdown_heading_span_and_keeps_child_locator(tmp_path):
    body = "\n".join([
        "# Guide", "Intro.", "## Install", "Run the installer.", "Keep the full line.",
        "### Notes", "Use the local cache.", "## Troubleshooting", "Check the log.",
    ])
    _, router = _source(tmp_path, body, relative="guide.md")
    child = _child("install-child", 4, 5, "Run the installer.\nKeep the full line.")
    child["source_path"] = "raw_data/lib/1/guide.md"
    child["parser"] = "text-line-v1"

    parents, _ = strategy._parent_documents(
        router, [child], RouteConfig(top_k=1, rerank_pool=1, context_char_budget=500),
    )

    assert len(parents) == 1
    parent = parents[0]
    assert parent["parent_kind"] == "markdown_heading_section"
    assert (parent["start_line"], parent["end_line"]) == (3, 7)
    assert parent["text"].splitlines()[0] == "## Install"
    assert "## Troubleshooting" not in parent["text"]
    assert parent["child_locator"] == {
        "source_path": child["source_path"], "start_line": 4, "end_line": 5,
    }


def test_multi_query_allocates_remainder_keeps_original_query_and_reranks_under_shared_lock(monkeypatch):
    lock = threading.Lock()
    queries = ["original query", "subquery one", "subquery two"]
    seen_configs = []

    class Rewriter:
        def rewrite(self, query, *, corpus_hint, max_subqueries):
            assert query == "original query"
            assert max_subqueries == 3
            return SimpleNamespace(retrieval_queries=lambda *, include_subqueries: queries)

    class RawRouter:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def retrieve(self, query, *, config, **kwargs):
            seen_configs.append((query, config.candidate_k))
            candidate = {
                "chunk_id": f"chunk-{len(seen_configs)}", "target_id": "target-1",
                "library": "lib", "version": "1", "source_path": "guide.html",
                "start_line": 1, "end_line": 1, "text": query,
            }
            assert len([candidate]) <= config.candidate_k
            return {"dense_candidates": [candidate], "bm25_candidates": [candidate]}

    class Reranker:
        def score(self, query, docs):
            assert lock.locked()
            return [float(index) for index, _ in enumerate(docs)]

    monkeypatch.setattr(strategy, "MultiCorpusRouter", RawRouter)
    router = SimpleNamespace(
        qdrant_client=object(), embedder=object(),
        indices=[SimpleNamespace(target_id="target-1", source_context_cache=None)],
        reranker=Reranker(),
        dense_search=lambda *args, **kwargs: [], keyword_search=lambda *args, **kwargs: [],
        search_lock=lock,
    )
    config = RouteConfig(candidate_k=5, top_k=2, rerank_pool=3, context_char_budget=500)

    result, error, used_queries = strategy._multi_query(
        router, "original query", {"library": "lib"}, config, Rewriter(),
    )

    assert error is None
    assert used_queries == queries
    assert seen_configs == [("original query", 2), ("subquery one", 2), ("subquery two", 1)]
    assert sum(slots for _, slots in seen_configs) == config.candidate_k
    assert [item["query"] for item in result["query_results"]] == queries
    assert result["reranker_used"] is True


def test_mmr_uses_same_model_vectors_and_selects_a_less_redundant_candidate():
    lock = threading.Lock()
    vectors = {
        "near query": [0.99, 0.01],
        "similar query": [1.0, 0.0],
        "orthogonal": [0.0, 1.0],
    }

    class Embedder:
        def get_text_embedding_batch(self, texts):
            assert lock.locked()
            return [vectors[text.rsplit("\n", 1)[-1]] for text in texts]

        def get_query_embedding(self, query):
            assert lock.locked()
            assert query == "balanced query"
            return [1.0, 1.0]

    router = SimpleNamespace(embedder=Embedder(), search_lock=lock)
    candidates = [
        {"chunk_id": "near", "source_path": "doc", "start_line": 1, "end_line": 1, "text": "near query"},
        {"chunk_id": "similar", "source_path": "doc", "start_line": 2, "end_line": 2, "text": "similar query"},
        {"chunk_id": "orthogonal", "source_path": "doc", "start_line": 3, "end_line": 3, "text": "orthogonal"},
    ]

    selected, timing = strategy._mmr_select(
        router, "balanced query", candidates,
        RouteConfig(candidate_k=3, top_k=2, rerank_pool=3, context_char_budget=500),
    )

    assert [item["chunk_id"] for item in selected] == ["near", "orthogonal"]
    assert timing["vector_source"] == "same_configured_embedding_model_reencoded"
    assert [item["mmr_rank"] for item in selected] == [1, 2]


def test_mmr_rejects_non_finite_document_vectors():
    class BadEmbedder:
        def get_text_embedding_batch(self, texts):
            return [[float("nan"), 0.0] for _ in texts]

        def get_query_embedding(self, query):
            return [1.0, 0.0]

    candidate = {
        "chunk_id": "bad-vector", "source_path": "doc", "start_line": 1,
        "end_line": 1, "text": "text",
    }
    with pytest.raises(ValueError, match="NaN或Inf"):
        strategy._mmr_select(
            SimpleNamespace(embedder=BadEmbedder(), search_lock=threading.Lock()),
            "query", [candidate],
            RouteConfig(candidate_k=1, top_k=1, rerank_pool=1, context_char_budget=500),
        )


def test_pinned_evaluation_only_admits_requested_target_and_binds_profile_and_model(
    tmp_path, monkeypatch,
):
    reviewed = IndexTarget(
        target_id=REVIEWED_3DTILES_TARGET["target_id"],
        library=REVIEWED_3DTILES_TARGET["library"],
        version=REVIEWED_3DTILES_TARGET["version"],
        strategy=REVIEWED_3DTILES_TARGET["strategy"],
        dense_collection="synthetic_reviewed_dense",
        bm25_path="data/synthetic/reviewed.sqlite",
        manifest_path=REVIEWED_3DTILES_TARGET["manifest_path"],
        manifest_sha256=REVIEWED_3DTILES_TARGET["manifest_sha256"],
        canonical_corpus_sha256=REVIEWED_3DTILES_TARGET["canonical_corpus_sha256"],
        chunk_count=1,
        vector_dimension=2,
        embedding_model="synthetic-explicit-model-id",
        corpus_id="ogc-3dtiles-1.1",
    )
    unrelated = IndexTarget(
        target_id="unrelated-target", library="broken-library", version="9",
        strategy="synthetic", dense_collection="unrelated_dense",
        bm25_path="missing.sqlite", manifest_path="missing/manifest.json",
        manifest_sha256="a" * 64, chunk_count=1, vector_dimension=2,
        embedding_model="wrong-model", corpus_id="broken-9",
    )
    selected_index = RoutedIndex(reviewed, reviewed.dense_collection, Path(reviewed.bm25_path), {}, None)
    unrelated_index = RoutedIndex(unrelated, unrelated.dense_collection, Path(unrelated.bm25_path), {}, None)
    snapshot = SimpleNamespace(
        revision=17,
        router=SimpleNamespace(indices=[selected_index, unrelated_index]),
    )
    admitted = []
    fake_sources = object()
    asset_calls = []
    monkeypatch.setattr(
        release_resolver, "admit_index",
        lambda target, **kwargs: admitted.append(target.target_id) or {"target_id": target.target_id},
    )
    monkeypatch.setattr(
        release_resolver.CorpusSources, "from_manifest",
        lambda *args, **kwargs: fake_sources,
    )
    monkeypatch.setattr(release_resolver, "load_source_urls", lambda path: {})

    def fingerprint(indices, *, root, qdrant_client):
        asset_calls.append([index.target_id for index in indices])
        return {index.target_id: {"synthetic": True} for index in indices}

    monkeypatch.setattr(release_resolver, "evaluation_asset_fingerprints", fingerprint)
    request_rows = [{
        "case_id": "case-reviewed", "query": "synthetic query",
        "library": reviewed.library, "version": reviewed.version,
        "target_id": reviewed.target_id,
    }]

    pinned, identity = release_resolver.build_pinned_evaluation_router(
        resolver=SimpleNamespace(root=tmp_path),
        request_snapshot=snapshot,
        rows=request_rows,
        qdrant_client=object(),
        embedder=object(),
        reranker=None,
        search_lock=threading.Lock(),
        expected_embedding_model="synthetic-explicit-model-id",
        profiles=("baseline",),
    )

    assert admitted == [reviewed.target_id]
    assert asset_calls == [[reviewed.target_id]]
    assert [index.target_id for index in pinned.indices] == [reviewed.target_id]
    assert pinned.indices[0].source_context_cache.profile == OGC_3DTILES_PDF_PROFILE
    assert identity["embedding_model"] == reviewed.embedding_model
    assert identity["assets"].keys() == {reviewed.target_id}
    assert pinned.run_identity["snapshot_revision"] == snapshot.revision
    pinned.close()

    admitted.clear()
    asset_calls.clear()
    with pytest.raises(ValueError, match="Embedding模型与manager不一致"):
        release_resolver.build_pinned_evaluation_router(
            resolver=SimpleNamespace(root=tmp_path),
            request_snapshot=snapshot,
            rows=request_rows,
            qdrant_client=object(),
            embedder=object(),
            reranker=None,
            search_lock=threading.Lock(),
            expected_embedding_model="different-model-id",
            profiles=("baseline",),
        )
    assert admitted == [reviewed.target_id]
    assert asset_calls == []
