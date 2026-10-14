"""只读记录授权数量与摘要，不打印用户或授权明细。"""
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

root = Path(__file__).resolve().parents[3]
out = Path(__file__).parent
phase = sys.argv[1]
if phase not in {"before", "after"}:
    raise SystemExit("phase must be before or after")
path = root / "data/file-management/manual-agent-identity.sqlite"
with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
    rows = db.execute(
        "SELECT * FROM resource_grants ORDER BY resource_kind,resource_id,principal_kind,principal_id"
    ).fetchall()
result = {"count": len(rows), "sha256": hashlib.sha256(
    json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
).hexdigest()}
if phase == "after":
    before = json.loads((out / "grants-before.json").read_text(encoding="utf-8"))
    result["unchanged"] = before == result
(out / f"grants-{phase}.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(result))
if phase == "after" and not result["unchanged"]:
    raise SystemExit(1)
