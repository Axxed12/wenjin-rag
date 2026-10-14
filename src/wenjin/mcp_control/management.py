"""管理端MCP连接配置和broker客户端。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

ENTRY_INFO = {
    "legacy": {
        "name": "旧版状态化 MCP",
        "protocol": "2025-11-25",
        "transport": "Streamable HTTP",
        "default_url": "http://127.0.0.1:18765/mcp",
        "requirements": [
            "连接时调用 initialize 并完成 notifications/initialized",
            "协商客户端能力，服务端可能签发 MCP-Session-Id",
            "发送请求时保留服务端返回的会话标识",
        ],
    },
    "stateless": {
        "name": "新版无状态 MCP",
        "protocol": "2026-07-28",
        "transport": "Streamable HTTP",
        "default_url": "http://127.0.0.1:18768/mcp-stateless",
        "requirements": [
            "支持 server/discover 能力发现",
            "每个工具调用按协议发送请求级 _meta",
            "不依赖 MCP-Session-Id 延续请求状态",
        ],
    },
}
_ID_RE = re.compile(r"^[a-z0-9-]{1,64}$")
_ACTIONS = {"start", "stop"}
ERROR_DETAILS = {
    "mcp_container_control_not_configured": "Docker MCP容器控制尚未配置。",
    "mcp_control_unavailable": "Docker MCP控制服务暂不可用。",
    "mcp_entry_not_found": "MCP入口不存在。",
    "mcp_revision_invalid": "配置版本号无效，请刷新入口列表。",
    "mcp_revision_conflict": "默认入口已被其他管理员修改，请刷新后重试。",
    "mcp_action_invalid": "操作仅支持启动或停止。",
    "mcp_generation_invalid": "容器版本标识无效，请刷新入口列表。",
    "mcp_generation_conflict": "MCP容器已被替换，请刷新入口状态后重试。",
    "mcp_operation_not_found": "操作记录不存在或标识无效。",
    "mcp_operation_expired": "操作记录已过期；请刷新容器状态后再操作。",
    "mcp_action_in_progress": "该入口已有操作正在进行。",
    "mcp_action_outcome_unknown": "上次操作结果尚不确定；刷新容器状态以完成复核。",
    "mcp_container_not_found": "未找到受管MCP容器；原生Python进程不能由此接口控制。",
    "mcp_container_ambiguous": "匹配到多个MCP容器，请检查Compose项目和服务实例。",
    "mcp_container_labels_changed": "MCP容器标签与受管目标不符，操作已拒绝。",
    "mcp_container_state_conflict": "当前容器状态不支持该操作，请刷新状态后重试。",
    "mcp_action_not_applied": "未能确认容器达到目标状态。",
    "mcp_action_state_unconfirmed": "Docker已接受请求，但目标状态尚未确认；刷新状态完成复核。",
    "mcp_operation_capacity_reached": "操作记录已满，请稍后重试。",
    "docker_engine_unavailable": "Docker Engine暂不可用。",
    "docker_engine_invalid_response": "Docker Engine返回了无法识别的状态。",
    "docker_engine_action_failed": "Docker Engine未能完成容器操作。",
    "mcp_control_internal_error": "容器控制操作失败。",
    "mcp_settings_unavailable": "默认入口配置暂不可用。",
    "mcp_control_url_invalid": "内部MCP容器控制服务地址无效。",
    "mcp_public_url_invalid": "MCP公开地址配置无效。",
    "mcp_request_invalid": "请求体必须是合法的JSON对象。",
}
RUNTIME_LABELS = {
    "created": "已创建，尚未启动",
    "running": "运行中；不代表MCP协议可用",
    "paused": "已暂停",
    "restarting": "正在重启",
    "removing": "正在移除",
    "exited": "已停止",
    "dead": "异常退出",
    "unknown": "状态未知",
    "unmanaged": "未受Docker管理",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class MCPControlError(RuntimeError):
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.code = code
        self.status = status


def _public_url(entry_id: str) -> str:
    variable = "WENJIN_MCP_" + ("LEGACY" if entry_id == "legacy" else "STATELESS") + "_PUBLIC_URL"
    value = os.environ.get(variable, ENTRY_INFO[entry_id]["default_url"])
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise MCPControlError("mcp_public_url_invalid", 503) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\r" in value
        or "\n" in value
    ):
        raise MCPControlError("mcp_public_url_invalid", 503)
    if port is not None and not 1 <= port <= 65535:
        raise MCPControlError("mcp_public_url_invalid", 503)
    return urllib.parse.urlunsplit(parsed)


class MCPControlService:
    """用稳定entry ID隔离配置文件与内部容器控制broker。"""

    def __init__(self, root: Path):
        self.path = Path(root).resolve() / "data" / "file-management" / "mcp-control" / "settings.sqlite"

    def _read(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as connection:
                with connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL)"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO settings(key,value,revision) VALUES('default_entry','legacy',0)"
                    )
                    row = connection.execute(
                        "SELECT value,revision FROM settings WHERE key='default_entry'"
                    ).fetchone()
            if row is None or row[0] not in ENTRY_INFO or not isinstance(row[1], int):
                raise ValueError("invalid settings")
            return {"default_entry": row[0], "revision": row[1]}
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise MCPControlError("mcp_settings_unavailable", 503) from exc

    def _write(self, data: dict, expected_revision: int) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path, timeout=5, isolation_level="IMMEDIATE")) as connection:
                with connection:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL)"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO settings(key,value,revision) VALUES('default_entry','legacy',0)"
                    )
                    cursor = connection.execute(
                        "UPDATE settings SET value=?,revision=? WHERE key='default_entry' AND revision=?",
                        (data["default_entry"], data["revision"], expected_revision),
                    )
                    return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise MCPControlError("mcp_settings_unavailable", 503) from exc

    @staticmethod
    def _entry_id(value: str) -> str:
        if value not in ENTRY_INFO:
            raise MCPControlError("mcp_entry_not_found", 404)
        return value

    def connection_config(self, entry_id: str | None = None) -> dict:
        if entry_id is None:
            entry_id = self._read()["default_entry"]
        entry_id = self._entry_id(entry_id)
        info = ENTRY_INFO[entry_id]
        return {
            "entry_id": entry_id,
            "name": info["name"],
            "protocol": info["protocol"],
            "transport": info["transport"],
            "endpoint": _public_url(entry_id),
            "auth": {"scheme": "Bearer", "credential_source": "管理页另行配置的MCP调用凭据"},
            "requirements": info["requirements"],
        }

    def _broker_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        base = os.environ.get("WENJIN_MCP_CONTROL_URL", "").rstrip("/")
        token = os.environ.get("WENJIN_MCP_CONTROL_TOKEN", "")
        if not base or not token:
            raise MCPControlError("mcp_container_control_not_configured", 503)
        try:
            parsed = urllib.parse.urlsplit(base)
            port = parsed.port
        except ValueError as exc:
            raise MCPControlError("mcp_control_url_invalid", 503) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or "\r" in base
            or "\n" in base
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise MCPControlError("mcp_control_url_invalid", 503)
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirectHandler()
            )
            with opener.open(request, timeout=60 if method == "POST" else 5) as response:
                body = json.loads(response.read(64 * 1024).decode("utf-8"))
                return body if isinstance(body, dict) else {}
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read(16 * 1024).decode("utf-8"))
                code = body.get("error", "mcp_control_unavailable")
            except Exception:
                code = "mcp_control_unavailable"
            raise MCPControlError(code, exc.code if exc.code in {404, 409, 410, 422, 503} else 503) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise MCPControlError("mcp_control_unavailable", 503) from exc

    def entries(self) -> dict:
        settings = self._read()
        control_configured = bool(
            os.environ.get("WENJIN_MCP_CONTROL_URL") and os.environ.get("WENJIN_MCP_CONTROL_TOKEN")
        )
        runtime: dict[str, dict] = {}
        if control_configured:
            try:
                response = self._broker_request("GET", "/v1/entries")
                for row in response.get("entries", []):
                    if isinstance(row, dict) and row.get("entry_id") in ENTRY_INFO:
                        runtime[row["entry_id"]] = row
            except MCPControlError as exc:
                runtime = {
                    entry_id: {
                        "runtime_state": "unmanaged",
                        "generation": None,
                        "control_error": exc.code,
                    }
                    for entry_id in ENTRY_INFO
                }
        result = []
        for entry_id, info in ENTRY_INFO.items():
            state = runtime.get(entry_id, {})
            result.append({
                "entry_id": entry_id,
                "name": info["name"],
                "protocol": info["protocol"],
                "endpoint": _public_url(entry_id),
                "is_default": settings["default_entry"] == entry_id,
                "runtime_state": state.get("runtime_state", "unmanaged"),
                "runtime_label": RUNTIME_LABELS.get(state.get("runtime_state", "unmanaged"), "状态未知"),
                "generation": state.get("generation"),
                "control_error": state.get("control_error"),
                "control_error_detail": ERROR_DETAILS.get(state.get("control_error")),
                "control_configured": control_configured,
            })
        return {"entries": result, "default_entry": settings["default_entry"], "revision": settings["revision"]}

    def set_default(self, entry_id: str, expected_revision: int) -> dict:
        entry_id = self._entry_id(entry_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise MCPControlError("mcp_revision_invalid", 422)
        current = self._read()
        if current["revision"] != expected_revision:
            raise MCPControlError("mcp_revision_conflict", 409)
        updated = {"revision": current["revision"] + 1, "default_entry": entry_id}
        if not self._write(updated, expected_revision):
            raise MCPControlError("mcp_revision_conflict", 409)
        return {**updated, "effect": "future_connection_recommendation_only"}

    def action(self, entry_id: str, payload: dict) -> dict:
        entry_id = self._entry_id(entry_id)
        if not isinstance(payload, dict) or payload.get("action") not in _ACTIONS:
            raise MCPControlError("mcp_action_invalid", 422)
        if set(payload) - {"action", "expected_generation"}:
            raise MCPControlError("mcp_request_invalid", 422)
        body = {"action": payload["action"]}
        generation = payload.get("expected_generation")
        if generation is not None:
            if not isinstance(generation, str) or not 1 <= len(generation) <= 128:
                raise MCPControlError("mcp_generation_invalid", 422)
            body["expected_generation"] = generation
        return self._broker_request("POST", f"/v1/entries/{entry_id}/actions", body)

    def operation(self, operation_id: str) -> dict:
        if not _ID_RE.fullmatch(operation_id):
            raise MCPControlError("mcp_operation_not_found", 404)
        result = self._broker_request("GET", f"/v1/operations/{operation_id}")
        result["error_detail"] = ERROR_DETAILS.get(result.get("error"))
        result["runtime_label"] = RUNTIME_LABELS.get(result.get("runtime_state"))
        return result
