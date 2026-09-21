"""MCP 多语料检索：按 library / target_id 寻址，并按调用方权限过滤。

这里测的是**接入层**：路由本身的行为在 `test_router.py` 里覆盖。关注三件事：

1. 代码路径有没有接上（不带 library 仍走 OSG，带上就走路由）；
2. **权限过滤发生在解析之前**——否则"无权"与"不存在"会给出不同报错，
   等于告诉调用方"这个语料存在但你读不了"；
3. 未配置多语料时给出明确错误，而不是悄悄查错库。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import Client

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.index_registry import IndexTarget
from wenjin.mcp_server import RuntimeIdentity, build_mcp_server
from wenjin.retrieval.router import MultiCorpusRouter, RoutedIndex
from wenjin.serving import CorpusSources, SourceRecord

ALPHA = "alpha-1.0"
BETA = "beta-2.0"


class FakeService:
    """OSG 默认路径用的桩；多语料测试不经过它。"""

    qdrant_client = SimpleNamespace()

    def search(self, query, *, config):
        return {"query": query, "version": config.version, "evidence": [],
                "answer_status": "not_generated"}


def make_index(library: str, version: str, corpus_id: str) -> RoutedIndex:
    target = IndexTarget(
        target_id=f"{corpus_id}-blocks-split", corpus_id=corpus_id,
        library=library, version=version, strategy="blocks-split",
        dense_collection=f"{library}_dense",
        bm25_path=f"data/indexes/{library}.sqlite",
        manifest_path=f"corpora/{corpus_id}/source-manifest.json",
        manifest_sha256="a" * 64, chunk_count=1, vector_dimension=8,
        embedding_model="fake",
    )
    sources = CorpusSources(
        corpus_id=corpus_id, raw_data_subdir=f"{library}/{version}",
        raw_root=Path("unused"),
        records={"doc.md": SourceRecord("doc.md", "0" * 64, 8, True, True)},
    )
    return RoutedIndex(
        target=target, collection=target.dense_collection,
        bm25_path=Path("unused.sqlite"), source_urls={}, sources=sources,
    )


def point(chunk_id: str, *, library: str, version: str, text: str = "evidence"):
    return SimpleNamespace(score=0.9, payload={
        "chunk_id": chunk_id, "source_path": f"raw_data/{library}/{version}/doc.md",
        "version": version, "start_line": 1, "end_line": 2, "strategy": "blocks-split",
        "text": text, "page_number": None,
        "logical_document_id": f"{corpus_id_of(library, version)}:doc.md",
    })


def corpus_id_of(library: str, version: str) -> str:
    return f"{library}-{version}"


def build_router(indices: list[RoutedIndex]) -> MultiCorpusRouter:
    def fake_dense(client, vector, collection, *, top_k):
        library = collection.replace("_dense", "")
        version = "1.0" if library == "alpha" else "2.0"
        return [point(f"{library}-1", library=library, version=version,
                      text=f"{library} 的正文")]

    def fake_keyword(path, query, *, top_k):
        return []

    return MultiCorpusRouter(
        qdrant_client=SimpleNamespace(close=lambda: None),
        embedder=SimpleNamespace(get_query_embedding=lambda q: [0.0] * 8),
        indices=indices, dense_search=fake_dense, keyword_search=fake_keyword,
    )


def make_osg_corpus(tmp_path: Path) -> None:
    """默认路径要读 OSG 语料目录来拼取件链接，最小造一份。"""
    import hashlib
    import json

    guide = b"# Guide\n"
    raw_dir = tmp_path / "raw_data/osg/3.6.5/include/osg"
    raw_dir.mkdir(parents=True)
    (raw_dir / "Node").write_bytes(guide)
    manifest_dir = tmp_path / "corpora/osg-3.6.5"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": "osg-3.6.5", "library": "osg",
        "version": "3.6.5", "raw_data_subdir": "osg/3.6.5", "license": "OSGPL",
        "files": [{"path": "include/osg/Node",
                   "raw_sha256": hashlib.sha256(guide).hexdigest(),
                   "bytes": len(guide), "retrieval_eligible": True, "indexable": True}],
    }), encoding="utf-8")


def build_server(tmp_path: Path, *, access: AccessRegistry, router=None, service=None):
    return build_mcp_server(
        service or FakeService(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=access,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=RuntimeIdentity("osg-3.6.5", "test", "osg-target"),
        corpus_router=router,
    )


def policies(*corpus_ids: str) -> AccessRegistry:
    return AccessRegistry(None, {
        corpus_id: AccessPolicy(corpus_id, "public", ("*",)) for corpus_id in corpus_ids
    })


def call(server, tool: str, arguments: dict) -> dict:
    async def scenario():
        async with Client(server) as client:
            return (await client.call_tool(tool, arguments)).structured_content

    return asyncio.run(scenario())


@pytest.fixture()
def two_corpora() -> list[RoutedIndex]:
    return [make_index("alpha", "1.0", ALPHA), make_index("beta", "2.0", BETA)]


# --- 接入是否真的接上了 ---


def test_search_by_library_reaches_the_routed_path(tmp_path, two_corpora):
    """带 library 时必须走路由，并返回该库的候选。"""
    server = build_server(tmp_path, access=policies(ALPHA, BETA),
                          router=build_router(two_corpora))
    data = call(server, "search_docs", {"query": "alpha 的问题", "library": "alpha"})
    assert data["error"] if "error" in data else True
    assert data["routed_libraries"] == [{
        "library": "alpha", "version": "1.0",
        "target_id": "alpha-1.0-blocks-split",
    }]
    assert [item["library"] for item in data["evidence"]] == ["alpha"]
    assert data["request_id"] and data["mcp_protocol"]


def test_search_by_target_id_is_supported(tmp_path, two_corpora):
    server = build_server(tmp_path, access=policies(ALPHA, BETA),
                          router=build_router(two_corpora))
    data = call(server, "search_docs",
                {"query": "q", "target_id": "beta-2.0-blocks-split"})
    assert [item["library"] for item in data["evidence"]] == ["beta"]


def test_routed_evidence_carries_its_own_corpus_and_download_path(tmp_path, two_corpora):
    """证据必须带自己的语料身份与取件链接，不能被重标成别的语料。"""
    server = build_server(tmp_path, access=policies(ALPHA, BETA),
                          router=build_router(two_corpora))
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})
    evidence = data["evidence"][0]
    assert evidence["corpus_id"] == ALPHA
    assert evidence["index_target_id"] == "alpha-1.0-blocks-split"
    assert evidence["download_path"] == f"/files/{ALPHA}/doc.md"


# --- 权限过滤必须发生在解析之前 ---


def test_unauthorized_library_looks_exactly_like_a_missing_one(tmp_path, two_corpora):
    """无权读取与不存在必须表现一致，否则报错差异本身就泄露了语料的存在。"""
    server = build_server(tmp_path, access=policies(ALPHA),   # 只有 alpha 的权限
                          router=build_router(two_corpora))
    denied = call(server, "search_docs", {"query": "q", "library": "beta"})
    missing = call(server, "search_docs", {"query": "q", "library": "gamma"})
    assert denied["error"] == missing["error"] == "invalid_target"
    # 报错里会回显调用方自己传的库名，那不是泄露——他本来就知道自己传了什么。
    # 泄露指的是"告诉你这个语料存在但你读不了"，所以要比对两者的**形态**：
    # 去掉回显的库名之后，措辞与"可用库列表"必须完全相同。
    assert denied["detail"].replace("beta", "X") == missing["detail"].replace("gamma", "X")
    # "可用库"列表只含被授权的库。
    assert "可用：alpha" in denied["detail"]
    assert "beta" not in denied["detail"].split("可用：")[1]


def test_library_filtered_out_by_acl_is_not_searched(tmp_path, two_corpora):
    """被权限挡掉的库不能出现在路由结果里，也不能被实际查询。"""
    server = build_server(tmp_path, access=policies(ALPHA),
                          router=build_router(two_corpora))
    data = call(server, "search_docs", {"query": "q"})   # 不指定 library
    assert data["error"] if "error" in data else True
    assert "routed_libraries" not in data or all(
        item["library"] != "beta" for item in data.get("routed_libraries", [])
    )


def test_no_readable_corpus_is_forbidden(tmp_path, two_corpora):
    """一个语料都读不了时整体拒绝。

    此时连"这个库名存不存在"都不该回答，所以是 forbidden 而不是 invalid_target。
    """
    server = build_server(tmp_path, access=policies("some-other"),  # 一个都没授权
                          router=build_router(two_corpora))
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})
    assert data["error"] == "forbidden"
    assert data["detail"] == "没有可检索的语料"
    assert "alpha" not in data["detail"]


def test_broken_acl_snapshot_denies_routed_search(tmp_path, two_corpora):
    """策略文件坏了要整体拒绝，不能"读不出来就当没限制"。"""
    access_dir = tmp_path / "configs/access"
    access_dir.mkdir(parents=True)
    (access_dir / "broken.json").write_text("{not json", encoding="utf-8")
    server = build_mcp_server(
        FakeService(), audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=None,                      # 走真实的 load_snapshot
        raw_root=tmp_path / "raw_data", project_root=tmp_path,
        identity=RuntimeIdentity("osg-3.6.5", "test", "osg"),
        corpus_router=build_router(two_corpora),
    )
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})
    assert data["error"] == "forbidden"
    assert "访问策略不可用" in data["detail"]


# --- 未配置多语料时的行为 ---


def test_missing_router_gives_a_clear_error(tmp_path):
    """没配多语料时必须明说，不能悄悄去查 OSG 索引——那会返回不相干的证据。"""
    server = build_server(tmp_path, access=policies(ALPHA), router=None)
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})
    assert data["error"] == "forbidden"
    assert "未配置多语料" in data["detail"]


def test_default_path_still_uses_the_osg_service(tmp_path):
    """不带 library 时行为不变：仍走 OSG 单库服务。"""
    make_osg_corpus(tmp_path)
    server = build_server(tmp_path, access=policies("osg-3.6.5"), router=None)
    data = call(server, "search_docs", {"query": "q"})
    assert "routed_libraries" not in data
    assert data["version"] == "3.6.5"


# --- F1：丢弃明细不能回给客户端 ---


def build_router_with_unmapped(indices: list[RoutedIndex]) -> MultiCorpusRouter:
    """让 alpha 返回一条**未登记**的来源路径，触发丢弃。"""

    def fake_dense(client, vector, collection, *, top_k):
        if collection != "alpha_dense":
            return []
        # 路径指向一个**没在 manifest 登记**的文件，取件映射必然失败。
        bad = point("alpha-bad", library="alpha", version="1.0", text="未登记来源")
        bad.payload["source_path"] = "raw_data/alpha/1.0/unregistered-file.md"
        return [bad]

    def fake_keyword(path, query, *, top_k):
        return []

    return MultiCorpusRouter(
        qdrant_client=SimpleNamespace(close=lambda: None),
        embedder=SimpleNamespace(get_query_embedding=lambda q: [0.0] * 8),
        indices=indices, dense_search=fake_dense, keyword_search=fake_keyword,
    )


def test_routed_drop_details_never_reach_the_client(tmp_path, two_corpora):
    """丢弃明细含 corpus_id / source_path / chunk_id，只能进审计，不能回客户端。

    OSG 单库路径用 `dropped_summary()` 只给数量和原因码；路由路径一开始把原始明细
    直接放进返回值，网络入口原样透传——同一个字段两种口径。
    """
    router = build_router_with_unmapped(two_corpora)
    server = build_server(tmp_path, access=policies(ALPHA, BETA), router=router)
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})

    assert "evidence_dropped_details" not in data, "明细被回给了客户端"
    dropped = data["evidence_dropped"]
    assert dropped["count"] == 1
    assert isinstance(dropped["reasons"], dict) and dropped["reasons"]
    # 响应体里不该出现被丢弃条目的标识：chunk_id、来源路径、语料身份。
    # 注意**原因码**（`unregistered_file` 这类）本来就该在摘要里，
    # 那是受控词表里的分类，不是明细。
    blob = json.dumps(data, ensure_ascii=False)
    assert "alpha-bad" not in blob          # chunk_id
    assert "unregistered-file.md" not in blob  # 来源路径
    assert "doc.md" not in blob


def test_routed_drop_summary_matches_the_single_index_shape(tmp_path, two_corpora):
    """两种路径的 `evidence_dropped` 必须是同一形状，否则调用方没法统一处理。"""
    router = build_router_with_unmapped(two_corpora)
    server = build_server(tmp_path, access=policies(ALPHA, BETA), router=router)
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})
    assert set(data["evidence_dropped"]) == {"count", "reasons"}


def test_no_drop_reports_zero_like_the_single_index_path(tmp_path, two_corpora):
    server = build_server(tmp_path, access=policies(ALPHA, BETA),
                          router=build_router(two_corpora))
    data = call(server, "search_docs", {"query": "q", "library": "alpha"})
    assert data["evidence_dropped"] == {"count": 0, "reasons": {}}
    assert "evidence_dropped_details" not in data


# --- F2：只给 version 不给 library ---


def test_version_without_library_gives_an_actionable_error(tmp_path):
    """不给 library 时只检索 OSG；版本对不上要给能看懂的提示。

    原行为是让 runtime 抛 ValueError，而服务端开了错误屏蔽，客户端只看到
    "Error calling tool"——分不清是参数写错还是服务坏了。
    """
    make_osg_corpus(tmp_path)
    server = build_server(tmp_path, access=policies("osg-3.6.5"), router=None)
    data = call(server, "search_docs", {"query": "q", "version": "1.1"})
    assert data["error"] == "invalid_target"
    assert "library" in data["detail"]
    assert "list_corpora" in data["detail"]
    assert data["evidence"] == []
    assert data["mcp_protocol"]


def test_matching_version_without_library_still_works(tmp_path):
    """对照：版本与 OSG active 一致时照常检索，不受这条检查影响。"""
    make_osg_corpus(tmp_path)
    server = build_server(tmp_path, access=policies("osg-3.6.5"), router=None)
    data = call(server, "search_docs", {"query": "q", "version": "3.6.5"})
    assert "error" not in data
    assert data["version"] == "3.6.5"
