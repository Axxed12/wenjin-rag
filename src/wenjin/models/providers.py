"""本地与远程模型适配器。在线remote调用只依赖httpx，不导入torch/HF模型包。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

PROTOCOL = "wenjin-model-api/1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_ADMISSION_ERROR_CODES = {
    "model_queue_full",
    "model_queue_timeout",
    "model_service_shutting_down",
}


class ModelProviderError(RuntimeError):
    """不携带底层URL、凭据、query或模型路径的稳定provider错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def model_provider_error_detail(code: str) -> str:
    """返回可安全外发的稳定模型错误说明，不附带URL、查询或模型路径。"""
    return {
        "model_queue_full": "模型服务等待队列已满，请稍后重试",
        "model_queue_timeout": "模型服务排队超过等待时限，请稍后重试",
        "model_service_shutting_down": "模型服务正在关闭，请稍后重试",
        "model_provider_read_timeout": "模型服务读取超时，请稍后重试",
        "model_client_budget_too_small": "模型客户端读取预算小于服务端排队预算",
    }.get(code, "模型服务暂不可用，请稍后重试")


class RemoteModelClient:
    """固定身份、严格校验数据形状的模型API客户端。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        expected_embedding_model: str,
        expected_reranker_model: str,
        timeout_seconds: float = 90.0,
        connect_timeout_seconds: float = 5.0,
        pool_timeout_seconds: float = 5.0,
        ca_bundle: str | None = None,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url or not token or not expected_embedding_model or not expected_reranker_model:
            raise ModelProviderError("model_provider_configuration_invalid")
        try:
            parsed_url = urlsplit(base_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError
            _ = parsed_url.port
        except ValueError:
            raise ModelProviderError("model_provider_configuration_invalid") from None
        if (
            not 0 < timeout_seconds <= 300
            or not 0 < connect_timeout_seconds <= 60
            or not 0 < pool_timeout_seconds <= 60
        ):
            raise ModelProviderError("model_provider_configuration_invalid")
        try:
            if ca_bundle:
                import ssl

                tls_verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=ca_bundle)
            else:
                tls_verify = True
        except Exception:
            raise ModelProviderError("model_provider_ca_invalid") from None
        try:
            self._client = client or httpx.Client(
                base_url=base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {token}"},
                # 推理读取预算与建连、连接池等待分开配置，避免误把其中一种超时当总SLA。
                timeout=httpx.Timeout(
                    timeout_seconds, connect=connect_timeout_seconds, pool=pool_timeout_seconds
                ),
                verify=tls_verify,
                trust_env=False,
                transport=transport,
            )
        except Exception:
            raise ModelProviderError("model_provider_configuration_invalid") from None
        self._owns_client = client is None
        self._closed = False
        self.embedding_model_id = expected_embedding_model
        self.reranker_model_id = expected_reranker_model
        try:
            self.identity = self._get_identity()
            if not _SHA256.fullmatch(str(self.identity["implementation_sha256"])):
                raise ModelProviderError("model_identity_invalid")
            embedding = self.identity["embedding"]
            reranker = self.identity["reranker"]
            if embedding["model_id"] != expected_embedding_model:
                raise ModelProviderError("model_identity_mismatch")
            if reranker["model_id"] != expected_reranker_model:
                raise ModelProviderError("model_identity_mismatch")
            if embedding["input_protocol"] != "raw-text-v1" or embedding["normalized"] is not True:
                raise ModelProviderError("model_identity_invalid")
            if not _SHA256.fullmatch(embedding["asset_sha256"]):
                raise ModelProviderError("model_identity_invalid")
            if not _SHA256.fullmatch(reranker["asset_sha256"]):
                raise ModelProviderError("model_identity_invalid")
            if not 1 <= embedding["dimensions"] <= 65536:
                raise ModelProviderError("model_identity_invalid")
            limits = self.identity["limits"]
            if not all(
                1 <= limits[key] <= maximum
                for key, maximum in (
                    ("max_batch_size", 512),
                    ("max_text_chars", 1_000_000),
                    ("max_documents", 512),
                )
            ):
                raise ModelProviderError("model_identity_invalid")
        except BaseException:
            self.close()
            raise

    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        if self._closed:
            raise ModelProviderError("model_provider_closed")
        try:
            chunks: list[bytes] = []
            received = 0
            with self._client.stream(method, path, json=json_body) as response:
                if response.status_code != 200:
                    error_bytes = bytearray()
                    for chunk in response.iter_bytes():
                        error_bytes.extend(chunk[: max(0, 16_384 - len(error_bytes))])
                        if len(error_bytes) >= 16_384:
                            break
                    if response.status_code == 503:
                        try:
                            error_body = json.loads(error_bytes)
                        except Exception:
                            error_body = None
                        code = error_body.get("error") if isinstance(error_body, dict) else None
                        if code in _ADMISSION_ERROR_CODES:
                            raise ModelProviderError(code)
                    raise ModelProviderError("model_provider_unavailable")
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise ModelProviderError("model_provider_response_invalid")
                    chunks.append(chunk)
            value = json.loads(b"".join(chunks))
        except ModelProviderError:
            raise
        except httpx.ReadTimeout:
            raise ModelProviderError("model_provider_read_timeout") from None
        except Exception:
            raise ModelProviderError("model_provider_unavailable") from None
        if not isinstance(value, dict):
            raise ModelProviderError("model_provider_response_invalid")
        return value

    def _get_identity(self) -> dict[str, Any]:
        identity = self._request("GET", "/v1/identity")
        if identity.get("protocol") != PROTOCOL:
            raise ModelProviderError("model_protocol_mismatch")
        try:
            embedding = identity["embedding"]
            reranker = identity["reranker"]
            limits = identity["limits"]
            if (
                not isinstance(embedding, dict)
                or not isinstance(reranker, dict)
                or not isinstance(limits, dict)
            ):
                raise ValueError
            if not isinstance(embedding["model_id"], str) or not isinstance(
                reranker["model_id"], str
            ):
                raise ValueError
            if type(embedding["dimensions"]) is not int:
                raise ValueError
            if type(embedding["normalized"]) is not bool:
                raise ValueError
            if not isinstance(embedding["asset_sha256"], str) or not isinstance(
                reranker["asset_sha256"], str
            ):
                raise ValueError
            if embedding["input_protocol"] != "raw-text-v1":
                raise ValueError
            if not isinstance(identity.get("implementation_sha256"), str):
                raise ValueError
            for key in ("max_batch_size", "max_text_chars", "max_documents"):
                if type(limits[key]) is not int:
                    raise ValueError
            admission = identity["admission"]
            if (
                not isinstance(admission, dict)
                or type(admission.get("active")) is not int
                or type(admission.get("max_waiting")) is not int
                or type(admission.get("max_wait_seconds")) not in {int, float}
                or admission["active"] != 1
                or admission["max_waiting"] < 0
                or not 0 < float(admission["max_wait_seconds"]) <= 300
            ):
                raise ValueError
            if self._client.timeout.read is None or self._client.timeout.read <= float(
                admission["max_wait_seconds"]
            ):
                raise ModelProviderError("model_client_budget_too_small")
            if type(reranker["max_length"]) is not int or type(reranker["batch_size"]) is not int:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ModelProviderError("model_identity_invalid") from None
        return identity

    @property
    def embedding_dimensions(self) -> int:
        return int(self.identity["embedding"]["dimensions"])

    def embed(self, inputs: list[str], *, input_kind: str) -> list[list[float]]:
        if input_kind not in {"query", "document"}:
            raise ModelProviderError("model_request_invalid")
        limits = self.identity["limits"]
        if not isinstance(inputs, list) or len(inputs) > limits["max_batch_size"]:
            raise ModelProviderError("model_request_invalid")
        if any(
            not isinstance(text, str) or not text or len(text) > limits["max_text_chars"]
            for text in inputs
        ):
            raise ModelProviderError("model_request_invalid")
        if not inputs:
            return []
        body = {
            "protocol": PROTOCOL,
            "model_id": self.embedding_model_id,
            "asset_sha256": self.identity["embedding"]["asset_sha256"],
            "implementation_sha256": self.identity["implementation_sha256"],
            "input_kind": input_kind,
            "inputs": inputs,
        }
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ModelProviderError("model_request_invalid")
        value = self._request("POST", "/v1/embeddings", json_body=body)
        if (
            value.get("protocol") != PROTOCOL
            or value.get("model_id") != self.embedding_model_id
            or value.get("input_kind") != input_kind
            or value.get("asset_sha256") != self.identity["embedding"]["asset_sha256"]
            or value.get("implementation_sha256") != self.identity["implementation_sha256"]
            or value.get("dimensions") != self.embedding_dimensions
            or value.get("normalized") is not True
        ):
            raise ModelProviderError("model_provider_response_invalid")
        vectors = value.get("data")
        if not isinstance(vectors, list) or len(vectors) != len(inputs):
            raise ModelProviderError("model_provider_response_invalid")
        checked: list[list[float]] = []
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self.embedding_dimensions:
                raise ModelProviderError("model_provider_response_invalid")
            try:
                numeric = [float(component) for component in vector]
            except (TypeError, ValueError, OverflowError):
                raise ModelProviderError("model_provider_response_invalid") from None
            if not all(math.isfinite(component) for component in numeric):
                raise ModelProviderError("model_provider_response_invalid")
            norm = math.sqrt(sum(component * component for component in numeric))
            if not 0.99 <= norm <= 1.01:
                raise ModelProviderError("model_provider_response_invalid")
            checked.append(numeric)
        return checked

    def rerank(
        self, query: str, documents: list[str], *, max_length: int, batch_size: int
    ) -> list[float]:
        limits = self.identity["limits"]
        if (
            not isinstance(query, str)
            or not query
            or len(query) > limits["max_text_chars"]
            or not isinstance(documents, list)
            or not documents
            or len(documents) > limits["max_documents"]
            or any(
                not isinstance(doc, str) or not doc or len(doc) > limits["max_text_chars"]
                for doc in documents
            )
        ):
            raise ModelProviderError("model_request_invalid")
        body = {
            "protocol": PROTOCOL,
            "model_id": self.reranker_model_id,
            "asset_sha256": self.identity["reranker"]["asset_sha256"],
            "implementation_sha256": self.identity["implementation_sha256"],
            "query": query,
            "documents": documents,
            "max_length": int(max_length),
            "batch_size": int(batch_size),
        }
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ModelProviderError("model_request_invalid")
        value = self._request("POST", "/v1/rerank", json_body=body)
        if (
            value.get("protocol") != PROTOCOL
            or value.get("model_id") != self.reranker_model_id
            or value.get("asset_sha256") != self.identity["reranker"]["asset_sha256"]
        ):
            raise ModelProviderError("model_provider_response_invalid")
        if value.get("implementation_sha256") != self.identity["implementation_sha256"]:
            raise ModelProviderError("model_provider_response_invalid")
        scores = value.get("scores")
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise ModelProviderError("model_provider_response_invalid")
        try:
            result = [float(score) for score in scores]
        except (TypeError, ValueError, OverflowError):
            raise ModelProviderError("model_provider_response_invalid") from None
        if not all(math.isfinite(score) for score in result):
            raise ModelProviderError("model_provider_response_invalid")
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()


class RemoteEmbedding:
    def __init__(self, client: RemoteModelClient) -> None:
        self.client = client
        self.model_id = client.embedding_model_id
        self.dimensions = client.embedding_dimensions

    def get_query_embedding(self, query: str) -> list[float]:
        return self.client.embed([query], input_kind="query")[0]

    def get_text_embedding(self, text: str) -> list[float]:
        return self.client.embed([text], input_kind="document")[0]

    def get_text_embedding_batch(
        self, texts: list[str], *, show_progress: bool = False
    ) -> list[list[float]]:
        del show_progress
        result: list[list[float]] = []
        limit = self.client.identity["limits"]["max_batch_size"]
        for start in range(0, len(texts), limit):
            result.extend(self.client.embed(texts[start : start + limit], input_kind="document"))
        return result


class RemoteReranker:
    def __init__(self, client: RemoteModelClient, *, max_length: int, batch_size: int) -> None:
        self.client = client
        self.model_id = client.reranker_model_id
        self.max_length = int(max_length)
        self.batch_size = int(batch_size)
        self.device = "remote"

    def score(self, query: str, documents: list[str]) -> list[float]:
        return self.client.rerank(
            query, documents, max_length=self.max_length, batch_size=self.batch_size
        )

    def close(self) -> None:
        # RemoteModelClient由ModelBindings这个唯一owner关闭。
        return None


@dataclass
class ModelBindings:
    embedder: Any
    reranker: Any
    identity: dict[str, Any]
    _close: Any

    def close(self) -> None:
        close = self._close
        self._close = lambda: None
        close()


def build_remote_model_bindings(settings: Any, *, root=None) -> ModelBindings:
    """连接时校验远端身份；任何错误都会失败，不回退到本地模型。"""
    if getattr(settings, "model_provider", "local") != "remote":
        raise ModelProviderError("model_provider_configuration_invalid")
    validate = getattr(settings, "validate_model_provider", None)
    if callable(validate):
        validate()
    token_value = getattr(settings, "model_api_token", None)
    token = (
        token_value.get_secret_value()
        if hasattr(token_value, "get_secret_value")
        else str(token_value or "")
    )
    ca_value = getattr(settings, "model_api_ca_bundle", None)
    ca_path = None
    ca_sha256 = None
    if ca_value:
        from pathlib import Path

        try:
            ca_path = Path(ca_value)
            if not ca_path.is_absolute() and root is not None:
                ca_path = Path(root).resolve() / ca_path
            ca_path = ca_path.resolve(strict=True)
            if not ca_path.is_file():
                raise ValueError
            ca_sha256 = hashlib.sha256(ca_path.read_bytes()).hexdigest()
        except Exception:
            raise ModelProviderError("model_provider_ca_invalid") from None
    client = RemoteModelClient(
        str(getattr(settings, "model_api_url", "") or ""),
        token,
        expected_embedding_model=str(settings.embedding_model_id or ""),
        expected_reranker_model=str(settings.reranker_model_id or ""),
        timeout_seconds=float(getattr(settings, "model_api_timeout_seconds", 90.0)),
        connect_timeout_seconds=float(getattr(settings, "model_api_connect_timeout_seconds", 5.0)),
        pool_timeout_seconds=float(getattr(settings, "model_api_pool_timeout_seconds", 5.0)),
        ca_bundle=str(ca_path) if ca_path is not None else None,
    )
    if client.identity["reranker"]["max_length"] != int(
        getattr(settings, "reranker_max_length", 1024)
    ) or client.identity["reranker"]["batch_size"] != int(
        getattr(settings, "reranker_batch_size", 2)
    ):
        client.close()
        raise ModelProviderError("model_reranker_controls_mismatch")
    identity = {
        "provider": "remote",
        "protocol": PROTOCOL,
        "implementation_sha256": client.identity["implementation_sha256"],
        "base_url_sha256": hashlib.sha256(
            str(settings.model_api_url).rstrip("/").encode("utf-8")
        ).hexdigest(),
        "ca_bundle_sha256": ca_sha256,
        "embedding": dict(client.identity["embedding"]),
        "reranker": dict(client.identity["reranker"]),
        "runtime": dict(client.identity.get("runtime", {})),
        "limits": dict(client.identity["limits"]),
    }
    return ModelBindings(
        embedder=RemoteEmbedding(client),
        reranker=RemoteReranker(
            client,
            max_length=int(getattr(settings, "reranker_max_length", 1024)),
            batch_size=int(getattr(settings, "reranker_batch_size", 2)),
        )
        if getattr(settings, "search_reranker_enabled", True)
        else None,
        identity=identity,
        _close=client.close,
    )
