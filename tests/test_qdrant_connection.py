"""真实 Qdrant HTTP client 在代理存在时的直连与显式代理行为。"""

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wenjin.qdrant_connection import create_qdrant_client

API_KEY = "unit-test-qdrant-api-key"
QDRANT_RESPONSE = json.dumps({
    "result": {"collections": []}, "status": "ok", "time": 0.001,
}).encode("utf-8")


@contextmanager
def http_endpoint():
    """启动记录请求的本机HTTP端点，不连接真实Qdrant。"""
    requests: list[dict[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append({
                "path": self.path,
                "api_key": self.headers.get("api-key"),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(QDRANT_RESPONSE)))
            self.end_headers()
            self.wfile.write(QDRANT_RESPONSE)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def install_proxy_environment(monkeypatch, proxy_url: str) -> None:
    """强制HTTPX考虑代理变量，即使本机目标地址通常会被NO_PROXY排除。"""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy_url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")


def test_default_factory_reaches_local_qdrant_with_proxy_variables_set(
    monkeypatch,
):
    with http_endpoint() as (origin_url, origin_requests):
        with http_endpoint() as (proxy_url, proxy_requests):
            install_proxy_environment(monkeypatch, proxy_url)
            client = create_qdrant_client(
                url=origin_url, api_key=API_KEY, timeout=3,
            )
            try:
                result = client.get_collections()
            finally:
                client.close()

            assert result.collections == []
            assert len(origin_requests) == 1 and proxy_requests == [], (
                "expected one origin request and no proxy request; "
                f"origin_requests={len(origin_requests)} proxy_requests={len(proxy_requests)}"
            )
            assert origin_requests[0]["api_key"] == API_KEY


def test_explicit_trust_env_true_is_honored(monkeypatch):
    with http_endpoint() as (origin_url, origin_requests):
        with http_endpoint() as (proxy_url, proxy_requests):
            install_proxy_environment(monkeypatch, proxy_url)
            client = create_qdrant_client(
                url=origin_url, api_key=API_KEY, timeout=3, trust_env=True,
            )
            try:
                result = client.get_collections()
            finally:
                client.close()

            assert result.collections == []
            assert origin_requests == []
            assert len(proxy_requests) == 1
            assert proxy_requests[0]["api_key"] == API_KEY


def test_non_url_location_argument_is_forwarded():
    client = create_qdrant_client(location=":memory:")
    try:
        assert client.get_collections().collections == []
    finally:
        client.close()
