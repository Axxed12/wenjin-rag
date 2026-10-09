"""共享模型的 MCP 并发回归：用真实 FastMCP Client 和合成索引隔离验证。"""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.index_registry import IndexTarget
from wenjin.mcp_server import RuntimeIdentity, build_mcp_server
from wenjin.retrieval.router import MultiCorpusRouter, RoutedIndex
from wenjin.retrieval.service import RetrievalService
from wenjin.serving import CorpusSources, SourceRecord

OSG = "osg-3.6.5"
ALPHA = "alpha-1.0"
BETA = "beta-2.0"
REQUESTS = 4
WAIT_SECONDS = 0.75


class InFlightProbe:
    """以屏障制造可观测的重叠；屏障带超时，失败时不会挂住测试进程。"""

    def __init__(self, parties: int):
        self.barrier = threading.Barrier(parties)
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.barrier_timeouts = 0

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        try:
            self.barrier.wait(timeout=WAIT_SECONDS)
        except threading.BrokenBarrierError:
            # 共享锁生效时，后续调用在锁外等待；首个调用超时后逐个通过。
            with self._lock:
                self.barrier_timeouts += 1
        finally:
            with self._lock:
                self.active -= 1


class CountProbe:
    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0

    def enter(self) -> None:
        with self._lock:
            self.calls += 1


class SyntheticModels:
    def __init__(self, *, embedding_probe=None, reranker_probe=None):
        self.embedding_probe = embedding_probe
        self.reranker_probe = reranker_probe

    def get_query_embedding(self, query: str) -> list[float]:
        if self.embedding_probe is not None:
            self.embedding_probe.enter()
        return [0.0] * 8

    def score(self, query: str, documents: list[str]) -> list[float]:
        if self.reranker_probe is not None:
            self.reranker_probe.enter()
        return [0.9] * len(documents)

    def close(self) -> None:
        pass


def make_index(library: str, version: str, corpus_id: str, raw_root: Path) -> RoutedIndex:
    target = IndexTarget(
        target_id=f"{corpus_id}-target", corpus_id=corpus_id,
        library=library, version=version, strategy="blocks-split",
        dense_collection=f"{library}_dense", bm25_path=f"{library}.sqlite",
        manifest_path=f"corpora/{corpus_id}/source-manifest.json",
        manifest_sha256="a" * 64, chunk_count=1, vector_dimension=8,
        embedding_model="synthetic",
    )
    sources = CorpusSources(
        corpus_id=corpus_id, raw_data_subdir=f"{library}/{version}",
        raw_root=raw_root,
        records={"doc.md": SourceRecord("doc.md", "0" * 64, 8, True, True)},
    )
    return RoutedIndex(
        target=target, collection=target.dense_collection,
        bm25_path=Path("unused.sqlite"), source_urls={}, sources=sources,
    )


def make_default_manifest(root: Path) -> None:
    """默认单库路径只需一份临时 manifest；检索桩不返回证据。"""
    import hashlib
    import json

    raw = b"synthetic OSG source\n"
    manifest = root / "corpora" / OSG
    manifest.mkdir(parents=True)
    (manifest / "source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": OSG, "library": "osg",
        "version": "3.6.5", "raw_data_subdir": "osg/3.6.5", "license": "test",
        "files": [{"path": "doc.md", "raw_sha256": hashlib.sha256(raw).hexdigest(),
                   "bytes": len(raw), "retrieval_eligible": True, "indexable": True}],
    }), encoding="utf-8")


def make_service(root: Path, models: SyntheticModels) -> RetrievalService:
    bm25_path = root / "default.sqlite"
    bm25_path.touch()

    def empty_dense(client, embedder, collection, query, *, top_k):
        # 默认服务的 dense API 接收模型对象，由此和路由共享同一 embedding 实例。
        embedder.get_query_embedding(query)
        return []

    return RetrievalService(
        qdrant_client=SimpleNamespace(close=lambda: None), embedder=models,
        bm25_path=bm25_path, source_urls={}, collection_name="osg_dense",
        supported_version="3.6.5", index_target_id="osg-target",
        reranker=models, dense_search=empty_dense,
        keyword_search=lambda path, query, *, top_k: [],
    )


def make_router(root: Path, models: SyntheticModels) -> MultiCorpusRouter:
    indices = [
        make_index("alpha", "1.0", ALPHA, root / "raw_data"),
        make_index("beta", "2.0", BETA, root / "raw_data"),
    ]

    def dense(client, vector, collection, *, top_k):
        library = collection.removesuffix("_dense")
        version, corpus_id = ("1.0", ALPHA) if library == "alpha" else ("2.0", BETA)
        return [SimpleNamespace(score=0.9, payload={
            "chunk_id": f"{library}-chunk", "source_path": f"raw_data/{library}/{version}/doc.md",
            "version": version, "start_line": 1, "end_line": 1,
            "strategy": "blocks-split", "text": f"{library} synthetic evidence",
            "page_number": None, "logical_document_id": f"{corpus_id}:doc.md",
        })]

    return MultiCorpusRouter(
        qdrant_client=SimpleNamespace(close=lambda: None), embedder=models,
        reranker=models, indices=indices, dense_search=dense,
        keyword_search=lambda path, query, *, top_k: [],
    )


def policies(*, private_beta: bool = False) -> AccessRegistry:
    beta = AccessPolicy(BETA, "internal", ()) if private_beta else AccessPolicy(BETA, "public", ("*",))
    return AccessRegistry(Path("unused"), {
        OSG: AccessPolicy(OSG, "public", ("*",)),
        ALPHA: AccessPolicy(ALPHA, "public", ("*",)),
        BETA: beta,
    })


def make_server(root: Path, models: SyntheticModels, *, access=None):
    make_default_manifest(root)
    service = make_service(root, models)
    router = make_router(root, models)
    server = build_mcp_server(
        service, audit=SearchAudit(root / "audit.jsonl"),
        access=access or policies(), raw_root=root / "raw_data", project_root=root,
        identity=RuntimeIdentity(OSG, "test", "osg-target"), corpus_router=router,
    )
    return server, service, router


def run_calls(server, args: list[dict]) -> list[dict]:
    async def scenario():
        async with Client(server) as client:
            return await asyncio.wait_for(
                asyncio.gather(*(
                    client.call_tool("search_docs", item) for item in args
                )), timeout=15,
            )

    return [result.structured_content for result in asyncio.run(scenario())]


@pytest.mark.parametrize("operation", ["embedding", "reranker"])
def test_concurrent_scoped_mcp_calls_share_model_mutex(tmp_path, operation):
    """多库请求会为每个权限子集建 scoped router，仍必须争用同一模型互斥域。"""
    embed = InFlightProbe(REQUESTS) if operation == "embedding" else None
    rerank = InFlightProbe(REQUESTS) if operation == "reranker" else None
    models = SyntheticModels(embedding_probe=embed, reranker_probe=rerank)
    server, _, _ = make_server(tmp_path, models)
    args = [
        {"query": f"concurrent docs {n}", "target_id": (
            f"{ALPHA}-target" if n % 2 == 0 else f"{BETA}-target"
        )}
        for n in range(REQUESTS)
    ]

    payloads = run_calls(server, args)
    probe = embed or rerank
    assert all("error" not in payload for payload in payloads)
    assert probe.calls == REQUESTS
    assert probe.peak == 1, f"{operation} 在途峰值应为 1，实测 {probe.peak}"


def test_mixed_default_and_multicorpus_calls_share_embedding_mutex(tmp_path):
    """单库默认 service 与多库 scoped router 交错调用时保护同一个 embedder。"""
    probe = InFlightProbe(REQUESTS)
    models = SyntheticModels(embedding_probe=probe)
    server, service, router = make_server(tmp_path, models)
    args = [
        {"query": f"mixed query {n}"} if n % 2 == 0 else {
            "query": f"mixed query {n}", "target_id": f"{ALPHA}-target",
        }
        for n in range(REQUESTS)
    ]

    payloads = run_calls(server, args)
    assert all("error" not in payload for payload in payloads)
    assert probe.calls == REQUESTS
    assert probe.peak == 1, f"单库/多库共享 embedding 在途峰值应为 1，实测 {probe.peak}"
    assert service.search_lock is router.search_lock


def test_lifespan_builds_router_with_runtime_model_mutex(tmp_path, monkeypatch):
    """正常 lifespan 装配也把 runtime 的互斥锁传给 create_router。"""
    from wenjin.mcp import runtime

    probe = InFlightProbe(REQUESTS)
    models = SyntheticModels(embedding_probe=probe)
    make_default_manifest(tmp_path)
    service = make_service(tmp_path, models)
    identity = RuntimeIdentity(OSG, "test", "osg-target")
    captured = {}

    monkeypatch.setattr(runtime, "create_runtime", lambda **kwargs: (service, identity))

    def create_router_for_test(*, root, qdrant_client, embedder, reranker, search_lock=None):
        captured["received_lock"] = search_lock
        router = make_router(root, models)
        # 与 create_router/from_route_file 的生产装配一致：注入生命周期锁。
        if search_lock is not None:
            router.search_lock = search_lock
        captured["router"] = router
        return router

    monkeypatch.setattr(runtime, "create_router", create_router_for_test)
    server = build_mcp_server(
        None, audit=SearchAudit(tmp_path / "lifespan-audit.jsonl"),
        access=policies(), raw_root=tmp_path / "raw_data", project_root=tmp_path,
    )
    payloads = run_calls(server, [
        {"query": f"lifespan {n}"} if n % 2 == 0 else {
            "query": f"lifespan {n}", "target_id": f"{ALPHA}-target",
        }
        for n in range(REQUESTS)
    ])

    assert all("error" not in payload for payload in payloads)
    assert probe.calls == REQUESTS
    assert probe.peak == 1, f"lifespan单库/多库混合 embedding 在途峰值应为1，实测{probe.peak}"
    assert captured["received_lock"] is service.search_lock
    assert captured["router"].search_lock is service.search_lock


def test_acl_denial_skips_models_and_does_not_pollute_authorized_subset(tmp_path):
    """无权目标在模型调用前被挡住，授权调用的路由视图只含 alpha。"""
    embed = CountProbe()
    rerank = CountProbe()
    models = SyntheticModels(embedding_probe=embed, reranker_probe=rerank)
    server, _, _ = make_server(tmp_path, models, access=policies(private_beta=True))

    denied, allowed = run_calls(server, [
        {"query": "private beta", "target_id": f"{BETA}-target"},
        {"query": "public alpha", "target_id": f"{ALPHA}-target"},
    ])

    assert denied["error"] in {"forbidden", "invalid_target"}
    assert denied["evidence"] == []
    assert "beta" not in str(denied.get("routed_libraries", []))
    assert [item["library"] for item in allowed["evidence"]] == ["alpha"]
    assert allowed["routed_libraries"] == [{
        "library": "alpha", "version": "1.0", "target_id": f"{ALPHA}-target",
    }]
    assert embed.calls == 1
    assert rerank.calls == 1
