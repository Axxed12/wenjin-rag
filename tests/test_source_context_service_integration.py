"""生产ranker集成合同：来源上下文只改模型输入，绝不改返回证据。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content
from wenjin.index_registry import IndexTarget
from wenjin.indexing.bm25 import bm25_query, build_bm25_index
from wenjin.ingestion.chunking import Chunk
from wenjin.mcp_server import RuntimeIdentity, build_mcp_server
from wenjin.retrieval import router as router_module
from wenjin.retrieval.rerank import candidate_document
from wenjin.retrieval.router import (
    DEFAULT_MULTI_CORPUS_ROUTE,
    MultiCorpusRouter,
    RouteConfig,
    RoutedIndex,
)
from wenjin.retrieval.service import RetrievalService, SearchConfig
from wenjin.retrieval.source_context import (
    OGC_3DTILES_PDF_PROFILE,
    REVIEWED_3DTILES_TARGET,
    SourceContextIntegrityError,
    VerifiedSourceContextCache,
    reviewed_profile_for_target,
)
from wenjin.serving import CorpusSources, SourceRecord

CORPUS_ID = "ogc-3dtiles-1.1"
SOURCE_RELATIVE = "guide.html"
SOURCE_PATH = f"raw_data/{CORPUS_ID}/{SOURCE_RELATIVE}"
TARGET_ID = REVIEWED_3DTILES_TARGET["target_id"]


class ScoreSpy:
    def __init__(self, scores: list[float] | None = None):
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.scores = scores

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.queries.append(query)
        self.documents = list(documents)
        return self.scores or [1.0 - index / 100 for index in range(len(documents))]


class DummyClient:
    def close(self):
        pass


def _reviewed_target() -> IndexTarget:
    return IndexTarget(
        target_id=REVIEWED_3DTILES_TARGET["target_id"],
        library=REVIEWED_3DTILES_TARGET["library"],
        version=REVIEWED_3DTILES_TARGET["version"],
        strategy="blocks-split",
        dense_collection="test_3dtiles_dense",
        bm25_path="data/indexes/test.sqlite",
        manifest_path=REVIEWED_3DTILES_TARGET["manifest_path"],
        manifest_sha256=REVIEWED_3DTILES_TARGET["manifest_sha256"],
        canonical_corpus_sha256=REVIEWED_3DTILES_TARGET["canonical_corpus_sha256"],
        chunk_count=1,
        vector_dimension=8,
        embedding_model="fake",
        corpus_id=CORPUS_ID,
    )


def _verified_fixture(tmp_path: Path, *, body: str = "A source fact.\n"):
    """Create a temporary manifest-backed source and a candidate on its final line."""
    raw_root = tmp_path / "raw_data"
    source_file = raw_root / CORPUS_ID / SOURCE_RELATIVE
    source_file.parent.mkdir(parents=True, exist_ok=True)
    raw = (
        "<h1>Annex A</h1>\r\n"
        "<h2>A.9 Class Property</h2>\r\n"
        + body.replace("\n", "\r\n")
    ).encode("utf-8")
    source_file.write_bytes(raw)
    hashed = hash_content(raw, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8")
    record = SourceRecord(
        relative=SOURCE_RELATIVE,
        raw_sha256=hashed.raw_sha256,
        size=len(raw),
        retrieval_eligible=True,
        indexable=True,
        encoding="utf-8",
        canonical_sha256=hashed.canonical_sha256,
        normalization=TEXT_NEWLINE_LF_V1,
    )
    sources = CorpusSources(
        corpus_id=CORPUS_ID,
        raw_data_subdir=CORPUS_ID,
        raw_root=raw_root,
        records={SOURCE_RELATIVE: record},
    )
    candidate = {
        "chunk_id": f"1.1:{SOURCE_PATH}:blocks-split:0000",
        "source_path": SOURCE_PATH,
        "version": "1.1",
        "start_line": 3,
        "end_line": 3,
        "page_number": None,
        "text": body,
        "content_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "source_raw_sha256": hashed.raw_sha256,
        "source_canonical_sha256": hashed.canonical_sha256,
        "library": "ogc-3dtiles",
        "target_id": TARGET_ID,
    }
    cache = VerifiedSourceContextCache(sources, profile=OGC_3DTILES_PDF_PROFILE)
    return source_file, sources, candidate, cache, record


def _router(index: RoutedIndex, candidate: dict, reranker: ScoreSpy) -> MultiCorpusRouter:
    point = SimpleNamespace(score=0.9, payload=dict(candidate))

    def dense_search(client, vector, collection, *, top_k):
        return [point]

    return MultiCorpusRouter(
        qdrant_client=DummyClient(),
        embedder=SimpleNamespace(get_query_embedding=lambda query: [0.0] * 8),
        indices=[index],
        reranker=reranker,
        dense_search=dense_search,
        keyword_search=lambda path, query, *, top_k: [],
    )


def _index(sources: CorpusSources, *, cache=None, target: IndexTarget | None = None) -> RoutedIndex:
    target = target or _reviewed_target()
    return RoutedIndex(
        target=target,
        collection=target.dense_collection,
        bm25_path=Path("unused.sqlite"),
        source_urls={SOURCE_PATH: "https://example.test/guide.html"},
        sources=sources,
        source_context_cache=cache,
    )


def test_router_scores_contextual_document_but_returns_original_text_and_locator(tmp_path):
    _, sources, candidate, cache, _ = _verified_fixture(tmp_path)
    spy = ScoreSpy()
    router = _router(_index(sources, cache=cache), candidate, spy)

    result = router.search(
        "find source fact",
        target_id=TARGET_ID,
        config=RouteConfig(candidate_k=5, top_k=1, rerank_pool=5),
    )

    assert len(spy.documents) == 1
    assert spy.documents[0].startswith("HTML heading path: Annex A > A.9 Class Property\n")
    assert spy.documents[0].endswith(candidate["text"])
    assert candidate["text"] not in spy.documents[0].split("Source:", 1)[0]
    evidence = result["evidence"][0]
    assert evidence["text"] == candidate["text"]
    assert (evidence["source_path"], evidence["start_line"], evidence["end_line"]) == (
        SOURCE_PATH, 3, 3
    )
    assert result["retrieval"]["source_context_diagnostics"] == {"context_applied:mapped": 1}


def test_real_bm25_query_carries_source_hashes_through_router_to_context_cache(tmp_path):
    _, sources, candidate, cache, _ = _verified_fixture(
        tmp_path, body="Application-specific data is recorded here.\n"
    )
    chunk = Chunk(
        candidate["chunk_id"], candidate["source_path"], candidate["version"],
        candidate["start_line"], candidate["end_line"], candidate["text"],
        "blocks-split", content_sha256=candidate["content_sha256"],
        source_raw_sha256=candidate["source_raw_sha256"],
        source_canonical_sha256=candidate["source_canonical_sha256"],
        normalization=TEXT_NEWLINE_LF_V1,
    )
    bm25_path = tmp_path / "bm25.sqlite"
    build_bm25_index(bm25_path, [chunk])
    spy = ScoreSpy()
    index = replace(_index(sources, cache=cache), bm25_path=bm25_path)
    router = MultiCorpusRouter(
        qdrant_client=DummyClient(),
        embedder=SimpleNamespace(get_query_embedding=lambda query: [0.0] * 8),
        indices=[index],
        reranker=spy,
        dense_search=lambda *args, **kwargs: [],
        keyword_search=bm25_query,
    )
    bm25_candidate = bm25_query(bm25_path, "Application-specific data")[0]

    result = router.search(
        "Application-specific data", target_id=TARGET_ID,
        config=RouteConfig(candidate_k=5, top_k=1, rerank_pool=5),
    )

    assert bm25_candidate["content_sha256"] == candidate["content_sha256"]
    assert bm25_candidate["source_raw_sha256"] == candidate["source_raw_sha256"]
    assert bm25_candidate["source_canonical_sha256"] == candidate["source_canonical_sha256"]
    assert len(spy.documents) == 1
    assert spy.documents[0].startswith("HTML heading path: Annex A > A.9 Class Property\n")
    assert result["retrieval"]["source_context_diagnostics"] == {"context_applied:mapped": 1}
    evidence = result["evidence"][0]
    assert evidence["text"] == candidate["text"]
    assert (evidence["source_path"], evidence["start_line"], evidence["end_line"]) == (
        SOURCE_PATH, 3, 3
    )


def test_bm25_old_schema_returns_null_source_hashes_and_cache_fails_closed(tmp_path):
    _, sources, candidate, cache, _ = _verified_fixture(tmp_path)
    legacy_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy_path) as database:
        database.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5("
            "chunk_id UNINDEXED, source_path UNINDEXED, version UNINDEXED, "
            "start_line UNINDEXED, end_line UNINDEXED, strategy UNINDEXED, "
            "retrieval_text, raw_text UNINDEXED)"
        )
        database.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate["chunk_id"], SOURCE_PATH, "1.1", 3, 3, "blocks-split",
             "Application-specific data", candidate["text"]),
        )

    legacy_candidate = bm25_query(legacy_path, "Application-specific data")[0]

    assert legacy_candidate["content_sha256"] is None
    assert legacy_candidate["source_raw_sha256"] is None
    assert legacy_candidate["source_canonical_sha256"] is None
    legacy_candidate.update(library="ogc-3dtiles", target_id=TARGET_ID)
    with pytest.raises(SourceContextIntegrityError, match="双Hash身份不匹配"):
        cache.document_for_candidate(legacy_candidate)


def test_route_factory_registers_cache_only_for_exact_reviewed_target(tmp_path, monkeypatch):
    route = json.loads(Path(DEFAULT_MULTI_CORPUS_ROUTE).read_text(encoding="utf-8"))
    route_path = tmp_path / "route.json"
    route_path.write_text(json.dumps(route), encoding="utf-8")
    fake_sources = SimpleNamespace()
    monkeypatch.setattr(router_module, "admit_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        router_module.CorpusSources, "from_manifest",
        lambda *args, **kwargs: fake_sources,
    )
    monkeypatch.setattr(router_module, "load_source_urls", lambda *args, **kwargs: {})

    router = MultiCorpusRouter.from_route_file(
        route_path, root=Path.cwd(), qdrant_client=DummyClient(), embedder=object(),
    )

    cache_targets = [index.target.target_id for index in router.indices
                     if index.source_context_cache is not None]
    assert cache_targets == [TARGET_ID]
    assert all(index.sources is fake_sources for index in router.indices)


def test_cli_defaults_keep_reviewed_route_and_production_budget():
    from scripts.search_routed import ROOT, build_parser

    args = build_parser().parse_args(["a query"])

    assert args.route == ROOT / DEFAULT_MULTI_CORPUS_ROUTE
    assert args.candidate_k == 10
    assert args.top_k == 5
    assert args.context_char_budget == 8000


def test_unreviewed_router_target_uses_baseline_document_and_reports_status(tmp_path):
    _, sources, candidate, cache, _ = _verified_fixture(tmp_path)
    unreviewed = replace(_reviewed_target(), manifest_sha256="a" * 64)
    assert reviewed_profile_for_target(unreviewed) is None
    spy = ScoreSpy()
    router = _router(_index(sources, cache=None, target=unreviewed), candidate, spy)

    result = router.search(
        "query", target_id=unreviewed.target_id,
        config=RouteConfig(candidate_k=5, top_k=1, rerank_pool=5),
    )

    assert spy.documents == [candidate_document({**candidate, "target_id": unreviewed.target_id})]
    assert result["retrieval"]["source_context_diagnostics"] == {
        "unregistered_target_profile": 1
    }
    assert result["evidence"][0]["text"] == candidate["text"]
    assert cache._structures == {}


def test_single_corpus_service_keeps_baseline_frame_without_registered_cache(tmp_path):
    candidate = {
        "chunk_id": "osg:doc:0000", "source_path": "include/osg/Node",
        "version": "3.6.5", "start_line": 1, "end_line": 1,
        "text": "struct Node {};\n", "content_sha256": "unused",
    }
    spy = ScoreSpy()
    bm25 = tmp_path / "bm25.sqlite"
    bm25.touch()
    service = RetrievalService(
        qdrant_client=DummyClient(), embedder=object(), bm25_path=bm25,
        source_urls={}, collection_name="osg", supported_version="3.6.5",
        reranker=spy,
        dense_search=lambda *args, **kwargs: [SimpleNamespace(score=0.8, payload=candidate)],
        keyword_search=lambda *args, **kwargs: [],
    )

    result = service.search("node", config=SearchConfig(candidate_k=5, top_k=1))

    assert spy.documents == [candidate_document(candidate)]
    assert result["retrieval"]["source_context_diagnostics"] == {
        "unregistered_target_profile": 1
    }
    assert result["evidence"][0]["text"] == candidate["text"]


def test_verified_cache_marks_same_line_h1_prose_h2_ambiguous(tmp_path):
    body = "<h1>Annex A</h1> prose between headings <h2>A.9 Class Property</h2>target body.\n"
    source_file, sources, candidate, cache, _ = _verified_fixture(tmp_path, body=body)

    document, status = cache.document_for_candidate(candidate)

    assert status == "ambiguous_source_structure"
    assert "HTML heading path:" not in document
    assert document.endswith(candidate_document(candidate))
    assert candidate["text"] == body
    assert source_file.exists()


def test_full_json_guard_cannot_bypass_candidate_or_source_hash_checks(tmp_path):
    _, sources, candidate, cache, record = _verified_fixture(
        tmp_path,
        body=(
            '{"$schema":"https://json-schema.org/draft/2020-12/schema",'
            '"$id":"urn:test", "required":["values"]}\n'
        ),
    )
    assert cache.document_for_candidate(candidate)[1] == "complete_self_describing_json_schema"

    bad_content_hash = {**candidate, "content_sha256": "0" * 64}
    with pytest.raises(SourceContextIntegrityError, match="正文Hash不匹配"):
        cache.document_for_candidate(bad_content_hash)

    bad_raw_identity = {**candidate, "source_raw_sha256": "f" * 64}
    with pytest.raises(SourceContextIntegrityError, match="双Hash身份不匹配"):
        cache.document_for_candidate(bad_raw_identity)

    bad_canonical_record = replace(record, canonical_sha256="e" * 64)
    sources.records[SOURCE_RELATIVE] = bad_canonical_record
    with pytest.raises(SourceContextIntegrityError, match="双Hash身份不匹配"):
        cache.document_for_candidate(candidate)


def test_verified_cache_hit_uses_immutable_snapshot_even_if_mtime_is_preserved(tmp_path):
    source_file, _, candidate, cache, _ = _verified_fixture(tmp_path)
    first_document, first_status = cache.document_for_candidate(candidate)
    original_stat = source_file.stat()
    source_file.write_bytes(source_file.read_bytes().replace(b"A.9", b"A.8"))
    os.utime(source_file, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    second_document, second_status = cache.document_for_candidate(candidate)
    assert second_document == first_document
    assert second_status == first_status == "context_applied:mapped"

    # 新cache没有已验证快照，不能接受mtime未变但字节已修改的来源。
    cold_cache = VerifiedSourceContextCache(
        cache.sources, profile=OGC_3DTILES_PDF_PROFILE
    )
    with pytest.raises(SourceContextIntegrityError, match="原文Hash校验失败"):
        cold_cache.document_for_candidate(candidate)


def test_changed_source_hash_identity_invalidates_cached_structure(tmp_path, monkeypatch):
    from wenjin.retrieval import source_context

    source_file, sources, candidate, cache, record = _verified_fixture(tmp_path)
    parser_calls = 0
    original_parser = source_context.extract_html_structure

    def counted_parser(content):
        nonlocal parser_calls
        parser_calls += 1
        return original_parser(content)

    monkeypatch.setattr(source_context, "extract_html_structure", counted_parser)
    first_document, _ = cache.document_for_candidate(candidate)
    assert parser_calls == 1

    changed_raw = source_file.read_bytes().replace(b"A.9", b"A.8")
    source_file.write_bytes(changed_raw)
    changed_hash = hash_content(
        changed_raw, normalization=record.normalization, encoding=record.encoding
    )
    sources.records[SOURCE_RELATIVE] = replace(
        record,
        raw_sha256=changed_hash.raw_sha256,
        size=len(changed_raw),
        canonical_sha256=changed_hash.canonical_sha256,
        encoding="UTF8",  # alias encoding keeps the bytes valid but changes cache identity
    )
    changed_candidate = {
        **candidate,
        "source_raw_sha256": changed_hash.raw_sha256,
        "source_canonical_sha256": changed_hash.canonical_sha256,
    }

    second_document, second_status = cache.document_for_candidate(changed_candidate)
    assert parser_calls == 2
    assert second_status == "context_applied:mapped"
    assert "A.8 Class Property" in second_document
    assert "A.9 Class Property" not in second_document
    assert first_document != second_document


def test_mcp_acl_denial_happens_before_context_loader_and_ranker(tmp_path):
    _, sources, candidate, cache, _ = _verified_fixture(tmp_path)
    cache_calls = 0
    original_document = cache.document_for_candidate

    def counted_document(item):
        nonlocal cache_calls
        cache_calls += 1
        return original_document(item)

    cache.document_for_candidate = counted_document
    source_reads = 0
    original_read = sources.read_verified

    def counted_read(relative, **kwargs):
        nonlocal source_reads
        source_reads += 1
        return original_read(relative, **kwargs)

    sources.read_verified = counted_read
    spy = ScoreSpy()
    router = _router(_index(sources, cache=cache), candidate, spy)
    access = AccessRegistry(None, {
        "some-other-corpus": AccessPolicy("some-other-corpus", "public", ("*",))
    })
    server = build_mcp_server(
        SimpleNamespace(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=access,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=RuntimeIdentity("osg-3.6.5", "test", "osg-target"),
        corpus_router=router,
    )

    async def call():
        async with Client(server) as client:
            result = await client.call_tool("search_docs", {
                "query": "query", "target_id": TARGET_ID,
            })
            return result.structured_content

    payload = asyncio.run(call())

    assert payload["error"] == "forbidden"
    assert payload["evidence"] == []
    assert cache_calls == source_reads == len(spy.documents) == 0


def test_mcp_masks_verified_source_integrity_failure_without_leaking_hashes(tmp_path):
    _, sources, candidate, cache, _ = _verified_fixture(tmp_path)
    bad = {**candidate, "content_sha256": "0" * 64}
    spy = ScoreSpy()
    router = _router(_index(sources, cache=cache), bad, spy)
    access = AccessRegistry(None, {
        CORPUS_ID: AccessPolicy(CORPUS_ID, "public", ("*",))
    })
    server = build_mcp_server(
        SimpleNamespace(), audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=access, raw_root=tmp_path / "raw_data", project_root=tmp_path,
        identity=RuntimeIdentity("osg-3.6.5", "test", "osg-target"),
        corpus_router=router,
    )

    async def call():
        async with Client(server) as client:
            result = await client.call_tool("search_docs", {
                "query": "query", "target_id": TARGET_ID,
            })
            return result.structured_content

    payload = asyncio.run(call())
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["error"] == "source_integrity_failure"
    assert payload["evidence"] == []
    assert SOURCE_PATH not in serialized and "0" * 64 not in serialized
    assert spy.documents == []
