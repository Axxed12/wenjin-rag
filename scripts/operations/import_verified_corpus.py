"""导入已审查的候选包；预检所有冲突，拒绝覆盖不同内容，不操作索引。"""

import argparse
import json
import shutil
from pathlib import Path

from scripts.support.corpus_validation import safe_path
from scripts.support.verified_dataset import verify
from wenjin.content_hashing import sha256_hex
from wenjin.ingestion.verification import verify_manifest_hashes


def import_package(stage: Path, target: Path, review: dict) -> dict:
    if review.get("status") != "PASS":
        raise ValueError("缺少通过的语义/来源复核记录")
    files = [p for folder in ("raw_data", "corpora", "evaluation/datasets")
             for p in (stage / folder).rglob("*") if p.is_file()]
    bundle = {p.relative_to(stage).as_posix(): sha256_hex(p.read_bytes()) for p in files}
    if bundle != review["approved_files"]:
        raise ValueError("候选包在审查后发生变化")
    checks = [verify_manifest_hashes(p, raw_data_root=stage / "raw_data")
              for p in (stage / "corpora").glob("*/source-manifest.json")]
    rows = []
    for split in ("validation", "test"):
        p = stage / "evaluation/datasets" / f"rag-{split}-001.jsonl"
        raw = p.read_bytes()
        if sha256_hex(raw) != p.with_suffix(".sha256").read_text().split()[0]:
            raise ValueError("题集封存不匹配")
        rows.extend(json.loads(line) for line in raw.decode("utf-8").splitlines())
    result = verify(stage, rows)
    if result["status"] != "PASS":
        raise ValueError(result["errors"])
    # 所有目标先查冲突；不因中途发现旧文件而覆盖已有正式语料。
    for rel, expected in bundle.items():
        dest = safe_path(target, rel)
        if dest.exists() and (not dest.is_file() or sha256_hex(dest.read_bytes()) != expected):
            raise ValueError(f"目标冲突，拒绝覆盖: {dest}")
    for rel, expected in bundle.items():
        dest = safe_path(target, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(safe_path(stage, rel), dest)
        if sha256_hex(dest.read_bytes()) != expected:
            raise ValueError(f"复制后Hash不匹配: {dest}")
    return {"status": "PASS", "copied_or_identical_files": len(bundle),
            "corpora": checks, "questions": len(rows), "approved_files": bundle,
            "index_action": "none"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = import_package(args.stage.resolve(), args.target.resolve(),
                            json.loads(args.review.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "approved_files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
