"""逐库核验候选索引：三资产指纹、逐 chunk 来源绑定、唯一 ID、维度、双 Hash、页码。

真正的判定在 `wenjin.index_audit.audit_index`，本脚本只做编排和输出——
启动校验、候选验收和切换前检查用的是同一份实现，不再各自维护一套强弱不一的检查。

只读：不修改active注册表、索引或语料。
运行：uv run python scripts/verify_candidate_indexes.py --route configs/indexes/multicorpus-candidates-002.json
"""

import argparse
import json
import sys
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wenjin.index_audit import audit_index  # noqa: E402
from wenjin.index_registry import IndexTarget  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="逐库核验候选索引资产")
    parser.add_argument("--route", type=Path,
                        default=ROOT / "configs/indexes/multicorpus-candidates-002.json")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "artifacts/corpus-acceptance-002/candidate-index-audit.json")
    parser.add_argument("--target", type=Path,
                        help="只审计单个目标配置（例如验证active或某个候选）")
    parser.add_argument("--all-targets", dest="all_targets", action="store_true",
                        help="审计 configs/indexes 下的全部目标（含 active）；"
                             "冻结前的只读复验用这一条，不必手写循环")
    arguments = parser.parse_args()
    if arguments.target and arguments.all_targets:
        parser.error("--target 与 --all-targets 不能同时使用")

    if arguments.all_targets:
        # 复用目录模块的扫描：它同时覆盖公开与内部两处配置，并对同 ID 冲突报错。
        from wenjin.catalog import index_target_configs

        configs = [("all", item) for _, item in sorted(index_target_configs(ROOT).items())]
        route_id = "all-index-targets"
        if arguments.output == parser.get_default("output"):
            arguments.output = ROOT / "artifacts/index-freeze-check/index-audit.json"
    elif arguments.target:
        configs = [("single", json.loads(arguments.target.read_text(encoding="utf-8-sig")))]
        route_id = str(arguments.target)
    else:
        route_path = arguments.route.resolve()
        route = json.loads(route_path.read_text(encoding="utf-8-sig"))
        configs = [("route", item) for item in route["candidates"]]
        route_id = route["route_id"]

    from wenjin.settings import Settings

    settings = Settings()
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    try:
        results = []
        for _, item in configs:
            target = IndexTarget.from_dict(item)
            try:
                results.append(audit_index(target, root=ROOT, qdrant_client=client))
            except ResponseHandlingException as error:
                # 环境没起来时要说清楚是"没连上"，不能让它看起来像索引不合格。
                raise SystemExit(
                    f"Qdrant 不可用（{settings.qdrant_url}），无法复验索引 "
                    f"{target.target_id}：{error}\n"
                    "先把 Qdrant 起起来再复验；这次没有读到任何索引结论。"
                ) from error
    finally:
        client.close()

    failed = [item for item in results if item["status"] != "PASS"]
    report = {
        "route_id": route_id,
        "status": "PASS" if not failed else "FAIL",
        "total_chunks": sum(item["chunk_count"] for item in results),
        "targets": results,
        "boundary": (
            "逐条核验索引与manifest的绑定；未做检索效果评测，不代表检索质量通过。"
        ),
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": report["status"],
        "total_chunks": report["total_chunks"],
        "targets": [{
            "target_id": item["target_id"],
            "chunk_count": item["chunk_count"],
            "vector_dimension": item["vector_dimension"],
            "collection_status": item["collection_status"],
            "manifest_records": item["manifest_records"],
            "status": item["status"],
            "failed_checks": sorted(k for k, v in item["problems"].items() if v),
        } for item in results],
    }, ensure_ascii=False, indent=2))
    print(f"报告：{arguments.output}")
    raise SystemExit(report["status"] != "PASS")


if __name__ == "__main__":
    main()
