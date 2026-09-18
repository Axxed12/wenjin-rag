"""验证独立评测环境：发布时间边界、关键指标导入与无需API的实际计算。"""

import asyncio
import importlib.metadata
import json
import os
import tomllib
from datetime import datetime
from pathlib import Path

# 在导入ragas前关闭遥测；本检查不读取.env或调用模型API。
os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from ragas import SingleTurnSample  # noqa: E402
from ragas.metrics import (  # noqa: E402
    Faithfulness,
    IDBasedContextPrecision,
    IDBasedContextRecall,
    LLMContextPrecisionWithReference,
    LLMContextRecall,
)

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    lock_path = Path(__file__).with_name("uv.lock")
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    cutoff = datetime.fromisoformat("2026-07-15T23:59:59+08:00")
    violations = []
    checked = 0
    for package in lock["package"]:
        artifacts = package.get("wheels", []) + ([package["sdist"]] if "sdist" in package else [])
        for artifact in artifacts:
            uploaded = artifact.get("upload-time")
            if uploaded is None:
                raise RuntimeError(f"锁文件缺少发布时间：{package['name']}")
            checked += 1
            if datetime.fromisoformat(uploaded.replace("Z", "+00:00")) > cutoff:
                violations.append({"package": package["name"], "uploaded": uploaded})
    if violations:
        raise RuntimeError(f"存在截止日之后的依赖文件：{violations}")

    # 2个返回ID命中1个参考ID：precision=1/2，recall=1/1。
    sample = SingleTurnSample(
        retrieved_context_ids=["relevant", "irrelevant"],
        reference_context_ids=["relevant"],
    )
    precision = await IDBasedContextPrecision().single_turn_ascore(sample)
    recall = await IDBasedContextRecall().single_turn_ascore(sample)
    assert precision == 0.5 and recall == 1.0
    assert all((Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall))
    report = {
        "scope": "installation and deterministic metric smoke; not OSG quality evaluation",
        "versions": {name: importlib.metadata.version(name) for name in
                     ("ragas", "openai", "langchain-community", "langchain-openai")},
        "cutoff": cutoff.isoformat(),
        "lock_artifacts_checked": checked,
        "cutoff_violations": violations,
        "id_metric_smoke": {"precision": precision, "recall": recall},
        "llm_metrics": "imports passed; no LLM call performed",
        "api_calls": 0,
        "status": "PASS",
    }
    output = ROOT / "artifacts/ragas-install-check.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
