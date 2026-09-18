"""只用validation验证新格式解析后，原始证据是否仍可定位并保留关键文字。"""

import argparse
import json
from collections import Counter
from pathlib import Path

from wenjin.ingestion.multiformat import parse_file


def normalized(value: str) -> str:
    return "".join(value.casefold().split())


def evaluate(root: Path, dataset: Path) -> dict:
    rows = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines()]
    parsed = {}
    file_reports = {}
    for row in rows:
        for evidence in row["evidence"]:
            rel = evidence["path"]
            if rel not in parsed:
                path = (root / rel).resolve()
                if not path.is_relative_to(root.resolve()):
                    raise ValueError(f"证据路径越界：{rel}")
                units = parse_file(path, rel)
                parsed[rel] = units
                file_reports[rel] = {
                    "parser": units[0].parser,
                    "units": len(units),
                    "source_lines": units[0].end_line,
                    "text_chars": sum(len(unit.text) for unit in units),
                    "warnings": [warning for unit in units for warning in unit.warnings],
                }
    checks = []
    for row in rows:
        for evidence in row["evidence"]:
            units = parsed[evidence["path"]]
            if evidence.get("page") is not None:
                unit = next((u for u in units if u.page_number == evidence["page"]), None)
                text = unit.text if unit else ""
                locator_valid = unit is not None
            else:
                unit = units[0]
                lines = unit.text.splitlines()
                start, end = evidence["start_line"], evidence["end_line"]
                locator_valid = 1 <= start <= end <= len(lines)
                text = "\n".join(lines[start - 1:end]) if locator_valid else ""
            atoms = row.get("required_evidence_atoms", [])
            missing = [atom for atom in atoms if normalized(atom) not in normalized(text)]
            checks.append({
                "id": row["id"], "path": evidence["path"],
                "page": evidence.get("page"), "start_line": evidence.get("start_line"),
                "end_line": evidence.get("end_line"), "locator_valid": locator_valid,
                "text_present": bool(text.strip()), "required_atoms_missing": missing,
            })
    failures = [c for c in checks if not c["locator_valid"] or not c["text_present"]
                or c["required_atoms_missing"]]
    return {
        "status": "PASS" if not failures else "FAIL",
        "dataset": str(dataset), "questions": len(rows), "evidence_spans": len(checks),
        "files": len(parsed),
        "parser_counts": dict(Counter(report["parser"] for report in file_reports.values())),
        "file_reports": file_reports, "checks": checks, "failures": failures,
        "boundary": "验证定位和显式关键原子；不等于全部答案语义或检索效果通过。",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path,
                        default=Path("evaluation/datasets/rag-validation-001.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = evaluate(root, args.dataset.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in (
        "status", "questions", "evidence_spans", "files", "parser_counts", "failures"
    )}, ensure_ascii=False, indent=2))
    raise SystemExit(report["status"] != "PASS")


if __name__ == "__main__":
    main()
