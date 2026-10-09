"""仅调整3D Tiles候选深度与rerank池的两题隔离检索对照。"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SUPPLEMENT = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement"
PAIRED = SUPPLEMENT / "paired-results.json"
QUESTIONS = SUPPLEMENT / "questions.jsonl"
QUESTION_SHA256 = "8c69891fddf0002210b9446150d35d4acc71217d58f401033ca0e2fef8595162"
BASE_ROUTE = ROOT / "artifacts/line-preserving-validation-2026-09-29/supplemental-candidate-route-003.json"
OUTPUT = ROOT / "artifacts/phase1-source-replay-validation/phase2-candidate-depth-pair.json"
REPORT = ROOT / "artifacts/phase1-source-replay-validation/phase2-candidate-depth-pair.md"
QUESTION_IDS = ("supplemental-3dtiles-longline-003", "supplemental-3dtiles-longline-005")
TARGETS = {
    QUESTION_IDS[0]: {
        "html": ("raw_data/ogc-3dtiles/1.1/22-025r4.html", 5903, None, "Application-specific data"),
        "pdf_value": ("raw_data/ogc-3dtiles/1.1/22-025r4.pdf", None, 207, "Application-specific data"),
        "pdf_scope": ("raw_data/ogc-3dtiles/1.1/22-025r4.pdf", None, 205, "Table A.10"),
    },
    QUESTION_IDS[1]: {
        "html": ("raw_data/ogc-3dtiles/1.1/22-025r4.html", 7128, None, "BATCH_LENGTH"),
        "pdf": ("raw_data/ogc-3dtiles/1.1/22-025r4.pdf", None, 240, "BATCH_LENGTH"),
    },
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _matches(item: dict, selector: tuple[str, int | None, int | None, str]) -> bool:
    source, line, page, phrase = selector
    return (
        item.get("source_path") == source
        and (item.get("start_line") == line if line is not None else item.get("page_number") == page)
        and (line is None or item.get("end_line") == line)
        and phrase in item.get("text", "")
    )


def preflight() -> tuple[dict, dict[str, dict], dict]:
    raw = QUESTIONS.read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != QUESTION_SHA256:
        raise ValueError("固定补充题集SHA不匹配")
    questions = {
        row["id"]: row
        for row in (json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip())
    }
    paired = json.loads(PAIRED.read_text(encoding="utf-8"))
    if paired.get("status") != "completed" or paired.get("dataset", {}).get("sha256") != QUESTION_SHA256:
        raise ValueError("paired输入未完成或题集SHA不匹配")
    details = {row["input"]["id"]: row for row in paired.get("details", [])}
    if len(details) != 24 or set(details) != set(questions):
        raise ValueError("paired结果必须与固定题集的24个唯一ID完全对应")
    for question_id, detail in details.items():
        frozen = questions[question_id]
        if any(detail["input"].get(key) != frozen.get(key) for key in ("id", "split", "library", "version", "query", "evidence")):
            raise ValueError(f"{question_id}输入与固定题集不一致")
        if frozen.get("split") != "supplemental_validation":
            raise ValueError("题集包含非supplemental_validation问题")
    expected_controls = {
        "library": "ogc-3dtiles", "version": "1.1", "candidate_k": 20, "top_k": 5,
        "rrf_k": 60, "rerank_pool": 30, "context_char_budget": 8000,
        "reranker_max_length": 1024, "reranker_batch_size": 2,
        "sequential_shared_models": True,
    }
    if paired.get("controls") != expected_controls:
        raise ValueError("原paired controls与已审核基线不匹配")
    target_id = "ogc-3dtiles-1.1-blocks-split-candidate-003"
    if paired.get("target_pair", {}).get("new", {}).get("target_id") != target_id:
        raise ValueError("paired target身份不匹配")
    route = json.loads(BASE_ROUTE.read_text(encoding="utf-8"))
    matches = [item for item in route.get("candidates", []) if item.get("target_id") == target_id]
    if len(matches) != 1 or matches[0].get("library") != "ogc-3dtiles" or str(matches[0].get("version")) != "1.1":
        raise ValueError("隔离候选route缺少唯一3D Tiles 1.1 target")
    rows = {question_id: details[question_id] for question_id in QUESTION_IDS}
    return paired, rows, {
        "status": "preflight_passed",
        "dataset_sha256": actual_sha,
        "question_count": len(details),
        "unique_ids": len(details),
        "qids": list(QUESTION_IDS),
        "target_id": target_id,
        "baseline_controls": expected_controls,
        "candidate_controls": {**expected_controls, "candidate_k": 50, "rerank_pool": 60},
        "route_path": BASE_ROUTE.relative_to(ROOT).as_posix(),
        "model_runs": 2,
        "default_instruction_only": True,
    }


def _stage_target_ranks(candidates: list[dict], target_map: dict) -> dict:
    out = {}
    for name, selector in target_map.items():
        matches = [
            (rank, item)
            for rank, item in enumerate(candidates, start=1)
            if _matches(item, selector)
        ]
        out[name] = [
            {
                "rank": rank,
                "chunk_id": item["chunk_id"],
                "source_path": item["source_path"],
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "page_number": item.get("page_number"),
                "text_sha256": _sha(item["text"]),
                "declared_content_sha256": item.get("content_sha256"),
                "characters": len(item["text"]),
            }
            for rank, item in matches
        ]
    return out


def _selected(selected: list[dict], target_map: dict, question_id: str) -> dict:
    target_in_selected = {
        name: any(_matches(item, selector) for item in selected)
        for name, selector in target_map.items()
    }
    selected_items = []
    for rank, item in enumerate(selected, start=1):
        selected_items.append({
            "rank": rank,
            "chunk_id": item["chunk_id"],
            "source_path": item["source_path"],
            "start_line": item.get("start_line"),
            "end_line": item.get("end_line"),
            "page_number": item.get("page_number"),
            "characters": len(item["text"]),
            "text_sha256": _sha(item["text"]),
            "text": item["text"],
            "parser": item.get("parser"),
            "ocr_derived": item.get("ocr_derived"),
        })
    summary = {
        "context_chars_used": sum(len(item["text"]) for item in selected),
        "context_char_budget": 8000,
        "selected_candidates": selected_items,
        "target_in_selected": target_in_selected,
    }
    if question_id == QUESTION_IDS[0]:
        summary["scope_complete_from_separate_pdf_evidence"] = (
            target_in_selected["pdf_scope"] and target_in_selected["pdf_value"]
        )
    else:
        html = next((item for item in selected if _matches(item, target_map["html"])), None)
        summary["complete_html_target_selected"] = bool(
            html and all(token in html["text"] for token in ("BATCH_LENGTH", "GlobalPropertyInteger", "all points"))
        )
    return summary


def assemble_condition(question_id: str, config, retrieval: dict) -> dict[str, Any]:
    """纯结果组装：共享真实/合成retrieval输入，便于CPU验证记录契约。"""
    from wenjin.retrieval.service import select_context

    ranked = retrieval["ranked_candidates"]
    selected = select_context(ranked, top_k=config.top_k, char_budget=config.context_char_budget)
    stages = {
        "dense": retrieval["dense_candidates"],
        "bm25": retrieval["bm25_candidates"],
        "fused": retrieval["fused_candidates"],
        "ranked": ranked,
    }
    target_id = "ogc-3dtiles-1.1-blocks-split-candidate-003"
    allowed_paths = {
        "raw_data/ogc-3dtiles/1.1/22-025r4.html",
        "raw_data/ogc-3dtiles/1.1/22-025r4.pdf",
    }
    for values in stages.values():
        for item in values:
            if item.get("target_id") != target_id:
                raise ValueError(f"{question_id} candidate target_id mismatch")
            if item.get("library") != "ogc-3dtiles" or str(item.get("version")) != "1.1":
                raise ValueError(f"{question_id} candidate corpus identity mismatch")
            if item.get("source_path") not in allowed_paths:
                raise ValueError(f"{question_id} source path outside allowlist")
            if item.get("content_sha256") and item["content_sha256"] != _sha(item["text"]):
                raise ValueError(f"{question_id} candidate body SHA mismatch")
    stage_summary = {
        name: {
            "count": len(values),
            "target_ranks": _stage_target_ranks(values, TARGETS[question_id]),
            "candidates": [
                {
                    "rank": rank,
                    "chunk_id": item["chunk_id"],
                    "source_path": item["source_path"],
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "page_number": item.get("page_number"),
                    "text_sha256": _sha(item["text"]),
                    "content_sha256": item.get("content_sha256"),
                    "score": item.get("score", item.get("rerank_score")),
                    "rrf_score": item.get("rrf_score"),
                    "rerank_score": item.get("rerank_score"),
                    "text": item["text"] if any(_matches(item, target) for target in TARGETS[question_id].values()) else None,
                }
                for rank, item in enumerate(values, start=1)
            ],
        }
        for name, values in stages.items()
    }
    return {
        "controls": {
            "candidate_k": config.candidate_k,
            "rrf_k": config.rrf_k,
            "rerank_pool": config.rerank_pool,
            "top_k": config.top_k,
            "context_char_budget": config.context_char_budget,
            "reranker_max_length": 1024,
        },
        "timing_seconds": retrieval["timing_seconds"],
        "stages": stage_summary,
        "selected_context": _selected(selected, TARGETS[question_id], question_id),
    }


def saved_pair_summary(row: dict, question_id: str) -> dict[str, Any]:
    """从旧paired序列化结构提取基线，selected_context本身是对象。"""
    arm = row["arms"]["candidate_003"]
    return {
        "target_ranks": {
            stage: _stage_target_ranks(arm["stages"][stage]["candidates"], TARGETS[question_id])
            for stage in ("dense", "bm25", "fused", "ranked")
        },
        "selected_chunk_ids": [item["chunk_id"] for item in arm["selected_context"]["evidence"]],
    }


def _save_checkpoint(result: dict[str, Any]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temporary, OUTPUT)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * (2**attempt))


def _run() -> None:
    paired, rows, checks = preflight()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
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
    route_path = BASE_ROUTE
    router = None
    try:
        router = MultiCorpusRouter.from_route_file(
            route_path,
            root=ROOT,
            qdrant_client=client,
            embedder=embedder,
            reranker=reranker,
        )
        result: dict[str, Any] = {
            "status": "running",
            "experiment": "candidate_k_50_rerank_pool_60_vs_saved_candidate_k_20_pool_30",
            "preregistration": "artifacts/phase1-source-replay-validation/phase2-candidate-depth-preregistration.md",
            "input_sha256": QUESTION_SHA256,
            "controls": checks,
            "model": {"embedding": settings.embedding_model_path.name, "reranker": settings.reranker_model_path.name,
                      "device": reranker.device},
            "instructions": {"name": "DEFAULT_INSTRUCTION", "changed": False},
            "questions": {},
        }
        if OUTPUT.is_file():
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            if previous.get("status") == "completed":
                raise ValueError("此候选深度实验已有completed结果，拒绝重复运行")
            if (previous.get("experiment") != result["experiment"]
                    or previous.get("input_sha256") != QUESTION_SHA256
                    or previous.get("controls") != checks
                    or previous.get("instructions") != result["instructions"]):
                raise ValueError("已有检查点题集、控制参数或指令不匹配，拒绝续跑")
            result = previous
            result["status"] = "running"
            result.pop("failure", None)
        configs = {
            "baseline_20_30": RouteConfig(candidate_k=20, top_k=5, rrf_k=60, max_libraries=5,
                                            rerank_pool=30, context_char_budget=8000),
            "candidate_50_60": RouteConfig(candidate_k=50, top_k=5, rrf_k=60, max_libraries=5,
                                             rerank_pool=60, context_char_budget=8000),
        }
        for question_id, row in rows.items():
            qresult = result["questions"].setdefault(
                question_id, {"query": row["input"]["query"], "conditions": {}}
            )
            for condition, config in configs.items():
                if condition in qresult.get("conditions", {}):
                    continue
                retrieval = router.retrieve(
                    row["input"]["query"], library="ogc-3dtiles", version="1.1", config=config
                )
                qresult["conditions"][condition] = assemble_condition(question_id, config, retrieval)
                _save_checkpoint(result)
            qresult["comparison_to_saved_pair"] = saved_pair_summary(row, question_id)
            _save_checkpoint(result)
        result["status"] = "completed"
        result["finished_unix"] = time.time()
        _save_checkpoint(result)
    except Exception as error:
        result["status"] = "failed"
        result["failure"] = {"type": type(error).__name__, "detail": str(error)}
        result["finished_unix"] = time.time()
        _save_checkpoint(result)
        raise
    finally:
        reranker.close()
        client.close()
    print(f"JSON: {OUTPUT.relative_to(ROOT).as_posix()}")
    print(f"Report: {REPORT.relative_to(ROOT).as_posix()}")
    _write_report(result)


def _write_report(result: dict[str, Any]) -> None:
    lines = [
        "# 3D Tiles Q003/Q005 candidate depth comparison",
        "",
        "One sequential local retrieval process; DEFAULT_INSTRUCTION, max_length=1024, RRF k=60, top5, and 8,000-character budget fixed. Only candidate_k/rerank_pool differ (20/30 vs 50/60).",
        "",
        "| Question | Condition | Dense target ranks | BM25 target ranks | Fused target ranks | Reranked target ranks | Selected targets | Context chars |",
        "|---|---|---|---|---|---|---|---:|",
    ]
    for question_id, question in result["questions"].items():
        for condition, outcome in question["conditions"].items():
            ranks = outcome["stages"]
            def rank_text(stage: str) -> str:
                targets = ranks[stage]["target_ranks"]
                return "; ".join(f"{name}:{','.join(str(item['rank']) for item in items) or '—'}" for name, items in targets.items())
            selected = outcome["selected_context"]
            selected_targets = ",".join(name for name, yes in selected["target_in_selected"].items() if yes) or "—"
            lines.append(
                f"| {question_id} | {condition} | {rank_text('dense')} | {rank_text('bm25')} | "
                f"{rank_text('fused')} | {rank_text('ranked')} | {selected_targets} | "
                f"{selected['context_chars_used']}/{selected['context_char_budget']} |"
            )
    lines.extend(["", "Full stage candidates, target body hashes, and selected source text are preserved in the paired JSON.", ""])
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    paired, _, checks = preflight()
    if "--preflight-only" in sys.argv:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return
    _run()


if __name__ == "__main__":
    main()
