"""对独立审核的3D Tiles补充验证题做一次旧/完整行候选paired检索。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from threading import Lock
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.experiments.compare_line_preserving_indexes import (  # noqa: E402
    _arm_result,
    _load_source_line_map,
    make_question_record,
    validate_paired_routes,
)

ARTIFACTS = ROOT / "artifacts/line-preserving-validation-2026-09-29"
SUPPLEMENT = ARTIFACTS / "3dtiles-supplement"
DATASET = SUPPLEMENT / "questions.jsonl"
DATASET_SHA = SUPPLEMENT / "questions.sha256"
SOURCE_REVIEW = SUPPLEMENT / "question-source-audit.json"
INDEPENDENT_REVIEW = SUPPLEMENT / "independent-question-review.json"
OLD_ROUTE = ROOT / "configs/indexes/multicorpus-candidates-002.json"
NEW_TARGET = ROOT / "configs/indexes/ogc-3dtiles-1.1-blocks-split-candidate-003.json"
RESULTS = SUPPLEMENT / "paired-results.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_reviewed_questions() -> tuple[list[dict], dict]:
    """要求manual来源审计和独立测试先以同一Hash签署PASS。"""
    if DATASET.resolve().parent != SUPPLEMENT.resolve():
        raise ValueError("补充QA文件路径不在固定supplement allowlist内")
    required_files = (DATASET, DATASET_SHA, SOURCE_REVIEW, INDEPENDENT_REVIEW)
    if any(not path.is_file() for path in required_files):
        raise FileNotFoundError("补充QA数据及双重review尚未齐备")
    reviews = {
        "source": json.loads(SOURCE_REVIEW.read_text(encoding="utf-8-sig")),
        "independent": json.loads(INDEPENDENT_REVIEW.read_text(encoding="utf-8-sig")),
    }
    for role, review in reviews.items():
        if review.get("status") != "PASS" or not review.get("dataset_sha256"):
            raise ValueError(f"{role} review未签署PASS")
    approved_hashes = {review["dataset_sha256"] for review in reviews.values()}
    if len(approved_hashes) != 1:
        raise ValueError("manual source audit与独立测试审核的补充数据Hash不一致")

    raw = DATASET.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    sidecar = DATASET_SHA.read_text(encoding="utf-8").split()[0].lower()
    if actual != sidecar or actual not in approved_hashes:
        raise ValueError("补充QA数据SHA256未与sidecar和双重review同时匹配")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows or any(
        row.get("split") != "supplemental_validation"
        or row.get("library") != "ogc-3dtiles"
        or str(row.get("version")) != "1.1"
        or not row.get("query")
        or not row.get("evidence")
        for row in rows
    ):
        raise ValueError("补充题必须明确属于3D Tiles 1.1 supplemental_validation并含证据")
    if len({row.get("id") for row in rows}) != len(rows):
        raise ValueError("补充题ID重复")
    for row in rows:
        for span in row["evidence"]:
            evidence_path = str(span.get("path") or "").replace("\\", "/")
            if (
                span.get("corpus_id") not in (None, "ogc-3dtiles-1.1")
                or not evidence_path.startswith("raw_data/ogc-3dtiles/1.1/")
                or ".." in Path(evidence_path).parts
            ):
                raise ValueError(f"补充证据跨出3D Tiles语料：{row['id']}")
    return rows, {"sha256": actual, "count": len(rows), "reviews": reviews}


def _checkpoint_report(path: Path, report: dict[str, Any], *, replace=os.replace) -> None:
    """原子保存检查点；Windows 上目标文件被短暂占用时有限重试。"""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (2 ** attempt))


def _resume_checkpoint(
    path: Path, expected: dict[str, Any], rows: list[dict],
) -> tuple[dict[str, Any], set[str], dict[str, Any] | None]:
    """只恢复同一题集/路由/参数的失败或未完成实验，并保留已完成题目。"""
    if not path.is_file():
        return expected, set(), None
    previous = json.loads(path.read_text(encoding="utf-8"))
    for key in ("experiment_scope", "dataset", "controls", "target_pair"):
        if previous.get(key) != expected.get(key):
            raise ValueError(f"已有检查点的 {key} 与本次实验不一致，拒绝续跑")
    if previous.get("status") == "completed":
        raise ValueError("同一题集已有完整结果，拒绝重复运行模型实验")
    details = previous.get("details")
    if not isinstance(details, list):
        raise ValueError("已有检查点缺少有效 details，拒绝续跑")
    valid_rows = {row["id"]: row for row in rows}
    valid_ids = set(valid_rows)
    completed: set[str] = set()
    for detail in details:
        input_record = detail.get("input")
        question_id = input_record.get("id") if isinstance(input_record, dict) else None
        arms = detail.get("arms", {})
        source_row = valid_rows.get(question_id)
        if (question_id not in valid_ids or question_id in completed
                or source_row is None
                or input_record.get("query") != source_row.get("query")
                or input_record.get("evidence") != source_row.get("evidence")
                or not isinstance(arms, dict)
                or any(not isinstance(arms.get(name), dict) or not arms[name]
                       for name in ("baseline", "candidate_003"))):
            raise ValueError("已有检查点含未知/重复题目或不完整的成对检索结果")
        completed.add(question_id)
    recovery = {
        "previous_status": previous.get("status"),
        "previous_failure": previous.get("failure"),
        "resumed_completed_question_ids": [row["id"] for row in rows if row["id"] in completed],
        "remaining_question_ids": [row["id"] for row in rows if row["id"] not in completed],
    }
    previous["status"] = "running"
    previous.pop("failure", None)
    previous["checkpoint_recovery"] = recovery
    return previous, completed, recovery


def main() -> None:
    rows, review_state = _load_reviewed_questions()
    route_pair = validate_paired_routes(OLD_ROUTE, NEW_TARGET)
    baseline_route_path = ARTIFACTS / "supplemental-baseline-route-002.json"
    candidate_route_path = ARTIFACTS / "supplemental-candidate-route-003.json"
    baseline_route_path.write_text(
        json.dumps(route_pair["baseline_route"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    candidate_route_path.write_text(
        json.dumps(route_pair["candidate_route"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_line_map, blank_lines, source_corpora = _load_source_line_map(
        route_pair["baseline_route"], rows,
    )

    report: dict[str, Any] = {
        "status": "running",
        "experiment_scope": "supplemental_validation_only",
        "dataset": {"path": DATASET.relative_to(ROOT).as_posix(), **review_state},
        "controls": {"library": "ogc-3dtiles", "version": "1.1", "candidate_k": 20,
                     "top_k": 5, "rrf_k": 60, "rerank_pool": 30,
                     "context_char_budget": 8000, "reranker_max_length": 1024,
                     "reranker_batch_size": 2, "sequential_shared_models": True},
        "target_pair": route_pair["replaced_target"],
        "source_map": {"source_files": len({path for path, _ in source_line_map}),
                       "line_files": len(blank_lines)},
        "details": [],
    }

    def checkpoint() -> None:
        _checkpoint_report(RESULTS, report)

    report, completed_ids, recovery = _resume_checkpoint(RESULTS, report, rows)
    if recovery is not None:
        print(f"从检查点续跑：已保留 {len(completed_ids)}/{len(rows)} 题，继续剩余题目。",
              flush=True)

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    client = None
    reranker = None
    current_id = None
    try:
        import torch

        from wenjin.qdrant_connection import create_qdrant_client
        from wenjin.retrieval.rerank import Qwen3Reranker
        from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig
        from wenjin.settings import Settings

        settings = Settings()
        client = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        embedder = settings.embedding()
        reranker = Qwen3Reranker(
            settings.reranker_model_path,
            device="cuda" if torch.cuda.is_available() else "cpu",
            max_length=1024,
            batch_size=2,
        )
        report["models"] = {
            "embedding": settings.embedding_model_path.name,
            "embedding_runtime_max_length": getattr(embedder, "max_length", None),
            "embedding_sentence_transformer_max_seq_length": getattr(
                getattr(embedder, "_model", None), "max_seq_length", None
            ),
            "reranker": settings.reranker_model_path.name,
        }
        shared_lock = Lock()
        routers = []
        for arm_name, route_path in (
            ("baseline", baseline_route_path), ("candidate_003", candidate_route_path),
        ):
            router = MultiCorpusRouter.from_route_file(
                route_path, root=ROOT, qdrant_client=client, embedder=embedder,
                reranker=reranker, search_lock=shared_lock,
            )
            original_retrieve = router.retrieve

            def capture(*args, _original=original_retrieve, _router=router, **kwargs):
                result = _original(*args, **kwargs)
                _router.last_retrieval = result
                return result

            router.retrieve = capture
            routers.append((arm_name, router))
        config = RouteConfig(
            candidate_k=20, top_k=5, rrf_k=60, max_libraries=5,
            rerank_pool=30, context_char_budget=8000,
        )
        for row in rows:
            current_id = row["id"]
            if current_id in completed_ids:
                continue
            route_args = {"library": "ogc-3dtiles", "version": "1.1"}
            arms = {}
            for arm_name, router in routers:
                index = router.resolve("ogc-3dtiles", "1.1")[0]
                search_result = router.search(
                    row["query"], library="ogc-3dtiles", version="1.1", config=config,
                )
                arms[arm_name] = _arm_result(
                    search_result, router.last_retrieval, row["evidence"], row["evidence"],
                    source_line_map, blank_lines, budget=8000,
                )
                arms[arm_name].update({
                    "target_id": index.target_id,
                    "routed_corpus_id": index.corpus_identity,
                    "cross_library_scope": False,
                })
            record = make_question_record(row, route_args, arms["baseline"], arms["candidate_003"])
            for field in ("role", "case_type", "target_line", "source_hash", "evidence_policy"):
                if field in row:
                    record["input"][field] = row[field]
            report["details"].append(record)
            completed_ids.add(current_id)
            checkpoint()
            print(f"[{len(completed_ids)}/{len(rows)}] {current_id} baseline+candidate完成",
                  flush=True)
        report["status"] = "completed"
        report["question_count"] = len(report["details"])
        checkpoint()
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {"question_id": current_id, "error_type": type(error).__name__,
                             "detail": str(error)}
        checkpoint()
        raise
    finally:
        if reranker is not None:
            reranker.close()
        if client is not None:
            client.close()
    print(f"supplemental paired结果：{RESULTS.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
