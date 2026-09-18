"""只读验收暂存语料；输出双Hash、归档溯源和题集隔离证据，不修改交付包。"""

import argparse
import json
import re
import tarfile
import zipfile
from collections import defaultdict
from contextlib import ExitStack
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath

import pymupdf

from wenjin.content_hashing import (
    BINARY_IDENTITY_V1,
    TEXT_NEWLINE_LF_V1,
    hash_content,
    sha256_hex,
)


def safe_path(root: Path, value: str) -> Path:
    """先检查跨平台绝对路径，再检查解析后的真实路径边界。"""
    if PureWindowsPath(value).drive or value.startswith(("/", "\\")):
        raise ValueError(f"absolute path: {value}")
    path = (root / value.replace("\\", "/")).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"path escape: {value}")
    return path


def hashes(raw: bytes, binary: bool = False) -> dict:
    h = hash_content(
        raw, normalization=BINARY_IDENTITY_V1 if binary else TEXT_NEWLINE_LF_V1,
        encoding=None if binary else "utf-8",
    )
    return {k: getattr(h, k) for k in (
        "raw_sha256", "canonical_sha256", "normalization", "encoding"
    )}


def normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def audit(root: Path, project: Path) -> dict:
    root = root.resolve()
    manifest_bytes = (root / "manifest.draft.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    errors, files, spans, documents = [], [], [], {}
    raw_map = {}
    for entry in manifest["entries"]:
        path = safe_path(root, entry["local_path"])
        raw = path.read_bytes()
        row = {"path": path.relative_to(root).as_posix(), **hashes(
            raw, path.suffix in {".gz", ".zip", ".pdf"}
        ), "bytes": len(raw)}
        row["raw_match"] = row["raw_sha256"] == entry["sha256"]
        row["bytes_match"] = len(raw) == entry["bytes"]
        row["declared_date_within_cutoff"] = entry["release_date"] <= "2026-07-15"
        row["canonical_predeclared"] = bool(entry.get("canonical_sha256"))
        if not all(row[k] for k in (
            "raw_match", "bytes_match", "declared_date_within_cutoff"
        )):
            errors.append({"raw": row})
        raw_map[row["path"]] = row
        files.append(row)
    declared = set(raw_map)
    actual = {p.relative_to(root).as_posix() for p in (root / "raw").rglob("*")
              if p.is_file()}
    if declared != actual or len(files) != manifest["total_files"]:
        errors.append({"raw_inventory": sorted(declared ^ actual)})
    if sum(f["bytes"] for f in files) != manifest["total_bytes"]:
        errors.append({"total_bytes": "mismatch"})

    datasets, seals = {}, []
    for split in ("validation", "test"):
        path = root / "datasets" / f"rag-{split}-001.jsonl"
        raw = path.read_bytes()
        seal = path.with_suffix(".sha256").read_text().split()[0]
        seals.append({"split": split, "sha256": sha256_hex(raw),
                      "match": sha256_hex(raw) == seal})
        if sha256_hex(raw) != seal:
            errors.append({"seal": split})
        datasets[split] = [json.loads(line) for line in raw.decode("utf-8").splitlines()
                           if line.strip()]
    split_docs = defaultdict(set)
    identities = defaultdict(set)
    with ExitStack() as stack:
        archives = {}
        for split, rows in datasets.items():
            for row in rows:
                for ev in row["evidence"]:
                    path = safe_path(root, ev["path"])
                    rel = path.relative_to(root).as_posix()
                    if rel not in documents:
                        data = path.read_bytes()
                        doc = {"path": rel, **hashes(data, path.suffix == ".pdf")}
                        archive = ev.get("source_archive")
                        member = ev.get("archive_member")
                        if archive and member:
                            ap = safe_path(root, archive)
                            # 只读取成员；从不解包到文件系统，避免归档路径写越界。
                            safe_path(root, member)
                            if archive not in archives:
                                archives[archive] = stack.enter_context(
                                    zipfile.ZipFile(ap) if ap.suffix == ".zip"
                                    else tarfile.open(ap)
                                )
                            arc = archives[archive]
                            if isinstance(arc, zipfile.ZipFile):
                                original = arc.read(member)
                            else:
                                info = arc.getmember(member)
                                if not info.isfile():
                                    raise ValueError(f"not a regular member: {member}")
                                original = arc.extractfile(info).read()
                            doc["archive_raw_match"] = data == original
                            doc["archive_canonical_match"] = (
                                hashes(original)["canonical_sha256"] == doc["canonical_sha256"]
                            )
                            if not doc["archive_raw_match"]:
                                errors.append({"archive_bytes_mismatch": rel})
                        documents[rel] = doc
                    doc = documents[rel]
                    source = ev.get("source_archive") or rel
                    source = source.replace("\\", "/")
                    expected = raw_map.get(source, {}).get("raw_sha256")
                    if expected != ev.get("source_sha256"):
                        errors.append({"source_hash_mismatch": row["id"], "path": rel})
                    split_docs[split].add(doc["canonical_sha256"])
                    identities[split].add((source, ev.get("archive_member")))
                    span = {"id": row["id"], "path": rel, "page": ev.get("page"),
                            "start_line": ev.get("start_line"), "end_line": ev.get("end_line")}
                    if path.suffix == ".pdf":
                        with pymupdf.open(path) as pdf:
                            page = ev.get("page")
                            valid = isinstance(page, int) and 1 <= page <= len(pdf)
                            span["text"] = pdf[page - 1].get_text() if valid else ""
                            span["pdf_page_count"] = len(pdf)
                    else:
                        lines = path.read_bytes().decode("utf-8").replace(
                            "\r\n", "\n").replace("\r", "\n").splitlines()
                        a, b = ev.get("start_line"), ev.get("end_line")
                        valid = isinstance(a, int) and isinstance(b, int) and 1 <= a <= b <= len(lines)
                        span["line_count"] = len(lines)
                        span["text"] = "\n".join(lines[a - 1:b]) if valid else ""
                    span["valid"] = valid
                    if not valid:
                        errors.append({"invalid_evidence": span})
                    spans.append(span)
    exact, near = [], []
    existing = []
    skipped_imported_copies = []
    for path in [project / "corpora/osg-3.6.5/dev-seed.jsonl",
                 *sorted((project / "evaluation/datasets").glob("*.jsonl"))]:
        if not path.is_file():
            continue
        if path.name in {"rag-validation-001.jsonl", "rag-test-001.jsonl"}:
            skipped_imported_copies.append(str(path))
            continue  # 原交付与其正式导入副本不是两个独立split，不把自比对当泄漏。
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip():
                existing.append(json.loads(line))
    groups = [("validation-test", datasets["validation"], datasets["test"]),
              ("new-existing", datasets["validation"] + datasets["test"], existing)]
    internal_duplicates = []
    for split, rows in datasets.items():
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if normalized(a["query"]) == normalized(b["query"]):
                    internal_duplicates.append({"split": split, "a": a["id"], "b": b["id"]})
    if internal_duplicates:
        errors.append({"internal_duplicate_queries": internal_duplicates})
    for label, left, right in groups:
        for a in left:
            for b in right:
                for field in ("query", "answer_rubric"):
                    av, bv = normalized(a.get(field, "")), normalized(b.get(field, ""))
                    if not av or not bv:
                        continue
                    pair = {"comparison": label, "a": a.get("id"), "b": b.get("id"),
                            "field": field}
                    if av == bv:
                        exact.append(pair)
                    elif field == "query":
                        ratio = SequenceMatcher(None, av, bv).ratio()
                        if ratio >= 0.65:
                            near.append({**pair, "ratio": ratio})
    overlap = sorted(split_docs["validation"] & split_docs["test"])
    identity_overlap = sorted(identities["validation"] & identities["test"])
    if overlap or identity_overlap or exact:
        errors.append({"leakage": {"content": overlap, "identity": identity_overlap,
                                   "exact": exact}})
    return {
        "status": "FAIL" if errors else "MECHANICAL_PASS_REVIEW_REQUIRED",
        "root": str(root), "manifest_sha256": sha256_hex(manifest_bytes),
        "files": files, "dataset_seals": seals,
        "dataset_counts": {k: len(v) for k, v in datasets.items()},
        "documents": list(documents.values()), "evidence": spans,
        "document_counts": {k: len(v) for k, v in split_docs.items()},
        "content_overlap": overlap, "identity_overlap": identity_overlap,
        "exact_duplicates": exact, "near_query_candidates": near, "errors": errors,
        "internal_duplicate_queries": internal_duplicates,
        "skipped_same_dataset_imports": skipped_imported_copies,
        "limits": ["Dates and licenses need upstream verification",
                   "No automatic semantic entailment or semantic duplicate verdict",
                   "Canonical hashes computed now are not upstream authenticity proof",
                   "PDF evidence read from original PDF, not supplied text cache"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root, Path(__file__).resolve().parents[1])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in (
        "status", "dataset_counts", "document_counts", "near_query_candidates", "errors"
    )}, ensure_ascii=False, indent=2))
    raise SystemExit(bool(result["errors"]))


if __name__ == "__main__":
    main()
