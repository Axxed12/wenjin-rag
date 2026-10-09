"""把实测解析器与验收层级写回新语料manifest；不改变indexable或索引。"""

import json
from pathlib import Path

from wenjin.ingestion.multiformat import parser_for_path

ROOT = Path(__file__).resolve().parents[2]
VALIDATION = ROOT / "evaluation/datasets/rag-validation-001.jsonl"


def main() -> None:
    validation_paths = {
        evidence["path"]
        for line in VALIDATION.read_text(encoding="utf-8").splitlines()
        for evidence in json.loads(line)["evidence"]
    }
    changed = []
    for path in sorted((ROOT / "corpora").glob("*/source-manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        prefix = f"raw_data/{manifest['raw_data_subdir']}/"
        for entry in manifest["files"]:
            if not entry.get("retrieval_eligible"):
                continue
            parser = "pymupdf-page-v1" if entry["path"].lower().endswith(".pdf") \
                else parser_for_path(Path(entry["path"]))
            entry["parser"] = parser
            formal_path = prefix + entry["path"]
            entry["parser_status"] = (
                "validation_evidence_passed"
                if formal_path in validation_paths
                else "format_smoke_passed"
            )
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        changed.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    print(json.dumps({"updated_manifests": changed, "indexable_changed": False},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
