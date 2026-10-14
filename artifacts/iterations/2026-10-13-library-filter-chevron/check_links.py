"""检查本轮更新文档的本地Markdown链接。"""
import json
import re
from pathlib import Path
from urllib.parse import unquote

root = Path(__file__).resolve().parents[3]
out = Path(__file__).parent
documents = [root / "code.md", root / "docs/admin-frontend.md",
             root / "docs/development-log.md", *out.glob("*.md")]
missing = []
checked = 0
for document in documents:
    for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        target = unquote(target.strip("<> ").split("#")[0])
        if not target or re.match(r"^(https?://|mailto:|app:|codex:)", target):
            continue
        path = Path(target)
        if not path.is_absolute():
            path = document.parent / path
        checked += 1
        if not path.exists():
            missing.append({"document": str(document.relative_to(root)), "target": target})
result = {"checked": checked, "missing": missing,
          "code_lines": len((root / "code.md").read_text(encoding="utf-8").splitlines())}
(out / "link-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(bool(missing))
