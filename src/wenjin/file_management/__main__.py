"""`python -m wenjin.file_management` 本机管理入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

from wenjin.file_management.api import create_app, validate_listen_security

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_URL = "http://127.0.0.1:8777"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m wenjin.file_management")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="启动本机文件管理HTTP服务")
    serve.add_argument(
        "--root", type=Path, default=Path(os.getenv("WENJIN_PROJECT_ROOT", PROJECT_ROOT))
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8777)
    serve.add_argument(
        "--identity-db",
        type=Path,
        default=Path(os.environ["WENJIN_IDENTITY_DB"]) if os.getenv("WENJIN_IDENTITY_DB") else None,
        help="显式启用本地用户/授权模式的SQLite数据库路径；不自动创建首位管理员",
    )
    serve.add_argument("--token", default=os.getenv("WENJIN_FILE_ADMIN_TOKEN"))
    serve.add_argument(
        "--max-upload-bytes",
        type=int,
        default=int(os.getenv("WENJIN_FILE_MAX_UPLOAD_BYTES", 104857600)),
    )

    bootstrap = sub.add_parser("bootstrap-admin", help="初始化本地身份库并创建首位管理员")
    bootstrap.add_argument("--identity-db", type=Path, required=True)
    bootstrap.add_argument("--username", required=True)

    for name in (
        "list",
        "show",
        "upload",
        "replace",
        "delete",
        "adopt",
        "retry",
        "job",
        "search",
        "download",
    ):
        item = sub.add_parser(name)
        item.add_argument("--url", default=DEFAULT_URL, help="文件管理服务base URL")
        item.add_argument("--token", default=os.getenv("WENJIN_FILE_ADMIN_TOKEN"))
        if name in {"show", "delete", "adopt"}:
            item.add_argument("file_id")
        elif name == "retry":
            item.add_argument("job_id")
        elif name == "job":
            item.add_argument("job_id")
        elif name == "upload":
            item.add_argument("path", type=Path)
            item.add_argument("--name")
        elif name == "replace":
            item.add_argument("file_id")
            item.add_argument("path", type=Path)
            item.add_argument("--name")
        elif name == "search":
            item.add_argument("file_id")
            item.add_argument("query")
        elif name == "download":
            item.add_argument("file_id")
            item.add_argument("output", type=Path)
            item.add_argument("--path", help="manifest登记的相对原文路径；单文件可省略")
    return parser


def _run_serve(args) -> int:
    token = args.token or os.getenv("WENJIN_FILE_ADMIN_TOKEN")
    validate_listen_security(args.host, token)
    if not 1 <= args.port <= 65535:
        raise ValueError("port必须在1到65535之间")
    if args.max_upload_bytes < 1:
        raise ValueError("max-upload-bytes必须大于0")
    import uvicorn

    app = create_app(args.root.resolve(), admin_token=token, max_upload_bytes=args.max_upload_bytes,
                     identity_db=args.identity_db.resolve() if args.identity_db else None)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, log_level="info")
    return 0


def _run_request(args) -> int:
    base = args.url.rstrip("/")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    timeout = httpx.Timeout(60.0, connect=5.0)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        if args.command == "list":
            response = client.get(f"{base}/api/files", headers=headers)
        elif args.command == "show":
            response = client.get(
                f"{base}/api/files/{quote(args.file_id, safe='')}", headers=headers
            )
        elif args.command in {"upload", "replace"}:
            path = args.path.resolve(strict=True)
            name = args.name or path.name
            encoded_name = quote(name, safe="")
            headers.update(
                {"X-File-Name": encoded_name, "Content-Type": "application/octet-stream"}
            )
            # HTTPX从文件句柄分块读取，CLI不会把大文件一次性装进内存。
            with path.open("rb") as stream:
                url = (
                    f"{base}/api/files"
                    if args.command == "upload"
                    else f"{base}/api/files/{quote(args.file_id, safe='')}"
                )
                method = client.post if args.command == "upload" else client.put
                response = method(url, headers=headers, content=stream)
        elif args.command == "delete":
            response = client.delete(
                f"{base}/api/files/{quote(args.file_id, safe='')}", headers=headers
            )
        elif args.command == "adopt":
            response = client.post(
                f"{base}/api/files/{quote(args.file_id, safe='')}/adopt", headers=headers
            )
        elif args.command == "retry":
            response = client.post(
                f"{base}/api/jobs/{quote(args.job_id, safe='')}/retry", headers=headers
            )
        elif args.command == "job":
            response = client.get(f"{base}/api/jobs/{quote(args.job_id, safe='')}", headers=headers)
        elif args.command == "search":
            response = client.post(
                f"{base}/api/search",
                headers=headers,
                json={"file_id": args.file_id, "query": args.query},
            )
        elif args.command == "download":
            query = {"path": args.path} if args.path else None
            response = client.get(
                f"{base}/api/files/{quote(args.file_id, safe='')}/download",
                headers=headers,
                params=query,
            )
            if response.is_success:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(response.content)
                print(f"已保存 {len(response.content)} 字节到 {args.output}")
                return 0
        else:
            raise ValueError(f"不支持命令: {args.command}")
    if response.headers.get("content-type", "").startswith("application/json"):
        try:
            print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        except ValueError:
            print(response.text)
    else:
        print(response.text)
    return 0 if response.is_success else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _run_serve(args)
        if args.command == "bootstrap-admin":
            from getpass import getpass

            from wenjin.identity import IdentityStore

            password = getpass("首位管理员密码: ")
            confirm = getpass("再次输入密码: ")
            if password != confirm:
                raise ValueError("两次输入的密码不一致")
            identity = IdentityStore(args.identity_db.resolve())
            identity.initialize()
            user = identity.bootstrap_first_admin(args.username, password)
            print(json.dumps({"user_id": user.user_id, "username": user.username, "role": user.role},
                             ensure_ascii=False))
            return 0
        return _run_request(args)
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"文件管理命令失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
