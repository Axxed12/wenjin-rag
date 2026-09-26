"""一次加载模型，为5个新语料分别构建隔离候选索引；不修改active注册表。

资产命名规则：配置 `<corpus-id>-<strategy>-candidate-<代次>.json`、
collection `<corpus>_dense_<strategy>_candidate_<代次>`、
BM25 `<corpus>_bm25_<strategy>_candidate_<代次>.sqlite`。
代次只增不改：已有配置会被复用并重新校验，绝不覆盖。

运行：uv run python -m scripts.build_multicorpus_candidates --generation 002 --strategy blocks-split
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from qdrant_client import QdrantClient

from wenjin.index_registry import load_target, validate_target
from wenjin.indexing.build import build_candidate_index
from wenjin.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
CORPORA = ["osgearth-3.8", "gdal-3.13.1", "proj-9.8.1",
           "ogc-3dtiles-1.1", "ogc-wms-1.3.0"]


def safe_name(corpus_id: str) -> str:
    return corpus_id.replace("-", "_").replace(".", "_")


def main() -> None:
    parser = argparse.ArgumentParser(description="为5个新语料构建隔离候选索引")
    parser.add_argument("--generation", default="001", help="候选代次，例如001或002")
    parser.add_argument("--strategy", default="blocks",
                        choices=["blocks", "blocks-split", "blocks-split-parented"])
    parser.add_argument(
        "--artifacts",
        type=Path,
        help="构建报告输出目录；默认为artifacts/corpus-acceptance-<代次>",
    )
    arguments = parser.parse_args()
    generation = arguments.generation
    strategy = arguments.strategy
    artifact_dir = arguments.artifacts or ROOT / f"artifacts/corpus-acceptance-{generation}"
    stem_suffix = f"{strategy}-candidate-{generation}"
    file_suffix = strategy.replace("-", "_")

    settings = Settings()
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    results = []
    try:
        for corpus_id in CORPORA:
            stem = safe_name(corpus_id)
            config = ROOT / "configs/indexes" / f"{corpus_id}-{stem_suffix}.json"
            bm25 = ROOT / "data/indexes" / f"{stem}_bm25_{file_suffix}_candidate_{generation}.sqlite"
            if config.exists():
                target = load_target(config)
                validation = validate_target(target, root=ROOT, qdrant_client=client)
                results.append({"corpus_id": corpus_id, "target": asdict(target),
                                "validation": validation, "action": "reused_validated"})
                continue
            collection = f"{stem}_dense_{file_suffix}_candidate_{generation}"
            if client.collection_exists(collection) or bm25.exists():
                raise FileExistsError(
                    f"上次未完成的候选资产存在，请先审查并清理：{collection} / {bm25}"
                )
            target = build_candidate_index(
                manifest_path=ROOT / "corpora" / corpus_id / "source-manifest.json",
                target_id=f"{corpus_id}-{stem_suffix}",
                strategy=strategy,
                dense_collection=collection,
                bm25_path=bm25,
                qdrant_client=client,
                embedder=embedder,
                embedding_model=settings.embedding_model_path.name,
                project_root=ROOT,
            )
            validation = validate_target(target, root=ROOT, qdrant_client=client)
            config.write_text(json.dumps(asdict(target), ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
            results.append({"corpus_id": corpus_id, "target": asdict(target),
                            "validation": validation})
    finally:
        client.close()
    route = {
        "schema_version": 1,
        "route_id": f"multicorpus-candidates-{generation}",
        "strategy": strategy,
        "routing": "explicit library and version; fan out only when caller omits library",
        "active_unchanged": "osg-3.6.5-blocks-dualhash-003",
        "candidates": [item["target"] for item in results],
    }
    route_path = ROOT / "configs/indexes" / f"multicorpus-candidates-{generation}.json"
    route_path.write_text(json.dumps(route, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    output = artifact_dir / f"candidate-index-build-{generation}.json"
    output.write_text(json.dumps({"results": results, "route": route},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "PASS", "targets": [
        {"id": x["target"]["target_id"], "chunks": x["target"]["chunk_count"]}
        for x in results
    ]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
