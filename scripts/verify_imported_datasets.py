"""校验正式/候选题集的证据身份、行页范围、隔离及可机械匹配的答案原子。"""

import argparse
import copy
import json
import re
from collections import defaultdict
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup
from lxml import etree

from scripts.audit_external_corpus import hashes, normalized, safe_path


def readable(text: str, suffix: str) -> str:
    if suffix == ".html":
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    if suffix == ".1":
        text = re.sub(r"\\f[BIP]", "", text).replace("\\-", "-")
    return text


def answer_atoms(row: dict) -> list[str]:
    """只自动抽取可确定匹配的原子；自然语言关系仍须人工/模型逐题复核。"""
    atoms = re.findall(
        r"\d{4}-\d{2}-\d{2}|\b\d+\.\d+(?:\.\d+)?\b|"
        r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|OGC\s+\d{2}-\d+[a-z]?\d*",
        row["answer_rubric"],
    )
    return sorted(set(atoms + row.get("required_evidence_atoms", [])))


def verify(root: Path, rows: list[dict]) -> dict:
    entries = {}
    for path in (root / "corpora").glob("*/source-manifest.json"):
        m = json.loads(path.read_text(encoding="utf-8"))
        for e in m["files"]:
            entries[f"raw_data/{m['raw_data_subdir']}/{e['path']}"] = e
    errors, evidence_records = [], []
    identities, contents = defaultdict(set), defaultdict(set)
    ids, questions = set(), {}
    atom_count = 0
    cache = {}
    for row in rows:
        if row["id"] in ids:
            errors.append({"id": row["id"], "error": "duplicate_id"})
        ids.add(row["id"])
        q = normalized(row["query"])
        if q in questions:
            errors.append({"id": row["id"], "error": "duplicate_query", "other": questions[q]})
        questions[q] = row["id"]
        texts = []
        for ev in row["evidence"]:
            path = safe_path(root, ev["path"])
            entry = entries[ev["path"]]
            if not entry.get("retrieval_eligible"):
                errors.append({"id": row["id"], "error": "evidence_outside_scope"})
            if path not in cache:
                data = path.read_bytes()
                cache[path] = {"hashes": hashes(data, path.suffix == ".pdf"), "data": data}
            cached = cache[path]
            h = cached["hashes"]
            for key in ("raw_sha256", "canonical_sha256", "normalization"):
                if ev.get(key) != h[key] or entry[key] != h[key]:
                    errors.append({"id": row["id"], "error": "evidence_hash", "field": key})
            contents[row["split"]].add(h["canonical_sha256"])
            identities[row["split"]].add(entry["logical_document_id"])
            if path.suffix == ".pdf":
                with pymupdf.open(path) as pdf:
                    n = ev["page"]
                    if not isinstance(n, int) or not 1 <= n <= len(pdf):
                        errors.append({"id": row["id"], "error": "page_bounds"})
                        continue
                    text = pdf[n - 1].get_text()
            else:
                if "lines" not in cached:
                    cached["lines"] = cached["data"].decode("utf-8").splitlines()
                lines = cached["lines"]
                a, b = ev["start_line"], ev["end_line"]
                if not isinstance(a, int) or not isinstance(b, int) or not 1 <= a <= b <= len(lines):
                    errors.append({"id": row["id"], "error": "line_bounds"})
                    continue
                text = readable("\n".join(lines[a - 1:b]), path.suffix)
                if path.suffix == ".html" and ev.get("anchor"):
                    if "anchors" not in cached:
                        cached["anchors"] = set(re.findall(
                            r'\bid=["\x27]([^"\x27]+)', cached["data"].decode("utf-8")
                        ))
                    if ev["anchor"] not in cached["anchors"]:
                        errors.append({"id": row["id"], "error": "html_anchor_missing",
                                       "anchor": ev["anchor"]})
                if path.suffix == ".xsd" and ev.get("element_path"):
                    tree = etree.fromstring(cached["data"], etree.XMLParser(
                        resolve_entities=False, no_network=True
                    ))
                    for xpath in ev["element_path"].split(" + "):
                        if not tree.xpath(xpath, namespaces={"xs": "http://www.w3.org/2001/XMLSchema"}):
                            errors.append({"id": row["id"], "error": "xpath_missing", "xpath": xpath})
            texts.append(text)
        combined = normalized("\n".join(texts))
        atoms = answer_atoms(row)
        atom_count += len(atoms)
        missing = [a for a in atoms if normalized(a) not in combined]
        if missing:
            errors.append({"id": row["id"], "error": "answer_atom_missing", "atoms": missing})
        evidence_records.append({"id": row["id"], "atoms": atoms, "missing": missing})
    for name, groups in [("content", contents), ("logical_document", identities)]:
        overlap = groups["validation"] & groups["test"]
        if overlap:
            errors.append({"error": f"{name}_leakage", "overlap": sorted(overlap)})
    return {"status": "FAIL" if errors else "PASS", "questions": len(rows),
            "answer_atoms": atom_count, "errors": errors, "atom_checks": evidence_records,
            "limits": "Atom hits are necessary, not sufficient for semantic entailment."}


def mutation_checks(root: Path, rows: list[dict]) -> list[dict]:
    """用曾经漏检的合法区间与错误页码做变异，避免仅测试行号越界。"""
    results = []
    for qid, field, value in [
        ("rag-validation-021", "page", 3),
        ("rag-test-015", "end_line", 215),
        ("rag-test-038", "end_line", 9804),
        ("rag-test-039", "end_line", 2425),
    ]:
        changed = copy.deepcopy(rows)
        row = next(r for r in changed if r["id"] == qid)
        ev = row["evidence"][-1] if qid == "rag-validation-021" else row["evidence"][0]
        ev[field] = value
        report = verify(root, changed)
        detected = any(e.get("id") == qid and e["error"] == "answer_atom_missing"
                       for e in report["errors"])
        results.append({"id": qid, "mutation": {field: value}, "detected": detected})
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    rows = []
    for split in ("validation", "test"):
        path = args.root / "evaluation/datasets" / f"rag-{split}-001.jsonl"
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    result = verify(args.root, rows)
    if args.self_test:
        result["mutations"] = mutation_checks(args.root, rows)
        if not all(m["detected"] for m in result["mutations"]):
            result["status"] = "FAIL"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "atom_checks"}, ensure_ascii=False))
    raise SystemExit(result["status"] != "PASS")


if __name__ == "__main__":
    main()
