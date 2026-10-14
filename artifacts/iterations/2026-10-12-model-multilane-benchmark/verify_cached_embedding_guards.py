"""Verify cached LlamaIndex/ST guard ownership without loading weights or CUDA."""

from __future__ import annotations

import inspect

from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from sentence_transformers import SentenceTransformer
from wenjin.model_api.experiment_adapter import _install_embed_retry_guard


class _FakeArray(list):
    def tolist(self):
        return list(self)


class _FakeSentenceModel:
    def encode(self, inputs, **kwargs):
        return _FakeArray([[1.0, 0.0] for _ in inputs])


def main() -> None:
    signature = inspect.signature(HuggingFaceEmbedding.__init__)
    assert signature.parameters["parallel_process"].default is False
    assert not hasattr(SentenceTransformer, "_parallel_process")
    encode_parameters = inspect.signature(SentenceTransformer.encode).parameters
    assert {"pool", "device"}.issubset(encode_parameters)

    embedder = HuggingFaceEmbedding.model_construct(
        _model=_FakeSentenceModel(),
        _parallel_process=False,
        _target_devices=None,
        normalize=True,
        show_progress_bar=False,
        embed_batch_size=16,
    )
    _install_embed_retry_guard(embedder)
    assert embedder._embed_with_retry(["input"]) == [[1.0, 0.0]]

    embedder._parallel_process = True
    try:
        embedder._embed_with_retry(["input"])
    except RuntimeError as exc:
        assert str(exc) == "experiment_embedding_parallel_process_unsupported"
    else:
        raise AssertionError("mutated LlamaIndex multiprocessing flag was not rejected")

    embedder._parallel_process = False
    embedder._target_devices = ["cuda:0"]
    try:
        embedder._embed_with_retry(["input"])
    except RuntimeError as exc:
        assert str(exc) == "experiment_embedding_parallel_process_unsupported"
    else:
        raise AssertionError("mutated LlamaIndex target devices were not rejected")
    print("cached_embedding_process_guard=passed")


if __name__ == "__main__":
    main()
