"""克隆当前索引，验证候选、切换、新服务查询、回退并清理候选。"""

import json
import shutil
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def clone_collection(client, source: str, target: str, *, vector_dimension: int) -> int:
    from qdrant_client import models

    client.create_collection(
        target,
        vectors_config=models.VectorParams(
            size=vector_dimension, distance=models.Distance.COSINE
        ),
    )
    offset = None
    copied = 0
    while True:
        records, offset = client.scroll(
            source,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        points = [
            models.PointStruct(id=record.id, vector=record.vector, payload=record.payload)
            for record in records
        ]
        if points:
            client.upsert(target, points=points, wait=True)
            copied += len(points)
        if offset is None:
            return copied


def main() -> None:
    from qdrant_client import QdrantClient

    from wenjin.index_registry import (
        IndexRegistryStore,
        resolve_project_path,
        validate_target,
    )
    from wenjin.mcp_server import create_default_service
    from wenjin.retrieval.service import SearchConfig
    from wenjin.settings import Settings

    registry = IndexRegistryStore(ROOT / "data/indexes/index-registry.json")
    original = registry.active()
    suffix = uuid4().hex[:8]
    candidate_collection = f"{original.dense_collection}_candidate_{suffix}"
    original_bm25 = resolve_project_path(ROOT, original.bm25_path)
    candidate_bm25 = original_bm25.with_name(
        f"{original_bm25.stem}_candidate_{suffix}{original_bm25.suffix}"
    )
    candidate = replace(
        original,
        target_id=f"{original.target_id}-candidate-{suffix}",
        dense_collection=candidate_collection,
        bm25_path=str(candidate_bm25.relative_to(ROOT)).replace("\\", "/"),
    )
    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    switched = False
    report = {
        "run_id": "index-switch-rollback-001",
        "original": asdict(original),
        "candidate": asdict(candidate),
    }
    started = time.perf_counter()
    try:
        shutil.copy2(original_bm25, candidate_bm25)
        report["cloned_points"] = clone_collection(
            client,
            original.dense_collection,
            candidate_collection,
            vector_dimension=original.vector_dimension,
        )
        report["candidate_validation"] = validate_target(
            candidate, root=ROOT, qdrant_client=client
        )
        switched_state = registry.switch(candidate, reason="identical clone switch demo")
        switched = True
        report["switch_revision"] = switched_state["revision"]
        service = create_default_service()
        try:
            smoke = service.search(
                "osgViewer::Viewer 的 run() 做什么？",
                config=SearchConfig(version=candidate.version, top_k=1),
            )
        finally:
            service.close()
        report["smoke"] = {
            "index_target_id": smoke["index_target_id"],
            "collection": smoke["collection"],
            "evidence_count": len(smoke["evidence"]),
            "top_source": smoke["evidence"][0]["source_path"] if smoke["evidence"] else None,
        }
        if smoke["index_target_id"] != candidate.target_id or not smoke["evidence"]:
            raise RuntimeError("候选索引切换后的新服务冒烟失败")
        rollback_state = registry.rollback(reason="complete identical clone demo")
        switched = False
        report["rollback_revision"] = rollback_state["revision"]
        report["active_after_rollback"] = registry.active().target_id
        if registry.active().target_id != original.target_id:
            raise RuntimeError("回退后active target不正确")
        report["status"] = "PASS"
    finally:
        if switched:
            registry.rollback(reason="automatic rollback after demo failure")
        if client.collection_exists(candidate_collection):
            client.delete_collection(candidate_collection)
        client.close()
        candidate_bm25.unlink(missing_ok=True)
        report["seconds"] = round(time.perf_counter() - started, 3)
        output = ROOT / "artifacts/runs/index-switch-rollback-001.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
