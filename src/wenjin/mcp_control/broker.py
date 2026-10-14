"""仅允许操作固定Compose项目中两个MCP容器的Docker broker。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wenjin.mcp_control.management import ERROR_DETAILS

ENTRIES = {"legacy": "mcp", "stateless": "mcp-stateless"}
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
GRACE_SECONDS = 20
ENGINE_TIMEOUT_SECONDS = 30
MAX_OPERATIONS = 100
OPERATION_TTL_SECONDS = 900
OPERATION_ID_RE = re.compile(r"^op-[a-f0-9]{24}$")


class BrokerError(RuntimeError):
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.code = code
        self.status = status


class ActionOutcomeUnknown(RuntimeError):
    """Engine接受了动作，但随后的状态读取未能确认结果。"""


class UnixHTTPConnection(http.client.HTTPConnection):
    """把http.client请求发送到Docker Engine Unix socket。"""

    def __init__(self, socket_path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


class DockerEngineClient:
    """通过Docker兼容HTTP API查找和控制指定容器。"""

    def __init__(
        self,
        project: str,
        socket_path: str = "/var/run/docker.sock",
        timeout: float = ENGINE_TIMEOUT_SECONDS,
    ):
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
            raise ValueError("MCP控制broker要求合法的固定Compose项目名")
        self.project = project
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        connection = UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read(1_048_576)
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException) as exc:
            raise BrokerError("docker_engine_unavailable", 503) from exc
        finally:
            connection.close()

    def matching(self, service: str) -> list[dict]:
        filters = json.dumps({"label": [
            f"com.docker.compose.project={self.project}",
            f"com.docker.compose.service={service}",
        ]}, separators=(",", ":"))
        query = urllib.parse.urlencode({"all": "1", "filters": filters})
        status, body = self.request("GET", f"/containers/json?{query}")
        if status != 200:
            raise BrokerError("docker_engine_unavailable", 503)
        try:
            rows = json.loads(body)
        except json.JSONDecodeError as exc:
            raise BrokerError("docker_engine_invalid_response", 503) from exc
        if not isinstance(rows, list):
            raise BrokerError("docker_engine_invalid_response", 503)
        matched = []
        for row in rows:
            labels = row.get("Labels") if isinstance(row, dict) else None
            if (
                isinstance(labels, dict)
                and labels.get("com.docker.compose.project") == self.project
                and labels.get("com.docker.compose.service") == service
                and isinstance(row.get("Id"), str)
                and row["Id"]
            ):
                matched.append(row)
        if not matched:
            raise BrokerError("mcp_container_not_found", 404)
        if len(matched) != 1:
            raise BrokerError("mcp_container_ambiguous", 409)
        return matched

    @staticmethod
    def generation(container_id: str) -> str:
        return hashlib.sha256(container_id.encode("ascii", "strict")).hexdigest()[:24]

    def inspect(self, container_id: str) -> dict:
        status, body = self.request("GET", f"/containers/{urllib.parse.quote(container_id, safe='')}/json")
        if status != 200:
            raise BrokerError("docker_engine_unavailable", 503)
        try:
            result = json.loads(body)
            labels = result["Config"]["Labels"]
            state = result["State"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise BrokerError("docker_engine_invalid_response", 503) from exc
        return {"labels": labels, "state": state}

    def entry(self, entry_id: str) -> dict:
        service = ENTRIES[entry_id]
        row = self.matching(service)[0]
        inspected = self.inspect(row["Id"])
        expected = {
            "com.docker.compose.project": self.project,
            "com.docker.compose.service": service,
        }
        if not isinstance(inspected["labels"], dict) or any(
            inspected["labels"].get(key) != value for key, value in expected.items()
        ):
            raise BrokerError("mcp_container_labels_changed", 409)
        state = inspected["state"]
        status = state.get("Status")
        if status not in {"created", "running", "paused", "restarting", "removing", "exited", "dead"}:
            status = "unknown"
        return {
            "entry_id": entry_id,
            "runtime_state": status,
            "generation": self.generation(row["Id"]),
        }

    def control(self, entry_id: str, action: str, expected_generation: str | None) -> dict:
        current = self.entry(entry_id)
        if expected_generation is not None and current["generation"] != expected_generation:
            raise BrokerError("mcp_generation_conflict", 409)
        status = current["runtime_state"]
        stopped_states = {"created", "exited", "dead"}
        if action == "start" and status == "running":
            return {"runtime_state": current["runtime_state"], "generation": current["generation"]}
        if action == "stop" and status in stopped_states:
            return {"runtime_state": status, "generation": current["generation"]}
        if action == "start" and status not in stopped_states:
            raise BrokerError("mcp_container_state_conflict", 409)
        if action == "stop" and status != "running":
            raise BrokerError("mcp_container_state_conflict", 409)
        rows = self.matching(ENTRIES[entry_id])
        container_id = rows[0]["Id"]
        inspected = self.inspect(container_id)
        if self.generation(container_id) != current["generation"]:
            raise BrokerError("mcp_generation_conflict", 409)
        labels = inspected["labels"]
        if not isinstance(labels, dict) or (
            labels.get("com.docker.compose.project") != self.project
            or labels.get("com.docker.compose.service") != ENTRIES[entry_id]
        ):
            raise BrokerError("mcp_container_labels_changed", 409)
        current_status = inspected["state"].get("Status")
        if action == "start" and current_status == "running":
            return {"runtime_state": current_status, "generation": current["generation"]}
        if action == "stop" and current_status in stopped_states:
            return {"runtime_state": current_status, "generation": current["generation"]}
        if action == "start" and current_status not in stopped_states:
            raise BrokerError("mcp_container_state_conflict", 409)
        if action == "stop" and current_status != "running":
            raise BrokerError("mcp_container_state_conflict", 409)
        if action == "start":
            try:
                status, _ = self.request(
                    "POST", f"/containers/{urllib.parse.quote(container_id, safe='')}/start"
                )
            except BrokerError as exc:
                if exc.code == "docker_engine_unavailable":
                    raise ActionOutcomeUnknown("mcp_action_outcome_unknown") from exc
                raise
            if status not in {204, 304}:
                raise BrokerError("docker_engine_action_failed", 503)
        else:
            query = urllib.parse.urlencode({"t": str(GRACE_SECONDS)})
            try:
                status, _ = self.request(
                    "POST", f"/containers/{urllib.parse.quote(container_id, safe='')}/stop?{query}"
                )
            except BrokerError as exc:
                if exc.code == "docker_engine_unavailable":
                    raise ActionOutcomeUnknown("mcp_action_outcome_unknown") from exc
                raise
            if status not in {204, 304}:
                raise BrokerError("docker_engine_action_failed", 503)
        try:
            final = self.inspect(container_id)
        except BrokerError as exc:
            raise ActionOutcomeUnknown("mcp_action_state_unconfirmed") from exc
        final_labels = final["labels"]
        if (
            self.generation(container_id) != current["generation"]
            or not isinstance(final_labels, dict)
            or final_labels.get("com.docker.compose.project") != self.project
            or final_labels.get("com.docker.compose.service") != ENTRIES[entry_id]
        ):
            raise ActionOutcomeUnknown("mcp_action_state_unconfirmed")
        final_status = final["state"].get("Status")
        if action == "start" and final_status != "running":
            raise ActionOutcomeUnknown("mcp_action_state_unconfirmed")
        if action == "stop" and final_status not in stopped_states:
            raise ActionOutcomeUnknown("mcp_action_state_unconfirmed")
        return {"runtime_state": final_status, "generation": current["generation"]}


class OperationRegistry:
    """有界保留异步动作，并在超时后保持目标入口busy直至状态复核。"""

    def __init__(self, engine: DockerEngineClient):
        self.engine = engine
        self.lock = threading.Lock()
        self.operations: dict[str, dict] = {}
        self.active_by_entry: dict[str, str] = {}
        self.uncertain_by_entry: dict[str, str] = {}

    def _prune(self):
        now = time.time()
        for operation_id, operation in list(self.operations.items()):
            if operation["state"] in {"succeeded", "failed"} and now - operation["updated_at"] > OPERATION_TTL_SECONDS:
                del self.operations[operation_id]
        while len(self.operations) >= MAX_OPERATIONS:
            removable = next((key for key, value in self.operations.items() if value["state"] in {"succeeded", "failed"}), None)
            if removable is None:
                raise BrokerError("mcp_operation_capacity_reached", 503)
            del self.operations[removable]

    def start(self, entry_id: str, action: str, expected_generation: str | None) -> dict:
        with self.lock:
            self._prune()
            if entry_id in self.active_by_entry:
                raise BrokerError("mcp_action_in_progress", 409)
            uncertain_id = self.uncertain_by_entry.get(entry_id)
            if uncertain_id:
                raise BrokerError("mcp_action_outcome_unknown", 409)
            operation_id = "op-" + uuid.uuid4().hex[:24]
            now = time.time()
            self.operations[operation_id] = {
                "operation_id": operation_id,
                "entry_id": entry_id,
                "action": action,
                "state": "running",
                "runtime_state": None,
                "generation": None,
                "target_generation": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "expected_generation": expected_generation,
            }
            self.active_by_entry[entry_id] = operation_id
            worker = threading.Thread(
                target=self._run, args=(operation_id,), daemon=True, name=f"mcp-control-{entry_id}"
            )
            worker.start()
            return {
                "operation_id": operation_id,
                "state": "running",
                "entry_id": entry_id,
                "action": action,
                "impact": (
                    "停止会中断该入口上的客户端连接；已有Agent配置不会自动迁移。"
                    if action == "stop"
                    else "仅启动已创建的目标容器；已有Agent配置不会自动迁移。"
                ),
            }

    def _run(self, operation_id: str):
        with self.lock:
            operation = self.operations.get(operation_id)
            if operation is None:
                return
            entry_id, action = operation["entry_id"], operation["action"]
            expected = operation["expected_generation"]
        try:
            before = self.engine.entry(entry_id)
            target_generation = before["generation"]
            if expected is not None and expected != target_generation:
                raise BrokerError("mcp_generation_conflict", 409)
            with self.lock:
                operation = self.operations.get(operation_id)
                if operation is not None:
                    operation["target_generation"] = target_generation
            result = self.engine.control(entry_id, action, target_generation)
            state, error = "succeeded", None
            runtime, generation = result["runtime_state"], result["generation"]
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            state, error, runtime, generation = "outcome_unknown", "mcp_action_outcome_unknown", None, None
        except ActionOutcomeUnknown:
            state, error, runtime, generation = "outcome_unknown", "mcp_action_state_unconfirmed", None, None
        except BrokerError as exc:
            state, error, runtime, generation = "failed", exc.code, None, None
        except Exception:
            state, error, runtime, generation = "failed", "mcp_control_internal_error", None, None
        with self.lock:
            operation = self.operations.get(operation_id)
            if operation is None:
                return
            operation.update(
                state=state,
                error=error,
                runtime_state=runtime,
                generation=generation or operation.get("target_generation"),
                updated_at=time.time(),
            )
            if state == "outcome_unknown":
                self.uncertain_by_entry[entry_id] = operation_id
                self.active_by_entry.pop(entry_id, None)
            else:
                self.active_by_entry.pop(entry_id, None)

    def get(self, operation_id: str) -> dict:
        with self.lock:
            operation = self.operations.get(operation_id)
            if (
                operation is not None
                and operation["state"] in {"succeeded", "failed"}
                and time.time() - operation["updated_at"] > OPERATION_TTL_SECONDS
            ):
                del self.operations[operation_id]
                operation = None
            if operation is None:
                raise BrokerError("mcp_operation_expired", 410)
            result = {
                key: value for key, value in operation.items()
                if not key.startswith("expected_") and key != "target_generation"
            }
            if result["generation"] is None:
                result["generation"] = operation["target_generation"]
            return result

    def entries(self) -> dict:
        result = []
        for entry_id in ENTRIES:
            try:
                row = self.engine.entry(entry_id)
            except BrokerError as exc:
                if exc.code not in {"mcp_container_not_found", "mcp_container_ambiguous"}:
                    raise
                result.append({
                    "entry_id": entry_id,
                    "runtime_state": "unmanaged",
                    "generation": None,
                    "control_error": exc.code,
                })
                continue
            with self.lock:
                uncertain_id = self.uncertain_by_entry.get(entry_id)
                operation = self.operations.get(uncertain_id) if uncertain_id else None
            if operation is not None:
                target_reached = (
                    row["generation"] == operation.get("target_generation")
                    and operation["action"] == "start"
                    and row["runtime_state"] == "running"
                ) or (
                    row["generation"] == operation.get("target_generation")
                    and operation["action"] == "stop"
                    and row["runtime_state"] in {"created", "exited", "dead"}
                )
                with self.lock:
                    if target_reached:
                        operation["state"] = "succeeded"
                        operation["error"] = None
                        operation["runtime_state"] = row["runtime_state"]
                        operation["generation"] = row["generation"]
                        operation["updated_at"] = time.time()
                        self.uncertain_by_entry.pop(entry_id, None)
                        self.active_by_entry.pop(entry_id, None)
            result.append({**row, "control_error": None})
        return {"entries": result, "runtime_is_protocol_readiness": False}


class MCPControlBroker:
    def __init__(self, token: str, project: str, engine: DockerEngineClient | None = None):
        if not token or len(token) < 32:
            raise ValueError("MCP控制broker要求至少32字符的secret")
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
            raise ValueError("MCP控制broker要求显式合法的Compose项目名")
        self.token = token
        self.operations = OperationRegistry(engine or DockerEngineClient(project=project))

    @classmethod
    def from_env(cls, engine: DockerEngineClient | None = None):
        """在监听HTTP端口前校验必需secret和固定Compose项目。"""
        token = os.environ.get("WENJIN_MCP_CONTROL_TOKEN", "")
        project = os.environ.get("WENJIN_MCP_CONTROL_PROJECT", "")
        return cls(token, project, engine=engine)

    @staticmethod
    def _json(payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _error(code: str) -> dict:
        return {"error": code, "detail": ERROR_DETAILS.get(code, "MCP容器控制请求失败。")}

    def authorized(self, header: str) -> bool:
        scheme, separator, credential = header.partition(" ")
        return (
            bool(separator)
            and scheme.lower() == "bearer"
            and bool(credential)
            and secrets.compare_digest(credential, self.token)
        )

    def handle(self, method: str, target: str, authorization: str, body: bytes) -> tuple[int, bytes]:
        if not self.authorized(authorization):
            return 401, self._json({"error": "unauthorized", "detail": "需要有效的控制broker凭据。"})
        parsed = urllib.parse.urlsplit(target)
        try:
            if method == "GET" and parsed.path == "/v1/entries":
                return 200, self._json(self.operations.entries())
            if method == "GET" and parsed.path.startswith("/v1/operations/"):
                operation_id = parsed.path.rsplit("/", 1)[-1]
                if not OPERATION_ID_RE.fullmatch(operation_id):
                    raise BrokerError("mcp_operation_not_found", 404)
                return 200, self._json(self.operations.get(operation_id))
            match = re.fullmatch(r"/v1/entries/(legacy|stateless)/actions", parsed.path)
            if method == "POST" and match:
                try:
                    payload = json.loads(body)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BrokerError("mcp_request_invalid", 422) from exc
                if (
                    not isinstance(payload, dict)
                    or set(payload) - {"action", "expected_generation"}
                    or payload.get("action") not in {"start", "stop"}
                ):
                    raise BrokerError("mcp_action_invalid", 422)
                generation = payload.get("expected_generation")
                if generation is not None and (not isinstance(generation, str) or not 1 <= len(generation) <= 128):
                    raise BrokerError("mcp_generation_invalid", 422)
                result = self.operations.start(match.group(1), payload["action"], generation)
                return 202, self._json(result)
            raise BrokerError("not_found", 404)
        except BrokerError as exc:
            return exc.status, self._json(self._error(exc.code))
        except (TimeoutError, socket.timeout, OSError, http.client.HTTPException):
            return 503, self._json(self._error("docker_engine_unavailable"))
        except Exception:
            return 503, self._json(self._error("mcp_control_internal_error"))


class BrokerRequestHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _dispatch(self):
        if not self.server.broker.authorized(self.headers.get("Authorization", "")):
            self._send(401, {"error": "unauthorized", "detail": "需要有效的控制broker凭据。"})
            return
        self.connection.settimeout(5)
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit():
            self._send(400, MCPControlBroker._error("mcp_request_invalid"))
            return
        if int(length) > 16_384:
            self._send(413, {"error": "request_too_large", "detail": "请求体无效或超过16 KiB。"})
            return
        try:
            body = self.rfile.read(int(length))
        except socket.timeout:
            self._send(408, {"error": "request_body_timeout", "detail": "请求体读取超时。"})
            return
        status, payload = self.server.broker.handle(
            self.command,
            self.path,
            self.headers.get("Authorization", ""),
            body,
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send(self, status: int, payload: dict):
        data = self.server.broker._json(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("mcp-control-broker: " + fmt % args, flush=True)


def main():
    broker = MCPControlBroker.from_env()
    host = os.environ.get("WENJIN_MCP_CONTROL_HOST", "0.0.0.0")
    port = int(os.environ.get("WENJIN_MCP_CONTROL_PORT", "8788"))
    server = ThreadingHTTPServer((host, port), BrokerRequestHandler)
    server.broker = broker
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
