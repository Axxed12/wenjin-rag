"""启动本机手动Agent服务；只保留一份GPU模型，凭据不输出。"""

import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SECRET = ROOT / "deploy/secrets/manual-agent"
IDENTITY = ROOT / "data/file-management/manual-agent-identity.sqlite"


def main():
    SECRET.mkdir(parents=True, exist_ok=True)
    token_file = SECRET / "model_api_token"
    if not token_file.exists():
        token_file.write_text(secrets.token_hex(32), encoding="ascii")
    model = {
        "image": "wenjin-model-node:dual-smoke-20261008",
        "container_name": "wenjin-manual-model",
        "restart": "unless-stopped",
        "gpus": "all",
        "command": [
            "python",
            "-m",
            "wenjin.model_api",
            "serve",
            "--root",
            "/app",
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
        ],
        "ports": ["127.0.0.1:18766:8765"],
        "environment": {
            "PYTHONPATH": "/app/src",
            "WENJIN_MODEL_PROVIDER": "local",
            "EMBEDDING_MODEL_PATH": "/models/embedding",
            "RERANKER_MODEL_PATH": "/models/reranker",
            "EMBEDDING_MODEL_ID": "Qwen3-Embedding-0.6B",
            "RERANKER_MODEL_ID": "Qwen3-Reranker-0.6B",
            "QDRANT_API_KEY": "",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "WENJIN_MODEL_API_MAX_WAITING": "8",
            "WENJIN_MODEL_API_MAX_WAIT_SECONDS": "30",
        },
        "secrets": ["model_api_token"],
        "volumes": [
            f"{ROOT.as_posix()}/src:/app/src:ro",
            "E:/Models/Qwen3-Embedding-0.6B:/models/embedding:ro",
            "E:/Models/Qwen3-Reranker-0.6B:/models/reranker:ro",
        ],
        "read_only": True,
        "tmpfs": ["/tmp:size=1g"],
        "shm_size": "1gb",
        "stop_grace_period": "120s",
    }
    config = {
        "services": {"model": model},
        "secrets": {"model_api_token": {"file": token_file.as_posix()}},
    }
    compose = OUT / "compose.json"
    compose.write_text(json.dumps(config, indent=2), encoding="utf-8")
    subprocess.run(
        ["docker", "compose", "-p", "wenjin-manual", "-f", str(compose), "up", "-d"], check=True
    )
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "WENJIN_MODEL_PROVIDER": "remote",
            "WENJIN_MODEL_API_URL": "http://127.0.0.1:18766",
            "WENJIN_MODEL_API_TOKEN": token_file.read_text().strip(),
            "WENJIN_IDENTITY_DB": str(IDENTITY),
            "WENJIN_FILE_MANAGEMENT_URL": "http://127.0.0.1:18777",
            "WENJIN_MCP_ALLOWED_HOSTS": "127.0.0.1:18765,localhost:18765",
            "WENJIN_AUDIT_INCLUDE_QUERY": "0",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    env.pop("WENJIN_MCP_API_KEY", None)
    env.update(
        {"EMBEDDING_MODEL_ID": "Qwen3-Embedding-0.6B", "RERANKER_MODEL_ID": "Qwen3-Reranker-0.6B"}
    )
    import time
    from urllib.request import Request, urlopen

    ready = False
    for _ in range(120):
        try:
            req = Request(
                "http://127.0.0.1:18766/v1/identity",
                headers={"Authorization": "Bearer " + env["WENJIN_MODEL_API_TOKEN"]},
            )
            with urlopen(req, timeout=2) as response:
                identity = json.load(response)
            (OUT / "model-identity.json").write_text(
                json.dumps(identity, indent=2), encoding="utf-8"
            )
            ready = True
            break
        except Exception:
            time.sleep(1)
    if not ready:
        raise RuntimeError("model_not_ready; backend services not started")
    processes = {}
    for name, module, port in [
        ("management", "wenjin.file_management", "18777"),
        ("mcp", "wenjin.mcp_server", "18765"),
    ]:
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
                existing = json.loads((OUT / "processes.json").read_text())
                processes[name] = existing[name]
                continue
        except OSError:
            pass
        args = [sys.executable, "-m", module]
        if name == "management":
            args += ["serve", "--root", str(ROOT)]
        args += ["--host", "127.0.0.1", "--port", port, "--identity-db", str(IDENTITY)]
        with (
            (OUT / f"{name}.stdout.log").open("ab") as stdout,
            (OUT / f"{name}.stderr.log").open("ab") as stderr,
        ):
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        processes[name] = {"pid": process.pid, "port": int(port), "module": module}
    (OUT / "processes.json").write_text(json.dumps(processes, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"started": processes, "identity_db": str(IDENTITY), "model_lanes": 1},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
