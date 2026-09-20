"""第一、二批修复的验收证据：用真实语料跑一遍关键判定，输出可复现的JSON。

只读：不改任何 manifest、索引配置或注册表。覆盖两批要求的验收项：
- 身份一致性（不改语料、不重建索引）
- 取件四条件：ACL、manifest登记、可信目录、raw_sha256
- 路径适配：OSG裸路径与新语料前缀都能定位
- 归档件可下载
- 编码类文件名与路径分段编码

运行：uv run python scripts/verify_batch1_2.py
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wenjin.index_registry import (  # noqa: E402
    IndexRegistryStore,
    resolve_corpus_identity,
)
from wenjin.paths import UnsafePathError, resolve_within  # noqa: E402
from wenjin.serving import (  # noqa: E402
    CorpusSources,
    SourceModified,
    SourceNotFound,
    SourceTooLarge,
    download_path_for,
)

OUTPUT = ROOT / "artifacts/corpus-acceptance-002/batch1-2-acceptance.json"

# 每个语料挑一条真实chunk路径，覆盖两类来源写法。
CHUNK_PATH_CASES = [
    ("osg-3.6.5", "include/osg/Node"),
    ("osg-3.6.5", "include/osg/Group"),
    ("proj-9.8.1", "raw_data/proj/9.8.1/README.md"),
    ("proj-9.8.1", "raw_data/proj/9.8.1/man/man1/projsync.1"),
    ("ogc-3dtiles-1.1", "raw_data/ogc-3dtiles/1.1/22-025r4.pdf"),
    ("ogc-wms-1.3.0", "raw_data/ogc-wms/1.3.0/06-042.pdf"),
    ("osgearth-3.8", "raw_data/osgearth/3.8/docs/source/earthfile.md"),
]


def sources_for(corpus_id: str) -> CorpusSources:
    manifest = ROOT / "corpora" / corpus_id / "source-manifest.json"
    return CorpusSources.from_manifest(
        manifest, raw_root=ROOT / "raw_data", expected_corpus_id=corpus_id
    )


def check_identity() -> list[dict]:
    """三个身份来源必须一致；旧配置走兼容回退。"""
    results = []
    registry = IndexRegistryStore(ROOT / "data/indexes/index-registry.json").load()
    targets = [("active", registry["active"])]
    for path in sorted((ROOT / "configs/indexes").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for item in data.get("candidates", [data]):
            if "manifest_path" in item:
                targets.append((str(path.relative_to(ROOT)).replace("\\", "/"), item))
    for label, item in targets:
        target = load_target_from(item)
        identity = resolve_corpus_identity(target, root=ROOT)
        results.append({
            "label": label,
            "target_id": target.target_id,
            "explicit_corpus_id": target.corpus_id,
            "resolved": identity.corpus_id,
            "source": identity.source,
            "directory": Path(target.manifest_path).parent.name,
        })
    return results


def load_target_from(item: dict):
    from wenjin.index_registry import IndexTarget

    return IndexTarget.from_dict(item)


def check_fetch(corpus_id: str, chunk_path: str) -> dict:
    """取件的四项判定，逐项报告。"""
    record_result = {
        "corpus_id": corpus_id,
        "chunk_source_path": chunk_path,
        "download_path": None,
        "registered": False,
        "resolved_relative": None,
        "bytes": None,
        "raw_sha256_matches": None,
        "media_type": None,
        "inline": None,
        "error": None,
    }
    try:
        sources = sources_for(corpus_id)
        record = sources.match_chunk_path(chunk_path)
        record_result["registered"] = True
        record_result["resolved_relative"] = record.relative
        record_result["download_path"] = download_path_for(corpus_id, record.relative)
        verified = sources.read_verified(record.relative)
        record_result["bytes"] = len(verified.content)
        record_result["raw_sha256_matches"] = (
            hashlib.sha256(verified.content).hexdigest() == record.raw_sha256
        )
        record_result["media_type"] = verified.media_type
        record_result["inline"] = verified.inline
    except (SourceNotFound, SourceModified, SourceTooLarge, ValueError) as error:
        record_result["error"] = f"{type(error).__name__}: {error}"
    return record_result


def check_negative() -> list[dict]:
    """必须被拒绝的情形。"""
    cases = []
    sources = sources_for("osg-3.6.5")

    for label, path in [("未登记文件", "include/osg/DoesNotExist"),
                        ("跨语料路径", "raw_data/proj/9.8.1/README.md")]:
        try:
            sources.match_chunk_path(path)
            cases.append({"case": label, "blocked": False, "detail": "未拦截"})
        except SourceNotFound as error:
            cases.append({"case": label, "blocked": True, "detail": str(error)})

    for label, relative in [("目录逃逸", "../README.md"),
                            ("绝对路径", "/etc/passwd")]:
        try:
            resolve_within(ROOT / "raw_data", relative)
            cases.append({"case": label, "blocked": False, "detail": "未拦截"})
        except UnsafePathError as error:
            cases.append({"case": label, "blocked": True, "detail": str(error)})
    return cases


def check_archives() -> dict:
    """归档件（retrieval_eligible=False）也必须能下载。"""
    report = {}
    for corpus_id in ("osgearth-3.8", "gdal-3.13.1", "proj-9.8.1"):
        sources = sources_for(corpus_id)
        archives = [item for item in sources.listing() if not item.retrieval_eligible]
        outcome = {"archived_files": len(archives), "downloadable": 0, "largest_mb": 0.0,
                   "failures": []}
        for record in archives:
            try:
                verified = sources.read_verified(record.relative)
                outcome["downloadable"] += 1
                outcome["largest_mb"] = max(
                    outcome["largest_mb"], round(len(verified.content) / 1048576, 2)
                )
            except (SourceNotFound, SourceModified, SourceTooLarge, ValueError) as error:
                outcome.setdefault("failures", []).append(
                    {"path": record.relative, "error": f"{type(error).__name__}: {error}"}
                )
        report[corpus_id] = outcome
    return report


def check_encoding() -> list[dict]:
    """路径分段编码与RFC 5987文件名编码；结果带可判定的ok字段。"""
    cases = [
        ("空格与特殊字符", "docs/a b#c?d/note.md",
         "/files/demo-1.0/docs/a%20b%23c%3Fd/note.md"),
        ("中文文件名", "docs/中文 文件.md",
         "/files/demo-1.0/docs/%E4%B8%AD%E6%96%87%20%E6%96%87%E4%BB%B6.md"),
    ]
    results = []
    for label, source, expected in cases:
        actual = download_path_for("demo-1.0", source)
        results.append({
            "case": label,
            "input": source,
            "download_path": actual,
            "expected": expected,
            # 未转义的 # 和 ? 会截断URL，中文必须被百分号编码。
            "ok": actual == expected
            and "#" not in actual and "?" not in actual
            and actual.isascii(),
        })
    return results


def collect_failures(report: dict) -> list[dict]:
    """把报告里**每一类**验收项的失败都收集起来。

    抽成纯函数是为了能做变异测试：把某类结果改成失败，断言退出码会变成非零。
    只统计"显然失败"的项会让验收脚本在归档件或编码项失败时仍然报PASS，
    那样它的PASS就不能作为完整验收依据。
    """
    failures: list[dict] = []
    for item in report.get("chunk_path_cases", []):
        if item.get("error"):
            failures.append({"gate": "chunk_path", "detail": item})
        elif item.get("raw_sha256_matches") is not True:
            # 定位成功但Hash没对上，同样是失败，不能因为没抛异常就放过。
            failures.append({"gate": "chunk_path_hash", "detail": item})
    for item in report.get("identity", []):
        if not item.get("resolved") or item.get("resolved") != item.get("directory"):
            # 三方一致时 resolved 必须等于目录名；不等说明身份校验被绕过。
            failures.append({"gate": "identity", "detail": item})
    for item in report.get("negative_cases", []):
        if not item.get("blocked"):
            failures.append({"gate": "negative", "detail": item})
    for corpus_id, outcome in (report.get("archives") or {}).items():
        if outcome.get("downloadable") != outcome.get("archived_files"):
            failures.append({"gate": "archive", "detail": {"corpus_id": corpus_id, **outcome}})
    for item in report.get("encoding", []):
        if not item.get("ok"):
            failures.append({"gate": "encoding", "detail": item})
    return failures


def main() -> None:
    report = {
        "scope": "第一、二批修复的只读验收；未改动任何manifest、索引配置或注册表",
        "identity": check_identity(),
        "chunk_path_cases": [check_fetch(corpus, path) for corpus, path in CHUNK_PATH_CASES],
        "negative_cases": check_negative(),
        "archives": check_archives(),
        "encoding": check_encoding(),
    }
    failures = collect_failures(report)
    report["status"] = "PASS" if not failures else "FAIL"
    report["failures"] = failures

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"身份检查：{len(report['identity'])} 项")
    for item in report["chunk_path_cases"]:
        state = "OK " if not item["error"] else "FAIL"
        print(f"  {state} {item['corpus_id']:16s} {item['chunk_source_path'][:42]:42s} "
              f"{item['bytes']}字节 hash={item['raw_sha256_matches']} {item['media_type']}")
    for item in report["negative_cases"]:
        print(f"  {'OK ' if item['blocked'] else 'FAIL'} 拦截 {item['case']}")
    print(f"状态：{report['status']}；报告：{OUTPUT}")
    raise SystemExit(report["status"] != "PASS")


if __name__ == "__main__":
    main()
