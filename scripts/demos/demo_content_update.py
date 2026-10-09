"""新增一个受控文档，全量重建候选，验证切换、旧题回归和回退。"""

import json
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SENTINEL = "WENJIN_UPDATE_SENTINEL_20260916"
NEW_PATH = "docs/wenjin-update-demo.md"


def create_demo_package(package: Path) -> Path:
    original_manifest = ROOT / "corpora/osg-3.6.5/source-manifest.json"
    original = json.loads(original_manifest.read_text(encoding="utf-8-sig"))
    raw = package / "raw"
    shutil.copytree(ROOT / "raw_data/osg/3.6.5", raw)
    # 临时实验包自带raw目录，不引用正式raw_data，避免修改原语料。
    original.pop("raw_data_subdir", None)
    original["raw_root"] = "raw"
    new_file = raw / NEW_PATH
    new_file.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# Wenjin controlled index update fixture\n"
        f"Sentinel: {SENTINEL}\n"
        "The UpdateDemo::refreshIndex() maintenance test method returns bool.\n"
        "This file exists only to verify full rebuild, switch, regression, and rollback.\n"
    )
    new_file.write_text(text, encoding="utf-8", newline="\n")
    original["library"] = "osg"
    original["version"] = "3.6.5-update-demo-001"
    original["tag"] = "controlled-update-fixture"
    original["scope"] = "selected upstream files plus one local controlled update fixture"
    hashed = hash_content(
        new_file.read_bytes(), normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
    )
    original["files"].append({
        "path": NEW_PATH,
        "raw_sha256": hashed.raw_sha256,
        "canonical_sha256": hashed.canonical_sha256,
        "encoding": "utf-8",
        "normalization": TEXT_NEWLINE_LF_V1,
        "bytes": new_file.stat().st_size,
        "indexable": True,
        "source_url": "urn:wenjin:test-fixture:index-update-001",
    })
    manifest = package / "manifest.json"
    manifest.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    from wenjin.index_registry import IndexRegistryStore, validate_target
    from wenjin.indexing.build import build_candidate_index
    from wenjin.mcp_server import create_default_service
    from wenjin.qdrant_connection import create_qdrant_client
    from wenjin.retrieval.service import SearchConfig
    from wenjin.settings import Settings

    suffix = uuid4().hex[:8]
    package = ROOT / f"data/packages/osg-update-demo-{suffix}"
    collection = f"osg_3_6_5_update_demo_{suffix}"
    bm25 = ROOT / f"data/indexes/osg_update_demo_{suffix}.sqlite"
    registry = IndexRegistryStore(ROOT / "data/indexes/index-registry.json")
    original = registry.active()
    settings = Settings()
    client = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    switched = False
    candidate = None
    report = {"run_id": "content-update-switch-rollback-001", "original": asdict(original)}
    started = time.perf_counter()
    try:
        manifest = create_demo_package(package)
        candidate = build_candidate_index(
            manifest_path=manifest,
            target_id=f"osg-update-demo-{suffix}",
            strategy="blocks",
            dense_collection=collection,
            bm25_path=bm25,
            qdrant_client=client,
            embedder=settings.embedding(),
            embedding_model=settings.embedding_model_path.name,
            project_root=ROOT,
        )
        report["candidate"] = asdict(candidate)
        report["validation"] = validate_target(candidate, root=ROOT, qdrant_client=client)
        state = registry.switch(candidate, reason="controlled added-file update demo")
        switched = True
        report["switch_revision"] = state["revision"]

        service = create_default_service()
        try:
            new_result = service.search(
                f"{SENTINEL} 的 refreshIndex 返回什么？",
                config=SearchConfig(version=candidate.version, top_k=3),
            )
            regressions = [
                ("Viewer 的 run() 做什么？", "include/osgViewer/Viewer"),
                ("NodeMask 如何控制遍历？", "include/osg/Node"),
                ("Geode 添加 Drawable 的接口？", "include/osg/Geode"),
                ("readNodeFile 失败返回什么？", "include/osgDB/ReadFile"),
            ]
            regression_rows = []
            for query, expected_source in regressions:
                result = service.search(
                    query, config=SearchConfig(version=candidate.version, top_k=5)
                )
                sources = [item["source_path"] for item in result["evidence"]]
                regression_rows.append({
                    "query": query,
                    "expected_source": expected_source,
                    "sources": sources,
                    "passed": expected_source in sources,
                })
        finally:
            service.close()
        report["new_document"] = {
            "query": new_result["query"],
            "sources": [item["source_path"] for item in new_result["evidence"]],
            "sentinel_found": any(SENTINEL in item["text"] for item in new_result["evidence"]),
        }
        report["regressions"] = regression_rows
        if not report["new_document"]["sentinel_found"] or not all(
            row["passed"] for row in regression_rows
        ):
            raise RuntimeError("新增文档或旧问题回归失败")

        state = registry.rollback(reason="complete controlled added-file demo")
        switched = False
        report["rollback_revision"] = state["revision"]
        report["active_after_rollback"] = registry.active().target_id

        old_service = create_default_service(use_reranker=False)
        try:
            after = old_service.search(
                SENTINEL, config=SearchConfig(version=original.version, top_k=5)
            )
        finally:
            old_service.close()
        report["after_rollback"] = {
            "sources": [item["source_path"] for item in after["evidence"]],
            "new_document_absent": NEW_PATH not in {
                item["source_path"] for item in after["evidence"]
            },
        }
        if registry.active().target_id != original.target_id or not report["after_rollback"][
            "new_document_absent"
        ]:
            raise RuntimeError("回退后的active或新增文档隔离检查失败")
        report["status"] = "PASS"
    finally:
        if switched:
            registry.rollback(reason="automatic rollback after content update demo failure")
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()
        bm25.unlink(missing_ok=True)
        shutil.rmtree(package, ignore_errors=True)
        report["seconds"] = round(time.perf_counter() - started, 3)
        report["candidate_assets_cleaned"] = (
            not bm25.exists() and not package.exists()
        )
        output = ROOT / "artifacts/runs/content-update-switch-rollback-001.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
