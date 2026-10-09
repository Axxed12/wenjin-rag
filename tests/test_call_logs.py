"""管理调用日志脱敏投影、分页、轮转和文件边界。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from wenjin.audit import SearchAudit
from wenjin.file_management.api import create_app
from wenjin.file_management.call_logs import CallLogStore as LegacyCallLogStore
from wenjin.management_http.call_logs import CallLogMiddleware, CallLogStore, MCPAuditReader


def test_old_call_log_import_reexports_the_canonical_management_http_types():
    assert LegacyCallLogStore is CallLogStore


def _mcp_row(request_id: str, *, target="search_docs", status="success", timestamp=None) -> dict:
    return {
        "timestamp_utc": timestamp or datetime.now(timezone.utc).isoformat(),
        "event": "mcp_tool_audit",
        "request_id": request_id,
        "tool": target,
        "retrieval_target": "index:alpha-test",
        "status": status,
        "error_type": "ValueError" if status == "error" else None,
        "elapsed_seconds": 0.0125,
        "query": "TOP SECRET QUERY",
        "query_sha256": "sensitive-query-hash",
        "session_id_sha256": "sensitive-session-hash",
        "client_id_sha256": "sensitive-client-hash",
        "source_paths": ["private/source.txt"],
        "returned_chunk_ids": ["secret-chunk"],
        "rawerror": "secret exception text",
        "body": "secret evidence body",
    }


def _write_jsonl(path: Path, rows: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False))
            stream.write("\n")


def test_http_calls_persist_filter_and_paginate_without_sensitive_values(tmp_path):
    store = CallLogStore(tmp_path, audit_sink="off")
    now = datetime.now(timezone.utc)
    ids = [uuid4().hex for _ in range(3)]
    store.record_http(
        request_id=ids[0], occurred_at=now.isoformat(), method="GET",
        target="/api/files/{file_id:str}", duration_ms=1.25, status_code=200,
        retrieval_target="file:0123456789abcdef0123456789abcdef",
    )
    store.record_http(
        request_id=ids[1], occurred_at=(now + timedelta(seconds=1)).isoformat(), method="DELETE",
        target="/api/files/{file_id:str}", duration_ms=2.5, status_code=409,
    )
    # 不接受实际URL/query进入持久日志。
    store.record_http(
        request_id=ids[2], occurred_at=(now + timedelta(seconds=2)).isoformat(), method="POST",
        target="/api/search?query=confidential", duration_ms=3, status_code=422,
    )
    store.close()

    reopened = CallLogStore(tmp_path, audit_sink="off")
    all_calls = reopened.list_calls(limit=2)
    assert all_calls["total"] == 3
    assert len(all_calls["items"]) == 2
    assert all_calls["items"][0]["request_id"] == ids[2]
    assert all_calls["items"][0]["target"] == "POST /api/unknown"
    assert all_calls["items"][1]["request_id"] == ids[1]
    assert all_calls["items"][1]["type"] == "http"
    assert all_calls["items"][1]["status"] == "error"
    assert all_calls["items"][1]["status_code"] == 409
    assert all_calls["items"][1]["error_code"] == "conflict"
    assert all_calls["items"][1]["retrieval_target"] is None
    assert reopened.list_calls(call_type="http", status="success")["total"] == 1
    assert reopened.list_calls(call_type="http", target="GET /api/files/{file_id:str}")["total"] == 1
    assert reopened.list_calls(
        call_type="http", retrieval_target="file:0123456789abcdef0123456789abcdef"
    )["total"] == 1
    assert reopened.list_calls(offset=99)["items"] == []
    with pytest.raises(ValueError):
        reopened.list_calls(offset=10_001)
    with pytest.raises(ValueError):
        reopened.list_calls(retrieval_target="private\nvalue")
    assert set(all_calls["items"][0]) == {
        "request_id", "occurred_at", "type", "target", "retrieval_target",
        "duration_ms", "status", "status_code", "error_code"
    }


def test_middleware_uses_route_template_safe_state_target_and_skips_self(tmp_path):
    store = CallLogStore(tmp_path, audit_sink="off")

    async def search(request: Request):
        payload = await request.json()
        request.state.call_target = "file:0123456789abcdef0123456789abcdef"
        return JSONResponse({"accepted": bool(payload)})

    async def config(_request: Request):
        return JSONResponse({"ready": True})

    async def logs(_request: Request):
        return JSONResponse(store.list_calls(call_type="http"))

    app = Starlette(routes=[
        Route("/api/search", search, methods=["POST"]),
        Route("/api/admin/config", config, methods=["GET"]),
        Route("/api/admin/calls", logs, methods=["GET"]),
    ])
    app.add_middleware(CallLogMiddleware, store=store)
    with TestClient(app) as client:
        response = client.post(
            "/api/search?query=private-query",
            json={"query": "private-body"},
        )
        assert response.status_code == 200
        assert len(response.headers["X-Request-ID"]) == 32
        client.get("/api/admin/config")
        first_logs = client.get("/api/admin/calls").json()
        count_after_first_read = first_logs["total"]
        client.get("/api/admin/calls")
        assert store.list_calls(call_type="http")["total"] == count_after_first_read == 2
    rows = store.list_calls(call_type="http")["items"]
    search_row = next(row for row in rows if row["target"] == "POST /api/search")
    assert search_row["retrieval_target"] == "file:0123456789abcdef0123456789abcdef"
    assert "private-query" not in json.dumps(rows)
    assert "private-body" not in json.dumps(rows)


def test_admin_calls_api_is_bearer_protected_and_filters_without_recursing(tmp_path):
    class MinimalManager:
        def resume_pending_jobs(self):
            return []

        def close(self):
            return None

    app = create_app(tmp_path, manager=MinimalManager(), admin_token="isolated-fake-token")
    target = "file:0123456789abcdef0123456789abcdef"
    app.state.call_logs.record_http(
        request_id=uuid4().hex, occurred_at=datetime.now(timezone.utc).isoformat(),
        method="POST", target="/api/admin/search", retrieval_target=target,
        duration_ms=1, status_code=200,
    )

    with TestClient(app) as client:
        denied = client.get("/api/admin/calls")
        assert denied.status_code == 401
        response = client.get(
            "/api/admin/calls?type=http&retrieval_target=file%3A0123456789abcdef0123456789abcdef",
            headers={"Authorization": "Bearer isolated-fake-token"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["items"][0]["retrieval_target"] == target
        # Calls API本身和拒绝请求都不会反过来污染持久记录。
        assert app.state.call_logs.list_calls(call_type="http")["total"] == 1


def test_http_log_storage_failure_does_not_fail_api_and_marks_coverage(tmp_path, monkeypatch):
    store = CallLogStore(tmp_path, audit_sink="off")

    async def endpoint(_request: Request):
        return JSONResponse({"ready": True})

    app = Starlette(routes=[Route("/api/ready", endpoint, methods=["GET"])])
    app.add_middleware(CallLogMiddleware, store=store)

    def fail_write(**_kwargs):
        raise PermissionError("private storage path")

    monkeypatch.setattr(store, "record_http", fail_write)
    with TestClient(app) as client:
        response = client.get("/api/ready")

    assert response.status_code == 200
    coverage = store.list_calls(call_type="http")["coverage"]["http"]
    assert coverage["status"] == "degraded"
    assert coverage["write_failures"] == 1
    assert "private storage path" not in json.dumps(coverage)


def test_mcp_jsonl_is_allowlist_projected_and_dropped_events_are_not_calls(tmp_path):
    root = tmp_path.resolve()
    path = root / "artifacts" / "audit" / "search_docs.jsonl"
    valid = _mcp_row(uuid4().hex)
    bad_id = _mcp_row("not-a-request-id")
    unknown_tool = _mcp_row(uuid4().hex, target="list_corpora")
    download = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "event": "mcp_download_audit",
        "request_id": uuid4().hex,
        "outcome": "denied",
        "elapsed_seconds": 0.02,
        "relative_path": "secret/private.pdf",
        "client_id_sha256": "secret-client-hash",
        "detail": "raw private diagnostic",
        "bytes_served": 900,
    }
    dropped = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "event": "mcp_evidence_dropped",
        "details": [{"source_path": "secret/path"}],
    }
    _write_jsonl(path, [valid, bad_id, unknown_tool, download, dropped, "{bad-json"])
    store = CallLogStore(root, audit_sink="file")
    page = store.list_calls(call_type="mcp", limit=50)
    assert page["total"] == 2
    assert {row["target"] for row in page["items"]} == {"search_docs", "download"}
    assert all(set(row) == {
        "request_id", "occurred_at", "type", "target", "retrieval_target",
        "duration_ms", "status", "status_code", "error_code"
    } for row in page["items"])
    download_row = next(row for row in page["items"] if row["target"] == "download")
    assert download_row["status"] == "error" and download_row["error_code"] == "forbidden"
    assert download_row["retrieval_target"] is None
    assert next(row for row in page["items"] if row["target"] == "search_docs")["retrieval_target"] == "index:alpha-test"
    assert page["coverage"]["mcp"]["malformed_lines"] == 1
    assert page["coverage"]["mcp"]["status"] == "available"
    serialized = json.dumps(page, ensure_ascii=False)
    for secret in (
        "TOP SECRET QUERY", "sensitive-query-hash", "sensitive-session-hash",
        "sensitive-client-hash", "private/source.txt", "secret-chunk",
        "secret exception text", "secret evidence body", "private.pdf", "raw private diagnostic",
    ):
        assert secret not in serialized


def test_legacy_mcp_search_without_target_is_explicitly_unknown(tmp_path):
    root = tmp_path.resolve()
    path = root / "artifacts" / "audit" / "search_docs.jsonl"
    legacy_success = _mcp_row(uuid4().hex)
    legacy_success.pop("retrieval_target")
    failed_without_result = _mcp_row(uuid4().hex, status="error")
    failed_without_result.pop("retrieval_target")
    _write_jsonl(path, [legacy_success, failed_without_result])

    page = CallLogStore(root, audit_sink="file").list_calls(call_type="mcp")

    assert page["total"] == 2
    assert {row["retrieval_target"] for row in page["items"]} == {"unknown"}
    assert {row["status"] for row in page["items"]} == {"success", "error"}


def test_real_search_audit_shape_projects_only_safe_fields(tmp_path):
    path = tmp_path / "artifacts" / "audit" / "search_docs.jsonl"
    audit = SearchAudit(path, include_query=True)
    request_id = audit.record(
        query="never expose this question", version="3.6.5", top_k=5,
        context_char_budget=8000, session_id="raw-session", client_id="raw-client",
        elapsed_seconds=0.04,
        result={
            "evidence": [{"chunk_id": "private-chunk", "source_path": "private/file.md", "text": "private body"}],
            "index_target_id": "ogc-tiles-v003",
            "retrieval": {"timing_seconds": {"dense": 0.01}},
        },
    )
    audit.record_dropped_evidence_details(
        query="no request id so it is not a call", client_id="raw-client",
        dropped=[{"source_path": "private/dropped.md"}],
    )
    audit.close()
    page = CallLogStore(tmp_path, audit_sink="file").list_calls(call_type="mcp")
    assert page["total"] == 1
    assert page["items"][0]["request_id"] == request_id
    assert page["items"][0]["target"] == "search_docs"
    assert page["items"][0]["retrieval_target"] == "index:ogc-tiles-v003"
    output = json.dumps(page, ensure_ascii=False)
    for secret in ("never expose", "private-chunk", "private/file.md", "private body", "raw-session", "raw-client"):
        assert secret not in output


def test_rotation_files_are_merged_newest_first(tmp_path):
    root = tmp_path.resolve()
    path = root / "artifacts" / "audit" / "search_docs.jsonl"
    audit = SearchAudit(path, include_query=False)
    evidence = [{"chunk_id": f"id-{index:04d}", "source_path": "safe/path/" + ("x" * 48)} for index in range(600)]
    ids = []
    for _ in range(140):
        ids.append(audit.record(
            query="q", version="3.6.5", top_k=5, context_char_budget=8000,
            session_id=None, elapsed_seconds=0.01,
            result={"evidence": evidence, "retrieval": {"timing_seconds": {}}},
        ))
    audit.close()
    assert Path(f"{path}.1").is_file() or Path(f"{path}.2").is_file()
    page = CallLogStore(
        root, audit_sink="file", audit_limits={"max_line_bytes": 150_000}
    ).list_calls(call_type="mcp", limit=200)
    assert page["total"] == 140
    timestamps = [row["occurred_at"] for row in page["items"]]
    assert timestamps[0] == max(timestamps)
    assert timestamps[-1] == min(timestamps)
    assert {row["request_id"] for row in page["items"]} == set(ids)
    assert page["coverage"]["mcp"]["malformed_lines"] == 0


@pytest.mark.parametrize(
    ("sink", "expected"),
    [("stdout", "stdout_only"), ("off", "disabled"), ("rotating", "unsupported_sink")],
)
def test_non_file_sink_reports_no_readable_history(tmp_path, sink, expected):
    page = CallLogStore(tmp_path, audit_sink=sink).list_calls(call_type="mcp")
    assert page["total"] == 0
    coverage = page["coverage"]["mcp"]
    assert coverage["status"] == "unavailable"
    assert coverage["reason"] == expected
    assert coverage["message"]


def test_missing_outside_root_and_permission_failures_are_safe(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    missing = CallLogStore(root, audit_sink="file").list_calls(call_type="mcp")
    assert missing["coverage"]["mcp"]["reason"] == "file_missing"

    outside = tmp_path.parent / f"external-{uuid4().hex}.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    outside_page = CallLogStore(root, audit_sink="file", audit_path=outside).list_calls(call_type="mcp")
    assert outside_page["coverage"]["mcp"]["reason"] == "path_outside_root"

    inside = root / "artifacts" / "audit" / "search_docs.jsonl"
    _write_jsonl(inside, [_mcp_row(uuid4().hex)])
    original_open = Path.open

    def denied_open(path, *args, **kwargs):
        if path == inside:
            raise PermissionError("secret absolute path and OS diagnostic")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    denied = CallLogStore(root, audit_sink="file").list_calls(call_type="mcp")
    assert denied["coverage"]["mcp"]["reason"] == "permission_denied"
    assert "secret absolute path" not in json.dumps(denied)


def test_reader_caps_bytes_rows_and_line_size(tmp_path):
    root = tmp_path.resolve()
    path = root / "audit.jsonl"
    rows = [_mcp_row(uuid4().hex) for _ in range(4)]
    _write_jsonl(path, rows)
    reader = MCPAuditReader(
        root, sink="file", path=path, max_file_bytes=1024 * 1024,
        max_total_bytes=1024 * 1024, max_line_bytes=1024, max_rows=2,
    )
    found, coverage = reader.read()
    assert len(found) == 2
    assert coverage["truncated"] is True

    # 超长单行在有限读取预算内被计为坏行，不尝试解析或返回其内容。
    path.write_text(json.dumps(_mcp_row(uuid4().hex)) + "x" * 5000 + "\n", encoding="utf-8")
    found, coverage = MCPAuditReader(root, sink="file", path=path, max_line_bytes=512).read()
    assert found == []
    assert coverage["malformed_lines"] == 1
