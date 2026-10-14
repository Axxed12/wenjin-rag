"""管理员MCP入口与固定容器控制合同；所有状态写入pytest临时目录。"""

from __future__ import annotations

import asyncio
import http.client
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from wenjin.management_http.app import create_app
from wenjin.mcp_control.broker import (
    BrokerError,
    BrokerRequestHandler,
    DockerEngineClient,
    MCPControlBroker,
    OperationRegistry,
)
from wenjin.mcp_control.management import MCPControlError, MCPControlService

pytestmark = pytest.mark.integration

_ADMIN_TOKEN = "synthetic-entry-control-bootstrap-bearer-2026"
_ADMIN_PASSWORD = "Entry_Control_Admin_Password_2026!"
_READER_TEMP_PASSWORD = "Entry_Control_Reader_Temporary_2026!"
_READER_PASSWORD = "Entry_Control_Reader_Changed_2026!"
_BROKER_TOKEN = "synthetic-entry-control-broker-token-2026-32chars"


class _Manager:
    """管理app启动所需的最小合成manager。"""

    def resume_pending_jobs(self):
        return []


class _SessionClient:
    def __init__(self, client: TestClient):
        self.client = client
        self.token: str | None = None

    def request(self, method: str, url: str, **kwargs):
        previous = self.client.cookies.get("wenjin_session")
        self.client.cookies.delete("wenjin_session")
        if self.token:
            self.client.cookies.set("wenjin_session", self.token)
        try:
            response = self.client.request(method, url, **kwargs)
            cookie = SimpleCookie()
            cookie.load(response.headers.get("set-cookie", ""))
            if "wenjin_session" in cookie:
                self.token = cookie["wenjin_session"].value
            return response
        finally:
            self.client.cookies.delete("wenjin_session")
            if previous:
                self.client.cookies.set("wenjin_session", previous)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs):
        return self.request("PUT", url, **kwargs)


def _same_origin(csrf: str) -> dict[str, str]:
    return {"Origin": "http://testserver", "X-CSRF-Token": csrf}


def _login(client: _SessionClient, username: str, password: str) -> dict:
    response = client.post(
        "/api/auth/login",
        headers={"Origin": "http://testserver"},
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _new_app(root: Path, *, mcp_control_service=None):
    root.mkdir(parents=True, exist_ok=True)
    return create_app(
        root,
        admin_token=_ADMIN_TOKEN,
        manager=_Manager(),
        identity_db=root / "identity.sqlite",
        mcp_control_service=mcp_control_service,
    )


def _bootstrap_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/bootstrap",
        headers={"Authorization": f"Bearer {_ADMIN_TOKEN}", "Origin": "http://testserver"},
        json={"username": "entry-admin", "password": _ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text


def _admin_session(base: TestClient) -> tuple[_SessionClient, str]:
    admin = _SessionClient(base)
    session = _login(admin, "entry-admin", _ADMIN_PASSWORD)
    return admin, session["csrf_token"]


def _assert_cn_http_error(response, *, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == code
    detail = body.get("detail", "")
    assert any("\u4e00" <= char <= "\u9fff" for char in detail), body


def test_entries_and_connection_config_are_static_and_do_not_expose_control_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("WENJIN_MCP_CONTROL_URL", raising=False)
    monkeypatch.delenv("WENJIN_MCP_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("WENJIN_MCP_LEGACY_PUBLIC_URL", "http://localhost:18765/mcp")
    monkeypatch.setenv("WENJIN_MCP_STATELESS_PUBLIC_URL", "http://localhost:18768/mcp-stateless")
    service = MCPControlService(tmp_path)

    with TestClient(
        _new_app(tmp_path, mcp_control_service=service), client=("127.0.0.1", 52341)
    ) as base:
        _bootstrap_admin(base)
        admin, csrf = _admin_session(base)
        response = admin.get("/api/admin/mcp/entries")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["default_entry"] == "legacy"
        assert body["revision"] == 0
        assert {row["entry_id"] for row in body["entries"]} == {"legacy", "stateless"}
        legacy = next(row for row in body["entries"] if row["entry_id"] == "legacy")
        modern = next(row for row in body["entries"] if row["entry_id"] == "stateless")
        assert legacy["endpoint"] == "http://localhost:18765/mcp"
        assert modern["endpoint"] == "http://localhost:18768/mcp-stateless"
        assert legacy["runtime_state"] == modern["runtime_state"] == "unmanaged"
        assert legacy["control_configured"] is modern["control_configured"] is False

        old_config = admin.get("/api/admin/mcp/connection-config?entry_id=legacy")
        new_config = admin.get("/api/admin/mcp/connection-config?entry_id=stateless")
        assert old_config.status_code == new_config.status_code == 200
        assert old_config.json()["protocol"] == "2025-11-25"
        assert new_config.json()["protocol"] == "2026-07-28"
        assert old_config.json()["endpoint"] == "http://localhost:18765/mcp"
        assert new_config.json()["endpoint"] == "http://localhost:18768/mcp-stateless"
        for config in (old_config.json(), new_config.json()):
            assert config["auth"]["scheme"] == "Bearer"
            assert "credential_source" in config["auth"]
            assert "token" not in json.dumps(config).lower()

        unknown = admin.get("/api/admin/mcp/connection-config?entry_id=unknown")
        assert unknown.status_code == 404
        assert unknown.json()["error"] == "mcp_entry_not_found"
        assert any("\u4e00" <= char <= "\u9fff" for char in unknown.json()["detail"])
        disabled = admin.post(
            "/api/admin/mcp/entries/legacy/actions",
            headers=_same_origin(csrf),
            json={"action": "start"},
        )
        assert disabled.status_code == 503
        assert disabled.json()["error"] == "mcp_container_control_not_configured"


def test_default_entry_uses_cas_persists_and_only_changes_future_recommendation(
    tmp_path: Path,
):
    service = MCPControlService(tmp_path)
    with TestClient(
        _new_app(tmp_path, mcp_control_service=service), client=("127.0.0.1", 52341)
    ) as base:
        _bootstrap_admin(base)
        admin, csrf = _admin_session(base)
        headers = _same_origin(csrf)
        updated = admin.put(
            "/api/admin/mcp/default-entry",
            headers=headers,
            json={"entry_id": "stateless", "expected_revision": 0},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json() == {
            "revision": 1,
            "default_entry": "stateless",
            "effect": "future_connection_recommendation_only",
        }
        stale = admin.put(
            "/api/admin/mcp/default-entry",
            headers=headers,
            json={"entry_id": "legacy", "expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "mcp_revision_conflict"
        invalid = admin.put(
            "/api/admin/mcp/default-entry",
            headers=headers,
            json={"entry_id": "arbitrary", "expected_revision": 1},
        )
        assert invalid.status_code == 404
        assert invalid.json()["error"] == "mcp_entry_not_found"

    reopened = MCPControlService(tmp_path).entries()
    assert reopened["default_entry"] == "stateless"
    assert reopened["revision"] == 1


def test_admin_session_role_origin_csrf_and_first_password_change_are_required(
    tmp_path: Path,
):
    with TestClient(_new_app(tmp_path), client=("127.0.0.1", 52341)) as base:
        _bootstrap_admin(base)
        admin, csrf = _admin_session(base)
        no_session = base.get("/api/admin/mcp/entries")
        assert no_session.status_code == 401
        assert admin.get("/api/admin/mcp/entries").status_code == 200

        bad_origin = admin.put(
            "/api/admin/mcp/default-entry",
            headers={"Origin": "https://attacker.invalid", "X-CSRF-Token": csrf},
            json={"entry_id": "stateless", "expected_revision": 0},
        )
        missing_csrf = admin.put(
            "/api/admin/mcp/default-entry",
            headers={"Origin": "http://testserver"},
            json={"entry_id": "stateless", "expected_revision": 0},
        )
        assert bad_origin.status_code == missing_csrf.status_code == 403

        created = admin.post(
            "/api/admin/users",
            headers=_same_origin(csrf),
            json={
                "username": "entry-reader",
                "password": _READER_TEMP_PASSWORD,
                "role": "reader",
            },
        )
        assert created.status_code == 201, created.text
        reader = _SessionClient(base)
        first = _login(reader, "entry-reader", _READER_TEMP_PASSWORD)
        assert first["must_change_password"] is True
        assert reader.get("/api/admin/mcp/entries").status_code == 403
        changed = reader.post(
            "/api/auth/password",
            headers=_same_origin(first["csrf_token"]),
            json={"old_password": _READER_TEMP_PASSWORD, "new_password": _READER_PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        _login(reader, "entry-reader", _READER_PASSWORD)
        assert reader.get("/api/admin/mcp/entries").status_code == 403


def test_blocking_broker_call_does_not_block_another_management_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class BlockingBrokerHandler(BrokerRequestHandler):
        def do_GET(self):
            self.server.entered.set()
            self.server.release.wait(timeout=2.0)
            super().do_GET()

    class BlockingBrokerServer(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, address, broker):
            self.broker = broker
            self.entered = threading.Event()
            self.release = threading.Event()
            super().__init__(address, BlockingBrokerHandler)

    broker = MCPControlBroker(_BROKER_TOKEN, "wenjin", engine=_BlockingEngine())
    broker_server = BlockingBrokerServer(("127.0.0.1", 0), broker)
    broker_thread = threading.Thread(target=broker_server.serve_forever, daemon=True)
    broker_thread.start()
    monkeypatch.setenv("WENJIN_MCP_CONTROL_URL", f"http://127.0.0.1:{broker_server.server_port}")
    monkeypatch.setenv("WENJIN_MCP_CONTROL_TOKEN", _BROKER_TOKEN)
    service = MCPControlService(tmp_path)
    app = _new_app(tmp_path, mcp_control_service=service)
    try:
        with TestClient(app, client=("127.0.0.1", 52341)) as base:
            _bootstrap_admin(base)
            admin, _csrf = _admin_session(base)

            async def run_concurrent_requests():
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    client.cookies.set("wenjin_session", admin.token)
                    started = time.monotonic()
                    pending = asyncio.create_task(client.get("/api/admin/mcp/entries"))
                    assert await asyncio.to_thread(broker_server.entered.wait, 1.2)
                    health = await asyncio.wait_for(client.get("/api/health"), timeout=0.3)
                    elapsed = time.monotonic() - started
                    broker_server.release.set()
                    entries = await asyncio.wait_for(pending, timeout=2.0)
                    return health, entries, elapsed

            health, entries, elapsed = asyncio.run(run_concurrent_requests())
            assert health.status_code == 200
            assert entries.status_code == 200
            assert elapsed < 0.3
    finally:
        broker_server.release.set()
        broker_server.shutdown()
        broker_server.server_close()
        broker_thread.join(timeout=1)


@pytest.mark.parametrize(
    ("url", "expected_code"),
    [
        ("ftp://localhost:18765/mcp", "mcp_public_url_invalid"),
        ("http://user:pass@localhost:18765/mcp", "mcp_public_url_invalid"),
        ("http://localhost:18765/mcp?token=leak", "mcp_public_url_invalid"),
        ("http://localhost:99999/mcp", "mcp_public_url_invalid"),
    ],
)
def test_invalid_public_url_is_rejected_without_echoing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    expected_code: str,
):
    monkeypatch.setenv("WENJIN_MCP_LEGACY_PUBLIC_URL", url)
    with pytest.raises(MCPControlError) as error:
        MCPControlService(tmp_path).connection_config("legacy")
    assert error.value.code == expected_code
    assert url not in str(error.value)


def test_unconfigured_control_broker_fails_closed_for_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("WENJIN_MCP_CONTROL_URL", raising=False)
    monkeypatch.delenv("WENJIN_MCP_CONTROL_TOKEN", raising=False)
    with pytest.raises(MCPControlError) as error:
        MCPControlService(tmp_path).action("legacy", {"action": "start"})
    assert error.value.code == "mcp_container_control_not_configured"
    with pytest.raises(MCPControlError) as invalid_action:
        MCPControlService(tmp_path).action("legacy", {"action": "restart"})
    assert invalid_action.value.code == "mcp_action_invalid"
    with pytest.raises(MCPControlError) as invalid_generation:
        MCPControlService(tmp_path).action("legacy", {"action": "stop", "expected_generation": 1})
    assert invalid_generation.value.code == "mcp_generation_invalid"


def test_management_json_is_bounded_typed_closed_and_returns_chinese_errors(
    tmp_path: Path,
):
    with TestClient(_new_app(tmp_path), client=("127.0.0.1", 52341)) as base:
        _bootstrap_admin(base)
        admin, csrf = _admin_session(base)
        headers = {**_same_origin(csrf), "Content-Type": "application/json"}
        url = "/api/admin/mcp/default-entry"

        malformed = admin.put(url, headers=headers, content=b"{")
        non_object = admin.put(url, headers=headers, content=b"[]")
        _assert_cn_http_error(malformed, status=400, code="request_rejected")
        _assert_cn_http_error(non_object, status=400, code="request_rejected")

        too_large_body = (
            b'{"entry_id":"legacy","expected_revision":0,"padding":"' + b"x" * 8200 + b'"}'
        )
        too_large = admin.put(url, headers=headers, content=too_large_body)
        _assert_cn_http_error(too_large, status=413, code="request_rejected")

        missing = admin.put(url, headers=headers, json={"entry_id": "legacy"})
        wrong_type = admin.put(
            url,
            headers=headers,
            json={"entry_id": "legacy", "expected_revision": True},
        )
        extra = admin.put(
            url,
            headers=headers,
            json={"entry_id": "legacy", "expected_revision": 0, "extra": "ignored?"},
        )
        bad_entry_type = admin.put(
            url,
            headers=headers,
            json={"entry_id": [], "expected_revision": 0},
        )
        _assert_cn_http_error(missing, status=422, code="mcp_request_invalid")
        _assert_cn_http_error(wrong_type, status=422, code="mcp_revision_invalid")
        _assert_cn_http_error(extra, status=422, code="mcp_request_invalid")
        _assert_cn_http_error(bad_entry_type, status=422, code="mcp_request_invalid")

        action_extra = admin.post(
            "/api/admin/mcp/entries/legacy/actions",
            headers=headers,
            json={"action": "start", "extra": "ignored?"},
        )
        _assert_cn_http_error(action_extra, status=422, code="mcp_request_invalid")


def test_broker_json_is_bounded_closed_and_returns_stable_chinese_errors():
    broker = MCPControlBroker(_BROKER_TOKEN, "wenjin", engine=_BlockingEngine())
    malformed = broker.handle(
        "POST",
        "/v1/entries/legacy/actions",
        f"Bearer {_BROKER_TOKEN}",
        b"{",
    )
    non_object = broker.handle(
        "POST",
        "/v1/entries/legacy/actions",
        f"Bearer {_BROKER_TOKEN}",
        b"[]",
    )
    extra = broker.handle(
        "POST",
        "/v1/entries/legacy/actions",
        f"Bearer {_BROKER_TOKEN}",
        b'{"action":"start","unexpected":"x"}',
    )
    for (status, payload), expected_code in (
        (malformed, "mcp_request_invalid"),
        (non_object, "mcp_action_invalid"),
        (extra, "mcp_action_invalid"),
    ):
        body = json.loads(payload)
        assert status == 422
        assert body["error"] == expected_code
        assert any("\u4e00" <= char <= "\u9fff" for char in body.get("detail", ""))

    server = _TestBrokerHTTPServer(("127.0.0.1", 0), broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/entries/legacy/actions",
            data=b"x" * (16 * 1024 + 1),
            method="POST",
            headers={"Authorization": f"Bearer {_BROKER_TOKEN}"},
        )
        with pytest.raises(urllib.error.HTTPError) as response:
            urllib.request.urlopen(request, timeout=1.0)
        assert response.value.code == 413
        body = json.loads(response.value.read().decode("utf-8"))
        assert body["error"] == "request_too_large"
        assert any("\u4e00" <= char <= "\u9fff" for char in body.get("detail", ""))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_mcp_control_settings_connections_are_explicitly_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import wenjin.mcp_control.management as management_module

    original_connect = sqlite3.connect
    closed: list[bool] = []

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            return super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(management_module.sqlite3, "connect", tracking_connect)
    service = MCPControlService(tmp_path)
    assert service.entries()["revision"] == 0
    service.set_default("stateless", 0)
    assert len(closed) == 3


class _EngineState:
    def __init__(self, rows: list[dict] | None = None, *, project: str = "wenjin"):
        self.project = project
        self.rows = (
            rows
            if rows is not None
            else [
                {
                    "Id": "legacy-container-id",
                    "Labels": {
                        "com.docker.compose.project": project,
                        "com.docker.compose.service": "mcp",
                    },
                    "Status": "exited",
                },
                {
                    "Id": "stateless-container-id",
                    "Labels": {
                        "com.docker.compose.project": project,
                        "com.docker.compose.service": "mcp-stateless",
                    },
                    "Status": "running",
                },
            ]
        )
        self.lock = threading.Lock()
        self.actions: list[tuple[str, str]] = []
        self.list_filters: list[dict[str, list[str]]] = []
        self.inspections: list[str] = []
        self.changed_labels: dict[str, dict] = {}
        self.post_states: dict[str, str | None] = {}


class _FakeDockerHandler(BaseHTTPRequestHandler):
    server: "_FakeDockerHTTPServer"

    def _send(self, status: int, payload: dict | list | None = None):
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        with self.server.state.lock:
            if parsed.path == "/containers/json":
                # Docker Engine 通常先按labels过滤；测试server故意可返回过滤外条目，
                # 以验证客户端仍二次核对精确project/service labels。
                self.server.state.list_filters.append(parse_qs(parsed.query))
                rows = [dict(row) for row in self.server.state.rows]
                self._send(200, rows)
                return
            if parsed.path.startswith("/containers/") and parsed.path.endswith("/json"):
                container_id = parsed.path.split("/")[2]
                self.server.state.inspections.append(container_id)
                row = next(
                    (item for item in self.server.state.rows if item["Id"] == container_id), None
                )
                if row is None:
                    self._send(404, {"message": "not found"})
                    return
                labels = self.server.state.changed_labels.get(container_id, row["Labels"])
                self._send(200, {"Config": {"Labels": labels}, "State": {"Status": row["Status"]}})
                return
        self._send(404, {"message": "not found"})

    def do_POST(self):
        parsed = urlsplit(self.path)
        parts = parsed.path.split("/")
        if len(parts) >= 4 and parts[1] == "containers":
            container_id, verb = parts[2], parts[3]
            with self.server.state.lock:
                row = next(
                    (item for item in self.server.state.rows if item["Id"] == container_id), None
                )
                if row is None:
                    self._send(404, {"message": "not found"})
                    return
                if verb == "start":
                    final_state = self.server.state.post_states.get("start", "running")
                    if final_state is not None:
                        row["Status"] = final_state
                elif verb == "stop":
                    final_state = self.server.state.post_states.get("stop", "exited")
                    if final_state is not None:
                        row["Status"] = final_state
                else:
                    self._send(404, {"message": "not found"})
                    return
                self.server.state.actions.append((container_id, verb))
            self._send(204)
            return
        self._send(404, {"message": "not found"})

    def log_message(self, fmt, *args):
        pass


class _FakeDockerHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, state: _EngineState):
        self.state = state
        super().__init__(address, _FakeDockerHandler)


class _TestBrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, broker):
        super().__init__(address, BrokerRequestHandler)
        self.broker = broker


class _TCPDockerEngineClient(DockerEngineClient):
    """Uses real Docker Engine HTTP requests over a synthetic loopback TCP server."""

    def __init__(self, host: str, port: int, project: str, timeout: float = 1.0):
        super().__init__(project=project, socket_path="synthetic-tcp-transport", timeout=timeout)
        self.host = host
        self.port = port

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(
                method,
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, response.read(1_048_576)
        finally:
            connection.close()


def _serve_fake_engine(state: _EngineState, *, project: str | None = None):
    server = _FakeDockerHTTPServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return (
        server,
        thread,
        _TCPDockerEngineClient("127.0.0.1", server.server_port, project or state.project),
    )


def _serve_broker(engine, token: str = _BROKER_TOKEN, project: str = "wenjin"):
    broker = MCPControlBroker(token, project, engine=engine)
    server = _TestBrokerHTTPServer(("127.0.0.1", 0), broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def _wait_operation(getter, operation_id: str, *, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = getter(operation_id)
        if last["state"] != "running":
            return last
        time.sleep(0.01)
    raise AssertionError(f"operation still pending after {timeout}s: {last}")


def test_manager_to_broker_to_synthetic_docker_engine_http_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state = _EngineState()
    engine_server, engine_thread, engine = _serve_fake_engine(state)
    broker_server, broker_thread, broker_url = _serve_broker(engine)
    monkeypatch.setenv("WENJIN_MCP_CONTROL_URL", broker_url)
    monkeypatch.setenv("WENJIN_MCP_CONTROL_TOKEN", _BROKER_TOKEN)
    monkeypatch.setenv("WENJIN_MCP_LEGACY_PUBLIC_URL", "http://localhost:18765/mcp")
    monkeypatch.setenv("WENJIN_MCP_STATELESS_PUBLIC_URL", "http://localhost:18768/mcp-stateless")
    service = MCPControlService(tmp_path)
    app = _new_app(tmp_path, mcp_control_service=service)
    try:
        with TestClient(app, client=("127.0.0.1", 52341)) as base:
            _bootstrap_admin(base)
            admin, csrf = _admin_session(base)
            listed = admin.get("/api/admin/mcp/entries")
            assert listed.status_code == 200, listed.text
            body = listed.json()
            modern = next(row for row in body["entries"] if row["entry_id"] == "stateless")
            assert modern["runtime_state"] == "running"
            assert modern["control_configured"] is True
            list_filters = [json.loads(item["filters"][0]) for item in state.list_filters]
            assert {tuple(sorted(filters["label"])) for filters in list_filters} >= {
                (
                    "com.docker.compose.project=wenjin",
                    "com.docker.compose.service=mcp",
                ),
                (
                    "com.docker.compose.project=wenjin",
                    "com.docker.compose.service=mcp-stateless",
                ),
            }
            config = admin.get("/api/admin/mcp/connection-config?entry_id=stateless").json()
            assert _BROKER_TOKEN not in json.dumps(config)
            assert broker_url not in json.dumps(body)
            assert broker_url not in json.dumps(config)

            stopped = admin.post(
                "/api/admin/mcp/entries/stateless/actions",
                headers=_same_origin(csrf),
                json={"action": "stop", "expected_generation": modern["generation"]},
            )
            assert stopped.status_code == 202, stopped.text
            operation_id = stopped.json()["operation_id"]
            assert stopped.json()["state"] == "running"

            def get_operation(op_id: str):
                result = admin.get(f"/api/admin/mcp/operations/{op_id}")
                assert result.status_code == 200, result.text
                return result.json()

            done = _wait_operation(get_operation, operation_id)
            assert done["state"] == "succeeded"
            assert done["runtime_state"] == "exited"
            assert state.actions == [("stateless-container-id", "stop")]
            refreshed = admin.get("/api/admin/mcp/entries").json()
            modern_after = next(
                row for row in refreshed["entries"] if row["entry_id"] == "stateless"
            )
            assert modern_after["runtime_state"] == "exited"
    finally:
        broker_server.shutdown()
        broker_server.server_close()
        broker_thread.join(timeout=1)
        engine_server.shutdown()
        engine_server.server_close()
        engine_thread.join(timeout=1)


@pytest.mark.parametrize(
    ("rows", "expected_code"),
    [
        ([], "mcp_container_not_found"),
        (
            [
                {
                    "Id": "duplicate-a",
                    "Labels": {
                        "com.docker.compose.project": "wenjin",
                        "com.docker.compose.service": "mcp",
                    },
                    "Status": "exited",
                },
                {
                    "Id": "duplicate-b",
                    "Labels": {
                        "com.docker.compose.project": "wenjin",
                        "com.docker.compose.service": "mcp",
                    },
                    "Status": "exited",
                },
            ],
            "mcp_container_ambiguous",
        ),
        (
            [
                {
                    "Id": "wrong-project",
                    "Labels": {
                        "com.docker.compose.project": "other",
                        "com.docker.compose.service": "mcp",
                    },
                    "Status": "running",
                }
            ],
            "mcp_container_not_found",
        ),
        (
            [
                {
                    "Id": "wrong-service",
                    "Labels": {
                        "com.docker.compose.project": "wenjin",
                        "com.docker.compose.service": "unrelated",
                    },
                    "Status": "running",
                }
            ],
            "mcp_container_not_found",
        ),
    ],
)
def test_engine_lookup_requires_one_exact_fixed_project_service_match(
    tmp_path: Path, rows: list[dict], expected_code: str
):
    state = _EngineState(rows)
    server, thread, engine = _serve_fake_engine(state)
    try:
        with pytest.raises(BrokerError) as error:
            engine.entry("legacy")
        assert error.value.code == expected_code
        filters = json.loads(state.list_filters[-1]["filters"][0])
        assert set(filters["label"]) == {
            "com.docker.compose.project=wenjin",
            "com.docker.compose.service=mcp",
        }
        assert not state.actions
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_generation_change_and_running_stopped_idempotence_do_not_issue_engine_actions(
    tmp_path: Path,
):
    state = _EngineState()
    server, thread, engine = _serve_fake_engine(state)
    try:
        old_generation = "0" * 24
        with pytest.raises(BrokerError, match="mcp_generation_conflict"):
            engine.control("legacy", "start", old_generation)
        assert engine.control("stateless", "start", None)["runtime_state"] == "running"
        state.rows[0]["Status"] = "exited"
        assert engine.control("legacy", "stop", None)["runtime_state"] == "exited"
        assert state.actions == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_inspect_label_drift_blocks_action_after_container_list_match():
    state = _EngineState()
    state.changed_labels["legacy-container-id"] = {
        "com.docker.compose.project": "other",
        "com.docker.compose.service": "mcp",
    }
    server, thread, engine = _serve_fake_engine(state)
    try:
        with pytest.raises(BrokerError) as error:
            engine.control("legacy", "start", None)
        assert error.value.code == "mcp_container_labels_changed"
        assert state.actions == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize("initial_state", ["paused", "restarting", "unknown"])
def test_stop_rejects_states_that_do_not_prove_container_is_stopped(
    initial_state: str,
):
    state = _EngineState()
    state.rows[0]["Status"] = initial_state
    server, thread, engine = _serve_fake_engine(state)
    try:
        with pytest.raises(BrokerError) as error:
            engine.control("legacy", "stop", None)
        assert error.value.code == "mcp_container_state_conflict"
        assert state.actions == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.mark.parametrize(
    ("action", "initial_state", "unchanged_state"),
    [
        ("stop", "running", "running"),
        ("start", "exited", "exited"),
    ],
)
def test_engine_action_requires_post_action_inspect_to_confirm_target_state(
    action: str, initial_state: str, unchanged_state: str
):
    state = _EngineState()
    state.rows[0]["Status"] = initial_state
    state.post_states[action] = unchanged_state
    server, thread, engine = _serve_fake_engine(state)
    broker = MCPControlBroker(_BROKER_TOKEN, "wenjin", engine=engine)
    try:
        accepted_status, accepted_body = broker.handle(
            "POST",
            "/v1/entries/legacy/actions",
            f"Bearer {_BROKER_TOKEN}",
            json.dumps({"action": action}).encode("utf-8"),
        )
        assert accepted_status == 202
        operation_id = json.loads(accepted_body)["operation_id"]
        result = _wait_operation(
            lambda op_id: json.loads(
                broker.handle(
                    "GET",
                    f"/v1/operations/{op_id}",
                    f"Bearer {_BROKER_TOKEN}",
                    b"",
                )[1]
            ),
            operation_id,
        )
        assert result["state"] == "outcome_unknown"
        assert result["error"] == "mcp_action_state_unconfirmed"
        assert state.actions == [("legacy-container-id", action)]
        assert state.inspections.count("legacy-container-id") >= 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


class _BlockingEngine:
    def __init__(self, *, failure: BaseException | None = None):
        self.started = threading.Event()
        self.release = threading.Event()
        self.failure = failure

    def entry(self, entry_id: str):
        return {"entry_id": entry_id, "runtime_state": "exited", "generation": "g" * 24}

    def control(self, entry_id: str, action: str, expected_generation: str | None):
        self.started.set()
        if self.failure is not None:
            raise self.failure
        self.release.wait(timeout=2)
        return {
            "runtime_state": "running" if action == "start" else "stopped",
            "generation": "g" * 24,
        }


def test_pending_actions_are_bounded_and_per_entry_mutex_survives_caller_disconnect(
    monkeypatch: pytest.MonkeyPatch,
):
    import wenjin.mcp_control.broker as broker_module

    engine = _BlockingEngine()
    server, thread, broker_url = _serve_broker(engine)

    def broker_request(path: str, payload: dict | None = None):
        request = urllib.request.Request(
            broker_url + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            method="POST" if payload is not None else "GET",
            headers={
                "Authorization": f"Bearer {_BROKER_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=1.0) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    try:
        accepted_status, started = broker_request("/v1/entries/legacy/actions", {"action": "start"})
        assert accepted_status == 202
        operation_id = started["operation_id"]
        # 202响应被关闭后调用方不再轮询；broker线程仍执行已接受的动作。
        assert engine.started.wait(timeout=1)
        busy_status, busy_body = broker_request("/v1/entries/legacy/actions", {"action": "stop"})
        assert busy_status == 409
        assert busy_body["error"] == "mcp_action_in_progress"

        monkeypatch.setattr(broker_module, "MAX_OPERATIONS", 1)
        bounded = OperationRegistry(_BlockingEngine())
        bounded_engine = bounded.engine
        bounded.start("legacy", "start", None)
        assert bounded_engine.started.wait(timeout=1)
        with pytest.raises(BrokerError) as capacity:
            bounded.start("stateless", "start", None)
        assert capacity.value.code == "mcp_operation_capacity_reached"
        bounded_engine.release.set()

        engine.release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, operation = broker_request(f"/v1/operations/{operation_id}")
            assert status == 200
            if operation["state"] != "running":
                break
            time.sleep(0.01)
        assert operation["state"] == "succeeded"
        assert operation["runtime_state"] == "running"
    finally:
        engine.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_engine_timeout_is_outcome_unknown_and_remains_tracked_until_reconciliation():
    registry = OperationRegistry(_BlockingEngine(failure=TimeoutError("synthetic timeout")))
    started = registry.start("legacy", "start", None)
    result = _wait_operation(registry.get, started["operation_id"])
    assert result["state"] == "outcome_unknown"
    assert result["error"] == "mcp_action_outcome_unknown"
    with pytest.raises(BrokerError) as blocked:
        registry.start("legacy", "stop", None)
    assert blocked.value.code == "mcp_action_outcome_unknown"


def test_entries_reconciles_unknown_only_for_matching_generation_and_target_state():
    generation = "g" * 24

    class ReconcilingEngine:
        def __init__(self):
            self.current_state = "exited"
            self.actions = 0

        def entry(self, entry_id: str):
            return {
                "entry_id": entry_id,
                "runtime_state": self.current_state,
                "generation": generation,
            }

        def control(self, entry_id: str, action: str, expected_generation: str | None):
            self.actions += 1
            if self.actions == 1:
                # Engine调用超时后，容器其实达到了start目标；需entries检查后才解锁。
                self.current_state = "running"
                raise TimeoutError("synthetic reply timeout")
            assert action == "stop"
            assert expected_generation == generation
            self.current_state = "exited"
            return {"runtime_state": "exited", "generation": generation}

    engine = ReconcilingEngine()
    registry = OperationRegistry(engine)
    started = registry.start("legacy", "start", None)
    unknown = _wait_operation(registry.get, started["operation_id"])
    assert unknown["state"] == "outcome_unknown"
    assert unknown["error"] == "mcp_action_outcome_unknown"
    assert registry.operations[unknown["operation_id"]]["generation"] == generation

    # 状态未复核前，另一个动作明确得到outcome_unknown。
    with pytest.raises(BrokerError) as blocked:
        registry.start("legacy", "stop", None)
    assert blocked.value.code == "mcp_action_outcome_unknown"

    entries = registry.entries()
    legacy = next(row for row in entries["entries"] if row["entry_id"] == "legacy")
    assert legacy["runtime_state"] == "running"
    reconciled = registry.get(unknown["operation_id"])
    assert reconciled["state"] == "succeeded"
    assert reconciled["error"] is None
    assert reconciled["generation"] == generation

    followup = registry.start("legacy", "stop", generation)
    completed = _wait_operation(registry.get, followup["operation_id"])
    assert completed["state"] == "succeeded"
    assert completed["runtime_state"] == "exited"


def test_broker_rejects_invalid_token_and_arbitrary_project_or_entry():
    engine = _BlockingEngine()
    with pytest.raises(ValueError):
        MCPControlBroker("short", "wenjin", engine=engine)
    with pytest.raises(ValueError):
        MCPControlBroker(_BROKER_TOKEN, "Invalid project!", engine=engine)
    broker = MCPControlBroker(_BROKER_TOKEN, "wenjin", engine=engine)
    status, body = broker.handle("GET", "/v1/entries/arbitrary", f"Bearer {_BROKER_TOKEN}", b"")
    assert status == 404
    assert json.loads(body)["error"] == "not_found"
    status, body = broker.handle("GET", "/v1/entries", "Bearer wrong-token", b"")
    assert status == 401
    assert json.loads(body)["error"] == "unauthorized"


def test_broker_requires_explicit_valid_project_and_uses_it_for_engine_label_filter(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("WENJIN_MCP_CONTROL_TOKEN", _BROKER_TOKEN)
    monkeypatch.delenv("WENJIN_MCP_CONTROL_PROJECT", raising=False)
    with pytest.raises(ValueError):
        MCPControlBroker.from_env(engine=_BlockingEngine())
    monkeypatch.setenv("WENJIN_MCP_CONTROL_PROJECT", "Not a valid project!")
    with pytest.raises(ValueError):
        MCPControlBroker.from_env(engine=_BlockingEngine())

    project = "synthetic-mcp-project-7"
    monkeypatch.setenv("WENJIN_MCP_CONTROL_PROJECT", project)
    state = _EngineState(project=project)
    server, thread, engine = _serve_fake_engine(state, project=project)
    try:
        broker = MCPControlBroker.from_env(engine=engine)
        status, body = broker.handle("GET", "/v1/entries", f"Bearer {_BROKER_TOKEN}", b"")
        assert status == 200
        assert len(json.loads(body)["entries"]) == 2
        expected = {
            "com.docker.compose.project=" + project,
            "com.docker.compose.service=mcp",
        }
        assert any(
            set(json.loads(query["filters"][0])["label"]) == expected
            for query in state.list_filters
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
