"""把"同父块多 child 进池"和"池子变大"拆开的对照矩阵。

上一轮的 `child-pool` 臂相对 `after-rerank` 同时动了两件事：融合不再按父块去重
（同父块的多个 child 都能进重排池），以及池子里因此多了若干条候选。两个变量捆在
一起，看不出增益归谁。这里用两种办法把它们分开：

1. **扫 `candidate_k`**：让两条臂的池子条数在某个 k 上自然对齐，再看 child 粒度指标；
2. **钉死 `pool_cap`**：两条臂都只重排同样多的条数，重排算力相同，差别只剩池子成分
   （同样的名额是花在"覆盖更多父块"还是"允许同父块的兄弟占位"）。

指标一律复用 `parent_child_calibration.run_arm`，不另写一套，否则和已有三条臂不可比。

用法（不传 `--root` 就用仓库根，会评到真索引上，别这么干）：
    ./.venv/Scripts/python.exe tests/parent_child_debundle.py \
        --root "%TEMP%/wenjin-parented-mnfvc4vw" \
        --route configs/indexes/multicorpus-candidates-probe-003.json \
        --output artifacts/parent-child-verification/debundle-matrix.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本机系统代理会拦发往 Qdrant 的回环请求（httpx 读 Windows 注册表里的代理设置，
# 返回 502 且响应体为空），必须在建客户端之前关掉。
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

from tests.parent_child_calibration import run_arm  # noqa: E402

# (label, parent_order, candidate_k, pool_cap)
# `dedup-*`：融合按父块去重，一个父块只留一个代表 child；
# `nodedup-*`：融合只按 chunk_id 去重，同父块的兄弟都能进池。
# `k*` 是一路检索的候选数；`cap*` 是把进池条数钉死（None 表示不截断）。
MATRIX: list[tuple[str, str, int, int | None]] = [
    # 两条自然臂：k=20 时 dedup 池 18.3 条、nodedup 池 26.9 条，池子大小差 8.6。
    ("dedup-k20", "after-rerank", 20, None),
    ("nodedup-k20", "child-pool", 20, None),
    # 把 dedup 的 k 调到 40，池子涨到 27.7 ≈ nodedup-k20 的 26.9：大小对齐，
    # 看"多出来的名额是换成更多父块，还是留着同父块的兄弟"哪种更好。
    ("dedup-k40", "after-rerank", 40, None),
    ("nodedup-k40", "child-pool", 40, None),
    # 钉死重排条数：两边都只重排同样多的条数，重排算力相同，差别只剩池子成分。
    ("cap20-dedup", "after-rerank", 40, 20),
    ("cap20-nodedup", "child-pool", 40, 20),
    ("cap27-dedup", "after-rerank", 40, 27),
    ("cap27-nodedup", "child-pool", 40, 27),
    # 现役实现（融合前换父块，重排读父块正文）在两个 k 上的对照列。
    ("parentpool-k20", "before-fusion", 20, None),
    ("parentpool-k40", "before-fusion", 40, None),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="去重方式 × 池子大小的对照矩阵")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--route", type=Path, required=True)
    parser.add_argument("--dataset", type=Path,
                        default=ROOT / "evaluation/datasets/rag-validation-001.jsonl")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-reranker", action="store_true",
                        help="只在量池子大小时用：不加载重排模型，快得多")
    parser.add_argument("--only", default=None, help="只跑 label 含该子串的臂")
    arguments = parser.parse_args()

    root = arguments.root.resolve()
    route = arguments.route if arguments.route.is_absolute() else root / arguments.route
    route = route.resolve()
    dataset = arguments.dataset.resolve()
    if not route.is_file():
        raise SystemExit(f"路由清单不存在：{route}")

    reports = []
    for label, order, candidate_k, pool_cap in MATRIX:
        if arguments.only and arguments.only not in label:
            continue
        print(f"--- {label}: {order} candidate_k={candidate_k} pool_cap={pool_cap}",
              flush=True)
        report = run_arm(
            route, dataset, root=root,
            reranker=not arguments.no_reranker,
            parent_order=order, candidate_k=candidate_k, pool_cap=pool_cap,
        )
        report["label"] = label
        # 落盘前抹掉本机绝对路径：报告要能直接贴出去。
        report["root"] = "<TEMP>/wenjin-parented-*"
        report["route"] = "configs/indexes/multicorpus-candidates-probe-003.json"
        reports.append(report)
        summary = report["summary_at_5"]
        child = report["summary_at_5_child_granularity"]
        pool = report["pool"]
        print(
            f"    parent all_spans={summary['all_spans_hit_rate']:.4f} "
            f"mrr={summary['first_evidence_touch_mrr']:.4f} | "
            f"child all_spans={child['all_spans_hit_rate']:.4f} "
            f"mrr={child['first_evidence_touch_mrr']:.4f} | "
            f"pool={pool['size_mean']} parents={pool['distinct_parents_mean']} "
            f"raw_children={pool['raw_children_mean']} "
            f"parents_of_top_child={pool['top_child_distinct_parents_mean']}",
            flush=True,
        )
        if not arguments.no_reranker:
            import torch

            torch.cuda.empty_cache()

    output = {
        "note": (
            "去重方式 × 池子大小的对照矩阵。validation 是调参集，其数字不能用来宣称"
            "对外性能提升；只在临时根的真实候选索引上读，不改任何索引或语料。"
        ),
        "root": "<TEMP>/wenjin-parented-*（临时真拷贝）",
        "reports": reports,
        "status": "PASS" if all(item["status"] == "PASS" for item in reports) else "FAIL",
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"报告：{arguments.output}")


if __name__ == "__main__":
    main()
