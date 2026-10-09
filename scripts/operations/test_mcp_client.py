"""用独立HTTP客户端验证状态化MCP初始化、工具发现和真实检索。"""

import argparse
import asyncio
import json
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports.http import StreamableHttpTransport


async def run(
    url: str,
    query: str,
    *,
    api_key: str | None = None,
    ca_cert: Path | None = None,
) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    transport = StreamableHttpTransport(
        url,
        headers=headers,
        verify=str(ca_cert) if ca_cert else True,
    )
    client = Client(transport, timeout=120)
    async with client:
        tools = await client.list_tools()
        session_id = client.transport.get_session_id()
        result = await client.call_tool("search_docs", {"query": query, "top_k": 2})
        return {
            "url": url,
            "session_established": bool(session_id),
            "session_id_length": len(session_id) if session_id else 0,
            "tools": [tool.name for tool in tools],
            "result": result.structured_content,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--api-key", help="Bearer密钥；也可通过WENJIN_MCP_API_KEY环境变量")
    parser.add_argument("--ca-cert", type=Path, help="自签名CA/证书PEM路径")
    args = parser.parse_args()
    import os

    api_key = args.api_key or os.getenv("WENJIN_MCP_API_KEY")
    text = json.dumps(
        asyncio.run(run(args.url, args.query, api_key=api_key, ca_cert=args.ca_cert)),
        ensure_ascii=False,
        indent=2,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
