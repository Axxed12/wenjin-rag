"""CPU-only test of the MCP experiment client's lane-bound identity patch."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[3]
PROVIDER = REPO / "src" / "wenjin" / "models" / "providers.py"
EXPECTED_LANES_PATCH = '''            admission = identity["admission"]
            # 仅隔离benchmark client允许已明确绑定的实验lane身份。
            expected_lanes = int(os.environ.get("WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES", "1"))
            if expected_lanes not in {1, 2, 4}:
                raise ValueError
            if expected_lanes != 1 and identity.get("experiment_adapter", {}).get("lane_count") != expected_lanes:
                raise ValueError
'''


def patched_provider(tmp_path: Path):
    source = PROVIDER.read_text(encoding="utf-8")
    patched = source.replace("import math\n", "import math\nimport os\n", 1)
    patched = patched.replace('            admission = identity["admission"]\n', EXPECTED_LANES_PATCH, 1)
    patched = patched.replace('or admission["active"] != 1',
                              'or admission["active"] != expected_lanes', 1)
    assert patched != source
    assert 'or admission["active"] != expected_lanes' in patched
    path = tmp_path / "providers_experiment.py"
    path.write_text(patched, encoding="utf-8")
    name = f"providers_experiment_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def make_identity(active: int, adapter_lane: int | None = None) -> dict:
    value = {
        "protocol": "wenjin-model-api/1",
        "implementation_sha256": "c" * 64,
        "embedding": {
            "model_id": "embed-test", "asset_sha256": "a" * 64,
            "dimensions": 2, "normalized": True, "input_protocol": "raw-text-v1",
        },
        "reranker": {
            "model_id": "rerank-test", "asset_sha256": "b" * 64,
            "max_length": 384, "batch_size": 3,
        },
        "limits": {"max_batch_size": 8, "max_text_chars": 1000, "max_documents": 16},
        "admission": {"active": active, "max_waiting": 8, "max_wait_seconds": 30.0},
    }
    if adapter_lane is not None:
        value["experiment_adapter"] = {"lane_count": adapter_lane}
    return value


def create_client(module, identity: dict):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=identity, request=request)
    )
    return module.RemoteModelClient(
        "http://identity.test", "dummy-token",
        expected_embedding_model="embed-test", expected_reranker_model="rerank-test",
        transport=transport,
    )


def test_default_identity_accepts_one_lane_and_rejects_two(tmp_path, monkeypatch):
    module = patched_provider(tmp_path)
    monkeypatch.delenv("WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES", raising=False)
    client = create_client(module, make_identity(1))
    client.close()

    with pytest.raises(module.ModelProviderError) as error:
        create_client(module, make_identity(2, adapter_lane=2))
    assert error.value.code == "model_identity_invalid"


def test_explicit_two_lane_identity_is_accepted_and_mismatch_rejected(tmp_path, monkeypatch):
    module = patched_provider(tmp_path)
    monkeypatch.setenv("WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES", "2")
    client = create_client(module, make_identity(2, adapter_lane=2))
    client.close()

    for identity in (make_identity(1, adapter_lane=2), make_identity(2, adapter_lane=1),
                     make_identity(2)):
        with pytest.raises(module.ModelProviderError) as error:
            create_client(module, identity)
        assert error.value.code == "model_identity_invalid"


def test_unsupported_expected_lane_is_rejected(tmp_path, monkeypatch):
    module = patched_provider(tmp_path)
    monkeypatch.setenv("WENJIN_EXPERIMENT_EXPECTED_MODEL_LANES", "3")
    with pytest.raises(module.ModelProviderError) as error:
        create_client(module, make_identity(3, adapter_lane=3))
    assert error.value.code == "model_identity_invalid"
