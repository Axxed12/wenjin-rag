"""只读作业状态及授权摘要；不输出主体、凭据或业务正文。"""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).parent


def inspect():
    identity = ROOT / "data/file-management/manual-agent-identity.sqlite"
    with closing(sqlite3.connect(identity.as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT * FROM resource_grants ORDER BY resource_kind,resource_id,principal_kind,principal_id"
        ).fetchall()
        grants = {"count": len(rows), "sha256": hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()}
    jobs = []
    for path in ROOT.joinpath("data").rglob("*.sqlite"):
        if "indexes" in path.parts or "indexes-private" in path.parts or (
            "releases" in path.parts and path.name != "release-jobs.sqlite"
        ):
            continue
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("jobs", "evaluation_jobs", "release_jobs"):
                if table not in tables:
                    continue
                counts = dict(db.execute(f"SELECT status,COUNT(*) FROM {table} GROUP BY status"))
                active = sum(counts.get(status, 0) for status in (
                    "queued", "preparing", "running", "cancelling", "pending", "building", "deleting"
                ))
                jobs.append({"database": str(path.relative_to(ROOT)), "table": table,
                             "active": active, "counts": counts})
    return {"grants": grants, "jobs": jobs}


if __name__ == "__main__":
    result = inspect()
    (OUT / "runtime-before.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
