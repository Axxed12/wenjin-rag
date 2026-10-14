"""只读冒烟验证，不创建用户、token或资料授权。"""

import json
import math
import time
from pathlib import Path

import httpx

from wenjin.models.providers import RemoteModelClient

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def main():
    checks = {}
    with httpx.Client(trust_env=False, timeout=5) as http:
        for _ in range(60):
            try:
                response = http.post(
                    "http://127.0.0.1:18765/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {},
                            "clientInfo": {"name": "manual-startup-check", "version": "1"},
                        },
                    },
                    headers={"Accept": "application/json, text/event-stream"},
                )
                checks["mcp_without_credentials_status"] = response.status_code
                break
            except httpx.TransportError:
                time.sleep(1)
        checks["management_health_status"] = http.get(
            "http://127.0.0.1:18777/api/health"
        ).status_code
        checks["admin_page_status"] = http.get("http://127.0.0.1:18777/admin/").status_code
        checks["model_without_credentials_status"] = http.get(
            "http://127.0.0.1:18766/v1/identity"
        ).status_code
    token = (ROOT / "deploy/secrets/manual-agent/model_api_token").read_text().strip()
    model = RemoteModelClient(
        "http://127.0.0.1:18766",
        token,
        expected_embedding_model="Qwen3-Embedding-0.6B",
        expected_reranker_model="Qwen3-Reranker-0.6B",
    )
    vectors = model.embed(["手动Agent服务启动检查"], input_kind="query")
    checks["embedding_vectors"] = len(vectors)
    checks["embedding_dimensions"] = len(vectors[0])
    checks["embedding_norm"] = math.sqrt(sum(x * x for x in vectors[0]))
    checks["model_admission"] = model.identity.get("admission")
    checks["rerank_scores"] = model.rerank(
        "维护周期是多少？",
        ["维护周期为每周一次。", "设备外壳为蓝色。"],
        max_length=1024,
        batch_size=2,
    )
    model.close()
    checks["passed"] = (
        checks.get("mcp_without_credentials_status") == 401
        and checks["management_health_status"] == 200
        and checks["admin_page_status"] == 200
        and checks["model_without_credentials_status"] == 401
        and checks["embedding_dimensions"] == 1024
        and len(checks["rerank_scores"]) == 2
        and all(math.isfinite(x) for x in checks["rerank_scores"])
    )
    (OUT / "startup-checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(checks, ensure_ascii=False))
    if not checks["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
