"""CPU-only tests for model adapter shape and SentenceTransformer encode guards."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

SOURCE_ROOT = Path(__file__).parent / "experimental-source"
sys.path.insert(0, str(SOURCE_ROOT))

from wenjin.model_api.experiment_adapter import (  # noqa: E402
    _embedding_model,
    _install_embed_retry_guard,
    _pin_sentence_transformer,
    _prepare_reranker,
    _validate_encode_call,
)


class _FakeSentenceTransformer:
    def __init__(self, transformer=None):
        self.transformer = transformer
        self.device = _Device("cuda:0")
        self.training = False

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index):
        assert index == 0
        return self.transformer

    def modules(self):
        return [self]

    def parameters(self):
        return []

    def named_parameters(self):
        return []

    def named_buffers(self):
        return []

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        self.training = False
        return self

    def preprocess(self, values):
        return values

    def forward(self, *args, **kwargs):
        return {"result": "forward"}

    def encode(self, *, device=None, pool=None):
        self.to(self.device if device is None else device)
        self.eval()
        return "encoded"


class _Device:
    def __init__(self, value):
        if isinstance(value, _Device):
            self.type, self.index = value.type, value.index
        else:
            pieces = str(value).split(":", 1)
            self.type = pieces[0]
            self.index = int(pieces[1]) if len(pieces) == 2 else None

    def __eq__(self, other):
        return isinstance(other, _Device) and (self.type, self.index) == (other.type, other.index)

    def __str__(self):
        return f"{self.type}:{self.index}" if self.index is not None else self.type


class _FakeTransformer:
    def __init__(self, auto_model):
        self.auto_model = auto_model


class _FakeModel:
    def __init__(self):
        self.device = _Device("cuda:0")
        self.config = SimpleNamespace(
            model_type="qwen3", rope_scaling=None, attention_dropout=0.0, use_cache=True
        )
        self.training = False

    def modules(self):
        return [self]

    def parameters(self):
        return []

    def named_parameters(self):
        return []

    def named_buffers(self):
        return []

    def forward(self, *args, **kwargs):
        return {"result": "forward"}


class _FakeAutoModel(_FakeModel):
    pass


def _encode_signature() -> inspect.Signature:
    return inspect.Signature(
        [
            inspect.Parameter("device", inspect.Parameter.KEYWORD_ONLY, default=None),
            inspect.Parameter("pool", inspect.Parameter.KEYWORD_ONLY, default=None),
        ]
    )


def test_full_pin_guards_accept_only_fixed_mock_shapes_without_cuda(monkeypatch) -> None:
    fake_torch = SimpleNamespace(device=_Device, dtype=type("DType", (), {}), cuda=SimpleNamespace())
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    fake_transformer_module = SimpleNamespace(Transformer=_FakeTransformer)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers.base.modules.transformer",
        fake_transformer_module,
    )
    auto_model = _FakeAutoModel()
    transformer = _FakeTransformer(auto_model)
    sentence_model = _FakeSentenceTransformer(transformer)
    embedder = SimpleNamespace(
        _model=sentence_model,
        _parallel_process=False,
        _target_devices=None,
        _embed_with_retry=lambda values: values,
    )

    embedding = _pin_sentence_transformer(embedder, lanes=2, streams=[None, None], origin=None)

    assert embedding["device"] == "cuda:0"
    assert embedding["config"]["rope_scaling"] is None
    assert sentence_model.encode() == "encoded"
    assert embedder._embed_with_retry(["input"]) == ["input"]
    embedder._parallel_process = True
    with pytest.raises(RuntimeError, match="experiment_embedding_parallel_process_unsupported"):
        embedder._embed_with_retry(["input"])
    embedder._parallel_process = False
    with pytest.raises(RuntimeError, match="experiment_embedding_multiprocess_pool_rejected"):
        sentence_model.encode(pool={"processes": []})
    with pytest.raises(RuntimeError, match="experiment_embedding_multiprocess_devices_rejected"):
        sentence_model.encode(device=["cuda:0", "cuda:1"])
    with pytest.raises(RuntimeError, match="experiment_embedding_device_change_rejected"):
        sentence_model.encode(device="cuda:1")

    reranker_model = _FakeModel()
    reranker = SimpleNamespace(
        _instance=SimpleNamespace(
            model=reranker_model,
            tokenizer=object(),
            device=_Device("cuda:0"),
        )
    )
    reranker_identity = _prepare_reranker(
        reranker, lanes=2, streams=[None, None], origin=None
    )
    assert reranker_identity["device"] == "cuda:0"
    assert reranker._instance._experiment_tokenizer_lock is not None


def test_embedding_shape_reads_parallel_flag_from_llama_index_adapter() -> None:
    sentence_transformer = _FakeSentenceTransformer()
    model, parallel_process = _embedding_model(
        SimpleNamespace(
            _model=sentence_transformer,
            _parallel_process=False,
            _target_devices=None,
        )
    )

    assert model is sentence_transformer
    assert parallel_process is False


@pytest.mark.parametrize(
    "embedder",
    [
        SimpleNamespace(
            _model=_FakeSentenceTransformer(),
            _parallel_process=True,
            _target_devices=None,
        ),
        SimpleNamespace(
            _model=_FakeSentenceTransformer(),
            _parallel_process=False,
            _target_devices=["cuda:0"],
        ),
        SimpleNamespace(_model=_FakeSentenceTransformer(), _target_devices=None),
        SimpleNamespace(_model=None, _parallel_process=False, _target_devices=None),
    ],
)
def test_embedding_shape_rejects_enabled_or_unknown_process_mode(embedder) -> None:
    with pytest.raises(RuntimeError):
        _embedding_model(embedder)


def test_encode_guard_allows_default_and_frozen_device_only() -> None:
    signature = _encode_signature()
    _validate_encode_call(signature, torch, torch.device("cpu"), (), {})
    _validate_encode_call(
        signature, torch, torch.device("cpu"), (), {"device": "cpu"}
    )


def test_retry_guard_rechecks_process_flags_at_each_embedding_call() -> None:
    embedder = SimpleNamespace(
        _parallel_process=False,
        _target_devices=None,
        _embed_with_retry=lambda values: values,
    )
    _install_embed_retry_guard(embedder)

    assert embedder._embed_with_retry(["input"]) == ["input"]
    embedder._parallel_process = True
    with pytest.raises(RuntimeError, match="experiment_embedding_parallel_process_unsupported"):
        embedder._embed_with_retry(["input"])


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"pool": {"processes": []}}, "experiment_embedding_multiprocess_pool_rejected"),
        ({"device": ["cuda:0", "cuda:1"]}, "experiment_embedding_multiprocess_devices_rejected"),
        ({"device": "cuda:0"}, "experiment_embedding_device_change_rejected"),
    ],
)
def test_encode_guard_rejects_process_pool_device_lists_and_device_change(kwargs, code) -> None:
    with pytest.raises(RuntimeError, match=code):
        _validate_encode_call(_encode_signature(), torch, torch.device("cpu"), (), kwargs)
