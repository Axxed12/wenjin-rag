"""为 OSG chunks 建立 SQLite FTS5/BM25 索引。

**不读运行时注册表**：首次建库时注册表还不存在（`manage_indexes.py init` 需要索引
已经建好并通过校验），依赖它会让新克隆陷入"建库要先有注册表、初始化注册表要先有索引"
的循环。产物名取自**已提交的目标配置**，冷启动和已有索引的机器走同一条路。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_TARGET = "configs/indexes/osg-3.6.5-blocks-dualhash.json"


def main() -> None:
    from ingest import load_documents
    from wenjin.index_registry import load_target
    from wenjin.indexing.bm25 import build_bm25_index
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="项目根目录；用于在临时环境里验证首次建库顺序")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="索引目标配置（已提交）；产物名取自它")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析文档与产物名，不算向量、不写盘")
    args = parser.parse_args()
    root = args.root.resolve()
    # load_documents 的可信语料根默认指向仓库内的 raw_data。--root 指到别处时
    # 必须一起切过去，否则会读到**另一个**语料的原文并按哈希不符报错。
    os.environ["WENJIN_RAW_DATA_ROOT"] = str(root / "raw_data")
    documents = load_documents(root / "corpora/osg-3.6.5/source-manifest.json")
    chunks = []
    for document in documents:
        if args.strategy == "fixed":
            chunks.extend(fixed_chunks(document, size=1200, overlap=120))
        else:
            chunks.extend(block_chunks(document, max_chars=1600))
    target = load_target(root / args.target)
    matches_target = args.strategy == target.strategy
    output = (
        root / target.bm25_path
        if matches_target
        else root / "data/indexes" / f"osg_3_6_5_bm25_{args.strategy}.sqlite"
    )
    if args.dry_run:
        print(json.dumps({
            "root": str(root), "strategy": args.strategy, "chunks": len(chunks),
            "bm25_path": str(output.relative_to(root)).replace("\\", "/"),
            "index_target_id": target.target_id if matches_target else None,
            "dry_run": True,
            "note": "只解析；不需要运行时注册表，也不算向量",
        }, ensure_ascii=False, indent=2))
        return
    started = time.perf_counter()
    build_bm25_index(output, chunks)
    report = {
        "index": str(output),
        "strategy": args.strategy,
        "chunk_count": len(chunks),
        "index_target_id": target.target_id if matches_target else None,
        "seconds": round(time.perf_counter() - started, 3),
        "note": "FTS5/BM25 keyword baseline; no retrieval quality claim.",
    }
    # 报告写到选定的 root；父目录要显式创建，全新临时根里 artifacts/runs 还不存在。
    report_path = root / "artifacts/runs" / f"bm25-index-{args.strategy}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
