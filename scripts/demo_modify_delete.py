"""受控修改/删除资料后的全量重建、双索引核验、切换及回退演示。"""

import json
import shutil
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODIFIED_PATH = "README.md"
DELETED_PATH = "examples/osgshape/osgshape.cpp"
SENTINEL = "WENJIN_MODIFY_SENTINEL_20260917"


def make_package(package: Path, *, mode: str) -> Path:
    """只在临时包里改动原始文件，并同步manifest哈希/文件清单。"""
    if mode not in {"modify", "delete"}:
        raise ValueError("mode必须为modify或delete")
    original = json.loads(
        (ROOT / "corpora/osg-3.6.5/source-manifest.json").read_text(encoding="utf-8-sig")
    )
    raw = package / "raw"
    shutil.copytree(ROOT / "raw_data/osg/3.6.5", raw)
    # 临时实验包自带raw目录，不引用正式raw_data，避免修改原语料。
    original.pop("raw_data_subdir", None)
    original["raw_root"] = "raw"
    original["library"] = "osg"
    original["version"] = f"3.6.5-{mode}-demo-001"
    original["tag"] = f"controlled-{mode}-fixture"
    original["scope"] = f"selected upstream files with one local {mode} fixture"
    if mode == "modify":
        source = raw / MODIFIED_PATH
        with source.open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"\n## Controlled modification fixture\n{SENTINEL} returns bool.\n")
        entry = next(item for item in original["files"] if item["path"] == MODIFIED_PATH)
        hashed = hash_content(
            source.read_bytes(), normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
        )
        entry.pop("sha256", None)
        entry["raw_sha256"] = hashed.raw_sha256
        entry["canonical_sha256"] = hashed.canonical_sha256
        entry["encoding"] = "utf-8"
        entry["normalization"] = TEXT_NEWLINE_LF_V1
        entry["bytes"] = source.stat().st_size
        entry["source_url"] = "urn:wenjin:test-fixture:modify-001"
    else:
        source = raw / DELETED_PATH
        source.unlink()
        original["files"] = [
            item for item in original["files"] if item["path"] != DELETED_PATH
        ]
    manifest = package / "manifest.json"
    manifest.write_text(
        json.dumps(original, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def source_counts(client, collection: str, bm25_path: Path, source_path: str) -> dict:
    """分别从Qdrant payload和SQLite元数据读取目标文件的chunk数量。"""
    from qdrant_client import models

    condition = models.Filter(
        must=[models.FieldCondition(
            key="source_path", match=models.MatchValue(value=source_path)
        )]
    )
    dense = client.count(collection, count_filter=condition, exact=True).count
    with sqlite3.connect(bm25_path) as database:
        bm25 = database.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_path = ?", (source_path,)
        ).fetchone()[0]
    return {"qdrant": dense, "bm25": bm25}


def run_case(mode: str, *, client, embedder, registry, settings) -> dict:
    from wenjin.index_registry import validate_target
    from wenjin.indexing.build import build_candidate_index
    from wenjin.mcp_server import create_default_service
    from wenjin.retrieval.service import SearchConfig

    suffix = uuid4().hex[:8]
    package = ROOT / f"data/packages/osg-{mode}-demo-{suffix}"
    collection = f"osg_3_6_5_{mode}_demo_{suffix}"
    bm25 = ROOT / f"data/indexes/osg_{mode}_demo_{suffix}.sqlite"
    original = registry.active()
    source_path = MODIFIED_PATH if mode == "modify" else DELETED_PATH
    result = {"mode": mode, "original_target_id": original.target_id}
    switched = False
    started = time.perf_counter()
    try:
        manifest = make_package(package, mode=mode)
        candidate = build_candidate_index(
            manifest_path=manifest,
            target_id=f"osg-{mode}-demo-{suffix}",
            strategy="blocks",
            dense_collection=collection,
            bm25_path=bm25,
            qdrant_client=client,
            embedder=embedder,
            embedding_model=settings.embedding_model_path.name,
            project_root=ROOT,
        )
        result["candidate"] = asdict(candidate)
        result["validation"] = validate_target(candidate, root=ROOT, qdrant_client=client)
        result["source_counts_before_switch"] = source_counts(
            client, collection, bm25, source_path
        )
        if mode == "modify":
            old_bm25 = ROOT / original.bm25_path
            result["original_source_counts"] = source_counts(
                client, original.dense_collection, old_bm25, source_path
            )
            if result["source_counts_before_switch"]["qdrant"] < 1:
                raise RuntimeError("修改文件未进入Qdrant")
            if result["source_counts_before_switch"]["bm25"] < 1:
                raise RuntimeError("修改文件未进入BM25")
        elif result["source_counts_before_switch"] != {"qdrant": 0, "bm25": 0}:
            raise RuntimeError("删除文件仍存在于候选双索引")

        state = registry.switch(candidate, reason=f"controlled {mode} fixture demo")
        switched = True
        result["switch_revision"] = state["revision"]
        service = create_default_service(use_reranker=False)
        try:
            if service.index_target_id != candidate.target_id:
                raise RuntimeError("新Service未加载候选target")
            if mode == "modify":
                query = f"{SENTINEL} returns what?"
                searched = service.search(
                    query, config=SearchConfig(version=candidate.version, top_k=5)
                )
                result["modified_query"] = {
                    "sources": [item["source_path"] for item in searched["evidence"]],
                    "sentinel_found": any(
                        SENTINEL in item["text"] for item in searched["evidence"]
                    ),
                }
                if not result["modified_query"]["sentinel_found"]:
                    raise RuntimeError("修改后的sentinel未进入Top-5")
            else:
                result["deleted_source_not_in_manifest"] = (
                    DELETED_PATH not in service.source_urls
                )
                if not result["deleted_source_not_in_manifest"]:
                    raise RuntimeError("新Service仍包含删除文件的来源")
            old_query = service.search(
                "Viewer 的 run() 做什么？",
                config=SearchConfig(version=candidate.version, top_k=5),
            )
            result["old_query_regression"] = any(
                item["source_path"] == "include/osgViewer/Viewer"
                for item in old_query["evidence"]
            )
            if not result["old_query_regression"]:
                raise RuntimeError("旧Viewer问题回归失败")
        finally:
            service.close()

        state = registry.rollback(reason=f"complete controlled {mode} fixture demo")
        switched = False
        result["rollback_revision"] = state["revision"]
        result["active_after_rollback"] = registry.active().target_id
        old_bm25 = ROOT / original.bm25_path
        result["source_counts_after_rollback"] = source_counts(
            client, original.dense_collection, old_bm25, source_path
        )
        if result["active_after_rollback"] != original.target_id:
            raise RuntimeError("回退未恢复原active")
        if mode == "modify":
            if result["source_counts_after_rollback"] != result["original_source_counts"]:
                raise RuntimeError("回退后原文件chunk数量变化")
        elif min(result["source_counts_after_rollback"].values()) < 1:
            raise RuntimeError("回退后删除文件未恢复")
        result["status"] = "PASS"
    finally:
        if switched:
            registry.rollback(reason=f"automatic rollback after {mode} demo failure")
        if client.collection_exists(collection):
            client.delete_collection(collection)
        bm25.unlink(missing_ok=True)
        shutil.rmtree(package, ignore_errors=True)
        result["seconds"] = round(time.perf_counter() - started, 3)
        result["candidate_assets_cleaned"] = (
            not client.collection_exists(collection)
            and not bm25.exists()
            and not package.exists()
        )
    return result


def main() -> None:
    from qdrant_client import QdrantClient

    from wenjin.index_registry import IndexRegistryStore
    from wenjin.settings import Settings

    registry = IndexRegistryStore(ROOT / "data/indexes/index-registry.json")
    original = registry.active()
    settings = Settings()
    embedder = settings.embedding()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    report = {
        "run_id": "content-modify-delete-001",
        "original_target_id": original.target_id,
        "cases": [],
    }
    try:
        for mode in ("modify", "delete"):
            report["cases"].append(
                run_case(
                    mode, client=client, embedder=embedder,
                    registry=registry, settings=settings,
                )
            )
        report["active_after_all_cases"] = registry.active().target_id
        report["status"] = (
            "PASS" if report["active_after_all_cases"] == original.target_id
            and all(item["status"] == "PASS" for item in report["cases"])
            else "FAIL"
        )
    finally:
        client.close()
        output = ROOT / "artifacts/runs/content-modify-delete-001.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
