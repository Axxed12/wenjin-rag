"""Static synthetic transport probe run from the released MCP image."""

from __future__ import annotations

import socket
import uvicorn
from pathlib import Path
from types import SimpleNamespace

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.stateless_server import build_stateless_mcp_server, create_stateless_app


TOKEN = "short-lived-static-image-probe-only"


def main() -> None:
    root = Path("/tmp/mcp-stateless-static-fixture")
    root.mkdir(parents=True, exist_ok=True)
    server = build_stateless_mcp_server(
        service=SimpleNamespace(
            supported_version="synthetic-image-v1", index_target_id="synthetic-image-v1",
            reranker=None, search_policy=None,
        ),
        identity=RuntimeIdentity(
            corpus_id="synthetic-image-corpus", corpus_id_source="acceptance-fixture",
            target_id="synthetic-image-v1", embedding_model="synthetic-only",
        ),
        audit=SearchAudit(root / "audit.jsonl"),
        access=AccessRegistry(root / "access", {}),
        access_dir=root / "access",
        project_root=root,
        bearer_enabled=True,
    )
    app = create_stateless_app(
        path="/mcp-stateless", server=server, api_key=TOKEN,
        client_id="synthetic-static-client",
        allowed_hosts=["localhost", "127.0.0.1", "127.0.0.1:443"],
    )
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 18768))
    uvicorn.run(app, host="0.0.0.0", port=18768, log_level="warning")


if __name__ == "__main__":
    main()
