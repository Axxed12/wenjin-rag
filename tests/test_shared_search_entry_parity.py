"""Shared management/MCP entry contracts on isolated synthetic search assets."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from tests.support.shared_search_fixture import build_shared_search_fixture
from wenjin.audit import SearchAudit
from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.index_registry import IndexRegistryStore
from wenjin.management_http.app import create_app
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.server import build_mcp_server
from wenjin.retrieval.lazy_reranker import build_shared_reranker
from wenjin.retrieval.search_policy import SearchPolicy
from wenjin.retrieval.service import RetrievalService, load_source_urls
from wenjin.settings import Settings

pytestmark = pytest.mark.integration

_BOOTSTRAP = "shared-search-test-bootstrap-token"
_ADMIN_PASSWORD = "Synthetic_Search_Admin_2026!"
_READER_PASSWORD = "Synthetic_Search_Reader_2026!"


class _Manager:
    def __init__(self, assets):
        self.qdrant_client = assets["qdrant"]
        self.embedder = assets["embedder"]
        self.embedding_model = assets["target"].embedding_model
        self._model_lock = threading.Lock()
        self.closed = False

    def resume_pending_jobs(self):
        return []

    def list_files(self):
        return []

    def close(self):
        self.closed = True


class _FakeReranker:
    instances = []
    observed_locks = []

    def __init__(self, _path, *, device, max_length, batch_size):
        assert (device, max_length, batch_size) == ("cpu", 768, 3)
        self.closed = 0
        self.document_counts = []
        type(self).instances.append(self)

    def score(self, _query, documents):
        assert any(lock.locked() for lock in type(self).observed_locks)
        self.document_counts.append(len(documents))
        # Deliberately reorder candidates by their source marker, identically at both entries.
        scores = [
            float(int(re.search(r"shared_marker_(\d+)", text).group(1)))
            for text in documents
        ]
        return scores

    def close(self):
        self.closed += 1


def _origin(csrf: str | None = None) -> dict[str, str]:
    headers = {"Origin": "http://testserver"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def _rpc_json(response) -> dict:
    assert response.status_code == 200, response.text
    if "application/json" in response.headers.get("content-type", ""):
        return response.json()
    events = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events, response.text
    return json.loads(events[-1])


def _tool_result(
    client, session_id: str, token: str, request_id: int, *, top_k: int,
    library: str | None = None, version: str | None = None,
    context_char_budget: int = 8000,
):
    arguments = {
        "query": "amber widget shared lookup",
        "top_k": top_k,
        "context_char_budget": context_char_budget,
    }
    if library is not None:
        arguments["library"] = library
    if version is not None:
        arguments["version"] = version
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "MCP-Session-Id": session_id,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "search_docs",
                "arguments": arguments,
            },
        },
    )
    envelope = _rpc_json(response)
    result = envelope["result"]
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                decoded = json.loads(block["text"])
            except (KeyError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                return decoded
    raise AssertionError(f"MCP search returned no structured result: {result}")


def _mcp_session(client, token: str) -> str:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    }
    initialized = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "synthetic-search-contract", "version": "1"},
            },
        },
    )
    _rpc_json(initialized)
    session_id = initialized.headers.get("MCP-Session-Id")
    assert session_id
    notification = client.post(
        "/mcp",
        headers={**headers, "MCP-Session-Id": session_id},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notification.status_code in {200, 202}
    return session_id


def _rerank_state() -> dict[int, int]:
    return {id(instance): len(instance.document_counts) for instance in _FakeReranker.instances}


def _assert_rerank_delta(before: dict[int, int], *, enabled: bool, top_k: int, pool: int):
    additions = []
    for instance in _FakeReranker.instances:
        start = before.get(id(instance), 0)
        additions.extend(instance.document_counts[start:])
    if enabled:
        assert len(additions) == 1
        # Fusion may return fewer candidates than either channel, but reranking cannot
        # exceed the configured pool and must have enough rows to satisfy top_k.
        assert top_k <= additions[0] <= pool
    else:
        assert additions == []


class _TestClientTransport(httpx.BaseTransport):
    """Forward a real HTTP request through an active Starlette TestClient lifespan."""

    def __init__(self, client: TestClient):
        self.client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query.decode("ascii")
        response = self.client.request(
            request.method,
            path,
            headers=dict(request.headers),
            content=request.read(),
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )


def _create_credentials(store: IdentityStore, admin_password: str):
    admin = store.login("search-admin", admin_password).principal
    issued = store.issue_mcp_credential(admin, label="synthetic-search")
    store.create_user(admin, "search-reader", "Synthetic_Temporary_Reader_2026!", role="reader")
    first = store.login("search-reader", "Synthetic_Temporary_Reader_2026!")
    store.change_password(
        first.token, "Synthetic_Temporary_Reader_2026!", _READER_PASSWORD
    )
    reader = store.login("search-reader", _READER_PASSWORD).principal
    reader_credential = store.issue_mcp_credential(reader, label="synthetic-denied-reader")
    return admin, issued, reader, reader_credential


@pytest.mark.parametrize("base_candidate_k", [10, 20])
@pytest.mark.parametrize("reranker_enabled", [True, False])
def test_management_and_stateful_mcp_share_live_candidate_budget_rerank_and_acl(
    tmp_path, monkeypatch, base_candidate_k, reranker_enabled,
):
    root = tmp_path / f"entry-parity-{base_candidate_k}-{reranker_enabled}"
    root.mkdir()
    assets = build_shared_search_fixture(root=root)
    qdrant = assets["qdrant"]
    # LegacySearchService reads the project's canonical registry path. Keep this fully
    # synthetic and explicit rather than allowing its fallback to the real project root.
    IndexRegistryStore(root / "data" / "indexes" / "index-registry.json").initialize(
        assets["target"], reason="synthetic entry parity target"
    )
    model_dir = root / "models" / "synthetic-reranker"
    model_dir.mkdir(parents=True)
    (model_dir / "marker.bin").write_bytes(b"synthetic-model-tree")
    settings = Settings(
        _env_file=None,
        qdrant_api_key="synthetic-only",
        embedding_model_path=Path("synthetic-embedding-path"),
        embedding_model_id=assets["target"].embedding_model,
        reranker_model_path=Path("models/synthetic-reranker"),
        reranker_model_id="explicit-reranker-contract-id",
        search_candidate_k=base_candidate_k,
        search_rerank_pool=12,
        search_reranker_enabled=reranker_enabled,
        reranker_max_length=768,
        reranker_batch_size=3,
    )
    monkeypatch.setattr("torch.cuda.is_available", lambda: False)
    monkeypatch.setattr("wenjin.retrieval.rerank.Qwen3Reranker", _FakeReranker)
    _FakeReranker.instances.clear()
    _FakeReranker.observed_locks.clear()
    manager = _Manager(assets)
    _FakeReranker.observed_locks.append(manager._model_lock)

    app = create_app(
        root,
        admin_token=_BOOTSTRAP,
        manager=manager,
        settings=settings,
        identity_db=root / "data" / "identity.sqlite",
    )
    try:
        with TestClient(app, client=("127.0.0.1", 52343)) as admin_client:
            bootstrap = admin_client.post(
                "/api/auth/bootstrap",
                headers={
                    **_origin(),
                    "Authorization": f"Bearer {_BOOTSTRAP}",
                },
                json={"username": "search-admin", "password": _ADMIN_PASSWORD},
            )
            assert bootstrap.status_code == 201, bootstrap.text
            login = admin_client.post(
                "/api/auth/login",
                headers=_origin(),
                json={"username": "search-admin", "password": _ADMIN_PASSWORD},
            )
            assert login.status_code == 200, login.text
            csrf = login.json()["csrf_token"]
            store = app.state.identity_store
            admin = store.resolve_session(admin_client.cookies["wenjin_session"])
            store.synchronize_resource_catalog({("library", assets["target"].library)})
            granted = admin_client.post(
                "/api/admin/grants",
                headers=_origin(csrf),
                json={
                    "resource_kind": "library",
                    "resource_id": assets["target"].library,
                    "user_id": admin.user_id,
                    "allowed": True,
                    "expected_revision": 0,
                },
            )
            assert granted.status_code == 200, granted.text

            catalog_response = admin_client.get("/api/admin/libraries")
            assert catalog_response.status_code == 200, catalog_response.text
            source = next(
                source
                for group in catalog_response.json()["libraries"]
                if group["library_id"] == assets["target"].library
                for source in group["sources"]
                if source["index_target_id"] == assets["target"].target_id
            )
            assert source["searchable"] is True
            assert source["version_id"]
            public_config = admin_client.get("/api/admin/config")
            assert public_config.status_code == 200, public_config.text
            assert public_config.json()["authentication_required"] is True
            assert public_config.json()["authentication_mode"] == "accounts"
            config = public_config.json()["retrieval"]
            assert config["candidate_k"] == base_candidate_k
            assert config["reranker_enabled"] is reranker_enabled
            assert config["reranker_model_id"] == "explicit-reranker-contract-id"
            assert "synthetic-reranker" not in public_config.text

            events = []
            from wenjin.indexing.bm25 import bm25_query as real_bm25
            from wenjin.indexing.dense import dense_query_vector as real_dense

            def dense_spy(client, vector, collection, *, top_k=5):
                events.append(("dense", top_k))
                return real_dense(client, vector, collection, top_k=top_k)

            def bm25_spy(path, query, *, top_k=5, source_path=None):
                events.append(("bm25", top_k))
                return real_bm25(path, query, top_k=top_k, source_path=source_path)

            monkeypatch.setattr("wenjin.indexing.dense.dense_query_vector", dense_spy)
            monkeypatch.setattr("wenjin.indexing.bm25.bm25_query", bm25_spy)

            def management_search(top_k):
                response = admin_client.post(
                    "/api/admin/search",
                    headers=_origin(csrf),
                    json={
                        "query": assets["query"],
                        "top_k": top_k,
                        "context_char_budget": 2000,
                        "library_id": assets["target"].library,
                        "source_type": "shared",
                        "version_id": source["version_id"],
                        "corpus_id": assets["target"].corpus_identity(),
                        "index_target_id": assets["target"].target_id,
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()

            top_k_values = [5, 15] if base_candidate_k == 10 else [5]
            for top_k in top_k_values:
                events.clear()
                before_rerank = _rerank_state()
                management = management_search(top_k)
                expected_candidate = max(base_candidate_k, top_k)
                _assert_rerank_delta(
                    before_rerank,
                    enabled=reranker_enabled,
                    top_k=top_k,
                    pool=max(12, top_k),
                )
                assert sorted(events) == [("bm25", expected_candidate), ("dense", expected_candidate)]
                assert management["retrieval"]["candidate_k"] == expected_candidate
                assert management["retrieval"]["rerank_pool"] == max(12, top_k)
                assert management["retrieval_config"]["candidate_k"] == expected_candidate
                assert management["retrieval_config"]["rerank_pool"] == max(12, top_k)
                assert management["retrieval_config"]["reranker_enabled"] is reranker_enabled
                assert len(management["evidence"]) == top_k
                if reranker_enabled:
                    assert any(instance.document_counts for instance in _FakeReranker.instances)
                    markers = [
                        int(re.search(r"shared_marker_(\d+)", item["text"]).group(1))
                        for item in management["evidence"]
                    ]
                    assert markers == sorted(markers, reverse=True)
                else:
                    assert _FakeReranker.instances == []

            # A stale version and a reader without a library grant must be rejected before
            # either retrieval channel is called.
            events.clear()
            stale = admin_client.post(
                "/api/admin/search",
                headers=_origin(csrf),
                json={
                    "query": assets["query"], "top_k": 5,
                    "library_id": assets["target"].library, "source_type": "shared",
                    "version_id": "0" * 64,
                    "corpus_id": assets["target"].corpus_identity(),
                    "index_target_id": assets["target"].target_id,
                },
            )
            assert stale.status_code == 409, stale.text
            assert events == []

            admin, mcp_credential, reader, reader_credential = _create_credentials(
                store, _ADMIN_PASSWORD
            )
            mcp_service = RetrievalService(
                qdrant_client=qdrant,
                embedder=assets["embedder"],
                bm25_path=assets["bm25_path"],
                source_urls=load_source_urls(assets["manifest_path"]),
                collection_name=assets["collection"],
                supported_version=assets["target"].version,
                index_target_id=assets["target"].target_id,
                reranker=build_shared_reranker(settings, root=root),
                dense_search=dense_spy,
                keyword_search=bm25_spy,
                search_lock=threading.Lock(),
            )
            _FakeReranker.observed_locks.append(mcp_service.search_lock)
            mcp_service.search_policy = SearchPolicy.from_settings(settings)
            identity = RuntimeIdentity(
                # Force the MCP request through the configured routed target instead of the
                # single-library compatibility shortcut bound to the service identity.
                corpus_id="synthetic-default-placeholder",
                corpus_id_source="registry",
                target_id="synthetic-default-placeholder-target",
                embedding_model=assets["target"].embedding_model,
            )
            from wenjin.retrieval.router import MultiCorpusRouter

            mcp_router = MultiCorpusRouter.from_targets(
                [assets["target"]],
                root=root,
                qdrant_client=qdrant,
                embedder=assets["embedder"],
                reranker=mcp_service.reranker,
                search_lock=mcp_service.search_lock,
            )
            mcp_router.dense_search = dense_spy
            mcp_router.keyword_search = bm25_spy
            audit = SearchAudit(root / "data" / "mcp-audit.sqlite")
            bridge_http = httpx.Client(transport=_TestClientTransport(admin_client))
            bridge = ManagedFileHTTPClient(
                "http://127.0.0.1:52343",
                "",
                account_mode=True,
                client=bridge_http,
            )
            mcp = build_mcp_server(
                service=mcp_service,
                audit=audit,
                identity=identity,
                project_root=root,
                identity_store=store,
                corpus_router=mcp_router,
                managed_bridge=bridge,
            )
            mcp_asgi = mcp.http_app(
                path="/mcp",
                middleware=[Middleware(McpIdentityMiddleware, identity_store=store)],
                stateless_http=False,
                json_response=True,
                transport="streamable-http",
            )
            try:
                with TestClient(mcp_asgi, client=("127.0.0.1", 52344)) as mcp_client:
                    session_id = _mcp_session(mcp_client, mcp_credential.token)
                    for top_k in top_k_values:
                        events.clear()
                        before_rerank = _rerank_state()
                        mcp_result = _tool_result(
                            mcp_client, session_id, mcp_credential.token, 10 + top_k,
                            top_k=top_k,
                            library=assets["target"].library,
                            version=assets["target"].version,
                            context_char_budget=2000,
                        )
                        mcp_events = list(events)
                        _assert_rerank_delta(
                            before_rerank,
                            enabled=reranker_enabled,
                            top_k=top_k,
                            pool=max(12, top_k),
                        )
                        events.clear()
                        before_rerank = _rerank_state()
                        management = management_search(top_k)
                        _assert_rerank_delta(
                            before_rerank,
                            enabled=reranker_enabled,
                            top_k=top_k,
                            pool=max(12, top_k),
                        )
                        expected_candidate = max(base_candidate_k, top_k)
                        assert "retrieval" in mcp_result, mcp_result
                        assert sorted(mcp_events) == [
                            ("bm25", expected_candidate), ("dense", expected_candidate)
                        ]
                        assert sorted(events) == [
                            ("bm25", expected_candidate), ("dense", expected_candidate)
                        ]
                        assert mcp_result["retrieval"]["candidate_k"] == expected_candidate
                        assert mcp_result["retrieval"]["rerank_pool"] == max(12, top_k)
                        assert mcp_result["retrieval_config"]["candidate_k"] == expected_candidate
                        assert mcp_result["retrieval_config"]["rerank_pool"] == max(12, top_k)
                        assert mcp_result["retrieval_config"]["reranker_enabled"] is reranker_enabled
                        assert mcp_result["retrieval_config"] == management["retrieval_config"]
                        stable_fields = (
                            "text", "source_path", "start_line", "end_line",
                            "page_number", "index_target_id",
                        )
                        assert [
                            tuple(item.get(key) for key in stable_fields)
                            for item in mcp_result["evidence"]
                        ] == [
                            tuple(item.get(key) for key in stable_fields)
                            for item in management["evidence"]
                        ]

                    # Keep the single-library compatibility path covered separately:
                    # no release router is supplied, so default OSG searches use the
                    # service-bound target rather than the routed library branch above.
                    compatibility_identity = RuntimeIdentity(
                        corpus_id=assets["target"].corpus_identity(),
                        corpus_id_source="registry",
                        target_id=assets["target"].target_id,
                        embedding_model=assets["target"].embedding_model,
                    )
                    compatibility_audit = SearchAudit(
                        root / "data" / "compatibility-mcp-audit.sqlite"
                    )
                    from wenjin.indexing.dense import dense_query
                    compatibility_service = RetrievalService(
                        qdrant_client=qdrant,
                        embedder=assets["embedder"],
                        bm25_path=assets["bm25_path"],
                        source_urls=load_source_urls(assets["manifest_path"]),
                        collection_name=assets["collection"],
                        supported_version=assets["target"].version,
                        index_target_id=assets["target"].target_id,
                        reranker=mcp_service.reranker,
                        dense_search=dense_query,
                        keyword_search=bm25_spy,
                        search_lock=mcp_service.search_lock,
                    )
                    compatibility_service.search_policy = SearchPolicy.from_settings(
                        settings
                    )
                    compatibility_server = build_mcp_server(
                        service=compatibility_service,
                        audit=compatibility_audit,
                        identity=compatibility_identity,
                        project_root=root,
                        identity_store=store,
                        managed_bridge=bridge,
                    )
                    compatibility_asgi = compatibility_server.http_app(
                        path="/mcp",
                        middleware=[
                            Middleware(McpIdentityMiddleware, identity_store=store)
                        ],
                        stateless_http=False,
                        json_response=True,
                        transport="streamable-http",
                    )
                    try:
                        with TestClient(
                            compatibility_asgi, client=("127.0.0.1", 52345)
                        ) as compatibility_client:
                            compatibility_session = _mcp_session(
                                compatibility_client, mcp_credential.token
                            )
                            events.clear()
                            bad_version = _tool_result(
                                compatibility_client,
                                compatibility_session,
                                mcp_credential.token,
                                149,
                                top_k=5,
                                version="synthetic-wrong-version",
                                context_char_budget=2000,
                            )
                            assert bad_version["error"] == "invalid_target"
                            assert bad_version["evidence"] == []
                            assert events == []
                            events.clear()
                            before_rerank = _rerank_state()
                            compatibility = _tool_result(
                                compatibility_client,
                                compatibility_session,
                                mcp_credential.token,
                                150,
                                top_k=5,
                                context_char_budget=2000,
                            )
                            compatibility_events = list(events)
                            _assert_rerank_delta(
                                before_rerank,
                                enabled=reranker_enabled,
                                top_k=5,
                                pool=12,
                            )
                            events.clear()
                            before_rerank = _rerank_state()
                            management = management_search(5)
                            _assert_rerank_delta(
                                before_rerank,
                                enabled=reranker_enabled,
                                top_k=5,
                                pool=12,
                            )
                            expected_candidate = max(base_candidate_k, 5)
                            assert sorted(compatibility_events) == [
                                ("bm25", expected_candidate),
                                ("dense", expected_candidate),
                            ]
                            assert compatibility["retrieval_config"] == management[
                                "retrieval_config"
                            ]
                            stable_fields = (
                                "text", "source_path", "start_line", "end_line",
                                "page_number", "index_target_id",
                            )
                            assert [
                                tuple(item.get(key) for key in stable_fields)
                                for item in compatibility["evidence"]
                            ] == [
                                tuple(item.get(key) for key in stable_fields)
                                for item in management["evidence"]
                            ]
                            if reranker_enabled:
                                compatibility_service.search_policy = SearchPolicy(
                                    candidate_k=base_candidate_k,
                                    reranker_enabled=False,
                                )
                                mismatch_audit = SearchAudit(
                                    root / "data" / "mismatch-mcp-audit.sqlite"
                                )
                                mismatch_server = build_mcp_server(
                                    service=compatibility_service,
                                    audit=mismatch_audit,
                                    identity=compatibility_identity,
                                    project_root=root,
                                    identity_store=store,
                                    managed_bridge=bridge,
                                )
                                mismatch_asgi = mismatch_server.http_app(
                                    path="/mcp",
                                    middleware=[
                                        Middleware(
                                            McpIdentityMiddleware,
                                            identity_store=store,
                                        )
                                    ],
                                    stateless_http=False,
                                    json_response=True,
                                    transport="streamable-http",
                                )
                                events.clear()
                                try:
                                    with TestClient(
                                        mismatch_asgi,
                                        client=("127.0.0.1", 52346),
                                    ) as mismatch_client:
                                        mismatch_session = _mcp_session(
                                            mismatch_client, mcp_credential.token
                                        )
                                        mismatched = mismatch_client.post(
                                            "/mcp",
                                            headers={
                                                "Authorization": f"Bearer {mcp_credential.token}",
                                                "MCP-Session-Id": mismatch_session,
                                                "Accept": "application/json, text/event-stream",
                                            },
                                            json={
                                                "jsonrpc": "2.0",
                                                "id": 151,
                                                "method": "tools/call",
                                                "params": {
                                                    "name": "search_docs",
                                                    "arguments": {
                                                        "query": assets["query"],
                                                        "top_k": 5,
                                                        "context_char_budget": 2000,
                                                    },
                                                },
                                            },
                                        )
                                        mismatch_envelope = _rpc_json(mismatched)
                                        assert mismatch_envelope["result"]["isError"] is True
                                        assert events == []
                                finally:
                                    mismatch_audit.close()
                    finally:
                        compatibility_audit.close()

                    events.clear()
                    reader_session = _mcp_session(mcp_client, reader_credential.token)
                    denied = _tool_result(
                        mcp_client, reader_session, reader_credential.token, 90,
                        top_k=5, library=assets["target"].library,
                        version=assets["target"].version,
                    )
                    assert denied["error"] == "forbidden"
                    assert denied["evidence"] == []
                    assert events == []
            finally:
                mcp_service.close()
                audit.close()
                bridge.close()
                bridge_http.close()
    finally:
        qdrant.close()

    # App owns and closes its single reranker; the manager and test harness only borrow Qdrant.
    assert manager.closed is True
    if reranker_enabled:
        assert _FakeReranker.instances
        assert all(instance.closed == 1 for instance in _FakeReranker.instances)
    else:
        assert _FakeReranker.instances == []


def test_management_service_rejects_policy_reranker_mismatch_before_search(tmp_path):
    from wenjin.retrieval.legacy_catalog import LegacySearchService

    with pytest.raises(ValueError, match="启用状态不一致"):
        LegacySearchService(
            tmp_path,
            qdrant_client=None,
            embedder=None,
            reranker=object(),
            search_policy=SearchPolicy(candidate_k=10, reranker_enabled=False),
        )
