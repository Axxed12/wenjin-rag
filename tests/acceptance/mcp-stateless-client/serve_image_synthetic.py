"""Synthetic app builder run inside the released MCP image for TLS/WAL checks."""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.identity.store import IdentityStore
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.stateless_server import build_stateless_mcp_server, create_stateless_app


class SyntheticLibraryBridge:
    account_mode = True

    def list_libraries(self, *, authorization: str | None = None):
        if not authorization:
            raise RuntimeError("per-request authorization missing")
        return [{"library_id": "synthetic-library"}]


def main() -> None:
    root = Path("/tmp/mcp-stateless-container-fixture")
    root.mkdir(parents=True, exist_ok=True)
    store = IdentityStore(Path("/var/lib/wenjin/identity.sqlite"))
    service = SimpleNamespace(
        supported_version="synthetic-container-v1",
        index_target_id="synthetic-container-v1",
        reranker=None,
        search_policy=None,
    )
    server = build_stateless_mcp_server(
        service=service,
        identity=RuntimeIdentity(
            corpus_id="synthetic-container-corpus",
            corpus_id_source="acceptance-fixture",
            target_id="synthetic-container-v1",
            embedding_model="synthetic-only",
        ),
        identity_store=store,
        managed_bridge=SyntheticLibraryBridge(),
        audit=SearchAudit(root / "audit.jsonl"),
        access=AccessRegistry(root / "access", {}),
        access_dir=root / "access",
        project_root=Path("/app"),
    )
    app = create_stateless_app(path="/mcp-stateless", server=server, identity_store=store,
                               allowed_hosts=["127.0.0.1", "localhost", "mcp-stateless.test"])
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 18768))
    uvicorn.run(app, host="0.0.0.0", port=18768, log_level="warning")


if __name__ == "__main__":
    main()
