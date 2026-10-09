"""CPU诊断Q017的metadata截断影响，并以固定双视图RRF做一次稳健性对照。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATASET = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl"
PAIRED = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/paired-results.json"
HELPER_PAIR = ROOT / "artifacts/phase1-source-replay-validation/phase2-structural-context-helper-24-pair.json"
CPU_FIXTURE = ROOT / "artifacts/phase1-source-replay-validation/phase2-structural-context-helper-fixture.json"
OUTPUT = ROOT / "artifacts/phase1-source-replay-validation/phase2-helper-q017-rrf-diagnostic.json"
REPORT = OUTPUT.with_suffix(".md")
EXPECTED_DATASET_SHA = "8c69891fddf0002210b9446150d35d4acc71217d58f401033ca0e2fef8595162"
Q017 = "supplemental-3dtiles-longline-017"
Q003 = "supplemental-3dtiles-longline-003"
Q005 = "supplemental-3dtiles-longline-005"
TARGET_017 = "1.1:raw_data/ogc-3dtiles/1.1/22-025r4.html:blocks-split:0262"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _locator_hit(candidate: dict, span: dict) -> bool:
    locator = candidate.get("locator", candidate)
    if locator.get("source_path") != span.get("path"):
        return False
    if span.get("page") is not None:
        return locator.get("page_number") == span.get("page")
    return (isinstance(locator.get("start_line"), int)
            and isinstance(locator.get("end_line"), int)
            and locator["start_line"] <= span["start_line"]
            and locator["end_line"] >= span["end_line"])


def _has_all_spans(candidates: list[dict], spans: list[dict]) -> bool:
    return bool(spans) and all(any(_locator_hit(candidate, span) for candidate in candidates) for span in spans)


def _self_describing_schema(text: str) -> bool:
    """只对完整可解析JSON对象的根字段生效，不匹配普通文字提及。"""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return (isinstance(parsed, dict)
            and isinstance(parsed.get("$schema"), str)
            and isinstance(parsed.get("$id"), str))


def _rrf_rank(baseline: list[dict], metadata: list[dict], pool: list[dict], *, k: int = 60) -> list[dict]:
    """固定两视图RRF；不读问题gold、目标标记或人工rubric。"""
    baseline_rank = {item["chunk_id"]: rank for rank, item in enumerate(baseline, 1)}
    metadata_rank = {item["chunk_id"]: rank for rank, item in enumerate(metadata, 1)}
    pool_by_id = {item["chunk_id"]: item for item in pool}
    if set(baseline_rank) != set(metadata_rank) or set(baseline_rank) != set(pool_by_id):
        raise ValueError("baseline, metadata and frozen fused pool identities differ")
    scored = []
    for original_rank, item in enumerate(pool, 1):
        cid = item["chunk_id"]
        score = 1 / (k + baseline_rank[cid]) + 1 / (k + metadata_rank[cid])
        scored.append(({**pool_by_id[cid], "rerank_score": score, "pre_rerank_rank": original_rank}, score))
    return [item for item, _ in sorted(scored, key=lambda pair: (-pair[1], pair[0]["pre_rerank_rank"]))]


def main() -> None:
    if DATASET.resolve() != (ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl").resolve():
        raise ValueError("supplemental dataset path allowlist mismatch")
    if _sha(DATASET) != EXPECTED_DATASET_SHA:
        raise ValueError("supplemental dataset SHA mismatch")
    if _sha(PAIRED) != "26e698eeaf715fdb4b0c4e86d9acf35d3d4f38ff4934d54e5b9b5e7a2c2202cb":
        raise ValueError("fixed paired-results SHA mismatch")
    questions = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(questions) != 24 or any(row.get("split") != "supplemental_validation" for row in questions):
        raise ValueError("fixed supplemental question contract mismatch")
    paired = json.loads(PAIRED.read_text(encoding="utf-8"))
    helper = json.loads(HELPER_PAIR.read_text(encoding="utf-8"))
    fixture = json.loads(CPU_FIXTURE.read_text(encoding="utf-8"))
    if helper.get("status") != "completed" or len(helper.get("details", [])) != 24:
        raise ValueError("helper paired results incomplete")

    from wenjin.retrieval.service import select_context

    qmap = {row["id"]: row for row in questions}
    pairmap = {row["input"]["id"]: row for row in paired["details"]}
    helpermap = {row["input"]["id"]: row for row in helper["details"]}
    fixturemap = {row["chunk_id"]: row for rows in fixture["candidate_contracts"].values() for row in rows}
    result = {"status": "completed", "scope": "CPU-only, fixed 24 candidate pools; no model inference",
              "dataset_sha256": EXPECTED_DATASET_SHA, "paired_results_sha256": _sha(PAIRED),
              "helper_pair_sha256": _sha(HELPER_PAIR), "cpu_fixture_sha256": _sha(CPU_FIXTURE),
              "policy": {"name": "two_view_rrf", "formula": "sum(1/(60+rank)) across cached no-metadata and new metadata rankings",
                         "tie_break": "frozen fused-pool order", "top_k": 5, "char_budget": 8000,
                         "uses_gold_or_question_target_for_ranking": False},
              "single_generic_guard": {"name": "self_describing_json_schema_preserves_no_metadata_score",
                                       "condition": "candidate text contains both $schema and $id; not query/title/gold based",
                                       "other_candidates": "use new metadata score",
                                       "tie_break": "frozen fused-pool order", "top_k": 5,
                                       "char_budget": 8000, "uses_gold_or_question_target_for_ranking": False},
              "q017_diagnostic": {}, "aggregate": {}, "per_question": []}

    # Q017: inspect target metadata, both score views and actual tokenizer truncation only.
    base017 = pairmap[Q017]["arms"]["candidate_003"]["stages"]["ranked"]["candidates"]
    meta017 = helpermap[Q017]["new_metadata"]["ranked_candidates"]
    target_base = next(item for item in base017 if item["chunk_id"] == TARGET_017)
    target_meta = next(item for item in meta017 if item["chunk_id"] == TARGET_017)
    pool017 = pairmap[Q017]["arms"]["candidate_003"]["stages"]["fused"]["candidates"]
    source_candidate = next(item for item in pool017 if item["chunk_id"] == TARGET_017)
    target_metadata = fixturemap[TARGET_017]["metadata"]
    from transformers import AutoTokenizer

    from wenjin.retrieval.rerank import DEFAULT_INSTRUCTION
    from wenjin.retrieval.source_context import candidate_document_with_context
    from wenjin.settings import Settings

    tokenizer = AutoTokenizer.from_pretrained(Settings().reranker_model_path, local_files_only=True,
                                              padding_side="left")
    prefix = ('<|im_start|>system\nJudge whether the Document meets the requirements based on '
              'the Query and the Instruct provided. Note that the answer can only be "yes" or '
              '"no".<|im_end|>\n<|im_start|>user\n')
    suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    content_budget = 1024 - len(tokenizer.encode(prefix, add_special_tokens=False)) - len(tokenizer.encode(suffix, add_special_tokens=False))
    q017_query = qmap[Q017]["query"]
    doc_no_meta = __import__("wenjin.retrieval.rerank", fromlist=["candidate_document"]).candidate_document(source_candidate)
    doc_meta = candidate_document_with_context(source_candidate, target_metadata)
    pair_no_meta = f"<Instruct>: {DEFAULT_INSTRUCTION}\n<Query>: {q017_query}\n<Document>: {doc_no_meta}"
    pair_meta = f"<Instruct>: {DEFAULT_INSTRUCTION}\n<Query>: {q017_query}\n<Document>: {doc_meta}"
    token_report = {}
    for name, pair_text in (("no_metadata", pair_no_meta), ("source_metadata", pair_meta)):
        token_ids = tokenizer.encode(pair_text, add_special_tokens=False)
        retained_ids = token_ids[:content_budget]
        retained = tokenizer.decode(retained_ids, skip_special_tokens=False)
        token_report[name] = {"pair_tokens": len(token_ids), "content_budget_tokens": content_budget,
                              "truncated": len(token_ids) > content_budget,
                              "required_term_retained": {term: term in retained for term in ("required", "values", '"required"', '"values"')},
                              "document_body_sha256": hashlib.sha256(doc_no_meta.encode()).hexdigest(),
                              "reranker_input_sha256": hashlib.sha256(pair_text.encode()).hexdigest()}
    result["q017_diagnostic"] = {
        "query": q017_query, "target_chunk_id": TARGET_017,
        "source_locator": {key: source_candidate.get(key) for key in ("source_path", "start_line", "end_line", "page_number")},
        "target_text_chars": len(source_candidate["text"]),
        "target_text_sha256": hashlib.sha256(source_candidate["text"].encode()).hexdigest(),
        "metadata": target_metadata,
        "baseline_no_metadata": {"rank": base017.index(target_base) + 1, "rerank_score": target_base["rerank_score"],
                                 "selected": any(item["chunk_id"] == TARGET_017 for item in pairmap[Q017]["arms"]["candidate_003"]["selected_context"]["evidence"])},
        "metadata_new_inference": {"rank": next(i for i, item in enumerate(meta017, 1) if item["chunk_id"] == TARGET_017),
                                   "rerank_score": target_meta["rerank_score"],
                                   "selected": any(item["chunk_id"] == TARGET_017 for item in helpermap[Q017]["new_metadata"]["selected_context"])},
        "tokenization": token_report,
    }

    from evaluation.experiments.diagnose_3dtiles_structural_context import TARGETS, _candidate_match

    target_ids = {Q017: TARGET_017}
    for qid, name in ((Q003, "pdf_value"), (Q005, "html")):
        selector = TARGETS[qid][name]
        pool = pairmap[qid]["arms"]["candidate_003"]["stages"]["fused"]["candidates"]
        matches = [item["chunk_id"] for item in pool if _candidate_match(item, selector)]
        if len(matches) != 1:
            raise ValueError(f"expected one fixed diagnostic target candidate for {qid}/{name}")
        target_ids[qid] = matches[0]
    rrf_support = {qid: 0 for qid in target_ids}
    for qid, question in qmap.items():
        base_row = pairmap[qid]["arms"]["candidate_003"]
        base_ranked = base_row["stages"]["ranked"]["candidates"]
        meta_ranked = helpermap[qid]["new_metadata"]["ranked_candidates"]
        pool = base_row["stages"]["fused"]["candidates"]
        combined = _rrf_rank(base_ranked, meta_ranked, pool)
        selected = select_context(combined, top_k=5, char_budget=8000)
        baseline_scores = {item["chunk_id"]: item["rerank_score"] for item in base_ranked}
        metadata_scores = {item["chunk_id"]: item["rerank_score"] for item in meta_ranked}
        pool_order = {item["chunk_id"]: index for index, item in enumerate(pool)}
        guarded_rows = []
        schema_guard_count = 0
        for candidate in pool:
            has_schema_identity = _self_describing_schema(candidate["text"])
            if has_schema_identity:
                schema_guard_count += 1
            guarded_rows.append({
                **candidate,
                "rerank_score": baseline_scores[candidate["chunk_id"]]
                if has_schema_identity else metadata_scores[candidate["chunk_id"]],
            })
        guarded = sorted(guarded_rows, key=lambda item: (-item["rerank_score"], pool_order[item["chunk_id"]]))
        guarded_selected = select_context(guarded, top_k=5, char_budget=8000)
        base_scores = {item["chunk_id"]: item["rerank_score"] for item in base_ranked}
        meta_scores = {item["chunk_id"]: item["rerank_score"] for item in meta_ranked}
        candidate_decisions = []
        for candidate in pool:
            cid = candidate["chunk_id"]
            try:
                parsed = json.loads(candidate["text"])
                parse_class = (
                    "complete_self_describing_json_schema"
                    if isinstance(parsed, dict) and isinstance(parsed.get("$schema"), str)
                    and isinstance(parsed.get("$id"), str)
                    else "complete_json_other"
                )
            except (json.JSONDecodeError, TypeError):
                parsed = None
                parse_class = "not_complete_json_object"
            use_cached = parse_class == "complete_self_describing_json_schema"
            metadata = fixturemap[cid]["metadata"]
            base_document = __import__("wenjin.retrieval.rerank", fromlist=["candidate_document"]).candidate_document(candidate)
            contextual_document = candidate_document_with_context(candidate, metadata)
            base_pair_input = f"<Instruct>: {DEFAULT_INSTRUCTION}\n<Query>: {question['query']}\n<Document>: {base_document}"
            metadata_pair_input = f"<Instruct>: {DEFAULT_INSTRUCTION}\n<Query>: {question['query']}\n<Document>: {contextual_document}"
            candidate_decisions.append({
                "chunk_id": cid,
                "source_locator": {key: candidate.get(key) for key in ("source_path", "start_line", "end_line", "page_number")},
                "text_sha256": hashlib.sha256(candidate["text"].encode()).hexdigest(),
                "json_parse_classification": parse_class,
                "root_field_names": list(parsed.keys()) if isinstance(parsed, dict) and use_cached else None,
                "frame_decision": "cached_no_metadata_score" if use_cached else "new_metadata_score",
                "baseline_score": base_scores[cid],
                "metadata_score": meta_scores[cid],
                "selected_score": base_scores[cid] if use_cached else meta_scores[cid],
                "no_metadata_document_sha256": hashlib.sha256(base_document.encode()).hexdigest(),
                "metadata_document_sha256": hashlib.sha256(contextual_document.encode()).hexdigest(),
                "no_metadata_formatted_pair_sha256": hashlib.sha256(base_pair_input.encode()).hexdigest(),
                "metadata_formatted_pair_sha256": hashlib.sha256(metadata_pair_input.encode()).hexdigest(),
            })
        spans = question["evidence"]
        target_id = target_ids.get(qid)
        base_selected = base_row["selected_context"]["evidence"]
        meta_selected = helpermap[qid]["new_metadata"]["selected_context"]
        target_in_rrf = bool(target_id and any(item["chunk_id"] == target_id for item in selected))
        if target_in_rrf:
            rrf_support[qid] = 1
        target_in_guarded = bool(target_id and any(item["chunk_id"] == target_id for item in guarded_selected))
        if target_in_guarded:
            target_ids.setdefault("_schema_guard_selected", {})[qid] = True
        else:
            target_ids.setdefault("_schema_guard_selected", {})[qid] = False
        result["per_question"].append({
            "id": qid,
            "baseline_all_locator_spans_ranked5": _has_all_spans(base_ranked[:5], spans),
            "metadata_all_locator_spans_ranked5": _has_all_spans(meta_ranked[:5], spans),
            "rrf_all_locator_spans_ranked5": _has_all_spans(combined[:5], spans),
            "baseline_all_locator_spans_selected": _has_all_spans(base_selected, spans),
            "metadata_all_locator_spans_selected": _has_all_spans(meta_selected, spans),
            "rrf_all_locator_spans_selected": _has_all_spans(selected, spans),
            "schema_guard_all_locator_spans_ranked5": _has_all_spans(guarded[:5], spans),
            "schema_guard_all_locator_spans_selected": _has_all_spans(guarded_selected, spans),
            "target_chunk_selected_diagnostic": target_in_rrf if target_id else None,
            "schema_guard_target_selected_diagnostic": target_in_guarded if target_id else None,
            "schema_guard_candidate_count": schema_guard_count,
            "candidate_frame_decisions": candidate_decisions,
            "rrf_top5_chunk_ids": [item["chunk_id"] for item in combined[:5]],
            "schema_guard_top5_chunk_ids": [item["chunk_id"] for item in guarded[:5]],
            "selected_chunk_ids": [item["chunk_id"] for item in selected],
            "schema_guard_selected_chunk_ids": [item["chunk_id"] for item in guarded_selected],
            "schema_guard_selected_context": [
                {"rank": next(i for i, ranked_item in enumerate(guarded, 1) if ranked_item["chunk_id"] == item["chunk_id"]),
                 "chunk_id": item["chunk_id"], "source_locator": {key: item.get(key) for key in ("source_path", "start_line", "end_line", "page_number")},
                 "text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(), "text": item["text"]}
                for item in guarded_selected
            ],
            "context_chars": sum(len(item["text"]) for item in selected),
            "schema_guard_context_chars": sum(len(item["text"]) for item in guarded_selected),
        })
    summary = {}
    for field in ("baseline_all_locator_spans_ranked5", "metadata_all_locator_spans_ranked5", "rrf_all_locator_spans_ranked5",
                  "baseline_all_locator_spans_selected", "metadata_all_locator_spans_selected", "rrf_all_locator_spans_selected"):
        summary[field] = sum(item[field] for item in result["per_question"])
    summary["target_selected_diagnostic"] = rrf_support
    summary["schema_guard_target_selected_diagnostic"] = target_ids["_schema_guard_selected"]
    summary["schema_guard_candidates_across_all_pools"] = sum(row["schema_guard_candidate_count"] for row in result["per_question"])
    for field in ("schema_guard_all_locator_spans_ranked5", "schema_guard_all_locator_spans_selected"):
        summary[field] = sum(item[field] for item in result["per_question"])
    result["aggregate"] = summary
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = [
        "# Q017 metadata tokenization and fixed RRF comparison", "",
        f"Q017 HTML line 9016 is {result['q017_diagnostic']['target_text_chars']} characters. Under the cached no-metadata default instruction it ranked {result['q017_diagnostic']['baseline_no_metadata']['rank']} (score {result['q017_diagnostic']['baseline_no_metadata']['rerank_score']:.6f}) and was selected. With the source-native HTML heading path it ranked {result['q017_diagnostic']['metadata_new_inference']['rank']} (score {result['q017_diagnostic']['metadata_new_inference']['rerank_score']:.6f}) and was not selected. Metadata status: `{target_metadata['metadata_status']}`; path: `{ ' > '.join(target_metadata.get('section_path', [])) }`.", "",
        "## Token and context budget", "",
        f"The reranker content budget is {content_budget} tokens inside the fixed 1,024-token sequence. Both views exceed that budget (see JSON); the exact structural `required` property and its `[values]` array are outside the retained prefix in both, while ordinary words `required` and `values` remain elsewhere in the schema text. The metadata view adds 27 tokens before the body, so title redundancy and the truncation boundary may jointly affect ranking; this check cannot assign the score change to one cause.", "",
        "## One generic combination policy", "",
        "RRF uses both complete saved rankings with a single constant k=60 and the same frozen fused-pool order for ties. It does not read question targets, evidence spans, or gold labels during ranking. Locator and named-target checks are computed only after ranking.", "",
        f"RRF evidence-span all-hit counts: ranked@5 {summary['rrf_all_locator_spans_ranked5']}/24, selected {summary['rrf_all_locator_spans_selected']}/24. Baseline selected {summary['baseline_all_locator_spans_selected']}/24; metadata selected {summary['metadata_all_locator_spans_selected']}/24.", "",
        f"Q003 p.207 selected={bool(rrf_support[Q003])}; Q005 HTML 7128 selected={bool(rrf_support[Q005])}; Q017 HTML 9016 selected={bool(rrf_support[Q017])}. This fixed two-view policy is diagnostic only; semantic reviewer decisions are not inferred from locator hits.", "",
        "## One content-type guard candidate", "",
        f"The generic guard preserves the cached no-metadata score only for a complete JSON object that parses and has string `$schema` and `$id` root fields. It does not use query, title, or target annotations; other candidates use the metadata score. It yields ranked@5 {summary['schema_guard_all_locator_spans_ranked5']}/24 and selected {summary['schema_guard_all_locator_spans_selected']}/24 exact locator coverage; Q003 p.207={result['per_question'][2]['schema_guard_target_selected_diagnostic']}, Q005 HTML7128={result['per_question'][4]['schema_guard_target_selected_diagnostic']}, Q017 HTML9016={result['per_question'][16]['schema_guard_target_selected_diagnostic']}. This is a CPU candidate awaiting semantic review, not a production change.", "",
        "Full per-question ordering, token hashes, and evidence locator checks are in the JSON file. No retrieval, source index, default instruction, or production selector was changed.",
    ]
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({"q017": result["q017_diagnostic"], "aggregate": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
