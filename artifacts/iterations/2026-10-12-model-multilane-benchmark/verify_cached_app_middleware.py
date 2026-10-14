"""No-GPU app-factory and HTTP middleware contract for the cached image."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

from wenjin.model_api.server import create_model_api_app
from wenjin.models.providers import PROTOCOL


class _MockRuntime:
    identity = {
        "protocol": PROTOCOL,
        "implementation_sha256": "implementation-hash",
        "embedding": {"asset_sha256": "embedding-hash", "dimensions": 2},
        "reranker": {
            "asset_sha256": "reranker-hash",
            "max_length": 1024,
            "batch_size": 2,
        },
        "admission": {"active": 1},
    }

    def embed(self, inputs, input_kind):
        assert input_kind in {"query", "document"}
        return [[1.0, 0.0] for _ in inputs]

    def rerank_scores(self, query, documents, *, max_length, batch_size):
        assert query and max_length == 1024 and batch_size == 2
        return [0.8 - 0.1 * index for index, _ in enumerate(documents)]


async def main() -> None:
    os.environ.pop("WENJIN_MODEL_API_EXPERIMENT_ENABLE", None)
    os.environ.pop("WENJIN_MODEL_API_EXPERIMENT_BOUNDARY_PROBE", None)
    with tempfile.TemporaryDirectory(prefix="model-api-middleware-check-") as temporary:
        trace_path = Path(temporary) / "trace.jsonl"
        os.environ["WENJIN_MODEL_TRACE_PATH"] = str(trace_path)
        token = "cached-runtime-middleware-contract-token"
        settings = SimpleNamespace(
            model_api_max_waiting=2,
            model_api_max_wait_seconds=1.0,
            resolved_embedding_model_id="mock-embedding",
            resolved_reranker_model_id="mock-reranker",
        )
        runtime = _MockRuntime()
        app = create_model_api_app(
            root=Path(temporary), settings=settings, token=token, runtime=runtime
        )
        assert hasattr(app, "add_middleware")

        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/v1/health",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Experiment-Request-Id": "0123456789abcdef0123456789abcdef",
                    },
                )
                assert response.status_code == 200, response.text
                assert response.headers["x-experiment-request-id"] == "0123456789abcdef0123456789abcdef"
                embedding_response = await client.post(
                    "/v1/embeddings",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Experiment-Request-Id": "11111111111111111111111111111111",
                    },
                    json={
                        "protocol": PROTOCOL,
                        "model_id": "mock-embedding",
                        "asset_sha256": "embedding-hash",
                        "implementation_sha256": "implementation-hash",
                        "input_kind": "query",
                        "inputs": ["sample query"],
                    },
                )
                assert embedding_response.status_code == 200, embedding_response.text
                embedding_body = embedding_response.json()
                assert embedding_body["protocol"] == PROTOCOL
                assert embedding_body["data"] == [[1.0, 0.0]]
                assert embedding_body["dimensions"] == 2
                rerank_response = await client.post(
                    "/v1/rerank",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Experiment-Request-Id": "22222222222222222222222222222222",
                    },
                    json={
                        "protocol": PROTOCOL,
                        "model_id": "mock-reranker",
                        "asset_sha256": "reranker-hash",
                        "implementation_sha256": "implementation-hash",
                        "query": "sample query",
                        "documents": ["candidate one", "candidate two"],
                        "max_length": 1024,
                        "batch_size": 2,
                    },
                )
                assert rerank_response.status_code == 200, rerank_response.text
                rerank_body = rerank_response.json()
                assert rerank_body["protocol"] == PROTOCOL
                assert rerank_body["scores"] == [0.8, 0.7000000000000001]
        events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
        assert [event["event"] for event in events] == [
            "request_received",
            "request_completed",
            "request_received",
            "admission_enqueued",
            "worker_dequeued",
            "worker_start",
            "operation_enter",
            "operation_exit",
            "lane_drained",
            "request_completed",
            "request_received",
            "admission_enqueued",
            "worker_dequeued",
            "worker_start",
            "operation_enter",
            "operation_exit",
            "lane_drained",
            "request_completed",
        ]
        response_events = [event for event in events if event["event"] == "request_completed"]
        assert [event["status_code"] for event in response_events] == [200, 200, 200]
        assert [event["request_id"] for event in response_events] == [
            "0123456789abcdef0123456789abcdef",
            "11111111111111111111111111111111",
            "22222222222222222222222222222222",
        ]
        print("cached_app_factory_health_embedding_rerank_http=passed")


if __name__ == "__main__":
    asyncio.run(main())
