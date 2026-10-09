"""Release domain contract tests using only isolated synthetic assets."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier, RLock

import pytest
from qdrant_client import models

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.index_audit import IndexAdmissionError
from wenjin.index_fingerprint import file_sha256
from wenjin.index_registry import IndexTarget, target_raw_data_root
from wenjin.mcp.runtime import create_release_resolver
from wenjin.releases import (
    ReleaseCatalog,
    ReleaseConflictError,
    ReleaseService,
    ReleaseValidationError,
)
from wenjin.releases.resolver import RequestReleaseResolver, release_catalog_path
from wenjin.retrieval.source_context import (
    OGC_3DTILES_PDF_PROFILE,
    REVIEWED_3DTILES_TARGET,
    reviewed_profile_for_target,
    sealed_profile_for_target,
)

pytestmark = pytest.mark.integration
_LIBRARY_ID = "synthetic-release-library"


@pytest.fixture
def release_world(tmp_path: Path):
    """每例使用仓库外临时root与真实内存Qdrant，不加载项目设置。"""
    root = tmp_path / "release-project-root"
    assets = build_two_version_release_fixture(root=root)
    catalog_path = root / "data" / "releases" / "catalog.sqlite"
    catalog = ReleaseCatalog(catalog_path)
    service = ReleaseService(root, catalog, assets["qdrant"])
    try:
        yield {
            "root": root,
            "assets": assets,
            "catalog": catalog,
            "catalog_path": catalog_path,
            "service": service,
        }
    finally:
        assets["qdrant"].close()


def _prepare(world, label: str) -> dict:
    assets = world["assets"]
    return world["service"].prepare(
        _LIBRARY_ID,
        assets["targets"][label],
        reason=f"prepare synthetic marker {label}",
    )


def test_prepare_clones_admitted_assets_without_activating_or_owning_candidates(
    release_world,
):
    world = release_world
    assets = world["assets"]
    legacy_payload = dict(assets["targets"]["A"].__dict__)
    legacy_payload.pop("raw_snapshot_root")
    legacy_target = IndexTarget.from_dict(legacy_payload)
    assert legacy_target.raw_snapshot_root is None
    assert target_raw_data_root(world["root"], legacy_target) == (
        world["root"] / "raw_data"
    ).resolve()

    unsafe_target = IndexTarget.from_dict({
        **legacy_payload,
        "raw_snapshot_root": "../../outside",
    })
    with pytest.raises(ValueError, match="超出项目目录"):
        target_raw_data_root(world["root"], unsafe_target)

    records = {label: _prepare(world, label) for label in ("A", "B")}

    snapshot = world["catalog"].snapshot(_LIBRARY_ID)
    assert snapshot["active_release_id"] is None
    assert {record["status"] for record in records.values()} == {"ready"}

    release_targets = {label: record["target"] for label, record in records.items()}
    for label, record in records.items():
        target = record["target"]
        original = assets["targets"][label]
        assert record["source_target_id"] == original.target_id
        assert target["dense_collection"] != original.dense_collection
        assert target["bm25_path"] != original.bm25_path
        assert target["manifest_path"] != original.manifest_path
        assert target["raw_snapshot_root"]
        assert world["service"].validate(record["release_id"])["valid"] is True
        assert record["validation"]["source_files"] == 1
        assert record["validation"]["chunk_count"] == original.chunk_count == 1

        release_manifest = world["root"] / target["manifest_path"]
        manifest = json.loads(release_manifest.read_text(encoding="utf-8"))
        raw_path = (
            target_raw_data_root(world["root"], assets["targets"][label])
            / Path(manifest["raw_data_subdir"])
            / "release-guide.txt"
        )
        release_raw_path = (
            target_raw_data_root(world["root"], IndexTarget.from_dict(target))
            / Path(manifest["raw_data_subdir"])
            / "release-guide.txt"
        )
        assert assets["paths"][label]["raw"].is_file()
        assert release_raw_path.is_file()
        assert assets["expected_markers"][label] in release_raw_path.read_text(encoding="utf-8")
        assert file_sha256(raw_path) == file_sha256(release_raw_path)
        assert assets["qdrant"].collection_exists(original.dense_collection)
        assert f"qdrant:{target['dense_collection']}" in record["owned_resources"]
        assert original.bm25_path not in record["owned_resources"]

    assert release_targets["A"]["raw_snapshot_root"] != release_targets["B"]["raw_snapshot_root"]
    assert release_targets["A"]["dense_collection"] != release_targets["B"]["dense_collection"]


def test_reviewed_profile_is_strict_on_source_and_inherited_only_by_matching_seal():
    from types import SimpleNamespace

    approved_source = SimpleNamespace(**REVIEWED_3DTILES_TARGET)
    assert reviewed_profile_for_target(approved_source) == OGC_3DTILES_PDF_PROFILE

    release_target = SimpleNamespace(
        **{
            **REVIEWED_3DTILES_TARGET,
            "target_id": "release-uuid-renamed-target",
            "manifest_path": "data/releases/synthetic/release/source-manifest.json",
        }
    )
    # 候选只按完整review身份获得profile；release副本可重命名后从seal继承。
    assert reviewed_profile_for_target(release_target) is None
    assert sealed_profile_for_target(release_target, OGC_3DTILES_PDF_PROFILE) == (
        OGC_3DTILES_PDF_PROFILE
    )
    assert sealed_profile_for_target(release_target, "unregistered-profile") is None

    for key in (
        "library",
        "version",
        "strategy",
        "manifest_sha256",
        "canonical_corpus_sha256",
    ):
        tampered = SimpleNamespace(
            **{
                **release_target.__dict__,
                key: f"tampered-{key}",
            }
        )
        assert sealed_profile_for_target(tampered, OGC_3DTILES_PDF_PROFILE) is None


def test_prepare_seals_profile_and_resolver_uses_only_intact_profile(
    release_world, monkeypatch
):
    world = release_world
    assets = world["assets"]
    source_target = assets["targets"]["A"]
    catalog_path = release_catalog_path(world["root"])
    catalog = ReleaseCatalog(catalog_path)
    service = ReleaseService(world["root"], catalog, assets["qdrant"])
    import wenjin.releases.resolver as resolver_module
    import wenjin.releases.service as service_module

    # Wiring-only fixture: simulate a prior approval for this synthetic candidate.
    # Strict production approval identity remains covered by the pure policy contract above.
    def approved_for_fixture(target):
        return (
            OGC_3DTILES_PDF_PROFILE
            if target.target_id == source_target.target_id
            else None
        )

    def inherited_for_fixture(_target, profile):
        return profile if profile == OGC_3DTILES_PDF_PROFILE else None
    monkeypatch.setattr(service_module, "reviewed_profile_for_target", approved_for_fixture)
    monkeypatch.setattr(service_module, "sealed_profile_for_target", inherited_for_fixture)
    monkeypatch.setattr(resolver_module, "sealed_profile_for_target", inherited_for_fixture)

    record = service.prepare(
        _LIBRARY_ID, source_target, reason="synthetic profile wiring"
    )
    assert record["serving_profile"] == OGC_3DTILES_PDF_PROFILE
    assert service.validate(record["release_id"])["valid"] is True
    active = service.activate(
        _LIBRARY_ID,
        record["release_id"],
        expected_revision=0,
        expected_embedding_model=source_target.embedding_model,
        reason="activate synthetic approved-profile fixture",
    )

    resolver = create_release_resolver(
        root=world["root"],
        qdrant_client=assets["qdrant"],
        embedder=assets["embedder"],
        reranker=None,
        legacy_router=None,
        search_lock=RLock(),
        expected_embedding_model=source_target.embedding_model,
    )
    request = resolver.begin_request()
    selected = RequestReleaseResolver.resolve(request, library=_LIBRARY_ID)
    materialized = resolver.materialize_indices([selected])[0]
    assert active["active_release_id"] == record["release_id"]
    assert materialized.source_context_cache is not None
    assert materialized.source_context_cache.profile == OGC_3DTILES_PDF_PROFILE

    with closing(sqlite3.connect(catalog_path)) as database:
        payload = json.loads(database.execute(
            "SELECT payload_json FROM releases WHERE release_id=?",
            (record["release_id"],),
        ).fetchone()[0])
        payload["serving_profile"] = "unregistered-profile"
        database.execute(
            "UPDATE releases SET payload_json=? WHERE release_id=?",
            (json.dumps(payload, sort_keys=True), record["release_id"]),
        )
        database.commit()
    with pytest.raises(ReleaseValidationError):
        service.validate(record["release_id"])
    assert catalog.snapshot(_LIBRARY_ID)["active_release_id"] == (
        record["release_id"]
    )


def test_activate_persists_snapshot_and_rollback_restores_previous_release(release_world):
    world = release_world
    a = _prepare(world, "A")
    b = _prepare(world, "B")

    active_a = world["service"].activate(
        _LIBRARY_ID, a["release_id"], expected_revision=0, reason="activate A", actor="tester"
    )
    assert active_a["active_release_id"] == a["release_id"]
    assert active_a["revision"] == 1
    active_b = world["service"].activate(
        _LIBRARY_ID, b["release_id"], expected_revision=1, reason="activate B", actor="tester"
    )
    assert active_b["active_release_id"] == b["release_id"]
    assert active_b["revision"] == 2
    assert active_b["active"]["target"]["dense_collection"] == b["target"]["dense_collection"]

    reopened = ReleaseCatalog(world["catalog_path"]).snapshot(_LIBRARY_ID)
    assert reopened["revision"] == 2
    assert reopened["active_release_id"] == b["release_id"]

    restored = world["service"].rollback(
        _LIBRARY_ID, expected_revision=2, reason="restore previous A", actor="tester"
    )
    assert restored["revision"] == 3
    assert restored["active_release_id"] == a["release_id"]
    assert restored["active"]["target"]["dense_collection"] == a["target"]["dense_collection"]
    assert world["service"].validate(a["release_id"])["valid"] is True
    assert [event["action"] for event in world["catalog"].events(_LIBRARY_ID)] == [
        "rollback",
        "activate",
        "activate",
    ]


def test_same_dimension_wrong_embedding_identity_cannot_be_activated(release_world):
    world = release_world
    assets = world["assets"]
    expected_model = assets["targets"]["A"].embedding_model
    wrong_model_target = replace(
        assets["targets"]["B"], embedding_model="synthetic-different-model-same-dimension"
    )
    assert wrong_model_target.vector_dimension == assets["targets"]["A"].vector_dimension

    a = _prepare(world, "A")
    b = world["service"].prepare(
        _LIBRARY_ID, wrong_model_target, reason="same dimension, wrong model identity"
    )
    active_a = world["service"].activate(
        _LIBRARY_ID,
        a["release_id"],
        expected_revision=0,
        expected_embedding_model=expected_model,
        reason="activate serving-compatible A",
    )
    with pytest.raises(ReleaseValidationError, match="模型与当前服务不一致"):
        world["service"].activate(
            _LIBRARY_ID,
            b["release_id"],
            expected_revision=active_a["revision"],
            expected_embedding_model=expected_model,
            reason="reject same-dimension model mismatch",
        )

    after = world["catalog"].snapshot(_LIBRARY_ID)
    assert after["revision"] == active_a["revision"]
    assert after["active_release_id"] == a["release_id"]


@pytest.mark.parametrize("asset", ["raw", "manifest", "bm25", "qdrant"])
def test_tampered_sealed_asset_cannot_be_activated_and_keeps_current_release(
    release_world, asset: str
):
    world = release_world
    a = _prepare(world, "A")
    activated = world["service"].activate(
        _LIBRARY_ID, a["release_id"], expected_revision=0, reason="establish active A"
    )
    b = _prepare(world, "B")
    target = b["target"]

    if asset == "raw":
        manifest = json.loads((world["root"] / target["manifest_path"]).read_text(encoding="utf-8"))
        path = (
            target_raw_data_root(
                world["root"],
                IndexTarget.from_dict(target),
            )
            / Path(manifest["raw_data_subdir"])
            / "release-guide.txt"
        )
        path.write_bytes(path.read_bytes() + b"tampered\n")
    elif asset == "manifest":
        path = world["root"] / target["manifest_path"]
        path.write_bytes(path.read_bytes() + b" ")
    elif asset == "bm25":
        path = world["root"] / target["bm25_path"]
        # SQLite可忽略页尾数据；封存SHA仍必须发现任何字节变化。
        with path.open("ab") as output:
            output.write(b"tampered-seal")
    else:
        qdrant = world["assets"]["qdrant"]
        points, _ = qdrant.scroll(
            target["dense_collection"], limit=1, with_payload=True, with_vectors=False
        )
        assert len(points) == 1
        qdrant.set_payload(
            target["dense_collection"],
            payload={"synthetic_tamper_marker": "changed"},
            points=[points[0].id],
            wait=True,
        )

    with pytest.raises(ReleaseValidationError) as error:
        world["service"].activate(
            _LIBRARY_ID,
            b["release_id"],
            expected_revision=activated["revision"],
            reason=f"reject mutated {asset}",
        )
    assert error.value.code == "release_validation_failed"
    after = world["catalog"].snapshot(_LIBRARY_ID)
    assert after["revision"] == activated["revision"]
    assert after["active_release_id"] == a["release_id"]


def test_candidate_raw_and_manifest_changes_do_not_change_sealed_release_or_rollback(
    release_world,
):
    world = release_world
    assets = world["assets"]
    a = _prepare(world, "A")
    b = _prepare(world, "B")
    world["service"].activate(
        _LIBRARY_ID, a["release_id"], expected_revision=0, reason="activate sealed A"
    )
    world["service"].activate(
        _LIBRARY_ID, b["release_id"], expected_revision=1, reason="activate sealed B"
    )

    # 两个候选源的manifest与raw都变化；release应只依赖自己的封存副本。
    for label in ("A", "B"):
        raw_path = assets["paths"][label]["raw"]
        manifest_path = assets["paths"][label]["manifest"]
        raw_path.write_bytes(raw_path.read_bytes() + b"candidate changed after prepare\n")
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")

    assert world["service"].validate(a["release_id"])["valid"] is True
    assert world["service"].validate(b["release_id"])["valid"] is True
    restored = world["service"].rollback(
        _LIBRARY_ID, expected_revision=2, reason="rollback from sealed B to sealed A"
    )
    assert restored["active_release_id"] == a["release_id"]
    assert world["service"].validate(restored["active_release_id"])["valid"] is True

    sealed_a_target = IndexTarget.from_dict(restored["active"]["target"])
    release_manifest_path = world["root"] / sealed_a_target.manifest_path
    manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
    sealed_raw = (
        target_raw_data_root(world["root"], sealed_a_target)
        / Path(manifest["raw_data_subdir"])
        / "release-guide.txt"
    )
    assert assets["expected_markers"]["A"] in sealed_raw.read_text(encoding="utf-8")
    assert assets["expected_markers"]["B"] not in sealed_raw.read_text(encoding="utf-8")


def test_vector_direction_tamper_is_detected_when_payload_and_count_are_unchanged(
    release_world,
):
    world = release_world
    a = _prepare(world, "A")
    activated = world["service"].activate(
        _LIBRARY_ID, a["release_id"], expected_revision=0, reason="establish active A"
    )
    b = _prepare(world, "B")
    qdrant = world["assets"]["qdrant"]
    collection = b["target"]["dense_collection"]
    before, _ = qdrant.scroll(collection, limit=10, with_payload=True, with_vectors=True)
    assert len(before) == 1
    original = list(before[0].vector)
    payload = dict(before[0].payload or {})
    assert len(original) >= 2
    rotated = original.copy()
    rotated[0], rotated[1] = -original[1], original[0]
    assert rotated != original
    qdrant.upsert(
        collection,
        points=[models.PointStruct(id=before[0].id, vector=rotated, payload=payload)],
        wait=True,
    )

    after, _ = qdrant.scroll(collection, limit=10, with_payload=True, with_vectors=True)
    assert len(after) == len(before)
    assert after[0].payload == before[0].payload
    assert list(after[0].vector) == rotated

    with pytest.raises(ReleaseValidationError) as error:
        world["service"].activate(
            _LIBRARY_ID,
            b["release_id"],
            expected_revision=activated["revision"],
            reason="reject changed vector direction",
        )
    assert error.value.code == "release_validation_failed"
    snapshot = world["catalog"].snapshot(_LIBRARY_ID)
    assert snapshot["revision"] == activated["revision"]
    assert snapshot["active_release_id"] == a["release_id"]


def test_invalid_candidate_fails_prepare_without_touching_active_or_candidate_assets(
    release_world,
):
    world = release_world
    a = _prepare(world, "A")
    active = world["service"].activate(
        _LIBRARY_ID, a["release_id"], expected_revision=0, reason="establish active A"
    )
    candidate_b = world["assets"]["targets"]["B"]
    source_b = world["assets"]["paths"]["B"]["raw"]
    original = source_b.read_bytes()
    source_b.write_bytes(original + b"candidate-corruption\n")

    with pytest.raises(IndexAdmissionError):
        world["service"].prepare(_LIBRARY_ID, candidate_b, reason="reject changed candidate B")

    after = world["catalog"].snapshot(_LIBRARY_ID)
    assert after["revision"] == active["revision"]
    assert after["active_release_id"] == a["release_id"]
    assert source_b.is_file()
    assert candidate_b.bm25_path == world["assets"]["targets"]["B"].bm25_path
    assert world["assets"]["qdrant"].collection_exists(candidate_b.dense_collection)
    failed = [record for record in after["releases"] if record["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["source_target_id"] == candidate_b.target_id
    assert failed[0]["owned_resources"]


def test_same_revision_concurrent_activations_have_one_winner(release_world):
    world = release_world
    a = _prepare(world, "A")
    b = _prepare(world, "B")
    barrier = Barrier(2)

    def activate(release_id: str):
        barrier.wait(timeout=10)
        try:
            result = world["service"].activate(
                _LIBRARY_ID,
                release_id,
                expected_revision=0,
                reason="concurrent activation CAS",
            )
            return ("success", result["active_release_id"])
        except ReleaseConflictError as error:
            return ("conflict", error.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(activate, [a["release_id"], b["release_id"]]))

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "success"]
    snapshot = world["catalog"].snapshot(_LIBRARY_ID)
    assert snapshot["revision"] == 1
    assert snapshot["active_release_id"] in {a["release_id"], b["release_id"]}
