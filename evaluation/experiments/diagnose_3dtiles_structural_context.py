"""在固定3D Tiles候选池上评估可信来源结构metadata对重排的影响。"""

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

PAIRED = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/paired-results.json"
QUESTIONS = ROOT / "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl"
MANIFEST = ROOT / "corpora/ogc-3dtiles-1.1/source-manifest.json"
OUTPUT = ROOT / "artifacts/phase1-source-replay-validation/phase2-structural-context-pair.json"
REPORT = ROOT / "artifacts/phase1-source-replay-validation/phase2-structural-context-pair.md"
QUESTION_SHA256 = "8c69891fddf0002210b9446150d35d4acc71217d58f401033ca0e2fef8595162"
QIDS = ("supplemental-3dtiles-longline-003", "supplemental-3dtiles-longline-005")
TARGET_ID = "ogc-3dtiles-1.1-blocks-split-candidate-003"
TARGETS = {
    QIDS[0]: {
        "html": ("raw_data/ogc-3dtiles/1.1/22-025r4.html", 5903, None, "Application-specific data"),
        "pdf_value": ("raw_data/ogc-3dtiles/1.1/22-025r4.pdf", None, 207, "Application-specific data"),
        "pdf_scope": ("raw_data/ogc-3dtiles/1.1/22-025r4.pdf", None, 205, "Table A.10"),
    },
    QIDS[1]: {
        "html": ("raw_data/ogc-3dtiles/1.1/22-025r4.html", 7128, None, "BATCH_LENGTH"),
        "pdf": ("raw_data/ogc-3dtiles/1.1/22-025r4.pdf", None, 240, "BATCH_LENGTH"),
    },
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _html_visible(fragment: str) -> str:
    # 单行片段沿用生产HTML行解析，输出只用来生成metadata，不参与证据正文。
    from wenjin.ingestion.multiformat import _html_line

    return _html_line(fragment).strip()


def _read_verified_sources() -> tuple[dict[str, bytes], dict[str, Any]]:
    """复用manifest路径/Hash helper，并额外核对canonical Hash。"""
    from wenjin.content_hashing import hash_content
    from wenjin.manifest import file_records, load, read_verified_bytes, trusted_raw_root

    manifest = load(MANIFEST)
    raw_root = trusted_raw_root(manifest, ROOT / "raw_data")
    records = {entry["path"]: entry for entry in file_records(manifest)}
    sources: dict[str, bytes] = {}
    checks = {}
    for name in ("22-025r4.html", "22-025r4.pdf"):
        entry = records[name]
        content = read_verified_bytes(entry, raw_root)
        hashed = hash_content(
            content,
            normalization=entry["normalization"],
            encoding=entry.get("encoding"),
        )
        if hashed.canonical_sha256 != entry["canonical_sha256"]:
            raise ValueError(f"{name}: canonical source SHA256 mismatch")
        sources[f"raw_data/ogc-3dtiles/1.1/{name}"] = content
        checks[name] = {
            "bytes": len(content),
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "manifest_raw_match": hashed.raw_sha256 == entry["raw_sha256"],
            "manifest_canonical_match": hashed.canonical_sha256 == entry["canonical_sha256"],
        }
    return sources, checks


def _html_structure(content: bytes) -> tuple[dict[int, dict], dict[str, Any]]:
    from wenjin.retrieval.source_context import extract_html_structure

    structure = extract_html_structure(content)
    return structure.by_position, structure.diagnostics


def _pdf_structure(content: bytes) -> tuple[dict[int, dict], dict[str, Any]]:
    from wenjin.retrieval.source_context import OGC_3DTILES_PDF_PROFILE, extract_pdf_structure

    structure = extract_pdf_structure(content, profile=OGC_3DTILES_PDF_PROFILE)
    return structure.by_position, structure.diagnostics


def _metadata(candidate: dict, structures: dict[str, dict[int, dict]]) -> dict:
    from wenjin.retrieval.source_context import SourceStructure, metadata_for_candidate

    registered = {
        path: SourceStructure("pdf" if path.endswith(".pdf") else "html", values, {})
        for path, values in structures.items()
    }
    return metadata_for_candidate(candidate, registered)


def _document(candidate: dict, metadata: dict, *, direct: bool = False) -> str:
    from wenjin.retrieval.source_context import candidate_document_with_context

    return candidate_document_with_context(candidate, metadata)


def _candidate_match(candidate: dict, selector: tuple) -> bool:
    path, line, page, phrase = selector
    return (
        candidate.get("source_path") == path
        and (candidate.get("start_line") == line if line is not None else candidate.get("page_number") == page)
        and phrase in candidate.get("text", "")
    )


def _validate_inputs() -> tuple[dict, dict[str, dict], dict[str, list[dict]], dict]:
    actual_sha = hashlib.sha256(QUESTIONS.read_bytes()).hexdigest()
    if actual_sha != QUESTION_SHA256:
        raise ValueError("fixed supplemental question SHA256 mismatch")
    paired = json.loads(PAIRED.read_text(encoding="utf-8"))
    if paired.get("status") != "completed" or paired.get("dataset", {}).get("sha256") != actual_sha:
        raise ValueError("paired input must be completed and bound to fixed question SHA")
    expected_controls = {
        "library": "ogc-3dtiles", "version": "1.1", "candidate_k": 20,
        "top_k": 5, "rrf_k": 60, "rerank_pool": 30,
        "context_char_budget": 8000, "reranker_max_length": 1024,
        "reranker_batch_size": 2, "sequential_shared_models": True,
    }
    if paired.get("controls") != expected_controls:
        raise ValueError("paired controls differ from approved 20/30 baseline")
    rows = {item["input"]["id"]: item for item in paired["details"]}
    frozen = {
        item["id"]: item
        for item in (json.loads(line) for line in QUESTIONS.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    if len(rows) != 24 or set(rows) != set(frozen):
        raise ValueError("paired inputs must match the fixed 24-question supplemental set")
    for question_id, row in rows.items():
        for field in ("id", "split", "library", "version", "query", "evidence"):
            if row["input"].get(field) != frozen[question_id].get(field):
                raise ValueError(f"{question_id}: paired field differs from fixed question file: {field}")
        if row["input"].get("split") != "supplemental_validation":
            raise ValueError("unexpected split in paired inputs")
    candidate_pools = {
        qid: rows[qid]["arms"]["candidate_003"]["stages"]["fused"]["candidates"]
        for qid in QIDS
    }
    expected_sizes = {QIDS[0]: 27, QIDS[1]: 30}
    for qid, candidates in candidate_pools.items():
        if len(candidates) != expected_sizes[qid]:
            raise ValueError(f"{qid}: candidate pool size changed")
        for item in candidates:
            if item.get("target_id") != TARGET_ID or item.get("library") != "ogc-3dtiles" or str(item.get("version")) != "1.1":
                raise ValueError(f"{qid}: candidate identity mismatch")
            if item.get("source_path") not in {
                "raw_data/ogc-3dtiles/1.1/22-025r4.html",
                "raw_data/ogc-3dtiles/1.1/22-025r4.pdf",
            }:
                raise ValueError(f"{qid}: source outside explicit allowlist")
            if item.get("content_sha256") and item["content_sha256"] != _sha(item["text"]):
                raise ValueError(f"{qid}/{item['chunk_id']}: declared candidate text SHA mismatch")
    direct_path = ROOT / "artifacts/phase1-source-replay-validation/phase2-direct-evidence-rerank.json"
    direct = json.loads(direct_path.read_text(encoding="utf-8"))
    if direct.get("status") != "completed" or direct.get("input_integrity", {}).get("dataset_sha256_actual") != actual_sha:
        raise ValueError("saved no-metadata factorial reference does not match fixed dataset")
    if direct.get("controls", {}).get("candidate_k") != 20 or direct.get("controls", {}).get("rerank_pool") != 30:
        raise ValueError("saved no-metadata factorial reference controls mismatch")
    return rows, {qid: rows[qid] for qid in QIDS}, candidate_pools, {
        "dataset_path": "artifacts/line-preserving-validation-2026-09-29/3dtiles-supplement/questions.jsonl",
        "dataset_sha256": actual_sha,
        "question_count": len(rows),
        "qids": list(QIDS),
        "candidate_pool_sizes": expected_sizes,
        "target_id": TARGET_ID,
        "conditions": ["metadata_only_default_instruction", "metadata_plus_generic_direct_instruction"],
        "controls": {"candidate_k": 20, "rerank_pool": 30, "top_k": 5, "rrf_k": 60, "char_budget": 8000, "max_length": 1024, "batch_size": 2},
        "factorial_reference": {
            "source": direct_path.relative_to(ROOT).as_posix(),
            "no_metadata_default_instruction": {
                qid: direct["questions"][qid]["instructions"]["default"] for qid in QIDS
            },
            "no_metadata_generic_direct_instruction": {
                qid: direct["questions"][qid]["instructions"]["generic_direct_evidence"] for qid in QIDS
            },
        },
    }


def _build_result_fixture(sources: dict[str, bytes], source_hashes: dict, html_map: dict, pdf_map: dict, maps_meta: dict, inputs: dict) -> dict:
    """CPU阶段完整生成source map与输入契约fixture，GPU前即可独立审阅。"""
    structures = {"raw_data/ogc-3dtiles/1.1/22-025r4.html": html_map,
                  "raw_data/ogc-3dtiles/1.1/22-025r4.pdf": pdf_map}
    target_metadata = {}
    candidate_contracts = {}
    for qid in QIDS:
        candidates = inputs[qid]
        target_metadata[qid] = {}
        original_rows = []
        for label, selector in TARGETS[qid].items():
            found = [candidate for candidate in candidates if _candidate_match(candidate, selector)]
            target_metadata[qid][label] = [_metadata(item, structures) for item in found]
        for item in candidates:
            meta = _metadata(item, structures)
            augmented = _document(item, meta)
            if augmented.endswith(item["text"]) is False:
                raise AssertionError("reranker input lost verbatim source body")
            original_rows.append({
                "chunk_id": item["chunk_id"],
                "source_path": item["source_path"],
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "page_number": item.get("page_number"),
                "text_sha256": _sha(item["text"]),
                "declared_content_sha256": item.get("content_sha256"),
                "metadata": meta,
                "reranker_input_sha256": _sha(augmented),
            })
        candidate_contracts[qid] = original_rows
    return {
        "status": "cpu_fixture_completed",
        "created_unix": time.time(),
        "question_integrity": inputs["checks"],
        "source_hashes": source_hashes,
        "structure_extraction": maps_meta,
        "target_metadata": target_metadata,
        "candidate_contracts": candidate_contracts,
        "paired_candidate_integrity": {
            qid: {
                "candidate_count": len(rows),
                "declared_content_sha256_count": sum(row["declared_content_sha256"] is not None for row in rows),
                "body_hashes_computed": True,
                "source_locator_replay_evidence": "artifacts/phase1-source-replay-validation/phase2-candidate-source-replay.json",
            }
            for qid, rows in candidate_contracts.items()
        },
        "pdf_page_probes": {
            str(page): pdf_map.get(page, {}) for page in (205, 207, 240)
        },
        "controls": inputs["controls"],
        "text_and_locator_immutable": True,
        "index_modified": False,
        "source_bytes": {key: len(value) for key, value in sources.items()},
    }


def _save(result: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix(".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(6):
        try:
            os.replace(temp, OUTPUT)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.1 * 2**attempt)


def _rank_targets(ranked: list[dict], qid: str) -> dict:
    return {
        name: [rank for rank, item in enumerate(ranked, 1) if _candidate_match(item, selector)]
        for name, selector in TARGETS[qid].items()
    }


def _selected(ranked: list[dict], qid: str) -> dict:
    from wenjin.retrieval.service import select_context

    chosen = select_context(ranked, top_k=5, char_budget=8000)
    return {
        "context_chars_used": sum(len(item["text"]) for item in chosen),
        "context_char_budget": 8000,
        "target_in_selected": {
            name: any(_candidate_match(item, selector) for item in chosen)
            for name, selector in TARGETS[qid].items()
        },
        "selected_candidates": [
            {"rank": i, "chunk_id": item["chunk_id"], "source_path": item["source_path"],
             "start_line": item.get("start_line"), "end_line": item.get("end_line"),
             "page_number": item.get("page_number"), "text_sha256": _sha(item["text"]),
             "text": item["text"]}
            for i, item in enumerate(chosen, 1)
        ],
    }


def _write_report(result: dict) -> None:
    lines = [
        "# Q003/Q005 source-structure metadata experiment", "",
        "Frozen candidate pools and retrieval stages were reused. Source-native heading/table metadata was derived only after manifest raw and canonical hashes matched; metadata was added to reranker input, while returned text and locators remained unchanged.", "",
        "| Question | Condition | Target ranks | Selected targets | Context chars |", "|---|---|---|---|---:|",
    ]
    for qid, question in result.get("questions", {}).items():
        for condition, outcome in question.get("conditions", {}).items():
            selected = outcome["selected_context"]
            ranks = "; ".join(f"{key}={','.join(map(str, value)) or '—'}" for key, value in outcome["target_ranks"].items())
            picked = ",".join(key for key, yes in selected["target_in_selected"].items() if yes) or "—"
            lines.append(f"| {qid} | {condition} | {ranks} | {picked} | {selected['context_chars_used']}/8000 |")
    lines.extend(["", "## Factor separation", "", "The no-metadata/default-instruction and no-metadata/generic-direct-instruction arms are reused from `phase2-direct-evidence-rerank.json`; this run adds metadata/default and metadata/generic-direct on the same saved candidate pools. This yields the two-by-two comparison without rerunning the previously completed arms.", ""])
    for qid in QIDS:
        reference = result["question_integrity"]["factorial_reference"]
        for condition, key in (
            ("no_metadata_default_instruction", "no_metadata_default_instruction"),
            ("no_metadata_generic_direct_instruction", "no_metadata_generic_direct_instruction"),
        ):
            outcome = reference[key][qid]
            rank_values = outcome["target_ranks"]
            rank_text = "; ".join(
                f"{name}={','.join(str(item['rank']) for item in entries) or '—'}"
                for name, entries in rank_values.items()
            )
            selected = outcome["selected_context"]
            picked = ",".join(name for name, yes in selected["target_in_selected"].items() if yes) or "—"
            lines.append(f"- {qid} `{condition}`: {rank_text}; selected={picked}; {selected['context_chars_used']}/8000 chars")
    lines.extend(["", "## Limits", "", "Two supplemental stress questions do not establish general retrieval quality. The PDF mapping is specific to this source: level-1 roots plus numbered level-2 sections define the enclosing path; property-level and table-bookmark entries do not become section metadata. Table titles are emitted only when present on the candidate's own HTML source line or PDF physical page, so continuation pages do not inherit a title. Paired candidate records lacked declared content_sha256 values; candidate hashes are computed here and source replay is covered by `phase2-candidate-source-replay.md`.", ""])
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    _, qrows, pools, checks = _validate_inputs()
    sources, hashes = _read_verified_sources()
    html_map, html_stats = _html_structure(sources["raw_data/ogc-3dtiles/1.1/22-025r4.html"])
    pdf_map, pdf_stats = _pdf_structure(sources["raw_data/ogc-3dtiles/1.1/22-025r4.pdf"])
    structures = {"raw_data/ogc-3dtiles/1.1/22-025r4.html": html_map,
                  "raw_data/ogc-3dtiles/1.1/22-025r4.pdf": pdf_map}
    for qid, candidates in pools.items():
        for candidate in candidates:
            meta = _metadata(candidate, structures)
            if candidate["text"] not in _document(candidate, meta):
                raise AssertionError("source body changed in reranker input")
    fixture = _build_result_fixture(sources, hashes, html_map, pdf_map,
                                    {"html": html_stats, "pdf": pdf_stats},
                                    {"checks": checks, "controls": {**checks, "conditions": ["metadata_only_default_instruction", "metadata_plus_generic_direct_instruction"]}, **pools})
    result = {
        **fixture,
        "status": "cpu_fixture_completed",
        "started_unix": time.time(),
        "instructions": {},
        "model": None,
        "questions": {},
    }
    _save(result)
    if "--cpu-only" in sys.argv:
        _write_report(result)
        print(json.dumps({"status": result["status"], "source_hashes": hashes,
                          "structure_extraction": result["structure_extraction"],
                          "target_metadata": result["target_metadata"],
                          "fixture": OUTPUT.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=2))
        return

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch

    from wenjin.retrieval.rerank import DEFAULT_INSTRUCTION, Qwen3Reranker, rank_candidates
    from wenjin.settings import Settings

    direct_instruction = (
        "Given a technical documentation query, rank passages by how directly they support the "
        "requested claim for the stated entity and context. Prefer explicit definitions over "
        "related or similarly named material, and do not infer missing details from adjacent passages."
    )
    model_settings = Settings()
    reranker = Qwen3Reranker(model_settings.reranker_model_path,
                             device="cuda" if torch.cuda.is_available() else "cpu",
                             max_length=1024, batch_size=2, instruction=DEFAULT_INSTRUCTION)
    result.update({"status": "running", "instructions": {
        "metadata_only_default_instruction": DEFAULT_INSTRUCTION,
        "metadata_plus_generic_direct_instruction": direct_instruction,
    }, "model": {"name": model_settings.reranker_model_path.name, "device": reranker.device},
        "started_unix": time.time()})
    _save(result)
    try:
        for qid in QIDS:
            question = qrows[qid]["input"]["query"]
            qout = result["questions"].setdefault(qid, {"query": question, "conditions": {}})
            for condition, instruction in result["instructions"].items():
                if condition in qout["conditions"]:
                    continue
                reranker.instruction = instruction
                direct = condition == "metadata_plus_generic_direct_instruction"
                documents = [_document(c, _metadata(c, structures), direct=direct) for c in pools[qid]]
                content_budget = reranker.max_length - len(reranker.prefix_tokens) - len(reranker.suffix_tokens)
                tokenization_diagnostics = {}
                for target_name, selector in TARGETS[qid].items():
                    matching = next((candidate for candidate in pools[qid] if _candidate_match(candidate, selector)), None)
                    if matching is None:
                        tokenization_diagnostics[target_name] = {"present_in_candidate_pool": False}
                        continue
                    document = _document(matching, _metadata(matching, structures), direct=direct)
                    pair = reranker._format_pair(question, document)
                    token_ids = reranker.tokenizer.encode(pair, add_special_tokens=False)
                    if reranker.tokenizer.truncation_side == "right":
                        retained_ids = token_ids[:content_budget]
                    else:
                        retained_ids = token_ids[-content_budget:]
                    retained_text = reranker.tokenizer.decode(retained_ids, skip_special_tokens=False)
                    required_terms = [selector[3]]
                    if qid == QIDS[1] and target_name == "html":
                        required_terms = ["BATCH_LENGTH", "GlobalPropertyInteger", "all points"]
                    tokenization_diagnostics[target_name] = {
                        "present_in_candidate_pool": True,
                        "full_pair_tokens": len(token_ids),
                        "content_budget_tokens": content_budget,
                        "truncated": len(token_ids) > content_budget,
                        "required_terms_preserved": {term: term in retained_text for term in required_terms},
                        "section_metadata_preserved": all(term in retained_text for term in _metadata(matching, structures)["section_path"]),
                        "table_title_preserved": not _metadata(matching, structures)["table_title"] or _metadata(matching, structures)["table_title"] in retained_text,
                    }
                scores = reranker.score(question, documents)
                ranked = rank_candidates(pools[qid], scores)
                # 明确断言候选对象仍保留原正文与来源定位，结构metadata不进入输出证据。
                by_id = {item["chunk_id"]: item for item in pools[qid]}
                for scored in ranked:
                    original = by_id[scored["chunk_id"]]
                    for field in ("text", "source_path", "start_line", "end_line", "page_number"):
                        if scored.get(field) != original.get(field):
                            raise AssertionError(f"reranker altered candidate field {field}")
                qout["conditions"][condition] = {
                    "tokenization_diagnostics": tokenization_diagnostics,
                    "target_ranks": _rank_targets(ranked, qid),
                    "ranked_candidates": [
                        {"rank": rank, "chunk_id": item["chunk_id"], "source_path": item["source_path"],
                         "start_line": item.get("start_line"), "end_line": item.get("end_line"),
                         "page_number": item.get("page_number"), "text_sha256": _sha(item["text"]),
                         "rerank_score": item["rerank_score"]}
                        for rank, item in enumerate(ranked, 1)
                    ],
                    "selected_context": _selected(ranked, qid),
                }
                _save(result)
        result["status"] = "completed"
    except Exception as error:
        result["status"] = "failed"
        result["failure"] = {"type": type(error).__name__, "detail": str(error)}
        raise
    finally:
        reranker.close()
        result["finished_unix"] = time.time()
        _save(result)
        _write_report(result)
    print(f"JSON: {OUTPUT.relative_to(ROOT).as_posix()}")
    print(f"Report: {REPORT.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
