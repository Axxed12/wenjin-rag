"""Synthetic Python SDK 1.29 client for legacy compatibility/isolation checks."""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


LEGACY_TOKEN = "synthetic-only-legacy-token-2026"
STATELESS_TOKEN = "synthetic-only-stateless-token-2026"


def _tool_payload(result) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return {}


async def _legacy_success(url: str) -> dict:
    async with streamablehttp_client(
        url,
        headers={"Authorization": f"Bearer {LEGACY_TOKEN}"},
    ) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            session_id = get_session_id()
            listed = await session.list_tools()
            called = await session.call_tool("list_corpora", {})
            payload = _tool_payload(called)
            return {
                "sdkVersion": "1.29.0",
                "negotiatedProtocol": getattr(initialized, "protocolVersion", None),
                "sessionPresent": bool(session_id),
                "toolNames": [tool.name for tool in listed.tools],
                "callIsError": bool(getattr(called, "isError", False)),
                "callProtocol": payload.get("mcp_protocol"),
            }


async def _legacy_reject(url: str) -> dict:
    async with streamablehttp_client(
        url,
        headers={"Authorization": f"Bearer {STATELESS_TOKEN}"},
    ) as (read_stream, write_stream, get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            try:
                await session.initialize()
            except Exception as error:  # expected on the modern-only endpoint
                return {
                    "sdkVersion": "1.29.0",
                    "accepted": False,
                    "sessionPresent": bool(get_session_id()),
                    "errorType": type(error).__name__,
                }
            return {
                "sdkVersion": "1.29.0",
                "accepted": True,
                "sessionPresent": bool(get_session_id()),
            }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--expect-failure", action="store_true")
    args = parser.parse_args()
    try:
        result = await (_legacy_reject(args.url) if args.expect_failure
                        else _legacy_success(args.url))
    except Exception as error:
        if not args.expect_failure:
            raise
        result = {
            "sdkVersion": "1.29.0",
            "accepted": False,
            "errorType": type(error).__name__,
            "sessionPresent": False,
        }
    print(json.dumps(result, ensure_ascii=False))
    return int(args.expect_failure and result.get("accepted") is not False)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
