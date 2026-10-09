"""Disposable Docker image + TLS proxy + official TS client acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from wenjin.identity.store import IdentityStore


pytestmark = [pytest.mark.integration, pytest.mark.live]
ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "tests" / "acceptance" / "mcp-stateless-client"
IMAGE = os.getenv("MCP_STATELESS_IMAGE", "wenjin-mcp-stateless:2026-10-09")


def _run(args: list[str], *, timeout: int = 30, check: bool = True,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            check=False, env=env)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]} {result.stderr[-2000:]}")
    return result


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_tls_fixture(folder: Path) -> Path:
    config_path = folder / "Caddyfile"
    config_path.write_text(
        "localhost {\n"
        "  tls internal\n"
        "  reverse_proxy mcp-stateless:18768 {\n"
        "    header_up Host 127.0.0.1:443\n"
        "    header_up X-Forwarded-Proto https\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    return config_path


def _snapshot_identity_folder(folder: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.iterdir()) if path.is_file()
    }


def _make_manifest(project_root: Path) -> None:
    manifest_dir = project_root / "corpora" / "synthetic-library-1.0"
    manifest_dir.mkdir(parents=True)
    payload = {
        "schema_version": 3,
        "corpus_id": "synthetic-library-1.0",
        "library": "synthetic-library",
        "version": "1.0",
        "license": "synthetic-test-only",
        "source_url": "urn:synthetic:container-tls",
        "raw_data_subdir": "synthetic-library/1.0",
        "files": [{"path": "guide.txt", "raw_sha256": "0" * 64,
                   "bytes": 0, "indexable": True, "source_url": "urn:synthetic:guide"}],
    }
    (manifest_dir / "source-manifest.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def test_released_image_over_readonly_identity_volume_and_disposable_tls_proxy(tmp_path):
    if os.getenv("MCP_STATELESS_READONLY_FAILURE_PROBE") != "1":
        pytest.skip(
            "historical read-only SQLite WAL failure probe; set "
            "MCP_STATELESS_READONLY_FAILURE_PROBE=1 only to reproduce it"
        )
    if _run(["docker", "image", "inspect", IMAGE], check=False).returncode:
        pytest.skip(f"released stateless image is unavailable: {IMAGE}")
    node = _run(["node", "--version"], check=False)
    if node.returncode:
        pytest.skip("Node.js is required for the official TypeScript SDK acceptance")

    suffix = uuid.uuid4().hex[:10]
    network = f"mcp-stateless-test-{suffix}"
    app_name, proxy_name = f"mcp-stateless-app-{suffix}", f"mcp-stateless-proxy-{suffix}"
    evidence = {
        "classification": "synthetic-released-image-caddy-tls-official-ts-sdk",
        "image": IMAGE,
        "identityMount": "temporary-real-IdentityStore-read-only",
        "tls": "disposable-Caddy-internal-CA-trusted-by-NODE_EXTRA_CA_CERTS",
        "checks": {},
    }
    result_failure = None
    with tempfile.TemporaryDirectory(prefix="mcp-stateless-docker-accept-", ignore_cleanup_errors=True) as temp:
        root = Path(temp)
        identity_dir = root / "identity"
        identity_dir.mkdir()
        db = identity_dir / "identity.sqlite"
        store = IdentityStore(db)
        store.initialize()
        store.bootstrap_first_admin("tls-admin", "TemporaryTestPassword_2026!")
        admin = store.login("tls-admin", "TemporaryTestPassword_2026!").principal
        store.create_user(admin, "tls-reader", "TemporaryReaderPassword_2026!", role="reader")
        first_login = store.login("tls-reader", "TemporaryReaderPassword_2026!")
        store.change_password(first_login.token, "TemporaryReaderPassword_2026!", "TemporaryReaderPassword2_2026!")
        reader = store.login("tls-reader", "TemporaryReaderPassword2_2026!").principal
        credential = store.issue_mcp_credential(reader, label="temporary-container-tls")
        resource_revision = store.synchronize_resource("library", "synthetic-library", active=True)
        if resource_revision != 1:
            raise AssertionError("synthetic library resource did not start at revision 1")
        connection = __import__("sqlite3").connect(db)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()
        if journal_mode.lower() != "wal":
            raise AssertionError(f"IdentityStore did not retain its WAL mode: {journal_mode}")
        evidence["checks"]["identityJournalModeBeforeMount"] = journal_mode
        sidecars_before = [name for name in ("identity.sqlite-wal", "identity.sqlite-shm")
                           if (identity_dir / name).exists()]
        if sidecars_before:
            raise AssertionError(f"temporary identity DB still has SQLite sidecars: {sidecars_before}")
        db_hash_before = hashlib.sha256(db.read_bytes()).hexdigest()

        project_root = root / "project"
        project_root.mkdir()
        _make_manifest(project_root)
        caddyfile = _write_tls_fixture(root)
        caddy_data = root / "caddy-data"
        caddy_data.mkdir()
        port = _free_port()
        _run(["docker", "network", "create", network])
        try:
            _run([
                "docker", "run", "-d", "--name", app_name, "--network", network,
                "--network-alias", "mcp-stateless", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                "--mount", f"type=bind,source={identity_dir},target=/var/lib/wenjin,readonly",
                "--mount", f"type=bind,source={CLIENT_DIR / 'serve_image_synthetic.py'},target=/tmp/serve_image_synthetic.py,readonly",
                "--mount", f"type=bind,source={project_root / 'corpora'},target=/app/corpora,readonly",
                "--entrypoint", "python", IMAGE, "/tmp/serve_image_synthetic.py",
            ])
            _run([
                "docker", "run", "-d", "--name", proxy_name, "--network", network,
                "-p", f"127.0.0.1:{port}:443",
                "--mount", f"type=bind,source={caddyfile},target=/etc/caddy/Caddyfile,readonly",
                "--mount", f"type=bind,source={caddy_data},target=/data",
                "--tmpfs", "/config:rw,nosuid,nodev,size=16m",
                "--entrypoint", "caddy", "caddy:2.10-alpine", "run", "--config", "/etc/caddy/Caddyfile",
            ])

            import httpx

            url = f"https://localhost:{port}/mcp-stateless"
            deadline = time.monotonic() + 30
            last_error = None
            with httpx.Client(
                verify=False, timeout=3, trust_env=False,
            ) as http_client:
                while time.monotonic() < deadline:
                    try:
                        response = http_client.post(
                            url,
                            headers={"Authorization": f"Bearer {credential.token}",
                                     "MCP-Protocol-Version": "2026-07-28",
                                     "Mcp-Method": "server/discover",
                                     "Accept": "application/json, text/event-stream",
                                     "Content-Type": "application/json"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "server/discover",
                                  "params": {"_meta": {
                                      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                      "io.modelcontextprotocol/clientCapabilities": {},
                                  }}},
                        )
                        if response.status_code >= 200:
                            break
                        last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    except Exception as exc:  # noqa: BLE001
                        last_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                    time.sleep(0.25)
                else:
                    caddy_logs = _run(["docker", "logs", proxy_name], check=False).stderr
                    caddy_logs += _run(["docker", "logs", proxy_name], check=False).stdout
                    app_logs = _run(["docker", "logs", app_name], check=False).stderr
                    app_logs += _run(["docker", "logs", app_name], check=False).stdout
                    raise RuntimeError(
                        f"temporary HTTPS proxy did not become ready: {last_error}; "
                        f"caddy={caddy_logs[-1200:]}; app={app_logs[-1200:]}"
                    )

            ca_path = root / "caddy-root-ca.pem"
            _run(["docker", "cp", f"{proxy_name}:/data/caddy/pki/authorities/local/root.crt", str(ca_path)])
            evidence["tlsRootCaSha256"] = hashlib.sha256(ca_path.read_bytes()).hexdigest()
            identity_after_first_auth = _snapshot_identity_folder(identity_dir)
            evidence["checks"]["firstAuthWithoutWalSidecars"] = {
                "status": response.status_code,
                "passed": response.status_code == 200,
                "sidecarsBefore": sidecars_before,
                "identityUnchangedDuringRead": identity_after_first_auth == {"identity.sqlite": db_hash_before},
                "body": response.text[:300] if response.status_code != 200 else "ok",
            }
            if response.status_code != 200:
                result_failure = (
                    "formal image app could not authenticate against WAL IdentityStore on a read-only mount "
                    "when the writer had closed and no -wal/-shm sidecars existed"
                )

            sdk_env = {
                "PATH": os.environ.get("PATH", ""),
                "SystemRoot": os.environ.get("SystemRoot", ""),
                "TEMP": os.environ.get("TEMP", ""),
                "MCP_STATELESS_URL": url,
                "MCP_STATELESS_TOKEN": credential.token,
                "MCP_STATELESS_TOOL": "list_corpora",
                "MCP_STATELESS_ARGUMENTS_JSON": "{}",
                "NODE_EXTRA_CA_CERTS": str(ca_path),
            }
            # Writer is the same temporary identity store, closed on every transaction.
            identity_before_writer = identity_after_first_auth
            grant = store.set_grant(
                admin, resource_kind="library", resource_id="synthetic-library",
                user_id=reader.user_id, allowed=True, expected_revision=0,
            )
            evidence["checks"]["writerGrantRevision"] = grant.revision
            identity_after_writer = _snapshot_identity_folder(identity_dir)
            evidence["checks"]["identityFilesAfterWriter"] = identity_after_writer
            sdk_after = _run(["node", str(CLIENT_DIR / "client.mjs")], env=sdk_env,
                             timeout=45, check=False)
            if sdk_after.returncode:
                visible = []
                evidence["checks"]["officialSdkCallAfterGrant"] = {
                    "passed": False,
                    "returncode": sdk_after.returncode,
                    "diagnostic": sdk_after.stderr[-1500:].replace(credential.token, "[redacted]"),
                }
                result_failure = result_failure or "official TS client could not call the read-only mounted service after the writer grant"
            else:
                after_result = json.loads(sdk_after.stdout)
                visible = after_result["callResult"]["structuredContent"].get("corpora", [])
                evidence["checks"]["officialSdkCallAfterGrant"] = {
                    "passed": True,
                    "clientVersion": after_result["clientVersion"],
                    "status": after_result["observedResponses"][-1]["status"],
                    "resultType": after_result["observedResponses"][-1]["resultType"],
                    "cacheControl": after_result["observedResponses"][-1]["cacheControl"],
                }
            identity_after_read = _snapshot_identity_folder(identity_dir)
            evidence["checks"]["grantVisibleOnNextReadOnlyMountedRequest"] = {
                "passed": any(item.get("library") == "synthetic-library" for item in visible),
                "visibleLibraries": sorted({str(item.get("library")) for item in visible}),
                "writerSidecarsAfterGrant": [name for name in ("identity.sqlite-wal", "identity.sqlite-shm")
                                              if (identity_dir / name).exists()],
                "identityUnchangedByRead": identity_after_read == identity_after_writer,
            }
            if not evidence["checks"]["grantVisibleOnNextReadOnlyMountedRequest"]["identityUnchangedByRead"]:
                result_failure = "read-only container changed identity DB/sidecar bytes during post-grant request"
            if not evidence["checks"]["grantVisibleOnNextReadOnlyMountedRequest"]["passed"]:
                result_failure = "IdentityStore writer grant was not visible on the next ro-mounted MCP request"

            evidence["checks"]["identityFilesUnchangedByPostWriterRead"] = identity_after_read == identity_after_writer
            evidence["checks"]["sidecarsAbsentBeforeFirstContainerAuth"] = not sidecars_before

            if sdk_after.returncode == 0:
                revoked = store.set_grant(
                    admin, resource_kind="library", resource_id="synthetic-library",
                    user_id=reader.user_id, allowed=False, expected_revision=1,
                )
                identity_after_revoke_writer = _snapshot_identity_folder(identity_dir)
                sdk_revoked = _run(["node", str(CLIENT_DIR / "client.mjs")], env=sdk_env,
                                   timeout=45, check=False)
                if sdk_revoked.returncode == 0:
                    revoked_result = json.loads(sdk_revoked.stdout)
                    revoked_corpora = revoked_result["callResult"]["structuredContent"].get("corpora", [])
                    revoked_visible = not any(item.get("library") == "synthetic-library" for item in revoked_corpora)
                else:
                    revoked_visible = False
                post_revoke_files = _snapshot_identity_folder(identity_dir)
                evidence["checks"]["grantRevocationVisibleOnNextReadOnlyMountedRequest"] = {
                    "revision": revoked.revision,
                    "passed": revoked_visible,
                    "sdkCompleted": sdk_revoked.returncode == 0,
                    "identityUnchangedByRead": post_revoke_files == identity_after_revoke_writer,
                }
                if not revoked_visible:
                    result_failure = "revoked library grant remained visible or SDK read failed on next ro-mounted request"

            # Keep the transport/TLS result separate from the failing read-only WAL
            # condition: replace the account runner with a static synthetic app in the
            # same released image and verify the pinned SDK over the same TLS proxy.
            _run(["docker", "rm", "-f", app_name], check=False)
            _run([
                "docker", "run", "-d", "--name", app_name, "--network", network,
                "--network-alias", "mcp-stateless", "--read-only",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                "--mount", f"type=bind,source={CLIENT_DIR / 'serve_image_static.py'},target=/tmp/serve_image_static.py,readonly",
                "--entrypoint", "python", IMAGE, "/tmp/serve_image_static.py",
            ])
            _run(["docker", "rm", "-f", proxy_name], check=False)
            _run([
                "docker", "run", "-d", "--name", proxy_name, "--network", network,
                "-p", f"127.0.0.1:{port}:443",
                "--mount", f"type=bind,source={caddyfile},target=/etc/caddy/Caddyfile,readonly",
                "--mount", f"type=bind,source={caddy_data},target=/data",
                "--tmpfs", "/config:rw,nosuid,nodev,size=16m",
                "--entrypoint", "caddy", "caddy:2.10-alpine", "run", "--config", "/etc/caddy/Caddyfile",
            ])
            time.sleep(1)
            static_env = {
                **sdk_env,
                "MCP_STATELESS_TOKEN": "short-lived-static-image-probe-only",
            }
            static_sdk = _run(["node", str(CLIENT_DIR / "client.mjs")], env=static_env,
                              timeout=45, check=False)
            if static_sdk.returncode == 0:
                static_result = json.loads(static_sdk.stdout)
                evidence["checks"]["officialTsClientOverTlsToReleasedImage"] = {
                    "passed": True,
                    "clientVersion": static_result["clientVersion"],
                    "status": static_result["observedResponses"][-1]["status"],
                    "tool": "list_corpora",
                    "resultType": static_result["observedResponses"][-1]["resultType"],
                }
            else:
                evidence["checks"]["officialTsClientOverTlsToReleasedImage"] = {
                    "passed": False,
                    "returncode": static_sdk.returncode,
                    "diagnostic": static_sdk.stderr[-1200:].replace(
                        "short-lived-static-image-probe-only", "[redacted]"
                    ),
                }
                result_failure = result_failure or "official TS client failed over disposable TLS proxy to released image"

            app_logs = _run(["docker", "logs", app_name], check=False).stdout[-2500:]
            evidence["containerLogTailRedacted"] = [line for line in app_logs.splitlines()
                                                     if "synthetic" in line.lower() or "error" in line.lower()][-8:]
        finally:
            _run(["docker", "rm", "-f", proxy_name], check=False)
            _run(["docker", "rm", "-f", app_name], check=False)
            _run(["docker", "network", "rm", network], check=False)

    evidence_path = os.getenv("MCP_STATELESS_DOCKER_EVIDENCE_OUTPUT")
    if evidence_path:
        target = Path(evidence_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    if result_failure:
        pytest.fail(result_failure)
