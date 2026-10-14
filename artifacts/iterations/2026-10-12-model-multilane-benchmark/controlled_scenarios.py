"""Optional low-load contract probes for the isolated concurrency fixture.

These functions take already-authenticated in-memory clients from the runner.
They never start containers and never persist credentials. Results are suitable
for the synthetic-only run directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_error(exc: BaseException, fallback: str) -> str:
    # Only stable locally chosen labels enter artifacts; exception strings can
    # contain URLs, bearer tokens, or service-provided data.
    text = str(exc)
    if text in {"managed_replace_job_failed", "managed_delete_job_failed"}:
        return text
    return fallback if not type(exc).__name__ else f"{fallback}:{type(exc).__name__}"


def _frames(exc: BaseException) -> list[dict[str, Any]]:
    import traceback
    rows = []
    for frame in traceback.extract_tb(exc.__traceback__):
        rows.append({"file": Path(frame.filename).name, "line": frame.lineno,
                     "function": frame.name})
    return rows


def _sanitize(value: Any, key: str = "") -> Any:
    if any(term in key.lower() for term in ("token", "password", "secret", "authorization", "cookie")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(password|secret|token)(\s*[:=]\s*)[^\s,\"']+",
                       r"\1\2[REDACTED]", value)
        return value[:4000]
    return value


def _safe_rpc(value: Any, token: str) -> Any:
    value = _sanitize(value)
    if isinstance(value, dict):
        return {key: _safe_rpc(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_rpc(item, token) for item in value]
    if isinstance(value, str):
        value = value.replace(token, "[REDACTED]") if token else value
        return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)[:16000]
    return value


def _http_record(response: httpx.Response) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except (ValueError, json.JSONDecodeError):
        body = response.text[:4000]
    return {"status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body": _sanitize(body)}


def _extract_response_json(response: httpx.Response) -> dict[str, Any]:
    if "text/event-stream" not in response.headers.get("content-type", "").lower():
        value = response.json()
        return value if isinstance(value, dict) else {}
    for line in response.text.splitlines():
        if line.startswith("data:"):
            try:
                value = json.loads(line[5:].strip())
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and "jsonrpc" in value:
                return value
    raise ValueError("no_jsonrpc_envelope")


def _wait_job_record(admin: httpx.Client, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    last: dict[str, Any] = {"job_id": job_id, "status": "not_observed"}
    while time.monotonic() < deadline:
        response = admin.get(f"/api/jobs/{quote(job_id, safe='')}")
        last = {"poll": _http_record(response), "job_id": job_id}
        if response.status_code != 200:
            return last
        try:
            job = _sanitize(response.json())
        except (ValueError, json.JSONDecodeError):
            job = {"status": "invalid_json", "body": response.text[:2000]}
        last["job"] = job
        if isinstance(job, dict) and job.get("status") in {"completed", "deleted", "failed", "cancelled"}:
            return last
        time.sleep(0.1)
    last["poll_timeout"] = True
    return last


def _evidence(result: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tool = result.get("tool") if isinstance(result.get("tool"), dict) else {}
    items = tool.get("evidence", [])
    return tool, items if isinstance(items, list) else []


async def verify_collision_identity(
    *,
    shared_session: Any,
    managed_session: Any,
    shared_target_id: str,
    shared_marker: str,
    file_id: str,
    managed_version_id: str,
    managed_marker: str,
    query_shared: str,
    query_managed: str,
    output: Path,
) -> dict[str, Any]:
    """Send one shared + one managed request together and assert each result."""
    started_utc, started = utc_now(), time.perf_counter()
    shared_call = shared_session.call("tools/call", {"name": "search_docs", "arguments": {
        "query": query_shared, "target_id": shared_target_id, "top_k": 1}})
    managed_call = managed_session.call("tools/call", {"name": "search_docs", "arguments": {
        "query": query_managed, "file_id": file_id, "top_k": 1}})
    results = await asyncio.gather(shared_call, managed_call, return_exceptions=True)
    rows: list[dict[str, Any]] = []
    checks: list[bool] = []
    for link, result, marker in (("shared", results[0], shared_marker),
                                 ("managed", results[1], managed_marker)):
        if isinstance(result, BaseException):
            rows.append({"link": link, "success": False,
                         "error_category": type(result).__name__})
            checks.append(False)
            continue
        tool, evidence = _evidence(result)
        marker_ok = bool(evidence) and all(marker in str(item.get("text", "")) for item in evidence)
        if link == "shared":
            identity_ok = bool(evidence) and all(
                item.get("library") == "synthetic-concurrency"
                and item.get("version") == "1.0"
                and item.get("index_target_id") == shared_target_id for item in evidence)
        else:
            file_meta = tool.get("managed_file", {})
            identity_ok = (bool(evidence) and file_meta.get("file_id") == file_id
                           and file_meta.get("version_id") == managed_version_id
                           and all(item.get("file_id") == file_id
                                   and item.get("version_id") == managed_version_id
                                   for item in evidence))
        http_ok = result.get("http_status") == 200
        row_ok = http_ok and marker_ok and identity_ok and not tool.get("error")
        checks.append(row_ok)
        rows.append({"link": link, "http_status": result.get("http_status"),
                     "request_id": tool.get("request_id"), "evidence_count": len(evidence),
                     "expected_marker_match": marker_ok, "expected_identity_match": identity_ok,
                     "success": row_ok, "session_sha256": (shared_session if link == "shared"
                                                                  else managed_session).session_hash,
                     "evidence_identity": [{key: item.get(key) for key in
                                             ("library", "version", "index_target_id", "file_id", "version_id")
                                             if key in item} for item in evidence],
                     "server_timing_seconds": tool.get("timing_seconds", {})})
    report = {"scenario": "one-shared-and-one-managed-simultaneous-request",
              "started_utc": started_utc, "elapsed_seconds": round(time.perf_counter() - started, 6),
              "results": rows, "passed": all(checks),
              "interpretation": "single controlled collision only; no saturation or identity-concurrency claim"}
    write_json(output, report)
    return report


def run_managed_lifecycle(
    *,
    admin: httpx.Client,
    admin_headers: dict[str, str],
    mcp_base_url: str,
    library_id: str,
    file_id: str,
    user_id: str,
    current_grant_revision: int,
    query: str,
    reader_token: str,
    session_factory: Callable[..., Any],
    wait_job: Callable[[httpx.Client, str], dict[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    """Replace → verify new version → revoke/read-deny → regrant/delete probe.

    The reader MCP token remains in memory. This checks each state transition
    using real management HTTP and stateful MCP HTTP sessions.
    """
    operations: list[dict[str, Any]] = []
    errors: list[str] = []
    old_version: str | None = None
    active_version: str | None = None
    replacement = ("Synthetic managed concurrency replacement.\n"
                   "Unique marker: synthetic_managed_marker_v2.\n"
                   "Temporary benchmark data only.\n").encode("utf-8")
    async def read_file(label: str, expected_marker: str | None = None,
                        expected_version: str | None = None) -> dict[str, Any]:
        session = await session_factory(mcp_base_url, reader_token, label, 1)
        session.bind_token(reader_token)
        try:
            result = await session.call("tools/call", {"name": "search_docs", "arguments": {
                "query": query, "file_id": file_id, "top_k": 1}})
            tool, evidence = _evidence(result)
            return {"label": label, "http_status": result.get("http_status"),
                    "request_id": tool.get("request_id"), "rpc": _safe_rpc(result.get("rpc"), reader_token),
                    "transport_parse_error": result.get("transport_parse_error"),
                    "raw_response_text": result.get("raw_response_text"),
                    "tool_error": tool.get("error"), "evidence_count": len(evidence),
                    "evidence_identity": [{k: item.get(k) for k in
                        ("file_id", "version_id", "index_target_id") if k in item} for item in evidence],
                    "expected_marker_match": (all(expected_marker in str(item.get("text", ""))
                        for item in evidence) if expected_marker else None),
                    "expected_version_match": (all(item.get("version_id") == expected_version
                        for item in evidence) if expected_version else None),
                    "passed": bool(evidence) and not tool.get("error")
                        and result.get("http_status") == 200}
        finally:
            await session.close()

    async def overlapped(label: str, op_name: str, operation: Callable[[], Any],
                         expected_marker: str | None = None) -> dict[str, Any]:
        session = await session_factory(mcp_base_url, reader_token, label, 1)
        session.bind_token(reader_token)
        mcp_scheduled = utc_now()
        call_task = asyncio.create_task(session.call("tools/call", {"name": "search_docs", "arguments": {
            "query": query, "file_id": file_id, "top_k": 1}}))
        await asyncio.sleep(0.05)  # allow the MCP POST to enter flight before the control action
        action_issued = utc_now()
        op_response = None
        op_error = None
        try:
            try:
                action_response = await asyncio.to_thread(operation)
                action_response_received = utc_now()
                # Await job completion while the independent MCP request is already in flight.
                if op_name in {"replace", "delete"} and action_response.status_code == 202:
                    job_id = action_response.json().get("job_id")
                    job_record = await asyncio.to_thread(_wait_job_record, admin, job_id)
                    op_response = {"accepted": _http_record(action_response), **job_record}
                    job_value = job_record.get("job", {})
                    if not isinstance(job_value, dict) or job_value.get("status") not in {"completed", "deleted"}:
                        op_error = "managed_" + op_name + "_job_failed"
                else:
                    op_response = _http_record(action_response)
            except Exception as exc:
                action_response_received = utc_now()
                op_error = _safe_error(exc, "managed_" + op_name + "_request_failed")
            call_result = await asyncio.gather(call_task, return_exceptions=True)
            mcp_completed = utc_now()
            request_result = call_result[0]
            if isinstance(request_result, BaseException):
                request_result = {"error_code": _safe_error(request_result, "mcp_call_failed"),
                                  "trace_frames": _frames(request_result)}
            else:
                tool, evidence = _evidence(request_result)
                request_result = {"http_status": request_result.get("http_status"),
                    "request_id": tool.get("request_id"), "rpc": _safe_rpc(request_result.get("rpc"), reader_token),
                    "transport_parse_error": request_result.get("transport_parse_error"),
                    "raw_response_text": request_result.get("raw_response_text"),
                    "tool_error": tool.get("error"), "evidence_count": len(evidence),
                    "expected_marker_match": (all(expected_marker in str(item.get("text", ""))
                        for item in evidence) if expected_marker else None),
                    "evidence_identity": [{k: item.get(k) for k in
                        ("file_id", "version_id", "index_target_id") if k in item} for item in evidence]}
            return {"operation": op_name, "mcp_request_scheduled_utc": mcp_scheduled,
                    "management_action_issued_utc": action_issued,
                    "management_action_response_received_utc": action_response_received,
                    "mcp_request_completed_utc": mcp_completed,
                    "observed_overlap": ("possible" if mcp_completed >= action_issued else "no"),
                    "operation_completed_utc": utc_now(), "operation_result": op_response,
                    "operation_error_code": op_error, "overlap_request": request_result}
        finally:
            if not call_task.done():
                call_task.cancel()
                await asyncio.gather(call_task, return_exceptions=True)
            await session.close()

    def record_http(name: str, response: httpx.Response) -> dict[str, Any]:
        value = {"operation": name, **_http_record(response)}
        operations.append(value)
        return value

    try:
        old_info = admin.get(f"/api/files/{quote(file_id, safe='')}")
        record_http("initial_file_info", old_info)
        if old_info.status_code == 200:
            old_version = old_info.json().get("active_version_id")
            active_version = old_version
        else:
            errors.append("managed_initial_file_info_failed")

        # Replacement request begins an MCP read before the management transition.
        replacement_race = asyncio.run(overlapped("replace-overlap", "replace", lambda: admin.put(
            f"/api/files/{quote(file_id, safe='')}?library_id={quote(library_id, safe='')}",
            headers=admin_headers | {"Content-Type": "application/octet-stream",
                                     "X-File-Name": "managed-guide.txt"}, content=replacement),
            "synthetic_managed_marker_v2"))
        operations.append(replacement_race)
        rep = replacement_race.get("operation_result", {})
        replace_job = rep.get("job") if isinstance(rep, dict) else None
        replacement_ok = bool(replace_job and replace_job.get("status") == "completed")
        if not replacement_ok:
            errors.append("managed_replace_job_failed")
        new_info_response = admin.get(f"/api/files/{quote(file_id, safe='')}")
        record_http("replacement_file_info", new_info_response)
        if new_info_response.status_code == 200:
            candidate = new_info_response.json().get("active_version_id")
            if candidate and candidate != old_version:
                active_version = candidate
        replacement_search = asyncio.run(read_file("replace-after", "synthetic_managed_marker_v2", active_version))
        operations.append(replacement_search)
        replacement_ok = replacement_ok and replacement_search.get("passed", False) and bool(active_version and active_version != old_version)

        revoke_race = asyncio.run(overlapped("revoke-overlap", "revoke", lambda: admin.post(
            "/api/admin/grants", headers=admin_headers, json={"user_id": user_id,
            "resource_kind": "managed_file", "resource_id": file_id, "allowed": False,
            "expected_revision": current_grant_revision})))
        operations.append(revoke_race)
        revoke_response = revoke_race.get("operation_result", {})
        revoke_http_ok = revoke_response.get("status_code") == 200
        denied = asyncio.run(read_file("revoke-after"))
        operations.append(denied)
        revoke_ok = revoke_http_ok and denied.get("http_status") == 200 and denied.get("evidence_count") == 0 and bool(denied.get("tool_error"))
        if not revoke_ok:
            errors.append("managed_revoke_contract_failed")

        regrant = admin.post("/api/admin/grants", headers=admin_headers, json={"user_id": user_id,
            "resource_kind": "managed_file", "resource_id": file_id, "allowed": True,
            "expected_revision": current_grant_revision + 1})
        record_http("regrant", regrant)
        regranted = asyncio.run(read_file("regrant-after", "synthetic_managed_marker_v2", active_version))
        operations.append(regranted)
        regrant_ok = regrant.status_code == 200 and regranted.get("passed", False) and regranted.get("expected_marker_match")
        if not regrant_ok:
            errors.append("managed_regrant_contract_failed")

        delete_race = asyncio.run(overlapped("delete-overlap", "delete", lambda: admin.delete(
            f"/api/files/{quote(file_id, safe='')}", headers=admin_headers)))
        operations.append(delete_race)
        delete_response = delete_race.get("operation_result", {})
        delete_job = delete_response.get("job") if isinstance(delete_response, dict) else None
        after_delete = admin.get(f"/api/files/{quote(file_id, safe='')}")
        record_http("deleted_file_info", after_delete)
        deleted_status = after_delete.json().get("status") if after_delete.status_code == 200 else None
        post_delete = asyncio.run(read_file("delete-after"))
        operations.append(post_delete)
        delete_ok = bool(delete_job and delete_job.get("status") in {"completed", "deleted"}
            and after_delete.status_code == 200 and deleted_status == "deleted"
            and post_delete.get("http_status") == 200 and post_delete.get("evidence_count") == 0
            and bool(post_delete.get("tool_error")))
        if not delete_ok:
            errors.append("managed_delete_contract_failed")
        report = {"scenario": "managed-replace-revoke-delete", "file_id_sha256": hashlib.sha256(file_id.encode()).hexdigest(),
            "old_version_id": old_version, "active_version_after_replace": active_version,
            "replacement_sha256": hashlib.sha256(replacement).hexdigest(), "operations": operations,
            "checks": {"replacement": replacement_ok, "revoke": revoke_ok,
                       "regrant": regrant_ok, "delete": delete_ok},
            "passed": replacement_ok and revoke_ok and regrant_ok and delete_ok,
            "interpretation": "MCP requests were scheduled before management actions; observed_overlap is possible when the client request had not completed at action issue time, but server-side overlap is not proven; all inputs are synthetic; one chunk and top_k=1 cannot rule out mixed-version multi-chunk evidence"}
    except Exception as exc:
        report = {"scenario": "managed-replace-revoke-delete", "passed": False,
                  "error_code": _safe_error(exc, "managed_lifecycle_failed"),
                  "trace_frames": _frames(exc), "operations": operations}
        write_json(output_dir / "controlled-managed-lifecycle-failure.json", report)
        raise
    write_json(output_dir / "controlled-managed-lifecycle.json", report)
    if not report["passed"]:
        write_json(output_dir / "controlled-managed-lifecycle-failure.json", report)
    return report


async def probe_mcp_cancellation(
    *, base_url: str, token: str, session_factory: Callable[..., Any],
    query: str, target_id: str, expected_marker: str, cancel_after_ms: int,
    output: Path,
) -> dict[str, Any]:
    """Send one real cancelled notification and then verify same-session reuse.

    This observes protocol/client state only; synchronous downstream work may
    continue in its worker thread, so server-side work termination stays unknown.
    """
    started_utc, started = utc_now(), time.perf_counter()
    session = await session_factory(base_url, token, "cancel-probe", 1)
    request_id = session.sequence
    operation: asyncio.Task | None = None
    notification: dict[str, Any] | None = None
    call_result: dict[str, Any] | None = None
    call_state = "not_started"
    call_exception: BaseException | None = None
    closed_session_id_sha256 = session.session_hash
    try:
        operation = asyncio.create_task(session.call("tools/call", {"name": "search_docs", "arguments": {
            "query": query, "target_id": target_id, "top_k": 1}}))
        await asyncio.sleep(max(cancel_after_ms, 0) / 1000)
        sent_cancel = not operation.done()
        if sent_cancel:
            cancel_start = time.perf_counter()
            try:
                response = await session.client.post("/mcp", headers={
                    "Authorization": "Bearer " + session._token,
                    "MCP-Session-Id": session.session_id,
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                }, json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": request_id, "reason": "controlled cancellation probe"}},
                    timeout=3)
                notification = {"http_status": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "raw_response_text": response.text[:16000].replace(session._token, "[REDACTED]")}
                try:
                    notification["rpc"] = _extract_response_json(response)
                except (ValueError, json.JSONDecodeError):
                    notification["rpc_parse_error"] = "no_jsonrpc_envelope"
            except Exception as exc:
                notification = {"error_code": _safe_error(exc, "cancel_notification_failed"),
                                 "trace_frames": _frames(exc)}
            notification["elapsed_seconds"] = round(time.perf_counter() - cancel_start, 6)
        else:
            notification = {"not_sent": True, "reason": "original_request_finished_before_cancel"}
        try:
            call_result = await asyncio.wait_for(operation, timeout=5)
            call_state = "completed" if call_result.get("http_status") == 200 else "http_error"
        except asyncio.TimeoutError as exc:
            call_exception = exc
            call_state = "client_wait_timeout"
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        except BaseException as exc:
            call_exception = exc
            call_state = "call_exception"
    finally:
        if operation is not None and not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        await session.close()

    # Follow-up uses a fresh protocol session so a poisoned/cancelled original
    # session cannot hide whether the service remains usable.
    next_result = None
    next_error = None
    next_tool: dict[str, Any] = {}
    next_evidence: list[dict[str, Any]] = []
    next_ok = False
    next_session = None
    try:
        next_session = await session_factory(base_url, token, "cancel-followup", 1)
        next_result = await next_session.call("tools/call", {"name": "search_docs", "arguments": {
            "query": query, "target_id": target_id, "top_k": 1}})
        next_tool, next_evidence = _evidence(next_result)
        next_ok = (next_result.get("http_status") == 200 and not next_tool.get("error")
                   and bool(next_evidence) and all(expected_marker in str(item.get("text", ""))
                   and item.get("index_target_id") == target_id for item in next_evidence))
    except Exception as exc:
        next_error = {"error_code": _safe_error(exc, "independent_followup_failed"),
                      "trace_frames": _frames(exc)}
    finally:
        if next_session is not None:
            await next_session.close()

    def call_payload(value: dict[str, Any] | None, exc: BaseException | None) -> dict[str, Any] | None:
        if value is None:
            return ({"error_code": _safe_error(exc, "mcp_call_failed"), "trace_frames": _frames(exc)}
                    if exc else None)
        tool = value.get("tool", {})
        return {"http_status": value.get("http_status"), "content_type": value.get("content_type"),
            "request_id": value.get("request_id"), "rpc": _safe_rpc(value.get("rpc"), token),
            "transport_parse_error": value.get("transport_parse_error"),
            "raw_response_text": value.get("raw_response_text"),
            "tool_error": tool.get("error") if isinstance(tool, dict) else None,
            "evidence_count": len(tool.get("evidence", [])) if isinstance(tool, dict) else 0,
            "evidence_identity": [{k: item.get(k) for k in ("index_target_id", "file_id", "version_id") if k in item}
                                  for item in tool.get("evidence", [])] if isinstance(tool, dict) else []}
    report = {"scenario": "stateful-MCP-cancellation-probe", "cancel_after_ms": cancel_after_ms,
              "started_utc": started_utc, "elapsed_seconds": round(time.perf_counter() - started, 6),
              "request_id": request_id, "original_session_sha256": closed_session_id_sha256,
              "cancel_notification": notification, "original_call_state": call_state,
              "original_call": call_payload(call_result, call_exception),
              "independent_followup": {"http_status": next_result.get("http_status") if next_result else None,
                "request_id": next_tool.get("request_id"), "rpc": _safe_rpc(next_result.get("rpc"), token) if next_result else None,
                "raw_response_text": next_result.get("raw_response_text") if next_result else None,
                "tool_error": next_tool.get("error"), "evidence_count": len(next_evidence),
                "expected_marker_match": bool(next_evidence) and all(expected_marker in str(item.get("text", ""))
                    for item in next_evidence), "expected_identity_match": bool(next_evidence) and all(
                    item.get("index_target_id") == target_id for item in next_evidence),
                "passed": next_ok, "error": next_error},
              "server_work_stopped": "unknown",
              "interpretation": "original session is always closed; the follow-up uses a distinct initialized MCP session; client cancellation does not prove synchronous worker/model termination"}
    write_json(output, report)
    return report
