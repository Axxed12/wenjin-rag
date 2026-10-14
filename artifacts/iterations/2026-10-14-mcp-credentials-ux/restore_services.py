"""仅在两端口空闲且无作业时恢复已确认拓扑，不停止任何进程。"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "artifacts/iterations/2026-10-12-library-visibility-consistency"))
from reload_services import processes
from runtime_check import inspect


def main():
    identity = ROOT / "data/file-management/manual-agent-identity.sqlite"
    if not identity.exists() or processes():
        raise RuntimeError("身份库缺失或已有监听，未启动服务")
    jobs = inspect()["jobs"]
    if any(item["active"] for item in jobs):
        raise RuntimeError("存在进行中的作业，未启动服务")
    token = (ROOT / "deploy/secrets/manual-agent/model_api_token").read_text().strip()

    def model_identity():
        with urlopen(Request("http://127.0.0.1:18766/v1/identity",
                             headers={"Authorization": "Bearer " + token}), timeout=5) as response:
            return json.load(response)

    model_before = model_identity()
    backup = identity.parent / "backups/identity-before-credential-ux-20261014.sqlite"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise RuntimeError("备份已存在，拒绝覆盖或重复启动")
    with closing(sqlite3.connect(identity.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup)) as destination:
            source.backup(destination)
    if processes():
        raise RuntimeError("备份期间出现其他监听，未启动第二个管理器")
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "src"), "WENJIN_MODEL_PROVIDER": "remote",
        "WENJIN_MODEL_API_URL": "http://127.0.0.1:18766", "WENJIN_MODEL_API_TOKEN": token,
        "WENJIN_IDENTITY_DB": str(identity), "WENJIN_FILE_MANAGEMENT_URL": "http://127.0.0.1:18777",
        "WENJIN_MCP_ALLOWED_HOSTS": "127.0.0.1:18765,localhost:18765",
        "WENJIN_AUDIT_INCLUDE_QUERY": "0", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "EMBEDDING_MODEL_ID": "Qwen3-Embedding-0.6B", "RERANKER_MODEL_ID": "Qwen3-Reranker-0.6B",
    })
    env.pop("WENJIN_MCP_API_KEY", None)
    launch = {}
    for port, module in ((18777, "wenjin.file_management"), (18765, "wenjin.mcp_server")):
        name = "management" if port == 18777 else "mcp"
        args = [sys.executable, "-m", module]
        if port == 18777:
            args += ["serve", "--root", str(ROOT)]
        args += ["--host", "127.0.0.1", "--port", str(port), "--identity-db", str(identity)]
        with (OUT / f"{name}.stdout.log").open("ab") as stdout, (OUT / f"{name}.stderr.log").open("ab") as stderr:
            child = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        launch[name] = child.pid
    ready = False
    for _ in range(45):
        try:
            with urlopen("http://127.0.0.1:18777/api/health", timeout=2) as response:
                ready = response.status == 200
            if ready and {row["port"] for row in processes()} == {18777, 18765}:
                break
        except OSError:
            pass
        time.sleep(0.5)
    if not ready:
        raise RuntimeError("启动未就绪，请核对本轮服务日志")
    result = {"health": 200, "launch_pids": launch, "listeners_after": processes(),
              "model_identity_unchanged": model_before == model_identity(), "job_preflight": jobs}
    (OUT / "runtime-restored.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"health": 200, "model_identity_unchanged": result["model_identity_unchanged"]}))


if __name__ == "__main__":
    main()
