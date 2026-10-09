"""Temporary model API + personal-library HTTP stand-in for CLI runtime checks."""

from __future__ import annotations

import socket
from collections import Counter

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from wenjin.model_api.server import ModelAPIRuntime, create_model_api_app
from wenjin.models.providers import PROTOCOL
from wenjin.settings import Settings

TOKEN = "temporary-synthetic-model-api-token-2026"
EMBED_ID = "synthetic-marker-embedder-v1"
RERANK_ID = "synthetic-marker-reranker-v1"
COUNTS: Counter[str] = Counter()
AUTH_OBSERVED = False


class SyntheticEmbedder:
    def get_query_embedding(self, text: str) -> list[float]:
        return [1.0] + [0.0] * 7

    def get_text_embedding_batch(self, texts, *, show_progress=False):
        del show_progress
        return [[1.0] + [0.0] * 7 for _ in texts]


class SyntheticReranker:
    max_length = 1024
    batch_size = 2

    def score(self, query: str, documents: list[str]) -> list[float]:
        return [1.0 if query in document else 0.0 for document in documents]

    def close(self):
        pass


def _identity() -> dict:
    return {
        "protocol": PROTOCOL,
        "implementation_sha256": "c" * 64,
        "embedding": {
            "model_id": EMBED_ID, "asset_sha256": "a" * 64,
            "dimensions": 8, "normalized": True, "input_protocol": "raw-text-v1",
        },
        "reranker": {
            "model_id": RERANK_ID, "asset_sha256": "b" * 64,
            "max_length": 1024, "batch_size": 2,
        },
        "limits": {"max_batch_size": 128, "max_text_chars": 100_000,
                   "max_documents": 128},
    }


async def _libraries(request: Request):
    global AUTH_OBSERVED
    authorization = request.headers.get("authorization", "")
    AUTH_OBSERVED = AUTH_OBSERVED or authorization.startswith("Bearer wjm_")
    COUNTS["management_libraries"] += 1
    return JSONResponse({"libraries": [{"library_id": "osg"}]})


async def _status(request: Request):
    del request
    return JSONResponse({"counts": dict(COUNTS), "personalAuthorizationObserved": AUTH_OBSERVED})


async def _upstream_probe(request: Request):
    return JSONResponse({"syntheticUpstream": request.url.path})


def main() -> None:
    runtime = ModelAPIRuntime(
        embedder=SyntheticEmbedder(), reranker=SyntheticReranker(), identity=_identity()
    )
    settings = Settings(
        _env_file=None,
        qdrant_api_key="synthetic-only",
        model_provider="local",
        embedding_model_id=EMBED_ID,
        reranker_model_id=RERANK_ID,
    )
    app = create_model_api_app(
        root=__import__("pathlib").Path("/tmp/model-api-synthetic"),
        settings=settings,
        token=TOKEN,
        runtime=runtime,
    )
    app.routes.extend([
        Route("/api/mcp/libraries", _libraries, methods=["GET"]),
        Route("/status", _status, methods=["GET"]),
        Route("/mcp", _upstream_probe, methods=["GET", "POST"]),
        Route("/mcp/{path:path}", _upstream_probe, methods=["GET", "POST"]),
        Route("/files/{path:path}", _upstream_probe, methods=["GET"]),
        Route("/admin/", _upstream_probe, methods=["GET"]),
    ])

    async def count_requests(request, call_next):
        COUNTS[request.url.path] += 1
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=count_requests)

    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 8000))
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
