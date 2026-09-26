"""在validation上比较五库候选索引的dense、BM25、RRF和0.6B重排。

复用`run_retrieval_eval.metrics/summarize`做行级证据覆盖，只补两件现有脚本没有的事：
1) 按题集的library+version路由到对应候选索引；2) PDF证据只有物理页、没有行号，
需要单独的页级判定（既有实现遇到start_line=None会直接报错）。

证据判定沿用"Top-K chunk联合覆盖整段证据"，并已实测：blocks-split下validation的
51个行级证据，行号联合覆盖与按文本重建覆盖零分歧。
只使用validation（50题）；test保持冻结，不参与调参。
运行：uv run python evaluation/run_multicorpus_eval.py

父子化候选有两条臂，用 `--parent-order` 选：
- `before-fusion`（默认）：融合前就把 child 换成父块，**重排模型读的是父块正文**；
- `after-rerank`：检索与重排在 child 粒度上做，只在返回前换成父块（小块匹配、
  小块排序、只把大块用于返回）。这条臂多一列 `returned`：`rerank` 是重排看到的那份
  （child），`returned` 才是交给调用方的证据（父块），两者在 k=5 上的差别就是返回窗口
  被放大的净效应。两条臂都按**父块身份**做融合去重，否则同一父块的多个 child 会各占
  一个候选名额。
"""

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evaluation.run_retrieval_eval import metrics, summarize  # noqa: E402
from wenjin.retrieval.router import normalize_library  # noqa: E402

LEADING_VERSION = re.compile(r"^\d+(?:\.\d+)*")


def route_arguments(dataset_library: str, dataset_version: str) -> tuple[str, str | None]:
    """把题集里的展示名和版本串转成路由参数；版本只取开头的数字段。"""
    match = LEADING_VERSION.match(dataset_version.strip())
    return normalize_library(dataset_library), match.group(0) if match else None


def evidence_libraries(evidence: list[dict]) -> list[str]:
    """证据文件所在的语料库目录；跨库题用单库路由无法覆盖全部证据。"""
    return sorted({span["path"].split("/")[1] for span in evidence})


def chunks_required(bm25_path: Path, span: dict) -> int:
    """该证据在索引里跨几个chunk；>=2说明必须靠联合召回才能覆盖。"""
    with closing(sqlite3.connect(bm25_path)) as database:
        if span.get("page") is not None:
            return database.execute(
                "SELECT COUNT(*) FROM chunks WHERE source_path = ? AND page_number = ?",
                (span["path"], span["page"]),
            ).fetchone()[0]
        return database.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_path = ? AND start_line <= ? "
            "AND end_line >= ?",
            (span["path"], span["end_line"], span["start_line"]),
        ).fetchone()[0]


def page_span_hit(top: list[dict], span: dict) -> int | None:
    """返回第一个落在该物理页上的候选名次；没有则返回None。"""
    return next(
        (
            rank
            for rank, candidate in enumerate(top, start=1)
            if candidate["source_path"] == span["path"]
            and candidate.get("page_number") == span["page"]
        ),
        None,
    )


def evidence_metrics(
    candidates: list[dict], evidence: list[dict], *, k: int,
    blank_lines: dict[str, frozenset[int]] | None = None,
) -> dict:
    """行级证据交给既有metrics，页级证据单独判定，再合并成同一结构。

    `blank_lines` 透传给 `metrics`：空行不承载内容，把它算作"必须覆盖"会把
    "内容全在、只是中间隔了个空行"判成未覆盖（见 `wenjin.coverage`）。
    """
    top = candidates[:k]
    line_spans = [span for span in evidence if span.get("page") is None]
    page_spans = [span for span in evidence if span.get("page") is not None]
    parts = [metrics(top, [span], k=k, blank_lines=blank_lines) for span in line_spans]
    page_ranks = [page_span_hit(top, span) for span in page_spans]
    recalls = [part["span_recall"] for part in parts] + [
        float(rank is not None) for rank in page_ranks
    ]
    singles = [part["single_chunk_span_rate"] for part in parts] + [
        float(rank is not None) for rank in page_ranks
    ]
    ranks = [part["first_evidence_touch_rank"] for part in parts] + page_ranks
    touched = [rank for rank in ranks if rank is not None]
    first_rank = min(touched) if touched else None
    return {
        "returned": len(top),
        "k": k,
        "span_recall": statistics.fmean(recalls),
        "single_chunk_span_rate": statistics.fmean(singles),
        "all_spans_hit": all(recalls),
        "any_span_hit": any(recalls),
        "spans_total": len(evidence),
        "spans_covered": int(sum(recalls)),
        "page_level_spans": len(page_spans),
        "first_evidence_touch_rank": first_rank,
        "reciprocal_rank": 0.0 if first_rank is None else 1.0 / first_rank,
    }


def dataset_label(dataset: Path, *, root: Path = ROOT) -> str:
    """报告里给题集一个标识，且不指认开发机的绝对路径。

    题集**可以在给定的 `root` 之外**——评测临时根里的候选时，题集往往仍取仓库那一份。
    那种情况下 `relative_to` 会抛 `ValueError`。而这个字段只是报告里的一个标识，
    **不该为它让跑完的一整轮评测作废**：它是在 50 题全部跑完、模型也释放之后才写的。
    落在 root 之外时退回文件名。
    """
    try:
        return str(dataset.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return dataset.name


def blank_line_map(route_path: Path, evidence: list[dict], *,
                   root: Path = ROOT) -> dict[str, frozenset[int]]:
    """证据文件里**解析后为空**的行号。

    必须用解析后的文本判断，不能用原始 HTML：像 `</div>` 这样的纯标记行原文非空，
    但行映射解析后是空行，切片时会被当成块分隔符。用原文判断会得出错误的空行集合。

    只解析**确实出现在行级证据里**的文件：3D Tiles 那份 51MB / 359 页的 PDF 不参与
    行级判定，没必要为它付解析开销。

    `root` 默认是仓库根；评测临时根里的候选时由 `--root` 传进来，否则会去读仓库那棵
    语料树、和被测的那份对不上。
    """
    from wenjin.ingestion.multiformat import load_retrieval_documents

    wanted = {span["path"] for span in evidence if span.get("start_line") is not None}
    if not wanted:
        return {}
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    result: dict[str, frozenset[int]] = {}
    for item in route["candidates"]:
        manifest_path = root / item["manifest_path"]
        if not manifest_path.is_file():
            continue
        raw_root = root / "raw_data"
        for document in load_retrieval_documents(manifest_path, raw_root):
            path = document["source_path"]
            if path not in wanted or path in result:
                continue
            result[path] = frozenset(
                number for number, line in enumerate(document["text"].splitlines(), start=1)
                if not line.strip()
            )
    return result


def dedupe_by_parent(candidates: list[dict]) -> list[dict]:
    """按父块去重，保留名次最好的那条；没有 `parent_id` 的候选按 chunk_id 去重。

    只用在"重排之后才换成父块"那条臂上：换成父块的时机越晚，同一条证据被重复计入的
    机会越多，而重复的结果等于凭空吃掉一个证据名额。正常路径上融合已经按父块身份合并，
    所以这是兜底——把这条不变量放在替换发生的地方，而不是依赖上游那一步的键怎么取。
    """
    seen: set = set()
    kept: list[dict] = []
    for item in candidates:
        key = item.get("parent_id") or item["chunk_id"]
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def main() -> None:
    parser = argparse.ArgumentParser(description="五库候选索引的validation检索对照")
    # --root 让这个脚本也能评测**临时根**里的候选（父子片段这类实验建在真拷贝里，不碰
    # 仓库资产）。三个路径参数的默认值因此不能在构造 parser 时就算死——它们要跟着
    # 给定的 root 走，所以先留 None，解析完再补。不传 --root 时结果与从前完全一致。
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="项目根。默认是仓库根；指向临时根即可评测那里的候选索引。")
    parser.add_argument("--route", type=Path, default=None)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-reranker", action="store_true", help="只跑dense/BM25/RRF")
    # 父子化候选的两条臂。默认值保持从前那一条，行为一个字都不变。
    parser.add_argument(
        "--parent-order",
        choices=["before-fusion", "after-rerank"],
        default="before-fusion",
        help=(
            "父子替换发生在哪一步（只对父子化候选有意义，非父子化候选两者等价）："
            "before-fusion=默认，融合前就换成父块，检索与**重排**看到的都是父块正文；"
            "after-rerank=小块匹配、小块排序，重排后才换成父块返回"
        ),
    )
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    arguments.route = arguments.route or root / "configs/indexes/multicorpus-candidates-002.json"
    arguments.dataset = arguments.dataset or root / "evaluation/datasets/rag-validation-001.jsonl"
    arguments.output = arguments.output or (
        root / "artifacts/corpus-acceptance-002/validation-retrieval-eval.json"
    )

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from qdrant_client import QdrantClient

    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query_vector
    from wenjin.retrieval.fusion import identity_by_parent, rrf_fuse
    from wenjin.retrieval.parents import resolve_parents
    from wenjin.retrieval.rerank import Qwen3Reranker, candidate_document, rank_candidates
    from wenjin.retrieval.router import MultiCorpusRouter
    from wenjin.settings import Settings

    settings = Settings()
    dataset = [
        json.loads(line)
        for line in arguments.dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    embedder = settings.embedding()
    reranker = None
    routing = None
    # 一次性建好全数据集的"解析后空行"表：空行不算证据，见 evidence_metrics。
    blank_lines = blank_line_map(
        arguments.route.resolve(),
        [span for row in dataset for span in row["evidence"]],
        root=root,
    )
    print(f"空行表：{len(blank_lines)} 个证据文件，"
          f"共 {sum(len(v) for v in blank_lines.values())} 个空行")
    records = []
    try:
        if not arguments.no_reranker:
            reranker = Qwen3Reranker(
                settings.reranker_model_path,
                device="cuda" if torch.cuda.is_available() else "cpu",
                max_length=1024,
                batch_size=2,
            )
        # 构造路由时会逐个校验候选索引；任一不合格就不会进入本次评测。
        routing = MultiCorpusRouter.from_route_file(
            arguments.route.resolve(),
            root=root,
            qdrant_client=client,
            embedder=embedder,
            reranker=reranker,
        )
        for row in dataset:
            library, version = route_arguments(row["library"], row["version"])
            index = routing.resolve(library, version)[0]
            vector = embedder.get_query_embedding(row["query"])
            dense_started = time.perf_counter()
            dense = [
                {"library": library, "score": point.score, **point.payload}
                for point in dense_query_vector(
                    client, vector, index.collection, top_k=arguments.candidate_k
                )
            ]
            dense_seconds = time.perf_counter() - dense_started
            terms = query_terms(row["query"])
            bm25_started = time.perf_counter()
            keyword = (
                [{"library": library, **item}
                 for item in bm25_query(index.bm25_path, row["query"], top_k=arguments.candidate_k)]
                if terms else []
            )
            bm25_seconds = time.perf_counter() - bm25_started
            after_rerank = arguments.parent_order == "after-rerank"
            fusion_started = time.perf_counter()
            if after_rerank:
                # 小块匹配、小块排序、只把大块用于返回：检索与重排都停在 child 粒度，
                # `returned` 才是父块。融合仍按**父块身份**去重——同一父块的多个 child
                # 若各占一条，候选名额就被同一段正文吃掉多次（而正文仍是 child 自己的，
                # 所以这一步只影响"谁进候选池"，不影响重排看到的文本）。
                # 非父子化候选这里零开销（没有 parent_id 就不读父块表）。
                fused = rrf_fuse(
                    {"dense": dense, "bm25": keyword},
                    k=arguments.rrf_k,
                    identity=identity_by_parent,
                )
            else:
                # 默认臂：融合前就换成父块，于是**重排模型读的是被放大的父块正文**。
                dense = resolve_parents(dense, bm25_path=index.bm25_path)
                keyword = resolve_parents(keyword, bm25_path=index.bm25_path)
                fused = rrf_fuse({"dense": dense, "bm25": keyword}, k=arguments.rrf_k)
            fusion_seconds = time.perf_counter() - fusion_started
            rerank_started = time.perf_counter()
            if reranker is not None and fused:
                scores = reranker.score(row["query"], [candidate_document(item) for item in fused])
                ranked = rank_candidates(fused, scores)
            else:
                ranked = fused
            rerank_seconds = time.perf_counter() - rerank_started
            # 重排之后、交给调用方之前才换成父块。按父块去重在这里是兜底：
            # 融合已按父块身份合并，正常一个父块只会有一条；重复等于同一段原文占两条证据。
            returned = (
                dedupe_by_parent(resolve_parents(ranked, bm25_path=index.bm25_path))
                if after_rerank else ranked
            )
            retrievers = {"dense": dense, "bm25": keyword, "rrf": fused}
            if reranker is not None:
                retrievers["rerank"] = ranked
            if after_rerank:
                # 多一列 returned：与"重排看到的那份"（child）分开报，
                # 两者在 k=5 上的差别就是"返回窗口被放大"的净效应。
                retrievers["returned"] = returned
            records.append({
                "id": row["id"],
                "library": library,
                "dataset_library": row["library"],
                "version": index.version,
                "query": row["query"],
                "query_type": row.get("query_type"),
                "bm25_terms": terms,
                "evidence_spans": len(row["evidence"]),
                "max_chunks_per_span": max(
                    chunks_required(index.bm25_path, span) for span in row["evidence"]
                ),
                "evidence_libraries": evidence_libraries(row["evidence"]),
                "metrics": {
                    name: {f"at_{k}": evidence_metrics(
                        results, row["evidence"], k=k, blank_lines=blank_lines)
                           for k in (5, 20)}
                    for name, results in retrievers.items()
                },
                "top_ids": {name: [item["chunk_id"] for item in results[:5]]
                            for name, results in retrievers.items()},
                "timing_seconds": {
                    "dense": round(dense_seconds, 6),
                    "bm25": round(bm25_seconds, 6),
                    "fusion": round(fusion_seconds, 6),
                    "rerank": round(rerank_seconds, 6),
                },
            })
    finally:
        if routing is not None:
            routing.close()
        else:
            client.close()
            if reranker is not None:
                reranker.close()

    retriever_names = ["dense", "bm25", "rrf"] + (["rerank"] if reranker is not None else [])
    if arguments.parent_order == "after-rerank":
        # returned 是"调用方真正拿到的那份"（父块）；rerank 是"重排看到的那份"（child）。
        retriever_names.append("returned")
    libraries = sorted({record["library"] for record in records})

    def summary_for(records_subset: list[dict]) -> dict:
        return {name: {f"at_{k}": summarize(records_subset, name, k) for k in (5, 20)}
                for name in retriever_names}

    def latency_for(records_subset: list[dict]) -> dict:
        return {
            stage: {
                "mean": round(statistics.fmean(r["timing_seconds"][stage] for r in records_subset), 4),
                "stdev": round(statistics.pstdev(r["timing_seconds"][stage] for r in records_subset), 4),
            }
            for stage in ("dense", "bm25", "fusion", "rerank")
        }

    report = {
        "route_id": json.loads(arguments.route.read_text(encoding="utf-8-sig"))["route_id"],
        "dataset": dataset_label(arguments.dataset, root=root),
        "dataset_role": "validation only; test 40题保持冻结且未参与本次调参",
        "candidate_k": arguments.candidate_k,
        "rrf_k": arguments.rrf_k,
        # 只有非默认臂才多写这两项：默认臂的产物必须与从前的逐题对照可比，
        # 不被新增字段搅乱（开关本身不改默认路径的任何一条记录）。
        **(
            {
                "parent_order": arguments.parent_order,
                "parent_order_note": (
                    "重排在 child 正文上做（小块匹配、小块排序）；`rerank` 列是重排看到的那份，"
                    "`returned` 列才是换成父块之后交给调用方的证据。两者在 k=5 上的差别就是"
                    "返回窗口被放大的净效应。"
                ),
            }
            if arguments.parent_order != "before-fusion" else {}
        ),
        "reranker": None if reranker is None else {
            "model": settings.reranker_model_path.name,
            "max_length": reranker.max_length,
            "batch_size": reranker.batch_size,
        },
        "evidence_match": (
            "复用run_retrieval_eval.metrics：Top-K chunk联合覆盖整段行级证据；"
            "PDF证据只登记物理页，按页判定并单列"
        ),
        "boundary": (
            "本机小样本validation对照；不是第三方盲测，也不代表检索效果验收通过。"
            "RRF和reranker分数只是排序信号，不能当作答案置信度"
        ),
        "questions": len(records),
        "summary": {
            "overall": summary_for(records),
            "per_library": {library: summary_for(
                [r for r in records if r["library"] == library]) for library in libraries},
        },
        "latency_seconds": {
            "overall": latency_for(records),
            "per_library": {library: latency_for(
                [r for r in records if r["library"] == library]) for library in libraries},
        },
        "multi_span_questions": [
            {
                "id": record["id"],
                "library": record["library"],
                "spans": record["evidence_spans"],
                "spans_total": record["metrics"]["dense"]["at_5"]["spans_total"],
                "covered_at_5": {
                    name: record["metrics"][name]["at_5"]["spans_covered"]
                    for name in retriever_names
                },
            }
            for record in records if record["evidence_spans"] > 1
        ],
        # 单条证据跨多个chunk的题：必须靠联合召回覆盖，单独列出逐题结果。
        "joint_recall_questions": [
            {
                "id": record["id"],
                "library": record["library"],
                "max_chunks_per_span": record["max_chunks_per_span"],
                "spans_total": record["metrics"]["dense"]["at_5"]["spans_total"],
                "covered": {
                    name: {
                        f"at_{k}": record["metrics"][name][f"at_{k}"]["spans_covered"]
                        for k in (5, 20)
                    }
                    for name in retriever_names
                },
            }
            for record in records if record["max_chunks_per_span"] >= 2
        ],
        # 跨库题（证据分布在两个语料库）用单库路由必然覆盖不全，必须与排序失败分开。
        "cross_library_questions": [
            {
                "id": record["id"],
                "dataset_library": record["dataset_library"],
                "evidence_libraries": record["evidence_libraries"],
                "note": "证据跨库，单库路由无法覆盖全部证据；需要跨库扇出才能覆盖",
                "covered_at_5": {
                    name: record["metrics"][name]["at_5"]["spans_covered"]
                    for name in retriever_names
                },
                "spans_total": record["metrics"]["dense"]["at_5"]["spans_total"],
            }
            for record in records if len(record["evidence_libraries"]) > 1
        ],
        "failures_at_5": {
            name: [record["id"] for record in records
                   if not record["metrics"][name]["at_5"]["all_spans_hit"]]
            for name in retriever_names
        },
        "failure_reasons_at_5": {
            name: {
                record["id"]: (
                    "证据跨库，单库路由无法覆盖全部证据"
                    if len(record["evidence_libraries"]) > 1
                    else "单库内排序未在Top-5覆盖全部证据"
                )
                for record in records
                if not record["metrics"][name]["at_5"]["all_spans_hit"]
            }
            for name in retriever_names
        },
        "details": records,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"]["overall"], ensure_ascii=False, indent=2))
    print(json.dumps(report["latency_seconds"]["overall"], ensure_ascii=False))
    print(f"报告：{arguments.output}")


if __name__ == "__main__":
    main()
