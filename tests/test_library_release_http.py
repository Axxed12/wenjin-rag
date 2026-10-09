"""Release-management HTTP contracts on a synthetic temp root only."""

from __future__ import annotations

import json
import time

import pytest
from qdrant_client import QdrantClient
from starlette.testclient import TestClient

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.file_management import FileManagementService
from wenjin.management_http.app import create_app
from wenjin.releases.resolver import release_catalog_path

pytestmark = pytest.mark.integration
_ADMIN_TOKEN = "synthetic-release-http-admin-test-token"


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_TOKEN}"}


def _wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/admin/release-jobs/{job_id}", headers=_admin_headers()
        )
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"ready", "failed"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"release job {job_id} did not reach a terminal state")


def test_release_http_prepare_cas_rollback_and_safe_job_projection(tmp_path):
    root = tmp_path / "release-http-project-root"
    qdrant = QdrantClient(":memory:")
    assets = build_two_version_release_fixture(root=root, qdrant_client=qdrant)
    target_config = root / "configs" / "indexes" / "synthetic-release-targets.json"
    target_config.parent.mkdir(parents=True, exist_ok=True)
    target_config.write_text(
        json.dumps({
            "schema_version": 1,
            "candidates": [
                assets["targets"][label].__dict__ for label in ("A", "B")
            ],
        }),
        encoding="utf-8",
    )
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=assets["embedder"],
        embedding_model="synthetic-marker-embedder-v1",
    )
    app = create_app(root, admin_token=_ADMIN_TOKEN, manager=manager)
    catalog_path = release_catalog_path(root)

    try:
        with TestClient(app) as client:
            library_id = assets["library_id"]
            headers = _admin_headers()

            # Read-only empty-state listing must not create a catalog as a side effect.
            initial = client.get(
                f"/api/admin/libraries/{library_id}/releases", headers=headers
            )
            assert initial.status_code == 200, initial.text
            assert initial.json()["active_release_id"] is None
            assert catalog_path.exists() is False

            targets = client.get(
                "/api/admin/release-targets",
                params={"library_id": library_id},
                headers=headers,
            )
            assert targets.status_code == 200, targets.text
            target_ids = {item["target_id"] for item in targets.json()["targets"]}
            assert target_ids == {
                assets["targets"]["A"].target_id,
                assets["targets"]["B"].target_id,
            }

            prepared_ids: dict[str, str] = {}
            for label in ("A", "B"):
                created = client.post(
                    "/api/admin/releases/prepare",
                    headers=headers,
                    json={
                        "library_id": library_id,
                        "target_id": assets["targets"][label].target_id,
                        "reason": f"synthetic fixture release {label}",
                    },
                )
                assert created.status_code == 202, created.text
                job = created.json()["job"]
                # API projection must not expose the internal captured target JSON/hash.
                assert "target_json" not in job
                assert "target_sha256" not in job
                finished = _wait_job(client, job["job_id"])
                assert finished["status"] == "ready", finished
                assert finished["stage"] == "ready"
                assert finished["progress"] == 100
                assert "target_json" not in finished
                assert "target_sha256" not in finished
                prepared_ids[label] = finished["result"]["release_id"]

            releases = client.get(
                f"/api/admin/libraries/{library_id}/releases", headers=headers
            )
            assert releases.status_code == 200, releases.text
            assert releases.json()["active_release_id"] is None
            assert {
                item["release_id"] for item in releases.json()["releases"]
            } == set(prepared_ids.values())

            activate_a = client.post(
                f"/api/admin/libraries/{library_id}/activate",
                headers=headers,
                json={
                    "release_id": prepared_ids["A"],
                    "expected_revision": 0,
                    "reason": "activate synthetic A",
                },
            )
            assert activate_a.status_code == 200, activate_a.text
            assert activate_a.json()["active_release_id"] == prepared_ids["A"]
            assert activate_a.json()["revision"] == 1

            activate_b = client.post(
                f"/api/admin/libraries/{library_id}/activate",
                headers=headers,
                json={
                    "release_id": prepared_ids["B"],
                    "expected_revision": 1,
                    "reason": "activate synthetic B",
                },
            )
            assert activate_b.status_code == 200, activate_b.text
            assert activate_b.json()["active_release_id"] == prepared_ids["B"]
            assert activate_b.json()["revision"] == 2

            stale_cas = client.post(
                f"/api/admin/libraries/{library_id}/activate",
                headers=headers,
                json={
                    "release_id": prepared_ids["A"],
                    "expected_revision": 1,
                    "reason": "stale CAS must not switch",
                },
            )
            assert stale_cas.status_code == 409, stale_cas.text
            assert stale_cas.json()["error"] == "revision_conflict"
            still_b = client.get(
                f"/api/admin/libraries/{library_id}/releases", headers=headers
            )
            assert still_b.json()["active_release_id"] == prepared_ids["B"]
            assert still_b.json()["revision"] == 2

            rollback = client.post(
                f"/api/admin/libraries/{library_id}/rollback",
                headers=headers,
                json={"expected_revision": 2, "reason": "rollback to A"},
            )
            assert rollback.status_code == 200, rollback.text
            assert rollback.json()["active_release_id"] == prepared_ids["A"]
            assert rollback.json()["revision"] == 3
    finally:
        # The app lifespan closes the manager; client close is safe if already closed.
        qdrant.close()
