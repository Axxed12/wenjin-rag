"""Opt-in official-image + Qdrant + HTTPS runtime acceptance (synthetic only)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

try:
    if version("fastmcp").split(".", 1)[0] != "4" or version("mcp").split(".", 1)[0] != "2":
        pytest.skip("container acceptance helper requires FastMCP 4 / MCP SDK 2", allow_module_level=True)
except PackageNotFoundError:
    pytest.skip("container acceptance helper requires FastMCP 4 / MCP SDK 2", allow_module_level=True)

pytestmark = [pytest.mark.integration, pytest.mark.live]

IMAGE = "wenjin-mcp-stateless:2026-10-09"
IMAGE_ID = "sha256:1f00cf6f96f22b68302845bad5eb8dbd2e44e976c720edf5029d9849d709fa62"
CLIENT_SCRIPT = Path(__file__).parent / "acceptance" / "mcp-stateless-client" / "client.mjs"
CLIENT_CWD = CLIENT_SCRIPT.parent
REPO = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=capture, text=True, timeout=60, check=False
    )
    if check and result.returncode:
        raise AssertionError(f"docker {' '.join(args[:3])} failed: {result.stderr[-3000:]}")
    return result


def _wait_http(url: str, *, attempts: int = 80, verify: bool | str = True) -> None:
    import httpx

    for _ in range(attempts):
        try:
            response = httpx.get(url, timeout=1, trust_env=False, verify=verify)
            if response.status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise TimeoutError(f"temporary endpoint did not become ready: {url}")


def _run_sdk(url: str, token: str, tool: str, args: dict, ca_file: Path) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "NODE_EXTRA_CA_CERTS": str(ca_file),
        "MCP_STATELESS_URL": url,
        "MCP_STATELESS_TOKEN": token,
        "MCP_STATELESS_TOOL": tool,
        "MCP_STATELESS_ARGUMENTS_JSON": json.dumps(args, separators=(",", ":")),
    }
    result = subprocess.run(
        ["node", str(CLIENT_SCRIPT)], cwd=CLIENT_CWD, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise AssertionError(f"official TS SDK failed: {result.stderr[-3000:]}")
    return json.loads(result.stdout)


def test_official_launcher_qdrant_remote_model_and_caddy_tls(tmp_path):
    if os.getenv("MCP_STATELESS_CONTAINER_ACCEPTANCE") != "1":
        pytest.skip("set MCP_STATELESS_CONTAINER_ACCEPTANCE=1 for disposable Docker acceptance")
    if shutil.which("docker") is None or shutil.which("node") is None:
        pytest.skip("Docker and Node.js are required for this explicit container acceptance")
    image = _docker("image", "inspect", IMAGE, "--format", "{{.Id}}").stdout.strip()
    assert image == IMAGE_ID, f"expected final image {IMAGE_ID}, got {image}"

    run_id = uuid.uuid4().hex[:10]
    names = {key: f"mcp-stateless-{key}-{run_id}" for key in ("qdrant", "deps", "app", "caddy")}
    network = f"mcp-stateless-net-{run_id}"
    qdrant_port = _free_port()
    https_port = _free_port()
    subnet_octet = 100 + int(run_id[:2], 16) % 100
    subnet = f"10.239.{subnet_octet}.0/24"
    # Docker allocates low addresses first to Qdrant/dependency/app containers.
    caddy_ip = f"10.239.{subnet_octet}.250"
    work = Path(tempfile.mkdtemp(prefix=f"mcp-stateless-final-{run_id}-"))
    identity_dir = work / "identity"
    management_identity_dir = work / "management-identity"
    project_root = work / "project"
    management_project_root = work / "management-project"
    caddy_data = work / "caddy-data"
    caddy_config = work / "Caddyfile"
    for path in (identity_dir, management_identity_dir, project_root, management_project_root,
                 caddy_data / "data", caddy_data / "config"):
        path.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    evidence: dict = {
        "image": IMAGE,
        "imageId": IMAGE_ID,
        "client": "@modelcontextprotocol/client 2.3.1",
        "protocolVersion": "2026-07-28",
        "classification": "synthetic Qdrant/index/model/identity; official image CLI; official TS network client; Caddy internal TLS",
        "networkLoopbackPublishedPorts": {"qdrant": qdrant_port, "https": https_port},
    }
    try:
        _docker("network", "create", "--driver", "bridge", "--subnet", subnet, network)
        _docker("run", "-d", "--name", names["qdrant"], "--network", network,
                "--network-alias", "qdrant", "-p", f"127.0.0.1:{qdrant_port}:6333",
                "qdrant/qdrant:v1.19.1")
        created.append(names["qdrant"])
        import httpx

        qdrant_url = f"http://127.0.0.1:{qdrant_port}"
        _wait_http(f"{qdrant_url}/readyz")
        from qdrant_client import QdrantClient

        from tests.support.library_release_fixture import build_two_version_release_fixture

        qdrant = QdrantClient(url=qdrant_url, timeout=10)
        fixture = build_two_version_release_fixture(
            root=project_root, library_id="osg", qdrant_client=qdrant
        )
        evidence["syntheticFixture"] = {
            "library": "osg", "activeVersion": fixture["targets"]["A"].version,
            "marker": "release_marker_A", "collections": fixture["collections"],
        }

        from wenjin.identity import IdentityStore

        db_path = identity_dir / "identity.sqlite"
        store = IdentityStore(db_path)
        store.initialize()
        store.bootstrap_first_admin("synthetic-admin", "Temp_Admin_2026!x")
        admin = store.login("synthetic-admin", "Temp_Admin_2026!x").principal
        store.create_user(admin, "synthetic-reader", "Temp_Reader_2026!x", role="reader")
        first_login = store.login("synthetic-reader", "Temp_Reader_2026!x")
        store.change_password(first_login.token, "Temp_Reader_2026!x", "Ready_Reader_2026!x")
        reader = store.login("synthetic-reader", "Ready_Reader_2026!x").principal
        credential = store.issue_mcp_credential(reader, label="temporary-stateless-acceptance")
        store.synchronize_resource("library", "osg", active=True)
        store.set_grant(admin, resource_kind="library", resource_id="osg",
                        user_id=reader.user_id, allowed=True, expected_revision=0)
        with store._connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert journal_mode.lower() == "wal"
        assert not Path(f"{db_path}-wal").exists() and not Path(f"{db_path}-shm").exists()
        db_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
        evidence["identity"] = {"journalMode": journal_mode, "sidecarsInitiallyAbsent": True,
                                 "mainDbSha256BeforeReads": db_sha,
                                 "readerCredentialPresent": True, "grantInitially": True}

        management_db = management_identity_dir / "identity.sqlite"
        management_store = IdentityStore(management_db)
        management_store.initialize()
        management_store.bootstrap_first_admin("synthetic-web-admin", "Temp_Web_Admin_2026!x")

        deps_script = Path(__file__).parent / "acceptance" / "mcp-stateless-client" / "serve_synthetic_model_management.py"
        qonly_script = Path(__file__).parent / "acceptance" / "mcp-stateless-client" / "check_queryonly_store.py"
        _docker("run", "-d", "--name", names["deps"], "--network", network,
                "--network-alias", "synthetic-dependencies", "--network-alias", "mcp",
                "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                "-v", f"{deps_script}:/tmp/serve_synthetic.py:ro", IMAGE,
                "python", "/tmp/serve_synthetic.py")
        created.append(names["deps"])
        _wait_http("http://127.0.0.1:1/", attempts=1) if False else None
        for _ in range(80):
            probe = _docker("exec", names["deps"], "python", "-c",
                            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/status',timeout=1)",
                            check=False)
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            logs = _docker("logs", names["deps"], check=False).stdout
            raise TimeoutError(f"temporary remote-model/management app did not start: {logs[-3000:]}")

        management_script = Path(__file__).parent / "acceptance" / "mcp-stateless-client" / "serve_synthetic_management_runtime.py"
        management_container = f"{names['app']}-management"
        _docker("run", "-d", "--name", management_container, "--network", network,
                "--network-alias", "management", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=96m",
                "-v", f"{management_identity_dir}:/var/lib/management:rw",
                "-v", f"{management_project_root}:/app/management-project:rw",
                "-v", f"{management_script}:/tmp/serve_management.py:ro",
                "-e", "QDRANT_URL=http://qdrant:6333",
                "-e", "QDRANT_API_KEY=synthetic-only",
                "-e", "WENJIN_MODEL_PROVIDER=remote",
                "-e", "WENJIN_MODEL_API_URL=http://synthetic-dependencies:8000",
                "-e", "WENJIN_MODEL_API_TOKEN=temporary-synthetic-model-api-token-2026",
                "-e", "EMBEDDING_MODEL_ID=synthetic-marker-embedder-v1",
                "-e", "RERANKER_MODEL_ID=synthetic-marker-reranker-v1",
                "-e", f"FORWARDED_ALLOW_IPS={caddy_ip}",
                IMAGE, "python", "/tmp/serve_management.py")
        created.append(management_container)
        for _ in range(100):
            probe = _docker("exec", management_container, "python", "-c",
                            "import urllib.error,urllib.request;\n"
                            "try: urllib.request.urlopen('http://127.0.0.1:8777/api/auth/mode',timeout=1)\n"
                            "except urllib.error.HTTPError: pass",
                            check=False)
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            logs = _docker("logs", management_container, check=False).stdout
            raise TimeoutError(f"temporary real management app did not start: {logs[-3000:]}")

        _docker("run", "-d", "--name", names["app"], "--network", network,
                "--network-alias", "mcp-stateless", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "-v", f"{identity_dir}:/var/lib/wenjin:rw",
                "-v", f"{project_root}:/app/project:ro",
                "-v", f"{qonly_script}:/tmp/check_queryonly_store.py:ro",
                "-e", "QDRANT_URL=http://qdrant:6333",
                "-e", "QDRANT_API_KEY=synthetic-only",
                "-e", "WENJIN_MODEL_PROVIDER=remote",
                "-e", "WENJIN_MODEL_API_URL=http://synthetic-dependencies:8000",
                "-e", "WENJIN_MODEL_API_TOKEN=temporary-synthetic-model-api-token-2026",
                "-e", "EMBEDDING_MODEL_ID=synthetic-marker-embedder-v1",
                "-e", "RERANKER_MODEL_ID=synthetic-marker-reranker-v1",
                "-e", "WENJIN_FILE_MANAGEMENT_URL=http://synthetic-dependencies:8000",
                "-e", "WENJIN_FILE_MANAGEMENT_ALLOW_PRIVATE_HTTP=1",
                "-e", "WENJIN_MCP_ALLOW_PRIVATE_HTTP=1",
                "-e", f"WENJIN_MCP_STATELESS_ALLOWED_HOSTS=localhost,localhost:{https_port}",
                "-e", f"FORWARDED_ALLOW_IPS={caddy_ip}",
                "-e", "WENJIN_AUDIT_SINK=stdout",
                "-e", "WENJIN_AUDIT_INCLUDE_QUERY=0",
                IMAGE, "python", "-m", "wenjin.mcp.stateless_server",
                "--host", "0.0.0.0", "--port", "18768",
                "--identity-db", "/var/lib/wenjin/identity.sqlite",
                "--project-root", "/app/project")
        created.append(names["app"])
        for _ in range(80):
            probe = _docker(
                "exec", names["app"], "python", "-c",
                "import urllib.error,urllib.request;\n"
                "try: urllib.request.urlopen('http://127.0.0.1:18768/mcp-stateless',timeout=1)\n"
                "except urllib.error.HTTPError: pass",
                check=False,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            logs = _docker("logs", names["app"], check=False).stdout
            raise TimeoutError(f"official stateless launcher did not start: {logs[-3000:]}")

        production_caddyfile = (REPO / "deploy" / "Caddyfile.stateless").read_text(encoding="utf-8")
        test_caddyfile = production_caddyfile.replace("{$PUBLIC_DOMAIN}", "localhost")
        test_caddyfile = test_caddyfile.replace("localhost {", "localhost {\n\ttls internal", 1)
        # The legacy test upstream shares a synthetic container with the model API,
        # so only its service port is adapted; route matchers/ordering stay production-exact.
        test_caddyfile = test_caddyfile.replace("mcp:8765", "mcp:8000")
        caddy_config.write_text(test_caddyfile, encoding="utf-8")
        _docker("run", "-d", "--name", names["caddy"], "--network", network,
                "--ip", caddy_ip, "-p", f"127.0.0.1:{https_port}:443",
                "-v", f"{caddy_config}:/etc/caddy/Caddyfile:ro",
                "-v", f"{caddy_data / 'data'}:/data",
                "-v", f"{caddy_data / 'config'}:/config", "caddy:2.10-alpine")
        created.append(names["caddy"])
        _wait_http(f"https://localhost:{https_port}/status", attempts=80, verify=False)
        ca_file = work / "root.crt"
        _docker("cp", f"{names['caddy']}:/data/caddy/pki/authorities/local/root.crt", str(ca_file))
        modern_url = f"https://localhost:{https_port}/mcp-stateless"
        result = _run_sdk(modern_url, credential.token, "search_docs",
                          {"query": "release_marker_A", "library": "osg", "top_k": 1}, ca_file)
        call = result["callResult"]
        assert call["isError"] is False
        assert call["structuredContent"]["mcp_protocol"] == "2026-07-28"
        assert "release_marker_A" in json.dumps(call["structuredContent"])
        assert result["protocolEra"] == "modern"
        assert result["observedResponses"][-1]["resultType"] == "complete"
        evidence_items = call["structuredContent"].get("evidence", [])
        assert evidence_items, "successful synthetic search must return source evidence"
        download_path = evidence_items[0].get("download_path")
        assert isinstance(download_path, str) and download_path.startswith("/mcp-stateless/files/")
        download_url = f"https://localhost:{https_port}{download_path}"
        downloaded = httpx.get(
            download_url, headers={"Authorization": f"Bearer {credential.token}"},
            verify=str(ca_file), timeout=15,
        )
        raw_source = project_root / "raw_data" / "osg" / "1.0" / "release-guide.txt"
        expected_source = raw_source.read_bytes()
        assert downloaded.status_code == 200, downloaded.text[:500]
        source_sha = hashlib.sha256(expected_source).hexdigest()
        assert hashlib.sha256(downloaded.content).hexdigest() == source_sha
        assert "release_marker_A" in downloaded.text
        assert "no-store" in downloaded.headers.get("cache-control", "").lower()
        evidence["officialTsSearch"] = {
            "passed": True, "resultType": result["observedResponses"][-1]["resultType"],
            "isError": call["isError"], "markerObserved": True,
            "protocol": call["structuredContent"]["mcp_protocol"],
            "https": True, "tlsCaFile": "temporary Caddy internal root (not retained in evidence)",
            "entrypoint": "python -m wenjin.mcp.stateless_server",
            "download": {"status": downloaded.status_code, "sha256": source_sha,
                         "matchesFixtureBytes": True, "noStore": True,
                         "pathIncludesReleaseId": "release_id=" in download_path},
        }
        with store._connect() as connection:
            identity_sha_after_read = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert identity_sha_after_read == db_sha
        evidence["identity"]["firstNoWriterSegmentHashUnchanged"] = True
        query_only = _docker("exec", names["app"], "python", "/tmp/check_queryonly_store.py")
        query_only_result = json.loads(query_only.stdout.strip().splitlines()[-1])
        assert query_only_result == {"initializeRejected": True, "queryOnly": 1,
                                     "writeRejected": True}
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == db_sha
        evidence["queryOnlyStore"] = query_only_result
        evidence["identity"]["hashUnchangedAfterQueryOnlyProbe"] = True

        revoked_grant = store.set_grant(admin, resource_kind="library", resource_id="osg",
                                        user_id=reader.user_id, allowed=False, expected_revision=1)
        after_revoke_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
        revoked = _run_sdk(modern_url, credential.token, "list_corpora", {}, ca_file)
        revoked_corpora = revoked["callResult"]["structuredContent"].get("corpora", [])
        assert revoked_corpora == [], revoked_corpora
        denied_download = httpx.get(
            download_url, headers={"Authorization": f"Bearer {credential.token}"},
            verify=str(ca_file), timeout=15,
        )
        assert denied_download.status_code == 403, denied_download.text[:500]
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == after_revoke_sha
        evidence["grantRevocation"] = {
            "writerRevision": revoked_grant.revision, "nextRequestObserved": True,
            "corpusCount": len(revoked_corpora), "corpora": revoked_corpora,
            "deniedDownloadStatus": denied_download.status_code,
            "mainDbHashUnchangedDuringRead": True, "mainDbSha256AfterWriter": after_revoke_sha,
        }

        restored_grant = store.set_grant(admin, resource_kind="library", resource_id="osg",
                                         user_id=reader.user_id, allowed=True, expected_revision=2)
        after_regrant_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
        restored = _run_sdk(modern_url, credential.token, "list_corpora", {}, ca_file)
        restored_corpora = restored["callResult"]["structuredContent"].get("corpora", [])
        assert len(restored_corpora) == 1
        restored_download = httpx.get(
            download_url, headers={"Authorization": f"Bearer {credential.token}"},
            verify=str(ca_file), timeout=15,
        )
        assert restored_download.status_code == 200
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == after_regrant_sha
        evidence["grantWriterUpdate"] = {
            "writerRevision": restored_grant.revision,
            "nextRequestCorpusCount": len(restored_corpora),
            "visibleLibraries": sorted({str(item.get("library", item.get("library_id", "")))
                                         for item in restored_corpora}),
            "downloadStatus": restored_download.status_code,
            "mainDbHashUnchangedDuringRead": True, "mainDbSha256AfterWriter": after_regrant_sha,
        }

        route_checks = {}
        for route in ("/mcp", "/files/probe"):
            response = httpx.get(f"https://localhost:{https_port}{route}", verify=str(ca_file), timeout=10)
            assert response.status_code == 200
            assert response.json() == {"syntheticUpstream": route}
            route_checks[route] = response.json()
        evidence["caddySyntheticUpstreamProxyChecks"] = route_checks

        origin = f"https://localhost:{https_port}"
        with httpx.Client(verify=str(ca_file), trust_env=False, timeout=15) as web:
            login = web.post(
                f"{origin}/api/auth/login", headers={"Origin": origin},
                json={"username": "synthetic-web-admin", "password": "Temp_Web_Admin_2026!x"},
            )
            assert login.status_code == 200, login.text[:500]
            set_cookie = login.headers.get("set-cookie", "")
            assert "HttpOnly" in set_cookie and "Secure" in set_cookie
            csrf = login.json()["csrf_token"]
            no_csrf = web.post(
                f"{origin}/api/auth/credentials", headers={"Origin": origin},
                json={"label": "synthetic-proxy-no-csrf"},
            )
            assert no_csrf.status_code == 403
            wrong_origin = web.post(
                f"{origin}/api/auth/credentials",
                headers={"Origin": "https://foreign.invalid", "X-CSRF-Token": csrf},
                json={"label": "synthetic-proxy-wrong-origin"},
            )
            assert wrong_origin.status_code == 403
            issued = web.post(
                f"{origin}/api/auth/credentials",
                headers={"Origin": origin, "X-CSRF-Token": csrf},
                json={"label": "synthetic-proxy-issued-credential"},
            )
            assert issued.status_code == 201, issued.text[:500]
        evidence["managementHttpsAuth"] = {
            "actualCreateApp": True, "loginStatus": login.status_code,
            "cookieSecure": "Secure" in set_cookie,
            "cookieHttpOnly": "HttpOnly" in set_cookie,
            "sessionCookiePersisted": bool(login.cookies),
            "wrongOriginStatus": wrong_origin.status_code,
            "missingCsrfStatus": no_csrf.status_code,
            "validCsrfCredentialIssueStatus": issued.status_code,
        }

        status = _docker("exec", names["deps"], "python", "-c",
                         "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/status').read().decode())")
        dependency_status = json.loads(status.stdout.strip())
        evidence["syntheticDependencyCounters"] = {
            "counts": dependency_status["counts"],
            "personalAuthorizationObserved": dependency_status["personalAuthorizationObserved"],
        }
        assert dependency_status["personalAuthorizationObserved"] is True
        evidence["databaseSha256AfterNoWriterSegments"] = hashlib.sha256(db_path.read_bytes()).hexdigest()

        evidence_path = REPO / "artifacts" / "iterations" / "2026-10-09-mcp-stateless" / "docker-launcher-tls-runtime.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        for name in reversed([*created, *names.values()]):
            _docker("rm", "-f", name, check=False)
        _docker("network", "rm", network, check=False)
        # Avoid deleting a path that Docker may still hold if container cleanup failed.
        if not any(_docker("inspect", name, check=False).returncode == 0 for name in created):
            shutil.rmtree(work)
