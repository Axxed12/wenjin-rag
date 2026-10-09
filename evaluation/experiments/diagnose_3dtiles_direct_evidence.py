"""在既有003融合候选池上隔离比较两种reranker指令；不重新检索或改索引。"""

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

RESULTS = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/paired-results.json"
OUTPUT = ROOT / "artifacts/phase1-source-replay-validation/phase2-direct-evidence-rerank.json"
REPORT = ROOT / "artifacts/phase1-source-replay-validation/phase2-direct-evidence-rerank.md"
FROZEN_QUESTION_PATH = "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl"
FROZEN_QUESTION_SHA256 = "8c69891fddf0002210b9446150d35d4acc71217d58f401033ca0e2fef8595162"

DIRECT_EVIDENCE_INSTRUCTION = (
    "Given a technical documentation query, rank passages by how directly they support the "
    "requested claim for the stated entity and context. Prefer explicit definitions over "
    "related or similarly named material, and do not infer missing details from adjacent passages."
)
QUESTION_IDS = (
    "supplemental-3dtiles-longline-003",
    "supplemental-3dtiles-longline-005",
)
TARGETS = {
    QUESTION_IDS[0]: {
        "html": ("22-025r4.html", 5903, None, "extras"),
        "pdf_value": ("22-025r4.pdf", None, 207, "Application-specific data"),
        "pdf_scope": ("22-025r4.pdf", None, 205, "Table A.10"),
    },
    QUESTION_IDS[1]: {
        "html": ("22-025r4.html", 7128, None, "BATCH_LENGTH"),
        "pdf": ("22-025r4.pdf", None, 240, "BATCH_LENGTH"),
    },
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_matches(
    candidate: dict, target: tuple[str, int | None, int | None, str]
) -> bool:
    filename, line, page, marker = target
    if not candidate["source_path"].endswith(filename):
        return False
    position_matches = (
        candidate.get("start_line") == line
        if line is not None
        else candidate.get("page_number") == page
    )
    return position_matches and marker in candidate["text"]


def _rank_summary(ranked: list[dict], target_map: dict[str, tuple]) -> dict[str, Any]:
    ranks = {}
    for name, target in target_map.items():
        matches = [
            (rank, item)
            for rank, item in enumerate(ranked, start=1)
            if _candidate_matches(item, target)
        ]
        ranks[name] = [
            {
                "rank": rank,
                "chunk_id": item["chunk_id"],
                "source_path": item["source_path"],
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "page_number": item.get("page_number"),
                "characters": len(item["text"]),
                "text_sha256": _sha256_text(item["text"]),
                "rerank_score": item["rerank_score"],
                "pre_rerank_rank": item["pre_rerank_rank"],
            }
            for rank, item in matches
        ]
    return ranks


def _selected_summary(
    selected: list[dict], target_map: dict[str, tuple], question_id: str
) -> dict[str, Any]:
    target_in_selected = {
        name: any(_candidate_matches(item, target) for item in selected)
        for name, target in target_map.items()
    }
    result = {
        "context_chars_used": sum(len(item["text"]) for item in selected),
        "context_char_budget": 8000,
        "selected_candidates": [
            {
                "rank": item["final_rank"],
                "chunk_id": item["chunk_id"],
                "source_path": item["source_path"],
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "page_number": item.get("page_number"),
                "characters": len(item["text"]),
                "text_sha256": _sha256_text(item["text"]),
                "text": item["text"],
                "parser": item.get("parser"),
                "ocr_derived": item.get("ocr_derived"),
                "content_sha256": item.get("content_sha256"),
                "required_html_phrases": {
                    phrase: phrase in item["text"]
                    for phrase in ("BATCH_LENGTH", "GlobalPropertyInteger", "all points")
                } if question_id == QUESTION_IDS[1] else None,
            }
            for item in selected
        ],
        "target_in_selected": target_in_selected,
    }
    if question_id == QUESTION_IDS[0]:
        result["scope_complete_from_separate_pdf_evidence"] = (
            target_in_selected.get("pdf_scope", False)
            and target_in_selected.get("pdf_value", False)
        )
    elif question_id == QUESTION_IDS[1]:
        html_candidate = next(
            (item for item in selected if _candidate_matches(item, target_map["html"])),
            None,
        )
        result["complete_html_target_selected"] = bool(
            html_candidate
            and all(
                phrase in html_candidate["text"]
                for phrase in ("BATCH_LENGTH", "GlobalPropertyInteger", "all points")
            )
        )
    return result


def _write_outputs(result: dict[str, Any]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(OUTPUT)
    lines = [
        "# Q003/Q005 direct-evidence reranker instruction experiment",
        "",
        "Model scoring used one sequential model instance. Candidate pools were loaded from the existing candidate_003 fused results; no retrieval, index, or default-route setting changed.",
        "",
        f"- Device: `{result.get('model', {}).get('device')}`",
        f"- Reranker: `{result.get('model', {}).get('name')}`; max_length=1024; batch_size=2",
        "- Variable: default instruction vs generic direct-evidence instruction only.",
        "- Candidate pools: Q003 27; Q005 30. top_k=5; context budget=8000 chars.",
        "",
    ]
    for question_id, question in result.get("questions", {}).items():
        lines.extend([f"## {question_id}", "", f"Query: {question['query']}", ""])
        for instruction_name, outcome in question["instructions"].items():
            selected = outcome["selected_context"]
            ranks = outcome["target_ranks"]
            lines.extend([
                f"### {instruction_name}",
                "",
                f"Selected context: {selected['context_chars_used']}/{selected['context_char_budget']} chars.",
                "",
                "Target ranks: " + "; ".join(
                    f"{name}={','.join(str(item['rank']) for item in entries) if entries else 'not in pool'}"
                    for name, entries in ranks.items()
                ),
                "",
                "Selected IDs: " + ", ".join(item["chunk_id"] for item in selected["selected_candidates"]),
                "",
            ])
    lines.extend([
        "## Boundary",
        "",
        "This two-question experiment is exploratory. It does not justify changing the default instruction. If a relevant heading is absent from a chunk, evidence was kept as separate chunks; no returned text was concatenated to bypass the line/page citation contract.",
        "",
    ])
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    if not RESULTS.is_file():
        raise FileNotFoundError(RESULTS)
    paired = json.loads(RESULTS.read_text(encoding="utf-8"))
    if paired.get("status") != "completed":
        raise ValueError("输入paired结果未完成")
    details = paired.get("details")
    if not isinstance(details, list) or len(details) != 24:
        raise ValueError("输入paired结果必须包含24条详情")
    ids = [item.get("input", {}).get("id") for item in details]
    if len(set(ids)) != 24 or any(not isinstance(value, str) for value in ids):
        raise ValueError("输入题目ID必须完整且唯一")
    if any(item.get("input", {}).get("split") != "supplemental_validation" for item in details):
        raise ValueError("输入包含非预注册split题目")
    dataset_relpath = paired["dataset"].get("path")
    if dataset_relpath != FROZEN_QUESTION_PATH:
        raise ValueError("paired dataset不在唯一允许的补充题集路径")
    dataset_path = (ROOT / FROZEN_QUESTION_PATH).resolve()
    dataset_sha = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    if dataset_sha != FROZEN_QUESTION_SHA256 or dataset_sha != paired["dataset"].get("sha256"):
        raise ValueError("输入题集SHA与paired记录不一致")
    expected_controls = {
        "library": "ogc-3dtiles",
        "version": "1.1",
        "candidate_k": 20,
        "top_k": 5,
        "rrf_k": 60,
        "rerank_pool": 30,
        "context_char_budget": 8000,
        "reranker_max_length": 1024,
        "reranker_batch_size": 2,
        "sequential_shared_models": True,
    }
    if paired.get("controls") != expected_controls:
        raise ValueError("输入paired controls与预注册值不一致")
    by_id = {item["input"]["id"]: item for item in details}
    frozen_questions = {
        question["id"]: question
        for question in (
            json.loads(line)
            for line in dataset_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    if set(frozen_questions) != set(ids):
        raise ValueError("paired详情ID与固定SHA题集ID集合不一致")
    for question_id, row in by_id.items():
        frozen = frozen_questions[question_id]
        for field in ("id", "split", "library", "version", "query", "answer_rubric", "expected_status", "evidence"):
            if row["input"].get(field) != frozen.get(field):
                raise ValueError(f"{question_id} paired input字段 {field} 与冻结题集不一致")
    rows = {question_id: by_id.get(question_id) for question_id in QUESTION_IDS}
    if any(row is None for row in rows.values()):
        raise ValueError("输入缺少Q003/Q005已审核补充题")
    if any(row["input"].get("role") != "supplemental_validation" for row in rows.values()):
        raise ValueError("Q003/Q005 role与预注册值不符")
    if any(row["input"].get("library") != "ogc-3dtiles" or row["input"].get("version") != "1.1" for row in rows.values()):
        raise ValueError("Q003/Q005语料身份不符")
    expected_target_id = paired.get("target_pair", {}).get("new", {}).get("target_id")
    if expected_target_id != "ogc-3dtiles-1.1-blocks-split-candidate-003":
        raise ValueError("paired新候选target_id与003预注册身份不符")
    allowed_source_paths = {
        "raw_data/ogc-3dtiles/1.1/22-025r4.html",
        "raw_data/ogc-3dtiles/1.1/22-025r4.pdf",
    }
    candidate_pools = {
        question_id: rows[question_id]["arms"]["candidate_003"]["stages"]["fused"]["candidates"]
        for question_id in QUESTION_IDS
    }
    expected_pool_sizes = {QUESTION_IDS[0]: 27, QUESTION_IDS[1]: 30}
    if {key: len(value) for key, value in candidate_pools.items()} != expected_pool_sizes:
        raise ValueError("现有003融合候选池大小与预注册值不符")
    for question_id, candidates in candidate_pools.items():
        chunk_ids = [item.get("chunk_id") for item in candidates]
        if len(set(chunk_ids)) != len(candidates) or any(not isinstance(value, str) for value in chunk_ids):
            raise ValueError(f"{question_id}融合候选chunk_id缺失或重复")
        for item in candidates:
            if item.get("target_id") != expected_target_id:
                raise ValueError(f"{question_id}/{item['chunk_id']}目标target_id不符")
            if item.get("library") != "ogc-3dtiles" or item.get("version") != "1.1":
                raise ValueError(f"{question_id}/{item['chunk_id']}语料库身份不符")
            if item.get("source_path") not in allowed_source_paths:
                raise ValueError(f"{question_id}/{item['chunk_id']}来源路径不在允许范围")
            if not isinstance(item.get("text"), str) or not item["text"]:
                raise ValueError(f"{question_id}存在空候选正文")
            if (
                item.get("content_sha256") is not None
                and item.get("content_sha256") != _sha256_text(item["text"])
            ):
                raise ValueError(f"{question_id}/{item['chunk_id']}候选正文Hash不匹配")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    import torch

    from wenjin.retrieval.rerank import (
        DEFAULT_INSTRUCTION,
        Qwen3Reranker,
        candidate_document,
        rank_candidates,
    )
    from wenjin.retrieval.service import select_context
    from wenjin.settings import Settings
    target_identity = {}
    for question_id, targets in TARGETS.items():
        target_identity[question_id] = {}
        pool = candidate_pools[question_id]
        for name, selector in targets.items():
            matches = [item for item in pool if _candidate_matches(item, selector)]
            if len(matches) > 1:
                raise ValueError(f"{question_id}/{name}目标定位不唯一")
            if matches:
                item = matches[0]
                text_hash = _sha256_text(item["text"])
                if item.get("content_sha256") != text_hash:
                    raise ValueError(f"{question_id}/{name}候选正文Hash不匹配")
                target_identity[question_id][name] = {
                    "present_in_fused_pool": True,
                    "chunk_id": item["chunk_id"],
                    "source_path": item["source_path"],
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "page_number": item.get("page_number"),
                    "text_sha256": text_hash,
                }
            else:
                target_identity[question_id][name] = {"present_in_fused_pool": False}

    # 先完成全部输入/目标检查再决定是否加载torch或模型；便于独立CPU审阅门禁。
    if "--preflight-only" in sys.argv:
        print(json.dumps({
            "status": "preflight_passed",
            "input_integrity": {
                "dataset_path": paired["dataset"]["path"],
                "dataset_sha256": dataset_sha,
                "question_count": len(details),
                "unique_id_count": len(set(ids)),
                "split": "supplemental_validation",
            },
            "controls": expected_controls,
            "candidate_pool_sizes": {key: len(value) for key, value in candidate_pools.items()},
            "target_identity": target_identity,
        }, ensure_ascii=False, indent=2))
        return

    settings = Settings()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    reranker = Qwen3Reranker(
        settings.reranker_model_path,
        device=device,
        max_length=1024,
        batch_size=2,
        instruction=DEFAULT_INSTRUCTION,
    )
    result: dict[str, Any] = {
        "status": "running",
        "started_unix": time.time(),
        "source_results": RESULTS.relative_to(ROOT).as_posix(),
        "input_integrity": {
                "dataset_path": paired["dataset"]["path"],
                "dataset_sha256_expected": paired["dataset"]["sha256"],
                "dataset_sha256_actual": dataset_sha,
            "question_count": len(details),
            "unique_id_count": len(set(ids)),
            "split": "supplemental_validation",
        },
        "controls": {
            "candidate_k": 20,
            "rrf_k": 60,
            "rerank_pool": 30,
            "top_k": 5,
            "context_char_budget": 8000,
            "max_length": 1024,
            "batch_size": 2,
            "fused_pool_sizes": expected_pool_sizes,
            "one_model_instance_sequential": True,
        },
        "instructions": {
            "default": DEFAULT_INSTRUCTION,
            "generic_direct_evidence": DIRECT_EVIDENCE_INSTRUCTION,
        },
        "model": {"name": settings.reranker_model_path.name, "device": device},
        "questions": {},
    }
    try:
        for question_id in QUESTION_IDS:
            row = rows[question_id]
            query = row["input"]["query"]
            candidates = candidate_pools[question_id]
            question_result: dict[str, Any] = {
                "query": query,
                "target_identity": target_identity[question_id],
                "instructions": {},
            }
            target_markers = (
                ("pdf_scope", "Table A.10"),
                ("pdf_value", "Application-specific data"),
                ("html", "Application-specific data"),
            ) if question_id == QUESTION_IDS[0] else (
                ("html", "BATCH_LENGTH"),
                ("html", "GlobalPropertyInteger"),
                ("html", "all points"),
            )
            content_budget = (
                reranker.max_length
                - len(reranker.prefix_tokens)
                - len(reranker.suffix_tokens)
            )
            for instruction_name, instruction in result["instructions"].items():
                reranker.instruction = instruction
                target_tokenization = {}
                for target_name, target in TARGETS[question_id].items():
                    matching = next(
                        (candidate for candidate in candidates
                         if _candidate_matches(candidate, target)),
                        None,
                    )
                    if matching is None:
                        target_tokenization[target_name] = {"present_in_fused_pool": False}
                        continue
                    pair = reranker._format_pair(query, candidate_document(matching))
                    input_tokens = reranker.tokenizer.encode(pair, add_special_tokens=False)
                    used_tokens = (
                        input_tokens[:content_budget]
                        if reranker.tokenizer.truncation_side == "right"
                        else input_tokens[-content_budget:]
                    )
                    decoded_after_truncation = reranker.tokenizer.decode(
                        used_tokens, skip_special_tokens=False
                    )
                    markers = [marker for name, marker in target_markers if name == target_name]
                    target_tokenization[target_name] = {
                        **target_identity[question_id][target_name],
                        "full_pair_tokens": len(input_tokens),
                        "content_budget_tokens": content_budget,
                        "truncated": len(input_tokens) > content_budget,
                        "required_phrases_preserved": {
                            marker: marker in decoded_after_truncation for marker in markers
                        },
                    }

                # 同一指令/问题只给既有候选池打分一次；不在目标字段循环内重复调用模型。
                scores = reranker.score(query, [candidate_document(c) for c in candidates])
                ranked = rank_candidates(candidates, scores)
                reproduction = None
                if instruction_name == "default":
                    stored_ranked = rows[question_id]["arms"]["candidate_003"]["stages"]["ranked"]["candidates"]
                    old_scores = {item["chunk_id"]: item["rerank_score"] for item in stored_ranked}
                    reproduction = {
                        "rank_order_matches_saved_paired_result": [
                            item["chunk_id"] for item in ranked
                        ] == [item["chunk_id"] for item in stored_ranked],
                        "max_absolute_score_delta": max(
                            abs(item["rerank_score"] - old_scores[item["chunk_id"]])
                            for item in ranked
                        ),
                    }
                selected = select_context(ranked, top_k=5, char_budget=8000)
                for index, candidate in enumerate(selected, start=1):
                    candidate["final_rank"] = index
                question_result["instructions"][instruction_name] = {
                    "baseline_reproduction": reproduction,
                    "target_tokenization": target_tokenization,
                    "target_ranks": _rank_summary(ranked, TARGETS[question_id]),
                    "ranked_candidates": [
                        {
                            "rank": rank,
                            "chunk_id": item["chunk_id"],
                            "source_path": item["source_path"],
                            "start_line": item.get("start_line"),
                            "end_line": item.get("end_line"),
                            "page_number": item.get("page_number"),
                            "characters": len(item["text"]),
                            "text_sha256": _sha256_text(item["text"]),
                            "pre_rerank_rank": item["pre_rerank_rank"],
                            "rerank_score": item["rerank_score"],
                        }
                        for rank, item in enumerate(ranked, start=1)
                    ],
                    "selected_context": _selected_summary(
                        selected, TARGETS[question_id], question_id
                    ),
                }
            result["questions"][question_id] = question_result
        result["status"] = "completed"
    except Exception as error:
        result["status"] = "failed"
        result["failure"] = {"type": type(error).__name__, "detail": str(error)}
        raise
    finally:
        reranker.close()
        result["finished_unix"] = time.time()
        _write_outputs(result)
    print(f"JSON: {OUTPUT.relative_to(ROOT).as_posix()}")
    print(f"Report: {REPORT.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
