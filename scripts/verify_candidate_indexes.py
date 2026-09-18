"""逐库核验候选索引：三资产指纹、行数、唯一chunk ID、维度、双Hash、页码和逻辑文档ID。

每个候选必须独立通过核验才能进入跨库路由；本脚本只读，不修改active注册表。
运行：uv run python scripts/verify_candidate_indexes.py --route configs/indexes/multicorpus-candidates-002.json
"""

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from qdrant_client import QdrantClient

from wenjin.index_registry import IndexTarget, resolve_project_path, validate_target

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_FIELDS = (
    "chunk_id",
    "source_path",
    "version",
    "start_line",
    "end_line",
    "strategy",
    "content_sha256",
    "source_raw_sha256",
    "source_canonical_sha256",
    "normalization",
    "page_number",
    "logical_document_id",
)


def read_bm25_rows(path: Path) -> list[dict]:
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        return [dict(row) for row in database.execute(
            "SELECT chunk_id, start_line, end_line, content_sha256, source_raw_sha256, "
            "source_canonical_sha256, normalization, page_number, logical_document_id, "
            "raw_text FROM chunks"
        )]


def read_qdrant_payloads(client: QdrantClient, collection: str) -> list[dict]:
    payloads = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection, limit=512, offset=offset, with_payload=True, with_vectors=False
        )
        payloads.extend(point.payload for point in points)
        if offset is None:
            return payloads


def audit_target(client: QdrantClient, target: IndexTarget, *, root: Path) -> dict:
    """返回该候选的核验结果；任何一项不一致都会把status置为FAIL。"""
    validation = validate_target(target, root=root, qdrant_client=client)
    bm25_path = resolve_project_path(root, target.bm25_path)
    rows = read_bm25_rows(bm25_path)
    payloads = read_qdrant_payloads(client, target.dense_collection)

    bm25_ids = [row["chunk_id"] for row in rows]
    qdrant_ids = [payload["chunk_id"] for payload in payloads]
    by_id = {row["chunk_id"]: row for row in rows}
    mismatched_fields = []
    text_hash_mismatch = []
    for payload in payloads:
        row = by_id.get(payload["chunk_id"])
        if row is None:
            continue
        for field in PAYLOAD_FIELDS:
            if field in {"chunk_id", "source_path", "version", "strategy"}:
                continue
            if payload.get(field) != row.get(field):
                mismatched_fields.append(
                    {"chunk_id": payload["chunk_id"], "field": field,
                     "qdrant": payload.get(field), "bm25": row.get(field)}
                )
        if row["raw_text"] != payload.get("text"):
            text_hash_mismatch.append(
                {"chunk_id": payload["chunk_id"], "reason": "bm25与qdrant正文不一致"}
            )
        elif hashlib.sha256(payload["text"].encode("utf-8")).hexdigest() != payload.get(
            "content_sha256"
        ):
            text_hash_mismatch.append(
                {"chunk_id": payload["chunk_id"], "reason": "正文与content_sha256不一致"}
            )

    page_bm25 = sum(row["page_number"] is not None for row in rows)
    page_qdrant = sum(payload.get("page_number") is not None for payload in payloads)
    logical_bm25 = sum(bool(row["logical_document_id"]) for row in rows)
    logical_qdrant = sum(bool(payload.get("logical_document_id")) for payload in payloads)
    checks = {
        "bm25_ids_unique": len(set(bm25_ids)) == len(bm25_ids),
        "qdrant_ids_unique": len(set(qdrant_ids)) == len(qdrant_ids),
        "chunk_id_sets_equal": set(bm25_ids) == set(qdrant_ids),
        "payload_field_mismatches": len(mismatched_fields),
        "text_hash_mismatches": len(text_hash_mismatch),
        "page_number_matches": page_bm25 == page_qdrant,
        "logical_document_id_matches": logical_bm25 == logical_qdrant,
        "dual_hash_complete": all(
            len(row["content_sha256"] or "") == 64
            and len(row["source_raw_sha256"] or "") == 64
            and len(row["source_canonical_sha256"] or "") == 64
            and bool(row["normalization"])
            for row in rows
        ),
    }
    failed = [name for name, ok in checks.items() if ok is False] + [
        name for name in ("payload_field_mismatches", "text_hash_mismatches")
        if checks[name] > 0
    ]
    return {
        "target_id": target.target_id,
        "library": target.library,
        "version": target.version,
        "strategy": target.strategy,
        "dense_collection": target.dense_collection,
        "bm25_path": target.bm25_path,
        "chunk_count": target.chunk_count,
        "vector_dimension": validation["vector_dimension"],
        "collection_status": validation["collection_status"],
        "bm25_metadata_rows": validation["bm25_metadata_rows"],
        "qdrant_metadata_points": validation["qdrant_metadata_points"],
        "page_number_chunks": {"bm25": page_bm25, "qdrant": page_qdrant},
        "logical_document_id_chunks": {"bm25": logical_bm25, "qdrant": logical_qdrant},
        "checks": checks,
        "mismatched_fields": mismatched_fields[:20],
        "text_hash_mismatch_examples": text_hash_mismatch[:20],
        "status": "PASS" if not failed else "FAIL",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="逐库核验候选索引资产")
    parser.add_argument("--route", type=Path,
                        default=ROOT / "configs/indexes/multicorpus-candidates-002.json")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts/corpus-acceptance-002/candidate-index-audit.json")
    arguments = parser.parse_args()

    route_path = arguments.route.resolve()
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    from wenjin.settings import Settings

    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        results = [
            audit_target(client, IndexTarget.from_dict(item), root=ROOT)
            for item in route["candidates"]
        ]
    finally:
        client.close()
    report = {
        "route_id": route["route_id"],
        "route_path": str(
            route_path.relative_to(ROOT) if route_path.is_relative_to(ROOT) else route_path
        ).replace("\\", "/"),
        "status": "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL",
        "total_chunks": sum(item["chunk_count"] for item in results),
        "targets": results,
        "boundary": "只核验索引资产一致性；未做检索效果评测，不代表检索质量通过。",
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {"status": report["status"], "total_chunks": report["total_chunks"],
         "targets": [{k: item[k] for k in
                      ("target_id", "chunk_count", "vector_dimension", "collection_status",
                       "page_number_chunks", "logical_document_id_chunks", "status")}
                     for item in results]},
        ensure_ascii=False, indent=2))
    raise SystemExit(report["status"] != "PASS")


if __name__ == "__main__":
    main()
