"""读取最终授权摘要，保留运行前文件；不打印授权明细。"""
import json
from pathlib import Path

from runtime_check import inspect

out = Path(__file__).parent
before = json.loads((out / "runtime-before.json").read_text(encoding="utf-8"))
after = inspect()
after["grants_unchanged"] = before["grants"] == after["grants"]
(out / "runtime-after.json").write_text(
    json.dumps(after, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps({"grants": after["grants"], "grants_unchanged": after["grants_unchanged"]}))
raise SystemExit(not after["grants_unchanged"])
