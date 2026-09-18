import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.index_registry import IndexTarget
from wenjin.retrieval import router as router_module
from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig, RoutedIndex, normalize_library


def target(library: str, version: str, *, chunks: int = 3) -> IndexTarget:
    return IndexTarget(
        target_id=f"{library}-{version}-candidate-002",
        library=library,
        version=version,
        strategy="blocks-split",
        dense_collection=f"{library}_{version}_dense".replace(".", "_").replace("-", "_"),
        bm25_path=f"data/indexes/{library}.sqlite",
        manifest_path=f"corpora/{library}/source-manifest.json",
        manifest_sha256="a" * 64,
        chunk_count=chunks,
        vector_dimension=1024,
        embedding_model="fake",
    )


def index(library: str, version: str, *, chunks: int = 3) -> RoutedIndex:
    return RoutedIndex(
        target=target(library, version, chunks=chunks),
        collection=target(library, version).dense_collection,
        bm25_path=Path("unused.sqlite"),
        source_urls={},
    )


def point(chunk_id: str, *, library: str, text: str = "evidence") -> SimpleNamespace:
    return SimpleNamespace(
        score=0.9,
        payload={
            "chunk_id": chunk_id,
            "source_path": f"raw_data/{library}/1.0/doc.md",
            "version": "1.0",
            "start_line": 1,
            "end_line": 2,
            "strategy": "blocks-split",
            "text": text,
            "page_number": None,
            "logical_document_id": f"{library}-doc",
        },
    )


def build_router(indices: list[RoutedIndex], *, dense_by_collection: dict, keyword: list | None = None):
    def fake_dense(client, vector, collection, *, top_k):
        return dense_by_collection.get(collection, [])

    def fake_keyword(path, query, *, top_k):
        return keyword or []

    return MultiCorpusRouter(
        qdrant_client=SimpleNamespace(close=lambda: None),
        embedder=SimpleNamespace(get_query_embedding=lambda query: [0.0, 1.0]),
        indices=indices,
        dense_search=fake_dense,
        keyword_search=fake_keyword,
    )


def test_normalize_library_accepts_display_names():
    assert normalize_library("OGC WMS") == "ogc-wms"
    assert normalize_library(" osgEarth ") == "osgearth"


def test_resolve_requires_known_library_and_version():
    routing = build_router([index("proj", "9.8.1")], dense_by_collection={})
    assert [item.library for item in routing.resolve("PROJ")] == ["proj"]
    assert [item.library for item in routing.resolve("proj", "9.8.1")] == ["proj"]
    with pytest.raises(ValueError, match="没有library"):
        routing.resolve("nope")
    with pytest.raises(ValueError, match="没有版本"):
        routing.resolve("proj", "9.9.9")


def test_explicit_library_queries_only_that_index():
    proj, gdal = index("proj", "9.8.1"), index("gdal", "3.13.1")
    routing = build_router(
        [proj, gdal],
        dense_by_collection={
            proj.collection: [point("proj-1", library="proj")],
            gdal.collection: [point("gdal-1", library="gdal")],
        },
    )
    result = routing.search("proj_create_crs_to_crs", library="PROJ", config=RouteConfig(top_k=5))
    assert [item["library"] for item in result["routed_libraries"]] == ["proj"]
    assert {item["library"] for item in result["evidence"]} == {"proj"}
    assert result["answer_status"] == "not_generated"
    assert result["evidence_status"] == "unverified"


def test_fan_out_respects_max_libraries_and_reports_skipped():
    indices = [index(name, "1.0") for name in ("a", "b", "c")]
    routing = build_router(
        indices,
        dense_by_collection={
            item.collection: [point(f"{item.library}-1", library=item.library)] for item in indices
        },
    )
    result = routing.search("Node API", config=RouteConfig(top_k=3, max_libraries=2))
    assert [item["library"] for item in result["routed_libraries"]] == ["a", "b"]
    assert result["skipped_libraries"] == ["c"]


def test_route_config_rejects_unbounded_budgets():
    with pytest.raises(ValueError, match="candidate_k"):
        RouteConfig(candidate_k=100).validate()
    with pytest.raises(ValueError, match="top_k"):
        RouteConfig(top_k=9, candidate_k=5).validate()
    with pytest.raises(ValueError, match="rerank_pool"):
        RouteConfig(top_k=5, rerank_pool=4).validate()


def test_from_route_file_fails_closed_when_one_candidate_is_invalid(tmp_path, monkeypatch):
    route = {
        "schema_version": 1,
        "route_id": "demo",
        "candidates": [json.loads(json.dumps({
            "target_id": "demo-1", "library": "demo", "version": "1.0", "strategy": "blocks-split",
            "dense_collection": "demo_dense", "bm25_path": "data/indexes/demo.sqlite",
            "manifest_path": "corpora/demo/source-manifest.json", "manifest_sha256": "a" * 64,
            "chunk_count": 1, "vector_dimension": 1024, "embedding_model": "fake",
        }))],
    }
    path = tmp_path / "route.json"
    path.write_text(json.dumps(route), encoding="utf-8")
    calls = []

    def fake_validate(target, *, root, qdrant_client):
        calls.append(target.target_id)
        raise ValueError("BM25行数与期望不一致")

    monkeypatch.setattr(router_module, "validate_target", fake_validate)
    with pytest.raises(ValueError, match="BM25行数"):
        MultiCorpusRouter.from_route_file(
            path, root=tmp_path, qdrant_client=SimpleNamespace(), embedder=object()
        )
    assert calls == ["demo-1"]
