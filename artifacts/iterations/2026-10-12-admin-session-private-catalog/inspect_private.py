"""只读核实私域目标；不实例化管理器/身份库，不输出正文或密钥。"""
import json
from pathlib import Path

from qdrant_client import QdrantClient

from wenjin.index_audit import admit_index
from wenjin.index_registry import IndexTarget
from wenjin.retrieval.legacy_catalog import LegacySearchService
from wenjin.settings import Settings

root = Path(__file__).resolve().parents[3]
settings = Settings.for_project_root(root)
client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key, timeout=60)
service = LegacySearchService(root, client, None, expected_embedding_model="Qwen3-Embedding-0.6B")
routes = []
for path in sorted((root / "configs/indexes-private").glob("*.json")):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data.get("schema_version") == 1 and isinstance(data.get("candidates"), list):
        for raw in data["candidates"]:
            target = IndexTarget.from_dict(raw)
            report = admit_index(target, root=root, qdrant_client=client)
            routes.append({"target_id": target.target_id, "library": target.library,
                           "quick_ready": service._catalog_target_ready(target),
                           "full_admission": report["status"]})
snapshot = service._release_snapshot()
catalog = service.catalog()
private_sources = [{"library_id": row["library_id"],
                    "target_id": source.get("index_target_id"),
                    "source_scope": source.get("source_scope"),
                    "publication_status": source.get("publication_status"),
                    "is_active": source.get("is_active"),
                    "searchable": source.get("searchable")}
                   for row in catalog["libraries"] for source in row["sources"]
                   if source.get("source_scope", "").startswith("private")]
result = {"private_routes": routes, "private_sources": private_sources,
          "release_active_libraries": [key for key, value in snapshot["libraries"].items()
                                       if value.get("active")]}
(Path(__file__).parent / "private-readonly-check.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
client.close()
