"""SDK-level separation checks between the legacy and modern MCP processes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "tests" / "acceptance" / "mcp-stateless-client"
LEGACY_PYTHON_DEFAULT = Path(r"E:\日常任务\rag-project\.venv\Scripts\python.exe")


def _base_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "PYTHONPATH": str(ROOT / "src"),
    }
    return {key: value for key, value in env.items() if value}


def _start_server(script: Path, python: Path, ready_pattern: str) -> tuple[subprocess.Popen, str]:
    process = subprocess.Popen(
        [str(python), str(script)],
        cwd=ROOT,
        env=_base_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    collected: list[str] = []
    ready = re.compile(ready_pattern)
    result: list[str] = []
    errors: list[BaseException] = []

    def read_startup() -> None:
        try:
            for line in process.stdout:
                collected.append(line.rstrip())
                match = ready.search(line)
                if match:
                    result.append(match.group(1))
                    return
                if process.poll() is not None:
                    return
        except BaseException as error:  # ensure a blocked pipe read has a bounded join
            errors.append(error)

    reader = threading.Thread(target=read_startup, daemon=True)
    reader.start()
    reader.join(timeout=20)
    if reader.is_alive() or errors or not result:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise RuntimeError(f"synthetic listener did not become ready: {collected} {errors}")
    return process, result[0]


def _stop(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _run_json(command: list[str], *, env: dict[str, str], cwd: Path) -> dict:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout)


def test_official_sdk_clients_keep_the_two_protocol_eras_isolated(tmp_path):
    legacy_python = Path(os.environ.get("MCP_LEGACY_PYTHON", str(LEGACY_PYTHON_DEFAULT)))
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    default_modern_python = (
        Path(temp) / "codex-mcp-stateless-probe-20261009" / "Scripts" / "python.exe"
        if temp else None
    )
    modern_python = (
        Path(os.environ["MCP_MODERN_PYTHON"])
        if os.environ.get("MCP_MODERN_PYTHON") else default_modern_python
    )
    if not legacy_python.is_file():
        pytest.skip(f"set MCP_LEGACY_PYTHON to a read-only MCP SDK 1.x environment; missing {legacy_python}")
    if modern_python is None or not modern_python.is_file():
        pytest.skip("set MCP_MODERN_PYTHON or provision the isolated MCP 2.x probe environment")

    old_process = modern_process = None
    try:
        old_process, old_url = _start_server(
            CLIENT_DIR / "serve_synthetic_legacy.py",
            legacy_python,
            r"synthetic legacy MCP ready at (http://\S+/mcp)",
        )
        modern_process, modern_url = _start_server(
            CLIENT_DIR / "serve_synthetic.py",
            modern_python,
            r"synthetic MCP ready at (http://\S+/mcp-stateless)",
        )

        legacy_success = _run_json(
            [str(legacy_python), str(CLIENT_DIR / "legacy_client.py"), old_url],
            env=_base_env(),
            cwd=ROOT,
        )
        assert legacy_success["negotiatedProtocol"] == "2025-11-25"
        assert legacy_success["sessionPresent"] is True
        assert legacy_success["callIsError"] is False
        assert legacy_success["callProtocol"] == "2025-11-25"
        assert {"search_docs", "list_corpora", "list_managed_files"}.issubset(
            legacy_success["toolNames"]
        )

        # Modern SDK is pinned to the fixed modern protocol. It probes discovery,
        # fails against the old endpoint, and must never issue initialize.
        modern_env = {
            **_base_env(),
            "MCP_STATELESS_URL": old_url,
            "MCP_STATELESS_TOKEN": "synthetic-only-legacy-token-2026",
            "MCP_STATELESS_TOOL": "list_corpora",
            "MCP_STATELESS_EXPECT_CONNECT_FAILURE": "true",
        }
        modern_against_old = _run_json(
            ["node", str(CLIENT_DIR / "client.mjs")],
            env=modern_env,
            cwd=CLIENT_DIR,
        )
        methods = [item["bodyMethod"] for item in modern_against_old["observedRequests"]]
        assert modern_against_old["expectedConnectFailure"] is True
        assert "server/discover" in methods
        assert "initialize" not in methods

        # SDK 1.29 performs its real initialize handshake against the modern-only
        # entry. The endpoint rejects it instead of creating a legacy session.
        legacy_against_modern = _run_json(
            [
                str(legacy_python), str(CLIENT_DIR / "legacy_client.py"),
                modern_url, "--expect-failure",
            ],
            env=_base_env(),
            cwd=ROOT,
        )
        assert legacy_against_modern["accepted"] is False
        assert legacy_against_modern["sessionPresent"] is False

        evidence = {
            "classification": "synthetic-two-process-sdk-era-isolation",
            "legacy": {
                "sdkVersion": legacy_success["sdkVersion"],
                "negotiatedProtocol": legacy_success["negotiatedProtocol"],
                "sessionIssued": legacy_success["sessionPresent"],
                "toolCallError": legacy_success["callIsError"],
                "toolResultProtocol": legacy_success["callProtocol"],
                "tools": legacy_success["toolNames"],
            },
            "modernClientAgainstLegacy": {
                "sdkVersion": modern_against_old["clientVersion"],
                "connectFailed": modern_against_old["expectedConnectFailure"],
                "methods": methods,
                "autoFallbackInitialize": "initialize" in methods,
            },
            "legacyClientAgainstModern": {
                "sdkVersion": legacy_against_modern["sdkVersion"],
                "initializeRejected": legacy_against_modern["accepted"] is False,
                "sessionIssued": legacy_against_modern["sessionPresent"],
                "errorType": legacy_against_modern.get("errorType"),
            },
            "endpoints": {"legacyPath": "/mcp", "modernPath": "/mcp-stateless"},
        }
        evidence_path = os.getenv("MCP_STATELESS_ERA_EVIDENCE_OUTPUT")
        if evidence_path:
            target = Path(evidence_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    finally:
        _stop(modern_process)
        _stop(old_process)
