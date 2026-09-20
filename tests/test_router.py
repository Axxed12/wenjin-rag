import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.index_registry import IndexTarget
from wenjin.retrieval import router as router_module
from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig, RoutedIndex, normalize_library
from wenjin.serving import CorpusSources, SourceRecord


def target(library: str, version: str, *, chunks: int = 3) -> IndexTarget:
    return IndexTarget(
        target_id=f"{library}-{version}-candidate-002",
        library=library,
        version=version,
        # 必须与 sources() 的 corpus_id 一致：真实路由里 CorpusSources.from_manifest
        # 会强制"目录名 + manifest声明 + 目标身份"三方相同，夹具也要守住这个前提。
        corpus_id=f"{library}-{version}",
        strategy="blocks-split",
        dense_collection=f"{library}_{version}_dense".replace(".", "_").replace("-", "_"),
        bm25_path=f"data/indexes/{library}.sqlite",
        manifest_path=f"corpora/{library}/source-manifest.json",
        manifest_sha256="a" * 64,
        chunk_count=chunks,
        vector_dimension=1024,
        embedding_model="fake",
    )


def sources(library: str, version: str) -> CorpusSources:
    """与point()里的source_path对应的最小登记记录。

    假chunk的source_path统一写成`raw_data/<library>/1.0/...`，所以subdir也用1.0；
    语料身份仍按传入的version，两者在夹具里不必相等。
    """
    return CorpusSources(
        corpus_id=f"{library}-{version}",
        raw_data_subdir=f"{library}/1.0",
        raw_root=Path("unused"),
        records={"doc.md": SourceRecord("doc.md", "0" * 64, 8, True, True)},
    )


def index(library: str, version: str, *, chunks: int = 3) -> RoutedIndex:
    return RoutedIndex(
        target=target(library, version, chunks=chunks),
        collection=target(library, version).dense_collection,
        bm25_path=Path("unused.sqlite"),
        source_urls={},
        sources=sources(library, version),
    )


def point(chunk_id: str, *, library: str, text: str = "evidence",
          version: str = "1.0") -> SimpleNamespace:
    """假候选。`version` 要与目标一致：真实索引里审计强制 payload 版本等于目标版本。"""
    return SimpleNamespace(
        score=0.9,
        payload={
            "chunk_id": chunk_id,
            "source_path": f"raw_data/{library}/1.0/doc.md",
            "version": version,
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
    # 报告用 target_id：同一个 library 有多个版本时，只写 library 说不清裁掉的是哪个。
    assert result["skipped_libraries"] == ["c-1.0-candidate-002"]


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

    def fake_admit(target, *, root, qdrant_client):
        calls.append(target.target_id)
        raise ValueError("BM25行数与期望不一致")

    monkeypatch.setattr(router_module, "admit_index", fake_admit)
    with pytest.raises(ValueError, match="BM25行数"):
        MultiCorpusRouter.from_route_file(
            path, root=tmp_path, qdrant_client=SimpleNamespace(), embedder=object()
        )
    assert calls == ["demo-1"]


# --- 第五批：多版本路由、唯一身份、辅助入口 ---


def test_ambiguous_library_without_version_is_rejected():
    """一个 library 有多个版本时，只给 library 必须报错而不是把它们混在一起。

    旧行为是返回全部版本，再在下游用 library 做字典键——两个版本互相覆盖，
    一个版本的结果会静默消失。
    """
    routing = build_router([index("proj", "9.8.1"), index("proj", "9.8.2")],
                           dense_by_collection={})
    with pytest.raises(ValueError, match="有多个版本"):
        routing.resolve("proj")
    # 补上 version 或用 target_id 就能唯一确定。
    assert [item.version for item in routing.resolve("proj", "9.8.1")] == ["9.8.1"]
    assert routing.resolve_target("proj-9.8.2-candidate-002").version == "9.8.2"


def test_error_message_lists_versions_and_target_ids():
    routing = build_router([index("proj", "9.8.1"), index("proj", "9.8.2")],
                           dense_by_collection={})
    with pytest.raises(ValueError) as error:
        routing.resolve("proj")
    message = str(error.value)
    assert "9.8.1" in message and "9.8.2" in message
    assert "proj-9.8.1-candidate-002" in message


def test_each_target_contributes_its_own_ranking_list():
    """每个 target 必须作为**独立**的排名列表参与 RRF。

    RRF 把结果集的键名记进 `ranks`，所以"谁贡献了这条候选"是可观测的：
    修复前键是 `f"{library}:{route}"`，两个版本的列表会互相覆盖，
    幸存的那条列表里混着两个版本的候选——归属就说不清了。
    """
    older, newer = index("proj", "9.8.1"), index("proj", "9.8.2")
    routing = build_router(
        [older, newer],
        dense_by_collection={
            older.collection: [point("older-1", library="proj", text="旧版本正文",
                                     version="9.8.1")],
            newer.collection: [point("newer-1", library="proj", text="新版本正文",
                                     version="9.8.2")],
        },
    )
    fused = {item["chunk_id"]: item
             for item in routing.retrieve("参数", config=RouteConfig(max_libraries=5))[
                 "fused_candidates"]}
    assert set(fused) == {"older-1", "newer-1"}, "一个版本的候选被另一个覆盖了"
    assert set(fused["older-1"]["ranks"]) == {"proj-9.8.1-candidate-002:dense"}
    assert set(fused["newer-1"]["ranks"]) == {"proj-9.8.2-candidate-002:dense"}


def test_two_versions_of_one_library_both_reach_evidence():
    older, newer = index("proj", "9.8.1"), index("proj", "9.8.2")
    routing = build_router(
        [older, newer],
        dense_by_collection={
            older.collection: [point("older-1", library="proj", text="旧版本正文",
                                     version="9.8.1")],
            newer.collection: [point("newer-1", library="proj", text="新版本正文",
                                     version="9.8.2")],
        },
    )
    result = routing.search("参数", config=RouteConfig(top_k=5, max_libraries=5))
    assert len(result["routed_libraries"]) == 2
    assert {item["index_target_id"] for item in result["evidence"]} == {
        "proj-9.8.1-candidate-002", "proj-9.8.2-candidate-002"
    }


def test_evidence_carries_its_owning_target_and_version():
    """证据必须说明自己属于哪个 target/版本，不能只写 library。"""
    older, newer = index("proj", "9.8.1"), index("proj", "9.8.2")
    routing = build_router(
        [older, newer],
        dense_by_collection={
            older.collection: [point("older-1", library="proj", text="旧", version="9.8.1")],
            newer.collection: [point("newer-1", library="proj", text="新", version="9.8.2")],
        },
    )
    result = routing.search("参数", config=RouteConfig(top_k=5, max_libraries=5))
    pairs = {(item["index_target_id"], item["version"]) for item in result["evidence"]}
    assert pairs == {("proj-9.8.1-candidate-002", "9.8.1"),
                     ("proj-9.8.2-candidate-002", "9.8.2")}
    assert all(item["corpus_id"] for item in result["evidence"])


def test_payload_cannot_spoof_its_owning_library():
    """payload 里出现 library/target_id 时不能覆盖路由标注的归属。

    否则一条候选可以自称属于别的语料，取件链接就会指向另一个库。
    """
    proj = index("proj", "9.8.1")
    spoofed = point("proj-1", library="proj")
    spoofed.payload["library"] = "gdal"
    spoofed.payload["target_id"] = "gdal-3.13.1-candidate-002"
    routing = build_router([proj], dense_by_collection={proj.collection: [spoofed]})
    result = routing.search("参数", config=RouteConfig(top_k=5))
    assert {item["library"] for item in result["evidence"]} == {"proj"}
    assert {item["index_target_id"] for item in result["evidence"]} == {
        "proj-9.8.1-candidate-002"
    }


def test_target_id_and_library_together_is_rejected():
    """三种寻址方式互斥：同时给出时不猜调用方想要哪一个。"""
    routing = build_router([index("proj", "9.8.1")], dense_by_collection={})
    with pytest.raises(ValueError, match="不能与 library/version 同时使用"):
        routing.search("参数", library="proj", target_id="proj-9.8.1-candidate-002")


def test_unknown_target_id_lists_available():
    routing = build_router([index("proj", "9.8.1")], dense_by_collection={})
    with pytest.raises(ValueError, match="没有target_id"):
        routing.resolve_target("nope")


def test_catalog_exposes_unique_target_id_and_corpus_id():
    routing = build_router([index("proj", "9.8.1"), index("proj", "9.8.2")],
                           dense_by_collection={})
    rows = routing.catalog()
    assert len(rows) == 2
    assert {row["target_id"] for row in rows} == {
        "proj-9.8.1-candidate-002", "proj-9.8.2-candidate-002"
    }
    assert {row["corpus_id"] for row in rows} == {"proj-9.8.1", "proj-9.8.2"}


def test_from_route_file_actually_builds_a_router(tmp_path, monkeypatch):
    """成功路径必须真的被走到。

    此前唯一的 from_route_file 测试把 admit_index 桩成抛错，因此从没执行到构造
    CorpusSources 那一行——一个错误的关键字参数（corpus_id vs expected_corpus_id）
    就这样躲过了全部测试，跨库路由入口实际上构造不出来。
    """
    manifest_dir = tmp_path / "corpora" / "demo-1.0"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": "demo-1.0", "library": "demo",
        "version": "1.0", "raw_data_subdir": "demo/1.0", "license": "test",
        "files": [{"path": "doc.md", "raw_sha256": "0" * 64, "canonical_sha256": "0" * 64,
                   "normalization": "text-newline-lf-v1", "encoding": "utf-8",
                   "retrieval_eligible": True, "logical_document_id": "demo-1.0:doc.md",
                   "source_url": "https://example.org/demo/doc.md"}],
    }), encoding="utf-8")
    route = {"schema_version": 1, "route_id": "demo", "candidates": [{
        "target_id": "demo-1.0-candidate-002", "corpus_id": "demo-1.0",
        "library": "demo", "version": "1.0", "strategy": "blocks-split",
        "dense_collection": "demo_dense", "bm25_path": "data/indexes/demo.sqlite",
        "manifest_path": "corpora/demo-1.0/source-manifest.json",
        "manifest_sha256": "a" * 64, "chunk_count": 1, "vector_dimension": 1024,
        "embedding_model": "fake",
    }]}
    path = tmp_path / "route.json"
    path.write_text(json.dumps(route), encoding="utf-8")

    monkeypatch.setattr(router_module, "admit_index", lambda *a, **k: {"status": "PASS"})
    routing = MultiCorpusRouter.from_route_file(
        path, root=tmp_path, qdrant_client=SimpleNamespace(), embedder=object()
    )
    assert [item.target_id for item in routing.indices] == ["demo-1.0-candidate-002"]
    assert routing.indices[0].sources.corpus_id == "demo-1.0"


# --- 第五批复审：目标身份隔离的三处边界 ---


def test_duplicate_target_id_is_rejected_at_construction():
    """target_id 是唯一寻址方式，重复会让两处选择规则不一致。

    `resolve_target` 取第一个匹配，而归属映射按 key 保留最后一个——
    于是"查的是 A、引用的却是 B 的语料和下载地址"。
    """
    duplicated = [index("proj", "9.8.1"), index("proj", "9.8.1")]
    assert duplicated[0].target_id == duplicated[1].target_id
    with pytest.raises(ValueError, match="重复的 target_id"):
        build_router(duplicated, dense_by_collection={})


def test_same_library_and_version_with_two_targets_is_ambiguous():
    """同库同版本可以有不同策略/模型/批次的目标，只给库和版本仍不够。"""
    first = RoutedIndex(
        target=IndexTarget(
            target_id="proj-9.8.1-blocks-split-candidate-002", library="proj",
            version="9.8.1", corpus_id="proj-9.8.1", strategy="blocks-split",
            dense_collection="proj_a", bm25_path="data/indexes/a.sqlite",
            manifest_path="corpora/proj-9.8.1/source-manifest.json",
            manifest_sha256="a" * 64, chunk_count=1, vector_dimension=1024,
            embedding_model="fake",
        ),
        collection="proj_a", bm25_path=Path("unused"), source_urls={},
        sources=sources("proj", "9.8.1"),
    )
    second = RoutedIndex(
        target=IndexTarget(
            target_id="proj-9.8.1-blocks-split-candidate-003", library="proj",
            version="9.8.1", corpus_id="proj-9.8.1", strategy="blocks-split",
            dense_collection="proj_b", bm25_path="data/indexes/b.sqlite",
            manifest_path="corpora/proj-9.8.1/source-manifest.json",
            manifest_sha256="a" * 64, chunk_count=1, vector_dimension=1024,
            embedding_model="fake",
        ),
        collection="proj_b", bm25_path=Path("unused"), source_urls={},
        sources=sources("proj", "9.8.1"),
    )
    routing = build_router([first, second], dense_by_collection={})
    with pytest.raises(ValueError) as error:
        routing.resolve("proj", "9.8.1")
    message = str(error.value)
    assert "有多个索引目标" in message
    assert "candidate-002" in message and "candidate-003" in message
    # 用 target_id 就能唯一确定。
    assert routing.resolve_target("proj-9.8.1-blocks-split-candidate-003").version == "9.8.1"


def test_omitting_library_still_fans_out():
    """省略 library 的显式扇出必须保留：那是调用方主动要求的跨库行为。"""
    indices = [index("a", "1.0"), index("b", "1.0"), index("c", "2.0")]
    routing = build_router(indices, dense_by_collection={})
    assert len(routing.resolve(None)) == 3
    assert len(routing.resolve(None, "1.0")) == 2


def test_cross_target_same_chunk_id_does_not_merge():
    """chunk_id 只在单个索引内唯一；跨目标同名不能并成一条。

    并成一条会正文取先出现的那份、分数却叠加两边的排名贡献——证据与分数都失真。
    """
    older = index("proj", "9.8.1")
    newer = index("proj", "9.8.2")
    routing = build_router(
        [older, newer],
        dense_by_collection={
            older.collection: [point("README.md:0000", library="proj", text="BODY_A",
                                     version="9.8.1")],
            newer.collection: [point("README.md:0000", library="proj", text="BODY_B",
                                     version="9.8.2")],
        },
    )
    fused = routing.retrieve("参数", config=RouteConfig(max_libraries=5))["fused_candidates"]
    assert len(fused) == 2, "跨目标的同名 chunk 被合并了"
    by_text = {item["text"]: item for item in fused}
    assert set(by_text) == {"BODY_A", "BODY_B"}
    # 每一条只应记录自己那个目标的名次，分数不叠加另一边的贡献。
    assert set(by_text["BODY_A"]["ranks"]) == {"proj-9.8.1-candidate-002:dense"}
    assert set(by_text["BODY_B"]["ranks"]) == {"proj-9.8.2-candidate-002:dense"}
    assert by_text["BODY_A"]["rrf_score"] == by_text["BODY_B"]["rrf_score"]
    # 原始 chunk_id 仍然保留为证据字段。
    assert {item["chunk_id"] for item in fused} == {"README.md:0000"}


def test_same_target_across_retrievers_still_merges():
    """对照：同一目标内 dense 与 BM25 命中同一条 chunk 时必须正常合并。"""
    proj = index("proj", "9.8.1")
    document = point("proj-1", library="proj", text="同一段正文", version="9.8.1")
    keyword_hit = {"chunk_id": "proj-1", "source_path": "raw_data/proj/1.0/doc.md",
                   "version": "9.8.1", "start_line": 1, "end_line": 2,
                   "strategy": "blocks-split", "text": "同一段正文", "score": 1.0}
    routing = build_router([proj], dense_by_collection={proj.collection: [document]},
                           keyword=[keyword_hit])
    # 查询里必须含英文/代码标识符，否则 BM25 那一路不会被调用。
    fused = routing.retrieve("proj_create_crs_to_crs", config=RouteConfig())["fused_candidates"]
    assert len(fused) == 1
    assert set(fused[0]["ranks"]) == {"proj-9.8.1-candidate-002:dense",
                                      "proj-9.8.1-candidate-002:bm25"}
    assert fused[0]["rrf_score"] > 0.016
