"""五库开发题的paired fresh retrieval验证；只返回证据，不生成答案。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_ROUTE = ROOT / "configs/indexes/multicorpus-candidates-003.json"
DEFAULT_DATASET = ROOT / "evaluation/datasets/developer-validation-2026-10-04.jsonl"
DEFAULT_OUTPUT = ROOT / "artifacts/iterations/2026-10-04-retrieval-validation"
CONTROLS = {
    "candidate_k": 20,
    "rrf_k": 60,
    "rerank_pool": 30,
    "top_k": 5,
    "context_char_budget": 8000,
    "reranker_max_length": 1024,
    "reranker_batch_size": 2,
}


class IterationPhaseError(RuntimeError):
    """保留发生错误的实验阶段供attempt日志定位。"""

    def __init__(self, phase: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.phase = phase
        self.cause = cause


def canonical_json_bytes(value: Any) -> bytes:
    """用固定JSON编码计算记录身份，避免空格或字典顺序影响checkpoint。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def checkpoint_sha256(record: dict[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def build_case_identity(
    row: dict[str, Any], *, dataset_sha256: str, route_sha256: str,
    controls: dict[str, Any], model: dict[str, Any], runtime_fingerprint: dict[str, Any] | None = None,
) -> str:
    identity = {
        "dataset_sha256": dataset_sha256,
        "dataset_revision": row.get("dataset_revision"),
        "lock_state": row.get("lock_state"),
        "case_id": row["case_id"],
        "query": row["query"],
        "route_sha256": route_sha256,
        "controls": controls,
        "model": model,
        "runtime_fingerprint": runtime_fingerprint or {},
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def write_case_checkpoint(path: Path, record: dict[str, Any]) -> None:
    """先写同目录临时文件，再原子替换，避免中断留下半份成功记录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = dict(record)
    stored["record_sha256"] = checkpoint_sha256(stored)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(stored) + b"\n")
    temporary.replace(path)


def load_case_checkpoint(path: Path, *, expected_identity: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("case_identity") != expected_identity:
        raise ValueError(f"checkpoint identity mismatch: {path.name}")
    expected_sha = checkpoint_sha256(record)
    if record.get("record_sha256") != expected_sha:
        raise ValueError(f"checkpoint record hash mismatch: {path.name}")
    return record


def _span_locator_result(candidates: list[dict[str, Any]], span: dict[str, Any]) -> dict[str, Any]:
    """用区间并集检查完整定位覆盖；只有相交时单列partial。"""
    matching = [item for item in candidates if item.get("source_path") == span.get("path")]
    locator = span.get("locator") or {}
    page = locator.get("page")
    if page is not None:
        matching = [item for item in matching if item.get("page_number") == page]
        page_start = locator.get("page_start_line")
        page_end = locator.get("page_end_line")
        if page_start is None or page_end is None:
            return {"covered": bool(matching), "partial": False, "granularity": "physical_page"}
        start, end, granularity = page_start, page_end, "physical_page_and_page_local_lines"
    else:
        start, end, granularity = locator.get("start_line"), locator.get("end_line"), "source_line_range"
    if start is None or end is None:
        return {"covered": False, "partial": False, "granularity": granularity}
    intervals = sorted(
        (int(item["start_line"]), int(item["end_line"]))
        for item in matching
        if item.get("start_line") is not None and item.get("end_line") is not None
    )
    cursor = int(start)
    touched = False
    for item_start, item_end in intervals:
        if item_end < cursor:
            continue
        if item_start > cursor:
            break
        touched = True
        cursor = max(cursor, item_end + 1)
        if cursor > int(end):
            return {"covered": True, "partial": False, "granularity": granularity}
    partial = touched or any(
        item.get("start_line") is not None and item.get("end_line") is not None
        and item["start_line"] <= int(end) and item["end_line"] >= int(start)
        for item in matching
    )
    return {"covered": False, "partial": partial, "granularity": granularity}


def locator_coverage_proxy(candidates: list[dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
    """按来源locator做确定性覆盖proxy；不读取query判断语义，也不参与选择。"""
    groups = row.get("source_groups", [])
    group_results = []
    by_group: dict[str, bool] = {}
    for index, group in enumerate(groups):
        group_id = str(group.get("source_group_id", f"group-{index + 1}"))
        spans = group.get("source_spans", [])
        outcomes = [_span_locator_result(candidates, span) for span in spans]
        covered = [item["covered"] for item in outcomes]
        full = bool(spans) and all(covered)
        by_group[group_id] = full
        group_results.append({
            "group_id": group_id,
            "span_total": len(spans),
            "span_locator_proxy_covered": sum(covered),
            "span_locator_proxy_partial": sum(item["partial"] for item in outcomes),
            "locator_granularity": sorted({item["granularity"] for item in outcomes}),
            "all_spans_locator_proxy_covered": full,
        })
    fact_results = []
    facts = row.get("reference_facts", [])
    for fact in facts:
        group_ids = fact.get("supporting_source_group_ids", [])
        covered = any(by_group.get(str(group_id), False) for group_id in group_ids)
        fact_results.append({
            "fact_id": fact.get("fact_id"),
            "supporting_source_group_ids": group_ids,
            "locator_proxy_covered": covered,
        })
    return {
        "kind": "deterministic_locator_proxy_not_semantic_judgment",
        "source_groups_total": len(group_results),
        "source_groups_locator_proxy_covered": sum(item["all_spans_locator_proxy_covered"] for item in group_results),
        "reference_facts_total": len(fact_results),
        "reference_facts_locator_proxy_covered": sum(item["locator_proxy_covered"] for item in fact_results),
        "source_groups": group_results,
        "reference_facts": fact_results,
    }


def _span_presence_in_chunks(index: Any, span: dict[str, Any]) -> tuple[bool, bool]:
    """严格核对题目source身份、production path与BM25中的locator保留范围。"""
    try:
        source_record = index.sources.match_chunk_path(str(span.get("path", "")))
    except (KeyError, TypeError, ValueError, OSError):
        return False, False
    manifest_relative = span.get("manifest_relative_path")
    if not manifest_relative or source_record.relative != manifest_relative:
        return False, False
    manifest_path = ROOT / index.target.manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    entry = next((item for item in manifest.get("files", [])
                  if item.get("path") == manifest_relative), None)
    if entry is None:
        return False, False
    expected_source_id = f"{manifest.get('corpus_id')}:{manifest_relative}"
    if span.get("source_id") != expected_source_id:
        return False, False
    if span.get("source_sha256") != entry.get("raw_sha256"):
        return False, False
    locator = span.get("locator") or {}
    with sqlite3.connect(f"file:{index.bm25_path.resolve().as_posix()}?mode=ro", uri=True) as db:
        paths = db.execute(
            "SELECT DISTINCT source_path FROM chunks WHERE source_path=? AND source_raw_sha256=?",
            (span["path"], span["source_sha256"]),
        ).fetchall()
        if len(paths) != 1 or paths[0][0] != span.get("path"):
            return False, False
        if locator.get("page") is not None:
            rows = db.execute(
                "SELECT page_number,start_line,end_line FROM chunks WHERE source_path=? AND source_raw_sha256=? AND page_number=?",
                (span["path"], span["source_sha256"], locator["page"]),
            ).fetchall()
            start, end = locator.get("page_start_line"), locator.get("page_end_line")
        else:
            rows = db.execute(
                "SELECT page_number,start_line,end_line FROM chunks WHERE source_path=? AND source_raw_sha256=?",
                (span["path"], span["source_sha256"]),
            ).fetchall()
            start, end = locator.get("start_line"), locator.get("end_line")
    if start is None or end is None:
        return True, bool(rows)
    intervals = [{"source_path": span["path"], "page_number": page,
                  "start_line": first, "end_line": last} for page, first, last in rows]
    return True, _span_locator_result(intervals, span)["covered"]


def classify_locator_failure(
    row: dict[str, Any], *, source_presence: list[list[tuple[bool, bool]]],
    stage_proxies: dict[str, dict[str, Any]],
) -> str | None:
    """定位proxy首次丢失阶段；不会将结果解释成语义失败或拒答能力。"""
    groups = row.get("source_groups", [])
    if not groups:
        return None
    group_source_available = [all(present for present, _ in group) for group in source_presence]
    if not any(group_source_available):
        return "source_absent"
    if not any(available and all(retained for _, retained in source_presence[i])
               for i, available in enumerate(group_source_available)):
        return "chunk_retention"
    if not stage_proxies["fused"].get("source_groups_locator_proxy_covered", 0):
        return "retrieval"
    if not stage_proxies["ranked"].get("source_groups_locator_proxy_covered", 0):
        return "rerank"
    if not stage_proxies["selected"].get("source_groups_locator_proxy_covered", 0):
        return "budget"
    return None


def _candidate_record(candidate: dict[str, Any], *, stage_rank: int) -> dict[str, Any]:
    text = candidate.get("text", "")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {
        "stage_rank": stage_rank,
        "chunk_id": candidate.get("chunk_id"),
        "library": candidate.get("library"),
        "version": candidate.get("version"),
        "target_id": candidate.get("target_id"),
        "source_path": candidate.get("source_path"),
        "logical_document_id": candidate.get("logical_document_id"),
        "locator": {
            "start_line": candidate.get("start_line"),
            "end_line": candidate.get("end_line"),
            "page_number": candidate.get("page_number"),
            "page_start_line": candidate.get("page_start_line", candidate.get("start_line") if candidate.get("page_number") is not None else None),
            "page_end_line": candidate.get("page_end_line", candidate.get("end_line") if candidate.get("page_number") is not None else None),
        },
        "score": candidate.get("score"),
        "rrf_score": candidate.get("rrf_score"),
        "rerank_score": candidate.get("rerank_score"),
        "pre_rerank_rank": candidate.get("pre_rerank_rank"),
        "content_sha256": content_hash,
        "declared_content_sha256": candidate.get("content_sha256"),
        "content_hash_matches_declared": candidate.get("content_sha256") == content_hash,
        "source_raw_sha256": candidate.get("source_raw_sha256"),
        "source_canonical_sha256": candidate.get("source_canonical_sha256"),
        "text": text,
    }


def _stage_record(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_candidate_record(candidate, stage_rank=index) for index, candidate in enumerate(candidates, 1)]


def paired_case(
    router: Any, row: dict[str, Any], *, reranker: Any,
    config: Any, case_identity: str,
) -> dict[str, Any]:
    """一次fresh production retrieval产出两臂，共用完全相同dense/BM25/RRF候选。"""
    started = time.perf_counter()
    try:
        retrieval = router.retrieve(
            row["query"], library=row["library"], version=row["version"], config=config,
        )
    except Exception as error:
        raise IterationPhaseError("retrieval", error) from error
    target_indices = {index.target_id: index for index in router.indices}
    fused = retrieval["fused_candidates"]
    frame_started = time.perf_counter()
    frames, frame_records = [], []
    from wenjin.retrieval.rerank import candidate_document, rank_candidates
    from wenjin.retrieval.router import normalize_library
    try:
        for candidate in fused:
            index = target_indices[candidate["target_id"]]
            if index.source_context_cache is None:
                frame, context_status = candidate_document(candidate), "unknown_profile_baseline_frame"
            else:
                frame, context_status = index.source_context_cache.document_for_candidate(candidate)
            frames.append(frame)
            frame_records.append({
                "chunk_id": candidate["chunk_id"],
                "target_id": candidate["target_id"],
                "frame_sha256": hashlib.sha256(frame.encode("utf-8")).hexdigest(),
                "frame_chars": len(frame),
                "context_status": context_status,
                "frame": frame,
            })
    except Exception as error:
        raise IterationPhaseError("source_context_frame", error) from error
    frame_build_seconds = time.perf_counter() - frame_started
    model_rerank_started = time.perf_counter()
    try:
        rerank_scores = reranker.score(row["query"], frames) if frames else []
        if len(rerank_scores) != len(fused):
            raise ValueError("reranker score count differs from fused candidate count")
        if any(not math.isfinite(float(score)) for score in rerank_scores):
            raise ValueError("reranker returned a non-finite score")
        ranked = rank_candidates(fused, rerank_scores) if fused else []
    except Exception as error:
        raise IterationPhaseError("rerank", error) from error
    model_rerank_seconds = time.perf_counter() - model_rerank_started
    from wenjin.retrieval.service import select_context
    try:
        baseline_selected = select_context(fused, top_k=config.top_k, char_budget=config.context_char_budget)
        ranked_selected = select_context(ranked, top_k=config.top_k, char_budget=config.context_char_budget)
        for selected in (baseline_selected, ranked_selected):
            used_chars = sum(len(item.get("text", "")) for item in selected)
            if len(selected) > config.top_k or used_chars > config.context_char_budget:
                raise ValueError("production selector output exceeds top_k/context_char_budget")
    except Exception as error:
        raise IterationPhaseError("budget_selection", error) from error

    stages = {
        "dense": _stage_record(retrieval["dense_candidates"]),
        "bm25": _stage_record(retrieval["bm25_candidates"]),
        "fused": _stage_record(fused),
        "ranked": _stage_record(ranked),
        "baseline_selected": _stage_record(baseline_selected),
        "selected": _stage_record(ranked_selected),
    }
    source_presence = []
    source_diagnostics = []
    for group in row.get("source_groups", []):
        spans_status = []
        spans_details = []
        for span in group.get("source_spans", []):
            matching = [idx for idx in router.indices
                        if idx.library == normalize_library(str(span.get("library", "")))
                        and idx.version == span.get("version")]
            if matching:
                status = _span_presence_in_chunks(matching[0], span)
            else:
                status = (False, False)
            spans_status.append(status)
            spans_details.append({
                "source_group_id": group.get("source_group_id"),
                "source_id": span.get("source_id"),
                "source_path": span.get("path"),
                "manifest_relative_path": span.get("manifest_relative_path"),
                "manifest_source_present_and_identity_valid": status[0],
                "full_locator_retained_in_bm25": status[1],
                "source_sha256": span.get("source_sha256"),
                "locator": span.get("locator"),
            })
        source_presence.append(spans_status)
        source_diagnostics.extend(spans_details)
    proxies = {
        name: locator_coverage_proxy(candidates, row)
        for name, candidates in (
            ("dense", retrieval["dense_candidates"]),
            ("bm25", retrieval["bm25_candidates"]),
            ("fused", fused),
            ("ranked", ranked[:config.top_k]),
            ("baseline_selected", baseline_selected),
            ("selected", ranked_selected),
        )
    }
    failure = classify_locator_failure(row, source_presence=source_presence, stage_proxies=proxies)
    negative = row.get("expected_status") == "unsupported"
    dense_ids = [item.get("chunk_id") for item in retrieval["dense_candidates"]]
    bm25_ids = [item.get("chunk_id") for item in retrieval["bm25_candidates"]]
    pool_fingerprint = hashlib.sha256(canonical_json_bytes([
        {"target_id": item.get("target_id"), "chunk_id": item.get("chunk_id"),
         "content_sha256": item.get("content_sha256")}
        for item in fused
    ])).hexdigest()
    return {
        "schema_version": 1,
        "case_identity": case_identity,
        "case_id": row["case_id"],
        "query": row["query"],
        "library": row["library"],
        "version": row["version"],
        "query_family": row.get("query_family"),
        "expected_status": row.get("expected_status"),
        "service_behavior": row.get("service_behavior"),
        "no_answer_behavior": "not_evaluated" if negative else "not_generated",
        "refusal_status": "not_evaluated",
        "retrieval": {
            "routed_libraries": retrieval["routed_libraries"],
            "bm25_terms": retrieval["bm25_terms"],
            "per_library": retrieval["per_library"],
            "dense_bm25_fused_candidate_ids_identical_across_arms": True,
            "dense_candidate_ids": dense_ids,
            "bm25_candidate_ids": bm25_ids,
            "shared_fused_pool_sha256": pool_fingerprint,
            "baseline_arm_pool_sha256": pool_fingerprint,
            "rerank_arm_pool_sha256": pool_fingerprint,
        },
        "controls": {
            "candidate_k": config.candidate_k,
            "rrf_k": config.rrf_k,
            "rerank_pool": config.rerank_pool,
            "top_k": config.top_k,
            "context_char_budget": config.context_char_budget,
        },
        "stages": stages,
        "rerank_model_inputs": frame_records,
        "coverage_locator_proxy": proxies,
        "source_presence_and_chunk_retention": source_diagnostics,
        "diagnosis": {
            "locator_failure_stage": failure,
            "source_groups_expected": len(row.get("source_groups", [])),
            "reference_facts_expected": len(row.get("reference_facts", [])),
            "semantic_review": "not_performed",
        },
        "timing_seconds": {
            **retrieval["timing_seconds"],
            "source_context_frame_build": round(frame_build_seconds, 6),
            "reranker_inference": round(model_rerank_seconds, 6),
            "paired_case_total": round(time.perf_counter() - started, 6),
        },
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files.append({"relative_path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
                      "sha256": _sha256_file(path)})
    return {"file_count": len(files), "files": files,
            "sha256": hashlib.sha256(canonical_json_bytes(files)).hexdigest()}


def _qdrant_collection_fingerprint(client: Any, collection: str) -> dict[str, Any]:
    info = client.get_collection(collection)
    point_hashes = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=[
                "chunk_id", "source_path", "logical_document_id", "content_sha256",
                "source_raw_sha256", "source_canonical_sha256", "start_line", "end_line", "page_number",
                "text",
            ],
            with_vectors=True,
        )
        for point in points:
            payload = point.payload or {}
            vector = point.vector
            if isinstance(vector, dict):
                vector_hashes = {name: hashlib.sha256(canonical_json_bytes(values)).hexdigest()
                                 for name, values in sorted(vector.items())}
            else:
                vector_hashes = hashlib.sha256(canonical_json_bytes(vector)).hexdigest()
            compact = {
                "point_id": str(point.id),
                "chunk_id": payload.get("chunk_id"),
                "source_path": payload.get("source_path"),
                "logical_document_id": payload.get("logical_document_id"),
                "content_sha256": payload.get("content_sha256"),
                "body_sha256": hashlib.sha256(str(payload.get("text", "")).encode("utf-8")).hexdigest(),
                "source_raw_sha256": payload.get("source_raw_sha256"),
                "source_canonical_sha256": payload.get("source_canonical_sha256"),
                "locator": {key: payload.get(key) for key in ("start_line", "end_line", "page_number")},
                "vector_sha256": vector_hashes,
            }
            point_hashes.append(hashlib.sha256(canonical_json_bytes(compact)).hexdigest())
        if offset is None:
            break
    point_hashes.sort()
    return {
        "status": str(info.status),
        "points_count": info.points_count,
        "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
        "scanned_point_count": len(point_hashes),
        "point_payload_and_vector_sha256": hashlib.sha256(canonical_json_bytes(point_hashes)).hexdigest(),
    }


def _asset_fingerprint(route_path: Path, route: dict[str, Any], qdrant_client: Any | None = None) -> dict[str, Any]:
    assets = {"route_sha256": _sha256_file(route_path), "targets": []}
    for item in route["candidates"]:
        bm25 = ROOT / item["bm25_path"]
        manifest = ROOT / item["manifest_path"]
        manifest_data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        raw_files = []
        raw_base = ROOT / "raw_data" / str(manifest_data.get("raw_data_subdir", ""))
        for source in manifest_data.get("files", []):
            source_path = raw_base / str(source.get("path", ""))
            raw_files.append({
                "path": str(source.get("path", "")),
                "exists": source_path.is_file(),
                "sha256": _sha256_file(source_path) if source_path.is_file() else None,
                "manifest_raw_sha256": source.get("raw_sha256"),
                "manifest_canonical_sha256": source.get("canonical_sha256"),
            })
        assets["targets"].append({
            "target_id": item["target_id"], "collection": item["dense_collection"],
            "bm25_sha256": _sha256_file(bm25), "manifest_sha256": _sha256_file(manifest),
            "raw_manifest_declared_sha256": item["manifest_sha256"],
            "bm25_size": bm25.stat().st_size, "manifest_size": manifest.stat().st_size,
            "source_files": raw_files,
            "dense_collection_fingerprint": (
                _qdrant_collection_fingerprint(qdrant_client, item["dense_collection"])
                if qdrant_client is not None else None
            ),
        })
    registry_path = ROOT / "data/indexes/index-registry.json"
    assets["active_registry"] = {
        "exists": registry_path.is_file(),
        "sha256": _sha256_file(registry_path) if registry_path.is_file() else None,
    }
    return assets


def _asset_fingerprint_errors(assets: dict[str, Any]) -> list[str]:
    errors = []
    for target in assets["targets"]:
        if target["manifest_sha256"] != target["raw_manifest_declared_sha256"]:
            errors.append(f"{target['target_id']}: manifest sha differs from route")
        for source in target["source_files"]:
            if not source["exists"]:
                errors.append(f"{target['target_id']}: source missing ({source['path']})")
            elif source["manifest_raw_sha256"] and source["sha256"] != source["manifest_raw_sha256"]:
                errors.append(f"{target['target_id']}: raw source sha mismatch ({source['path']})")
    return errors


def _code_fingerprint() -> dict[str, str]:
    paths = [
        ROOT / "evaluation/experiments/run_iteration_retrieval_validation.py",
        ROOT / "evaluation/quality/run_multicorpus_eval.py",
        ROOT / "src/wenjin/retrieval/router.py",
        ROOT / "src/wenjin/retrieval/source_context.py",
        ROOT / "src/wenjin/retrieval/rerank.py",
        ROOT / "src/wenjin/retrieval/service.py",
        ROOT / "src/wenjin/retrieval/fusion.py",
    ]
    return {path.relative_to(ROOT).as_posix(): _sha256_file(path) for path in paths if path.is_file()}


def _validate_locked_dataset(dataset_path: Path, rows: list[dict[str, Any]], route: dict[str, Any]) -> str:
    """锁题前只读校验sidecar、路由目标、source身份和locator结构。"""
    sidecar = dataset_path.with_suffix(".sha256")
    if not sidecar.is_file():
        raise ValueError(f"dataset sha256 sidecar missing: {sidecar.name}")
    parts = sidecar.read_text(encoding="utf-8").strip().split()
    actual_sha = _sha256_file(dataset_path)
    if len(parts) < 2 or parts[0].lower() != actual_sha or parts[1] != dataset_path.name:
        raise ValueError("dataset sha256 sidecar does not match the dataset bytes/name")
    def normalize(value: Any) -> str:
        return re.sub(r"[\s_]+", "-", str(value).strip().lower())

    targets = {(normalize(item["library"]), str(item["version"])): item
               for item in route["candidates"]}
    case_ids: set[str] = set()
    queries: set[str] = set()
    for row in rows:
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", case_id):
            raise ValueError(f"unsafe or missing case_id: {case_id!r}")
        if case_id in case_ids:
            raise ValueError(f"duplicate case_id: {case_id}")
        case_ids.add(case_id)
        if row.get("lock_state") != "locked":
            raise ValueError(f"case is not locked: {case_id}")
        query = row.get("query")
        if not isinstance(query, str) or not query.strip() or query.strip() in queries:
            raise ValueError(f"empty or duplicate query in case {case_id}")
        queries.add(query.strip())
        if row.get("dataset_revision") != dataset_path.stem:
            raise ValueError(f"dataset_revision mismatch in case {case_id}")
        if row.get("expected_status") not in {"supported", "unsupported"}:
            raise ValueError(f"invalid expected_status in case {case_id}")
        if row.get("dataset_type") != "developer_validation":
            raise ValueError(f"dataset_type must be developer_validation in case {case_id}")
        valid_case_types = {"positive"} if row["expected_status"] == "supported" else {"negative", "negative_scope"}
        if row.get("case_type") not in valid_case_types:
            raise ValueError(f"case_type/expected_status mismatch in case {case_id}")
        if not isinstance(row.get("query_family"), str) or not row["query_family"].strip():
            raise ValueError(f"missing query_family in case {case_id}")
        if row.get("service_behavior") != "not_generated":
            raise ValueError(f"retrieval dataset must declare not_generated in case {case_id}")
        route_key = (normalize(row.get("library", "")), str(row.get("version", "")))
        if route_key not in targets:
            raise ValueError(f"library/version is not routed in case {case_id}: {route_key}")
        target = targets[route_key]
        if row.get("source_manifest_sha256") != target.get("manifest_sha256"):
            raise ValueError(f"source manifest fingerprint differs from route in {case_id}")
        manifest_path = ROOT / target["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        files = {entry.get("path"): entry for entry in manifest.get("files", [])}
        groups = row.get("source_groups", [])
        group_ids = [group.get("source_group_id") for group in groups]
        if any(not isinstance(item, str) or not item for item in group_ids) or len(set(group_ids)) != len(group_ids):
            raise ValueError(f"invalid or duplicate source_group_id in case {case_id}")
        if row["expected_status"] == "supported" and not groups:
            raise ValueError(f"supported case lacks source groups: {case_id}")
        if row["expected_status"] == "unsupported" and (groups or row.get("reference_facts")):
            raise ValueError(f"unsupported case must not carry gold support spans/facts: {case_id}")
        for group in groups:
            spans = group.get("source_spans", [])
            if not spans:
                raise ValueError(f"empty source group in case {case_id}")
            for span in spans:
                rel = span.get("manifest_relative_path")
                entry = files.get(rel)
                expected_path = f"raw_data/{manifest.get('raw_data_subdir', '').strip('/')}/{rel}"
                if (not entry or span.get("path") != expected_path
                        or span.get("source_id") != f"{manifest.get('corpus_id')}:{rel}"
                        or span.get("source_sha256") != entry.get("raw_sha256")
                        or span.get("parser") != entry.get("parser")
                        or normalize(span.get("library", "")) != route_key[0]
                        or str(span.get("version")) != route_key[1]):
                    raise ValueError(f"source identity does not resolve to selected route in {case_id}")
                locator = span.get("locator") or {}
                if locator.get("page") is not None:
                    if (locator.get("start_line") is not None or locator.get("end_line") is not None
                            or not isinstance(locator.get("page"), int) or locator["page"] < 1
                            or not isinstance(locator.get("page_start_line"), int)
                            or not isinstance(locator.get("page_end_line"), int)
                            or locator["page_start_line"] < 1
                            or locator["page_end_line"] < locator["page_start_line"]):
                        raise ValueError(f"invalid PDF page-local locator in {case_id}")
                elif (not isinstance(locator.get("start_line"), int)
                      or not isinstance(locator.get("end_line"), int)
                      or locator["start_line"] < 1 or locator["end_line"] < locator["start_line"]
                      or locator.get("page_start_line") is not None
                      or locator.get("page_end_line") is not None):
                    raise ValueError(f"invalid source line locator in {case_id}")
        facts = row.get("reference_facts", [])
        fact_ids = [fact.get("fact_id") for fact in facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError(f"duplicate fact_id in case {case_id}")
        for fact in facts:
            if not fact.get("text", "").strip() or not fact.get("supporting_source_group_ids"):
                raise ValueError(f"reference fact lacks text/source group in {case_id}")
            if not set(fact["supporting_source_group_ids"]).issubset(group_ids):
                raise ValueError(f"reference fact references unknown source group in {case_id}")
        if row["expected_status"] == "supported" and not facts:
            raise ValueError(f"supported case lacks reference facts: {case_id}")
    return actual_sha


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _mark_interrupted_attempts(path: Path) -> list[dict[str, Any]]:
    """把上次进程退出前仍running的case写成明确的interrupted attempt。"""
    if not path.is_file():
        return []
    states: dict[tuple[str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        key = (str(event.get("case_id")), str(event.get("case_identity")))
        if event.get("status") == "running":
            states[key] = event
        elif event.get("status") in {"completed", "failed", "interrupted"}:
            states.pop(key, None)
    interrupted = []
    for (_, _), event in states.items():
        record = {
            "case_id": event["case_id"], "case_identity": event["case_identity"],
            "status": "interrupted", "phase": event.get("phase", "unknown"),
            "error_class": "ProcessInterrupted",
            "error_message": "previous attempt has no terminal checkpoint event",
            "finished_at": time.time(),
        }
        _append_jsonl(path, record)
        interrupted.append(record)
    return interrupted


def _attempt_failures(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        {key: event.get(key) for key in ("case_id", "case_identity", "status", "phase", "error_class", "error_message")}
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        for event in [json.loads(line)] if event.get("status") in {"failed", "interrupted"}
    ]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _summary(records: list[dict[str, Any]], rows: list[dict[str, Any]],
             runtime_failures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    diagnoses = Counter(record["diagnosis"]["locator_failure_stage"] or "locator_proxy_covered" for record in records)
    by_library: dict[str, dict[str, Any]] = {}
    by_family: dict[str, dict[str, Any]] = {}
    by_library_family: dict[str, dict[str, Any]] = {}
    planned_group_count = sum(len(row.get("source_groups", [])) for row in rows)
    planned_fact_count = sum(len(row.get("reference_facts", [])) for row in rows)
    overall_proxy = {
        stage: {
            "source_groups_numerator": 0, "source_groups_denominator": 0,
            "reference_facts_numerator": 0, "reference_facts_denominator": 0,
            "planned_source_groups_denominator": planned_group_count,
            "planned_reference_facts_denominator": planned_fact_count,
        }
        for stage in ("dense", "bm25", "fused", "ranked", "baseline_selected", "selected")
    }

    def bucket() -> dict[str, Any]:
        return {
            "cases": 0, "completed_cases": 0, "unsupported_cases": 0, "supported_cases": 0,
            "source_groups_denominator": 0, "reference_facts_denominator": 0,
            "completed_source_groups": 0, "completed_reference_facts": 0,
            "locator_proxy_by_arm": {
                stage: {"source_groups_numerator": 0, "source_groups_denominator": 0,
                        "reference_facts_numerator": 0, "reference_facts_denominator": 0,
                        "planned_source_groups_denominator": 0,
                        "planned_reference_facts_denominator": 0}
                for stage in ("dense", "bm25", "fused", "ranked", "baseline_selected", "selected")
            },
        }

    def keys_for(lib: str, family: str):
        return ((by_library, lib), (by_family, family), (by_library_family, f"{lib}::{family}"))

    # 样本与gold分母以锁定题集为准，即使个别运行失败也不会消失。
    for row in rows:
        lib, family = row["library"], str(row.get("query_family") or "unclassified")
        keys = keys_for(lib, family)
        groups_total = len(row.get("source_groups", []))
        facts_total = len(row.get("reference_facts", []))
        for destination, key in keys:
            destination.setdefault(key, bucket())
            item = destination[key]
            item["cases"] += 1
            item["unsupported_cases"] += row.get("expected_status") == "unsupported"
            item["supported_cases"] += row.get("expected_status") == "supported"
            item["source_groups_denominator"] += groups_total
            item["reference_facts_denominator"] += facts_total
            for arm in item["locator_proxy_by_arm"].values():
                arm["planned_source_groups_denominator"] += groups_total
                arm["planned_reference_facts_denominator"] += facts_total

    for record in records:
        lib, family = record["library"], str(record.get("query_family") or "unclassified")
        keys = keys_for(lib, family)
        for destination, key in keys:
            item = destination[key]
            item["completed_cases"] += 1
            item["completed_source_groups"] += record["diagnosis"]["source_groups_expected"]
            item["completed_reference_facts"] += record["diagnosis"]["reference_facts_expected"]
        for destination, key in keys:
            item = destination[key]
            for stage, proxy in record["coverage_locator_proxy"].items():
                arm = item["locator_proxy_by_arm"][stage]
                arm["source_groups_numerator"] += proxy["source_groups_locator_proxy_covered"]
                arm["source_groups_denominator"] += proxy["source_groups_total"]
                arm["reference_facts_numerator"] += proxy["reference_facts_locator_proxy_covered"]
                arm["reference_facts_denominator"] += proxy["reference_facts_total"]
        for stage, proxy in record["coverage_locator_proxy"].items():
            overall_proxy[stage]["source_groups_numerator"] += proxy["source_groups_locator_proxy_covered"]
            overall_proxy[stage]["source_groups_denominator"] += proxy["source_groups_total"]
            overall_proxy[stage]["reference_facts_numerator"] += proxy["reference_facts_locator_proxy_covered"]
            overall_proxy[stage]["reference_facts_denominator"] += proxy["reference_facts_total"]
    return {
        "schema_version": 1,
        "cases_total": len(rows),
        "cases_completed": len(records),
        "source_groups_total": sum(len(row.get("source_groups", [])) for row in rows),
        "reference_facts_total": sum(len(row.get("reference_facts", [])) for row in rows),
        "overall_locator_proxy_by_arm": overall_proxy,
        "case_failures_by_locator_stage": dict(sorted(diagnoses.items())),
        "runtime_failures_by_phase": dict(sorted(Counter(
            item.get("phase", "unknown") for item in (runtime_failures or [])
        ).items())),
        "runtime_failures": runtime_failures or [],
        "no_answer_behavior": "not_evaluated; retrieval-only experiment has no generated answer or refusal test",
        "semantic_quality": "not_assessed; locator coverage is a deterministic proxy only",
        "by_library": by_library,
        "by_query_family": by_family,
        "by_library_and_query_family": by_library_family,
        "cases": [{"case_id": row["case_id"], "completed": any(r["case_id"] == row["case_id"] for r in records)} for row in rows],
    }


def _saved_stage_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**item, **item.get("locator", {})} for item in items]


def amend_saved_diagnostics(dataset_path: Path, route_path: Path, output: Path) -> dict[str, Any]:
    """修订CPU定位诊断；只读原paired记录，不重新查询模型或改原case文件。"""
    from types import SimpleNamespace

    from wenjin.index_registry import IndexTarget
    from wenjin.serving import CorpusSources

    rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    dataset_sha = _validate_locked_dataset(dataset_path, rows, route)
    route_sha = _sha256_file(route_path)
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    before = json.loads((output / "assets-before.json").read_text(encoding="utf-8"))
    after = json.loads((output / "assets-after.json").read_text(encoding="utf-8"))
    if run.get("dataset_sha256") != dataset_sha or run.get("route_sha256") != route_sha:
        raise ValueError("saved run does not match current locked dataset/route")
    if before != after or run.get("assets_unchanged") is not True:
        raise ValueError("cannot amend diagnostics when source/index assets changed during the run")

    indices = {}
    for item in route["candidates"]:
        target = IndexTarget.from_dict(item)
        manifest_path = ROOT / item["manifest_path"]
        sources = CorpusSources.from_manifest(manifest_path, raw_root=ROOT / "raw_data",
                                              expected_corpus_id=target.corpus_identity())
        indices[(re.sub(r"[\s_]+", "-", item["library"].strip().lower()), item["version"])] = SimpleNamespace(
            target=target, sources=sources, bm25_path=ROOT / item["bm25_path"]
        )
    amendments = []
    amended_records = []
    saved_case_bindings = []
    for row in rows:
        matches = list((output / "cases").glob(f"{row['case_id']}-*.json"))
        if len(matches) != 1:
            raise ValueError(f"expected exactly one saved case for {row['case_id']}; found {len(matches)}")
        stored = json.loads(matches[0].read_text(encoding="utf-8"))
        checked = load_case_checkpoint(matches[0], expected_identity=stored.get("case_identity", ""))
        if checked is None or stored.get("case_id") != row["case_id"] or stored.get("query") != row["query"]:
            raise ValueError(f"saved case integrity/content mismatch: {row['case_id']}")
        saved_case_bindings.append({
            "case_id": row["case_id"],
            "case_file": matches[0].relative_to(output).as_posix(),
            "case_file_sha256": _sha256_file(matches[0]),
            "record_sha256": stored.get("record_sha256"),
            "case_identity": stored.get("case_identity"),
        })

        stage_map = {
            "dense": _saved_stage_candidates(stored["stages"]["dense"]),
            "bm25": _saved_stage_candidates(stored["stages"]["bm25"]),
            "fused": _saved_stage_candidates(stored["stages"]["fused"]),
            "ranked": _saved_stage_candidates(stored["stages"]["ranked"][:stored["controls"]["top_k"]]),
            "baseline_selected": _saved_stage_candidates(stored["stages"]["baseline_selected"]),
            "selected": _saved_stage_candidates(stored["stages"]["selected"]),
        }
        proxies = {stage: locator_coverage_proxy(candidates, row)
                   for stage, candidates in stage_map.items()}
        source_presence, source_details = [], []
        for group in row.get("source_groups", []):
            statuses = []
            for span in group.get("source_spans", []):
                key = (re.sub(r"[\s_]+", "-", str(span.get("library", "")).strip().lower()),
                       str(span.get("version", "")))
                index = indices.get(key)
                status = _span_presence_in_chunks(index, span) if index is not None else (False, False)
                statuses.append(status)
                source_details.append({
                    "source_group_id": group.get("source_group_id"),
                    "source_id": span.get("source_id"), "source_path": span.get("path"),
                    "manifest_relative_path": span.get("manifest_relative_path"),
                    "manifest_source_present_and_identity_valid": status[0],
                    "full_locator_retained_in_bm25": status[1],
                    "source_sha256": span.get("source_sha256"), "locator": span.get("locator"),
                })
            source_presence.append(statuses)
        failure = classify_locator_failure(row, source_presence=source_presence, stage_proxies=proxies)
        amendment = {
            "case_id": row["case_id"], "case_identity": stored["case_identity"],
            "original_locator_failure_stage": stored["diagnosis"]["locator_failure_stage"],
            "amended_locator_failure_stage": failure,
            "root_cause": "source_id and BM25 logical_document_id use different identifier namespaces; source presence now resolves by the already-validated exact production source_path",
            "source_presence_and_chunk_retention": source_details,
            "original_coverage_locator_proxy": stored["coverage_locator_proxy"],
            "amended_coverage_locator_proxy": proxies,
            "reranking_and_selected_candidates_unchanged": True,
        }
        amendments.append(amendment)
        amended_records.append({
            **stored,
            "diagnosis": {**stored["diagnosis"], "locator_failure_stage": failure},
            "coverage_locator_proxy": proxies,
        })
    result = {
        "schema_version": 1,
        "dataset_sha256": dataset_sha,
        "route_sha256": route_sha,
        "based_on_original_saved_case_records": True,
        "gpu_inference_repeated": False,
        "case_record_files_modified": False,
        "provenance": {
            "original_run_json_sha256": _sha256_file(output / "run.json"),
            "original_gpu_runner_sha256": run.get("code_sha256", {}).get(
                "evaluation/experiments/run_iteration_retrieval_validation.py"
            ),
            "amendment_runner_sha256": _sha256_file(Path(__file__)),
            "saved_case_count": len(saved_case_bindings),
            "saved_case_bindings": saved_case_bindings,
            "saved_case_bindings_sha256": hashlib.sha256(
                canonical_json_bytes(saved_case_bindings)
            ).hexdigest(),
        },
        "assets_before_after_identical": True,
        "amendments": amendments,
        "amended_summary": _summary(amended_records, rows),
        "interpretation": "locator/source-stage diagnosis only; saved ranking and evidence bodies were not changed; locator proxies are not semantic judgments",
    }
    _write_json(output / "diagnosis-amendment.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="五库developer validation fresh paired retrieval验证")
    parser.add_argument("--route", type=Path, default=DEFAULT_ROUTE)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--amend-diagnostics-only", action="store_true",
                        help="从已保存候选CPU重算source/locator诊断，不执行检索或模型")
    args = parser.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    dataset_path, route_path = args.dataset.resolve(), args.route.resolve()
    rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise SystemExit("dataset is empty")
    route = json.loads(route_path.read_text(encoding="utf-8-sig"))
    try:
        dataset_sha = _validate_locked_dataset(dataset_path, rows, route)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"dataset lock/source validation failed: {error}") from error
    route_sha = _sha256_file(route_path)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "cases").mkdir(exist_ok=True)
    if args.amend_diagnostics_only:
        amended = amend_saved_diagnostics(dataset_path, route_path, args.output)
        counts = amended["amended_summary"]["case_failures_by_locator_stage"]
        print(json.dumps({"diagnosis_amendment": str(args.output / "diagnosis-amendment.json"),
                          "cases": len(rows), "amended_failure_counts": counts}, ensure_ascii=False))
        return
    from wenjin.qdrant_connection import create_qdrant_client
    from wenjin.settings import Settings
    settings = Settings()
    client = create_qdrant_client(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    if not settings.embedding_model_path.is_dir() or not settings.reranker_model_path.is_dir():
        raise SystemExit("本地embedding/reranker模型目录缺失；未启动下载")
    model = {
        "embedding": settings.embedding_model_path.name,
        "reranker": settings.reranker_model_path.name,
        "embedding_tree": _tree_fingerprint(settings.embedding_model_path),
        "reranker_tree": _tree_fingerprint(settings.reranker_model_path),
    }
    before = _asset_fingerprint(route_path, route, client)
    asset_errors = _asset_fingerprint_errors(before)
    if asset_errors:
        raise SystemExit("read-only source/route preflight failed: " + "; ".join(asset_errors))
    code_fingerprint = _code_fingerprint()
    _write_json(args.output / "assets-before.json", before)
    _write_json(args.output / "model-fingerprint.json", model)

    import torch

    from wenjin.retrieval.rerank import Qwen3Reranker
    from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig
    embedder = settings.embedding()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    reranker = Qwen3Reranker(settings.reranker_model_path, device=device,
                              max_length=CONTROLS["reranker_max_length"],
                              batch_size=CONTROLS["reranker_batch_size"])
    router = None
    records: list[dict[str, Any]] = []
    runtime_failures: list[dict[str, Any]] = []
    attempts_path = args.output / "attempts.jsonl"
    _mark_interrupted_attempts(attempts_path)
    try:
        router = MultiCorpusRouter.from_route_file(route_path, root=ROOT, qdrant_client=client,
                                                   embedder=embedder, reranker=None)
        config = RouteConfig(candidate_k=CONTROLS["candidate_k"], top_k=CONTROLS["top_k"],
                             rrf_k=CONTROLS["rrf_k"], max_libraries=5,
                             rerank_pool=CONTROLS["rerank_pool"],
                             context_char_budget=CONTROLS["context_char_budget"])
        for index, row in enumerate(rows, 1):
            case_id = row["case_id"]
            identity = build_case_identity(row, dataset_sha256=dataset_sha, route_sha256=route_sha,
                                           controls=CONTROLS, model={"embedding": model["embedding"],
                                                                   "reranker": model["reranker"],
                                                                   "embedding_sha256": model["embedding_tree"]["sha256"],
                                                                   "reranker_sha256": model["reranker_tree"]["sha256"]},
                                           runtime_fingerprint={"code_sha256": code_fingerprint,
                                                                "assets": before})
            case_path = args.output / "cases" / f"{case_id}-{identity[:12]}.json"
            if not args.no_resume:
                recovered = load_case_checkpoint(case_path, expected_identity=identity)
                if recovered is not None:
                    records.append(recovered)
                    print(f"[{index}/{len(rows)}] {case_id}: resumed")
                    continue
            attempt = {"case_id": case_id, "case_identity": identity, "status": "running", "phase": "retrieve", "started_at": time.time()}
            _append_jsonl(attempts_path, attempt)
            try:
                record = paired_case(router, row, reranker=reranker, config=config, case_identity=identity)
                write_case_checkpoint(case_path, record)
                record = json.loads(case_path.read_text(encoding="utf-8"))
                records.append(record)
                _append_jsonl(attempts_path, {**attempt, "status": "completed", "phase": "complete", "finished_at": time.time(), "record_sha256": record["record_sha256"]})
                print(f"[{index}/{len(rows)}] {case_id}: complete; locator failure={record['diagnosis']['locator_failure_stage']}")
            except Exception as error:
                phase = error.phase if isinstance(error, IterationPhaseError) else "paired_case"
                cause = error.cause if isinstance(error, IterationPhaseError) else error
                failure = {"case_id": case_id, "phase": phase,
                           "error_class": type(cause).__name__, "error_message": str(cause)}
                runtime_failures.append(failure)
                _append_jsonl(attempts_path, {**attempt, "status": "failed", **failure, "finished_at": time.time()})
                print(f"[{index}/{len(rows)}] {case_id}: failed ({type(error).__name__})")
        runtime_failure_history = _attempt_failures(attempts_path)
        _write_json(args.output / "summary.json", _summary(records, rows, runtime_failure_history))
        after = _asset_fingerprint(route_path, route, client)
        _write_json(args.output / "assets-after.json", after)
        _write_json(args.output / "run.json", {
            "dataset_path": str(dataset_path.relative_to(ROOT)), "dataset_sha256": dataset_sha,
            "route_path": str(route_path.relative_to(ROOT)), "route_sha256": route_sha,
            "model": model, "device": device, "controls": CONTROLS,
            "code_sha256": code_fingerprint,
            "assets_unchanged": before == after,
            "active_index_or_config_touched": False,
            "answer_generation": "not performed",
        })
    finally:
        if router is not None:
            router.close()
        else:
            client.close()
        reranker.close()
    print(f"summary: {args.output / 'summary.json'}")


if __name__ == "__main__":
    main()
