"""把语料manifest统一为LF，并重算所有引用它的manifest_sha256。

背景：manifest_sha256原本是对manifest文件的原始字节做的SHA256。manifest在Windows上被写成
CRLF，而git提交时会把CRLF转成LF，于是"磁盘上的字节"和"仓库里的字节"不一致：Linux/CI检出
得到LF，哈希对不上，索引校验会直接失败。业界标准做法是把这类被哈希的文件统一成LF
（另见仓库根的.gitattributes）。

本脚本只改换行，不改任何JSON取值，并逐条证明这一点：
- 规范化前后解析出的JSON必须完全相等；
- manifest_content_fingerprint（基于内容、不受排版影响）必须完全相等。

默认只打印计划；确认后加--apply才写盘。
运行：uv run python scripts/normalize_manifest_newlines.py --apply
"""

import argparse
import hashlib
import json
from pathlib import Path

from wenjin.content_hashing import manifest_content_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def text_bytes(path: Path) -> bytes:
    """按原样读字节；这里不能做任何换行转换。"""
    return path.read_bytes()


def normalize(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def plan_manifests() -> list[dict]:
    """列出每个manifest的规范化计划；同时证明内容没有被改变。"""
    entries = []
    for path in sorted((ROOT / "corpora").glob("*/source-manifest.json")):
        raw = text_bytes(path)
        normalized = normalize(raw)
        before = json.loads(raw.decode("utf-8-sig"))
        after = json.loads(normalized.decode("utf-8-sig"))
        entries.append({
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "old_sha256": hashlib.sha256(raw).hexdigest(),
            "new_sha256": hashlib.sha256(normalized).hexdigest(),
            "old_bytes": len(raw),
            "new_bytes": len(normalized),
            "crlf_removed": raw.count(b"\r\n") + raw.count(b"\r") - normalized.count(b"\r"),
            "already_lf": raw.count(b"\r") == 0,
            "parsed_json_equal": before == after,
            "content_fingerprint_before": manifest_content_fingerprint(before),
            "content_fingerprint_after": manifest_content_fingerprint(after),
        })
    return entries


def write_manifests(manifests: list[dict], *, apply: bool) -> int:
    """把需要规范化的manifest按LF写回；已经全是LF的直接跳过。"""
    written = 0
    for item in manifests:
        if item["already_lf"]:
            continue
        path = ROOT / item["path"]
        normalized = normalize(path.read_bytes())
        if hashlib.sha256(normalized).hexdigest() != item["new_sha256"]:
            raise SystemExit(f"拒绝写入：{item['path']}的规范化结果与计划不一致")
        if apply:
            path.write_bytes(normalized)
        written += 1
    return written


def reference_files() -> list[Path]:
    """所有记录manifest_sha256的文件：索引目标配置和active注册表。"""
    return sorted((ROOT / "configs/indexes").glob("*.json")) + [
        ROOT / "data/indexes/index-registry.json"
    ]


def rewrite_references(mapping: dict[str, str], *, apply: bool) -> list[dict]:
    """只替换64位小写hex哈希字符串，其余内容原样保留。"""
    results = []
    for path in reference_files():
        raw = text_bytes(path)
        text = raw.decode("utf-8-sig")
        replaced = {}
        for old, new in mapping.items():
            count = text.count(old)
            if count:
                text = text.replace(old, new)
                replaced[old] = {"new": new, "count": count}
        normalized = normalize(text.encode("utf-8"))
        changed = normalized != raw
        results.append({
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "replacements": replaced,
            "line_endings_delta": len(raw) - len(normalized),
            "changed": changed,
        })
        if apply and changed:
            path.write_bytes(normalized)
    return results


def verify_references() -> list[dict]:
    """写盘后逐个核对：配置里记录的哈希必须等于磁盘上manifest的哈希。"""
    checks = []
    for path in sorted((ROOT / "configs/indexes").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for item in data.get("candidates", [data]):
            if "manifest_path" not in item:
                continue
            manifest = ROOT / item["manifest_path"]
            actual = hashlib.sha256(manifest.read_bytes()).hexdigest()
            checks.append({
                "config": str(path.relative_to(ROOT)).replace("\\", "/"),
                "target_id": item["target_id"],
                "recorded": item["manifest_sha256"],
                "actual": actual,
                "matches": actual == item["manifest_sha256"],
            })
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="manifest换行统一为LF并重算引用哈希")
    parser.add_argument("--apply", action="store_true", help="不传则只打印计划")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts/corpus-acceptance-002/manifest-lf-normalization.json")
    arguments = parser.parse_args()

    manifests = plan_manifests()
    if not all(item["parsed_json_equal"] for item in manifests):
        raise SystemExit("拒绝执行：规范化改变了JSON内容")
    if not all(
        item["content_fingerprint_before"] == item["content_fingerprint_after"]
        for item in manifests
    ):
        raise SystemExit("拒绝执行：规范化改变了内容指纹")
    mapping = {
        item["old_sha256"]: item["new_sha256"]
        for item in manifests if not item["already_lf"]
    }
    # 顺序不能反：manifest必须先落盘为LF，配置里的新哈希才会与磁盘一致。
    manifests_written = write_manifests(manifests, apply=arguments.apply)
    references = rewrite_references(mapping, apply=arguments.apply)
    report = {
        "status": "PASS",
        "applied": arguments.apply,
        "manifests_written": manifests_written,
        "reason": (
            "manifest_sha256是对文件原始字节的SHA256；manifest在Windows上为CRLF，"
            "而git按LF入库，导致跨平台检出后哈希失配"
        ),
        "manifests": manifests,
        "references": references,
        "verification": verify_references() if arguments.apply else [],
        "boundary": "只改换行，不改任何JSON取值；索引、向量和BM25行数均未改动。",
    }
    if arguments.apply:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
    print(json.dumps(
        {
            "applied": arguments.apply,
            "manifests_written": manifests_written,
            "manifests": [
                {k: item[k] for k in ("path", "old_sha256", "new_sha256", "crlf_removed",
                                      "parsed_json_equal", "content_fingerprint_before",
                                      "content_fingerprint_after")}
                for item in manifests
            ],
            "references_changed": sum(item["changed"] for item in references),
            "verification_failures": [
                item for item in report["verification"] if not item["matches"]
            ],
        },
        ensure_ascii=False, indent=2,
    ))


if __name__ == "__main__":
    main()
