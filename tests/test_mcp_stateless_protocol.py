"""Focused synthetic wire-contract tests for the 2026-07-28 endpoint."""

from __future__ import annotations

import io
import tempfile
import unittest
from importlib.metadata import PackageNotFoundError, version
from types import SimpleNamespace

import pytest

try:
    if version("fastmcp").split(".", 1)[0] != "4" or version("mcp").split(".", 1)[0] != "2":
        pytest.skip("stateless contracts require FastMCP 4.x and MCP SDK 2.x", allow_module_level=True)
except PackageNotFoundError:
    pytest.skip("stateless contracts require FastMCP 4.x and MCP SDK 2.x", allow_module_level=True)

from starlette.testclient import TestClient

from wenjin.access import AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.stateless_server import build_stateless_mcp_server, create_stateless_app

PROTOCOL = "2026-07-28"
TOKEN = "synthetic-only-stateless-token-2026"
pytestmark = pytest.mark.integration


def _request(
    client,
    *,
    method="server/discover",
    request_id=501,
    body_meta=None,
    protocol_header=PROTOCOL,
    method_header=None,
    extra_headers=None,
    remove_headers=(),
    authorization=True,
):
    params = {}
    if body_meta is not None:
        params["_meta"] = body_meta
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_header,
        "Mcp-Method": method_header or method,
    }
    if method == "tools/call":
        payload["params"].update({"name": "list_corpora", "arguments": {}})
        headers["Mcp-Name"] = "list_corpora"
    if authorization:
        headers["Authorization"] = f"Bearer {TOKEN}"
    headers.update(extra_headers or {})
    for name in remove_headers:
        headers.pop(name, None)
    return client.post("/mcp-stateless", headers=headers, json=payload)


def _error(response, *, status, code, request_id):
    assert response.status_code == status, response.text
    envelope = response.json()
    assert envelope["jsonrpc"] == "2.0"
    assert envelope["id"] == request_id
    assert envelope["error"]["code"] == code


class StatelessProtocolContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-stateless-contract-")
        from pathlib import Path

        self.root = Path(self.temp.name)
        service = SimpleNamespace(
            supported_version="synthetic-v1",
            index_target_id="synthetic-target-v1",
            reranker=None,
            search_policy=None,
        )
        server = build_stateless_mcp_server(
            service=service,
            identity=RuntimeIdentity(
                corpus_id="synthetic-corpus-v1",
                corpus_id_source="test-fixture",
                target_id="synthetic-target-v1",
                embedding_model="synthetic-only",
            ),
            audit=SearchAudit(stream=io.StringIO()),
            access=AccessRegistry(self.root / "access", {}),
            access_dir=self.root / "access",
            project_root=self.root,
            bearer_enabled=True,
        )
        app = create_stateless_app(
            server=server,
            api_key=TOKEN,
            client_id="synthetic-client",
            allowed_hosts=["testserver"],
        )
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.temp.cleanup()

    def test_missing_or_malformed_request_meta_is_invalid_params(self):
        cases = [
            (None, 601),
            ({"io.modelcontextprotocol/protocolVersion": PROTOCOL}, 602),
            ({
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": "not-an-object",
            }, 603),
            ({
                "io.modelcontextprotocol/protocolVersion": 123,
                "io.modelcontextprotocol/clientCapabilities": {},
            }, 604),
        ]
        for metadata, request_id in cases:
            with self.subTest(request_id=request_id):
                response = _request(self.client, body_meta=metadata, request_id=request_id)
                _error(response, status=400, code=-32602, request_id=request_id)

    def test_protocol_routing_headers_are_required_and_match_body(self):
        request_id = 611
        response = _request(
            self.client,
            method="tools/call",
            request_id=request_id,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            extra_headers={"Mcp-Name": "some-other-tool"},
        )
        _error(response, status=400, code=-32020, request_id=request_id)

        request_id = 613
        response = _request(
            self.client,
            request_id=request_id,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            remove_headers=("MCP-Protocol-Version",),
        )
        _error(response, status=400, code=-32020, request_id=request_id)

        request_id = 614
        response = _request(
            self.client,
            request_id=request_id,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": "2025-11-25",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        )
        _error(response, status=400, code=-32020, request_id=request_id)

        request_id = 615
        response = _request(
            self.client,
            request_id=request_id,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            remove_headers=("Mcp-Method",),
        )
        _error(response, status=400, code=-32020, request_id=request_id)

        request_id = 612
        response = _request(
            self.client,
            request_id=request_id,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
            extra_headers={"Mcp-Method": "tools/list"},
        )
        _error(response, status=400, code=-32020, request_id=request_id)

    def test_unsupported_matching_version_and_unknown_method(self):
        unsupported = "2025-11-25"
        request_id = 621
        response = _request(
            self.client,
            request_id=request_id,
            protocol_header=unsupported,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": unsupported,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        )
        _error(response, status=400, code=-32022, request_id=request_id)

        request_id = 622
        response = _request(
            self.client,
            method="unknown/method",
            request_id=request_id,
            body_meta={
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        )
        _error(response, status=404, code=-32601, request_id=request_id)

    def test_modern_endpoint_rejects_get_and_delete(self):
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                response = self.client.request(
                    method,
                    "/mcp-stateless",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.headers.get("allow"), "POST")

    def test_app_factory_fails_closed_without_identity_or_static_credential(self):
        with self.assertRaisesRegex(ValueError, "identity|credential|auth"):
            create_stateless_app(allowed_hosts=["testserver"])

    def test_legacy_session_header_is_ignored_and_auth_still_runs(self):
        metadata = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        response = _request(
            self.client,
            body_meta=metadata,
            extra_headers={"Mcp-Session-Id": "legacy-user-A-session"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("mcp-session-id", response.headers)

        denied = _request(
            self.client,
            body_meta=metadata,
            extra_headers={"Mcp-Session-Id": "legacy-user-A-session"},
            authorization=False,
        )
        self.assertEqual(denied.status_code, 401)


if __name__ == "__main__":
    unittest.main()
