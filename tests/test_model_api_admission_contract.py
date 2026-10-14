from __future__ import annotations

import asyncio
import json
import threading
import time

import httpx
import pytest

from wenjin.model_api.admission import ModelAdmission, ModelAdmissionError

pytestmark = pytest.mark.quick


def _code(exc: ModelAdmissionError) -> str:
    return exc.code


@pytest.mark.parametrize(
    ("provider", "model_read", "bridge_read", "expected_error"),
    [
        ("remote", "90", "210", None),
        ("remote", "30", "20", "budget"),
        ("remote", "NaN", "210", "invalid"),
        ("remote", "inf", "210", "invalid"),
        ("remote", "0", "210", "invalid"),
        ("remote", "301", "210", "invalid"),
        ("remote", "not-a-number", "210", "invalid"),
        ("local", "90", "20", None),
    ],
)
def test_managed_bridge_read_budget_is_validated_against_remote_model_read(
    monkeypatch, provider, model_read, bridge_read, expected_error
):
    from wenjin.mcp.managed_client import (
        ManagedFileServiceError,
        _bridge_timeout_from_environment,
    )

    monkeypatch.setenv("WENJIN_MODEL_PROVIDER", provider)
    monkeypatch.setenv("WENJIN_MODEL_API_TIMEOUT_SECONDS", model_read)
    monkeypatch.setenv("WENJIN_FILE_MANAGEMENT_READ_TIMEOUT_SECONDS", bridge_read)
    monkeypatch.delenv("WENJIN_FILE_MANAGEMENT_CONNECT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("WENJIN_FILE_MANAGEMENT_POOL_TIMEOUT_SECONDS", raising=False)

    if expected_error:
        with pytest.raises(ManagedFileServiceError) as caught:
            _bridge_timeout_from_environment()
        assert model_read not in str(caught.value)
        assert bridge_read not in str(caught.value)
        if expected_error == "budget":
            assert "读取预算必须大于" in str(caught.value)
        else:
            assert "模型服务读取超时配置无效" in str(caught.value)
    else:
        assert _bridge_timeout_from_environment().read == float(bridge_read)


def test_waiting_capacity_is_exact_and_fifo() -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=2, max_wait_seconds=2)
        release = threading.Event()
        first_started = threading.Event()
        order: list[str] = []
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def operation(name: str) -> str:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            if name == "first":
                first_started.set()
                assert release.wait(3), "test did not release first inference"
            order.append(name)
            with lock:
                active -= 1
            return name

        first = asyncio.create_task(admission.submit(operation, "first"))
        assert await asyncio.to_thread(first_started.wait, 2)
        second = asyncio.create_task(admission.submit(operation, "second"))
        third = asyncio.create_task(admission.submit(operation, "third"))
        await asyncio.sleep(0)  # run both submitters until they join the FIFO
        with pytest.raises(ModelAdmissionError) as caught:
            await admission.submit(operation, "overflow")
        assert _code(caught.value) == "model_queue_full"

        release.set()
        assert await asyncio.gather(first, second, third) == ["first", "second", "third"]
        assert order == ["first", "second", "third"]
        assert maximum_active == 1
        await admission.close()

    asyncio.run(scenario())


def test_zero_waiting_still_runs_one_active_job_and_rejects_the_next() -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=0, max_wait_seconds=1)
        release = threading.Event()
        entered = threading.Event()

        def operation() -> str:
            entered.set()
            assert release.wait(2), "test did not release active inference"
            return "done"

        first = asyncio.create_task(admission.submit(operation))
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(ModelAdmissionError) as caught:
            await admission.submit(lambda: "must-not-run")
        assert _code(caught.value) == "model_queue_full"
        release.set()
        assert await first == "done"
        await admission.close()

    asyncio.run(scenario())


def test_queued_cancel_and_active_caller_cancel_keep_worker_slot_until_thread_ends() -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=2, max_wait_seconds=2)
        release_first = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        later_started = threading.Event()
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def first_operation() -> str:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            first_started.set()
            assert release_first.wait(3), "test did not release active inference"
            with lock:
                active -= 1
            return "first"

        def second_operation() -> str:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            second_started.set()
            with lock:
                active -= 1
            return "second"

        def later_operation() -> str:
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            later_started.set()
            with lock:
                active -= 1
            return "later"

        first = asyncio.create_task(admission.submit(first_operation))
        assert await asyncio.to_thread(first_started.wait, 2)
        queued_cancel = asyncio.create_task(admission.submit(lambda: "cancelled"))
        await asyncio.sleep(0)
        queued_cancel.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued_cancel

        first.cancel()  # caller cancellation must not cancel the real worker thread
        with pytest.raises(asyncio.CancelledError):
            await first
        later = asyncio.create_task(admission.submit(later_operation))
        await asyncio.sleep(0)
        assert not later_started.is_set()

        release_first.set()
        assert await later == "later"
        assert not second_started.is_set()
        assert maximum_active == 1
        await admission.close()

    asyncio.run(scenario())


def test_shutdown_rejects_waiters_immediately_and_waits_for_active_thread() -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=2, max_wait_seconds=60)
        release = threading.Event()
        active_started = threading.Event()

        def blocking() -> str:
            active_started.set()
            assert release.wait(3), "test did not release active inference"
            return "done"

        active = asyncio.create_task(admission.submit(blocking))
        assert await asyncio.to_thread(active_started.wait, 2)
        queued = asyncio.create_task(admission.submit(lambda: "must-not-run"))
        await asyncio.sleep(0)
        close_task = asyncio.create_task(admission.close())
        with pytest.raises(ModelAdmissionError) as caught:
            await queued
        assert _code(caught.value) == "model_service_shutting_down"
        assert not close_task.done()
        assert not active.done()

        release.set()
        assert await active == "done"
        await close_task

    asyncio.run(scenario())


def test_expired_waiter_is_not_started_when_event_loop_resumes_late() -> None:
    async def scenario() -> None:
        wait_budget = 0.05
        admission = ModelAdmission(max_waiting=1, max_wait_seconds=wait_budget)
        release_first = threading.Event()
        first_started = threading.Event()
        loop_blocked = threading.Event()
        release_loop = threading.Event()
        expired_operation_called = threading.Event()
        deadline = time.monotonic() + wait_budget

        def blocking() -> str:
            first_started.set()
            assert release_first.wait(3), "test did not release active inference"
            return "first"

        def expired_operation() -> str:
            expired_operation_called.set()
            return "expired"

        first = asyncio.create_task(admission.submit(blocking))
        assert await asyncio.to_thread(first_started.wait, 2)
        expired = asyncio.create_task(admission.submit(expired_operation))
        await asyncio.sleep(0)

        def block_event_loop_until_after_deadline() -> None:
            loop_blocked.set()
            assert release_loop.wait(2), "test did not release event loop"

        asyncio.get_running_loop().call_soon(block_event_loop_until_after_deadline)
        assert await asyncio.to_thread(loop_blocked.wait, 2)

        def release_after_deadline() -> None:
            while time.monotonic() < deadline:
                pass
            release_first.set()
            release_loop.set()

        helper = threading.Thread(target=release_after_deadline, daemon=True)
        helper.start()
        with pytest.raises(ModelAdmissionError) as caught:
            await expired
        helper.join(timeout=2)
        assert _code(caught.value) == "model_queue_timeout"
        assert not expired_operation_called.is_set()
        assert await first == "first"
        await admission.close()

    asyncio.run(scenario())





def test_cancel_before_idle_reservation_is_dispatched_does_not_run_operation() -> None:
    async def scenario() -> None:
        class ParkedWorkerAdmission(ModelAdmission):
            def __init__(self) -> None:
                self.allow_worker = asyncio.Event()
                super().__init__(max_waiting=1, max_wait_seconds=1)

            async def _run(self) -> None:
                await self.allow_worker.wait()
                await super()._run()

        admission = ParkedWorkerAdmission()
        cancelled_operation_called = threading.Event()

        def must_not_run() -> str:
            cancelled_operation_called.set()
            return "cancelled-but-ran"

        submission = asyncio.create_task(admission.submit(must_not_run))
        while admission._active is None:
            await asyncio.sleep(0)
        assert not admission._active.started.is_set()
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission

        admission.allow_worker.set()
        assert await admission.submit(lambda: "next") == "next"
        assert not cancelled_operation_called.is_set()
        await admission.close()

    asyncio.run(scenario())

# HTTP contract through a local TCP listener with deterministic synchronous fake inference.
class _BlockingEmbedder:
    def __init__(self, block: bool):
        self.block = block
        self.entered = threading.Event()
        self.second_entered = threading.Event()
        self.release = threading.Event()
        self._calls = 0
        self.query_calls = 0
        self.document_calls = 0
        self._lock = threading.Lock()

    def _begin_call(self, kind: str) -> None:
        with self._lock:
            self._calls += 1
            call_number = self._calls
            if kind == "query":
                self.query_calls += 1
            else:
                self.document_calls += 1
        if self.block and call_number == 1:
            self.entered.set()
            assert self.release.wait(3), "test did not release fake inference"
        elif call_number > 1:
            self.second_entered.set()

    def get_query_embedding(self, _text: str) -> list[float]:
        self._begin_call("query")
        return [1.0, 0.0]

    def get_text_embedding_batch(self, texts, *, show_progress=False):
        del show_progress
        self._begin_call("document")
        return [[1.0, 0.0] for _ in texts]


class _FakeReranker:
    max_length = 384
    batch_size = 3
    device = "cpu"

    def __init__(self):
        self.calls = 0

    def score(self, _query: str, documents: list[str]) -> list[float]:
        self.calls += 1
        return [0.0 for _ in documents]

    def close(self) -> None:
        pass


def _serve_model_api(app):
    import socket
    from threading import Thread

    import uvicorn

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, "synthetic model API failed to start"
    return server, thread, f"http://127.0.0.1:{port}"


def _stop_model_api(server, thread) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "synthetic model API failed to stop"


def _synthetic_model_identity() -> dict:
    return {
        "protocol": "wenjin-model-api/1",
        "implementation_sha256": "c" * 64,
        "embedding": {
            "model_id": "synthetic-embed-v1", "asset_sha256": "a" * 64,
            "dimensions": 2, "normalized": True, "input_protocol": "raw-text-v1",
        },
        "reranker": {
            "model_id": "synthetic-rerank-v1", "asset_sha256": "b" * 64,
            "max_length": 384, "batch_size": 3,
        },
        "limits": {"max_batch_size": 2, "max_text_chars": 1000, "max_documents": 8},
    }


def _model_test_settings(tmp_path, *, max_waiting: int = 8, max_wait_seconds: float = 30):
    from wenjin.settings import Settings

    return Settings(
        _env_file=None, qdrant_api_key="synthetic-only", model_provider="local",
        embedding_model_id="synthetic-embed-v1", reranker_model_id="synthetic-rerank-v1",
        embedding_model_path=tmp_path / "unused-embedding",
        reranker_model_path=tmp_path / "unused-reranker",
        model_api_max_waiting=max_waiting,
        model_api_max_wait_seconds=max_wait_seconds,
    )


@pytest.mark.integration
def test_model_api_queue_full_and_timeout_are_explicit_http_and_provider_errors(tmp_path, monkeypatch):
    from wenjin.model_api.server import ModelAPIRuntime, create_model_api_app
    from wenjin.models.providers import ModelProviderError, RemoteModelClient

    def client_for(*, max_waiting: int, max_wait_seconds: float, embedder: _BlockingEmbedder):
        identity = _synthetic_model_identity()
        identity["admission"] = {"active": 1, "max_waiting": max_waiting, "max_wait_seconds": max_wait_seconds}
        runtime = ModelAPIRuntime(
            embedder=embedder, reranker=_FakeReranker(), identity=identity
        )
        app = create_model_api_app(
            root=tmp_path, settings=_model_test_settings(tmp_path, max_waiting=max_waiting, max_wait_seconds=max_wait_seconds),
            token="synthetic-model-admission-token-long-enough", runtime=runtime,
        )
        server, thread, url = _serve_model_api(app)
        client = RemoteModelClient(
            url, "synthetic-model-admission-token-long-enough",
            expected_embedding_model="synthetic-embed-v1",
            expected_reranker_model="synthetic-rerank-v1",
        )
        return server, thread, client

    embedder = _BlockingEmbedder(block=True)
    server, thread, client = client_for(max_waiting=0, max_wait_seconds=1, embedder=embedder)
    first_result: list[object] = []

    def first_request() -> None:
        try:
            first_result.append(client.embed(["first"], input_kind="query"))
        except Exception as exc:  # retain thread result for an assertion in the test thread
            first_result.append(exc)

    worker = threading.Thread(target=first_request, daemon=True)
    worker.start()
    try:
        assert embedder.entered.wait(2), "first fake inference did not start"
        with pytest.raises(ModelProviderError) as full:
            client.embed(["second"], input_kind="query")
        assert full.value.code == "model_queue_full"
        assert client._client.get("/v1/identity").status_code == 200
    finally:
        embedder.release.set()
        worker.join(timeout=3)
        client.close()
        _stop_model_api(server, thread)
    assert not worker.is_alive()
    assert first_result == [[[1.0, 0.0]]]

    timeout_embedder = _BlockingEmbedder(block=True)
    server, thread, client = client_for(
        max_waiting=1, max_wait_seconds=0.05, embedder=timeout_embedder
    )
    first_result = []
    worker = threading.Thread(
        target=lambda: first_result.append(client.embed(["first"], input_kind="query")), daemon=True
    )
    worker.start()
    try:
        assert timeout_embedder.entered.wait(2), "first fake inference did not start"
        with pytest.raises(ModelProviderError) as timed_out:
            client.embed(["queued"], input_kind="query")
        assert timed_out.value.code == "model_queue_timeout"
    finally:
        timeout_embedder.release.set()
        worker.join(timeout=3)
        client.close()
        _stop_model_api(server, thread)
    assert not worker.is_alive()
    assert first_result == [[[1.0, 0.0]]]


@pytest.mark.integration
def test_query_document_and_rerank_endpoints_share_one_fifo_admission(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from wenjin.model_api.server import ModelAPIRuntime, create_model_api_app

    embedder = _BlockingEmbedder(block=True)
    reranker = _FakeReranker()
    identity = _synthetic_model_identity()
    identity["admission"] = {"active": 1, "max_waiting": 2, "max_wait_seconds": 3.0}
    settings = _model_test_settings(tmp_path, max_waiting=2, max_wait_seconds=3)
    app = create_model_api_app(
        root=tmp_path,
        settings=settings,
        token="synthetic-model-admission-token-long-enough",
        runtime=ModelAPIRuntime(embedder=embedder, reranker=reranker, identity=identity),
    )
    server, thread, url = _serve_model_api(app)
    headers = {"Authorization": "Bearer synthetic-model-admission-token-long-enough"}
    common = {
        "protocol": "wenjin-model-api/1",
        "implementation_sha256": identity["implementation_sha256"],
    }
    embed_common = common | {
        "model_id": identity["embedding"]["model_id"],
        "asset_sha256": identity["embedding"]["asset_sha256"],
        "inputs": ["synthetic admission endpoint contract"],
    }
    rerank_body = common | {
        "model_id": identity["reranker"]["model_id"],
        "asset_sha256": identity["reranker"]["asset_sha256"],
        "query": "synthetic query",
        "documents": ["synthetic document"],
        "max_length": identity["reranker"]["max_length"],
        "batch_size": identity["reranker"]["batch_size"],
    }

    def post(path, body):
        return httpx.post(url + path, headers=headers, json=body, timeout=5)

    executor = ThreadPoolExecutor(max_workers=3)
    try:
        active = executor.submit(post, "/v1/embeddings", embed_common | {"input_kind": "query"})
        assert embedder.entered.wait(2), "query inference did not start"
        document = executor.submit(post, "/v1/embeddings", embed_common | {"input_kind": "document"})
        deadline = time.monotonic() + 2
        while len(app.state.inference_admission._waiting) < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(app.state.inference_admission._waiting) == 1
        rerank = executor.submit(post, "/v1/rerank", rerank_body)
        deadline = time.monotonic() + 2
        while len(app.state.inference_admission._waiting) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(app.state.inference_admission._waiting) == 2

        embedder.release.set()
        responses = [active.result(timeout=4), document.result(timeout=4), rerank.result(timeout=4)]
        assert [response.status_code for response in responses] == [200, 200, 200]
        assert [response.json().get("input_kind") for response in responses[:2]] == ["query", "document"]
        assert embedder.query_calls == 1
        assert embedder.document_calls == 1
        assert reranker.calls == 1
    finally:
        embedder.release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        _stop_model_api(server, thread)





@pytest.mark.integration
@pytest.mark.parametrize("code", ["model_queue_full", "model_queue_timeout"])
def test_model_admission_failures_are_mcp_tool_errors(code):
    from fastmcp import Client, FastMCP

    from wenjin.mcp.tools import _model_tool_error

    server = FastMCP("synthetic-model-admission-error")

    @server.tool()
    def search_docs(query: str):
        return _model_tool_error(code, query, request_id="a" * 32)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool(
                "search_docs", {"query": "synthetic"}, raise_on_error=False
            )
            assert result.is_error is True
            assert result.structured_content["error"] == code
            assert result.structured_content["evidence"] == []
            assert result.structured_content["evidence_status"] == "not_retrieved"

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("code", ["model_queue_full", "model_queue_timeout"])
def test_managed_http_model_errors_reach_mcp_as_typed_tool_errors(tmp_path, code):
    from fastmcp import Client
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from wenjin.access import AccessPolicy, AccessRegistry
    from wenjin.audit import SearchAudit
    from wenjin.mcp.managed_client import ManagedFileHTTPClient
    from wenjin.mcp.runtime import RuntimeIdentity
    from wenjin.mcp.server import build_mcp_server

    async def fail_search(_request):
        return JSONResponse({"error": code}, status_code=503)

    management = Starlette(routes=[Route("/api/search", fail_search, methods=["POST"])])
    server, thread, url = _serve_model_api(management)
    bridge = ManagedFileHTTPClient(url, "synthetic-management-token")
    mcp = build_mcp_server(
        service=object(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=AccessRegistry(
            None, {"managed-files": AccessPolicy("managed-files", "public", ("*",))}
        ),
        managed_bridge=bridge,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=RuntimeIdentity("synthetic-1.0", "test", "synthetic-target"),
    )

    async def scenario() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "search_docs",
                {"file_id": "a" * 32, "query": "synthetic queue error"},
                raise_on_error=False,
            )
            assert result.is_error is True
            assert result.structured_content["error"] == code
            assert result.structured_content["evidence"] == []
            assert result.structured_content["evidence_status"] == "not_retrieved"

    try:
        asyncio.run(scenario())
    finally:
        bridge.close()
        _stop_model_api(server, thread)


@pytest.mark.integration
def test_managed_bridge_read_timeout_reaches_mcp_as_typed_tool_error(tmp_path):
    from fastmcp import Client
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from wenjin.access import AccessPolicy, AccessRegistry
    from wenjin.audit import SearchAudit
    from wenjin.mcp.managed_client import ManagedFileHTTPClient
    from wenjin.mcp.runtime import RuntimeIdentity
    from wenjin.mcp.server import build_mcp_server

    received = threading.Event()
    release = threading.Event()

    async def held_search(_request):
        received.set()
        await asyncio.to_thread(release.wait)
        return JSONResponse({"error": "model_queue_timeout"}, status_code=503)

    management = Starlette(routes=[Route("/api/search", held_search, methods=["POST"])])
    server, thread, url = _serve_model_api(management)
    bridge = ManagedFileHTTPClient(
        url, "synthetic-management-token", timeout=httpx.Timeout(0.05, connect=1, pool=1)
    )
    mcp = build_mcp_server(
        service=object(),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=AccessRegistry(
            None, {"managed-files": AccessPolicy("managed-files", "public", ("*",))}
        ),
        managed_bridge=bridge,
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=RuntimeIdentity("synthetic-1.0", "test", "synthetic-target"),
    )

    async def scenario() -> None:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "search_docs",
                {"file_id": "b" * 32, "query": "synthetic bridge timeout"},
                raise_on_error=False,
            )
            assert received.is_set(), "management HTTP endpoint did not receive the request"
            assert result.is_error is True
            assert result.structured_content["error"] == "management_bridge_read_timeout"
            assert result.structured_content["evidence"] == []
            assert result.structured_content["evidence_status"] == "not_retrieved"

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        bridge.close()
        _stop_model_api(server, thread)


@pytest.mark.integration
def test_fastmcp_streamable_http_wire_sets_is_error_and_structured_code():
    import httpx
    from fastmcp import FastMCP

    from wenjin.mcp.tools import _model_tool_error

    app = FastMCP("synthetic-model-admission-wire")

    @app.tool()
    def search_docs(query: str):
        return _model_tool_error("model_queue_full", query, request_id="c" * 32)

    server, thread, base_url = _serve_model_api(app.http_app(path="/mcp"))
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def decode_rpc(response):
        assert response.status_code == 200, response.status_code
        if "application/json" in response.headers.get("content-type", ""):
            return response.json()
        for line in response.text.splitlines():
            if line.startswith("data:"):
                value = json.loads(line[5:].strip())
                if isinstance(value, dict) and "jsonrpc" in value:
                    return value
        raise AssertionError("MCP HTTP response did not contain a JSON-RPC message")

    try:
        with httpx.Client(base_url=base_url, timeout=3, trust_env=False) as client:
            init_response = client.post(
                "/mcp", headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                 "clientInfo": {"name": "admission-test", "version": "1"}}},
            )
            initialized = decode_rpc(init_response)
            assert initialized.get("result", {}).get("protocolVersion") == "2025-11-25"
            session_id = init_response.headers.get("MCP-Session-Id")
            session_id = session_id or init_response.headers.get("Mcp-Session-Id")
            session_id = session_id or client.cookies.get("mcp-session-id")
            versioned_headers = headers | {"MCP-Protocol-Version": "2025-11-25"}
            response = client.post(
                "/mcp", headers=versioned_headers | {"MCP-Session-Id": session_id},
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert response.status_code in {200, 202}
            session_id = response.headers.get("MCP-Session-Id") or session_id
            assert session_id, "MCP server did not issue a stateful session id"
            result = decode_rpc(client.post(
                "/mcp", headers=versioned_headers | {"MCP-Session-Id": session_id},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "search_docs", "arguments": {"query": "synthetic"}}},
            ))["result"]
            assert result["isError"] is True
            assert result["structuredContent"]["error"] == "model_queue_full"
            assert result["structuredContent"]["evidence_status"] == "not_retrieved"
    finally:
        _stop_model_api(server, thread)


@pytest.mark.integration
def test_tcp_disconnect_removes_queued_request_and_following_request_succeeds(tmp_path, monkeypatch):
    import socket
    from threading import Thread

    from wenjin.model_api.admission import ModelAdmission
    from wenjin.model_api.server import ModelAPIRuntime, create_model_api_app
    from wenjin.models.providers import RemoteModelClient

    queued_added = threading.Event()
    queued_removed = threading.Event()
    original_submit = ModelAdmission.submit
    original_remove = ModelAdmission._remove_waiting

    async def tracked_submit(self, operation, *args, **kwargs):
        inner = asyncio.create_task(original_submit(self, operation, *args, **kwargs))
        await asyncio.sleep(0)
        was_waiting = bool(self._waiting)
        if was_waiting:
            queued_added.set()
        try:
            return await inner
        except asyncio.CancelledError:
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            raise

    def tracked_remove(self, job):
        removed = original_remove(self, job)
        if removed:
            queued_removed.set()
        return removed

    monkeypatch.setattr(ModelAdmission, "submit", tracked_submit)
    monkeypatch.setattr(ModelAdmission, "_remove_waiting", tracked_remove)
    embedder = _BlockingEmbedder(block=True)
    identity = _synthetic_model_identity()
    identity["admission"] = {"active": 1, "max_waiting": 1, "max_wait_seconds": 5}
    runtime = ModelAPIRuntime(embedder=embedder, reranker=_FakeReranker(), identity=identity)
    app = create_model_api_app(
        root=tmp_path, settings=_model_test_settings(tmp_path, max_waiting=1, max_wait_seconds=5),
        token="synthetic-model-admission-token-long-enough", runtime=runtime,
    )
    server, server_thread, url = _serve_model_api(app)
    client = RemoteModelClient(
        url, "synthetic-model-admission-token-long-enough",
        expected_embedding_model="synthetic-embed-v1", expected_reranker_model="synthetic-rerank-v1",
    )
    first_result: list[object] = []
    third_result: list[object] = []

    def call_into(result: list[object], text: str) -> None:
        try:
            result.append(client.embed([text], input_kind="query"))
        except Exception as exc:  # capture for assertion on the main thread
            result.append(exc)

    first_thread = Thread(target=call_into, args=(first_result, "active"), daemon=True)
    raw_socket = None
    third_thread = None
    try:
        first_thread.start()
        assert embedder.entered.wait(2), "active fake inference did not start"
        body = json.dumps({
            "protocol": "wenjin-model-api/1", "model_id": "synthetic-embed-v1",
            "asset_sha256": "a" * 64, "implementation_sha256": "c" * 64,
            "input_kind": "query", "inputs": ["queued-disconnect"],
        }).encode()
        parsed = httpx.URL(url)
        raw_socket = socket.create_connection((parsed.host, parsed.port))
        headers = (
            f"POST /v1/embeddings HTTP/1.1\r\nHost: {parsed.host}:{parsed.port}\r\n"
            "Authorization: Bearer synthetic-model-admission-token-long-enough\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        raw_socket.sendall(headers + body)
        assert queued_added.wait(2), "second TCP request did not enter the waiting queue"
        raw_socket.shutdown(socket.SHUT_RDWR)
        raw_socket.close()
        raw_socket = None
        assert queued_removed.wait(2), "HTTP disconnect did not remove the queued admission"

        third_thread = Thread(target=call_into, args=(third_result, "after-disconnect"), daemon=True)
        third_thread.start()
        release_first = embedder.release
        release_first.set()
        first_thread.join(timeout=3)
        third_thread.join(timeout=3)
        assert not first_thread.is_alive() and not third_thread.is_alive()
        assert first_result == [[[1.0, 0.0]]]
        assert third_result == [[[1.0, 0.0]]]
    finally:
        if raw_socket is not None:
            raw_socket.close()
        embedder.release.set()
        client.close()
        _stop_model_api(server, server_thread)


@pytest.mark.integration
def test_tcp_disconnect_during_active_inference_does_not_release_worker_slot(tmp_path, monkeypatch):
    import socket
    from threading import Thread

    from wenjin.model_api.admission import ModelAdmission
    from wenjin.model_api.server import ModelAPIRuntime, create_model_api_app
    from wenjin.models.providers import RemoteModelClient

    active_cancelled = threading.Event()
    original_submit = ModelAdmission.submit

    async def tracked_submit(self, operation, *args, **kwargs):
        inner = asyncio.create_task(original_submit(self, operation, *args, **kwargs))
        await asyncio.sleep(0)
        was_waiting = bool(self._waiting)
        try:
            return await inner
        except asyncio.CancelledError:
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            if not was_waiting:
                active_cancelled.set()
            raise

    monkeypatch.setattr(ModelAdmission, "submit", tracked_submit)
    embedder = _BlockingEmbedder(block=True)
    identity = _synthetic_model_identity()
    identity["admission"] = {"active": 1, "max_waiting": 1, "max_wait_seconds": 5}
    runtime = ModelAPIRuntime(embedder=embedder, reranker=_FakeReranker(), identity=identity)
    app = create_model_api_app(
        root=tmp_path, settings=_model_test_settings(tmp_path, max_waiting=1, max_wait_seconds=5),
        token="synthetic-model-admission-token-long-enough", runtime=runtime,
    )
    server, server_thread, url = _serve_model_api(app)
    client = RemoteModelClient(
        url, "synthetic-model-admission-token-long-enough",
        expected_embedding_model="synthetic-embed-v1", expected_reranker_model="synthetic-rerank-v1",
    )
    second_result: list[object] = []
    raw_socket = None
    second_thread = None
    try:
        parsed = httpx.URL(url)
        raw_socket = socket.create_connection((parsed.host, parsed.port))
        body = json.dumps({
            "protocol": "wenjin-model-api/1", "model_id": "synthetic-embed-v1",
            "asset_sha256": "a" * 64, "implementation_sha256": "c" * 64,
            "input_kind": "query", "inputs": ["active-disconnect"],
        }).encode()
        headers = (
            f"POST /v1/embeddings HTTP/1.1\r\nHost: {parsed.host}:{parsed.port}\r\n"
            "Authorization: Bearer synthetic-model-admission-token-long-enough\r\n"
            "Content-Type: application/json\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        raw_socket.sendall(headers + body)
        assert embedder.entered.wait(2), "raw TCP fake inference did not start"
        raw_socket.shutdown(socket.SHUT_RDWR)
        raw_socket.close()
        raw_socket = None
        assert active_cancelled.wait(2), "active TCP disconnect was not observed by the handler"

        second_thread = Thread(target=lambda: second_result.append(
            client.embed(["after-active-disconnect"], input_kind="query")
        ), daemon=True)
        second_thread.start()
        assert not embedder.second_entered.wait(0.2), "next inference started before disconnected thread ended"
        embedder.release.set()
        second_thread.join(timeout=3)
        assert not second_thread.is_alive()
        assert second_result == [[[1.0, 0.0]]]
    finally:
        if raw_socket is not None:
            raw_socket.close()
        embedder.release.set()
        client.close()
        _stop_model_api(server, server_thread)


