"""Synthetic model API for isolated Docker delivery tests; no model files loaded."""

from __future__ import annotations

import hashlib
import json
import math
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL = "wenjin-model-api/1"
DIMENSIONS = 32
IMPLEMENTATION_SHA = "c" * 64
EMBEDDING_SHA = "a" * 64
RERANKER_SHA = "b" * 64


def embedding(text: str) -> list[float]:
    values = []
    seed = text.encode("utf-8")
    for index in range(DIMENSIONS):
        digest = hashlib.sha256(index.to_bytes(4, "big") + seed).digest()
        values.append((int.from_bytes(digest[:4], "big") / 2**31) - 1.0)
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def identity() -> dict:
    return {
        "protocol": PROTOCOL,
        "implementation_sha256": IMPLEMENTATION_SHA,
        "embedding": {
            "model_id": os.environ["EMBEDDING_MODEL_ID"],
            "asset_sha256": EMBEDDING_SHA,
            "implementation_sha256": IMPLEMENTATION_SHA,
            "dimensions": DIMENSIONS,
            "normalized": True,
            "input_protocol": "raw-text-v1",
        },
        "reranker": {
            "model_id": os.environ["RERANKER_MODEL_ID"],
            "asset_sha256": RERANKER_SHA,
            "implementation_sha256": IMPLEMENTATION_SHA,
            "max_length": 1024,
            "batch_size": 2,
        },
        "limits": {
            "max_batch_size": 64,
            "max_text_chars": 100000,
            "max_documents": 64,
        },
        "admission": {"active": 1, "max_waiting": 8, "max_wait_seconds": 30.0},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SyntheticDeliveryModel/1"

    def log_message(self, _format, *_args):
        # Never log request bodies, bearer tokens or business queries.
        return

    def _send(self, status: int, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        try:
            token = Path("/run/secrets/model_api_token").read_text(encoding="utf-8").strip()
        except OSError:
            return False
        return self.headers.get("Authorization") == f"Bearer {token}"

    def do_GET(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
        elif self.path == "/v1/health":
            self._send(200, {"protocol": PROTOCOL, "status": "ok"})
        elif self.path == "/v1/identity":
            self._send(200, identity())
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 8 * 1024 * 1024:
                raise ValueError
            body = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid_request"})
            return
        if body.get("protocol") != PROTOCOL or body.get("implementation_sha256") != IMPLEMENTATION_SHA:
            self._send(409, {"error": "model_identity_mismatch"})
            return
        if self.path == "/v1/embeddings":
            inputs = body.get("inputs")
            if (body.get("model_id") != os.environ["EMBEDDING_MODEL_ID"]
                    or body.get("asset_sha256") != EMBEDDING_SHA
                    or body.get("input_kind") not in {"query", "document"}
                    or not isinstance(inputs, list) or not inputs
                    or any(not isinstance(item, str) or not item for item in inputs)):
                self._send(400, {"error": "invalid_request"})
                return
            self._send(200, {
                "protocol": PROTOCOL,
                "model_id": os.environ["EMBEDDING_MODEL_ID"],
                "asset_sha256": EMBEDDING_SHA,
                "implementation_sha256": IMPLEMENTATION_SHA,
                "input_kind": body["input_kind"],
                "dimensions": DIMENSIONS,
                "normalized": True,
                "data": [embedding(item) for item in inputs],
            })
            return
        if self.path == "/v1/rerank":
            documents = body.get("documents")
            if (body.get("model_id") != os.environ["RERANKER_MODEL_ID"]
                    or body.get("asset_sha256") != RERANKER_SHA
                    or not isinstance(documents, list) or not documents):
                self._send(400, {"error": "invalid_request"})
                return
            query_terms = set(str(body.get("query", "")).casefold().split())
            scores = [float(len(query_terms & set(str(doc).casefold().split()))) for doc in documents]
            self._send(200, {
                "protocol": PROTOCOL,
                "model_id": os.environ["RERANKER_MODEL_ID"],
                "asset_sha256": RERANKER_SHA,
                "implementation_sha256": IMPLEMENTATION_SHA,
                "scores": scores,
            })
            return
        self._send(404, {"error": "not_found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
