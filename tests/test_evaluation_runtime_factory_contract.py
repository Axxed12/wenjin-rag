"""Production RuntimeFactory binding/context on an isolated synthetic index only."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from tests.support.library_release_fixture import build_two_version_release_fixture
from wenjin.evaluation_tasks.runtime import EvaluationRuntimeFactory
from wenjin.evaluation_tasks.service import EvaluationJobStore
from wenjin.retrieval.lazy_reranker import LazyQwen3Reranker
from wenjin.retrieval.search_policy import SearchPolicy

pytestmark = pytest.mark.integration


class _AdminWithSyntheticGrant:
    def list_users(self, actor):
        assert actor.role == "admin"
        return []

    def can_read(self, actor, resource_type, resource_id):
        return actor.role == "admin" and resource_type == "library" and resource_id == "synthetic-lib"


class _FakeReranker:
    instances = []

    def __init__(self, _path, *, device, max_length, batch_size):
        assert (device, max_length, batch_size) == ("cpu", 1024, 2)
        self.closed = 0
        type(self).instances.append(self)

    def score(self, _query, documents):
        return [1.0 - index / max(1, len(documents)) for index, _ in enumerate(documents)]

    def close(self):
        self.closed += 1


def test_runtime_factory_binds_and_runs_one_synthetic_case_without_closing_shared_client(
    tmp_path, monkeypatch,
):
    root = tmp_path / "evaluation-runtime-root"
    root.mkdir()
    assets = build_two_version_release_fixture(root=root, library_id="synthetic-lib")
    qdrant: QdrantClient = assets["qdrant"]
    try:
        target = assets["targets"]["A"]
        route_path = root / "configs" / "indexes" / "multicorpus-candidates-003.json"
        route_path.parent.mkdir(parents=True)
        route_path.write_text(
            json.dumps({"schema_version": 1, "candidates": [target.__dict__]}),
            encoding="utf-8",
        )
        dataset_path = root / "evaluation" / "datasets" / "developer-validation-2026-10-04.jsonl"
        dataset_path.parent.mkdir(parents=True)
        row = {
            "case_id": "dv-gdal-003", "query": "release marker A runtime fixture",
            "library": "synthetic-lib", "version": "1.0",
        }
        raw_dataset = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        dataset_path.write_bytes(raw_dataset)
        dataset_path.with_suffix(".sha256").write_text(
            hashlib.sha256(raw_dataset).hexdigest() + "  developer-validation-2026-10-04.jsonl\n",
            encoding="ascii",
        )
        for name in ("embedding", "reranker"):
            model_dir = root / "models" / name
            model_dir.mkdir(parents=True)
            (model_dir / "synthetic-model-marker.bin").write_bytes(f"synthetic-{name}".encode())

        settings = SimpleNamespace(
            embedding_model_path=Path("models/embedding"),
            reranker_model_path=Path("models/reranker"),
        )
        shared_reranker = LazyQwen3Reranker(
            root / "models" / "reranker",
            model_id="synthetic-shared-reranker-v1",
            device="cpu",
            max_length=1024,
            batch_size=2,
            factory=_FakeReranker,
        )
        manager = SimpleNamespace(
            qdrant_client=qdrant,
            embedder=assets["embedder"],
            embedding_model="synthetic-marker-embedder-v1",
            _model_lock=threading.Lock(),
        )
        identity = _AdminWithSyntheticGrant()
        app_state = SimpleNamespace(
            manager=manager, qdrant_client=qdrant, settings=settings,
            identity_store=identity, shared_reranker=shared_reranker,
            search_policy=SearchPolicy(
                candidate_k=20,
                rerank_pool=30,
                reranker_enabled=True,
                reranker_model_id="synthetic-shared-reranker-v1",
                reranker_max_length=1024,
                reranker_batch_size=2,
            ),
        )
        monkeypatch.setattr(
            EvaluationRuntimeFactory, "_runtime_environment",
            staticmethod(lambda: {"device": "cpu", "runtime": "synthetic-test"}),
        )
        monkeypatch.setattr("wenjin.retrieval.rerank.Qwen3Reranker", _FakeReranker)
        _FakeReranker.instances.clear()

        factory = EvaluationRuntimeFactory(root=root, app_state=app_state)
        store = EvaluationJobStore(root / "data" / "evaluation-tasks.sqlite")
        service = factory.create_service(identity, store)
        actor = SimpleNamespace(user_id="synthetic-admin", role="admin", must_change_password=False)

        async def scenario():
            submitted = await service.submit(actor, "gdal_dstalpha_diagnostic_v1")
            assert submitted.input_snapshot["case_ids"] == ["dv-gdal-003"]
            assert submitted.input_snapshot["resource_ids"] == ["synthetic-lib"]
            assert submitted.run_identity["model_bindings"]["embedding_model_id"] == "synthetic-marker-embedder-v1"
            context = factory.context_factory(submitted, service._job_dir(submitted.job_id))
            assert context.router.indices[0].target.target_id == target.target_id
            assert context.router.indices[0].sources is not None
            assert context.router.reranker is shared_reranker
            # The service creates its own context for execution; close this inspection context explicitly.
            context.close()
            assert shared_reranker.loaded is False
            running = service._running.get(submitted.job_id)
            if running is not None:
                await running
            completed = await asyncio.to_thread(store.get, submitted.job_id)
            assert completed.status == "completed", (
                completed.error_code, completed.stage, completed.result_summary,
            )
            case_file = service._job_dir(submitted.job_id) / "cases" / "dv-gdal-003.json"
            result = json.loads(case_file.read_text(encoding="utf-8"))
            body = "\n".join(item["text"] for item in result["profiles"]["baseline"]["selected"])
            assert "release_marker_A" in body
            assert result["run_identity_sha256"] == submitted.run_identity["run_identity_sha256"]
            assert len(_FakeReranker.instances) == 1
            assert _FakeReranker.instances[0].closed == 0
            assert qdrant.collection_exists(assets["collections"]["A"])
            await service.shutdown()
            assert shared_reranker.loaded is True
            assert _FakeReranker.instances[0].closed == 0
            shared_reranker.close()
            assert _FakeReranker.instances[0].closed == 1

        asyncio.run(scenario())
    finally:
        qdrant.close()


def test_remote_runtime_factory_binds_model_identity_without_local_weight_paths(tmp_path, monkeypatch):
    """Remote runtime binding must use the API identity, not local model directories."""
    root = tmp_path / "remote-evaluation-runtime-root"
    root.mkdir()
    assets = build_two_version_release_fixture(root=root, library_id="synthetic-lib")
    qdrant: QdrantClient = assets["qdrant"]
    try:
        target = assets["targets"]["A"]
        route_path = root / "configs" / "indexes" / "multicorpus-candidates-003.json"
        route_path.parent.mkdir(parents=True)
        route_path.write_text(
            json.dumps({"schema_version": 1, "candidates": [target.__dict__]}),
            encoding="utf-8",
        )
        dataset_path = root / "evaluation" / "datasets" / "developer-validation-2026-10-04.jsonl"
        dataset_path.parent.mkdir(parents=True)
        row = {
            "case_id": "dv-gdal-003", "query": "release marker A remote binding",
            "library": "synthetic-lib", "version": "1.0",
        }
        raw_dataset = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        dataset_path.write_bytes(raw_dataset)
        dataset_path.with_suffix(".sha256").write_text(
            hashlib.sha256(raw_dataset).hexdigest() + "  developer-validation-2026-10-04.jsonl\n",
            encoding="ascii",
        )

        identity = {
            "protocol": "wenjin-model-api/1",
            "implementation_sha256": "c" * 64,
            "embedding": {
                "model_id": "synthetic-marker-embedder-v1",
                "asset_sha256": "a" * 64,
                "dimensions": 8,
                "normalized": True,
                "input_protocol": "raw-text-v1",
            },
            "reranker": {
                "model_id": "synthetic-marker-reranker-v1",
                "asset_sha256": "b" * 64,
                "max_length": 1024,
                "batch_size": 2,
            },
            "limits": {"max_batch_size": 32, "max_text_chars": 4096, "max_documents": 128},
        }
        manager = SimpleNamespace(
            qdrant_client=qdrant,
            embedder=assets["embedder"],
            embedding_model="synthetic-marker-embedder-v1",
            _model_lock=threading.Lock(),
        )
        app_state = SimpleNamespace(
            manager=manager,
            qdrant_client=qdrant,
            settings=SimpleNamespace(model_provider="remote"),
            identity_store=_AdminWithSyntheticGrant(),
            model_provider_identity=identity,
            search_policy=SearchPolicy(
                candidate_k=20,
                rerank_pool=30,
                reranker_enabled=True,
                reranker_model_id="synthetic-marker-reranker-v1",
                reranker_max_length=1024,
                reranker_batch_size=2,
            ),
        )
        factory = EvaluationRuntimeFactory(root=root, app_state=app_state)
        actor = SimpleNamespace(user_id="synthetic-admin", role="admin", must_change_password=False)
        import builtins

        original_import = builtins.__import__

        def reject_local_inference_stack(name, *args, **kwargs):
            if name.split(".", 1)[0] in {"torch", "transformers", "sentence_transformers"}:
                raise AssertionError("remote evaluation binding imported local inference packages")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", reject_local_inference_stack)
        snapshot, run_identity = factory.binding_factory(
            factory.specs()["gdal_dstalpha_diagnostic_v1"], "synthetic-job", actor,
        )

        bindings = run_identity["model_bindings"]
        assert bindings["provider_identity"] == identity
        assert bindings["embedding_model_id"] == identity["embedding"]["model_id"]
        assert bindings["embedding_model_assets"] == {"sha256": "a" * 64}
        assert bindings["reranker_model_id"] == identity["reranker"]["model_id"]
        assert bindings["reranker_model_assets"] == {"sha256": "b" * 64}
        assert bindings["runtime_environment"]["provider"] == "remote"
        assert bindings["runtime_environment"]["device"] == "remote"
        assert snapshot["case_ids"] == ["dv-gdal-003"]
        assert not (root / "models").exists()
    finally:
        qdrant.close()
