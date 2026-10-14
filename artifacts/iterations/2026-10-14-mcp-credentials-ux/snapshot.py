"""只读记录凭据及授权摘要，不打印明细、哈希列或凭据前缀。"""
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

out = Path(__file__).parent
root = Path(__file__).resolve().parents[3]
phase = sys.argv[1]
if phase not in {"before", "after"}:
    raise SystemExit("invalid phase")
path = root / "data/file-management/manual-agent-identity.sqlite"
result = {}
with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
    for table, order in (("mcp_credentials", "credential_id"), ("resource_grants", "resource_kind,resource_id,principal_kind,principal_id")):
        rows = db.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()}
if phase == "after":
    result["unchanged"] = result == json.loads((out / "snapshot-before.json").read_text(encoding="utf-8"))
(out / f"snapshot-{phase}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
if phase == "after" and not result["unchanged"]:
    raise SystemExit(1)
