"""Loopback-only synthetic listener for the pinned TypeScript MCP client."""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.stateless_server import build_stateless_mcp_server, create_stateless_app


def main() -> None:
    # This project root is temporary and intentionally contains no corpus/index files.
    with tempfile.TemporaryDirectory(prefix="synthetic-mcp-stateless-") as temp:
        root = Path(temp)
        service = SimpleNamespace(
            supported_version="synthetic-v1",
            index_target_id="synthetic-target-v1",
            reranker=None,
            search_policy=None,
        )
        server = build_stateless_mcp_server(
            service=service,
            identity=RuntimeIdentity(
                corpus_id="synthetic-corpus-v1",
                corpus_id_source="acceptance-fixture",
                target_id="synthetic-target-v1",
                embedding_model="synthetic-only",
            ),
            audit=SearchAudit(root / "audit.jsonl"),
            access=AccessRegistry(root / "access", {}),
            access_dir=root / "access",
            project_root=root,
            bearer_enabled=True,
        )
        app = create_stateless_app(
            path="/mcp-stateless",
            server=server,
            api_key="synthetic-only-stateless-token-2026",
            client_id="synthetic-client",
            allowed_hosts=["127.0.0.1"],
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        print(f"synthetic MCP ready at http://127.0.0.1:{port}/mcp-stateless", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
