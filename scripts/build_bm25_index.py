"""为 OSG chunks 建立 SQLite FTS5/BM25 索引。"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from ingest import load_documents
    from wenjin.index_registry import IndexRegistryStore
    from wenjin.indexing.bm25 import build_bm25_index
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    args = parser.parse_args()
    documents = load_documents(ROOT / "corpora/osg-3.6.5/source-manifest.json")
    chunks = []
    for document in documents:
        if args.strategy == "fixed":
            chunks.extend(fixed_chunks(document, size=1200, overlap=120))
        else:
            chunks.extend(block_chunks(document, max_chars=1600))
    active = IndexRegistryStore(ROOT / "data/indexes/index-registry.json").active()
    output = (
        ROOT / active.bm25_path
        if args.strategy == active.strategy
        else ROOT / "data/indexes" / f"osg_3_6_5_bm25_{args.strategy}.sqlite"
    )
    started = time.perf_counter()
    build_bm25_index(output, chunks)
    report = {
        "index": str(output),
        "strategy": args.strategy,
        "chunk_count": len(chunks),
        "index_target_id": active.target_id if args.strategy == active.strategy else None,
        "seconds": round(time.perf_counter() - started, 3),
        "note": "FTS5/BM25 keyword baseline; no retrieval quality claim.",
    }
    report_path = ROOT / "artifacts/runs" / f"bm25-index-{args.strategy}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
