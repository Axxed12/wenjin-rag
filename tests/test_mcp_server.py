import asyncio

from fastmcp import Client

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


def test_mcp_initialize_discovers_and_calls_search_tool(tmp_path):
    async def scenario():
        server = build_mcp_server(FakeService(), audit=SearchAudit(tmp_path / "audit.jsonl"))
        async with Client(server) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools] == ["search_docs"]
            result = await client.call_tool("search_docs", {"query": "Node mask", "top_k": 2})
            data = result.structured_content
            assert data["query"] == "Node mask"
            assert data["mcp_protocol"] == EXPECTED_PROTOCOL_VERSION
            assert data["answer_status"] == "not_generated"
            assert len(data["request_id"]) == 32

    asyncio.run(scenario())
    assert '"tool":"search_docs"' in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
