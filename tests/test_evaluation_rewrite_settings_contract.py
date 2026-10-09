"""四臂运行时的query-rewrite回退配置只从显式临时.env载入。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from wenjin.evaluation_tasks.runtime import EvaluationRuntimeFactory
from wenjin.settings import Settings


pytestmark = pytest.mark.quick


def test_standard_settings_uses_private_query_rewrite_loader_with_temp_env(tmp_path: Path):
    project_root = tmp_path / "synthetic-runtime-root"
    project_root.mkdir()
    (project_root / ".env").write_text(
        "QDRANT_API_KEY=synthetic-qdrant-key\n"
        "EMBEDDING_MODEL_PATH=synthetic-embedding-model\n"
        "RERANKER_MODEL_PATH=synthetic-reranker-model\n"
        "QUERY_REWRITE_BASE_URL=https://rewrite.invalid/v1\n"
        "QUERY_REWRITE_API_KEY=synthetic-private-provider-key\n"
        "QUERY_REWRITE_MODEL=synthetic-rewriter-id\n"
        "QUERY_REWRITE_TIMEOUT_SECONDS=3.5\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=project_root / ".env")
    assert not getattr(settings, "query_rewrite_api_key", None)

    factory = EvaluationRuntimeFactory(
        root=project_root, app_state=SimpleNamespace(settings=settings),
    )
    rewrite = factory._rewrite_configuration()

    assert rewrite["base_url"] == "https://rewrite.invalid/v1"
    assert rewrite["model"] == "synthetic-rewriter-id"
    assert rewrite["timeout"] == 3.5
    assert rewrite["api_key"] == "synthetic-private-provider-key"
    assert "api_key" not in type(settings).model_fields
