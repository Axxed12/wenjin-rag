"""Synthetic managed rerank, model-error, and upload/query version contracts."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from qdrant_client import QdrantClient

from tests.test_file_management_service import MarkerEmbedder
from wenjin.file_management import FileManagementService
from wenjin.file_management.models import StaleSnapshotError
from wenjin.models.providers import ModelProviderError
from wenjin.retrieval.search_policy import SearchPolicy


class _SpyReranker:
    """Deterministic test double; scores corpus passages without loading a model."""

    def __init__(self, *, block_first=False, failure=None):
        self.documents = []
        self.calls = 0
        self.block_first = block_first
        self.failure = failure
        self.entered = threading.Event()
        self.release = threading.Event()

    def score(self, query, documents):
        self.calls += 1
        self.documents.append(tuple(documents))
        if self.failure is not None:
            raise self.failure
        if self.block_first and self.calls == 1:
            self.entered.set()
            assert self.release.wait(10), "test did not release the blocked reranker"
        return [100.0 if "preferredrank" in item else 0.0 for item in documents]


def _write_source(tmp_path: Path, name: str, content: str) -> Path:
    source = tmp_path / name
    source.write_text(content, encoding="utf-8")
    return source


def _manager(root: Path, client: QdrantClient, **kwargs) -> FileManagementService:
    return FileManagementService(
        root=root,
        qdrant_client=client,
        embedder=MarkerEmbedder(),
        embedding_model="synthetic-delivery-embedder-v1",
        **kwargs,
    )


@pytest.mark.integration
def test_managed_policy_reranks_candidates_to_final_and_disabled_skips_instance(tmp_path):
    client = QdrantClient(":memory:")
    manager = _manager(tmp_path, client)
    source = _write_source(
        tmp_path,
        "rerank-guide.txt",
        "sharedquery lowrank passage alpha " + "alpha " * 240 + "\n\n"
        + "sharedquery preferredrank passage beta " + "beta " * 240 + "\n\n"
        + "sharedquery middlerank passage gamma " + "gamma " * 240 + "\n",
    )
    job = manager.submit_import(source, display_name=source.name)
    assert manager.run_job(job.job_id).status == "completed"
    shared = _SpyReranker()
    policy = SearchPolicy(
        candidate_k=2,
        rerank_pool=8,
        context_char_budget=5000,
        reranker_enabled=True,
        reranker_model_id="synthetic-spy-reranker-v1",
    )
    try:
        result = manager.search(
            job.file_id,
            "sharedquery",
            reranker=shared,
            search_policy=policy,
            top_k=1,
            context_char_budget=5000,
        )
        assert shared.calls == 1
        assert len(shared.documents[0]) >= 2
        assert result["retrieval"]["reranker_used"] is True
        assert result["retrieval"]["fused_candidates"] == len(shared.documents[0])
        assert result["retrieval_config"] == policy.public_config(
            1, context_char_budget=5000
        )
        assert result["retrieval_config"]["candidate_k"] == 2
        assert result["retrieval_config"]["rerank_pool"] == 8
        assert len(result["evidence"]) == 1
        assert "preferredrank" in result["evidence"][0]["text"]
        snapshot = manager.snapshot(job.file_id)
        assert result["managed_file"]["version_id"] == snapshot.version_id

        disabled = SearchPolicy(
            candidate_k=2,
            rerank_pool=8,
            reranker_enabled=False,
            reranker_model_id="synthetic-spy-reranker-v1",
        )
        no_reranker = manager.search(
            job.file_id,
            "sharedquery",
            reranker=None,
            search_policy=disabled,
            top_k=1,
        )
        assert shared.calls == 1, "disabled policy must not invoke a leftover shared instance"
        assert no_reranker["retrieval"]["reranker_used"] is False
        assert no_reranker["retrieval_config"]["reranker_enabled"] is False
        with pytest.raises(RuntimeError, match="状态不一致"):
            manager.search(
                job.file_id,
                "sharedquery",
                reranker=shared,
                search_policy=disabled,
                top_k=1,
            )
    finally:
        manager.close()
        client.close()


@pytest.mark.integration
def test_managed_provider_failure_preserves_stable_error_code(tmp_path):
    client = QdrantClient(":memory:")
    manager = _manager(tmp_path, client)
    source = _write_source(tmp_path, "provider-failure.txt", "synthetic providererror marker")
    job = manager.submit_import(source, display_name=source.name)
    assert manager.run_job(job.job_id).status == "completed"
    code = "model_provider_read_timeout"
    reranker = _SpyReranker(failure=ModelProviderError(code))
    policy = SearchPolicy(reranker_enabled=True, reranker_model_id="synthetic-failing-model")
    try:
        with pytest.raises(ModelProviderError) as caught:
            manager.search(
                job.file_id,
                "providererror",
                reranker=reranker,
                search_policy=policy,
            )
        assert caught.value.code == code
        assert str(caught.value) == code
    finally:
        manager.close()
        client.close()


@pytest.mark.integration
def test_reranker_provider_failure_crosses_management_http_bridge_and_both_mcp_eras(tmp_path):
    """The real HTTP bridge must preserve a reranker error without empty-success evidence."""
    from starlette.middleware import Middleware

    from tests.test_mcp_private_route_delivery_contract import (
        _API_KEY,
        _modern_call,
        _modern_node_client_dir,
        _protocol_major,
        _start_server,
        _stop_server,
    )
    from wenjin.access import AccessPolicy, write_policy
    from wenjin.audit import SearchAudit
    from wenjin.auth import BearerAuthMiddleware
    from wenjin.managed_mcp import managed_file_corpus_id
    from wenjin.management_http.app import create_app as create_management_app
    from wenjin.mcp.managed_client import ManagedFileHTTPClient
    from wenjin.mcp.runtime import RuntimeIdentity

    root = tmp_path / "synthetic-managed-rerank-wire"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = _manager(root, qdrant)
    management_server = mcp_server = None
    management_thread = mcp_thread = None
    bridge = None
    try:
        source = _write_source(
            root,
            "synthetic-managed-wire.txt",
            "wirequery marker; this evidence must never be returned after provider failure\n",
        )
        job = manager.submit_import(source, display_name=source.name)
        assert manager.run_job(job.job_id).status == "completed"
        file_id = job.file_id
        access_dir = root / "configs" / "access"
        write_policy(
            access_dir,
            AccessPolicy(
                managed_file_corpus_id(file_id),
                "internal",
                ("synthetic-rerank-wire-client",),
            ),
        )

        management_token = "SyntheticManagementBearer_20261017_Rerank"
        management_app = create_management_app(
            root,
            manager=manager,
            admin_token=management_token,
        )
        management_server, management_thread, management_url = _start_server(management_app)
        # Inject only deterministic test dependencies after the test app lifecycle has
        # initialized; this exercises the production _search route and its shared-policy
        # plumbing without constructing or loading a real reranker.
        upstream_marker = "SYNTHETIC_SENSITIVE_UPSTREAM_BODY_SHOULD_NOT_ESCAPE"

        class FailingReranker:
            def __init__(self):
                self.calls = 0

            def score(self, query, documents):
                self.calls += 1
                raise ModelProviderError("model_queue_full") from RuntimeError(upstream_marker)

        reranker = FailingReranker()
        management_app.state.shared_reranker = reranker
        management_app.state.search_policy = SearchPolicy(
            candidate_k=3,
            rerank_pool=8,
            reranker_enabled=True,
            reranker_model_id="synthetic-rerank-wire-v1",
        )

        response = httpx.post(
            f"{management_url}/api/search",
            headers={"Authorization": f"Bearer {management_token}"},
            json={"file_id": file_id, "query": "wirequery"},
            timeout=10,
        )
        assert response.status_code == 503
        http_payload = response.json()
        assert http_payload["error"] == "model_queue_full"
        assert http_payload["evidence_status"] == "not_retrieved"
        assert "evidence" not in http_payload
        assert upstream_marker not in response.text
        assert "wirequery marker" not in response.text

        bridge = ManagedFileHTTPClient(management_url, management_token)
        identity = RuntimeIdentity(
            corpus_id="synthetic-managed-wire",
            corpus_id_source="synthetic-managed-wire",
            target_id="synthetic-managed-wire-target",
            embedding_model="synthetic-delivery-embedder-v1",
        )
        service = SimpleNamespace(
            qdrant_client=qdrant,
            reranker=None,
            search_lock=threading.RLock(),
            search_policy=SearchPolicy(reranker_enabled=False),
            search=lambda *args, **kwargs: {},
        )
        if _protocol_major() == 1:
            from wenjin.mcp.server import build_mcp_server

            mcp_server_object = build_mcp_server(
                service=service,
                audit=SearchAudit(root / "artifacts" / "managed-rerank-audit.jsonl"),
                access_dir=access_dir,
                bearer_enabled=True,
                identity=identity,
                managed_bridge=bridge,
                raw_root=root / "raw_data",
                project_root=root,
            )
            mcp_app = mcp_server_object.http_app(
                path="/mcp",
                middleware=[
                    Middleware(
                        BearerAuthMiddleware,
                        api_key=_API_KEY,
                        client_id="synthetic-rerank-wire-client",
                    )
                ],
                stateless_http=False,
                json_response=True,
            )
            mcp_path = "/mcp"
        elif _protocol_major() == 2:
            from wenjin.mcp.stateless_server import (
                build_stateless_mcp_server,
                create_stateless_app,
            )

            mcp_server_object = build_stateless_mcp_server(
                service=service,
                audit=SearchAudit(root / "artifacts" / "managed-rerank-audit.jsonl"),
                access_dir=access_dir,
                bearer_enabled=True,
                identity=identity,
                managed_bridge=bridge,
                raw_root=root / "raw_data",
                project_root=root,
            )
            mcp_app = create_stateless_app(
                server=mcp_server_object,
                api_key=_API_KEY,
                client_id="synthetic-rerank-wire-client",
                allowed_hosts=["127.0.0.1", "127.0.0.1:*"],
                project_root=root,
            )
            mcp_path = "/mcp-stateless"
            _modern_node_client_dir()
        else:
            pytest.skip(f"unsupported MCP SDK major version: {_protocol_major()}")

        mcp_server, mcp_thread, mcp_url = _start_server(mcp_app)
        mcp_endpoint = f"{mcp_url}{mcp_path}"
        arguments = {"file_id": file_id, "query": "wirequery"}
        if _protocol_major() == 1:
            tool_payload, is_error = asyncio.run(
                _legacy_call_with_error(mcp_endpoint, arguments)
            )
            assert is_error is True
        else:
            tool_payload = _modern_call(
                url=mcp_endpoint,
                tool="search_docs",
                arguments=arguments,
                expect_error=True,
            )
        assert tool_payload["error"] == "model_queue_full"
        assert tool_payload["evidence_status"] == "not_retrieved"
        assert tool_payload["evidence"] == []
        assert tool_payload["answer_status"] == "not_generated"
        assert upstream_marker not in str(tool_payload)
        assert "wirequery marker" not in str(tool_payload)
        assert reranker.calls >= 2, "HTTP and MCP must both cross the managed reranker path"
    finally:
        _stop_server(mcp_server, mcp_thread)
        _stop_server(management_server, management_thread)
        if bridge is not None:
            bridge.close()
        manager.close()
        qdrant.close()


async def _legacy_call_with_error(url: str, arguments: dict) -> tuple[dict, bool]:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from tests.test_mcp_private_route_delivery_contract import _API_KEY

    transport = StreamableHttpTransport(
        url,
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )
    async with Client(transport, timeout=15) as client:
        result = await client.call_tool("search_docs", arguments, raise_on_error=False)
        assert result.structured_content is not None
        return result.structured_content, bool(result.is_error)


@pytest.mark.integration
def test_upload_replace_overlaps_query_without_mixing_version_evidence_or_source_hash(tmp_path):
    activated = threading.Event()

    def hook(phase, context):
        if phase == "activating":
            activated.set()

    client = QdrantClient(":memory:")
    manager = _manager(tmp_path, client, fault_hook=hook)
    old_source = _write_source(
        tmp_path, "version-a.txt", "sharedquery amberversion onlyoldsource marker\n"
    )
    old_job = manager.submit_import(old_source, display_name="version-guide.txt")
    assert manager.run_job(old_job.job_id).status == "completed"
    old_snapshot = manager.snapshot(old_job.file_id)

    reranker = _SpyReranker(block_first=True)
    policy = SearchPolicy(reranker_enabled=True, reranker_model_id="synthetic-blocking-reranker")
    query_result: dict = {}
    query_failure: list[BaseException] = []

    def query_old_snapshot():
        try:
            query_result.update(manager.search(
                old_job.file_id,
                "sharedquery",
                reranker=reranker,
                search_policy=policy,
                version_id=old_snapshot.version_id,
            ))
        except BaseException as error:  # surfaced in the controlling test thread
            query_failure.append(error)

    query_thread = threading.Thread(target=query_old_snapshot, daemon=True)
    replace_thread = None
    try:
        query_thread.start()
        assert reranker.entered.wait(10), "query never reached the shared reranker"

        new_source = _write_source(
            tmp_path, "version-b.txt", "sharedquery cobaltversion onlynewsource marker\n"
        )
        replacement = manager.submit_import(
            new_source,
            display_name="version-guide.txt",
            replace_file_id=old_job.file_id,
        )
        replacement_result: list = []
        replace_thread = threading.Thread(
            target=lambda: replacement_result.append(manager.run_job(replacement.job_id)),
            daemon=True,
        )
        replace_thread.start()
        assert activated.wait(10), "replacement did not reach its activation boundary"
        # The active pointer cannot switch while the old query holds its snapshot read lock.
        with sqlite3.connect(manager.db_path) as database:
            active_id = database.execute(
                "SELECT active_version_id FROM files WHERE file_id=?", (old_job.file_id,)
            ).fetchone()[0]
        assert active_id == old_snapshot.version_id
        assert replace_thread.is_alive()
        reranker.release.set()
        query_thread.join(15)
        replace_thread.join(20)
        assert not query_thread.is_alive() and not replace_thread.is_alive()
        assert not query_failure, repr(query_failure)
        assert replacement_result and replacement_result[0].status == "completed"

        old_hash = hashlib.sha256(old_source.read_bytes()).hexdigest()
        assert query_result["managed_file"]["version_id"] == old_snapshot.version_id
        assert query_result["managed_file"]["source_sha256"] == old_hash
        assert query_result["evidence"]
        assert all(item["version_id"] == old_snapshot.version_id for item in query_result["evidence"])
        assert all(item["source_sha256"] == old_hash for item in query_result["evidence"])
        assert any("onlyoldsource" in item["text"] for item in query_result["evidence"])
        assert not any("onlynewsource" in item["text"] for item in query_result["evidence"])
        assert manager.get_original(
            old_job.file_id,
            expected_version_id=replacement.version_id,
        ) == new_source.read_bytes()
        with pytest.raises(StaleSnapshotError):
            manager.get_original(
                old_job.file_id,
                expected_version_id=old_snapshot.version_id,
            )

        new_snapshot = manager.snapshot(old_job.file_id)
        latest = manager.search(
            old_job.file_id,
            "sharedquery",
            reranker=_SpyReranker(),
            search_policy=policy,
            version_id=new_snapshot.version_id,
        )
        new_hash = hashlib.sha256(new_source.read_bytes()).hexdigest()
        assert latest["managed_file"]["version_id"] == replacement.version_id
        assert latest["managed_file"]["source_sha256"] == new_hash
        assert latest["evidence"]
        assert all(item["version_id"] == replacement.version_id for item in latest["evidence"])
        assert all(item["source_sha256"] == new_hash for item in latest["evidence"])
        assert any("onlynewsource" in item["text"] for item in latest["evidence"])
        assert not any("onlyoldsource" in item["text"] for item in latest["evidence"])
    finally:
        reranker.release.set()
        query_thread.join(10)
        if replace_thread is not None:
            replace_thread.join(10)
        manager.close()
        client.close()


@pytest.mark.integration
def test_http_cancel_during_first_embedding_batch_waits_then_cleans_candidate_and_keeps_old_active(
    tmp_path,
):
    """Cancellation is nonterminal during inference; later batches reveal its actual boundary."""
    from tests.test_mcp_private_route_delivery_contract import _start_server, _stop_server
    from wenjin.management_http.app import create_app as create_management_app

    first_batch_entered = threading.Event()
    release_first_batch = threading.Event()
    block_candidate = threading.Event()
    embed_call_sizes: list[int] = []
    delegate = MarkerEmbedder()

    class FirstBatchBarrierEmbedder:
        dimension = delegate.dimension

        def get_text_embedding_batch(self, texts, show_progress=False):
            embed_call_sizes.append(len(texts))
            if block_candidate.is_set() and not first_batch_entered.is_set():
                first_batch_entered.set()
                assert release_first_batch.wait(60), "cancel test did not release the first batch"
            return delegate.get_text_embedding_batch(texts, show_progress=show_progress)

        def get_query_embedding(self, query):
            return delegate.get_query_embedding(query)

    client = QdrantClient(":memory:")
    manager = FileManagementService(
        root=tmp_path,
        qdrant_client=client,
        embedder=FirstBatchBarrierEmbedder(),
        embedding_model="synthetic-cancel-embedder-v1",
    )
    token = "SyntheticManagementBearer_Cancel_20261017"
    app = create_management_app(tmp_path, manager=manager, admin_token=token)
    server = server_thread = None
    management_url = None
    server, server_thread, management_url = _start_server(app)
    old_source = _write_source(
        tmp_path,
        "active-version.txt",
        "oldactive marker remains searchable after cancellation",
    )
    old_job = manager.submit_import(old_source, display_name="cancel-guide.txt")
    old_result = manager.run_job(old_job.job_id)
    assert old_result.status == "completed", old_result.reason
    old_snapshot = manager.snapshot(old_job.file_id)
    embed_call_sizes.clear()

    # 25 independent long paragraphs force more than one 16-chunk embedding batch.
    replacement_source = _write_source(
        tmp_path,
        "cancel-candidate.txt",
        "\n\n".join(
            f"candidatebatch{index} " + (f"payload{index} " * 140)
            for index in range(25)
        ),
    )
    replacement = manager.submit_import(
        replacement_source,
        display_name="cancel-guide.txt",
        replace_file_id=old_job.file_id,
    )
    block_candidate.set()
    result: list = []
    worker_errors: list[BaseException] = []

    def run_candidate():
        try:
            result.append(manager.run_job(replacement.job_id))
        except BaseException as error:
            worker_errors.append(error)

    worker = threading.Thread(target=run_candidate, daemon=True)
    try:
        worker.start()
        assert first_batch_entered.wait(15), "replacement did not enter embedding batch one"
        blocked_job = manager.get_job(replacement.job_id)
        assert blocked_job.status == "running"
        assert blocked_job.phase == "building"
        assert not blocked_job.cancel_requested
        candidate_resources = blocked_job.resources
        collection = candidate_resources.get("collection")
        bm25 = candidate_resources.get("bm25")
        version_dir = candidate_resources.get("version_dir")
        assert collection and bm25 and version_dir

        response = httpx.post(
            f"{management_url}/api/jobs/{replacement.job_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        assert response.status_code == 202
        cancel_payload = response.json()
        assert cancel_payload["status"] == "running"
        assert cancel_payload["cancel_requested"] is True
        assert cancel_payload["phase"] == "building"

        # The HTTP cancellation request must not report terminal or free the active work.
        still_running = manager.get_job(replacement.job_id)
        assert still_running.status == "running"
        assert still_running.cancel_requested is True
        assert worker.is_alive()
        assert len(embed_call_sizes) == 1

        release_first_batch.set()
        worker.join(30)
        assert not worker.is_alive(), "candidate job did not finish after barrier release"
        assert not worker_errors, repr(worker_errors)
        assert result and result[0].status == "cancelled", result[0] if result else None
        assert result[0].cancel_requested is True
        assert len(embed_call_sizes) >= 2, (
            "fixture must demonstrate whether cancellation prevents later embedding batches"
        )

        # Current implementation checks cancellation after candidate build; record whether
        # all subsequent model batches ran instead of claiming per-batch cancellation.
        final_job = manager.get_job(replacement.job_id)
        assert final_job.status == "cancelled"
        assert final_job.phase == "cancelled"
        assert not client.collection_exists(collection)
        assert not (tmp_path / bm25).exists()
        assert not (tmp_path / version_dir).exists()

        current = manager.snapshot(old_job.file_id)
        assert current.version_id == old_snapshot.version_id
        old_hash = hashlib.sha256(old_source.read_bytes()).hexdigest()
        after_cancel = manager.search(
            old_job.file_id,
            "oldactive",
            version_id=old_snapshot.version_id,
        )
        assert after_cancel["managed_file"]["version_id"] == old_snapshot.version_id
        assert after_cancel["managed_file"]["source_sha256"] == old_hash
        assert after_cancel["evidence"]
        assert all(item["version_id"] == old_snapshot.version_id for item in after_cancel["evidence"])
        assert all(item["source_sha256"] == old_hash for item in after_cancel["evidence"])
        assert any("oldactive" in item["text"] for item in after_cancel["evidence"])
    finally:
        release_first_batch.set()
        worker.join(10)
        _stop_server(server, server_thread)
        manager.close()
        client.close()


@pytest.mark.integration
def test_custom_index_adapter_without_batch_lock_capability_keeps_whole_build_lock(tmp_path):
    """Legacy/custom adapters remain serialized unless they opt into per-batch locking."""
    from wenjin.file_management.backends import QdrantBm25Adapter

    client = QdrantClient(":memory:")
    embedder = MarkerEmbedder()
    inner = QdrantBm25Adapter(
        root=tmp_path,
        qdrant_client=client,
        embedder=embedder,
        embedding_model="synthetic-custom-adapter-embedder-v1",
    )
    manager_ref: dict = {}
    lock_observations: list[bool] = []

    class CustomAdapterWithoutBatchLock:
        # Deliberately do not declare supports_embedding_batch_lock.
        def build(self, **kwargs):
            attempted = threading.Event()
            acquired: list[bool] = []

            def probe_model_lock():
                attempted.set()
                got_lock = manager_ref["manager"]._model_lock.acquire(timeout=1)
                acquired.append(got_lock)
                if got_lock:
                    manager_ref["manager"]._model_lock.release()

            probe = threading.Thread(target=probe_model_lock, daemon=True)
            probe.start()
            assert attempted.wait(2)
            probe.join(2)
            assert not probe.is_alive()
            lock_observations.append(acquired[0])
            return inner.build(**kwargs)

        def verify(self, target):
            return inner.verify(target)

        def delete(self, resource_id):
            return inner.delete(resource_id)

        def exists(self, resource_id):
            return inner.exists(resource_id)

        def count(self, resource_id):
            return inner.count(resource_id)

    manager = FileManagementService(
        root=tmp_path,
        qdrant_client=client,
        embedder=embedder,
        embedding_model="synthetic-custom-adapter-embedder-v1",
        index_adapter=CustomAdapterWithoutBatchLock(),
    )
    manager_ref["manager"] = manager
    inner.embedding_lock = manager._model_lock
    source = _write_source(tmp_path, "custom-adapter.txt", "custom adapter fallback marker")
    job = manager.submit_import(source, display_name=source.name)
    try:
        completed = manager.run_job(job.job_id)
        assert completed.status == "completed", completed.reason
        assert lock_observations == [False], (
            "another thread must not acquire the shared model lock while a legacy adapter builds"
        )
        snapshot = manager.snapshot(job.file_id)
        result = manager.search(job.file_id, "fallback", version_id=snapshot.version_id)
        assert result["evidence"]
        assert result["managed_file"]["version_id"] == snapshot.version_id
    finally:
        manager.close()
        client.close()
