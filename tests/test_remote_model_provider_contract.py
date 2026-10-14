"""Remote model HTTP/provider contracts using only deterministic synthetic inference."""

from __future__ import annotations

import json
import math
import socket
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from threading import Thread

import httpx
import pytest
import uvicorn
from pydantic import ValidationError
from starlette.middleware import Middleware

from wenjin.audit import SearchAudit
from wenjin.file_management import FileManagementService
from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.management_http.app import create_app
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.server import build_mcp_server
from wenjin.model_api.server import ModelAPIRuntime, create_model_api_app
from wenjin.models.providers import (
    PROTOCOL,
    ModelProviderError,
    RemoteEmbedding,
    RemoteReranker,
    build_remote_model_bindings,
)
from wenjin.settings import Settings

pytestmark = pytest.mark.integration

_TOKEN = "synthetic-model-api-bearer-token-for-contract"
_EMBED_ID = "synthetic-embed-stable-id-v1"
_RERANK_ID = "synthetic-rerank-stable-id-v1"
_SHA = "a" * 64


class _SyntheticEmbedder:
    def __init__(self):
        self.query_inputs: list[str] = []
        self.document_batches: list[list[str]] = []

    @staticmethod
    def _vector(text: str) -> list[float]:
        # Deterministic normalized vector: the two inputs remain distinguishable.
        return [1.0, 0.0] if text.startswith("query:") else [0.0, 1.0]

    def get_query_embedding(self, text: str) -> list[float]:
        self.query_inputs.append(text)
        return self._vector(f"query:{text}")

    def get_text_embedding_batch(self, texts, *, show_progress=False):
        del show_progress
        self.document_batches.append(list(texts))
        return [self._vector(text) for text in texts]


class _SyntheticReranker:
    max_length = 384
    batch_size = 3

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        return [float(index) / 10 for index in range(len(documents))]

    def close(self):
        pass


def _identity(embed_id=_EMBED_ID, rerank_id=_RERANK_ID, *, max_wait_seconds=30.0):
    return {
        "protocol": PROTOCOL,
        "implementation_sha256": "c" * 64,
        "embedding": {
            "model_id": embed_id,
            "asset_sha256": _SHA,
            "dimensions": 2,
            "normalized": True,
            "input_protocol": "raw-text-v1",
        },
        "reranker": {
            "model_id": rerank_id,
            "asset_sha256": "b" * 64,
            "max_length": 384,
            "batch_size": 3,
        },
        "limits": {"max_batch_size": 2, "max_text_chars": 1000, "max_documents": 8},
        "admission": {"active": 1, "max_waiting": 8, "max_wait_seconds": max_wait_seconds},
    }


def _serve(app, *, ssl_certfile=None, ssl_keyfile=None):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
        if ssl_certfile is None else uvicorn.Config(
            app,
            log_level="critical",
            access_log=False,
            lifespan="on",
            ssl_certfile=str(ssl_certfile),
            ssl_keyfile=str(ssl_keyfile),
        )
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise RuntimeError("synthetic model API failed to start")
    return server, thread, f"http://127.0.0.1:{port}"


def _stop(server, thread):
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "synthetic model API failed to stop"


def _server_settings(tmp_path):
    return Settings(
        _env_file=None,
        qdrant_api_key="synthetic-only",
        model_provider="local",
        embedding_model_id=_EMBED_ID,
        reranker_model_id=_RERANK_ID,
        embedding_model_path=tmp_path / "no-model-loaded-embedding",
        reranker_model_path=tmp_path / "no-model-loaded-reranker",
    )


def _remote_settings(
    url, *, embed_id=_EMBED_ID, rerank_id=_RERANK_ID, token=_TOKEN, ca_bundle=None
):
    return Settings(
        _env_file=None,
        qdrant_api_key="synthetic-only",
        model_provider="remote",
        model_api_url=url,
        model_api_token=token,
        model_api_ca_bundle=ca_bundle,
        embedding_model_id=embed_id,
        reranker_model_id=rerank_id,
        # These deliberately point nowhere: remote mode must not load local weights.
        embedding_model_path="missing-local-embedding-tree",
        reranker_model_path="missing-local-reranker-tree",
        reranker_max_length=384,
        reranker_batch_size=3,
    )


def test_remote_adapters_use_one_authenticated_http_contract_for_query_document_and_rerank(tmp_path):
    embedder = _SyntheticEmbedder()
    reranker = _SyntheticReranker()
    runtime = ModelAPIRuntime(embedder=embedder, reranker=reranker, identity=_identity())
    app = create_model_api_app(
        root=tmp_path, settings=_server_settings(tmp_path), token=_TOKEN, runtime=runtime
    )
    server, thread, url = _serve(app)
    bindings = None
    try:
        settings = _remote_settings(url)
        bindings = build_remote_model_bindings(settings, root=tmp_path)

        assert isinstance(bindings.embedder, RemoteEmbedding)
        assert isinstance(bindings.reranker, RemoteReranker)
        assert bindings.identity["provider"] == "remote"
        assert bindings.identity["embedding"]["model_id"] == _EMBED_ID
        assert bindings.identity["reranker"]["model_id"] == _RERANK_ID

        query_vector = bindings.embedder.get_query_embedding("same raw input")
        document_vectors = bindings.embedder.get_text_embedding_batch(
            ["same raw input", "doc two", "doc three"]
        )
        scores = bindings.reranker.score("alpha query", ["doc one", "doc two"])

        assert query_vector == [1.0, 0.0]
        assert document_vectors == [[0.0, 1.0]] * 3
        assert [math.sqrt(sum(value * value for value in vector)) for vector in document_vectors] == [1.0] * 3
        assert embedder.query_inputs == ["same raw input"]
        # The remote adapter respects max_batch_size=2 and still uses document semantics.
        assert embedder.document_batches == [["same raw input", "doc two"], ["doc three"]]
        assert scores == [0.0, 0.1]
        assert reranker.calls == [("alpha query", ["doc one", "doc two"])]

        with httpx.Client(timeout=2) as client:
            health = client.get(f"{url}/v1/health", headers={"Authorization": f"Bearer {_TOKEN}"})
            denied = client.get(f"{url}/v1/identity")
        assert health.status_code == 200
        assert health.json() == {"protocol": PROTOCOL, "status": "ok"}
        assert denied.status_code == 401
        assert denied.json() == {"error": "unauthorized"}
    finally:
        if bindings is not None:
            bindings.close()
            bindings.close()
        _stop(server, thread)


def test_remote_identity_mismatch_and_upstream_failure_fail_closed_without_local_fallback(tmp_path, monkeypatch):
    embedder = _SyntheticEmbedder()
    reranker = _SyntheticReranker()
    runtime = ModelAPIRuntime(
        embedder=embedder,
        reranker=reranker,
        identity=_identity(embed_id="different-stable-embed-id"),
    )
    app = create_model_api_app(
        root=tmp_path, settings=_server_settings(tmp_path), token=_TOKEN, runtime=runtime
    )
    server, thread, url = _serve(app)
    monkeypatch.setattr(
        Settings,
        "embedding",
        lambda *_args, **_kwargs: pytest.fail("remote adapter attempted local embedding fallback"),
    )
    monkeypatch.setattr(
        Settings,
        "resolved_model_path",
        lambda *_args, **_kwargs: pytest.fail("remote adapter resolved a local model path"),
    )
    try:
        with pytest.raises(ModelProviderError) as mismatch:
            build_remote_model_bindings(_remote_settings(url), root=tmp_path)
        assert mismatch.value.code == "model_identity_mismatch"
        # Constructor rejects the server identity before any query/document inference.
        assert embedder.query_inputs == []
        assert embedder.document_batches == []
    finally:
        _stop(server, thread)


def test_remote_provider_errors_do_not_echo_credentials_query_or_server_exception(tmp_path):
    secret = "synthetic-secret-do-not-echo"
    query = "synthetic-query-do-not-echo"

    class BrokenEmbedder:
        def get_query_embedding(self, _text):
            raise RuntimeError(f"{secret} {query} C:\\private\\model")

        def get_text_embedding_batch(self, texts, *, show_progress=False):
            del texts, show_progress
            raise RuntimeError(secret)

    runtime = ModelAPIRuntime(
        embedder=BrokenEmbedder(), reranker=_SyntheticReranker(), identity=_identity()
    )
    app = create_model_api_app(
        root=tmp_path, settings=_server_settings(tmp_path), token=_TOKEN, runtime=runtime
    )
    server, thread, url = _serve(app)
    client = None
    try:
        with pytest.raises(ModelProviderError) as error:
            from wenjin.models.providers import RemoteModelClient

            client = RemoteModelClient(
                url,
                _TOKEN,
                expected_embedding_model=_EMBED_ID,
                expected_reranker_model=_RERANK_ID,
            )
            client.embed([query], input_kind="query")
        assert error.value.code == "model_provider_unavailable"
        rendered = str(error.value)
        assert all(value not in rendered for value in (secret, query, _TOKEN, "C:\\private"))
    finally:
        if client is not None:
            client.close()
        _stop(server, thread)


def test_remote_settings_require_explicit_identity_and_provider_does_not_resolve_weight_paths(tmp_path):
    settings = _remote_settings("http://127.0.0.1:1")
    settings.validate_model_provider()
    assert settings.resolved_embedding_model_id == _EMBED_ID
    assert settings.resolved_reranker_model_id == _RERANK_ID
    assert settings.resolved_model_path("embedding_model_path", root=tmp_path).name == "missing-local-embedding-tree"

    with pytest.raises(ValidationError, match="必须显式配置"):
        Settings(
            _env_file=None,
            qdrant_api_key="synthetic-only",
            model_provider="remote",
            model_api_url="http://127.0.0.1:1",
            model_api_token=_TOKEN,
        )


def test_remote_import_path_does_not_import_local_inference_stack():
    script = textwrap.dedent(
        """
        import importlib.abc
        import sys

        blocked = ('torch', 'transformers', 'sentence_transformers',
                   'llama_index.embeddings.huggingface')

        class BlockLocalInference(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == item or fullname.startswith(item + '.') for item in blocked):
                    raise AssertionError('unexpected local inference import: ' + fullname)
                return None

        sys.meta_path.insert(0, BlockLocalInference())
        import wenjin.settings
        import wenjin.models.providers
        assert not any(name == 'torch' or name.startswith('transformers')
                       or name.startswith('sentence_transformers')
                       for name in sys.modules)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_remote_timeout_is_a_safe_provider_error():
    from wenjin.models.providers import RemoteModelClient

    observed_timeouts = []

    def handler(request):
        observed_timeouts.append(request.extensions.get("timeout"))
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json=_identity(max_wait_seconds=0.1))
        raise httpx.ReadTimeout("synthetic token/query/path must not escape", request=request)

    client = RemoteModelClient(
        "http://model-api.invalid",
        _TOKEN,
        expected_embedding_model=_EMBED_ID,
        expected_reranker_model=_RERANK_ID,
        timeout_seconds=0.25,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelProviderError) as error:
            client.embed(["synthetic private query"], input_kind="query")
        assert error.value.code == "model_provider_read_timeout"
        assert "synthetic" not in str(error.value)
        assert len(observed_timeouts) == 2
        assert all(timeout["read"] == 0.25 for timeout in observed_timeouts)
    finally:
        client.close()


def test_identity_fetch_failure_closes_only_owned_http_client():
    from wenjin.models.providers import RemoteModelClient

    class InvalidIdentityTransport(httpx.BaseTransport):
        def __init__(self):
            self.closed = False

        def handle_request(self, request):
            return httpx.Response(200, json={"protocol": PROTOCOL}, request=request)

        def close(self):
            self.closed = True

    owned_transport = InvalidIdentityTransport()
    with pytest.raises(ModelProviderError) as error:
        RemoteModelClient(
            "http://synthetic.invalid",
            _TOKEN,
            expected_embedding_model=_EMBED_ID,
            expected_reranker_model=_RERANK_ID,
            transport=owned_transport,
        )
    assert error.value.code == "model_identity_invalid"
    assert owned_transport.closed is True

    caller_transport = InvalidIdentityTransport()
    caller_client = httpx.Client(transport=caller_transport)
    with pytest.raises(ModelProviderError):
        RemoteModelClient(
            "http://synthetic.invalid",
            _TOKEN,
            expected_embedding_model=_EMBED_ID,
            expected_reranker_model=_RERANK_ID,
            client=caller_client,
        )
    assert caller_transport.closed is False
    caller_client.close()
    assert caller_transport.closed is True


def test_remote_requests_pin_model_and_implementation_hashes_after_handshake():
    from wenjin.models.providers import RemoteModelClient

    request_bodies = []

    def handler(request):
        if request.url.path == "/v1/identity":
            return httpx.Response(200, json=_identity())
        body = json.loads(request.content)
        request_bodies.append((request.url.path, body))
        assert body["asset_sha256"] in {_SHA, "b" * 64}
        assert body["implementation_sha256"] == "c" * 64
        if request.url.path == "/v1/embeddings":
            # Simulate a remote node replacing model weights under the same model ID.
            return httpx.Response(409, json={"error": "model_identity_mismatch"})
        return httpx.Response(
            200,
            json={
                "protocol": PROTOCOL,
                "model_id": _RERANK_ID,
                "asset_sha256": "d" * 64,
                "implementation_sha256": "e" * 64,
                "scores": [0.5],
            },
        )

    client = RemoteModelClient(
        "http://model-api.invalid",
        _TOKEN,
        expected_embedding_model=_EMBED_ID,
        expected_reranker_model=_RERANK_ID,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ModelProviderError) as embedding_error:
            client.embed(["synthetic drift query"], input_kind="query")
        assert embedding_error.value.code == "model_provider_unavailable"
        with pytest.raises(ModelProviderError) as rerank_error:
            client.rerank("synthetic drift query", ["doc"], max_length=384, batch_size=3)
        assert rerank_error.value.code == "model_provider_response_invalid"
        assert request_bodies[0][1]["asset_sha256"] == _SHA
        assert request_bodies[1][1]["asset_sha256"] == "b" * 64
        assert request_bodies[0][1]["model_id"] == _EMBED_ID
        assert request_bodies[1][1]["model_id"] == _RERANK_ID
    finally:
        client.close()


def test_remote_provider_uses_configured_private_ca_for_https_and_rejects_wrong_ca(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    from wenjin.models.providers import RemoteModelClient

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-model-api")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "synthetic-ca.pem"
    key_path = tmp_path / "synthetic-server-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    wrong_ca = tmp_path / "wrong-ca.pem"
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "untrusted-synthetic-ca")])
    wrong_certificate = (
        x509.CertificateBuilder()
        .subject_name(wrong_name)
        .issuer_name(wrong_name)
        .public_key(wrong_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(wrong_key, hashes.SHA256())
    )
    wrong_ca.write_bytes(wrong_certificate.public_bytes(serialization.Encoding.PEM))

    runtime = ModelAPIRuntime(
        embedder=_SyntheticEmbedder(), reranker=_SyntheticReranker(), identity=_identity()
    )
    app = create_model_api_app(
        root=tmp_path, settings=_server_settings(tmp_path), token=_TOKEN, runtime=runtime
    )
    server, thread, http_url = _serve(app, ssl_certfile=cert_path, ssl_keyfile=key_path)
    bindings = None
    try:
        https_url = http_url.replace("http://", "https://", 1)
        bindings = build_remote_model_bindings(
            _remote_settings(https_url, ca_bundle=cert_path), root=tmp_path
        )
        assert bindings.identity["ca_bundle_sha256"] is not None
        assert bindings.embedder.get_query_embedding("private ca handshake") == [1.0, 0.0]

        with pytest.raises(ModelProviderError) as rejected:
            RemoteModelClient(
                https_url,
                _TOKEN,
                expected_embedding_model=_EMBED_ID,
                expected_reranker_model=_RERANK_ID,
                ca_bundle=str(wrong_ca),
            )
        assert rejected.value.code == "model_provider_unavailable"
        assert "wrong-ca" not in str(rejected.value)
    finally:
        if bindings is not None:
            bindings.close()
        _stop(server, thread)


def test_stateful_mcp_over_tcp_uses_remote_provider_through_management_http_bridge(tmp_path):
    from mcp.types import LATEST_PROTOCOL_VERSION
    from qdrant_client import QdrantClient

    class BridgeEmbedder:
        def __init__(self):
            self.query_inputs = []
            self.document_batches = []

        def get_query_embedding(self, text):
            self.query_inputs.append(text)
            return [1.0, 0.0]

        def get_text_embedding_batch(self, texts, *, show_progress=False):
            del show_progress
            self.document_batches.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    class ManagedOnlyService:
        search_policy = None
        reranker = None
        search_lock = None
        supported_version = "synthetic-managed-only"
        index_target_id = "synthetic-managed-target"

        def search(self, *_args, **_kwargs):
            raise AssertionError("managed MCP query must use the real HTTP bridge")

    def rpc(client, headers, body):
        response = client.post("/mcp", headers=headers, json=body)
        assert response.status_code == 200, response.text
        if "application/json" in response.headers.get("content-type", ""):
            return response.json(), response.headers
        events = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert events, response.text
        return json.loads(events[-1]), response.headers

    embedder = BridgeEmbedder()
    model_runtime = ModelAPIRuntime(
        embedder=embedder,
        reranker=None,
        identity=_identity(),
    )
    model_app = create_model_api_app(
        root=tmp_path,
        settings=_server_settings(tmp_path),
        token=_TOKEN,
        runtime=model_runtime,
    )
    model_api_responses = []

    async def observe_model_api(scope, receive, send):
        if scope.get("type") != "http":
            await model_app(scope, receive, send)
            return
        response_status = None
        response_body = bytearray()

        async def observed_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
            await send(message)

        await model_app(scope, receive, observed_send)
        model_api_responses.append((scope.get("path"), response_status, bytes(response_body)))

    model_server, model_thread, model_url = _serve(observe_model_api)
    bindings = None
    qdrant = QdrantClient(":memory:")
    manager = None
    management_server = management_thread = None
    mcp_server = mcp_thread = None
    admin_http = mcp_http = None
    bridge = None
    audit = None
    try:
        remote_settings = _remote_settings(model_url)
        remote_settings.search_reranker_enabled = False
        bindings = build_remote_model_bindings(remote_settings, root=tmp_path)
        manager = FileManagementService(
            root=tmp_path,
            qdrant_client=qdrant,
            embedder=bindings.embedder,
            embedding_model=_EMBED_ID,
        )

        identity_path = tmp_path / "data" / "identity" / "identity.sqlite"
        identities = IdentityStore(identity_path)
        identities.initialize()
        admin_password = "Synthetic_Remote_Admin_2026!"
        reader_temp_password = "Synthetic_Remote_Reader_Temporary_2026!"
        reader_password = "Synthetic_Remote_Reader_Ready_2026!"
        identities.bootstrap_first_admin("remote-admin", admin_password)
        admin_login = identities.login("remote-admin", admin_password)
        reader = identities.create_user(
            admin_login.principal,
            "remote-reader",
            reader_temp_password,
            role="reader",
        )
        temporary = identities.login("remote-reader", reader_temp_password)
        identities.change_password(temporary.token, reader_temp_password, reader_password)
        credential = identities.issue_mcp_credential(
            identities.login("remote-reader", reader_password).principal,
            label="remote-provider-tcp",
        )

        management_app = create_app(
            tmp_path,
            manager=manager,
            identity_db=identity_path,
        )
        management_server, management_thread, management_url = _serve(management_app)
        admin_http = httpx.Client(base_url=management_url, timeout=10)
        login = admin_http.post(
            "/api/auth/login",
            headers={"Origin": management_url},
            json={"username": "remote-admin", "password": admin_password},
        )
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]
        admin_headers = {"Origin": management_url, "X-CSRF-Token": csrf}

        content = b"Synthetic source for remote model bridge marker remote_bridge_alpha.\n"
        uploaded = admin_http.post(
            "/api/files",
            headers={**admin_headers, "X-File-Name": "remote-bridge.txt"},
            content=content,
        )
        assert uploaded.status_code == 202, uploaded.text
        job_id = uploaded.json()["job_id"]
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            job = admin_http.get(f"/api/jobs/{job_id}")
            assert job.status_code == 200, job.text
            if job.json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job.json()["status"] == "completed", (
            f"{job.text}; model-api-responses={model_api_responses!r}"
        )
        file_id = job.json()["file_id"]
        file_record = manager.get_file(file_id)
        identities.synchronize_resource("library", file_record.library_id, active=True)
        identities.synchronize_resource("managed_file", file_id, active=True)
        identities.set_grant(
            admin_login.principal,
            resource_kind="library",
            resource_id=file_record.library_id,
            user_id=reader.user_id,
            allowed=True,
            expected_revision=0,
        )
        identities.set_grant(
            admin_login.principal,
            resource_kind="managed_file",
            resource_id=file_id,
            user_id=reader.user_id,
            allowed=True,
            expected_revision=0,
        )

        bridge = ManagedFileHTTPClient(management_url, "", account_mode=True)
        audit = SearchAudit(tmp_path / "artifacts" / "remote-provider-mcp-audit.jsonl")
        mcp = build_mcp_server(
            service=ManagedOnlyService(),
            audit=audit,
            project_root=tmp_path,
            identity=RuntimeIdentity(
                corpus_id="synthetic-managed",
                corpus_id_source="synthetic",
                target_id="synthetic-managed-target",
                embedding_model=_EMBED_ID,
                model_provider="remote",
                model_identity=bindings.identity,
            ),
            managed_bridge=bridge,
            identity_store=identities,
        )
        mcp_app = mcp.http_app(
            path="/mcp",
            middleware=[Middleware(McpIdentityMiddleware, identity_store=identities)],
            stateless_http=False,
            json_response=True,
        )
        mcp_server, mcp_thread, mcp_url = _serve(mcp_app)
        mcp_http = httpx.Client(base_url=mcp_url, timeout=10)
        auth = {"Authorization": f"Bearer {credential.token}", "Accept": "application/json, text/event-stream"}
        initialized, init_headers = rpc(
            mcp_http,
            auth,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "remote-provider-contract", "version": "1"},
                },
            },
        )
        session = init_headers.get("Mcp-Session-Id")
        assert session
        session_headers = {**auth, "MCP-Session-Id": session}
        initialized_notice = mcp_http.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized_notice.status_code in {200, 202}

        listed, _ = rpc(
            mcp_http,
            session_headers,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "list_managed_files", "arguments": {}}},
        )
        list_result = listed["result"].get("structuredContent")
        if not isinstance(list_result, dict):
            list_result = json.loads(listed["result"]["content"][0]["text"])
        assert any(item["file_id"] == file_id for item in list_result["files"])

        searched, _ = rpc(
            mcp_http,
            session_headers,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "search_docs", "arguments": {
                 "file_id": file_id, "query": "remote_bridge_alpha", "top_k": 1,
             }}},
        )
        result = searched["result"].get("structuredContent")
        if not isinstance(result, dict):
            result = json.loads(searched["result"]["content"][0]["text"])
        assert result["evidence"]
        assert "remote_bridge_alpha" in result["evidence"][0]["text"]
        source = mcp_http.get(result["evidence"][0]["download_path"], headers=session_headers)
        assert source.status_code == 200, source.text
        assert source.content == content
        # Same service path handled ingestion documents and MCP search queries via remote HTTP.
        assert any("remote_bridge_alpha" in text for batch in embedder.document_batches for text in batch)
        assert "remote_bridge_alpha" in embedder.query_inputs
        assert identities.can_read(
            identities.login("remote-reader", reader_password).principal,
            "managed_file",
            file_id,
        )

        replacement_content = b"Synthetic replacement for remote model bridge marker remote_bridge_beta.\n"
        replacement = admin_http.put(
            f"/api/files/{file_id}",
            headers={**admin_headers, "X-File-Name": "remote-bridge.txt"},
            content=replacement_content,
        )
        assert replacement.status_code == 202, replacement.text
        replacement_job_id = replacement.json()["job_id"]
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            replacement_job = admin_http.get(f"/api/jobs/{replacement_job_id}")
            assert replacement_job.status_code == 200, replacement_job.text
            if replacement_job.json()["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert replacement_job.json()["status"] == "completed", replacement_job.text

        old_source = mcp_http.get(result["evidence"][0]["download_path"], headers=session_headers)
        assert old_source.status_code == 409, old_source.text
        assert b"remote_bridge_alpha" not in old_source.content
        searched_v2, _ = rpc(
            mcp_http,
            session_headers,
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "search_docs", "arguments": {
                 "file_id": file_id, "query": "remote_bridge_beta", "top_k": 1,
             }}},
        )
        result_v2 = searched_v2["result"].get("structuredContent")
        if not isinstance(result_v2, dict):
            result_v2 = json.loads(searched_v2["result"]["content"][0]["text"])
        assert result_v2["evidence"]
        assert "remote_bridge_beta" in result_v2["evidence"][0]["text"]
        source_v2 = mcp_http.get(result_v2["evidence"][0]["download_path"], headers=session_headers)
        assert source_v2.status_code == 200, source_v2.text
        assert source_v2.content == replacement_content
        assert any("remote_bridge_beta" in text for batch in embedder.document_batches for text in batch)
        assert "remote_bridge_beta" in embedder.query_inputs

        deleted = admin_http.delete(f"/api/files/{file_id}", headers=admin_headers)
        assert deleted.status_code == 202, deleted.text
        delete_job_id = deleted.json()["job_id"]
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            delete_job = admin_http.get(f"/api/jobs/{delete_job_id}")
            assert delete_job.status_code == 200, delete_job.text
            if delete_job.json()["status"] in {"deleted", "failed"}:
                break
            time.sleep(0.02)
        assert delete_job.json()["status"] == "deleted", delete_job.text
        listed_after_delete, _ = rpc(
            mcp_http,
            session_headers,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "list_managed_files", "arguments": {}}},
        )
        deleted_files = listed_after_delete["result"].get("structuredContent")
        if not isinstance(deleted_files, dict):
            deleted_files = json.loads(listed_after_delete["result"]["content"][0]["text"])
        assert all(item["file_id"] != file_id for item in deleted_files["files"])
        deleted_search, _ = rpc(
            mcp_http,
            session_headers,
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
             "params": {"name": "search_docs", "arguments": {
                 "file_id": file_id, "query": "remote_bridge_beta", "top_k": 1,
             }}},
        )
        deleted_result = deleted_search["result"].get("structuredContent")
        if not isinstance(deleted_result, dict):
            deleted_result = json.loads(deleted_search["result"]["content"][0]["text"])
        assert deleted_result["evidence"] == []
        deleted_source = mcp_http.get(result_v2["evidence"][0]["download_path"], headers=session_headers)
        assert deleted_source.status_code == 409, deleted_source.text
        assert b"remote_bridge_beta" not in deleted_source.content
    finally:
        if mcp_http is not None:
            mcp_http.close()
        if mcp_server is not None:
            _stop(mcp_server, mcp_thread)
        if bridge is not None:
            bridge.close()
        if admin_http is not None:
            admin_http.close()
        if management_server is not None:
            _stop(management_server, management_thread)
        if manager is not None:
            manager.close()
        qdrant.close()
        if bindings is not None:
            bindings.close()
        _stop(model_server, model_thread)
        if audit is not None:
            audit.close()
