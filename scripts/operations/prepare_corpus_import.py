"""为本次固定交付构造可审查候选包；不写正式目录、不变更外部冻结原件。"""

import argparse
import copy
import json
import tarfile
from pathlib import Path

from scripts.support.corpus_validation import hashes, safe_path
from wenjin.content_hashing import manifest_content_fingerprint, sha256_hex

PROJECT = Path(__file__).resolve().parents[2]
ARTIFACTS = PROJECT / "artifacts/corpus-acceptance-001"
PROJ_COMMIT = "f08fa86c478c4bbbf003b1ec751dd84aa6eca486"


def dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare(source: Path, stage: Path) -> None:
    manifest = json.loads((source / "manifest.draft.json").read_text(encoding="utf-8"))
    audit = json.loads((ARTIFACTS / "local-audit.json").read_text(encoding="utf-8"))
    if audit["errors"] or audit["manifest_sha256"] != sha256_hex(
        (source / "manifest.draft.json").read_bytes()
    ):
        raise ValueError("原件验收未通过或manifest已变更")
    packages, mapping, source_entries = {}, {}, {}
    for e in manifest["entries"]:
        key = e["library"]
        packages.setdefault(key, {
            "schema_version": 3, "corpus_id": f"{key}-{e['version']}",
            "library": key, "version": e["version"], "commit": e["commit"],
            "raw_data_subdir": f"{key}/{e['version']}", "retrieved_on": "2026-09-17",
            "release_date": e["release_date"], "cutoff": "2026-07-15",
            "license": e["license"], "source_url": e["homepage"], "files": [],
            "scope": "Exact retrieval_eligible file allowlist; archives are preservation only.",
            "index_state": "not_built; no active-index change",
        })
        source_entries[e["local_path"].replace("\\", "/")] = e

    def add(old: str, key: str, dest: str, raw: bytes, eligible: bool,
            url: str, provenance: dict | None = None) -> dict:
        package = packages[key]
        rel = f"raw_data/{package['raw_data_subdir']}/{dest}"
        target = safe_path(stage, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        binary = target.suffix in {".pdf", ".gz", ".zip"}
        h = hashes(raw, binary)
        # 归档验收与解析就绪分开：现有纯文本入口不能直接吞PDF/HTML/man。
        ready = eligible and target.suffix not in {".pdf", ".html", ".1", ".rst", ".xsd"}
        entry = {"path": dest, "bytes": len(raw), **h, "source_url": url,
                 "retrieval_eligible": eligible, "indexable": ready,
                 "parser_status": "text_ready" if ready else (
                     "format_adapter_pending" if eligible else "preservation_only"),
                 "logical_document_id": f"{package['corpus_id']}:{dest}",
                 "provenance": provenance or {"staging_path": old}}
        if key == "ogc-3dtiles" and target.suffix in {".pdf", ".html"}:
            entry["logical_document_id"] = "ogc:22-025r4"
        package["files"].append(entry)
        mapping[old] = {"path": rel, **h, "corpus_id": package["corpus_id"]}
        return entry

    referenced = {d["path"] for d in audit["documents"]}
    for old, e in source_entries.items():
        raw = safe_path(source, old).read_bytes()
        if sha256_hex(raw) != e["sha256"]:
            raise ValueError(f"原始文件改变: {old}")
        eligible = old in referenced and "proj-9.8-docs-index.html" not in old
        dest = Path(old).name
        if dest.endswith((".gz", ".zip")):
            dest = "archives/" + dest
        entry = add(old, e["library"], dest, raw, eligible, e["source_url"])
        if "proj-9.8-docs-index.html" in old:
            entry["exclusion_reason"] = "Mutable 9.8 branch snapshot; replace with fixed-commit RST."
            entry["source_last_modified"] = "2026-09-04T12:49:05Z"
            entry["cutoff_eligibility"] = "excluded_unpinned_snapshot"
        if old.endswith(".xsd"):
            entry["schema_revision"] = "1.3.0.2"
            entry["source_last_modified"] = "2025-04-09T01:48:37Z"
            entry["date_note"] = "2006-03-15 is protocol publication, not this schema revision."
    for d in audit["documents"]:
        old = d["path"]
        if old in mapping:
            continue
        key = old.split("/")[1]
        dest = old.removeprefix(f"extracted/{key}/")
        raw = safe_path(source, old).read_bytes()
        if sha256_hex(raw) != d["raw_sha256"]:
            raise ValueError(f"证据文件改变: {old}")
        ev = next(ev for split in ("validation", "test")
                  for row in map(json.loads, (source / f"datasets/rag-{split}-001.jsonl")
                                 .read_text(encoding="utf-8").splitlines())
                  for ev in row["evidence"] if ev["path"] == old)
        entry = source_entries[ev["source_archive"]]
        url = entry["source_url"]
        if key == "osgearth":
            url = f"https://raw.githubusercontent.com/pelicanmapping/osgearth/{entry['commit']}/{dest}"
        add(old, key, dest, raw, True, url, {
            "source_archive": mapping[ev["source_archive"]]["path"],
            "archive_member": ev["archive_member"], "archive_raw_sha256": ev["source_sha256"],
            "archive_member_raw_verified": True,
        })
    # 用户明确批准纳入全部七份man1手册；从已验收归档读取，避免信任游离派生件。
    with tarfile.open(source / "raw/proj/proj-9.8.1.tar.gz") as archive:
        for member in archive.getmembers():
            if member.name.startswith("proj-9.8.1/man/man1/") and member.name.endswith(".1"):
                if not member.isfile():
                    raise ValueError(member.name)
                dest = member.name.removeprefix("proj-9.8.1/")
                add("extracted/proj/" + dest, "proj", dest, archive.extractfile(member).read(),
                    True, source_entries["raw/proj/proj-9.8.1.tar.gz"]["source_url"], {
                        "source_archive": mapping["raw/proj/proj-9.8.1.tar.gz"]["path"],
                        "archive_member": member.name,
                        "archive_raw_sha256": source_entries["raw/proj/proj-9.8.1.tar.gz"]["sha256"],
                        "archive_member_raw_verified": True,
                    })
    pinned = "pinned/proj/docs/source/about_core.rst"
    add(pinned, "proj", "docs/source/about_core.rst",
        (ARTIFACTS / "proj-about-core-pinned.rst").read_bytes(), True,
        f"https://raw.githubusercontent.com/OSGeo/PROJ/{PROJ_COMMIT}/docs/source/about_core.rst",
        {"commit": PROJ_COMMIT, "retrieved_on": "2026-09-17"})

    changes, seals = [], []
    for split in ("validation", "test"):
        original = (source / f"datasets/rag-{split}-001.jsonl").read_bytes()
        expected = next(s["sha256"] for s in audit["dataset_seals"] if s["split"] == split)
        if sha256_hex(original) != expected:
            raise ValueError(f"冻结原件改变，需要重新验收: {split}")
        rows = [json.loads(line) for line in original.decode().splitlines()]
        for row in rows:
            before = copy.deepcopy(row)
            number = int(row["id"].rsplit("-", 1)[1])
            ev = row["evidence"]
            if split == "validation":
                if number in {21, 37}:
                    next(e for e in ev if e.get("page") == 3)["page"] = 1
                if number == 15:
                    ev[0]["start_line"] = 9
                    row["note"] = "Bing/Ion explicitly require license keys; ($) alone does not prove this for ESRI."
                if number == 22:
                    row["expected_status"] = "supported"
                    row["answer_rubric"] = row["answer_rubric"].split("；文档未说明")[0] + "。"
                    row.pop("note", None)
                if number == 28:
                    ev[1]["end_line"] = 15
                    row["answer_rubric"] += (
                        "但根 LICENSE.txt 第3项同时声明其为最终许可说明，优先于源码、头文件及文档中的许可文字。"
                    )
                if number in {45, 46}:
                    row["format"] = "rst"
                    ev[0].update(path=pinned, start_line=1 if number == 45 else 13,
                                 end_line=4 if number == 45 else 18,
                                 anchor=None, element_path=None, source_archive=None, archive_member=None)
                if number == 50:
                    row["format"] = "man-roff"
                    row["query_type"] = "api"
                    row["expected_status"] = "supported"
                    row["answer_rubric"] = (
                        "支持 --endpoint、--local-geojson-file、--user-writable-directory、"
                        "--system-directory、--target-dir、--bbox、--spatial-test、--source-id、"
                        "--area-of-use、--file、--all、--exclude-world-coverage、--quiet、--verbose、"
                        "--dry-run、--list-files、--no-version-filtering。区域可用 "
                        "--bbox west_long,south_lat,east_long,north_lat 指定；--spatial-test "
                        "默认为 intersects，也可选 contains。可用 --area-of-use 按区域名称筛选，"
                        "或 --source-id 按来源筛选；--file、--source-id、--area-of-use 和 --bbox 以 AND 组合。"
                    )
                    ev[0].update(path="extracted/proj/man/man1/projsync.1", start_line=34,
                                 end_line=181, anchor=None, element_path=None,
                                 archive_member="proj-9.8.1/man/man1/projsync.1")
                    row["note"] = "User-approved man/ scope; original insufficient label invalid."
            else:
                anchor_fixes = {
                    27: (0, "core-box"), 28: (0, "core-sphere"),
                    35: (1, "implicittiling-subdivision-rules"),
                    38: (0, "toc154"), 40: (1, "tileformats-batchtable-implementation-example"),
                }
                if number in anchor_fixes:
                    i, anchor = anchor_fixes[number]
                    ev[i]["anchor"] = anchor
                if number == 5:
                    ev[1]["start_line"] = 1000
                    row["expected_status"] = "supported"
                    row["note"] = "Two spans jointly support the answer; multiple spans do not imply partial support."
                if number == 9:
                    row["answer_rubric"] = row["answer_rubric"].replace(
                        "（synopsis 列出这 6 个以外的写法见下）", "（synopsis 列出这 5 个取值）")
                    row["expected_status"] = "supported"
                if number == 15:
                    ev[0]["end_line"] = 217
                    row["required_evidence_atoms"] = ["TRUE", "ON", "NO", "FALSE", "OFF"]
                if number == 12:
                    ev[1]["start_line"] = 285
                if number == 26:
                    row["answer_rubric"] = row["answer_rubric"].replace(
                        "参考椭球假定与 tileset 的参考椭球相同", "高度相对于 WGS 84 椭球测量")
                    row["required_evidence_atoms"] = ["WGS 84", "ellipsoid"]
                if number == 33:
                    extra = copy.deepcopy(ev[0])
                    extra.update(start_line=2027, end_line=2027, anchor=None)
                    ev.append(extra)
                    row["required_evidence_atoms"] = ["each city", "global tileset"]
                if number == 38:
                    ev[0]["end_line"] = 9825
                    row["query"] = row["query"].replace("首次发布日期", "首条修订日期")
                    row["answer_rubric"] = row["answer_rubric"].replace(
                        "2022-07-26 发布了 1.1.0", "1.1.0 的首条修订记录日期为 2022-07-26")
                if number == 39:
                    ev[0]["end_line"] = 2426
                if number == 40:
                    row["answer_rubric"] = (
                        "当前可检索范围是标准规范，没有 CesiumJS 运行时 API 的参数默认值依据，"
                        "因此无法给出 maximumScreenSpaceError 的默认数值。"
                        "规范中的对应概念是 Screen-Space Error (SSE)：渲染某 tile 而不渲染其"
                        "子节点时，简化几何相对源几何带来的像素差。不能从 SSE 定义推断实现的默认值。"
                    )
            if before != row:
                changes.append({"id": row["id"], "before": before, "after_editorial": copy.deepcopy(row)})
            for e in ev:
                old = e["path"]
                meta = mapping[old]
                e.update(meta)
                e["source_sha256"] = meta["raw_sha256"]
                if e.get("source_archive"):
                    e["source_archive"] = mapping[e["source_archive"]]["path"]
            row.update(
                schema_version=2, dataset_revision="pre-evaluation-repair-2026-09-17",
                review_status="agent_source_checked", scope_id="rag-multicorpus-scope-001",
                source_paths=sorted({e["path"] for e in ev}),
                source_sha256_all=sorted({e["raw_sha256"] for e in ev}),
                source_sha256=ev[0]["raw_sha256"],
            )
            row["archive_member"] = ev[0].get("archive_member") if len(ev) == 1 else None
        path = stage / "evaluation/datasets" / f"rag-{split}-001.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode()
        path.write_bytes(data)
        path.with_suffix(".sha256").write_text(sha256_hex(data) + "  " + path.name + "\n")
        seals.append({"split": split, "original_sha256": sha256_hex(original),
                      "imported_sha256": sha256_hex(data), "questions": len(rows),
                      "evaluated": False, "evaluation_status_basis": "user confirmation"})
    for p in packages.values():
        p["files"].sort(key=lambda e: e["path"])
        p["canonical_corpus_sha256"] = manifest_content_fingerprint(p)
        p["retrieval_content_sha256"] = manifest_content_fingerprint({
            "files": [{**e, "indexable": e["retrieval_eligible"]} for e in p["files"]]
        })
        p["retrieval_scope_files"] = [e["path"] for e in p["files"] if e["retrieval_eligible"]]
        p["exclusion_policy"] = "All unlisted archive members and preservation_only files excluded."
        dump(stage / f"corpora/{p['corpus_id']}/source-manifest.json", p)
    dump(stage / "evaluation/datasets/rag-multicorpus-scope-001.json", {
        "scope_id": "rag-multicorpus-scope-001", "scope_type": "exact_file_allowlist",
        "corpora": {p["corpus_id"]: p["retrieval_scope_files"] for p in packages.values()},
        "validation_libraries": ["osgearth", "proj", "ogc-wms"],
        "test_libraries": ["gdal", "ogc-3dtiles"],
        "proj_man_included": True, "archives_recursively_indexed": False,
        "review_note": "Agent reviewed; no human review claimed; test never used for tuning.",
    })
    dump(ARTIFACTS / "dataset-repairs.json", changes)
    dump(ARTIFACTS / "dataset-seals.json", seals)
    dump(ARTIFACTS / "path-mapping.json", mapping)
    print(json.dumps({"stage": str(stage), "corpora": len(packages),
                      "files": sum(len(p["files"]) for p in packages.values()),
                      "repaired_questions": len(changes)}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source.resolve(), args.stage.resolve())


if __name__ == "__main__":
    main()
