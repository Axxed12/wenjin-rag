from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from scripts.operations.team_delivery import (
    DeliveryError,
    _run_logged_child,
    collect_owned_docker_diagnostics,
    create_backup_bundle,
    create_offline_manifest,
    launch_managed_service,
    restore_backup_bundle,
    verify_backup_bundle,
    verify_offline_manifest,
)
from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.file_management import FileManagementService
from wenjin.identity.store import IdentityStore
from wenjin.index_registry import IndexRegistryStore
from wenjin.management_http.app import create_app
from wenjin.releases.catalog import ReleaseCatalog
from wenjin.releases.resolver import RequestReleaseResolver, release_catalog_path
from wenjin.releases.service import ReleaseService

pytestmark = pytest.mark.integration


class _ManagedEmbedder:
    dimension = 32

    @classmethod
    def _vector(cls, text: str) -> list[float]:
        vector = [0.0] * cls.dimension
        for token in text.lower().split():
            vector[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % cls.dimension] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    def get_text_embedding_batch(self, texts, show_progress=False):
        return [self._vector(text) for text in texts]

    def get_query_embedding(self, query):
        return self._vector(query)


def test_offline_manifest_binds_versions_host_and_asset_hash(tmp_path: Path):
    root = tmp_path / "package"
    root.mkdir()
    asset = root / "images" / "wenjin-backend.tar"
    asset.parent.mkdir()
    asset.write_bytes(b"synthetic docker image archive")
    manifest = tmp_path / "offline-manifest.json"

    created = create_offline_manifest(
        root,
        manifest,
        ["backend-image@wenjin-backend:2026-10-17=images/wenjin-backend.tar"],
        host_target="windows11",
        docker_desktop_version="4.35.1",
        docker_engine_version="27.2.0",
    )
    checked = verify_offline_manifest(manifest, root, expected_versions={"backend-image": "wenjin-backend:2026-10-17"}, expected_host="windows11")
    assert created["assets"] == checked["verified_assets"] == 1
    assert checked["assets"][0]["sha256"] == hashlib.sha256(asset.read_bytes()).hexdigest()
    with pytest.raises(DeliveryError, match="版本与要求不符"):
        verify_offline_manifest(manifest, root, expected_versions={"backend-image": "wrong"})
    asset.write_bytes(b"changed")
    with pytest.raises(DeliveryError, match="不匹配"):
        verify_offline_manifest(manifest, root)


@pytest.mark.parametrize("unsafe", ["../outside", "C:/private/file", "secret.env", "secrets/token", "data/../private"])
def test_delivery_manifest_rejects_unsafe_asset_paths(tmp_path: Path, unsafe: str):
    with pytest.raises(DeliveryError):
        create_offline_manifest(
            tmp_path, tmp_path / "manifest.json", [f"asset@1={unsafe}"],
            host_target="windows10", docker_desktop_version="4.0", docker_engine_version="1.0",
        )


def _make_identity(root: Path, library_id: str, file_id: str):
    store = IdentityStore(root / "data" / "file-management" / "manual-agent-identity.sqlite")
    store.initialize()
    store.bootstrap_first_admin("delivery-admin", "Temp-Admin-Pass-2026!")
    admin_session = store.login("delivery-admin", "Temp-Admin-Pass-2026!")
    admin = store.resolve_session(admin_session.token)
    reader = store.create_user(admin, "delivery-reader", "Temp-Reader-Pass-2026!")
    store.create_user(admin, "delivery-denied", "Temp-Denied-Pass-2026!")
    temporary = store.login("delivery-reader", "Temp-Reader-Pass-2026!")
    store.change_password(temporary.token, "Temp-Reader-Pass-2026!", "Final-Reader-Pass-2026!")
    store.synchronize_resource("library", library_id, active=True)
    store.synchronize_resource("managed_file", file_id, active=True)
    store.set_grant(
        admin,
        resource_kind="library",
        resource_id=library_id,
        user_id=reader.user_id,
        allowed=True,
        expected_revision=0,
    )
    return store


@pytest.mark.parametrize("raw_layout", ["raw_data", "data/file-management/raw_data"])
def test_backup_restore_recovers_login_acl_managed_version_release_and_local_qdrant(tmp_path: Path, raw_layout: str):
    source_root = tmp_path / "source-project"
    source_root.mkdir()
    qdrant_root = source_root / "qdrant-local"
    qdrant_root.mkdir()
    qdrant = QdrantClient(path=str(qdrant_root))
    release_fixture = build_two_version_release_fixture(
        root=source_root,
        library_id="synthetic-delivery-library",
        namespace="delivery",
        qdrant_client=qdrant,
    )
    manager = FileManagementService(
        root=source_root,
        qdrant_client=qdrant,
        embedder=_ManagedEmbedder(),
        embedding_model="delivery-test-embedder-v1",
        managed_raw_data_root=raw_layout,
    )
    first_file = tmp_path / "guide-v1.txt"
    first_file.write_text("amberwidget version one only", encoding="utf-8")
    first_job = manager.submit_import(first_file, display_name="guide.txt", library_id="delivery-guides")
    assert manager.run_job(first_job.job_id).status == "completed"
    second_file = tmp_path / "guide-v2.txt"
    second_file.write_text("amberwidget version two marker", encoding="utf-8")
    second_job = manager.submit_import(
        second_file,
        display_name="guide.txt",
        replace_file_id=first_job.file_id,
        library_id="delivery-guides",
    )
    assert manager.run_job(second_job.job_id).status == "completed"
    active_managed = manager.get_file(first_job.file_id)
    assert active_managed.active_version_id == second_job.version_id

    identity = _make_identity(source_root, "delivery-guides", first_job.file_id)
    catalog = ReleaseCatalog(source_root / "data" / "releases" / "release-catalog.sqlite")
    release_service = ReleaseService(source_root, catalog, qdrant)
    prepared = release_service.prepare(
        release_fixture["library_id"], release_fixture["targets"]["A"],
        reason="synthetic full release fixture", actor="delivery-test",
    )
    release_id = prepared["release_id"]
    release_service.activate(
        release_fixture["library_id"], release_id, expected_revision=0,
        reason="synthetic full release activation", actor="delivery-test",
        expected_embedding_model="synthetic-marker-embedder-v1",
    )
    profile = source_root / "configs" / "delivery-profile.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps({
        "library_id": release_fixture["library_id"],
        "release_id": release_id,
        "active_target_id": release_fixture["targets"]["A"].target_id,
        "qdrant_path": "qdrant-local",
    }, sort_keys=True) + "\n", encoding="utf-8")
    qdrant.close()
    manager.close()
    del identity, catalog

    bundle = tmp_path / "delivery-backup.zip"
    components = ["data/file-management", "data/indexes", "data/releases", "corpora", "qdrant-local", "configs/delivery-profile.json"]
    if raw_layout == "raw_data":
        components.append("raw_data")
    created = create_backup_bundle(
        source_root,
        components,
        bundle,
    )
    verified = verify_backup_bundle(bundle)
    assert created["files"] == verified["verified_files"]
    restored_root = tmp_path / "restored-project"
    restored = restore_backup_bundle(bundle, restored_root)
    assert restored["restored_files"] == verified["verified_files"]
    assert restored["rebased_managed_raw_versions"] == 2
    with pytest.raises(DeliveryError, match="尚不存在"):
        restore_backup_bundle(bundle, restored_root)

    restored_qdrant = QdrantClient(path=str(restored_root / "qdrant-local"))
    restored_manager = FileManagementService(
        root=restored_root,
        qdrant_client=restored_qdrant,
        embedder=_ManagedEmbedder(),
        embedding_model="delivery-test-embedder-v1",
        managed_raw_data_root=raw_layout,
    )
    identity_path = restored_root / "data" / "file-management" / "manual-agent-identity.sqlite"
    app = create_app(restored_root, manager=restored_manager, identity_db=identity_path)
    with TestClient(app) as client:
        reader = client.post("/api/auth/login", headers={"Origin": "http://testserver"}, json={
            "username": "delivery-reader", "password": "Final-Reader-Pass-2026!",
        })
        assert reader.status_code == 200, reader.text
        assert client.cookies.get("wenjin_session")
        assert client.get("/api/auth/me").status_code == 200
        headers = {"Origin": "http://testserver", "X-CSRF-Token": reader.json()["csrf_token"]}
        payload = {
            "library_id": "delivery-guides", "source_type": "managed",
            "file_id": first_job.file_id, "version_id": second_job.version_id,
            "query": "amberwidget",
        }
        response = client.post("/api/admin/search", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["managed_file"]["version_id"] == second_job.version_id
        assert any("version two marker" in item["text"] for item in result["evidence"])
        assert not any("version one only" in item["text"] for item in result["evidence"])
        stale = client.post("/api/admin/search", headers=headers, json={**payload, "version_id": "0" * 32})
        assert stale.status_code == 409, stale.text
        client.cookies.clear()
        denied = client.post("/api/auth/login", headers={"Origin": "http://testserver"}, json={
            "username": "delivery-denied", "password": "Temp-Denied-Pass-2026!",
        })
        assert denied.status_code == 200, denied.text
        denied_response = client.post(
            "/api/admin/search",
            headers={"Origin": "http://testserver", "X-CSRF-Token": denied.json()["csrf_token"]},
            json=payload,
        )
        assert denied_response.status_code == 403, denied_response.text

    registry = IndexRegistryStore(restored_root / "data" / "indexes" / "delivery-index-registry.json")
    assert registry.active().target_id == release_fixture["targets"]["A"].target_id
    restored_catalog = ReleaseCatalog(release_catalog_path(restored_root))
    assert restored_catalog.snapshot(release_fixture["library_id"])["active_release_id"] == release_id
    config = json.loads((restored_root / "configs" / "delivery-profile.json").read_text(encoding="utf-8"))
    assert config["release_id"] == release_id
    assert restored_qdrant.collection_exists(release_fixture["collections"]["A"])
    assert restored_qdrant.collection_exists(restored_manager.snapshot(first_job.file_id).dense_collection)
    restored_release_service = ReleaseService(restored_root, restored_catalog, restored_qdrant)
    assert restored_release_service.validate(release_id)["valid"] is True
    release_resolver = RequestReleaseResolver(
        root=restored_root, catalog=restored_catalog, release_service=restored_release_service,
        qdrant_client=restored_qdrant, embedder=release_fixture["embedder"],
        reranker=None, legacy_router=None, search_lock=__import__("threading").RLock(),
        expected_embedding_model="synthetic-marker-embedder-v1",
    )
    snapshot = release_resolver.begin_request()
    selected = RequestReleaseResolver.resolve(snapshot, library=release_fixture["library_id"])
    materialized = release_resolver.materialize_indices([selected])[0]
    from wenjin.retrieval.router import MultiCorpusRouter
    release_result = MultiCorpusRouter(
        qdrant_client=restored_qdrant, embedder=release_fixture["embedder"],
        indices=[materialized], reranker=None, search_lock=__import__("threading").RLock(),
    ).search("release_marker_A", library=release_fixture["library_id"])
    assert any("release_marker_A" in item["text"] for item in release_result["evidence"])
    source = release_resolver.sources_for(materialized.corpus_identity, release_id=release_id)
    assert "release_marker_A" in source.locate("release-guide.txt").read_text(encoding="utf-8")
    restored_manager.close()
    restored_qdrant.close()


def test_launcher_logs_exit_and_redacts_inherited_secret_without_command_or_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WENJIN_TEST_TOKEN", "synthetic-secret-value")
    output = tmp_path / "run"
    record = _run_logged_child(
        [sys.executable, "-c", "print('password=synthetic-secret-value'); raise SystemExit(7)"],
        cwd=tmp_path,
        output_dir=output,
        service="fixture",
        config_identity={"host": "127.0.0.1", "port": "0"},
    )
    saved = json.loads((output / "run-record.json").read_text(encoding="utf-8"))
    log = (output / "service-redacted.log").read_text(encoding="utf-8")
    assert record["exit_code"] == saved["exit_code"] == 7
    assert saved["started_at_utc"] and saved["finished_at_utc"]
    assert saved["command_recorded"] is False and saved["environment_recorded"] is False
    assert "synthetic-secret-value" not in log
    assert "synthetic-secret-value" not in json.dumps(saved)
    assert not any(key in saved for key in ("argv", "environment", "env"))

    normal = _run_logged_child(
        [sys.executable, "-c", "print('service stopped cleanly')"],
        cwd=tmp_path,
        output_dir=tmp_path / "normal",
        service="fixture",
        config_identity={"host": "127.0.0.1"},
        environment={"PATH": sys.path[0]},
    )
    assert normal["exit_code"] == 0 and normal["result"] == "exited"


def test_management_launcher_passes_project_and_identity_database(monkeypatch, tmp_path: Path):
    import scripts.operations.team_delivery as delivery

    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()
    python = tmp_path / "python.exe"
    python.write_bytes(b"synthetic interpreter fixture")
    identity_db = tmp_path / "private-data" / "accounts.sqlite"
    monkeypatch.setenv("WENJIN_IDENTITY_DB", str(identity_db))
    monkeypatch.setenv("WENJIN_FILE_ADMIN_TOKEN", "synthetic-admin-token")
    captured = {}

    def capture(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return {"result": "exited", "exit_code": 0}

    monkeypatch.setattr(delivery, "_run_logged_child", capture)
    result = launch_managed_service(
        "management", python, project_root, tmp_path / "diagnostics", port=18777
    )
    command = captured["command"]
    assert command[0] == str(python.resolve())
    assert str(project_root.resolve()) == command[3]
    assert command[4:6] == ["127.0.0.1", "18777"]
    assert command[6] == str(identity_db.resolve())
    assert "identity_db=Path(sys.argv[4]) if sys.argv[4] else None" in command[2]
    assert "os.environ.get('WENJIN_FILE_ADMIN_TOKEN')" in command[2]
    assert "synthetic-admin-token" not in " ".join(command)
    assert captured["config_identity"]["identity_database"] == "accounts.sqlite"
    assert "accounts.sqlite" not in json.dumps(result)

    monkeypatch.delenv("WENJIN_IDENTITY_DB")
    launch_managed_service("management", python, project_root, tmp_path / "diagnostics-default")
    assert captured["command"][6] == ""
    assert "identity_db=Path(sys.argv[4]) if sys.argv[4] else None" in captured["command"][2]
    assert captured["config_identity"]["identity_database"] == "not-configured"


def test_legacy_mcp_launcher_passes_project_root_without_exposing_secrets(monkeypatch, tmp_path: Path):
    import scripts.operations.team_delivery as delivery

    project_root = tmp_path / "synthetic-mcp-project"
    project_root.mkdir()
    python = tmp_path / "python.exe"
    python.write_bytes(b"synthetic interpreter fixture")
    monkeypatch.setenv("WENJIN_IDENTITY_DB", str(tmp_path / "identity.sqlite"))
    monkeypatch.setenv("WENJIN_MCP_API_KEY", "synthetic-mcp-secret")
    captured = {}

    def capture(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return {"result": "exited", "exit_code": 0}

    monkeypatch.setattr(delivery, "_run_logged_child", capture)
    launch_managed_service("legacy-mcp", python, project_root, tmp_path / "mcp-diagnostics")
    command = captured["command"]
    assert command[:3] == [str(python.resolve()), "-m", "wenjin.mcp_server"]
    root_arg = command.index("--project-root")
    assert command[root_arg + 1] == str(project_root.resolve())
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "18765"
    assert "synthetic-mcp-secret" not in " ".join(command)
    assert captured["config_identity"]["project_root_name"] == project_root.name
    assert captured["config_identity"]["identity_database"] == "resolved by service configuration; value omitted"


def test_docker_diagnostic_requires_exact_owned_run_label(tmp_path: Path, monkeypatch):
    import scripts.operations.team_delivery as delivery

    item = {"Id": "abc", "Name": "/fixture", "Config": {"Image": "fixture:1", "Labels": {"com.wenjin.delivery.run_id": "mine"}},
            "State": {"Status": "exited", "ExitCode": 9, "OOMKilled": False, "Error": "", "StartedAt": "s", "FinishedAt": "f"},
            "RestartCount": 1, "HostConfig": {"RestartPolicy": {"Name": "no"}}}

    class Result:
        returncode = 0
        stdout = json.dumps([item])
        stderr = ""

    monkeypatch.setattr(delivery, "_run", lambda command: Result())
    with pytest.raises(DeliveryError, match="匹配本次启动记录"):
        collect_owned_docker_diagnostics("abc", "other-run", tmp_path / "rejected.json")
    output = tmp_path / "diagnostic.json"
    result = collect_owned_docker_diagnostics("abc", "mine", output)
    record = json.loads(output.read_text(encoding="utf-8"))
    assert result["exit_code"] == record["container"]["exit_code"] == 9
    assert "Config" not in record["container"] and "Env" not in record["container"]


def test_backup_restore_rejects_tampered_and_unlisted_zip_members(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "marker.txt").write_text("initial", encoding="utf-8")
    bundle = tmp_path / "bundle.zip"
    create_backup_bundle(root, ["marker.txt"], bundle)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "marker.txt":
                data = b"changed"
            target.writestr(info, data)
    with pytest.raises(DeliveryError, match="hash或大小"):
        verify_backup_bundle(tampered)

    extra = tmp_path / "extra.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(extra, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr("unexpected.txt", "x")
    with pytest.raises(DeliveryError, match="额外成员"):
        verify_backup_bundle(extra)

    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as target:
        target.writestr("team-delivery-manifest.json", json.dumps({
            "schema_version": 1,
            "files": [{"path": "../escaped.txt", "size_bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}],
        }))
        target.writestr("../escaped.txt", "x")
    with pytest.raises(DeliveryError, match="上级目录"):
        verify_backup_bundle(traversal)


def test_sqlite_backup_api_includes_committed_wal_data(tmp_path: Path):
    root = tmp_path / "sqlite-source"
    root.mkdir()
    database = root / "identity-fixture.sqlite"
    import sqlite3

    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        writer.execute("INSERT INTO marker VALUES('committed-wal-value')")
        writer.commit()
        assert Path(str(database) + "-wal").exists()
        bundle = tmp_path / "sqlite-wal.zip"
        create_backup_bundle(root, ["identity-fixture.sqlite"], bundle)
    finally:
        writer.close()
    restored = tmp_path / "sqlite-restored"
    restore_backup_bundle(bundle, restored)
    with sqlite3.connect(restored / "identity-fixture.sqlite") as reader:
        assert reader.execute("SELECT value FROM marker").fetchone()[0] == "committed-wal-value"


def test_backup_manifest_describes_mutable_and_immutable_sqlite_methods(tmp_path: Path):
    root = tmp_path / "sqlite-scope-source"
    manager = root / "data" / "file-management" / "manager.sqlite"
    sealed_index = root / "data" / "file-management" / "indexes" / "release-bm25.sqlite"
    manager.parent.mkdir(parents=True)
    sealed_index.parent.mkdir(parents=True)
    import sqlite3

    with sqlite3.connect(manager) as connection:
        connection.execute("CREATE TABLE versions(raw_root TEXT, file_id TEXT, version TEXT)")
    with sqlite3.connect(sealed_index) as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")

    bundle = tmp_path / "sqlite-scope.zip"
    create_backup_bundle(root, ["data/file-management"], bundle)
    with zipfile.ZipFile(bundle) as archive:
        manifest = json.loads(archive.read("team-delivery-manifest.json"))
    methods = {item["path"]: item["snapshot_method"] for item in manifest["files"]}
    assert manifest["sqlite_scope"] == (
        "mutable coordination SQLite=SQLite Backup API; "
        "immutable index/sealed BM25 SQLite=byte copy; "
        "cross-store writers must be quiesced"
    )
    assert methods["data/file-management/manager.sqlite"] == "sqlite-backup-api"
    assert methods["data/file-management/indexes/release-bm25.sqlite"] == "file-copy"


def test_launcher_records_spawn_failure_without_command_details(tmp_path: Path):
    record = _run_logged_child(
        [str(tmp_path / "missing-python.exe")],
        cwd=tmp_path,
        output_dir=tmp_path / "failed-launch",
        service="fixture",
        config_identity={"host": "127.0.0.1"},
        environment={"PATH": ""},
    )
    assert record["result"] == "launch_failed"
    assert record["launch_error"] == "FileNotFoundError"
    assert record["finished_at_utc"]
    assert "command" not in record and "environment" not in record
