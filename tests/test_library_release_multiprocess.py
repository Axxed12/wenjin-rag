"""Cross-process SQLite CAS contract over already-sealed synthetic releases."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.releases import ReleaseCatalog, ReleaseService
from wenjin.releases.resolver import release_catalog_path

pytestmark = pytest.mark.integration


def _switch_in_child(catalog_path: Path, library_id: str, release_id: str, gate: Path):
    script = r"""
import json, sys, time
from pathlib import Path
from wenjin.releases import ReleaseCatalog, ReleaseConflictError

catalog_path, library_id, release_id, gate = sys.argv[1:]
gate = Path(gate)
deadline = time.monotonic() + 15
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not gate.exists():
    print(json.dumps({"result": "gate_timeout"}))
    raise SystemExit(2)
catalog = ReleaseCatalog(Path(catalog_path))
try:
    snapshot = catalog.activate(
        library_id, release_id, expected_revision=0,
        reason="multiprocess synthetic CAS contender",
    )
    print(json.dumps({"result": "activated", "revision": snapshot["revision"],
                      "active_release_id": snapshot["active_release_id"]}))
except ReleaseConflictError as exc:
    print(json.dumps({"result": "revision_conflict",
                      "expected_revision": exc.expected_revision,
                      "current_revision": exc.current_revision}))
"""
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(catalog_path),
            library_id,
            release_id,
            str(gate),
        ],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_process_cas(tmp_path):
    root = tmp_path / "r"
    assets = build_two_version_release_fixture(root=root)
    qdrant = assets["qdrant"]
    catalog_path = release_catalog_path(root)
    catalog = ReleaseCatalog(catalog_path)
    service = ReleaseService(root, catalog, qdrant)
    try:
        records = {
            label: service.prepare(
                assets["library_id"], target, reason=f"multiprocess candidate {label}"
            )
            for label, target in assets["targets"].items()
        }
        for record in records.values():
            assert service.validate(record["release_id"])["valid"] is True
        assert catalog.snapshot(assets["library_id"])["revision"] == 0

        gate = tmp_path / "g"
        processes = [
            _switch_in_child(
                catalog_path,
                assets["library_id"],
                records[label]["release_id"],
                gate,
            )
            for label in ("A", "B")
        ]
        gate.write_text("start", encoding="ascii")
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            outputs.append(json.loads(stdout.strip()))

        assert sorted(result["result"] for result in outputs) == [
            "activated",
            "revision_conflict",
        ]
        winner = next(result for result in outputs if result["result"] == "activated")
        conflict = next(result for result in outputs if result["result"] == "revision_conflict")
        assert winner["revision"] == 1
        assert conflict["expected_revision"] == 0
        assert conflict["current_revision"] == 1

        reopened = ReleaseCatalog(catalog_path).snapshot(assets["library_id"])
        assert reopened["revision"] == 1
        assert reopened["active_release_id"] == winner["active_release_id"]
        assert reopened["active"]["release_id"] == winner["active_release_id"]
        assert {item["release_id"] for item in reopened["releases"]} == {
            record["release_id"] for record in records.values()
        }
        assert sum(item["status"] == "active" for item in reopened["releases"]) == 1
    finally:
        qdrant.close()
