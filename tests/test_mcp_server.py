import asyncio

from fastmcp import Client

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.mcp_server import EXPECTED_PROTOCOL_VERSION, build_mcp_server


class FakeService:
    def search(self, query, *, config):
        return {
            "query": query,
            "version": config.version,
            "evidence": [{"source_path": "include/osg/Node", "start_line": 1, "end_line": 2}],
            "answer_status": "not_generated",
        }


def open_access() -> AccessRegistry:
    """测试用：放行所有已认证客户端。生产默认是拒绝，必须显式给策略。"""
    return AccessRegistry(
        None, {"osg-3.6.5": AccessPolicy("osg-3.6.5", "public", ("*",))}
    )


def test_mcp_initialize_discovers_and_calls_search_tool(tmp_path):
    async def scenario():
        server = build_mcp_server(
            FakeService(), audit=SearchAudit(tmp_path / "audit.jsonl"), access=open_access()
        )
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


def test_search_tool_denies_client_without_policy(tmp_path):
    """默认拒绝：没有策略文件时检索也不放行，只锁下载是没用的。

    服务端开了mask_error_details，所以拒绝必须是结构化结果而不是异常，
    否则客户端只看到"调用失败"，分不清被拒和服务故障。
    """
    async def scenario():
        server = build_mcp_server(
            FakeService(),
            audit=SearchAudit(tmp_path / "audit.jsonl"),
            access=AccessRegistry(None, {}),
        )
        async with Client(server) as client:
            result = await client.call_tool("search_docs", {"query": "Node mask"})
            data = result.structured_content
            assert data["error"] == "forbidden"
            assert data["evidence"] == []
            assert data["evidence_status"] == "not_retrieved"

    asyncio.run(scenario())


def test_search_fails_closed_when_corpus_cannot_be_determined(tmp_path):
    """注册表读不出来时不能"跳过检查继续检索"——那等于把默认拒绝变成默认放行。"""
    async def scenario():
        # project_root 下没有 data/indexes/index-registry.json
        server = build_mcp_server(
            FakeService(),
            audit=SearchAudit(tmp_path / "audit.jsonl"),
            access=open_access(),
            project_root=tmp_path,
        )
        async with Client(server) as client:
            result = await client.call_tool("search_docs", {"query": "Node mask"})
            data = result.structured_content
            assert data["error"] == "forbidden"
            assert data["evidence"] == []
            assert "确定目标语料" in data["detail"]

    asyncio.run(scenario())


def test_list_corpora_hides_corpora_without_permission(tmp_path):
    async def scenario():
        server = build_mcp_server(
            FakeService(),
            audit=SearchAudit(tmp_path / "audit.jsonl"),
            access=AccessRegistry(
                None, {"osg-3.6.5": AccessPolicy("osg-3.6.5", "internal", ("someone-else",))}
            ),
        )
        async with Client(server) as client:
            result = await client.call_tool("list_corpora", {})
            assert result.structured_content["corpora"] == []

    asyncio.run(scenario())
