import asyncio
import hashlib
import json

from fastmcp import Client

from wenjin.access import AccessPolicy, AccessRegistry, write_policy
from wenjin.audit import SearchAudit
from wenjin.mcp_server import EXPECTED_PROTOCOL_VERSION, RuntimeIdentity, build_mcp_server

CORPUS_ID = "osg-3.6.5"
IDENTITY = RuntimeIdentity(CORPUS_ID, "test", "osg-3.6.5-blocks-dualhash-003")
GUIDE = b"# Guide\n"


class FakeService:
    def search(self, query, *, config):
        return {
            "query": query,
            "version": config.version,
            "evidence": [{"chunk_id": "c1", "source_path": "include/osg/Node",
                          "start_line": 1, "end_line": 2, "text": "class Node;"}],
            "answer_status": "not_generated",
        }


def open_access() -> AccessRegistry:
    """测试用：放行所有已认证客户端。生产默认是拒绝，必须显式给策略。"""
    return AccessRegistry(None, {CORPUS_ID: AccessPolicy(CORPUS_ID, "public", ("*",))})


def make_project(tmp_path, *, raw_sha256: str) -> None:
    """最小项目：一个语料 + manifest登记记录，供取件与语料目录使用。"""
    raw_dir = tmp_path / "raw_data" / "osg" / "3.6.5" / "include" / "osg"
    raw_dir.mkdir(parents=True)
    (raw_dir / "Node").write_bytes(GUIDE)
    manifest_dir = tmp_path / "corpora" / CORPUS_ID
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "source-manifest.json").write_text(json.dumps({
        "schema_version": 3, "corpus_id": CORPUS_ID, "library": "osg", "version": "3.6.5",
        "raw_data_subdir": "osg/3.6.5", "license": "OSGPL",
        "files": [{"path": "include/osg/Node", "raw_sha256": raw_sha256,
                   "bytes": len(GUIDE), "retrieval_eligible": True, "indexable": True}],
    }), encoding="utf-8")


def server_for(tmp_path, *, access: AccessRegistry, bearer_enabled: bool = False):
    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    return build_mcp_server(
        FakeService(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=access,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        bearer_enabled=bearer_enabled,
        identity=IDENTITY,
    )


def test_mcp_initialize_discovers_and_calls_search_tool(tmp_path):
    async def scenario():
        server = server_for(tmp_path, access=open_access())
        async with Client(server) as client:
            tools = await client.list_tools()
            assert sorted(tool.name for tool in tools) == ["list_corpora", "search_docs"]
            result = await client.call_tool("search_docs", {"query": "Node mask", "top_k": 2})
            data = result.structured_content
            assert data["query"] == "Node mask"
            assert data["mcp_protocol"] == EXPECTED_PROTOCOL_VERSION
            assert data["answer_status"] == "not_generated"
            assert len(data["request_id"]) == 32

    asyncio.run(scenario())
    assert '"tool":"search_docs"' in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_search_evidence_carries_download_path(tmp_path):
    """OSG的裸source_path也要能生成取件链接——这是它唯一能定位原文的入口。"""
    async def scenario():
        server = server_for(tmp_path, access=open_access())
        async with Client(server) as client:
            result = await client.call_tool("search_docs", {"query": "Node mask"})
            evidence = result.structured_content["evidence"]
            assert evidence[0]["download_path"] == f"/files/{CORPUS_ID}/include/osg/Node"
            assert evidence[0]["corpus_id"] == CORPUS_ID
            assert result.structured_content["evidence_dropped"] == {"count": 0,
                                                                     "reasons": {}}

    asyncio.run(scenario())


def test_foreign_evidence_is_dropped_not_relabelled(tmp_path):
    """混入的证据不属于当前语料时丢弃并报告，绝不能贴上当前语料的名字返回。"""
    class LeakyService(FakeService):
        def search(self, query, *, config):
            result = super().search(query, config=config)
            result["evidence"].append({
                "chunk_id": "foreign", "source_path": "raw_data/proj/9.8.1/README.md",
                "corpus_id": "proj-9.8.1", "start_line": 1, "end_line": 2, "text": "secret",
            })
            return result

    async def scenario():
        make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
        server = build_mcp_server(
            LeakyService(), audit=SearchAudit(tmp_path / "audit.jsonl"), access=open_access(),
            raw_root=tmp_path / "raw_data", project_root=tmp_path, identity=IDENTITY,
        )
        async with Client(server) as client:
            data = (await client.call_tool("search_docs", {"query": "x"})).structured_content
            assert [item["chunk_id"] for item in data["evidence"]] == ["c1"]
            # 客户端只收到数量和原因码：私有语料名与路径不能出现在响应里。
            dropped = data["evidence_dropped"]
            assert dropped["count"] == 1
            assert dropped["reasons"] == {"foreign_corpus": 1}
            serialized = json.dumps(data, ensure_ascii=False)
            assert "secret" not in serialized
            assert "proj-9.8.1" not in serialized
            assert "README.md" not in serialized

    asyncio.run(scenario())


def test_search_tool_denies_client_without_policy(tmp_path):
    """默认拒绝：没有策略文件时检索也不放行，只锁下载是没用的。

    服务端开了mask_error_details，所以拒绝必须是结构化结果而不是异常，
    否则客户端只看到"调用失败"，分不清被拒和服务故障。
    """
    async def scenario():
        server = server_for(tmp_path, access=AccessRegistry(None, {}))
        async with Client(server) as client:
            result = await client.call_tool("search_docs", {"query": "Node mask"})
            data = result.structured_content
            assert data["error"] == "forbidden"
            assert data["evidence"] == []
            assert data["evidence_status"] == "not_retrieved"

    asyncio.run(scenario())


def test_search_fails_closed_when_corpus_cannot_be_determined(tmp_path):
    """身份无法确定时不能"跳过检查继续检索"——那等于把默认拒绝变成默认放行。"""
    async def scenario():
        # 既没有传identity，project_root下也没有注册表
        server = build_mcp_server(
            FakeService(),
            audit=SearchAudit(tmp_path / "audit.jsonl"),
            access=open_access(),
            project_root=tmp_path,
        )
        async with Client(server) as client:
            data = (await client.call_tool("search_docs", {"query": "Node mask"})).structured_content
            assert data["error"] == "forbidden"
            assert data["evidence"] == []
            assert "索引身份" in data["detail"]

    asyncio.run(scenario())


def test_search_fails_closed_on_corrupt_policy_snapshot(tmp_path):
    """策略文件损坏时检索拒绝，不回退到上一次成功的快照。"""
    async def scenario():
        make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
        access_dir = tmp_path / "configs" / "access"
        write_policy(access_dir, AccessPolicy(CORPUS_ID, "public", ("*",)))
        server = build_mcp_server(
            FakeService(), audit=SearchAudit(tmp_path / "audit.jsonl"),
            raw_root=tmp_path / "raw_data", project_root=tmp_path, identity=IDENTITY,
        )
        async with Client(server) as client:
            allowed = await client.call_tool("search_docs", {"query": "Node mask"})
            assert allowed.structured_content.get("error") is None

            (access_dir / "broken-9.9.json").write_text("{not json", encoding="utf-8")
            denied = await client.call_tool("search_docs", {"query": "Node mask"})
            assert denied.structured_content["error"] == "forbidden"

    asyncio.run(scenario())


def test_list_corpora_hides_corpora_without_permission(tmp_path):
    async def scenario():
        server = server_for(
            tmp_path,
            access=AccessRegistry(None, {
                CORPUS_ID: AccessPolicy(CORPUS_ID, "internal", ("someone-else",))}),
        )
        async with Client(server) as client:
            result = await client.call_tool("list_corpora", {})
            assert result.structured_content["corpora"] == []

    asyncio.run(scenario())


def test_list_corpora_revocation_takes_effect_on_next_call(tmp_path):
    """列表路径的撤权也要立刻生效：同一进程内授权→可见→撤权→不可见。"""
    async def scenario():
        make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
        access_dir = tmp_path / "configs" / "access"
        # 进程内调用没有HTTP头，身份是匿名的；用 `*` 表示撤权前匿名可读。
        write_policy(access_dir, AccessPolicy(CORPUS_ID, "public", ("*",)))
        server = build_mcp_server(
            FakeService(), audit=SearchAudit(tmp_path / "audit.jsonl"),
            raw_root=tmp_path / "raw_data", project_root=tmp_path, identity=IDENTITY,
        )
        async with Client(server) as client:
            before = await client.call_tool("list_corpora", {})
            assert before.structured_content["count"] == 1, "撤权前应可见"

            write_policy(access_dir, AccessPolicy(CORPUS_ID, "internal", ()))

            after = await client.call_tool("list_corpora", {})
            assert after.structured_content["corpora"] == [], "撤权后必须不可见"

    asyncio.run(scenario())


# --- 第五批：list_corpora 必须区分"有配置"与"真的能用" ---


def _fake_qdrant(*, exists: bool = True, points: int = 1):
    class Client:
        def collection_exists(self, name):
            return exists

        def count(self, name, *, count_filter, exact):
            return type("R", (), {"count": points})()

    return Client()


def add_index_assets(tmp_path, *, rows: int = 1, points: int = 1, collection: bool = True):
    """写出真实可校验的索引三资产：BM25 sqlite + 目标配置（manifest_sha256 对得上）。"""
    import sqlite3

    bm25 = tmp_path / "data/indexes/osg_3_6_5_bm25.sqlite"
    bm25.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(bm25) as database:
        database.execute("CREATE TABLE chunks(chunk_id, source_path)")
        for index in range(rows):
            database.execute("INSERT INTO chunks VALUES (?, ?)", (f"c{index}", "include/osg/Node"))
        database.commit()
    manifest = tmp_path / "corpora" / CORPUS_ID / "source-manifest.json"
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    config_dir = tmp_path / "configs/indexes"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "osg-3.6.5-blocks.json").write_text(json.dumps({
        "target_id": "osg-3.6.5-blocks", "corpus_id": CORPUS_ID, "library": "osg",
        "version": "3.6.5", "strategy": "blocks",
        "dense_collection": "osg_dense", "bm25_path": "data/indexes/osg_3_6_5_bm25.sqlite",
        "manifest_path": f"corpora/{CORPUS_ID}/source-manifest.json",
        "manifest_sha256": digest, "chunk_count": rows, "vector_dimension": 1024,
        "embedding_model": "fake",
    }), encoding="utf-8")


def preflight_service(*, client):
    class Service(FakeService):
        qdrant_client = client

    return Service()


def corpora_payload(tmp_path, *, service, access=None):
    """调用方自己先把项目文件铺好（make_project / add_index_assets）。"""
    server = build_mcp_server(
        service, audit=SearchAudit(tmp_path / "audit.jsonl"), access=access or open_access(),
        raw_root=tmp_path / "raw_data", project_root=tmp_path, identity=IDENTITY,
    )

    async def scenario():
        async with Client(server) as client:
            return (await client.call_tool("list_corpora", {})).structured_content

    return asyncio.run(scenario())


def test_list_corpora_reports_no_index_config(tmp_path):
    """没有索引配置提到它时，state 是 no_index_config，不是"可用"。"""
    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    data = corpora_payload(tmp_path, service=preflight_service(client=_fake_qdrant()))
    entry = data["corpora"][0]
    assert entry["index_state"] == "no_index_config"
    assert entry["has_index_config"] is False
    assert data["usable_count"] == 0


def test_list_corpora_reports_usable_after_preflight(tmp_path):
    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    add_index_assets(tmp_path)
    data = corpora_payload(tmp_path, service=preflight_service(client=_fake_qdrant()))
    entry = data["corpora"][0]
    assert entry["has_index_config"] is True
    assert entry["index_state"] == "usable"
    assert entry["index_usable_targets"] == ["osg-3.6.5-blocks"]
    assert data["usable_count"] == 1
    # 明确说出这不是完整准入，避免"usable"被当成验收结论。
    assert "NOT full admission" in data["index_state_note"]


def test_list_corpora_reports_unusable_when_collection_is_gone(tmp_path):
    """配置在、collection 没了：旧实现照样报 indexed=true，实际一条都检索不出来。"""
    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    add_index_assets(tmp_path)
    data = corpora_payload(
        tmp_path, service=preflight_service(client=_fake_qdrant(exists=False))
    )
    entry = data["corpora"][0]
    assert entry["has_index_config"] is True   # 配置确实在
    assert entry["index_state"] == "unusable"  # 但不能服务
    assert entry["index_failures"] == [
        {"target_id": "osg-3.6.5-blocks", "reason": "collection_missing"}
    ]


def test_list_corpora_reports_point_count_mismatch(tmp_path):
    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    add_index_assets(tmp_path, rows=1, points=7)
    data = corpora_payload(tmp_path, service=preflight_service(client=_fake_qdrant(points=7)))
    assert data["corpora"][0]["index_failures"] == [
        {"target_id": "osg-3.6.5-blocks", "reason": "point_count_mismatch"}
    ]


def test_list_corpora_reports_changed_manifest(tmp_path):
    """manifest 换过之后旧索引不该再被当成可用。"""
    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    add_index_assets(tmp_path)
    manifest = tmp_path / "corpora" / CORPUS_ID / "source-manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    data = corpora_payload(tmp_path, service=preflight_service(client=_fake_qdrant()))
    assert data["corpora"][0]["index_failures"] == [
        {"target_id": "osg-3.6.5-blocks", "reason": "manifest_changed"}
    ]


def test_list_corpora_reports_qdrant_unavailable(tmp_path):
    """拿不到 Qdrant 客户端时如实报不可用，不猜成可用。"""
    class NoClient(FakeService):
        pass

    make_project(tmp_path, raw_sha256=hashlib.sha256(GUIDE).hexdigest())
    data = corpora_payload(tmp_path, service=NoClient())
    entry = data["corpora"][0]
    assert entry["index_state"] in {"no_index_config", "unusable"}
