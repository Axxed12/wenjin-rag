"""从资料包manifest构建不覆盖现有索引的候选target配置。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from qdrant_client import QdrantClient

    from wenjin.index_registry import validate_target
    from wenjin.indexing.build import build_candidate_index
    from wenjin.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--bm25-output", type=Path, required=True)
    parser.add_argument("--target-output", type=Path, required=True)
    # 必须与 build_candidate_index 接受的三种策略一致：少了 blocks-split，
    # 这个入口就建不出已验收候选所用的那种切片。
    parser.add_argument("--strategy", choices=["blocks", "blocks-split", "fixed"],
                        default="blocks")
    args = parser.parse_args()
    if args.target_output.exists():
        raise FileExistsError(f"目标配置已存在：{args.target_output}")
    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        target = build_candidate_index(
            manifest_path=args.manifest,
            target_id=args.target_id,
            strategy=args.strategy,
            dense_collection=args.collection,
            bm25_path=args.bm25_output,
            qdrant_client=client,
            embedder=settings.embedding(),
            embedding_model=settings.embedding_model_path.name,
            project_root=ROOT,
        )
        validation = validate_target(target, root=ROOT, qdrant_client=client)
    finally:
        client.close()
    args.target_output.parent.mkdir(parents=True, exist_ok=True)
    args.target_output.write_text(
        json.dumps(asdict(target), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"target": asdict(target), "validation": validation},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
