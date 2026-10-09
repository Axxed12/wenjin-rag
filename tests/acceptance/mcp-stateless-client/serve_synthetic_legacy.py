"""Loopback-only synthetic 2025-11-25 server, launched with the project SDK 1 env."""

from __future__ import annotations

import io
import socket
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from starlette.middleware import Middleware

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.auth import BearerAuthMiddleware
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.server import build_mcp_server


TOKEN = "synthetic-only-legacy-token-2026"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="synthetic-mcp-legacy-") as temp:
        root = Path(temp)
        service = SimpleNamespace(
            supported_version="synthetic-v1",
            index_target_id="synthetic-legacy-target",
            reranker=None,
            search_policy=None,
        )
        server = build_mcp_server(
            service=service,
            identity=RuntimeIdentity(
                corpus_id="synthetic-legacy-corpus",
                corpus_id_source="acceptance-fixture",
                target_id="synthetic-legacy-target",
                embedding_model="synthetic-only",
            ),
            audit=SearchAudit(stream=io.StringIO()),
            access=AccessRegistry(root / "access", {}),
            access_dir=root / "access",
            project_root=root,
            bearer_enabled=True,
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        host = f"127.0.0.1:{port}"
        app = server.http_app(
            path="/mcp",
            middleware=[Middleware(
                BearerAuthMiddleware,
                api_key=TOKEN,
                client_id="synthetic-legacy-client",
            )],
            stateless_http=False,
            json_response=False,
            transport="streamable-http",
            host_origin_protection=True,
            allowed_hosts=[host, "127.0.0.1"],
        )
        print(f"synthetic legacy MCP ready at http://{host}/mcp", flush=True)
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
