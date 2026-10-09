"""复用50题已保存排序，CPU重建源chunk并回放route-003的无metadata路径。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATASET = ROOT / "evaluation/datasets/rag-validation-001.jsonl"
DATASET_SHA_FILE = ROOT / "evaluation/datasets/rag-validation-001.sha256"
BASELINE_RESULT = ROOT / "artifacts/corpus-acceptance-002/validation-retrieval-eval.json"
ROUTE_002 = ROOT / "configs/indexes/multicorpus-candidates-002.json"
ROUTE_003 = ROOT / "configs/indexes/multicorpus-candidates-003.json"
OUTPUT = ROOT / "artifacts/phase1-source-replay-validation/phase2-50-cpu-equivalence.json"
REPORT = OUTPUT.with_suffix(".md")
EXPECTED_SHA = "70fc10faeb2480cc6984b4545b8c3a5ba6beb2de252da536cd09c5a32626328e"


def _load_fixed_dataset() -> tuple[list[dict[str, Any]], str]:
    # 路径白名单在read_bytes之前检查，避免任意题集进入本回放。
    if DATASET.resolve() != (ROOT / "evaluation/datasets/rag-validation-001.jsonl").resolve():
        raise ValueError("dataset path is outside the fixed allowlist")
    raw = DATASET.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != EXPECTED_SHA:
        raise ValueError("rag-validation-001 SHA256 mismatch")
    sidecar = DATASET_SHA_FILE.read_text(encoding="utf-8").split()[0].lower()
    if sidecar != EXPECTED_SHA:
        raise ValueError("dataset SHA sidecar mismatch")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(rows) != 50 or any(row.get("split") != "validation" for row in rows):
        raise ValueError("fixed dataset must contain exactly 50 validation questions")
    return rows, actual


def _routes() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    old = json.loads(ROUTE_002.read_text(encoding="utf-8"))
    new = json.loads(ROUTE_003.read_text(encoding="utf-8"))
    if old.get("route_id") != "multicorpus-candidates-002" or new.get("route_id") != "multicorpus-candidates-003":
        raise ValueError("route identity mismatch")
    old_by = {(x["library"], str(x["version"])): x for x in old["candidates"]}
    new_by = {(x["library"], str(x["version"])): x for x in new["candidates"]}
    old_non3d = {key: value for key, value in old_by.items() if key[0] != "ogc-3dtiles"}
    new_non3d = {key: value for key, value in new_by.items() if key[0] != "ogc-3dtiles"}
    if old_non3d != new_non3d or len(old_non3d) != 4:
        raise ValueError("route 003 changed one or more non-3D targets")
    return old, new, new_by


def _route_identity(row: dict[str, Any]) -> tuple[str, str]:
    library = {"osgEarth": "osgearth", "PROJ": "proj", "OGC WMS": "ogc-wms"}.get(
        row["library"], row["library"].lower()
    )
    version = row["version"]
    if library == "ogc-wms":
        version = str(version).split()[0]
    return library, str(version)


def _source_chunks(route_targets: dict[tuple[str, str], dict[str, Any]]) -> tuple[dict[str, dict], dict[str, Any]]:
    from wenjin.ingestion.chunking import block_chunks
    from wenjin.ingestion.multiformat import load_retrieval_documents

    chunks: dict[str, dict] = {}
    sources = {}
    for (library, version), target in route_targets.items():
        if library == "ogc-3dtiles":
            continue
        manifest = ROOT / target["manifest_path"]
        documents = load_retrieval_documents(manifest, ROOT / "raw_data")
        target_count = 0
        for document in documents:
            for chunk in block_chunks(document, max_chars=1600, split_oversized=True):
                row = {
                    "chunk_id": chunk.chunk_id,
                    "source_path": chunk.source_path,
                    "version": chunk.version,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "page_number": chunk.page_number,
                    "text": chunk.text,
                    "content_sha256": chunk.content_sha256,
                    "library": library,
                    "target_id": target["target_id"],
                }
                if chunk.chunk_id in chunks:
                    raise ValueError(f"duplicate source chunk id: {chunk.chunk_id}")
                chunks[chunk.chunk_id] = row
                target_count += 1
        if target_count != target["chunk_count"]:
            raise ValueError(f"{library}/{version}: reconstructed {target_count}, route expects {target['chunk_count']}")
        sources[f"{library}/{version}"] = {
            "manifest_path": target["manifest_path"],
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "expected_manifest_sha256": target["manifest_sha256"],
            "canonical_corpus_sha256": target["canonical_corpus_sha256"],
            "reconstructed_chunks": target_count,
            "all_sources_manifest_verified": True,
        }
        if sources[f"{library}/{version}"]["manifest_sha256"] != target["manifest_sha256"]:
            raise ValueError(f"{library}/{version}: route manifest SHA mismatch")
    return chunks, sources


def _main() -> None:
    rows, dataset_sha = _load_fixed_dataset()
    old, new, route_targets = _routes()
    saved = json.loads(BASELINE_RESULT.read_text(encoding="utf-8"))
    if saved.get("route_id") != old["route_id"] or saved.get("dataset") != DATASET.relative_to(ROOT).as_posix():
        raise ValueError("saved baseline result is not bound to route 002 and the fixed dataset")
    saved_rows = saved.get("details")
    if len(saved_rows) != 50:
        raise ValueError("expected 50 saved ranked question records")
    source_rows = {row["id"]: row for row in rows}
    result_rows = {row["id"]: row for row in saved_rows}
    if set(source_rows) != set(result_rows):
        raise ValueError("saved score IDs differ from fixed validation set")
    for qid, row in source_rows.items():
        old_result = result_rows[qid]
        route_library, route_version = _route_identity(row)
        if (old_result.get("query") != row.get("query")
                or old_result.get("dataset_library") != row.get("library")
                or str(old_result.get("version")) != route_version):
            raise ValueError(f"{qid}: cached route input differs from fixed dataset")
        if (route_library, route_version) not in route_targets:
            raise ValueError(f"{qid}: library/version is not in route 003")

    source_chunk_map, source_records = _source_chunks(route_targets)
    from wenjin.retrieval.rerank import candidate_document
    from wenjin.retrieval.service import select_context
    from wenjin.retrieval.source_context import (
        candidate_document_with_context,
        metadata_for_candidate,
    )

    details = []
    for qid, row in source_rows.items():
        saved_row = result_rows[qid]
        route_library, route_version = _route_identity(row)
        target = route_targets[(route_library, route_version)]
        ordered_ids = saved_row["top_ids"]["rerank"]
        if len(ordered_ids) != 5:
            raise ValueError(f"{qid}: saved reranked top-5 is incomplete")
        candidates = []
        input_hashes = []
        for chunk_id in ordered_ids:
            candidate = source_chunk_map.get(chunk_id)
            if candidate is None or candidate["target_id"] != target["target_id"]:
                raise ValueError(f"{qid}: saved candidate does not resolve under unchanged target: {chunk_id}")
            base = candidate_document(candidate)
            metadata = metadata_for_candidate(candidate, {})
            augmented = candidate_document_with_context(candidate, metadata)
            if augmented.encode("utf-8") != base.encode("utf-8"):
                raise AssertionError("unknown library/profile must produce byte-identical reranker document")
            candidate = {**candidate, "rerank_score": 0.0}
            candidates.append(candidate)
            input_hashes.append(hashlib.sha256(base.encode("utf-8")).hexdigest())
        selected = select_context(candidates, top_k=5, char_budget=8000)
        details.append({
            "id": qid,
            "library": route_library,
            "version": route_version,
            "target_id": target["target_id"],
            "saved_ranked_ids_sha256": hashlib.sha256("\n".join(ordered_ids).encode()).hexdigest(),
            "ranked_top5_reranker_document_sha256": input_hashes,
            "unknown_source_metadata_status": [metadata_for_candidate(c, {}).get("metadata_status") for c in candidates],
            "selected_ids": [item["chunk_id"] for item in selected],
            "selected_chars": sum(len(item["text"]) for item in selected),
            "selected_count": len(selected),
            "selector_replay_scope": "cached reranked top-5 only; original score/order reused, no inference",
        })

    output = {
        "status": "completed",
        "scope": "50 validation questions; CPU-only cached ranking/selector replay",
        "dataset": {"path": DATASET.relative_to(ROOT).as_posix(), "sha256": dataset_sha,
                    "count": len(rows), "split": "validation"},
        "cached_scores": {"path": BASELINE_RESULT.relative_to(ROOT).as_posix(),
                          "route_id": old["route_id"], "reranker_inference_reused": True,
                          "new_model_inference": False},
        "routes": {"route_002": old["route_id"], "route_003": new["route_id"],
                   "four_non3d_targets_identical": True,
                   "changed_target_is_out_of_dataset_scope": "ogc-3dtiles"},
        "source_reconstruction": source_records,
        "question_details": details,
        "summary": {
            "questions": len(details),
            "all_top5_reranker_documents_byte_identical_without_metadata": True,
            "selected_rows": sum(row["selected_count"] for row in details),
            "selector_overflow_rejected": sum(row["selected_chars"] > 8000 for row in details),
            "selector_items_over_five": sum(row["selected_count"] > 5 for row in details),
        },
        "limitations": [
            "Saved 50-question artifact stores the final reranked top-5 IDs, not all 20 scorer documents or scores. This replay reconstructs and verifies only those five source documents per question.",
            "Selector is replayed over cached top-5 order, so this does not evaluate backfill from ranks 6–20.",
            "No reranker inference is rerun; route equivalence is established for the four unchanged non-3D targets.",
        ],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = [
        "# 50-question CPU route-equivalence and selector replay", "",
        f"Dataset SHA-256 `{dataset_sha}` passed the fixed allowlist and validation-split gate. The route-003 change only replaces the 3D Tiles target; all four targets used by these questions are field-identical to route 002. The cached route-002 ranking was reused, with no new model inference.", "",
        f"Manifest-verified chunk reconstruction resolved every saved top-5 ID. For {len(details)} questions, the 5 reranker document strings per question were byte-identical when passed through the unknown-profile metadata helper. The CPU selector selected {output['summary']['selected_rows']} total chunks, all within 8,000 characters and at most five per question.", "",
        "This is not a new 50-question model run: the saved artifact contains top-5 IDs only, so this replay does not claim to verify all 20 documents that entered the old scorer or to evaluate ranks 6–20 backfill.", "",
        f"Detailed input hashes, selected IDs, and route target identities: `{OUTPUT.relative_to(ROOT).as_posix()}`.",
    ]
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
