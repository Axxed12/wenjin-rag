"""Release resolver/MCP contracts using synthetic OSG and route-only libraries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp import Client
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.mcp.runtime import (
    RuntimeIdentity,
    create_release_resolver,
    create_router,
)
from wenjin.mcp.server import build_mcp_server
from wenjin.releases import ReleaseCatalog, ReleaseService
from wenjin.releases.resolver import RequestReleaseResolver, release_catalog_path
from wenjin.retrieval.router import DEFAULT_MULTI_CORPUS_ROUTE

pytestmark = pytest.mark.integration
_ADMIN_TOKEN = "synthetic-release-runtime-test-token"


class _UnusedDefaultSearchService:
    """release resolver route should serve the default library in these cases."""

    supported_version = "1.0"
    index_target_id = "osg-release-a"


def _access_for(*corpus_ids: str) -> AccessRegistry:
    return AccessRegistry(
        None,
        {
            corpus_id: AccessPolicy(corpus_id, "public", ("*",))
            for corpus_id in corpus_ids
        },
    )


def _tool_data(result) -> dict:
    assert result.is_error is False
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def test_default_osg_registry_and_route_library_survive_lazy_first_publish_and_bind_download(
    tmp_path,
):
    root = tmp_path / "runtime-project-root"
    qdrant = QdrantClient(":memory:")
    osg_assets = build_two_version_release_fixture(
        root=root, library_id="osg", qdrant_client=qdrant
    )
    route_assets = build_two_version_release_fixture(
        root=root, library_id="alpha", namespace="alpha", qdrant_client=qdrant
    )
    route_path = root / DEFAULT_MULTI_CORPUS_ROUTE
    route_path.parent.mkdir(parents=True, exist_ok=True)
    route_path.write_text(
        json.dumps({
            "schema_version": 1,
            "candidates": [route_assets["targets"]["A"].__dict__],
        }),
        encoding="utf-8",
    )

    # OSG仅在index-registry中；route003候选只有alpha，照真实装配顺序构造router。
    legacy_router = create_router(
        root=root,
        qdrant_client=qdrant,
        embedder=osg_assets["embedder"],
        reranker=None,
    )
    assert legacy_router is not None
    assert {item.library for item in legacy_router.indices} == {"osg", "alpha"}
    osg_legacy = next(item for item in legacy_router.indices if item.library == "osg")
    alpha_legacy = next(item for item in legacy_router.indices if item.library == "alpha")
    assert osg_legacy.target_id == osg_assets["targets"]["A"].target_id
    assert osg_legacy.target_id not in {
        candidate["target_id"] for candidate in json.loads(route_path.read_text())["candidates"]
    }

    resolver = create_release_resolver(
        root=root,
        qdrant_client=qdrant,
        embedder=osg_assets["embedder"],
        reranker=None,
        legacy_router=legacy_router,
        search_lock=legacy_router.search_lock,
        expected_embedding_model=osg_assets["targets"]["A"].embedding_model,
    )
    access = _access_for(
        "osg-1.0", "osg-2.0", "alpha-1.0", "alpha-2.0"
    )
    audit = SearchAudit(root / "artifacts" / "release-runtime-audit.jsonl")
    server = build_mcp_server(
        _UnusedDefaultSearchService(),
        audit=audit,
        access=access,
        raw_root=root / "raw_data",
        project_root=root,
        identity=RuntimeIdentity("osg-1.0", "synthetic-registry", osg_legacy.target_id),
        corpus_router=legacy_router,
        release_resolver=resolver,
    )

    try:
        # Resolver/MCP启动在release catalog之前：OSG默认仍来自registry，route只含alpha。
        before_catalog = resolver.begin_request()
        assert before_catalog.default_library == "osg"
        assert before_catalog.default_target_id == osg_legacy.target_id
        assert resolver.runtime["qdrant_client"] is qdrant
        assert resolver.runtime["embedder"] is osg_assets["embedder"]
        assert resolver.runtime["search_lock"] is legacy_router.search_lock

        async def call_before_publish():
            async with Client(server) as client:
                return _tool_data(await client.call_tool(
                    "search_docs", {"query": osg_assets["queries"]["A"], "top_k": 1}
                ))

        initial = asyncio.run(call_before_publish())
        assert initial["routed_libraries"][0]["target_id"] == osg_legacy.target_id
        assert osg_assets["expected_markers"]["A"] in initial["evidence"][0]["text"]

        # 首次publish在MCP对象创建后才创建catalog，Lazy resolver必须下一请求立即发现。
        catalog = ReleaseCatalog(release_catalog_path(root))
        service = ReleaseService(root, catalog, qdrant)
        release_a = service.prepare("osg", osg_assets["targets"]["A"], reason="first release A")
        release_b = service.prepare("osg", osg_assets["targets"]["B"], reason="release B")
        active_a = service.activate(
            "osg", release_a["release_id"], expected_revision=0, reason="activate first A"
        )
        assert active_a["revision"] == 1

        async def call_a_after_publish():
            async with Client(server) as client:
                return _tool_data(await client.call_tool(
                    "search_docs", {"query": osg_assets["queries"]["A"], "top_k": 1}
                ))

        first_release_a = asyncio.run(call_a_after_publish())
        assert first_release_a["routed_libraries"][0]["target_id"] == release_a["target"]["target_id"]
        assert osg_assets["expected_markers"]["A"] in first_release_a["evidence"][0]["text"]
        assert _release_id_from_evidence(first_release_a["evidence"][0]) == release_a["release_id"]

        # 先拿到A快照，再在同一个MCP search_docs请求材料化之前切到B。
        entered = threading.Event()
        resume = threading.Event()
        original_begin = resolver.begin_request
        blocked_once = {"value": True}

        def block_after_snapshot():
            snapshot = original_begin()
            if blocked_once["value"]:
                blocked_once["value"] = False
                entered.set()
                if not resume.wait(timeout=15):
                    raise TimeoutError("test did not release request snapshot")
            return snapshot

        resolver.begin_request = block_after_snapshot

        async def switch_during_mcp_request():
            async with Client(server) as client:
                pending = asyncio.create_task(client.call_tool(
                    "search_docs", {"query": osg_assets["queries"]["A"], "top_k": 1}
                ))
                assert await asyncio.to_thread(entered.wait, 10), "MCP request did not take snapshot"
                service.activate(
                    "osg", release_b["release_id"], expected_revision=1,
                    reason="switch while the A request is paused",
                )
                # candidate changed after sealing; the active request must use A's retained snapshot.
                source_b = osg_assets["paths"]["B"]["raw"]
                manifest_b = osg_assets["paths"]["B"]["manifest"]
                source_b.write_bytes(source_b.read_bytes() + b"changed candidate after prepare\n")
                manifest_b.write_bytes(manifest_b.read_bytes() + b" ")
                resume.set()
                old_result = _tool_data(await pending)
                resolver.begin_request = original_begin
                current_default = _tool_data(await client.call_tool(
                    "search_docs", {"query": osg_assets["queries"]["B"], "top_k": 1}
                ))
                current_library = _tool_data(await client.call_tool(
                    "search_docs", {
                        "query": osg_assets["queries"]["B"], "library": "osg", "top_k": 1,
                    }
                ))
                current_target = _tool_data(await client.call_tool(
                    "search_docs", {
                        "query": osg_assets["queries"]["B"],
                        "target_id": release_b["target"]["target_id"],
                        "top_k": 1,
                    }
                ))
                stale_target = _tool_data(await client.call_tool(
                    "search_docs", {
                        "query": osg_assets["queries"]["A"],
                        "target_id": osg_assets["targets"]["A"].target_id,
                        "top_k": 1,
                    }
                ))
                stale_release = _tool_data(await client.call_tool(
                    "search_docs", {
                        "query": osg_assets["queries"]["A"],
                        "target_id": release_a["target"]["target_id"],
                        "top_k": 1,
                    }
                ))
                return old_result, current_default, current_library, current_target, stale_target, stale_release

        old_result, current_default, current_library, current_target, stale_target, stale_release = (
            asyncio.run(switch_during_mcp_request())
        )
        assert old_result["routed_libraries"][0]["target_id"] == release_a["target"]["target_id"]
        assert osg_assets["expected_markers"]["A"] in old_result["evidence"][0]["text"]
        assert _release_id_from_evidence(old_result["evidence"][0]) == release_a["release_id"]

        for result in (current_default, current_library, current_target):
            assert result["routed_libraries"][0]["target_id"] == release_b["target"]["target_id"]
            assert osg_assets["expected_markers"]["B"] in result["evidence"][0]["text"]
            assert _release_id_from_evidence(result["evidence"][0]) == release_b["release_id"]
        for result in (stale_target, stale_release):
            assert result["error"] == "invalid_target"
            assert result["evidence"] == []

        download_path = current_default["evidence"][0]["download_path"]
        with TestClient(server.http_app(path="/mcp")) as http_client:
            downloaded = http_client.get(download_path)
        assert downloaded.status_code == 200, downloaded.text
        assert osg_assets["expected_markers"]["B"] in downloaded.text
        assert b"changed candidate after prepare" not in downloaded.content

        # Catalog已被本resolver观察后不可降级回legacy；rename只发生在本例临时root。
        catalog_path = release_catalog_path(root)
        unavailable_catalog = catalog_path.with_suffix(".temporarily-unavailable")
        catalog_path.rename(unavailable_catalog)
        try:
            async def call_without_catalog():
                async with Client(server) as client:
                    return _tool_data(await client.call_tool(
                        "search_docs", {
                            "query": osg_assets["queries"]["A"], "top_k": 1,
                        }
                    ))

            missing_catalog = asyncio.run(call_without_catalog())
            assert missing_catalog["error"] == "unavailable"
            assert missing_catalog["evidence"] == []
        finally:
            unavailable_catalog.rename(catalog_path)

        # 对同一release反复请求不会创建第二套客户端/向量器，搜索使用共享实例。
        assert resolver.runtime["qdrant_client"] is qdrant
        assert resolver.runtime["embedder"] is osg_assets["embedder"]
        assert legacy_router.qdrant_client is qdrant
        assert legacy_router.embedder is osg_assets["embedder"]
    finally:
        resolver.begin_request = original_begin if "original_begin" in locals() else resolver.begin_request
        audit.close()
        qdrant.close()


def test_prepared_only_catalog_keeps_legacy_but_lost_active_with_history_fails_closed(
    tmp_path,
):
    root = tmp_path / "history-root"
    qdrant = QdrantClient(":memory:")
    assets = build_two_version_release_fixture(
        root=root, library_id="history-contract-library", qdrant_client=qdrant
    )
    legacy_router = create_router(
        root=root,
        qdrant_client=qdrant,
        embedder=assets["embedder"],
        reranker=None,
    )
    resolver = create_release_resolver(
        root=root,
        qdrant_client=qdrant,
        embedder=assets["embedder"],
        reranker=None,
        legacy_router=legacy_router,
        search_lock=legacy_router.search_lock,
    )
    catalog = ReleaseCatalog(release_catalog_path(root))
    service = ReleaseService(root, catalog, qdrant)
    target_id = assets["targets"]["A"].target_id
    library_id = assets["library_id"]

    try:
        before_catalog = resolver.begin_request()
        assert RequestReleaseResolver.resolve(
            before_catalog, library=library_id
        ).target_id == target_id

        prepared = service.prepare(library_id, assets["targets"]["A"], reason="ready only")
        ready_snapshot = resolver.begin_request()
        assert ready_snapshot.catalog_snapshot["libraries"][library_id]["ever_activated"] is False
        assert any(index.target_id == target_id for index in ready_snapshot.router.indices)

        catalog.activate(
            library_id,
            prepared["release_id"],
            expected_revision=0,
            reason="synthetic activation before simulated pointer loss",
        )
        with closing(sqlite3.connect(catalog.path)) as database:
            database.execute(
                "UPDATE libraries SET active_release_id=NULL WHERE library_id=?",
                (library_id,),
            )
            database.commit()

        damaged_snapshot = resolver.begin_request()
        assert damaged_snapshot.catalog_snapshot["libraries"][library_id]["active"] is None
        assert damaged_snapshot.catalog_snapshot["libraries"][library_id]["ever_activated"] is True
        assert damaged_snapshot.router is None
        assert damaged_snapshot.default_target_id is None
        with pytest.raises(ValueError, match="没有可检索的active资料库"):
            RequestReleaseResolver.resolve(damaged_snapshot, library=library_id)
    finally:
        qdrant.close()


def test_active_same_dimension_model_mismatch_fails_request_materialization(tmp_path):
    root = tmp_path / "wrong-model-runtime-root"
    qdrant = QdrantClient(":memory:")
    assets = build_two_version_release_fixture(
        root=root, library_id="model-identity-library", qdrant_client=qdrant
    )
    model_a = assets["targets"]["A"].embedding_model
    target_b_wrong_model = replace(
        assets["targets"]["B"],
        embedding_model="synthetic-same-dimension-different-model-v9",
    )
    assert target_b_wrong_model.vector_dimension == assets["targets"]["A"].vector_dimension
    legacy_router = create_router(
        root=root,
        qdrant_client=qdrant,
        embedder=assets["embedder"],
        reranker=None,
    )
    catalog = ReleaseCatalog(release_catalog_path(root))
    service = ReleaseService(root, catalog, qdrant)
    resolver = None
    try:
        release_a = service.prepare(
            "model-identity-library", assets["targets"]["A"], reason="model A"
        )
        release_b = service.prepare(
            "model-identity-library", target_b_wrong_model, reason="same dimension, model B"
        )
        active_a = service.activate(
            "model-identity-library",
            release_a["release_id"],
            expected_revision=0,
            expected_embedding_model=model_a,
            reason="activate A for model A",
        )
        # 用不带在线identity参数的domain入口模拟catalog被错误切到B；serving端必须拒绝。
        service.activate(
            "model-identity-library",
            release_b["release_id"],
            expected_revision=active_a["revision"],
            reason="simulate stale/misconfigured publisher",
        )
        resolver = create_release_resolver(
            root=root,
            qdrant_client=qdrant,
            embedder=assets["embedder"],
            reranker=None,
            legacy_router=legacy_router,
            search_lock=legacy_router.search_lock,
            expected_embedding_model=model_a,
        )
        snapshot = resolver.begin_request()
        assert snapshot.catalog_snapshot["libraries"]["model-identity-library"][
            "active_release_id"
        ] == release_b["release_id"]
        selected = RequestReleaseResolver.resolve(
            snapshot, library="model-identity-library"
        )
        from wenjin.releases import ReleaseValidationError

        with pytest.raises(ReleaseValidationError, match="active release无法构造请求视图"):
            resolver.materialize_indices([selected])
        assert catalog.snapshot("model-identity-library")["active_release_id"] == (
            release_b["release_id"]
        )
    finally:
        qdrant.close()


def test_explicit_embedding_model_id_matches_management_and_mcp_runtime(tmp_path, monkeypatch):
    import wenjin.settings as settings_module
    from wenjin.management_http import app as management_app_module
    from wenjin.mcp import runtime as runtime_module

    root = tmp_path / "explicit-model-id-root"
    qdrant = QdrantClient(":memory:")
    assets = build_two_version_release_fixture(root=root, qdrant_client=qdrant)
    expected_model_id = assets["targets"]["A"].embedding_model
    settings = settings_module.Settings(
        _env_file=None,
        qdrant_api_key="synthetic-test-only",
        embedding_model_path=tmp_path / "same-dimension-different-folder-name",
        embedding_model_id=expected_model_id,
    )
    assert settings.embedding_model_path.name != expected_model_id
    assert settings.resolved_embedding_model_id == expected_model_id

    monkeypatch.setattr(
        settings_module.Settings,
        "embedding",
        lambda _self, *, root=None: assets["embedder"],
    )
    # create_runtime resolves settings through the project-root API; return this
    # synthetic object without reading ambient project configuration.
    monkeypatch.setattr(
        settings_module.Settings,
        "for_project_root",
        classmethod(lambda _cls, _root: settings),
    )
    monkeypatch.setattr(runtime_module, "prewarm_zh", lambda: None)
    management_qdrant = QdrantClient(":memory:")

    qdrant_factory_calls = 0

    def synthetic_qdrant_factory(**_kwargs):
        nonlocal qdrant_factory_calls
        qdrant_factory_calls += 1
        if qdrant_factory_calls == 1:
            return qdrant
        if qdrant_factory_calls == 2:
            return management_qdrant
        raise AssertionError("unexpected Qdrant client construction in model ID contract")

    monkeypatch.setattr(
        "wenjin.qdrant_connection.create_qdrant_client",
        synthetic_qdrant_factory,
    )

    runtime_service, runtime_identity = runtime_module.create_runtime(
        use_reranker=False, root=root
    )
    assert runtime_identity.embedding_model == expected_model_id
    assert runtime_service.embedder is assets["embedder"]

    app = management_app_module.create_app(root, settings=settings)
    try:
        with TestClient(app) as client:
            assert client.app.state.manager.embedding_model == expected_model_id
    finally:
        runtime_service.close()
        qdrant.close()
    assert qdrant_factory_calls == 2


def _release_id_from_evidence(evidence: dict) -> str:
    parsed = urlsplit(evidence["download_path"])
    values = parse_qs(parsed.query).get("release_id", [])
    assert len(values) == 1
    return values[0]
