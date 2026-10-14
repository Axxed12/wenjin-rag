"""核实后仅重载两项原生服务；不停止模型/Qdrant，不改变授权。"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from runtime_check import inspect

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent
IDENTITY = ROOT / "data/file-management/manual-agent-identity.sqlite"
EXPECTED = {18777: "wenjin.file_management", 18765: "wenjin.mcp_server"}


def processes():
    command = """
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-NetTCPConnection -LocalPort 18777,18765 -State Listen -ErrorAction SilentlyContinue |
ForEach-Object {
  $taskProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)"
  [pscustomobject]@{port=$_.LocalPort;pid=$_.OwningProcess;command=$taskProcess.CommandLine}
} | ConvertTo-Json -Compress
"""
    process = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                             capture_output=True, text=True, encoding="utf-8")
    if process.returncode and (process.stdout.strip() or process.stderr.strip()):
        raise RuntimeError("无法核实服务监听")
    output = process.stdout
    parsed = json.loads(output) if output.strip() else []
    return parsed if isinstance(parsed, list) else [parsed]


def main():
    before = inspect()
    if any(row["active"] for row in before["jobs"]):
        raise RuntimeError("存在进行中的后台任务，未执行重载")
    active = processes()
    resume = "--resume" in sys.argv
    if resume and active:
        raise RuntimeError("恢复启动要求两个端口均为空，避免第二个管理器")
    if not resume and {row["port"] for row in active} != set(EXPECTED):
        raise RuntimeError("服务端口不符合已确认拓扑，未执行重载")
    for row in active:
        command = row["command"]
        if f"-m {EXPECTED[row['port']]} " not in command or str(IDENTITY) not in command:
            raise RuntimeError("监听进程模块/身份库不匹配，未执行重载")
        if row["port"] == 18777 and f"--root {ROOT}" not in command:
            raise RuntimeError("管理进程root不匹配，未执行重载")
    token = (ROOT / "deploy/secrets/manual-agent/model_api_token").read_text().strip()
    with urlopen(Request("http://127.0.0.1:18766/v1/identity",
                         headers={"Authorization": "Bearer " + token}), timeout=5) as response:
        model_before = json.load(response)
    (OUT / "runtime-before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    # 备份成功后才停止服务；备份放在忽略目录，不输出账号或凭据hash。
    backup = IDENTITY.parent / "backups" / "identity-before-classification.sqlite"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists() and not resume:
        raise RuntimeError("备份已存在，拒绝覆盖；请核查本轮是否已重载")
    if resume and not backup.exists():
        raise RuntimeError("缺少本轮停机前备份，拒绝猜测性恢复")
    if not resume:
        with sqlite3.connect(IDENTITY.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as destination:
                source.backup(destination)
    # 各PID已经在上面核对；只停止已确认的两个服务，不按进程名称批量结束。
    for row in active:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Stop-Process -Id {int(row['pid'])} -ErrorAction Stop"], check=True)
    for _ in range(30):
        if not processes():
            break
        time.sleep(0.2)
    else:
        raise RuntimeError("旧监听尚未退出，未启动第二个管理器")
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(ROOT / "src"), "WENJIN_MODEL_PROVIDER": "remote",
        "WENJIN_MODEL_API_URL": "http://127.0.0.1:18766", "WENJIN_MODEL_API_TOKEN": token,
        "WENJIN_IDENTITY_DB": str(IDENTITY), "WENJIN_FILE_MANAGEMENT_URL": "http://127.0.0.1:18777",
        "WENJIN_MCP_ALLOWED_HOSTS": "127.0.0.1:18765,localhost:18765",
        "WENJIN_AUDIT_INCLUDE_QUERY": "0", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "EMBEDDING_MODEL_ID": "Qwen3-Embedding-0.6B", "RERANKER_MODEL_ID": "Qwen3-Reranker-0.6B",
    })
    env.pop("WENJIN_MCP_API_KEY", None)
    launch = {}
    for port, module in EXPECTED.items():
        name = "management" if port == 18777 else "mcp"
        args = [sys.executable, "-m", module]
        if port == 18777:
            args += ["serve", "--root", str(ROOT)]
        args += ["--host", "127.0.0.1", "--port", str(port), "--identity-db", str(IDENTITY)]
        with (OUT / f"{name}.stdout.log").open("ab") as stdout, (OUT / f"{name}.stderr.log").open("ab") as stderr:
            child = subprocess.Popen(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        launch[name] = child.pid
    for _ in range(45):
        try:
            with urlopen("http://127.0.0.1:18777/api/health", timeout=2) as response:
                ready = response.status == 200
            if ready and {row["port"] for row in processes()} == set(EXPECTED):
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("重载后未就绪，请检查本轮服务日志")
    after = inspect()
    if before["grants"] != after["grants"]:
        raise RuntimeError("授权摘要发生变化，需核查外部并发修改")
    with urlopen(Request("http://127.0.0.1:18766/v1/identity",
                         headers={"Authorization": "Bearer " + token}), timeout=5) as response:
        model_after = json.load(response)
    result = {"listeners_before": active, "listeners_after": processes(), "launch_pids": launch,
              "grants_unchanged": True, "model_identity_unchanged": model_before == model_after,
              "health": 200, "job_preflight": before["jobs"]}
    (OUT / "runtime-reload.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"health": 200, "grants_unchanged": True,
                      "model_identity_unchanged": result["model_identity_unchanged"], "launch_pids": launch}))


if __name__ == "__main__":
    main()
