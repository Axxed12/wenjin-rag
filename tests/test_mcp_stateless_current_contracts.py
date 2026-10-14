"""Compatibility contracts for the isolated MCP 2026-07-28 adapter."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

try:
    if version("fastmcp").split(".", 1)[0] != "4" or version("mcp").split(".", 1)[0] != "2":
        pytest.skip("stateless contracts require FastMCP 4.x and MCP SDK 2.x", allow_module_level=True)
except PackageNotFoundError:
    pytest.skip("stateless contracts require FastMCP 4.x and MCP SDK 2.x", allow_module_level=True)

from fastmcp.tools import ToolResult
from starlette.testclient import TestClient

from wenjin.access import AccessPolicy, AccessRegistry
from wenjin.audit import SearchAudit
from wenjin.identity.store import IdentityStore
from wenjin.mcp.managed_client import ManagedFileServiceError, _bridge_timeout_from_environment
from wenjin.mcp.runtime import RuntimeIdentity
from wenjin.mcp.stateless_identity import StatelessIdentityStore
from wenjin.mcp.stateless_server import build_stateless_mcp_server, create_stateless_app
from wenjin.mcp.stateless_tools import (
    STATELESS_DOWNLOAD_PREFIX,
    STATELESS_PROTOCOL_VERSION,
    _adapt_tool_result,
)
from wenjin.settings import Settings

pytestmark = pytest.mark.integration
PROTOCOL = STATELESS_PROTOCOL_VERSION
TOKEN = "synthetic-only-stateless-token-2026"


def _typed_tool_result(payload: dict, *, is_error: bool) -> ToolResult:
    return ToolResult(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        structured_content=payload,
        is_error=is_error,
    )


def test_typed_success_result_translates_protocol_and_download_paths():
    payload = {
        "mcp_protocol": "2025-11-25",
        "evidence": [
            {"download_path": "/files/shared/osg/source?release_id=release-a"}
        ],
    }

    adapted = _adapt_tool_result(_typed_tool_result(payload, is_error=False))

    expected = {
        "mcp_protocol": STATELESS_PROTOCOL_VERSION,
        "evidence": [
            {"download_path": f"{STATELESS_DOWNLOAD_PREFIX}/shared/osg/source?release_id=release-a"}
        ],
    }
    assert adapted.is_error is False
    assert adapted.structured_content == expected
    assert json.loads(adapted.content[0].text) == expected


@pytest.mark.parametrize(
    "code,detail",
    [
        ("model_queue_full", "模型服务等待队列已满，请稍后重试"),
        ("model_queue_timeout", "模型服务排队超过等待时限，请稍后重试"),
        ("management_bridge_read_timeout", "管理服务读取超时，请稍后重试"),
    ],
)
def test_typed_service_errors_remain_mcp_errors_with_safe_structured_detail(code, detail):
    payload = {
        "error": code,
        "detail": detail,
        "query": "synthetic only",
        "evidence": [],
        "evidence_status": "not_retrieved",
        "answer_status": "not_generated",
        "mcp_protocol": "2025-11-25",
    }

    adapted = _adapt_tool_result(_typed_tool_result(payload, is_error=True))

    assert adapted.is_error is True
    assert adapted.structured_content["error"] == code
    assert adapted.structured_content["detail"] == detail
    assert adapted.structured_content["evidence"] == []
    assert adapted.structured_content["evidence_status"] == "not_retrieved"
    assert adapted.structured_content["mcp_protocol"] == STATELESS_PROTOCOL_VERSION
    assert json.loads(adapted.content[0].text) == adapted.structured_content


@pytest.mark.parametrize("code", ["model_queue_full", "model_queue_timeout"])
def test_stateless_search_handler_preserves_typed_provider_errors_on_http_wire(tmp_path, code):
    from wenjin.models.providers import ModelProviderError

    class FailingSearchService:
        supported_version = "synthetic-v1"
        index_target_id = "synthetic-target-v1"
        search_policy = None
        reranker = None

        def search(self, query, *, config):
            del query, config
            raise ModelProviderError(code)

    corpus_id = "synthetic-corpus-v1"
    server = build_stateless_mcp_server(
        service=FailingSearchService(),
        identity=RuntimeIdentity(
            corpus_id=corpus_id,
            corpus_id_source="test-fixture",
            target_id="synthetic-target-v1",
            embedding_model="synthetic-only",
        ),
        audit=SearchAudit(stream=io.StringIO()),
        access=AccessRegistry(
            None, {corpus_id: AccessPolicy(corpus_id, "public", ("*",))}
        ),
        project_root=tmp_path,
    )
    app = create_stateless_app(
        server=server,
        api_key=TOKEN,
        client_id="synthetic-client",
        allowed_hosts=["testserver"],
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 701,
        "method": "tools/call",
        "params": {
            "name": "search_docs",
            "arguments": {"query": "synthetic-only query"},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "search_docs",
    }

    with TestClient(app) as client:
        response = client.post("/mcp-stateless", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    rpc = response.json()
    assert rpc["id"] == 701
    result = rpc["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == code
    assert result["structuredContent"]["evidence_status"] == "not_retrieved"
    assert result["structuredContent"]["mcp_protocol"] == PROTOCOL
    assert result["structuredContent"]["detail"]


def test_stateless_identity_adapter_is_query_only_without_initializing_or_mutating(tmp_path):
    db_path = (tmp_path / "identity.sqlite").resolve()
    writer = IdentityStore(db_path)
    writer.initialize()
    with writer._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    reader = StatelessIdentityStore(db_path)
    with pytest.raises(PermissionError, match="只读"):
        reader.initialize()

    # Keep a normal IdentityStore writer open so SQLite must coordinate with
    # live WAL/SHM sidecars while the stateless connection reads the committed page.
    with writer._connect() as wal_writer:
        wal_writer.execute("CREATE TABLE wal_probe (value TEXT)")
        wal_writer.execute("INSERT INTO wal_probe (value) VALUES ('synthetic')")
        assert Path(f"{db_path}-wal").is_file()
        assert Path(f"{db_path}-shm").is_file()
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        with reader._connect() as connection:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            assert connection.execute("SELECT value FROM wal_probe").fetchone()[0] == "synthetic"
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
                connection.execute("CREATE TABLE stateless_write_probe (value TEXT)")
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_stateless_remote_budget_configuration_preserves_queue_and_bridge_order(monkeypatch):
    values = {
        "QDRANT_API_KEY": "synthetic-only-qdrant-key",
        "WENJIN_MODEL_PROVIDER": "remote",
        "WENJIN_MODEL_API_URL": "https://synthetic-model.invalid",
        "WENJIN_MODEL_API_TOKEN": "synthetic-only-token",
        "WENJIN_MODEL_API_TIMEOUT_SECONDS": "90",
        "WENJIN_MODEL_API_CONNECT_TIMEOUT_SECONDS": "5",
        "WENJIN_MODEL_API_POOL_TIMEOUT_SECONDS": "5",
        "WENJIN_MODEL_API_MAX_WAITING": "3",
        "WENJIN_MODEL_API_MAX_WAIT_SECONDS": "17",
        "EMBEDDING_MODEL_ID": "synthetic-embedding",
        "RERANKER_MODEL_ID": "synthetic-reranker",
        "WENJIN_FILE_MANAGEMENT_READ_TIMEOUT_SECONDS": "210",
        "WENJIN_FILE_MANAGEMENT_CONNECT_TIMEOUT_SECONDS": "5",
        "WENJIN_FILE_MANAGEMENT_POOL_TIMEOUT_SECONDS": "5",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)
    bridge_timeout = _bridge_timeout_from_environment()

    assert settings.model_api_max_waiting == 3
    assert settings.model_api_max_wait_seconds == 17
    assert settings.model_api_timeout_seconds == 90
    assert bridge_timeout.read == 210
    assert bridge_timeout.read > settings.model_api_timeout_seconds

    monkeypatch.setenv("WENJIN_FILE_MANAGEMENT_READ_TIMEOUT_SECONDS", "90")
    with pytest.raises(ManagedFileServiceError, match="大于模型服务读取预算"):
        _bridge_timeout_from_environment()
