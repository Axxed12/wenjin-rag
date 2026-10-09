"""Loopback-only live acceptance server with an explicit file-based failpoint.

This helper is not the production CLI and is not included in the application entrypoint.
It loads the local embedding model only in the single server process that builds indexes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import uvicorn

from wenjin.file_management import FileManagementService
from wenjin.management_http.app import _LazyEmbedder, create_app
from wenjin.qdrant_connection import create_qdrant_client
from wenjin.settings import Settings


def _fault_hook(control_path: Path | None):
    def invoke(phase: str, context: dict) -> None:
        if control_path is None or not control_path.is_file():
            return
        try:
            config = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if config.get("phase") != phase:
            return
        action = config.get("action", "raise")
        if action == "raise":
            control_path.unlink(missing_ok=True)
            raise OSError(f"live acceptance failpoint at {phase}")
        if action == "pause":
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--fault-control", type=Path)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("acceptance helper only binds to loopback")
    settings = Settings()
    qdrant = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    manager = FileManagementService(
        root=args.root.resolve(),
        qdrant_client=qdrant,
        embedder=_LazyEmbedder(settings),
        embedding_model=settings.embedding_model_path.name,
        fault_hook=_fault_hook(args.fault_control.resolve() if args.fault_control else None),
    )
    app = create_app(args.root.resolve(), manager=manager)
    try:
        uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    finally:
        manager.close()
        qdrant.close()


if __name__ == "__main__":
    main()
