"""Loopback-only subprocess helpers for live managed MCP acceptance."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import uvicorn


def fault_hook(control_path: Path | None):
    def invoke(phase: str, context: dict) -> None:
        if control_path is None or not control_path.is_file():
            return
        try:
            config = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if config.get("phase") != phase:
            return
        if config.get("action", "raise") == "raise":
            control_path.unlink(missing_ok=True)
            raise OSError(f"managed MCP acceptance failpoint at {phase}")
        if config.get("action") == "pause":
            ready = Path(config["ready_file"])
            release = Path(config["release_file"])
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text(json.dumps({"phase": phase, "context": context}), encoding="utf-8")
            while not release.exists():
                time.sleep(0.05)
            control_path.unlink(missing_ok=True)
            return
        raise ValueError("acceptance failpoint action must be raise or pause")

    return invoke


def run_manager(args: argparse.Namespace) -> None:
    from wenjin.file_management import FileManagementService
    from wenjin.management_http.app import _LazyEmbedder, create_app
    from wenjin.qdrant_connection import create_qdrant_client
    from wenjin.settings import Settings

    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("acceptance manager only binds to loopback")
    root = args.root.resolve()
    settings = Settings()
    qdrant = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_LazyEmbedder(settings),
        embedding_model=settings.embedding_model_path.name,
        fault_hook=fault_hook(args.fault_control.resolve() if args.fault_control else None),
    )
    app = create_app(root, admin_token=args.admin_token, manager=manager)
    try:
        uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="warning")
    finally:
        manager.close()
        qdrant.close()


def run_mcp(args: argparse.Namespace) -> None:
    from starlette.middleware import Middleware

    from wenjin.audit import SearchAudit
    from wenjin.auth import BearerAuthMiddleware
    from wenjin.managed_mcp import ManagedFileHTTPClient
    from wenjin.mcp_server import build_mcp_server, create_router, create_runtime

    root = args.project_root.resolve()
    runtime, identity = create_runtime(use_reranker=False, root=root)
    audit = SearchAudit.from_environment(root)
    bridge = ManagedFileHTTPClient.from_environment()
    try:
        router = create_router(
            root=root,
            qdrant_client=runtime.qdrant_client,
            embedder=runtime.embedder,
            reranker=None,
            search_lock=runtime.search_lock,
        )
        server = build_mcp_server(
            runtime,
            audit=audit,
            project_root=root,
            access_dir=args.access_dir.resolve(),
            managed_bridge=bridge,
            bearer_enabled=True,
            identity=identity,
            corpus_router=router,
        )
        api_key = args.mcp_api_key
        server.run(
            transport="streamable-http",
            show_banner=False,
            host=args.host,
            port=args.port,
            path="/mcp",
            stateless_http=False,
            json_response=False,
            host_origin_protection=True,
            allowed_hosts=[f"{args.host}:{args.port}"],
            middleware=[Middleware(
                BearerAuthMiddleware, api_key=api_key, client_id=args.client_id
            )],
        )
    finally:
        bridge.close()
        audit.close()
        runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("manager", "mcp"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--access-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--admin-token", default="")
    parser.add_argument("--mcp-api-key", default="")
    parser.add_argument("--client-id", default="managed-mcp-authorized")
    parser.add_argument("--fault-control", type=Path)
    args = parser.parse_args()
    if args.mode == "manager":
        if args.root is None or not args.admin_token:
            parser.error("manager requires --root and --admin-token")
        run_manager(args)
    else:
        if args.project_root is None or args.access_dir is None or not args.mcp_api_key:
            parser.error("mcp requires --project-root, --access-dir, and --mcp-api-key")
        run_mcp(args)


if __name__ == "__main__":
    main()
