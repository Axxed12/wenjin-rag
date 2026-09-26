"""父子化检索：父块表、命中child后换父块、以及融合阶段的去重与名额。

去重是这套设计最容易出错的一处：同一父块会被多个 child 各自命中，只要有一条路径
没按父块合并，同一段正文就会占掉多个 top-K 名额，把别的证据挤出去——而结果只是
"少了几条"，看起来不会更差。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from wenjin.index_registry import IndexTarget
from wenjin.indexing.bm25 import bm25_query, build_bm25_index, load_parents
from wenjin.indexing.dense import create_dense_collection, dense_query_vector
from wenjin.ingestion.chunking import PARENTED_STRATEGY, block_chunks_parented
from wenjin.retrieval.fusion import identity_by_parent, rrf_fuse
from wenjin.retrieval.parents import resolve_parents
from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig, RoutedIndex
from wenjin.retrieval.service import RetrievalService, SearchConfig
from wenjin.serving import CorpusSources, SourceRecord

SOURCE_PATH = "raw_data/osg/3.6.5/include/osg/Test"
SUBDIR = "osg/3.6.5"
COLLECTION = "parented_test_dense"


class StubEmbedder:
    """固定向量：只验证链路与元数据，不测检索效果。"""

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def get_query_embedding(self, query):
        return [1.0, 0.0, 0.0, 0.0]


def text_of(lines: int = 6, *, width: int = 40) -> str:
    return "".join(f"line-{index:03d}-{'x' * width}\n" for index in range(lines))


def document(text: str | None = None, **extra) -> dict:
    return {
        "source_path": SOURCE_PATH,
        "version": "3.6.5",
        "text": text if text is not None else text_of(),
        **extra,
    }


def build_assets(tmp_path: Path, *, max_chars: int = 100, max_children: int = 2) -> dict:
    """真实的一对资产：BM25（含父块表）+ 内存 Qdrant（child 向量与 payload）。"""
    children, parents = block_chunks_parented(
        document(), max_chars=max_chars, max_children=max_children, max_parent_chars=10_000
    )
    bm25_path = tmp_path / "parented.sqlite"
    build_bm25_index(bm25_path, children, parents=parents)
    client = QdrantClient(":memory:")
    vectors = StubEmbedder().get_text_embedding_batch([child.text for child in children])
    create_dense_collection(client, COLLECTION, children, vectors)
    return {
        "children": children,
        "parents": parents,
        "bm25_path": bm25_path,
        "client": client,
        "text": document()["text"],
    }


def dense_points(client, *, top_k: int = 10) -> list[dict]:
    return [
        {"score": point.score, **point.payload}
        for point in dense_query_vector(client, [1.0, 0.0, 0.0, 0.0], COLLECTION, top_k=top_k)
    ]


@pytest.fixture()
def assets(tmp_path):
    return build_assets(tmp_path)


def sources() -> CorpusSources:
    return CorpusSources(
        corpus_id="osg-3.6.5",
        raw_data_subdir=SUBDIR,
        raw_root=Path("unused"),
        records={"include/osg/Test": SourceRecord("include/osg/Test", "0" * 64, 4096, True, True)},
    )


def routed_index(assets: dict, *, strategy: str = PARENTED_STRATEGY) -> RoutedIndex:
    target = IndexTarget(
        target_id="osg-3.6.5-parented-test",
        corpus_id="osg-3.6.5",
        library="osg",
        version="3.6.5",
        strategy=strategy,
        dense_collection=COLLECTION,
        bm25_path=str(assets["bm25_path"]),
        manifest_path="corpora/osg-3.6.5/source-manifest.json",
        manifest_sha256="a" * 64,
        chunk_count=len(assets["children"]),
        vector_dimension=4,
        embedding_model="fake",
    )
    return RoutedIndex(
        target=target,
        collection=COLLECTION,
        bm25_path=Path(assets["bm25_path"]),
        source_urls={SOURCE_PATH: "https://example.org/Test"},
        sources=sources(),
    )


# --- 索引侧：父块在哪、child 身上带了什么 -------------------------------------


def test_parents_live_outside_the_fts_table(assets):
    """父块若进了 FTS 表，它们自己就会被关键词命中，父子化就变成了"两种粒度混检"。"""
    import sqlite3

    with sqlite3.connect(assets["bm25_path"]) as database:
        chunk_ids = {row[0] for row in database.execute("SELECT chunk_id FROM chunks")}
        parent_ids = {row[0] for row in database.execute("SELECT chunk_id FROM parents")}
        parent_text = {row[0]: row[1] for row in database.execute("SELECT chunk_id, text FROM parents")}
    assert chunk_ids == {child.chunk_id for child in assets["children"]}
    assert parent_ids == {parent.chunk_id for parent in assets["parents"]}
    assert not chunk_ids & parent_ids
    # 父块正文必须是真的写进了索引，而不是检索时才现算的。
    assert parent_text == {parent.chunk_id: parent.text for parent in assets["parents"]}


def test_bm25_hits_carry_parent_id_and_never_return_a_parent(assets):
    hits = bm25_query(assets["bm25_path"], "line-000", top_k=10)
    assert hits, "夹具的查询词应当能命中 child"
    child_ids = {child.chunk_id for child in assets["children"]}
    parent_ids = {parent.chunk_id for parent in assets["parents"]}
    assert {hit["chunk_id"] for hit in hits} <= child_ids
    for hit in hits:
        assert hit["parent_id"] in parent_ids


def test_dense_payload_carries_parent_id(assets):
    points = dense_points(assets["client"])
    child_ids = {child.chunk_id for child in assets["children"]}
    assert {point["chunk_id"] for point in points} == child_ids
    for point in points:
        assert point["parent_id"] == next(
            child.parent_id for child in assets["children"] if child.chunk_id == point["chunk_id"]
        ), "向量侧缺 parent_id 时，向量命中的候选换不到父块"


def test_load_parents_returns_rows_and_tolerates_a_plain_index(tmp_path):
    assets = build_assets(tmp_path)
    wanted = {parent.chunk_id for parent in assets["parents"]}
    loaded = load_parents(assets["bm25_path"], wanted)
    assert set(loaded) == wanted
    assert loaded[assets["parents"][0].chunk_id]["text"] == assets["parents"][0].text
    assert load_parents(assets["bm25_path"], set()) == {}

    # 没有父子结构的索引（老候选）里没有 parents 表：返回空字典，不是报错。
    plain_path = tmp_path / "plain.sqlite"
    build_bm25_index(plain_path, assets["children"])
    assert load_parents(plain_path, wanted) == {}


# --- 替换：命中 child，返回父块 -----------------------------------------------


def test_resolve_parents_swaps_the_child_for_its_parent(assets):
    points = dense_points(assets["client"])
    resolved = resolve_parents(points, bm25_path=assets["bm25_path"])
    parents = {parent.chunk_id: parent for parent in assets["parents"]}
    assert {item["chunk_id"] for item in resolved} == set(parents)
    for item in resolved:
        parent = parents[item["chunk_id"]]
        assert (item["start_line"], item["end_line"]) == (parent.start_line, parent.end_line)
        assert item["text"] == parent.text
        assert item["source_path"] == SOURCE_PATH
    # 分数与路由归属是检索方标注的，不能被父块行覆盖。
    assert [item["score"] for item in resolved] == [point["score"] for point in points]
    # 命中的是哪个child要留下来，否则排查召回原因时无从下手。
    assert {item["child_chunk_id"] for item in resolved} == {
        child.chunk_id for child in assets["children"]
    }


def test_resolve_parents_is_a_noop_without_parent_ids(assets):
    plain = [{"chunk_id": "x", "text": "t", "score": 1.0}]
    assert resolve_parents(plain, bm25_path=assets["bm25_path"]) is plain


def test_resolve_parents_refuses_to_return_a_child_when_the_parent_is_gone(assets):
    """取不到父块是资产损坏：悄悄退回 child 会让证据少掉上下文，却看不出异常。"""
    broken = [{**point, "parent_id": "missing-parent"} for point in dense_points(assets["client"])]
    with pytest.raises(ValueError, match="取不到的父块"):
        resolve_parents(broken, bm25_path=assets["bm25_path"])


# --- 融合：一个父块只能占一个名额 ---------------------------------------------


def child(chunk_id: str, *, parent_id: str | None = None, text: str = "t") -> dict:
    item = {"chunk_id": chunk_id, "text": text, "score": 1.0}
    if parent_id is not None:
        item["parent_id"] = parent_id
    return item


def test_rrf_keeps_one_entry_per_parent():
    fused = rrf_fuse(
        {"dense": [child("c1", parent_id="p1"), child("c2", parent_id="p1"),
                   child("c3", parent_id="p2")]},
        k=60,
    )
    assert [item["chunk_id"] for item in fused] == ["c1", "c3"]
    # 同一父块的多个 child 不按次数累加：c1 拿最好的名次，c2 的名次不再加分。
    assert fused[0]["rrf_score"] == pytest.approx(1 / 61)


def test_rrf_keeps_the_best_rank_and_score_within_one_retriever():
    fused = rrf_fuse(
        {"dense": [child("c9", parent_id="p1"), child("c1", parent_id="p1")]},
        k=60,
    )
    assert len(fused) == 1
    assert fused[0]["ranks"] == {"dense": 1}
    assert fused[0]["original_scores"] == {"dense": 1.0}
    assert fused[0]["chunk_id"] == "c9", "正文应取最先出现的那份"


def test_rrf_identity_defaults_to_parent_and_ignores_it_when_absent():
    assert identity_by_parent(child("c1", parent_id="p1")) == "p1"
    assert identity_by_parent(child("c1")) == "c1"
    plain = rrf_fuse({"dense": [child("c1")], "bm25": [child("c1")]}, k=60)
    assert len(plain) == 1 and plain[0]["ranks"] == {"dense": 1, "bm25": 1}


# --- 端到端：两条产线路径都要换父块 -------------------------------------------


def test_router_replaces_children_with_their_parent_before_fusion(assets):
    index = routed_index(assets)
    router = MultiCorpusRouter(
        qdrant_client=assets["client"],
        embedder=StubEmbedder(),
        indices=[index],
        dense_search=dense_query_vector,
        keyword_search=bm25_query,
    )
    retrieval = router.retrieve(
        "line-000", config=RouteConfig(candidate_k=10, top_k=5, rerank_pool=10)
    )
    # 诊断列表保留原始命中：命中的确实是 child，"换成父块"发生在融合之前。
    assert {item["chunk_id"] for item in retrieval["dense_candidates"]} == {
        child.chunk_id for child in assets["children"]
    }
    parents = {parent.chunk_id: parent for parent in assets["parents"]}
    fused = retrieval["fused_candidates"]
    assert {item["chunk_id"] for item in fused} == set(parents)
    assert len(fused) == len(parents), "同一父块的多个child各占了一条候选"
    assert all(len(item["parent_id"]) > 0 for item in fused)
    for item in fused:
        assert item["text"] == parents[item["chunk_id"]].text
    router.close()


def test_router_evidence_is_the_parent_block(assets):
    """证据契约不变：返回的 source_path/行区间/正文必须逐字对应父块那段原文。"""
    router = MultiCorpusRouter(
        qdrant_client=assets["client"],
        embedder=StubEmbedder(),
        indices=[routed_index(assets)],
        dense_search=dense_query_vector,
        keyword_search=bm25_query,
    )
    result = router.search(
        "line-000", library="osg", version="3.6.5",
        config=RouteConfig(candidate_k=10, top_k=5, rerank_pool=10),
    )
    parents = {parent.chunk_id: parent for parent in assets["parents"]}
    assert result["evidence"], "父子化之后证据不能为空"
    assert len({item["chunk_id"] for item in result["evidence"]}) == len(result["evidence"])
    lines = assets["text"].splitlines(keepends=True)
    for rank, item in enumerate(result["evidence"], start=1):
        parent = parents[item["chunk_id"]]
        assert item["rank"] == rank
        assert item["source_path"] == SOURCE_PATH
        assert (item["start_line"], item["end_line"]) == (parent.start_line, parent.end_line)
        assert item["text"] == "".join(lines[parent.start_line - 1 : parent.end_line])
    assert result["answer_status"] == "not_generated"
    router.close()


def test_single_corpus_service_also_returns_the_parent(assets):
    """单库服务与跨库路由必须表现一致，否则同一份索引在两个入口给出不同粒度。"""
    children = assets["children"]
    points = [
        SimpleNamespace(score=0.9, payload={**point})
        for point in dense_points(assets["client"])
    ]
    service = RetrievalService(
        qdrant_client=assets["client"],
        embedder=StubEmbedder(),
        bm25_path=Path(assets["bm25_path"]),
        source_urls={SOURCE_PATH: "https://example.org/Test"},
        collection_name=COLLECTION,
        dense_search=lambda client, embedder, collection, query, top_k: points,
    )
    result = service.search(
        "line-000", config=SearchConfig(candidate_k=10, top_k=5)
    )
    parents = {parent.chunk_id: parent for parent in assets["parents"]}
    assert {item["chunk_id"] for item in result["evidence"]} == set(parents)
    assert len(result["evidence"]) == len(parents)
    assert len(children) > len(parents), "夹具里一个父块应当有多个child"
    for item in result["evidence"]:
        assert item["text"] == parents[item["chunk_id"]].text
        assert item["start_line"] == parents[item["chunk_id"]].start_line
    service.close()


# --- 另一条臂：重排在 child 粒度上做，只在返回前换父块 -------------------------


def test_resolve_parents_after_rerank_keeps_the_rerank_score(assets):
    """重排之后再换父块时，名次依据（rerank_score）必须留在证据上：
    丢了它，这条证据就说不清自己为什么排在前面，排序与正文就对不上了。
    """
    from wenjin.retrieval.rerank import rank_candidates

    fused = dense_points(assets["client"])
    ranked = rank_candidates(fused, [0.1 * index for index in range(len(fused))])
    returned = resolve_parents(ranked, bm25_path=assets["bm25_path"])
    parents = {parent.chunk_id: parent for parent in assets["parents"]}
    assert {item["chunk_id"] for item in returned} == set(parents)
    for item in returned:
        assert item["rerank_score"] is not None
        assert item["pre_rerank_rank"] is not None
        assert item["text"] == parents[item["chunk_id"]].text
        assert item["child_chunk_id"], "命中原因（哪个child）仍要留着"


def test_dedupe_by_parent_keeps_the_best_ranked_entry():
    from evaluation.run_multicorpus_eval import dedupe_by_parent

    kept = dedupe_by_parent([
        {"chunk_id": "c1", "parent_id": "p1", "text": "first"},
        {"chunk_id": "c2", "parent_id": "p1", "text": "second"},
        {"chunk_id": "c3", "parent_id": "p2", "text": "third"},
        {"chunk_id": "c4", "text": "no-parent"},
        {"chunk_id": "c4", "text": "no-parent-again"},
    ])
    assert [(item["chunk_id"], item["text"]) for item in kept] == [
        ("c1", "first"), ("c3", "third"), ("c4", "no-parent")
    ]
