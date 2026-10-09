"""用共享source_context helper做固定3D Tiles题集metadata-only paired评估。"""

from __future__ import annotations

import argparse
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

from evaluation.experiments.diagnose_3dtiles_structural_context import (  # noqa: E402
    MANIFEST,
    PAIRED,
    QIDS,
    QUESTION_SHA256,
    QUESTIONS,
    TARGET_ID,
    TARGETS,
    _candidate_match,
    _read_verified_sources,
    _sha,
    _validate_inputs,
)

OUT_DIR = ROOT / "artifacts/phase1-source-replay-validation"
CPU_PATH = OUT_DIR / "phase2-structural-context-helper-fixture.json"
PILOT_PATH = OUT_DIR / "phase2-structural-context-helper-pilot.json"
FULL_PATH = OUT_DIR / "phase2-structural-context-helper-24-pair.json"


def _checkpoint(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * 2**attempt)


def _load_frozen_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """固定路径与SHA先于解析；沿用已审阅的20/30 pool和source manifest helper。"""
    rows, two_rows, two_pools, checks = _validate_inputs()
    actual_sha = hashlib.sha256(QUESTIONS.read_bytes()).hexdigest()
    if actual_sha != QUESTION_SHA256:
        raise ValueError("fixed supplemental question SHA256 mismatch")
    paired = json.loads(PAIRED.read_text(encoding="utf-8"))
    all_pools = {
        qid: item["arms"]["candidate_003"]["stages"]["fused"]["candidates"]
        for qid, item in rows.items()
    }
    if len(all_pools) != 24:
        raise ValueError("expected fixed 24-question candidate_003 pools")
    for qid, candidates in all_pools.items():
        if not 1 <= len(candidates) <= 30:
            raise ValueError(f"{qid}: invalid saved rerank pool size")
        for candidate in candidates:
            if (candidate.get("target_id") != TARGET_ID
                    or candidate.get("library") != "ogc-3dtiles"
                    or str(candidate.get("version")) != "1.1"):
                raise ValueError(f"{qid}: candidate identity differs from allowlisted target")
            if candidate.get("source_path") not in {
                "raw_data/ogc-3dtiles/1.1/22-025r4.html",
                "raw_data/ogc-3dtiles/1.1/22-025r4.pdf",
            }:
                raise ValueError(f"{qid}: source outside explicit allowlist")
            if candidate.get("content_sha256") and candidate.get("content_sha256") != _sha(candidate["text"]):
                raise ValueError(f"{qid}/{candidate['chunk_id']}: candidate text SHA mismatch")
    if paired.get("status") != "completed" or paired.get("dataset", {}).get("sha256") != actual_sha:
        raise ValueError("saved pair is not completed against fixed dataset SHA")
    return {"rows": rows, "two_rows": two_rows, "checks": checks}, all_pools, paired


def _structures() -> tuple[dict[str, Any], dict[str, Any]]:
    from wenjin.retrieval.source_context import (
        OGC_3DTILES_PDF_PROFILE,
        extract_html_structure,
        extract_pdf_structure,
    )

    source_bytes, hashes = _read_verified_sources()
    structures = {
        "raw_data/ogc-3dtiles/1.1/22-025r4.html": extract_html_structure(
            source_bytes["raw_data/ogc-3dtiles/1.1/22-025r4.html"]
        ),
        "raw_data/ogc-3dtiles/1.1/22-025r4.pdf": extract_pdf_structure(
            source_bytes["raw_data/ogc-3dtiles/1.1/22-025r4.pdf"],
            profile=OGC_3DTILES_PDF_PROFILE,
        ),
    }
    if not all(check["manifest_raw_match"] and check["manifest_canonical_match"] for check in hashes.values()):
        raise ValueError("source must pass manifest raw and canonical hashes before structure extraction")
    return structures, {"sha256_verified_bytes": hashes, "manifest_path": MANIFEST.relative_to(ROOT).as_posix()}


def _fixture(inputs: dict[str, Any], pools: dict[str, Any], structures: dict[str, Any], hashes: dict[str, Any]) -> dict[str, Any]:
    from wenjin.retrieval.source_context import (
        candidate_document_with_context,
        metadata_for_candidate,
    )

    contracts = {}
    for qid, candidates in pools.items():
        contracts[qid] = []
        for candidate in candidates:
            meta = metadata_for_candidate(candidate, structures)
            doc = candidate_document_with_context(candidate, meta)
            if not doc.endswith(candidate["text"]):
                raise AssertionError(f"{qid}/{candidate['chunk_id']}: reranker input changed source body")
            contracts[qid].append({
                "chunk_id": candidate["chunk_id"],
                "locator": {key: candidate.get(key) for key in ("source_path", "start_line", "end_line", "page_number")},
                "text_sha256": _sha(candidate["text"]),
                "reranker_input_sha256": _sha(doc),
                "metadata": meta,
            })
    pdf = structures["raw_data/ogc-3dtiles/1.1/22-025r4.pdf"]
    return {
        "status": "cpu_fixture_completed",
        "scope": "fixed 3D Tiles 1.1 supplemental_validation only",
        "dataset_sha256": QUESTION_SHA256,
        "source_context_module": "src/wenjin/retrieval/source_context.py",
        "source_profile": "ogc-3dtiles-numbered-outline-v1; explicit 3D Tiles-only profile",
        "source_hashes": hashes,
        "candidate_count": sum(map(len, pools.values())),
        "candidate_body_hash_audit": {
            "declared_content_sha256_present": sum(bool(item.get("content_sha256"))
                                                     for rows in pools.values() for item in rows),
            "declared_hashes_missing": sum(not item.get("content_sha256")
                                            for rows in pools.values() for item in rows),
            "computed_text_hashes_recorded_per_candidate": True,
            "exact_source_locator_replay": "409/409 unique candidates across all 24 pools; see phase2-helper-24-candidate-source-replay.json.",
        },
        "candidate_contracts": contracts,
        "pdf_page_probes": {str(page): pdf.by_position.get(page, {}) for page in (205, 207, 240)},
        "controls": {"candidate_k": 20, "rerank_pool": 30, "top_k": 5, "char_budget": 8000,
                     "max_length": 1024, "batch_size": 2, "instruction": "default"},
        "no_index_or_evidence_mutation": True,
    }


def _rank_outcome(qid: str, ranked: list[dict], query: str, reranker: Any, candidates: list[dict], structures: dict[str, Any]) -> dict:
    from wenjin.retrieval.service import select_context
    from wenjin.retrieval.source_context import (
        candidate_document_with_context,
        metadata_for_candidate,
    )

    targets = TARGETS.get(qid, {})
    selected = select_context(ranked, top_k=5, char_budget=8000)
    diagnostic = {}
    for name, selector in targets.items():
        target = next((item for item in candidates if _candidate_match(item, selector)), None)
        if target is None:
            diagnostic[name] = {"in_candidate_pool": False}
            continue
        metadata = metadata_for_candidate(target, structures)
        document = candidate_document_with_context(target, metadata)
        token_ids = reranker.tokenizer.encode(reranker._format_pair(query, document), add_special_tokens=False)
        allowed = reranker.max_length - len(reranker.prefix_tokens) - len(reranker.suffix_tokens)
        retained = token_ids[:allowed] if reranker.tokenizer.truncation_side == "right" else token_ids[-allowed:]
        retained_text = reranker.tokenizer.decode(retained, skip_special_tokens=False)
        diagnostic[name] = {
            "in_candidate_pool": True,
            "metadata": metadata,
            "full_pair_tokens": len(token_ids), "content_budget_tokens": allowed,
            "truncated": len(token_ids) > allowed,
            "target_phrase_present_after_truncation": selector[3] in retained_text,
        }
    original = {item["chunk_id"]: item for item in candidates}
    for item in ranked:
        src = original[item["chunk_id"]]
        for field in ("text", "source_path", "start_line", "end_line", "page_number"):
            if item.get(field) != src.get(field):
                raise AssertionError(f"candidate evidence field changed: {field}")
    return {
        "candidate_count": len(ranked),
        "target_ranks": {name: [i for i, item in enumerate(ranked, 1) if _candidate_match(item, selector)]
                         for name, selector in targets.items()},
        "target_tokenization": diagnostic,
        "ranked_candidates": [{"rank": i, "chunk_id": item["chunk_id"], "rerank_score": item["rerank_score"],
                               "text_sha256": _sha(item["text"]),
                               "locator": {key: item.get(key) for key in ("source_path", "start_line", "end_line", "page_number")}}
                              for i, item in enumerate(ranked, 1)],
        "selected_context": [{"rank": next(i for i, row in enumerate(ranked, 1) if row["chunk_id"] == item["chunk_id"]),
                              "chunk_id": item["chunk_id"], "text_sha256": _sha(item["text"]), "text": item["text"],
                              "locator": {key: item.get(key) for key in ("source_path", "start_line", "end_line", "page_number")}}
                             for item in selected],
        "context_chars_used": sum(len(item["text"]) for item in selected),
        "context_char_budget": 8000,
        "target_in_selected": {name: any(_candidate_match(item, selector) for item in selected)
                               for name, selector in targets.items()},
    }


def _span_hit(candidate: dict[str, Any], span: dict[str, Any]) -> bool:
    locator = candidate.get("locator", candidate)
    if locator.get("source_path") != span.get("path"):
        return False
    if span.get("page") is not None:
        return locator.get("page_number") == span.get("page")
    start, end = locator.get("start_line"), locator.get("end_line")
    return isinstance(start, int) and isinstance(end, int) and start <= span["start_line"] and end >= span["end_line"]


def _covered(candidates: list[dict[str, Any]], spans: list[dict[str, Any]]) -> bool:
    return bool(spans) and all(any(_span_hit(candidate, span) for candidate in candidates) for span in spans)


def _write_summary(pilot: dict[str, Any], full: dict[str, Any]) -> None:
    groups = {"all_24": [], "longline_q002_q021": []}
    for detail in full.get("details", []):
        qid, spans = detail["input"]["id"], detail["input"].get("evidence", [])
        new_ranked = detail["new_metadata"]["ranked_candidates"]
        new_selected = detail["new_metadata"]["selected_context"]
        baseline_ranked = detail["baseline_cached"]["ranked_candidates"]
        baseline_selected = detail["baseline_cached"]["selected_context"].get("evidence", [])
        detail["locator_coverage_comparison"] = {
            "evidence_spans_all_hit_at_ranked_5": {
                "baseline_cached": _covered(baseline_ranked[:5], spans),
                "metadata_new_inference": _covered(new_ranked[:5], spans),
            },
            "evidence_spans_all_hit_at_ranked_20": {
                "baseline_cached": _covered(baseline_ranked[:20], spans),
                "metadata_new_inference": _covered(new_ranked[:20], spans),
            },
            "evidence_spans_all_hit_in_selected_context": {
                "baseline_cached": _covered(baseline_selected, spans),
                "metadata_new_inference": _covered(new_selected, spans),
            },
        }
        groups["all_24"].append(detail)
        if qid in {f"supplemental-3dtiles-longline-{n:03d}" for n in range(2, 22)}:
            groups["longline_q002_q021"].append(detail)

    group_metrics = {}
    for group, details in groups.items():
        group_metrics[group] = {"question_count": len(details)}
        for metric, name in (("evidence_spans_all_hit_at_ranked_5", "ranked_5"),
                             ("evidence_spans_all_hit_at_ranked_20", "ranked_20"),
                             ("evidence_spans_all_hit_in_selected_context", "selected") ):
            group_metrics[group][name] = {
                arm: sum(row["locator_coverage_comparison"][metric][arm] for row in details)
                for arm in ("baseline_cached", "metadata_new_inference")
            }
    full["locator_coverage_summary"] = group_metrics
    _checkpoint(FULL_PATH, full)
    mixed_frame = full.get("condition") == "mixed_frame_complete_json_schema_baseline_else_context"
    lines = [
        ("# Shared source-context helper: mixed-frame paired reranking" if mixed_frame
         else "# Shared source-context helper: metadata-only paired reranking"), "",
        ("Complete parseable JSON objects with root string `$schema` and `$id` used the cached no-metadata document frame; all other candidates used the helper-derived context frame. Retrieval pools and the completed route-003 no-metadata/default-instruction scores were reused as baseline; this condition ran new model inference over all 24 candidate pools. This is not a 24-question retrieval rerun."
         if mixed_frame else
         "The reranker received helper-derived metadata plus the unchanged source candidate body. Retrieval pools and the completed route-003 no-metadata/default-instruction scores were reused; only the metadata condition below ran new model inference. This is not a 24-question retrieval rerun."), "",
        "| Question | Target | Cached baseline rank/selected | New helper rank/selected |", "|---|---|---|---|",
    ]
    for detail in pilot.get("details", []):
        qid = detail["input"]["id"]
        new = detail["new_metadata"]
        for name, ranks in new["target_ranks"].items():
            old_rank = next((i for i, row in enumerate(detail["baseline_cached"]["ranked_candidates"], 1)
                             if _candidate_match(row, TARGETS[qid][name])), None)
            old_selected = any(_candidate_match(row, TARGETS[qid][name])
                               for row in detail["baseline_cached"]["selected_context"].get("evidence", []))
            lines.append(f"| {qid} | {name} | {old_rank or '—'} / {old_selected} | {ranks[0] if ranks else '—'} / {new['target_in_selected'].get(name, False)} |")
    lines.extend([
        "", "## Exact locator coverage", "",
        "| Set | Measure | Cached no-metadata baseline | New metadata inference |", "|---|---|---:|---:|",
    ])
    for group, label in (("all_24", "All 24"), ("longline_q002_q021", "Longline Q002–Q021")):
        metrics = full.get("locator_coverage_summary", {}).get(group, {})
        for measure, key in (("ranked@5", "ranked_5"), ("ranked@20", "ranked_20"), ("selected context", "selected")):
            values = metrics.get(key, {})
            lines.append(f"| {label} | {measure} | {values.get('baseline_cached', 0)}/{metrics.get('question_count', 0)} | {values.get('metadata_new_inference', 0)}/{metrics.get('question_count', 0)} |")
    lines.extend([
        "", "## Scope and limitations", "",
        "The PDF profile is explicitly limited to the reviewed 3D Tiles 1.1 source. Outline-derived context is labeled physical-page context, not paragraph scope. The mixed p.205 A.8.5/A.9 page receives no section/title metadata; p.207 and p.240 retain only weak page-level context. HTML section and table titles are used only when the candidate's source-line interval is unambiguous. The 50-question general validation set is not given this PDF profile.", "",
        "The 24 questions are a long-line supplemental stress set, not blind evaluation. The output preserves source body and locators; character budget selection remains the production selector. Full details and per-question checkpoints are in the JSON artifact.", "",
        f"Pilot status: `{pilot.get('status')}`. Full 24 status: `{full.get('status')}`.",
    ])
    path = FULL_PATH.with_suffix(".md")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-only", action="store_true", help="只写source/helper输入fixture，不加载模型")
    parser.add_argument("--summarize-existing", action="store_true", help="仅为已完成模型结果补算locator对照汇总")
    parser.add_argument("--mixed-frame", action="store_true",
                        help="完整JSON Schema候选沿用缓存baseline输入，其余候选使用source context")
    args = parser.parse_args()
    global CPU_PATH, PILOT_PATH, FULL_PATH
    if args.mixed_frame:
        CPU_PATH = OUT_DIR / "phase2-structural-context-helper-mixed-frame-fixture.json"
        PILOT_PATH = OUT_DIR / "phase2-structural-context-helper-mixed-frame-pilot.json"
        FULL_PATH = OUT_DIR / "phase2-structural-context-helper-mixed-frame-24-pair.json"
    if args.summarize_existing:
        pilot = json.loads(PILOT_PATH.read_text(encoding="utf-8"))
        full = json.loads(FULL_PATH.read_text(encoding="utf-8"))
        if full.get("status") != "completed" or len(full.get("details", [])) != 24:
            raise ValueError("existing result must contain a completed 24-question run")
        _write_summary(pilot, full)
        return
    inputs, pools, paired = _load_frozen_inputs()
    structures, source_hashes = _structures()
    fixture = _fixture(inputs, pools, structures, source_hashes)
    _checkpoint(CPU_PATH, fixture)
    if args.cpu_only:
        print(f"CPU fixture completed: {CPU_PATH.relative_to(ROOT)}")
        return

    # 单次GPU生命周期：先完成两题新helper pilot并保存独立文件，再延伸到24题。
    import torch

    from wenjin.retrieval.rerank import DEFAULT_INSTRUCTION, Qwen3Reranker, rank_candidates
    from wenjin.retrieval.source_context import (
        candidate_document_with_context,
        is_complete_self_describing_json_schema,
        json_schema_document_classification,
        metadata_for_candidate,
    )
    from wenjin.settings import Settings

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    settings = Settings()
    reranker = Qwen3Reranker(settings.reranker_model_path,
                             device="cuda" if torch.cuda.is_available() else "cpu",
                             max_length=1024, batch_size=2, instruction=DEFAULT_INSTRUCTION)
    condition = ("mixed_frame_complete_json_schema_baseline_else_context" if args.mixed_frame
                 else "metadata_only_default_instruction")
    pilot = {"status": "running", "dataset_sha256": QUESTION_SHA256,
             "helper": "wenjin.retrieval.source_context", "source_profile": "ogc-3dtiles-numbered-outline-v1",
             "condition": condition, "cpu_fixture": CPU_PATH.relative_to(ROOT).as_posix(),
             "controls": fixture["controls"], "details": [], "model": settings.reranker_model_path.name,
             "device": reranker.device}
    full = {"status": "running", "dataset_sha256": QUESTION_SHA256,
            "dataset_path": QUESTIONS.relative_to(ROOT).as_posix(), "question_count": 24,
            "helper": "wenjin.retrieval.source_context", "source_profile": "ogc-3dtiles-numbered-outline-v1",
            "condition": condition, "cpu_fixture": CPU_PATH.relative_to(ROOT).as_posix(),
            "baseline_source": "paired-results.json candidate_003 ranked/selected baseline from completed route-003 run",
            "controls": fixture["controls"], "details": [], "model": settings.reranker_model_path.name,
            "device": reranker.device}
    if args.mixed_frame:
        full["source_context_helper_sha256"] = _sha(
            (ROOT / "src/wenjin/retrieval/source_context.py").read_text(encoding="utf-8")
        )
        full["frozen_candidate_pool_sha256"] = _sha(
            json.dumps(pools, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        )
        full["frame_policy"] = (
            "Use cached no-metadata document frame only for complete json.loads(text) dicts whose root "
            "contains string $schema and $id; use source-context helper frame for every other candidate."
        )
    _checkpoint(PILOT_PATH, pilot)
    _checkpoint(FULL_PATH, full)
    try:
        for qid, row in inputs["rows"].items():
            candidates = pools[qid]
            query = row["input"]["query"]
            metadata_rows = [metadata_for_candidate(candidate, structures) for candidate in candidates]
            docs = [candidate_document_with_context(candidate, metadata)
                    for candidate, metadata in zip(candidates, metadata_rows)]
            if args.mixed_frame:
                from wenjin.retrieval.rerank import candidate_document

                frame_audit = []
                for candidate, metadata, document in zip(candidates, metadata_rows, docs):
                    schema_guard = is_complete_self_describing_json_schema(candidate)
                    base_document = candidate_document(candidate)
                    if schema_guard and document != base_document:
                        raise AssertionError("complete self-describing JSON must preserve baseline model input")
                    frame_audit.append({
                        "chunk_id": candidate["chunk_id"],
                        "locator": {key: candidate.get(key) for key in
                                    ("source_path", "start_line", "end_line", "page_number")},
                        "text_sha256": _sha(candidate["text"]),
                        "frame_decision": "cached_no_metadata_frame" if schema_guard else "new_source_context_frame",
                        "json_schema_guard": schema_guard,
                        "json_parse_classification": json_schema_document_classification(candidate),
                        "metadata": metadata,
                        "actual_document_sha256": _sha(document),
                        "baseline_document_sha256": _sha(base_document),
                    })
            else:
                frame_audit = []
            scores = reranker.score(query, docs)
            ranked = rank_candidates(candidates, scores)
            outcome = _rank_outcome(qid, ranked, query, reranker, candidates, structures)
            cached = row["arms"]["candidate_003"]
            detail = {"input": row["input"], "new_metadata": outcome,
                      "mixed_frame_candidate_inputs": frame_audit,
                      "baseline_cached": {"ranked_candidates": cached["stages"]["ranked"]["candidates"],
                                          "selected_context": cached["selected_context"],
                                          "locator_metrics": cached["stages"]["ranked"]["locator_metrics"],
                                          "contract_metrics": cached["stages"]["ranked"]["contract_metrics"]}}
            full["details"].append(detail)
            if qid in QIDS:
                pilot["details"].append(detail)
                _checkpoint(PILOT_PATH, pilot)
                if qid == QIDS[-1] and len(pilot["details"]) == len(QIDS):
                    check = {
                        QIDS[0]: "pdf_value",
                        QIDS[1]: "html",
                    }
                    for pilot_detail in pilot["details"]:
                        pilot_qid = pilot_detail["input"]["id"]
                        target = check[pilot_qid]
                        if not pilot_detail["new_metadata"]["target_in_selected"].get(target, False):
                            raise RuntimeError(f"helper pilot gate failed: {pilot_qid}/{target} not selected")
                    pilot["status"] = "pilot_passed"
                    pilot["pilot_gate"] = "Q003 PDF p.207 and Q005 HTML BATCH_LENGTH selected under unchanged default instruction"
                    _checkpoint(PILOT_PATH, pilot)
                    print("[pilot] two target checks passed; continuing 24-question paired run", flush=True)
            # 每题落盘，异常也可保留此前完成题目与失败题号。
            full["completed_questions"] = len(full["details"])
            _checkpoint(FULL_PATH, full)
            print(f"[{len(full['details'])}/24] {qid} metadata-only评分已保存", flush=True)
        pilot["status"] = "completed"
        full["status"] = "completed"
    except Exception as error:
        pilot["status"] = "failed" if pilot.get("completed_questions", 0) else "pilot_failed"
        full["status"] = "failed"
        failure = {"type": type(error).__name__, "detail": str(error)}
        pilot["failure"] = full["failure"] = failure
        raise
    finally:
        pilot["finished_unix"] = full["finished_unix"] = time.time()
        _checkpoint(PILOT_PATH, pilot)
        _checkpoint(FULL_PATH, full)
        _write_summary(pilot, full)
        reranker.close()
    print(f"CPU fixture: {CPU_PATH.relative_to(ROOT)}")
    print(f"Pilot: {PILOT_PATH.relative_to(ROOT)}")
    print(f"24 paired: {FULL_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
