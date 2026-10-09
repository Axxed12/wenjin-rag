"""安全的管理 HTTP 调用日志与 MCP JSONL 审计只读投影。

持久化HTTP行不包含请求正文、query string或原始异常。MCP文件日志只按字段白名单投影，
不会把SearchAudit里的query/hash、session/client标识、source path或evidence正文暴露给管理界面。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator
from uuid import uuid4

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

_REQUEST_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_SAFE_AUDIT_EVENTS = {"mcp_tool_audit", "mcp_download_audit"}
_SAFE_MCP_TARGETS = {"search_docs", "download"}
_SAFE_RETRIEVAL_TARGET = re.compile(
    r"^(?:unknown|(?:file|index|corpus):[A-Za-z0-9][A-Za-z0-9._-]{0,127}|"
    r"routed:[A-Za-z0-9][A-Za-z0-9._-]{0,127}(?:,[A-Za-z0-9][A-Za-z0-9._-]{0,127}){0,4}(?:,\+[1-9][0-9]*)?)$"
)
_SAFE_CALL_TYPES = {"all", "http", "mcp"}
_SAFE_STATUSES = {"all", "success", "error"}
_HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}
_MCP_ERROR_TYPES = {
    "ValueError": "invalid_request",
    "PermissionError": "forbidden",
    "AccessDenied": "forbidden",
    "KeyError": "not_found",
    "FileNotFoundError": "not_found",
    "TimeoutError": "timeout",
    "OSError": "operation_failed",
}
_DOWNLOAD_ERROR_CODES = {
    "denied": "forbidden",
    "not_found": "not_found",
    "hash_mismatch": "integrity_error",
    "too_large": "payload_too_large",
    "error": "operation_failed",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _duration_ms(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(min(float(value) * 1000, 7 * 24 * 60 * 60 * 1000), 3)


def _request_id(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and _REQUEST_ID.fullmatch(value) else None


def _safe_error_code(status_code: int) -> str | None:
    if status_code < 400:
        return None
    return _HTTP_ERROR_CODES.get(status_code, "request_failed")


def _project_mcp_row(raw: Any) -> dict[str, Any] | None:
    """只认有request_id的两类真实MCP调用，并只返回管理列表需要的字段。"""
    if not isinstance(raw, dict) or raw.get("event") not in _SAFE_AUDIT_EVENTS:
        return None
    request_id = _request_id(raw.get("request_id"))
    occurred_at = _normalise_timestamp(raw.get("timestamp_utc"))
    if request_id is None or occurred_at is None:
        return None

    if raw["event"] == "mcp_tool_audit":
        operation = raw.get("tool")
        if operation not in _SAFE_MCP_TARGETS - {"download"}:
            return None
        retrieval_target = raw.get("retrieval_target")
        if not isinstance(retrieval_target, str) or not _SAFE_RETRIEVAL_TARGET.fullmatch(retrieval_target):
            retrieval_target = "unknown"
        target = "search_docs"
        raw_status = raw.get("status")
        status = "success" if raw_status == "success" else "error" if raw_status == "error" else None
        if status is None:
            return None
        error_type = raw.get("error_type")
        error_code = _MCP_ERROR_TYPES.get(error_type, "tool_failed") if status == "error" else None
        duration_ms = _duration_ms(raw.get("elapsed_seconds"))
    else:
        target = "download"
        outcome = raw.get("outcome")
        if outcome == "served":
            status, error_code = "success", None
        elif outcome in _DOWNLOAD_ERROR_CODES:
            status, error_code = "error", _DOWNLOAD_ERROR_CODES[outcome]
        else:
            return None
        duration_ms = _duration_ms(raw.get("elapsed_seconds"))

    if duration_ms is None:
        return None
    return {
        "request_id": request_id,
        "occurred_at": occurred_at,
        "type": "mcp",
        "target": target,
        "retrieval_target": retrieval_target if raw["event"] == "mcp_tool_audit" else None,
        "duration_ms": duration_ms,
        "status": status,
        "status_code": None,
        "error_code": error_code,
    }


class MCPAuditReader:
    """读取被固定到项目root内的审计JSONL，并应用字段白名单投影。"""

    def __init__(
        self,
        root: Path,
        *,
        sink: str | None = None,
        path: Path | str | None = None,
        max_file_bytes: int = 5 * 1024 * 1024,
        max_total_bytes: int = 20 * 1024 * 1024,
        max_line_bytes: int = 64 * 1024,
        max_rows: int = 20_000,
        backup_count: int = 3,
    ) -> None:
        self.root = Path(root).resolve()
        self.sink = (sink if sink is not None else os.getenv("WENJIN_AUDIT_SINK", "stdout")).strip().lower()
        configured = path if path is not None else os.getenv("WENJIN_AUDIT_PATH")
        if configured is None:
            self.path = self.root / "artifacts" / "audit" / "search_docs.jsonl"
            self.path_source = "default_artifact"
        else:
            candidate = Path(configured)
            self.path = candidate if candidate.is_absolute() else self.root / candidate
            self.path_source = "configured_file"
        if min(max_file_bytes, max_total_bytes, max_line_bytes, max_rows) < 1 or backup_count < 0:
            raise ValueError("审计文件读取上限必须为正数，backup_count不得为负数")
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_line_bytes = max_line_bytes
        self.max_rows = max_rows
        self.backup_count = backup_count

    def _unavailable(self, reason: str, message: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return [], {
            "status": "unavailable",
            "reason": reason,
            "message": message,
            "malformed_lines": 0,
            "truncated": False,
        }

    def read(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if self.sink == "stdout":
            return self._unavailable("stdout_only", "MCP审计写入stdout，当前服务没有可读取的历史文件。")
        if self.sink == "off":
            return self._unavailable("disabled", "MCP审计sink已关闭，没有可读取的历史记录。")
        if self.sink != "file":
            return self._unavailable("unsupported_sink", "当前MCP审计sink不支持历史读取。")

        try:
            resolved_root = self.root.resolve(strict=True)
            resolved_path = self.path.resolve(strict=False)
            if not resolved_path.is_relative_to(resolved_root):
                return self._unavailable("path_outside_root", "MCP审计文件必须位于项目root内。")
            candidates = [self.path]
            candidates.extend(Path(f"{self.path}.{index}") for index in range(1, self.backup_count + 1))
            existing: list[tuple[Path, int, int]] = []
            for candidate in candidates:
                if candidate.is_symlink():
                    if not candidate.resolve(strict=False).is_relative_to(resolved_root):
                        return self._unavailable("path_outside_root", "MCP审计文件必须位于项目root内。")
                if candidate.is_file():
                    stat = candidate.stat()
                    existing.append((candidate, stat.st_size, stat.st_mtime_ns))
        except PermissionError:
            return self._unavailable("permission_denied", "无权读取MCP审计文件。")
        except OSError:
            return self._unavailable("read_failed", "MCP审计文件状态不可读取。")

        if not existing:
            return self._unavailable("file_missing", "没有找到可读取的MCP审计文件。")

        existing.sort(key=lambda item: item[2], reverse=True)
        projected: list[dict[str, Any]] = []
        malformed_lines = 0
        bytes_read_total = 0
        truncated = False
        scanned_rows = 0
        try:
            for path, size, _mtime in existing:
                if bytes_read_total >= self.max_total_bytes or scanned_rows >= self.max_rows:
                    truncated = True
                    break
                resolved_file = path.resolve(strict=True)
                if not resolved_file.is_relative_to(resolved_root):
                    return self._unavailable("path_outside_root", "MCP审计文件必须位于项目root内。")
                per_file_limit = min(self.max_file_bytes, self.max_total_bytes - bytes_read_total)
                start_at = max(0, size - per_file_limit)
                if start_at > 0:
                    truncated = True
                with path.open("rb") as stream:
                    if start_at:
                        stream.seek(start_at)
                        # 尾部截取时丢弃首条残行；不把一个半截JSON误判为完整事件。
                        stream.readline(self.max_line_bytes + 1)
                    while bytes_read_total < self.max_total_bytes and scanned_rows < self.max_rows:
                        raw_line = stream.readline(self.max_line_bytes + 1)
                        if not raw_line:
                            break
                        bytes_read_total += len(raw_line)
                        if len(raw_line) > self.max_line_bytes:
                            malformed_lines += 1
                            while raw_line and not raw_line.endswith(b"\n") and bytes_read_total < self.max_total_bytes:
                                raw_line = stream.readline(min(4096, self.max_total_bytes - bytes_read_total))
                                bytes_read_total += len(raw_line)
                            continue
                        if not raw_line.strip():
                            continue
                        scanned_rows += 1
                        try:
                            raw = json.loads(raw_line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            malformed_lines += 1
                            continue
                        row = _project_mcp_row(raw)
                        if row is not None:
                            projected.append(row)
                    if stream.tell() < size:
                        truncated = True
        except PermissionError:
            return self._unavailable("permission_denied", "无权读取MCP审计文件。")
        except OSError:
            return self._unavailable("read_failed", "MCP审计文件读取失败。")

        coverage = {
            "status": "available",
            "source": self.path_source,
            "message": "仅展示文件审计中可关联request_id的search_docs与原文download调用；其他事件不计入。",
            "malformed_lines": malformed_lines,
            "truncated": truncated,
        }
        return projected, coverage


class CallLogStore:
    """HTTP日志使用独立SQLite持久化；读取时与MCP文件审计的安全投影合并。"""

    def __init__(
        self,
        root: Path,
        *,
        audit_sink: str | None = None,
        audit_path: Path | str | None = None,
        audit_limits: dict[str, int] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.base = self.root / "data" / "file-management"
        self.base.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base / "calls.sqlite"
        self._write_failures = 0
        self._metrics_lock = Lock()
        self.audit_reader = MCPAuditReader(
            self.root, sink=audit_sink, path=audit_path, **(audit_limits or {})
        )
        self._initialise()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=10)
        try:
            # 初始化也纳入finally，避免PRAGMA或row_factory设置失败时漏关连接。
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        else:
            if db.in_transaction:
                db.commit()
        finally:
            db.close()

    def _initialise(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """CREATE TABLE IF NOT EXISTS http_calls(
                    request_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    target TEXT NOT NULL,
                    retrieval_target TEXT,
                    duration_ms REAL NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success','error')),
                    status_code INTEGER NOT NULL,
                    error_code TEXT
                )"""
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(http_calls)")}
            if "retrieval_target" not in columns:
                db.execute("ALTER TABLE http_calls ADD COLUMN retrieval_target TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS http_calls_time_idx ON http_calls(occurred_at)")
            db.execute("CREATE INDEX IF NOT EXISTS http_calls_status_idx ON http_calls(status)")
            db.execute("CREATE INDEX IF NOT EXISTS http_calls_target_idx ON http_calls(target)")
            db.execute("CREATE INDEX IF NOT EXISTS http_calls_retrieval_target_idx ON http_calls(retrieval_target)")

    def record_http(
        self,
        *,
        request_id: str,
        occurred_at: str,
        method: str,
        target: str,
        duration_ms: float,
        status_code: int,
        retrieval_target: str | None = None,
    ) -> None:
        safe_id = _request_id(request_id) or uuid4().hex
        safe_timestamp = _normalise_timestamp(occurred_at) or _utc_now()
        safe_method = method.upper() if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"} else "OTHER"
        # target只允许route模板，不接受query string、实际路径参数或控制字符。
        if (
            not isinstance(target, str) or not target.startswith("/api/") or "?" in target
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
        ):
            target = "/api/unknown"
        if len(target) > 300:
            target = "/api/unknown"
        route_target = f"{safe_method} {target}"
        safe_retrieval_target = None
        if retrieval_target is not None:
            safe_retrieval_target = (
                retrieval_target if isinstance(retrieval_target, str)
                and _SAFE_RETRIEVAL_TARGET.fullmatch(retrieval_target) else "unknown"
            )
        safe_duration = _duration_ms(float(duration_ms) / 1000)
        if safe_duration is None:
            safe_duration = 0.0
        code = status_code if isinstance(status_code, int) and not isinstance(status_code, bool) else 500
        code = min(max(code, 100), 599)
        status = "success" if code < 400 else "error"
        error_code = _safe_error_code(code)
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO http_calls(
                    request_id,occurred_at,target,retrieval_target,duration_ms,status,status_code,error_code
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (safe_id, safe_timestamp, route_target, safe_retrieval_target, safe_duration, status, code, error_code),
            )

    def list_calls(
        self,
        *,
        call_type: str = "all",
        status: str = "all",
        target: str | None = None,
        retrieval_target: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if call_type not in _SAFE_CALL_TYPES:
            raise ValueError("type必须是all、http或mcp")
        if status not in _SAFE_STATUSES:
            raise ValueError("status必须是all、success或error")
        if not 1 <= limit <= 200 or not 0 <= offset <= 10_000:
            raise ValueError("分页参数超出支持范围")
        if target is not None and (
            not isinstance(target, str) or not target or len(target) > 300
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
        ):
            raise ValueError("target筛选值无效")
        if retrieval_target is not None and (
            not isinstance(retrieval_target, str) or not retrieval_target or len(retrieval_target) > 300
            or any(ord(char) < 32 or ord(char) == 127 for char in retrieval_target)
        ):
            raise ValueError("retrieval_target筛选值无效")
        http_rows: list[dict[str, Any]] = []
        if call_type in {"all", "http"}:
            clauses: list[str] = []
            params: list[Any] = []
            if status != "all":
                clauses.append("status=?")
                params.append(status)
            if target:
                clauses.append("target=?")
                params.append(target)
            if retrieval_target:
                clauses.append("retrieval_target=?")
                params.append(retrieval_target)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            with self._connect() as db:
                http_total = db.execute(f"SELECT COUNT(*) FROM http_calls{where}", params).fetchone()[0]
                # 与MCP最多20k条投影合并时，单侧取前offset+limit足以得到全局page；
                # 不把持久HTTP日志全表装入Python内存。
                http_fetch = min(offset + limit, 10_200)
                stored = db.execute(
                    f"SELECT request_id,occurred_at,target,retrieval_target,duration_ms,status,status_code,error_code "
                    f"FROM http_calls{where} ORDER BY occurred_at DESC,request_id DESC LIMIT ?",
                    [*params, http_fetch],
                ).fetchall()
            for row in stored:
                http_rows.append({
                    "request_id": row["request_id"],
                    "occurred_at": row["occurred_at"],
                    "type": "http",
                    "target": row["target"],
                    "retrieval_target": row["retrieval_target"],
                    "duration_ms": row["duration_ms"],
                    "status": row["status"],
                    "status_code": row["status_code"],
                    "error_code": row["error_code"],
                })
        else:
            http_total = 0

        mcp_rows: list[dict[str, Any]] = []
        if call_type in {"all", "mcp"}:
            mcp_rows, mcp_coverage = self.audit_reader.read()
            if status != "all":
                mcp_rows = [row for row in mcp_rows if row["status"] == status]
            if target:
                mcp_rows = [row for row in mcp_rows if row["target"] == target]
            if retrieval_target:
                mcp_rows = [row for row in mcp_rows if row["retrieval_target"] == retrieval_target]
        else:
            _empty_rows, mcp_coverage = self.audit_reader._unavailable(
                "not_requested", "本次筛选未读取MCP审计文件。"
            )

        items = http_rows + mcp_rows
        items.sort(key=lambda row: (_normalise_timestamp(row["occurred_at"]) or "", row["request_id"]), reverse=True)
        # HTTP为SQL COUNT真实值；当前可读MCP JSONL则纳入合并total。过滤source限定时另一侧为零。
        total = (http_total if call_type in {"all", "http"} else 0) + len(mcp_rows)
        with self._metrics_lock:
            write_failures = self._write_failures
        return {
            "items": items[:offset + limit][offset:offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "coverage": {
                "http": {
                    "status": "degraded" if write_failures else "active",
                    "write_failures": write_failures,
                    "message": (
                        "部分HTTP调用未能写入持久日志；请检查管理数据目录的磁盘和权限。"
                        if write_failures else
                        "持久记录匹配到的管理HTTP API调用；调用日志读取接口本身不记入历史。"
                    ),
                },
                "mcp": mcp_coverage,
            },
        }

    def close(self) -> None:
        """保留lifespan关闭接口；所有SQLite连接均在单次操作上下文中释放。"""

    def note_write_failure(self) -> None:
        """并发请求共享的进程级降级计数，不持久化异常正文。"""
        with self._metrics_lock:
            self._write_failures += 1


class CallLogMiddleware(BaseHTTPMiddleware):
    """记录管理API的请求元数据；不读取body/query，不递归记录调用日志读取API。"""

    def __init__(self, app, *, store: CallLogStore) -> None:
        super().__init__(app)
        self.store = store

    @staticmethod
    def _route_template(request: Request) -> str:
        # Starlette路由在子scope中设置route，不会回写BaseHTTPMiddleware持有的scope，
        # 所以使用应用已注册的静态route表重新做一次无I/O匹配。
        for route in request.app.routes:
            match, _child_scope = route.matches(request.scope)
            if match is Match.FULL:
                candidate = getattr(route, "path", None)
                if isinstance(candidate, str) and candidate.startswith("/api/"):
                    return candidate
        return "/api/unmatched"

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/admin/calls":
            return await call_next(request)
        request_id = uuid4().hex
        start = time.perf_counter()
        status_code = 500
        response: Response | None = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            elapsed_ms = max((time.perf_counter() - start) * 1000, 0.0)
            target = self._route_template(request)
            retrieval_target = None
            if request.method == "POST" and path in {"/api/search", "/api/admin/search"}:
                state = request.scope.get("state") or {}
                candidate = state.get("call_target") if isinstance(state, dict) else None
                retrieval_target = (
                    candidate if isinstance(candidate, str)
                    and _SAFE_RETRIEVAL_TARGET.fullmatch(candidate) else "unknown"
                )
            try:
                await run_in_threadpool(
                    self.store.record_http,
                    request_id=request_id,
                    occurred_at=_utc_now(),
                    method=request.method,
                    target=target,
                    duration_ms=elapsed_ms,
                    status_code=status_code,
                    retrieval_target=retrieval_target,
                )
            except (sqlite3.Error, OSError):
                # 持久日志写入故障不能把已经完成的管理API请求改成失败响应；coverage会标记降级。
                self.store.note_write_failure()
