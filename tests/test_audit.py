import json
from io import StringIO

from wenjin.audit import SearchAudit, digest


def test_default_audit_records_metadata_without_query_or_session():
    stream = StringIO()
    audit = SearchAudit(stream=stream)
    result = {
        "evidence": [{"chunk_id": "c1", "source_path": "include/osg/Node"}],
        "retrieval": {"timing_seconds": {"dense": 0.2}},
    }
    request_id = audit.record(
        query="敏感 Query", version="3.6.5", top_k=5,
        context_char_budget=8000, session_id="session-secret",
        client_id="claude-client",
        elapsed_seconds=0.3, result=result,
    )
    audit.close()
    raw = stream.getvalue()
    row = json.loads(raw)
    assert row["event"] == "mcp_tool_audit"
    assert row["service"] == "wenjin-rag"
    assert row["request_id"] == request_id
    assert row["query_sha256"] == digest("敏感 Query")
    assert row["session_id_sha256"] == digest("session-secret")
    assert row["client_id_sha256"] == digest("claude-client")
    assert row["returned_chunk_ids"] == ["c1"]
    assert "敏感 Query" not in raw
    assert "session-secret" not in raw


def test_error_audit_records_type_without_exception_message(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = SearchAudit(path, include_query=True)
    audit.record(
        query="public", version="4.0", top_k=5,
        context_char_budget=8000, session_id=None,
        elapsed_seconds=0.01, error=ValueError("secret in message"),
    )
    audit.close()
    raw = path.read_text(encoding="utf-8")
    row = json.loads(raw)
    assert row["status"] == "error"
    assert row["error_type"] == "ValueError"
    assert row["query"] == "public"
    assert "secret in message" not in raw


def test_disabled_audit_returns_request_id_without_output():
    stream = StringIO()
    audit = SearchAudit(stream=stream, enabled=False)
    request_id = audit.record(
        query="q", version="3.6.5", top_k=5, context_char_budget=8000,
        session_id=None, elapsed_seconds=0.1, result={"evidence": []},
    )
    audit.close()
    assert len(request_id) == 32
    assert stream.getvalue() == ""


def test_audit_target_is_safe_and_backward_compatible():
    stream = StringIO()
    audit = SearchAudit(stream=stream)
    audit.record(
        query="private", version="3.6.5", top_k=5, context_char_budget=8000,
        session_id=None, elapsed_seconds=0.01,
        result={"managed_file": {"file_id": "0123456789abcdef0123456789abcdef"}},
    )
    audit.record(
        query="private", version="routed", top_k=5, context_char_budget=8000,
        session_id=None, elapsed_seconds=0.01,
        result={"routed_libraries": [
            {"target_id": "osg-3.6.5-active"}, {"target_id": "tiles-003"},
            {"target_id": "unsafe/private/path"},
        ]},
    )
    audit.record(
        query="private", version="unknown", top_k=5, context_char_budget=8000,
        session_id=None, elapsed_seconds=0.01,
        result={"index_target_id": "../private/source"},
    )
    audit.record(
        query="private", version="unknown", top_k=5, context_char_budget=8000,
        session_id=None, elapsed_seconds=0.01, error=ValueError("must not be logged"),
    )
    audit.close()
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert rows[0]["retrieval_target"] == "file:0123456789abcdef0123456789abcdef"
    assert rows[1]["retrieval_target"] == "routed:osg-3.6.5-active,tiles-003"
    assert rows[2]["retrieval_target"] == "unknown"
    assert rows[3]["retrieval_target"] == "unknown"
    assert "private" not in stream.getvalue()
    assert "unsafe/private/path" not in stream.getvalue()
    assert "must not be logged" not in stream.getvalue()


def test_configured_relative_audit_path_is_rooted_at_project(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    unrelated_cwd = tmp_path / "other-working-directory"
    project_root.mkdir()
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setenv("WENJIN_AUDIT_SINK", "file")
    monkeypatch.setenv("WENJIN_AUDIT_PATH", "artifacts/custom-audit.jsonl")

    audit = SearchAudit.from_environment(project_root)
    audit.record(
        query="query", version="3.6.5", top_k=5, context_char_budget=8000,
        session_id=None, elapsed_seconds=0.01,
    )
    audit.close()

    expected = project_root / "artifacts" / "custom-audit.jsonl"
    assert expected.is_file()
    assert not (unrelated_cwd / "artifacts" / "custom-audit.jsonl").exists()
