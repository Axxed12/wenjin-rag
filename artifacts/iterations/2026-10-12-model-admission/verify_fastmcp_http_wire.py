from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import uvicorn
from fastmcp import FastMCP

from wenjin.mcp.tools import _model_tool_error

mcp = FastMCP("docker-fastmcp-wire-contract")


def decode_rpc(response):
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            value = json.loads(line[5:].strip())
            if isinstance(value, dict) and "jsonrpc" in value:
                return value
    raise RuntimeError("MCP response did not contain a JSON-RPC message")


@mcp.tool()
def search_docs(query: str):
    return _model_tool_error("model_queue_full", query, request_id="d" * 32)


with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
app = mcp.http_app(path="/mcp")
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
try:
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("synthetic FastMCP HTTP server did not start")
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        initialized = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "docker-contract", "version": "1"}},
        })
        if initialized.status_code != 200:
            raise RuntimeError(f"initialize_http_{initialized.status_code}")
        init_rpc = decode_rpc(initialized)
        if not isinstance(init_rpc, dict) or init_rpc.get("result", {}).get("protocolVersion") != "2025-11-25":
            raise RuntimeError("negotiated_protocol_version_mismatch")
        sid = initialized.headers.get("MCP-Session-Id") or initialized.headers.get("Mcp-Session-Id")
        if not sid:
            raise RuntimeError("stateful_session_id_missing")
        session_headers = headers | {"MCP-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25"}
        ready = client.post("/mcp", headers=session_headers,
                            json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        if ready.status_code not in {200, 202}:
            raise RuntimeError(f"initialized_http_{ready.status_code}")
        called = client.post("/mcp", headers=session_headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "search_docs", "arguments": {"query": "synthetic"}},
        })
        if called.status_code != 200:
            raise RuntimeError(f"tools_call_http_{called.status_code}")
        rpc = decode_rpc(called)
        result = rpc.get("result", {})
        structured = result.get("structuredContent", {})
        if result.get("isError") is not True or structured.get("error") != "model_queue_full":
            raise RuntimeError("MCP error wire contract failed")
        print(json.dumps({"protocol": "2025-11-25", "stateful_session": True,
                          "result_isError": result.get("isError"),
                          "structured_error": structured.get("error"),
                          "evidence_status": structured.get("evidence_status"),
                          "evidence_count": len(structured.get("evidence", []))}, separators=(",", ":")))
finally:
    server.should_exit = True
    thread.join(timeout=5)
    if thread.is_alive():
        raise RuntimeError("synthetic FastMCP HTTP server did not stop")
