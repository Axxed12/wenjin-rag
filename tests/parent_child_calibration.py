"""校准：把父子契约判定跑到**真实索引**上，先确认检查器本身不产生假阳性。

检查器也要被检查。同一套判定在改动前后各跑一次：

- 改动**前**：真索引上的违规必须是 0。不是 0 就是我的判定写错了，不是实现错了；
- 改动**后**：同一套判定直接复用到新索引，数字才可比。

本脚本直接读已发布的 BM25 索引行，按 `source_path` 找回 manifest 登记的原文，
再用 `parent_child_probe` 的判定逐块比对。它**只读**，不改任何索引或语料。

用法（在项目根目录）：
    ./.venv/Scripts/python.exe tests/parent_child_calibration.py --indexes
    ./.venv/Scripts/python.exe tests/parent_child_calibration.py --evidence --no-reranker

`--evidence` 需要 Qdrant 与本地模型，跑的是 validation 50 题（test 集冻结，不跑）。
"""

import argparse
import json
import sqlite3
import statistics
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.parent_child_probe import (  # noqa: E402
    Chunk,
    document_report,
    evidence_violations,
)
from wenjin.index_audit import read_bm25_parents, read_bm25_rows  # noqa: E402
from wenjin.index_registry import resolve_project_path  # noqa: E402


def load_documents_for(
    manifest_path: Path, *, root: Path = ROOT
) -> tuple[dict[tuple, dict], dict[str, str | None]]:
    """按 manifest 的类型选加载器。

    返回 `(键 -> 解析单元, source_path -> manifest声明的解析器)`。

    键是 `(source_path, page_number)` 而不是光 `source_path`：一份 PDF 的每个物理页
    是**独立的解析单元**，共用一个 `source_path`。只用路径做键会让前面所有页被最后
    一页覆盖，检查于是报出成片的假阳性（这正是第一版校准暴露的问题）。

    两条加载路径的 `source_path` 口径不同（新语料带 `raw_data/<subdir>/` 前缀，
    OSG 是语料内相对路径），所以必须按 manifest 自己声明的键选，不能猜。
    """
    from wenjin.ingestion.multiformat import load_retrieval_documents

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    subdir = manifest.get("raw_data_subdir")
    uses_retrieval_scope = any("retrieval_eligible" in entry for entry in manifest["files"])
    if uses_retrieval_scope:
        documents = load_retrieval_documents(manifest_path, root / "raw_data")
    else:
        from ingest import load_documents

        documents = load_documents(manifest_path)
    parsers: dict[str, str | None] = {}
    for entry in manifest["files"]:
        parsers[entry["path"]] = entry.get("parser")
        if subdir:
            parsers[f"raw_data/{subdir}/{entry['path']}"] = entry.get("parser")
    return (
        {(document["source_path"], document.get("page_number")): document
         for document in documents},
        parsers,
    )


def chunk_from_row(row: dict) -> Chunk:
    """把 BM25 行还原成 `Chunk`；字段取公共字段，不依赖索引里的额外列。

    正文的键名两侧不同：`chunks` 表读出来是 `raw_text`，`parents` 表读出来是 `text`。
    """
    return Chunk(
        chunk_id=row["chunk_id"],
        source_path=row["source_path"],
        version=row["version"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        text=row["raw_text"] if "raw_text" in row else row["text"],
        strategy=row["strategy"],
        page_number=row.get("page_number"),
        parser=row.get("parser"),
        parent_id=row.get("parent_id"),
    )


def calibrate_index(
    label: str, manifest_path: Path, bm25_path: Path, *, root: Path = ROOT
) -> dict:
    """一个索引的逐块契约检查。"""
    documents, parsers = load_documents_for(manifest_path, root=root)
    rows, _ = read_bm25_rows(bm25_path)
    parent_rows = read_bm25_parents(bm25_path)
    by_unit: dict[tuple, list[Chunk]] = {}
    unmapped = []
    # 父块与 child 放进**同一批**块里再判定：父子关系（区间包含、无洞、引用可解析）
    # 只能靠两者同时在场才查得出来。父块不在 FTS 表里，所以必须单独读。
    for row in list(rows) + list(parent_rows or []):
        key = (row["source_path"], row.get("page_number"))
        if key not in documents:
            unmapped.append({"chunk_id": row["chunk_id"], "source_path": row["source_path"],
                             "page_number": row.get("page_number")})
            continue
        by_unit.setdefault(key, []).append(chunk_from_row(row))

    totals: dict[str, int] = {}
    examples: dict[str, list] = {}
    partial_pieces = 0
    # 单行硬切产生的半行片段是 blocks-split 的**既有正确行为**，单独计数、不算违规
    # （`wenjin.coverage` 也把它单列而不是当冲突）。除此以外的一切非零都是缺陷。
    allowed = {"partial_line_piece"}
    for key, chunks in by_unit.items():
        report = document_report(
            documents[key], chunks, declared_parser=parsers.get(key[0]),
        )
        partial_pieces += len(report["partial_line_piece"])
        for kind, items in report.items():
            if items and kind not in allowed:
                totals[kind] = totals.get(kind, 0) + len(items)
                examples.setdefault(kind, []).extend(items[:3])

    return {
        "label": label,
        "manifest_path": str(manifest_path.relative_to(root)).replace("\\", "/"),
        "bm25_path": str(bm25_path.relative_to(root)).replace("\\", "/"),
        "chunks": len(rows),
        "parents": None if parent_rows is None else len(parent_rows),
        "parented": parent_rows is not None,
        "parsed_units": len(by_unit),
        "unmapped_chunks": unmapped[:5],
        "unmapped_chunks_total": len(unmapped),
        # 单行硬切的半行片段：当前 blocks-split 就会产生，只有这一项允许非 0。
        "partial_line_pieces": partial_pieces,
        "violations": totals,
        "violation_total": sum(totals.values()),
        "examples": examples,
        "status": "PASS" if not totals and not unmapped else "FAIL",
    }


def route_indexes(route_path: Path, *, root: Path = ROOT) -> list[tuple[str, Path, Path]]:
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    return [
        (item["target_id"], root / item["manifest_path"], root / item["bm25_path"])
        for item in route["candidates"]
    ]


def active_index() -> tuple[str, Path, Path] | None:
    """active 索引也一并校准：它是 OSG 3.6.5，切分策略与多库候选不同。"""
    from wenjin.index_registry import IndexRegistryStore

    registry_path = ROOT / "data/indexes/index-registry.json"
    if not registry_path.is_file():
        return None
    target = IndexRegistryStore(registry_path).active()
    return (
        target.target_id,
        resolve_project_path(ROOT, target.manifest_path),
        resolve_project_path(ROOT, target.bm25_path),
    )


def run_indexes(route_path: Path, *, root: Path = ROOT) -> dict:
    results = [calibrate_index(*item, root=root) for item in route_indexes(route_path, root=root)]
    # active 索引只存在于真实仓库根，评测临时根时没有它。
    active = active_index() if root == ROOT else None
    if active is not None:
        try:
            results.append(calibrate_index(*active, root=root))
        except Exception as error:  # noqa: BLE001 - 校不准就报出来，不能静默跳过
            results.append({"label": active[0], "status": "ERROR", "detail": repr(error)})
    return {
        "mode": "indexes",
        "route": str(route_path.relative_to(root)).replace("\\", "/"),
        "note": (
            "校准用：真索引上的违规必须是 0，否则是判定写错而不是实现写错。"
            "partial_line_pieces 是单行硬切产生的半行片段，只有它允许非 0。"
        ),
        "indexes": results,
        "status": "PASS" if all(item.get("status") == "PASS" for item in results) else "FAIL",
    }


def _distinct_ids(items: list[dict]) -> int | None:
    """这批候选落在几个父块身份上；一条 id 都取不到时返回 None（不是 0，也不是 1）。"""
    keys = {item.get("parent_id") or item.get("chunk_id") for item in items}
    keys.discard(None)
    return len(keys) if keys else None


def _mean_or_none(values: list) -> float | None:
    values = [value for value in values if value is not None]
    return round(statistics.fmean(values), 2) if values else None


def run_arm(
    route_path: Path,
    dataset_path: Path,
    *,
    root: Path = ROOT,
    reranker: bool = True,
    candidate_k: int = 20,
    rrf_k: int = 60,
    char_budget: int = 8000,
    parent_order: str = "before-fusion",
    pool_cap: int | None = None,
) -> dict:
    """跑 validation 50 题的一条臂：返回指标、逐题明细、证据条数与去重判定。

    **指标用与基线同一套函数**（`evaluation.run_multicorpus_eval.evidence_metrics`
    与 `evaluation.run_retrieval_eval.summarize`），否则两条臂的数字不可比。

    两项口径要分开，混起来会得出不同结论：

    - `top_k_by_rank`：`ranked[:5]`，与评测脚本一致 → 用于 A/B 比对；
    - `returned_by_budget`：`select_context(ranked, top_k=5, char_budget=8000)`，
      即**服务实际返回**的证据条数（放不下就跳过，不截断）→ 用于条数实测。

    `pool_cap` 把进入重排的候选截到固定条数。去重方式一变，池子大小跟着变，两者本来
    捆在一起；固定 `pool_cap` 就能让两条臂的重排**算力相同**，差别只剩池子的成分。
    `candidate_k` 必须 ≥ `pool_cap`，否则截断之前候选就不够。
    """
    import os

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    import torch
    from qdrant_client import QdrantClient

    from evaluation.run_multicorpus_eval import (
        blank_line_map,
        dedupe_by_parent,
        evidence_metrics,
        route_arguments,
    )
    from evaluation.run_retrieval_eval import summarize
    from wenjin.indexing.bm25 import bm25_query, query_terms
    from wenjin.indexing.dense import dense_query_vector
    from wenjin.retrieval.fusion import rrf_fuse
    from wenjin.retrieval.parents import parent_identity, resolve_parents
    from wenjin.retrieval.rerank import Qwen3Reranker, candidate_document, rank_candidates
    from wenjin.retrieval.router import MultiCorpusRouter
    from wenjin.retrieval.service import select_context
    from wenjin.settings import Settings

    settings = Settings()
    dataset = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    def child_positions(bm25_path) -> dict:
        """`chunk_id -> 该 child 自己的位置`。用来把指标在**命中 child** 的粒度上再算一遍。

        父子化后返回的是父块，行区间更大，行级覆盖率会被**机械抬高**：同一段证据更容易
        被"更大的一段"盖住。要分清"真的召回更好"和"指标被抬高"，就得把同一批命中退回
        到 child 粒度再量一次——child 粒度不变好，说明提升主要来自区间变大。
        """
        with closing(sqlite3.connect(bm25_path)) as database:
            return {
                row[0]: {"source_path": row[1], "page_number": row[2],
                         "start_line": row[3], "end_line": row[4]}
                for row in database.execute(
                    "SELECT chunk_id, source_path, page_number, start_line, end_line FROM chunks"
                )
            }

    positions_cache: dict = {}
    # 空行表：证据区间里解析后为空的行不算必须覆盖，两条臂必须用同一份。
    # root 必须传下去：漏传会让空行表去读**仓库**那棵语料树，与被测的临时根对不上，
    # 两条臂的分母就不同了。
    blank_lines = blank_line_map(
        route_path.resolve(), [span for row in dataset for span in row["evidence"]], root=root
    )
    client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    embedder = settings.embedding()
    ranker = None
    routing = None
    records = []
    try:
        if reranker:
            ranker = Qwen3Reranker(
                settings.reranker_model_path,
                device="cuda" if torch.cuda.is_available() else "cpu",
                max_length=1024, batch_size=2,
            )
        routing = MultiCorpusRouter.from_route_file(
            route_path, root=root, qdrant_client=client, embedder=embedder, reranker=ranker,
        )
        for row in dataset:
            library, version = route_arguments(row["library"], row["version"])
            index = routing.resolve(library, version)[0]
            vector = embedder.get_query_embedding(row["query"])
            dense = [
                {"library": library, "score": point.score, **point.payload}
                for point in dense_query_vector(client, vector, index.collection, top_k=candidate_k)
            ]
            terms = query_terms(row["query"])
            keyword = (
                [{"library": library, **item}
                 for item in bm25_query(index.bm25_path, row["query"], top_k=candidate_k)]
                if terms else []
            )
            # 三条臂只在"什么时候把 child 换成父块"上不同：
            # - before-fusion：融合前就换 → 重排模型读的是**父块正文**；
            # - after-rerank：融合与重排都停在 child，返回前才换 → 重排读 **child 正文**；
            # - child-pool：同 after-rerank，但融合**不按父块去重**（只按 chunk_id），
            #   于是同一父块的多个 child 都能进重排池——用来分辨"重排粒度"和
            #   "按父块去重把候选砍掉了"这两个混淆因素。
            if parent_order == "before-fusion":
                fused = rrf_fuse(
                    {
                        "dense": resolve_parents(dense, bm25_path=index.bm25_path),
                        "bm25": resolve_parents(keyword, bm25_path=index.bm25_path),
                    },
                    k=rrf_k,
                )
            elif parent_order == "after-rerank":
                fused = rrf_fuse({"dense": dense, "bm25": keyword}, k=rrf_k,
                                 identity=parent_identity)
            elif parent_order == "child-pool":
                fused = rrf_fuse({"dense": dense, "bm25": keyword}, k=rrf_k,
                                 identity=lambda item: item["chunk_id"])
            else:
                raise ValueError(f"未知的 parent_order：{parent_order}")
            # 池子成分的诊断量必须在截断**之前**取：截断本身是设计变量，不是要解释的对象。
            raw_children = len({item["chunk_id"] for item in dense} | {
                item["chunk_id"] for item in keyword
            })
            if pool_cap is not None:
                fused = fused[:pool_cap]
            pool_size = len(fused)
            pool_distinct_parents = len({
                item.get("parent_id") or item["chunk_id"] for item in fused
            })
            if ranker is not None and fused:
                ranked = rank_candidates(
                    fused, ranker.score(row["query"], [candidate_document(item) for item in fused])
                )
            else:
                ranked = fused

            # 返回给调用方的那一份（父块窗口）与"实际匹配的那一份"（child 窗口）分开取。
            if parent_order == "before-fusion":
                top = ranked[:5]
                by_budget = select_context(ranked, top_k=5, char_budget=char_budget)
                cache_key = str(index.bm25_path)
                if cache_key not in positions_cache:
                    positions_cache[cache_key] = child_positions(index.bm25_path)
                known = positions_cache[cache_key]
                child_top = [
                    known.get(item.get("child_chunk_id"), {
                        "source_path": item["source_path"], "page_number": item.get("page_number"),
                        "start_line": item["start_line"], "end_line": item["end_line"],
                    })
                    for item in top
                ]
                child_chars = None
            else:
                returned = dedupe_by_parent(
                    resolve_parents(ranked, bm25_path=index.bm25_path)
                )[:5]
                top = returned
                by_budget = select_context(returned, top_k=5, char_budget=char_budget)
                # 同一批命中退回到实际匹配的窗口：即重排看到的那几个 child。
                child_top = ranked[:5]
                child_chars = sum(len(item["text"]) for item in child_top)
            evidence = [
                {
                    "rank": position,
                    "chunk_id": item["chunk_id"],
                    "source_path": item["source_path"],
                    "start_line": item["start_line"],
                    "end_line": item["end_line"],
                    # 页码必须带上：按页来源的行号是页内行号，漏了它会让跨页的
                    # 相同行区间被判成"重复证据"。
                    "page_number": item.get("page_number"),
                    "text": item["text"],
                }
                for position, item in enumerate(top, start=1)
            ]
            records.append({
                "id": row["id"],
                "query": row["query"],
                "library": library,
                "top_chunk_ids": [item["chunk_id"] for item in top],
                "top_positions": [
                    [item["source_path"], item.get("page_number"),
                     item["start_line"], item["end_line"]] for item in top
                ],
                "parent_chars": sum(len(item["text"]) for item in top),
                "child_chars": child_chars,
                # 池子成分：重排实际打了多少条、覆盖几个父块、原始候选里有几个 child。
                # `raw_children - pool_size` 就是"被去重丢掉的同父块兄弟"的条数。
                # 重排看到的那 5 条到底是谁：用来和基线的 top_ids 逐题对，确认 child 粒度
                # 这一列量的是不是同一条流水线，而不是靠均值相等去猜。
                "child_top_ids": [item.get("chunk_id") for item in child_top],
                "raw_children": raw_children,
                "pool_size": pool_size,
                "pool_distinct_parents": pool_distinct_parents,
                "returned_parents": len(top),
                # 重排看到的那 5 个 child 落在几个父块里：child 池的 top-5 会挤在同父块上，
                # 这一列量它挤到什么程度。before-fusion 的 child_top 只有位置、没有 id
                # （它是从父块反查出来的触发 child），这时记 None 而不是硬凑一个数。
                "top_child_distinct_parents": _distinct_ids(child_top),
                "returned_by_budget": len(by_budget),
                "budget_chunk_ids": [item["chunk_id"] for item in by_budget],
                "chars_by_budget": sum(len(item["text"]) for item in by_budget),
                "at_5": evidence_metrics(top, row["evidence"], k=5, blank_lines=blank_lines),
                # 按**服务实际交付**的那几条算一遍：父块约 2 倍于 child，8000 字符预算下
                # top-5 父块往往只放得下 3 条多，而基线 5 条 child 全放得下。at_5 给父子臂
                # 记满 5 条、给基线也记满 5 条，但两者的"交付量"并不相等，这一列量它。
                "at_5_by_budget": evidence_metrics(
                    by_budget, row["evidence"], k=5, blank_lines=blank_lines
                ),
                # 同一批命中退回到 child 粒度：父块区间更大带来的抬高不会出现在这里。
                "at_5_child": evidence_metrics(
                    child_top, row["evidence"], k=5, blank_lines=blank_lines
                ),
                "violations": evidence_violations(evidence),
            })
    finally:
        if routing is not None:
            routing.close()
        else:
            client.close()
            if ranker is not None:
                ranker.close()

    totals: dict[str, int] = {}
    for record in records:
        for kind, items in record["violations"].items():
            if items:
                totals[kind] = totals.get(kind, 0) + len(items)
    summary = summarize(
        [{"metrics": {"arm": {"at_5": record["at_5"]}}} for record in records], "arm", 5
    )
    summary_child = summarize(
        [{"metrics": {"arm": {"at_5": record["at_5_child"]}}} for record in records], "arm", 5
    )
    summary_by_budget = summarize(
        [{"metrics": {"arm": {"at_5": record["at_5_by_budget"]}}} for record in records], "arm", 5
    )
    returned = [record["returned_by_budget"] for record in records]
    parent_chars = [record["parent_chars"] for record in records]
    return {
        "route": str(route_path).replace("\\", "/"),
        "route_id": json.loads(route_path.read_text(encoding="utf-8-sig"))["route_id"],
        "parent_order": parent_order,
        "root": str(root).replace("\\", "/"),
        "dataset_role": "validation only（调参集）；test 集冻结且未参与本次运行",
        "questions": len(records),
        "reranker": reranker,
        "candidate_k": candidate_k,
        "rrf_k": rrf_k,
        "pool_cap": pool_cap,
        "summary_at_5": summary,
        # 同一批命中、换成 child 的位置再量一次。与 summary_at_5 的差就是
        # "父块区间更大"贡献的部分。非父子索引上两者必然相同（child 就是 chunk）。
        "summary_at_5_child_granularity": summary_child,
        # 服务实际交付的那几条（受 8000 字符预算限制）上的指标。与 summary_at_5 的差
        # 就是"指标记了 5 条、服务只给得出 3 条多"这一层。
        "summary_at_5_by_budget": summary_by_budget,
        "failures_at_5": [
            record["id"] for record in records if not record["at_5"]["all_spans_hit"]
        ],
        "returned_by_budget": {
            "mean": round(statistics.fmean(returned), 2),
            "stdev": round(statistics.pstdev(returned), 2),
            "min": min(returned),
            "max": max(returned),
            "questions_below_5": sum(1 for value in returned if value < 5),
        },
        "parent_chars": {
            "mean": round(statistics.fmean(parent_chars), 0),
            "stdev": round(statistics.pstdev(parent_chars), 0),
        },
        "pool": {
            "size_mean": round(statistics.fmean([r["pool_size"] for r in records]), 2),
            "distinct_parents_mean": round(
                statistics.fmean([r["pool_distinct_parents"] for r in records]), 2
            ),
            "raw_children_mean": round(
                statistics.fmean([r["raw_children"] for r in records]), 2
            ),
            "dropped_siblings_mean": round(
                statistics.fmean(
                    [r["raw_children"] - r["pool_size"] for r in records]
                ), 2
            ),
            "top_child_distinct_parents_mean": _mean_or_none(
                [r["top_child_distinct_parents"] for r in records]
            ),
            "returned_parents_mean": round(
                statistics.fmean([r["returned_parents"] for r in records]), 2
            ),
        },
        "violations": totals,
        "violation_total": sum(totals.values()),
        "details": records,
        "status": "PASS" if not totals else "FAIL",
    }


def run_budget(route_path: Path, *, root: Path = ROOT) -> dict:
    """父块比 child 大得多，`context_char_budget` 会先卡住条数——量一下卡掉多少。

    这里**不建索引**：直接在现有候选语料的解析单元上跑 `block_chunks_parented`，
    得到与父子化索引同形的 (child, parent) 分组，再用服务真实的 `select_context`
    算"8000 字符预算下 top-5 能放进几条"。

    抽样口径：一次查询的 top-5 是 5 个父块，它们来自哪个单元不影响预算，**大小才影响**。
    所以这里从整个语料里随机抽 5 个父块（不放回）跑真实的选择逻辑，重复多次取均值——
    按单元算会把问题放大（同一单元内的父块本来就不会同时进 top-5）。

    它是**预算可行性估计**，不是端到端指标：父块能不能被命中不在这里测。
    父块由真实切片函数切出，大小分布与真实索引一致。
    """
    import random

    from wenjin.ingestion.chunking import block_chunks_parented
    from wenjin.ingestion.multiformat import load_retrieval_documents
    from wenjin.retrieval.service import select_context

    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    per_library = {}
    for item in route["candidates"]:
        manifest_path = root / item["manifest_path"]
        if not manifest_path.is_file():
            continue
        documents = load_retrieval_documents(manifest_path, root / "raw_data")
        parent_sizes, child_sizes = [], []
        for document in documents:
            children, parents = block_chunks_parented(document, max_chars=1600)
            parent_sizes.extend(len(parent.text) for parent in parents)
            child_sizes.extend(len(child.text) for child in children)
        if len(parent_sizes) < 5:
            continue
        generator = random.Random(20260924)  # 固定种子：换台机器也得到同一组数字
        draws = 2000
        parent_fits, child_fits = [], []
        for _ in range(draws):
            sample = generator.sample(range(len(parent_sizes)), 5)
            parent_fits.append(len(select_context(
                [{"text": "x" * parent_sizes[index]} for index in sample],
                top_k=5, char_budget=8000,
            )))
            sample = generator.sample(range(len(child_sizes)), 5)
            child_fits.append(len(select_context(
                [{"text": "x" * child_sizes[index]} for index in sample],
                top_k=5, char_budget=8000,
            )))
        per_library[item["library"]] = {
            "parents": len(parent_sizes),
            "children": len(child_sizes),
            "parent_chars_mean": round(statistics.fmean(parent_sizes), 1),
            "parent_chars_max": max(parent_sizes),
            "child_chars_mean": round(statistics.fmean(child_sizes), 1),
            "child_chars_max": max(child_sizes),
            "parent_fit_mean": round(statistics.fmean(parent_fits), 2),
            "parent_fit_stdev": round(statistics.pstdev(parent_fits), 2),
            "child_fit_mean": round(statistics.fmean(child_fits), 2),
            "child_fit_stdev": round(statistics.pstdev(child_fits), 2),
            "parent_draws_returning_fewer_than_5": sum(1 for v in parent_fits if v < 5),
            "draws": draws,
        }
    return {
        "mode": "budget",
        "route": str(route_path).replace("\\", "/"),
        "note": (
            "预算可行性估计：用真实切片函数与真实 `select_context`，不建索引、不跑检索。"
            "8000 是 SearchConfig.context_char_budget 的默认值；每库随机抽 2000 次、"
            "每次抽 5 个块（固定种子）。child 列是同一口径的对照。"
        ),
        "per_library": per_library,
        "status": "PASS" if per_library else "ERROR",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="父子契约判定的真实索引校准")
    parser.add_argument("--indexes", action="store_true", help="逐块校准（不需要模型）")
    parser.add_argument("--evidence", action="store_true", help="返回层校准（需要 Qdrant）")
    parser.add_argument("--budget", action="store_true", help="上下文预算可行性估计（不需要模型）")
    parser.add_argument("--arm", action="store_true",
                        help="跑一条臂的 validation 50 题（指标 + 逐题明细 + 条数实测）")
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="项目根。指向评测临时根即可评那里的候选索引。")
    parser.add_argument("--no-reranker-arm", action="store_true", help="--arm 时跳过重排")
    parser.add_argument("--parent-order", default="before-fusion",
                        choices=["before-fusion", "after-rerank", "child-pool"],
                        help="什么时候把 child 换成父块")
    parser.add_argument("--route", type=Path,
                        default=ROOT / "configs/indexes/multicorpus-candidates-002.json")
    parser.add_argument("--dataset", type=Path,
                        default=ROOT / "evaluation/datasets/rag-validation-001.jsonl")
    parser.add_argument("--no-reranker", action="store_true")
    parser.add_argument("--candidate-k", type=int, default=20,
                        help="每路检索取多少候选（去重前）。默认 20，与基线一致。")
    parser.add_argument("--pool-cap", type=int, default=None,
                        help="把进重排的池子截到固定条数，用来在两条臂之间固定重排算力。")
    parser.add_argument("--label", default=None, help="写进报告的臂名，便于矩阵里对号")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if not (arguments.indexes or arguments.evidence or arguments.budget or arguments.arm):
        parser.error("至少选一个模式：--indexes / --evidence / --budget / --arm")
    root = arguments.root.resolve()

    reports = []
    if arguments.indexes:
        reports.append(run_indexes(arguments.route.resolve(), root=root))
    if arguments.budget:
        reports.append(run_budget(arguments.route.resolve(), root=root))
    if arguments.evidence or arguments.arm:
        arm = run_arm(
            arguments.route.resolve(), arguments.dataset.resolve(), root=root,
            reranker=not (arguments.no_reranker or arguments.no_reranker_arm),
            parent_order=arguments.parent_order,
            candidate_k=arguments.candidate_k,
            pool_cap=arguments.pool_cap,
        )
        if arguments.label:
            arm["label"] = arguments.label
        reports.append(arm)
    report = {
        "note": (
            "父子契约判定的校准证据。只读真实索引；不修改任何索引、配置或语料。"
            "validation 是调参集，其数字不能用来宣称对外性能提升。"
        ),
        "reports": reports,
        "status": "PASS" if all(item["status"] == "PASS" for item in reports) else "FAIL",
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        [{key: value for key, value in item.items() if key != "details"} for item in reports],
        ensure_ascii=False, indent=2,
    ))
    print(f"报告：{arguments.output}")


if __name__ == "__main__":
    main()
