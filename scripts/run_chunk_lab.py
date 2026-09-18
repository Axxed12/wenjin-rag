"""比较 OSG Geode 文件的两种切分基线，并检查 API 证据是否跨 chunk。

运行：uv run python scripts/run_chunk_lab.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "corpora/osg-3.6.5/source-manifest.json"
OUTPUT = ROOT / "artifacts/runs/chunk-lab-001.json"


def main() -> None:
    # ingest.py 当前位于项目根目录；脚本从 scripts/ 直接运行时把根目录加入导入路径。
    from ingest import load_documents
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks

    documents = load_documents(MANIFEST)
    document = next(doc for doc in documents if doc["source_path"] == "include/osg/Geode")
    strategies = {
        "fixed": fixed_chunks(document, size=1200, overlap=120),
        "blocks": block_chunks(document, max_chars=1600),
    }
    report = {"source": document["source_path"], "version": document["version"], "strategies": {}}
    for name, chunks in strategies.items():
        containing = [chunk for chunk in chunks if "addDrawable" in chunk.text]
        report["strategies"][name] = {
            "chunk_count": len(chunks),
            "contains_addDrawable": [
                {"chunk_id": c.chunk_id, "start_line": c.start_line, "end_line": c.end_line, "chars": len(c.text)}
                for c in containing
            ],
            "first_two": [
                {"chunk_id": c.chunk_id, "start_line": c.start_line, "end_line": c.end_line, "preview": c.text[:180]}
                for c in chunks[:2]
            ],
        }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
