# -*- coding: utf-8 -*-
"""问津 RAG：评测集构造前置——语料盘点器（只读，不修改任何索引或语料）

用途
----
扫描工作区中**实际存在**的资料包、manifest 与文件，产出机器可读的语料清单，
并标出哪些文档/证据区间已被既有数据集占用（供后续去重与泄漏检查使用）。

设计约束
--------
1. 只读取 corpora/ 与 data/packages/ 下已有 manifest 指向的真实文件；
   清单缺失或文件不存在者一律记为 missing，绝不补齐、不编造。
2. 不写入任何索引；只输出 evaluation/datasets/ 下的清单文件。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

# 项目根由脚本位置推导，不写死本机绝对路径：写死会让脚本换台机器就跑不了，
# 也会把开发机的目录结构带进仓库。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_PATH = os.path.join(ROOT, "evaluation", "datasets", "rag-dataset-corpus-inventory-001.json")

# 既有数据集：其证据区间不得在新数据集中复用
EXISTING_DATASETS = [
    ("corpora/osg-3.6.5/dev-seed.jsonl", "dev-seed", "validation"),
    ("evaluation/datasets/osg-3_6_5-frozen-001.jsonl", "osg-3_6_5-frozen-001", "test"),
    ("evaluation/datasets/osg-evidence-negative-001.jsonl", "osg-evidence-negative-001", "negative"),
]


def abspath(rel: str) -> str:
    return os.path.join(ROOT, rel.replace("/", os.sep))


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def classify_format(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1]
    ext = os.path.splitext(name)[1].lower()
    if ext == ".cpp":
        return "source-cpp"
    if ext == "" and ("/include/" in f"/{rel_path}"):
        return "source-cpp-header"
    if ext == ".md":
        return "markdown"
    if ext == ".txt":
        return "plain-text"
    if ext == ".pdf":
        return "pdf-text"
    if ext == ".png":
        return "image-raster"
    return ext.lstrip(".") or "unknown"


def collect_evidence_usage() -> dict:
    """返回 {相对文件路径: [{'dataset','split','id','start_line','end_line'}]}"""
    usage: dict[str, list] = {}
    for rel, name, split in EXISTING_DATASETS:
        fp = abspath(rel)
        if not os.path.exists(fp):
            continue
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            for e in row.get("evidence") or []:
                if isinstance(e, dict) and e.get("path"):
                    usage.setdefault(e["path"], []).append({
                        "dataset": name,
                        "split": split,
                        "query_id": row.get("id"),
                        "start_line": e.get("start_line"),
                        "end_line": e.get("end_line"),
                        "page": e.get("page"),
                        "bbox": e.get("bbox"),
                    })
    return usage


def build() -> dict:
    documents = []
    packages = []

    # ---- 1) OSG 3.6.5 源码语料 ----
    osg_manifest_rel = "corpora/osg-3.6.5/source-manifest.json"
    osg = json.load(open(abspath(osg_manifest_rel), encoding="utf-8"))
    pkg = {
        "package_id": osg["corpus_id"],
        "library": osg["library"],
        "version": osg["version"],
        "version_anchor": f"tag {osg['tag']} · commit {osg['commit']}",
        "source_type": "official-source",
        "source_url": osg["repository"],
        "license": "见 LICENSE.txt（OSG 自有许可）",
        "retrieved_on": osg["retrieved_on"],
        "manifest_path": osg_manifest_rel,
        "manifest_sha256": sha256_file(abspath(osg_manifest_rel)),
        "declared_file_count": len(osg["files"]),
        "actual_file_count": None,
        "evidence_kind": "line-numbers",
    }
    missing = []
    for f in osg["files"]:
        rel = f"raw_data/{osg['raw_data_subdir']}/{f['path']}"
        exists = os.path.exists(abspath(rel))
        if not exists:
            missing.append(rel)
        actual = sha256_file(abspath(rel)) if exists else None
        documents.append({
            "document_id": f"{osg['library']}:{f['path']}",
            "library": osg["library"],
            "version": osg["version"],
            "source_type": "official-source",
            "format": classify_format(f["path"]),
            "path": rel,
            "declared_path": f["path"],
            "sha256": f.get("raw_sha256") or f["sha256"],
            "sha256_actual": actual,
            "sha256_match": (
                actual == (f.get("raw_sha256") or f["sha256"])
            ) if actual else None,
            "bytes": f["bytes"],
            "indexable": f["indexable"],
            "license": pkg["license"],
            "evidence_kind": "line-numbers",
            "file_exists": exists,
        })
    present = [d for d in documents if d["file_exists"]]
    pkg["actual_file_count"] = len(present)
    pkg["missing_files"] = missing
    packages.append(pkg)

    # ---- 2) parse-lab 解析样本（教学固件，非基准语料）----
    pl_manifest_rel = "data/packages/parse-lab/manifest.json"
    pl = json.load(open(abspath(pl_manifest_rel), encoding="utf-8"))
    pl_pkg = {
        "package_id": "parse-lab",
        "library": "parse-lab",
        "version": pl["upstream_revision"][:12],
        "version_anchor": f"upstream commit {pl['upstream_revision']}",
        "source_type": "teaching-fixture",
        "source_url": pl["upstream_url"],
        "license": "见 data/packages/parse-lab/LICENSE.txt",
        "retrieved_on": "2026-09-06",
        "manifest_path": pl_manifest_rel,
        "manifest_sha256": sha256_file(abspath(pl_manifest_rel)),
        "declared_file_count": len(pl["files"]),
        "actual_file_count": None,
        "evidence_kind": "page+bbox",
        "caveat": "manifest 自述：parser teaching fixtures; not production corpus or held-out benchmark",
    }
    for f in pl["files"]:
        rel = f"data/packages/parse-lab/{f['path']}"
        exists = os.path.exists(abspath(rel))
        actual = sha256_file(abspath(rel)) if exists else None
        fmt = "pdf-scan-derived" if f["path"] == "scan.pdf" else classify_format(f["path"])
        documents.append({
            "document_id": f"parse-lab:{f['path']}",
            "library": "parse-lab",
            "version": pl["upstream_revision"][:12],
            "source_type": "teaching-fixture",
            "format": fmt,
            "path": rel,
            "declared_path": f["path"],
            "sha256": f.get("raw_sha256") or f["sha256"],
            "sha256_actual": actual,
            "sha256_match": (
                actual == (f.get("raw_sha256") or f["sha256"])
            ) if actual else None,
            "bytes": None,
            "indexable": None,
            "license": pl_pkg["license"],
            "evidence_kind": "page+bbox" if f["path"] == "original.pdf" else "derived-from-original",
            "derived": f["derived"],
            "transform": f.get("transform"),
            "file_exists": exists,
        })
    pl_pkg["actual_file_count"] = sum(1 for d in documents if d["file_exists"] and d["library"] == "parse-lab")
    packages.append(pl_pkg)

    # ---- 3) 证据占用 / 未使用文档 ----
    usage = collect_evidence_usage()
    for d in documents:
        d["used_by_datasets"] = usage.get(d["declared_path"] if d["library"] == "osg" else d["path"], [])

    osg_docs = [d for d in documents if d["library"] == "osg" and d["file_exists"]]
    used = [d for d in osg_docs if d["used_by_datasets"]]
    unused = [d for d in osg_docs if not d["used_by_datasets"]]

    # ---- 4) 格式覆盖 ----
    fmt_counts: dict[str, int] = {}
    for d in documents:
        if d["file_exists"] and d.get("indexable") is not False:
            fmt_counts[d["format"]] = fmt_counts.get(d["format"], 0) + 1

    REQUIRED_FORMATS = ["markdown", "html", "xml-schema", "pdf-text", "pdf-scan", "table-pdf", "source-cpp"]
    fmt_matrix = []
    for fmt in REQUIRED_FORMATS:
        n = fmt_counts.get(fmt, 0)
        fmt_matrix.append({
            "format": fmt,
            "present": n > 0,
            "count": n,
            "verdict": "缺失（工作区无此格式的已登记资料）" if n == 0 else ("偏少" if n < 3 else "满足 3-5 题下限"),
        })

    return {
        "inventory_version": "001",
        "generated_on": datetime.now().date().isoformat(),
        "generated_by": "evaluation/datasets/build_corpus_inventory.py",
        "scope": "工作区内实际存在、且有 manifest 指向的资料（不补齐、不推断）",
        "active_index": json.load(open(abspath("data/indexes/index-registry.json"), encoding="utf-8"))["active"],
        "packages": packages,
        "documents": documents,
        "summary": {
            "package_count": len(packages),
            "library_count": len({d["library"] for d in documents if d["file_exists"]}),
            "document_count": sum(1 for d in documents if d["file_exists"]),
            "indexable_document_count": sum(1 for d in documents if d["file_exists"] and d.get("indexable") is not False),
            "sha256_mismatch": [d["path"] for d in documents if d.get("sha256_match") is False],
            "missing_declared_files": [d["path"] for d in documents if not d["file_exists"]],
            "format_counts": fmt_counts,
            "format_matrix": fmt_matrix,
            "osg_documents_total": len(osg_docs),
            "osg_documents_used_by_existing_datasets": len(used),
            "osg_documents_unused": len(unused),
            "unused_document_paths": [d["declared_path"] for d in unused if d.get("indexable") is not False],
            "unused_indexable_count": sum(1 for d in unused if d.get("indexable") is not False),
            "evidence_used_files": sorted(usage.keys()),
        },
    }


if __name__ == "__main__":
    data = build()
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    s = data["summary"]
    print("=== 语料盘点 ===")
    print("资料包:", s["package_count"], "| 库:", s["library_count"], "| 文档:", s["document_count"],
          "| 可索引:", s["indexable_document_count"])
    print("SHA256 不匹配:", s["sha256_mismatch"])
    print("manifest 声明但文件缺失:", s["missing_declared_files"])
    print("格式计数:", s["format_counts"])
    print("OSG 文档: 共", s["osg_documents_total"], "已占用", s["osg_documents_used_by_existing_datasets"],
          "未使用", s["osg_documents_unused"], "(其中可索引", s["unused_indexable_count"], ")")
    print("未使用文档:")
    for p in s["unused_document_paths"]:
        print("   -", p)
    print("格式矩阵:")
    for m in s["format_matrix"]:
        print(f"   {m['format']:<14} present={m['present']!s:<5} count={m['count']:<3} {m['verdict']}")
    print("WROTE", OUT_PATH)
