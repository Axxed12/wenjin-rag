"""基于现有Qwen3模型实现的Bearer保护推理API。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from pathlib import Path
from threading import RLock
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wenjin.model_api.admission import ModelAdmission, ModelAdmissionError
from wenjin.models.providers import PROTOCOL

_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_BATCH_SIZE = 128
_MAX_DOCUMENTS = 128
_MAX_TEXT_CHARS = 100_000
_DEFAULT_MAX_WAITING = 8
_DEFAULT_MAX_WAIT_SECONDS = 30.0


class ModelAPIRuntime:
    def __init__(self, *, embedder: Any, reranker: Any, identity: dict[str, Any]) -> None:
        self.embedder = embedder
        self.reranker = reranker
        self.identity = identity
        self.inference_lock = RLock()

    def close(self) -> None:
        with self.inference_lock:
            if self.reranker is not None:
                self.reranker.close()
            close_embedding = getattr(self.embedder, "close", None)
            if callable(close_embedding):
                close_embedding()

    def embed(self, inputs: list[str], input_kind: str) -> list[list[float]]:
        with self.inference_lock:
            if input_kind == "query":
                query_batch = getattr(self.embedder, "get_query_embedding_batch", None)
                vectors = (
                    query_batch(inputs, show_progress=False)
                    if callable(query_batch)
                    else [self.embedder.get_query_embedding(text) for text in inputs]
                )
            else:
                vectors = self.embedder.get_text_embedding_batch(inputs, show_progress=False)
        return [[float(component) for component in vector] for vector in vectors]

    def rerank_scores(
        self, query: str, documents: list[str], *, max_length: int, batch_size: int
    ) -> list[float]:
        if (
            self.reranker is None
            or max_length != self.reranker.max_length
            or batch_size != self.reranker.batch_size
        ):
            raise ValueError("model request rejected")
        with self.inference_lock:
            return [float(score) for score in self.reranker.score(query, documents)]


class _ClientDisconnected(Exception):
    """排队时客户端断开；仅用于停止该HTTP等待者。"""


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _implementation_fingerprint() -> str:
    package_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    # 将应用侧embedding装配与实际Qwen reranker推理代码纳入协议实现身份；
    # 同一模型ID若部署了不同本地实现，后端必须重新绑定而不能沿用旧checkpoint。
    for relative in (
        "models/providers.py",
        "model_api/server.py",
        "model_api/admission.py",
        "settings.py",
        "retrieval/lazy_reranker.py",
        "retrieval/rerank.py",
    ):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((package_root / relative).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_model_api_listen(host: str, token: str | None) -> None:
    if not token or len(token) < 24:
        raise ValueError("WENJIN_MODEL_API_TOKEN必须至少24个字符")
    if not _loopback(host) and len(token) < 32:
        raise ValueError("非loopback模型API监听需要至少32字符Bearer token")


def _build_runtime(root: Path, settings: Any, token: str) -> ModelAPIRuntime:
    if getattr(settings, "model_provider", "local") != "local":
        raise RuntimeError("模型服务端必须配置WENJIN_MODEL_PROVIDER=local")
    from wenjin.retrieval.lazy_reranker import build_shared_reranker, model_tree_fingerprint

    root = Path(root).resolve()
    embedder = None
    reranker = None
    try:
        embedding_path = settings.resolved_model_path("embedding_model_path", root=root)
        # 先固定权重树身份再加载模型，加载后再复核可发现并拒绝期间发生的替换。
        embedding_fingerprint_before = model_tree_fingerprint(embedding_path)
        embedder = settings.embedding(root=root)
        reranker = build_shared_reranker(settings, root=root)
        if reranker is None:
            raise RuntimeError("模型API要求配置reranker")
        reranker_fingerprint_before = model_tree_fingerprint(reranker.model_path)
        reranker.bind_asset_fingerprint(reranker_fingerprint_before["sha256"])
        probe = embedder.get_query_embedding("wenjin-model-api-identity-probe")
        dimensions = len(probe)
        norm = math.sqrt(sum(float(item) ** 2 for item in probe))
        if dimensions < 1 or not math.isfinite(norm) or abs(norm - 1.0) > 0.01:
            raise RuntimeError("Embedding输出未通过向量契约校验")
        embedding_fingerprint = model_tree_fingerprint(embedding_path)
        if embedding_fingerprint != embedding_fingerprint_before:
            raise RuntimeError("Embedding模型资产在加载期间发生变化")
        if reranker_fingerprint_before != model_tree_fingerprint(reranker.model_path):
            raise RuntimeError("Reranker模型资产在服务启动期间发生变化")
        identity = {
            "protocol": PROTOCOL,
            "implementation_sha256": _implementation_fingerprint(),
            "embedding": {
                "model_id": settings.resolved_embedding_model_id,
                "asset_sha256": embedding_fingerprint["sha256"],
                "dimensions": dimensions,
                "normalized": True,
                "input_protocol": "raw-text-v1",
            },
            "reranker": {
                "model_id": settings.resolved_reranker_model_id,
                "asset_sha256": reranker_fingerprint_before["sha256"],
                "max_length": reranker.max_length,
                "batch_size": reranker.batch_size,
            },
            "runtime": {
                "device": reranker.device,
                "reranker_precision_policy": (
                    "float16" if str(reranker.device).startswith("cuda") else "float32"
                ),
                "embedding_dtype_policy": "backend_default_not_explicitly_pinned",
                "inference_dependencies": {
                    package: package_version(package)
                    for package in (
                        "torch",
                        "transformers",
                        "sentence-transformers",
                        "llama-index-embeddings-huggingface",
                    )
                },
            },
            "limits": {
                "max_batch_size": _MAX_BATCH_SIZE,
                "max_text_chars": _MAX_TEXT_CHARS,
                "max_documents": _MAX_DOCUMENTS,
            },
            "admission": {
                "active": 1,
                "max_waiting": int(
                    getattr(settings, "model_api_max_waiting", _DEFAULT_MAX_WAITING)
                ),
                "max_wait_seconds": float(
                    getattr(settings, "model_api_max_wait_seconds", _DEFAULT_MAX_WAIT_SECONDS)
                ),
            },
        }
        runtime = ModelAPIRuntime(embedder=embedder, reranker=reranker, identity=identity)
        runtime.token = token
        return runtime
    except BaseException:
        if reranker is not None:
            reranker.close()
        close_embedding = getattr(embedder, "close", None) if embedder is not None else None
        if callable(close_embedding):
            close_embedding()
        raise


def create_model_api_app(
    *, root: Path, settings: Any, token: str, runtime: ModelAPIRuntime | None = None
) -> Starlette:
    """构造可注入合成runtime的HTTP应用；正常服务通过lifespan拥有模型。"""
    if not token or len(token) < 24:
        raise ValueError("WENJIN_MODEL_API_TOKEN必须至少24个字符")
    max_waiting = getattr(settings, "model_api_max_waiting", _DEFAULT_MAX_WAITING)
    max_wait_seconds = getattr(settings, "model_api_max_wait_seconds", _DEFAULT_MAX_WAIT_SECONDS)
    if (
        type(max_waiting) is not int
        or not 0 <= max_waiting <= 128
        or type(max_wait_seconds) not in {int, float}
        or not math.isfinite(max_wait_seconds)
        or not 0 < max_wait_seconds <= 300
    ):
        raise ValueError("模型API准入队列配置无效")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        owned = runtime is None
        app.state.model_runtime = runtime or await run_in_threadpool(
            _build_runtime, Path(root), settings, token
        )
        app.state.inference_admission = ModelAdmission(
            max_waiting=max_waiting, max_wait_seconds=float(max_wait_seconds)
        )
        try:
            yield
        finally:
            # 先停止队列并等待已启动线程结束，再关闭它们正在使用的模型对象。
            await app.state.inference_admission.close()
            if owned:
                await run_in_threadpool(app.state.model_runtime.close)

    async def authenticate(request: Request) -> bool:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            return False
        authorization = values[0]
        scheme, separator, supplied = authorization.partition(" ")
        return bool(
            separator and scheme.lower() == "bearer" and hmac.compare_digest(supplied, token)
        )

    async def body_json(request: Request) -> dict[str, Any] | None:
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > _MAX_BODY_BYTES:
                    return None
            except ValueError:
                return None
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > _MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        return value if isinstance(value, dict) else None

    async def auth_then(request: Request, handler):
        if not await authenticate(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await handler(request)
        except ModelAdmissionError as exc:
            detail = {
                "model_queue_full": "模型服务等待队列已满，请稍后重试",
                "model_queue_timeout": "模型服务排队超过等待时限，请稍后重试",
                "model_service_shutting_down": "模型服务正在关闭，请稍后重试",
            }.get(exc.code, "模型服务暂不可用，请稍后重试")
            return JSONResponse(
                {"error": exc.code, "detail": detail, "evidence_status": "not_retrieved"},
                status_code=503,
            )
        except _ClientDisconnected:
            return JSONResponse({"error": "client_disconnected"}, status_code=499)
        except Exception:
            return JSONResponse({"error": "model_request_failed"}, status_code=503)

    async def admitted(request: Request, operation, *args, **kwargs):
        """监视ASGI disconnect；Starlette通常不会因http.disconnect取消handler task。"""
        task = asyncio.create_task(app.state.inference_admission.submit(operation, *args, **kwargs))
        try:
            while not task.done():
                if await request.is_disconnected():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise _ClientDisconnected
                done, _ = await asyncio.wait({task}, timeout=0.05)
                if done:
                    break
            return await task
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def health(request: Request):
        if not await authenticate(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"protocol": PROTOCOL, "status": "ok"})

    async def identity(request: Request):
        if not await authenticate(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(app.state.model_runtime.identity)

    async def embeddings(request: Request):
        async def execute(_request: Request):
            value = await body_json(request)
            if (
                value is None
                or value.get("protocol") != PROTOCOL
                or value.get("model_id") != settings.resolved_embedding_model_id
                or value.get("input_kind") not in {"query", "document"}
            ):
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            if (
                value.get("asset_sha256")
                != app.state.model_runtime.identity["embedding"]["asset_sha256"]
            ):
                return JSONResponse({"error": "model_identity_mismatch"}, status_code=409)
            if (
                value.get("implementation_sha256")
                != app.state.model_runtime.identity["implementation_sha256"]
            ):
                return JSONResponse({"error": "model_identity_mismatch"}, status_code=409)
            inputs = value.get("inputs")
            if (
                not isinstance(inputs, list)
                or not inputs
                or len(inputs) > _MAX_BATCH_SIZE
                or any(
                    not isinstance(item, str) or not item or len(item) > _MAX_TEXT_CHARS
                    for item in inputs
                )
            ):
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            vectors = await admitted(
                request, app.state.model_runtime.embed, inputs, value["input_kind"]
            )
            dimensions = app.state.model_runtime.identity["embedding"]["dimensions"]
            if len(vectors) != len(inputs) or any(
                len(vector) != dimensions
                or not all(math.isfinite(x) for x in vector)
                or abs(math.sqrt(sum(x * x for x in vector)) - 1.0) > 0.01
                for vector in vectors
            ):
                return JSONResponse({"error": "model_output_invalid"}, status_code=503)
            return JSONResponse(
                {
                    "protocol": PROTOCOL,
                    "model_id": settings.resolved_embedding_model_id,
                    "asset_sha256": app.state.model_runtime.identity["embedding"]["asset_sha256"],
                    "implementation_sha256": app.state.model_runtime.identity[
                        "implementation_sha256"
                    ],
                    "input_kind": value["input_kind"],
                    "dimensions": dimensions,
                    "normalized": True,
                    "data": vectors,
                }
            )

        return await auth_then(request, execute)

    async def rerank(request: Request):
        async def execute(_request: Request):
            value = await body_json(request)
            if (
                value is None
                or value.get("protocol") != PROTOCOL
                or value.get("model_id") != settings.resolved_reranker_model_id
            ):
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            if (
                value.get("asset_sha256")
                != app.state.model_runtime.identity["reranker"]["asset_sha256"]
            ):
                return JSONResponse({"error": "model_identity_mismatch"}, status_code=409)
            if (
                value.get("implementation_sha256")
                != app.state.model_runtime.identity["implementation_sha256"]
            ):
                return JSONResponse({"error": "model_identity_mismatch"}, status_code=409)
            query, documents = value.get("query"), value.get("documents")
            max_length, batch_size = value.get("max_length"), value.get("batch_size")
            if (
                not isinstance(query, str)
                or not query
                or len(query) > _MAX_TEXT_CHARS
                or not isinstance(documents, list)
                or not documents
                or len(documents) > _MAX_DOCUMENTS
                or any(
                    not isinstance(doc, str) or not doc or len(doc) > _MAX_TEXT_CHARS
                    for doc in documents
                )
                or type(max_length) is not int
                or type(batch_size) is not int
            ):
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            scores = await admitted(
                request,
                app.state.model_runtime.rerank_scores,
                query,
                documents,
                max_length=max_length,
                batch_size=batch_size,
            )
            if len(scores) != len(documents) or not all(math.isfinite(score) for score in scores):
                return JSONResponse({"error": "model_output_invalid"}, status_code=503)
            return JSONResponse(
                {
                    "protocol": PROTOCOL,
                    "model_id": settings.resolved_reranker_model_id,
                    "asset_sha256": app.state.model_runtime.identity["reranker"]["asset_sha256"],
                    "implementation_sha256": app.state.model_runtime.identity[
                        "implementation_sha256"
                    ],
                    "scores": scores,
                }
            )

        return await auth_then(request, execute)

    app = Starlette(
        routes=[
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/identity", identity, methods=["GET"]),
            Route("/v1/embeddings", embeddings, methods=["POST"]),
            Route("/v1/rerank", rerank, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.model_runtime = runtime
    app.state.inference_admission = None
    return app
