"""Local model API fingerprints model assets before load and rejects load-time drift."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.integration


class _Embedder:
    def __init__(self):
        self.closed = 0

    def get_query_embedding(self, _query):
        return [1.0, 0.0]

    def close(self):
        self.closed += 1


class _Reranker:
    def __init__(self, model_path: Path):
        self.model_path = model_path
        self.device = "cpu"
        self.max_length = 1024
        self.batch_size = 2
        self.bound_fingerprint = None
        self.closed = 0

    def bind_asset_fingerprint(self, fingerprint):
        self.bound_fingerprint = fingerprint

    def score(self, _query, _documents):
        return []

    def close(self):
        self.closed += 1


def _settings(root: Path, *, on_embedding_load=None):
    def embedding(*, root):
        if on_embedding_load is not None:
            on_embedding_load()
        return _Embedder()

    return SimpleNamespace(
        model_provider="local",
        resolved_model_path=lambda field, *, root: root / field.replace("_model_path", ""),
        resolved_embedding_model_id="synthetic-embedding-v1",
        resolved_reranker_model_id="synthetic-reranker-v1",
        embedding=embedding,
    )


def _patch_runtime_builders(monkeypatch, *, embedding_path, reranker):
    from wenjin.retrieval import lazy_reranker

    original_fingerprint = lazy_reranker.model_tree_fingerprint
    events = []

    def fingerprint(path):
        path = Path(path)
        events.append(("fingerprint", path))
        return original_fingerprint(path)

    monkeypatch.setattr(lazy_reranker, "model_tree_fingerprint", fingerprint)
    monkeypatch.setattr(lazy_reranker, "build_shared_reranker", lambda *_args, **_kwargs: reranker)
    return events


def _write_model_trees(root: Path):
    embedding_path = root / "embedding"
    reranker_path = root / "reranker"
    embedding_path.mkdir()
    reranker_path.mkdir()
    (embedding_path / "weights.bin").write_bytes(b"synthetic-embedding-v1")
    (reranker_path / "weights.bin").write_bytes(b"synthetic-reranker-v1")
    return embedding_path, reranker_path


def test_model_api_fingerprints_embedding_tree_before_calling_loader(tmp_path, monkeypatch):
    from wenjin.model_api.server import _build_runtime

    embedding_path, reranker_path = _write_model_trees(tmp_path)
    embedder = _Embedder()
    reranker = _Reranker(reranker_path)
    events = _patch_runtime_builders(monkeypatch, embedding_path=embedding_path, reranker=reranker)
    settings = _settings(tmp_path, on_embedding_load=lambda: events.append(("embedding_loader", None)))
    monkeypatch.setattr(settings, "embedding", lambda *, root: (
        events.append(("embedding_loader", None)) or embedder
    ))

    runtime = _build_runtime(tmp_path, settings, "synthetic-model-token")

    first_fingerprint = next(i for i, event in enumerate(events) if event[0] == "fingerprint")
    loader = next(i for i, event in enumerate(events) if event[0] == "embedding_loader")
    assert first_fingerprint < loader
    assert runtime.identity["embedding"]["asset_sha256"]
    runtime.close()
    assert (embedder.closed, reranker.closed) == (1, 1)


def test_model_api_rejects_embedding_tree_drift_during_load_and_closes_owned_resources(
    tmp_path, monkeypatch,
):
    from wenjin.model_api.server import _build_runtime

    embedding_path, reranker_path = _write_model_trees(tmp_path)
    embedder = _Embedder()
    reranker = _Reranker(reranker_path)
    _patch_runtime_builders(monkeypatch, embedding_path=embedding_path, reranker=reranker)

    def mutate_tree():
        (embedding_path / "weights.bin").write_bytes(b"synthetic-different-weights")

    settings = _settings(tmp_path, on_embedding_load=mutate_tree)
    monkeypatch.setattr(settings, "embedding", lambda *, root: (
        mutate_tree() or embedder
    ))

    with pytest.raises(RuntimeError, match="加载期间发生变化"):
        _build_runtime(tmp_path, settings, "synthetic-model-token")

    assert (embedder.closed, reranker.closed) == (1, 1)
