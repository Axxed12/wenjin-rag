"""启动本地/内网模型API：python -m wenjin.model_api serve --root PATH。"""

from __future__ import annotations

import argparse
from pathlib import Path

from wenjin.model_api.server import create_model_api_app, validate_model_api_listen
from wenjin.settings import Settings


class ModelAPISettings(Settings):
    """模型节点不持有Qdrant凭据；其余模型配置沿用项目同一Settings。"""

    qdrant_api_key: str = ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wenjin.model_api")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="启动Bearer保护的Qwen模型HTTP API")
    serve.add_argument("--root", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.error("未知命令")
    settings = ModelAPISettings.for_project_root(args.root)
    token_secret = settings.model_api_token
    token = token_secret.get_secret_value() if token_secret is not None else ""
    validate_model_api_listen(args.host, token)
    if settings.model_provider != "local":
        parser.error("模型API服务端必须配置WENJIN_MODEL_PROVIDER=local")
    import uvicorn

    app = create_model_api_app(root=args.root, settings=settings, token=token)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
