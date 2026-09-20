"""构建 OSG 文档的 dense Qdrant 索引。

**不读运行时注册表**：首次建库时注册表还不存在（`manage_indexes.py init` 需要索引
已经建好并通过校验），依赖它会让新克隆陷入"建库要先有注册表、初始化注册表要先有索引"
的循环。集合名取自**已提交的目标配置**，冷启动和已有索引的机器走同一条路。

示例：
    uv run python scripts/build_dense_index.py --strategy blocks
    uv run python scripts/build_dense_index.py --strategy fixed
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
    from qdrant_client import QdrantClient

    from ingest import load_documents
    from wenjin.index_registry import load_target
    from wenjin.indexing.dense import create_dense_collection, embed_chunks
    from wenjin.ingestion.chunking import block_chunks, fixed_chunks
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["fixed", "blocks"], default="blocks")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="项目根目录；用于在临时环境里验证首次建库顺序")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="索引目标配置（已提交）；集合名取自它")
    parser.add_argument("--dry-run", action="store_true",
                        help="只解析文档与产物名，不加载模型、不连 Qdrant、不写盘")
    args = parser.parse_args()
    root = args.root.resolve()
    # load_documents 的可信语料根默认指向仓库内的 raw_data。--root 指到别处时
    # 必须一起切过去，否则会读到**另一个**语料的原文并按哈希不符报错。
    os.environ["WENJIN_RAW_DATA_ROOT"] = str(root / "raw_data")
    settings = Settings()
    documents = load_documents(root / "corpora/osg-3.6.5/source-manifest.json")
    chunks = []
    for document in documents:
        if args.strategy == "fixed":
            chunks.extend(fixed_chunks(document, size=1200, overlap=120))
        else:
            chunks.extend(block_chunks(document, max_chars=1600))
    target = load_target(root / args.target)
    matches_target = args.strategy == target.strategy
    collection = (
        target.dense_collection
        if matches_target
        else f"osg_3_6_5_dense_{args.strategy}"
    )
    if args.dry_run:
        print(json.dumps({
            "root": str(root), "strategy": args.strategy, "chunks": len(chunks),
            "collection": collection,
            "index_target_id": target.target_id if matches_target else None,
            "dry_run": True,
            "note": "只解析；不需要运行时注册表，也不加载模型",
        }, ensure_ascii=False, indent=2))
        return
    started = time.perf_counter()
    embedder = settings.embedding()
    vectors = embed_chunks(embedder, chunks)
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        create_dense_collection(client, collection, chunks, vectors)
        report = {
            "collection": collection,
            "strategy": args.strategy,
            "source_version": "3.6.5",
            "index_target_id": target.target_id if matches_target else None,
            "chunk_count": len(chunks),
            "vector_dimension": len(vectors[0]),
            "seconds": round(time.perf_counter() - started, 3),
            "model_path": str(settings.embedding_model_path),
            "note": "Dense index build only; no retrieval quality claim.",
        }
    finally:
        client.close()
    # 报告写到选定的 root，不是仓库：--root 指到别处时所有产物都必须留在那里，
    # 否则临时环境的验证会往真实仓库里塞文件。
    output = root / "artifacts/runs" / f"dense-index-{args.strategy}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
