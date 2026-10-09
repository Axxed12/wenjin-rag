"""Shared HTTP/MCP policy and single-owner reranker core contracts."""

from __future__ import annotations

from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from wenjin.retrieval.lazy_reranker import LazyQwen3Reranker
from wenjin.retrieval.search_policy import SearchPolicy
from wenjin.settings import Settings

pytestmark = pytest.mark.quick


def test_explicit_settings_produce_same_single_and_route_budget_without_path_leaks(
    monkeypatch,
):
    # Ignore host environment overrides and never load the repository .env.
    for name in (
        "SEARCH_CANDIDATE_K",
        "SEARCH_RERANK_POOL",
        "SEARCH_RRF_K",
        "SEARCH_MAX_LIBRARIES",
        "SEARCH_CONTEXT_CHAR_BUDGET",
        "SEARCH_RERANKER_ENABLED",
        "RERANKER_MODEL_ID",
        "RERANKER_MAX_LENGTH",
        "RERANKER_BATCH_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(
        _env_file=None,
        qdrant_api_key="synthetic-only",
        reranker_model_path=Path("synthetic-model-path"),
        reranker_model_id="fixture-reranker-id",
        search_candidate_k=10,
        search_rerank_pool=12,
        reranker_max_length=768,
        reranker_batch_size=3,
    )
    policy = SearchPolicy.from_settings(settings)

    routed_5 = policy.route_config(5, context_char_budget=1200)
    single_5 = policy.single_config("1.0", 5, context_char_budget=1200)
    public_5 = policy.public_config(5, context_char_budget=1200)
    assert (routed_5.candidate_k, single_5.candidate_k) == (10, 10)
    assert (routed_5.top_k, single_5.top_k) == (5, 5)
    assert (routed_5.rerank_pool, single_5.rerank_pool) == (12, 12)
    assert (routed_5.context_char_budget, single_5.context_char_budget) == (1200, 1200)
    assert public_5 == {
        "candidate_k": 10,
        "base_candidate_k": 10,
        "rerank_pool": 12,
        "top_k": 5,
        "context_char_budget": 1200,
        "reranker_enabled": True,
        "reranker_model_id": "fixture-reranker-id",
        "reranker_max_length": 768,
        "reranker_batch_size": 3,
        "rrf_k": 60,
        "max_libraries": 5,
    }
    assert "synthetic-model-path" not in repr(public_5)
    assert "synthetic-only" not in repr(public_5)

    routed_15 = policy.route_config(15)
    single_15 = policy.single_config("1.0", 15)
    public_15 = policy.public_config(15)
    assert (routed_15.candidate_k, single_15.candidate_k) == (15, 15)
    assert (routed_15.rerank_pool, single_15.rerank_pool) == (15, 15)
    assert public_15["candidate_k"] == 15
    assert public_15["rerank_pool"] == 15


def test_default_settings_policy_uses_candidate_ten_for_http_and_mcp_configs():
    settings = Settings(_env_file=None, qdrant_api_key="synthetic-only")
    policy = SearchPolicy.from_settings(settings)

    routed = policy.route_config(5)
    single = policy.single_config("1.0", 5)
    assert settings.search_candidate_k == 10
    assert policy.candidate_k == 10
    assert (routed.candidate_k, single.candidate_k) == (10, 10)
    assert (routed.rerank_pool, single.rerank_pool) == (30, 30)


def test_policy_rejects_invalid_depth_instead_of_silently_clamping():
    policy = SearchPolicy(candidate_k=10)
    with pytest.raises(ValueError):
        policy.effective_candidate_k(21)
    with pytest.raises(ValueError):
        SearchPolicy(candidate_k=0).validate()


def test_lazy_reranker_constructs_once_scores_under_caller_lock_and_owner_closes_once():
    calls = {"construct": 0, "score": 0, "close": 0}
    model_lock = Lock()

    class FakeReranker:
        def __init__(self, model_path, *, device, max_length, batch_size):
            calls["construct"] += 1
            assert model_path.name == "reranker-fixture"
            assert (device, max_length, batch_size) == ("cpu", 768, 3)

        def score(self, query, documents):
            assert model_lock.locked()
            calls["score"] += 1
            return [float(index) for index, _ in enumerate(documents)]

        def close(self):
            calls["close"] += 1

    owner = LazyQwen3Reranker(
        Path("reranker-fixture"),
        model_id="explicit-fixture-id",
        device="cpu",
        max_length=768,
        batch_size=3,
        factory=FakeReranker,
    )
    assert owner.model_id == "explicit-fixture-id"
    assert not owner.loaded
    with model_lock:
        assert owner.score("query", ["first", "second"]) == [0.0, 1.0]
        assert owner.score("query", ["second"]) == [0.0]
    assert calls == {"construct": 1, "score": 2, "close": 0}
    assert owner.loaded
    owner.close()
    owner.close()
    assert calls == {"construct": 1, "score": 2, "close": 1}
    with pytest.raises(RuntimeError, match="owner已关闭"):
        owner.score("query", ["after-close"])


def test_lazy_reranker_concurrent_first_use_constructs_one_instance():
    calls = {"construct": 0, "score": 0, "close": 0}
    model_lock = Lock()
    barrier = Barrier(2)

    class FakeReranker:
        def __init__(self, *_args, **_kwargs):
            calls["construct"] += 1

        def score(self, _query, _documents):
            assert model_lock.locked()
            calls["score"] += 1
            return [1.0]

        def close(self):
            calls["close"] += 1

    owner = LazyQwen3Reranker(
        Path("fixture"), model_id="id", device="cpu", factory=FakeReranker
    )
    errors: list[Exception] = []

    def score_once():
        try:
            barrier.wait(timeout=2)
            with model_lock:
                assert owner.score("query", ["doc"]) == [1.0]
        except Exception as exc:  # surfaced in the main test thread
            errors.append(exc)

    workers = [Thread(target=score_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)
        assert not worker.is_alive()
    assert errors == []
    assert calls == {"construct": 1, "score": 2, "close": 0}
    owner.close()
    assert calls["close"] == 1


def test_lazy_reranker_model_load_failure_remains_explicit():
    def fail_factory(*_args, **_kwargs):
        raise RuntimeError("synthetic model load failure")

    owner = LazyQwen3Reranker(
        Path("fixture"), model_id="id", device="cpu", factory=fail_factory
    )
    with pytest.raises(RuntimeError, match="synthetic model load failure"):
        owner.score("query", ["doc"])
    assert owner.loaded is False
    owner.close()


def test_lazy_reranker_closes_unpublished_instance_when_post_load_fingerprint_fails(
    monkeypatch,
):
    import wenjin.retrieval.lazy_reranker as lazy_module

    fingerprint = {"file_count": 1, "files": [], "sha256": "synthetic-tree"}
    checks = {"count": 0}
    instances = []

    def fake_fingerprint(_path):
        checks["count"] += 1
        if checks["count"] == 2:
            raise OSError("synthetic post-load fingerprint failure")
        return fingerprint

    class FakeReranker:
        def __init__(self, *_args, **_kwargs):
            self.closed = 0
            instances.append(self)

        def score(self, _query, _documents):
            return [1.0]

        def close(self):
            self.closed += 1

    monkeypatch.setattr(lazy_module, "model_tree_fingerprint", fake_fingerprint)
    owner = LazyQwen3Reranker(
        Path("synthetic-model"), model_id="fixture", device="cpu", factory=FakeReranker
    )

    with pytest.raises(OSError, match="synthetic post-load fingerprint failure"):
        owner.score("query", ["doc"])
    assert owner.loaded is False
    assert len(instances) == 1
    assert instances[0].closed == 1

    assert owner.score("query", ["doc"]) == [1.0]
    assert owner.loaded is True
    assert len(instances) == 2
    assert instances[1].closed == 0
    owner.close()
    assert [instance.closed for instance in instances] == [1, 1]


def test_loaded_reranker_rejects_new_binding_after_model_tree_changes(tmp_path):
    from wenjin.retrieval.lazy_reranker import model_tree_fingerprint

    model_dir = tmp_path / "synthetic-reranker"
    model_dir.mkdir()
    marker = model_dir / "weights.marker"
    marker.write_bytes(b"synthetic-model-v1")
    instances = []

    class FakeReranker:
        def __init__(self, *_args, **_kwargs):
            self.closed = 0
            instances.append(self)

        def score(self, _query, _documents):
            return [1.0]

        def close(self):
            self.closed += 1

    owner = LazyQwen3Reranker(
        model_dir, model_id="synthetic-reranker-id", device="cpu", factory=FakeReranker
    )
    old_identity = model_tree_fingerprint(model_dir)["sha256"]
    owner.bind_asset_fingerprint(old_identity)
    assert owner.score("query", ["synthetic document"]) == [1.0]
    assert owner.loaded_asset_fingerprint["sha256"] == old_identity

    marker.write_bytes(b"synthetic-model-v2")
    new_identity = model_tree_fingerprint(model_dir)["sha256"]
    assert new_identity != old_identity
    with pytest.raises(RuntimeError, match="已加载reranker资产与固定评测输入不一致"):
        owner.bind_asset_fingerprint(new_identity)

    assert owner.loaded is True
    assert len(instances) == 1
    assert instances[0].closed == 0
    owner.close()
    assert instances[0].closed == 1
