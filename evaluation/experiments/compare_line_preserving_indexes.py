"""成对对照完整行切分索引的纯数据与契约校验函数。

本文件首阶段不加载模型、不连接Qdrant；真实paired runner后续只负责采集原始结果。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
_VERSION_PREFIX = re.compile(r"^\d+(?:\.\d+)*")
_EXPECTED_VALIDATION_SHA256 = "70fc10faeb2480cc6984b4545b8c3a5ba6beb2de252da536cd09c5a32626328e"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.quality.run_multicorpus_eval import (  # noqa: E402
    evidence_metrics as locator_metrics,
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_validation_dataset(
    dataset_path: Path,
    seal_path: Path,
    sha_path: Path,
) -> dict[str, Any]:
    """只放行当前封存记录所指向的50题validation，并拒绝已测test内容。"""
    dataset_path, seal_path, sha_path = map(Path, (dataset_path, seal_path, sha_path))
    expected_path = (ROOT / "evaluation/datasets/rag-validation-001.jsonl").resolve()
    # 先按路径拒绝test/frozen来源，避免为识别输入而读取已封存题目正文。
    if dataset_path.name != "rag-validation-001.jsonl" or dataset_path.resolve() != expected_path:
        raise ValueError("paired实验仅允许项目中的rag-validation-001.jsonl")
    raw = dataset_path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    sidecar_hash = sha_path.read_text(encoding="utf-8").split()[0].lower()
    if actual_hash != sidecar_hash or actual_hash != _EXPECTED_VALIDATION_SHA256:
        raise ValueError("题集Hash不匹配批准的validation版本")

    seals = json.loads(seal_path.read_text(encoding="utf-8-sig"))
    validation_seals = [item for item in seals if item.get("split") == "validation"]
    test_seals = [item for item in seals if item.get("split") == "test"]
    if len(validation_seals) != 1 or len(test_seals) != 1:
        raise ValueError("dataset seal缺少唯一validation/test记录")
    validation_seal, test_seal = validation_seals[0], test_seals[0]
    sealed_test_hashes = {
        test_seal.get("original_sha256"), test_seal.get("imported_sha256"),
        test_seal.get("evaluated_sha256"),
        *(item.get("sha256") for item in test_seal.get("revision_history", [])),
    }
    if actual_hash in sealed_test_hashes:
        raise ValueError("拒绝已封存或已评测的test题集")
    if (
        actual_hash != validation_seal.get("imported_sha256")
        or validation_seal.get("evaluated") is not False
    ):
        raise ValueError("题集不是当前未封存评测的validation版本")

    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if len(rows) != 50 or any(row.get("split") != "validation" for row in rows):
        raise ValueError("paired实验只接受50条且全部标记为validation的题目")
    if len({row.get("id") for row in rows}) != len(rows):
        raise ValueError("validation题目ID重复")
    return {"rows": rows, "sha256": actual_hash, "question_count": len(rows)}


def validate_paired_routes(old_route_path: Path, new_target_path: Path) -> dict[str, Any]:
    """复制旧route并仅替换3D Tiles目标；其他四库目标必须逐字段相同。"""
    old_route = json.loads(Path(old_route_path).read_text(encoding="utf-8-sig"))
    replacement = json.loads(Path(new_target_path).read_text(encoding="utf-8-sig"))
    candidates = old_route.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise ValueError("paired route必须含五个已知候选目标")
    matched = [
        index for index, item in enumerate(candidates)
        if item.get("library") == "ogc-3dtiles" and item.get("version") == "1.1"
    ]
    if len(matched) != 1:
        raise ValueError("旧route中3D Tiles 1.1目标不唯一")
    index = matched[0]
    old_target = candidates[index]
    if replacement.get("target_id") != "ogc-3dtiles-1.1-blocks-split-candidate-003":
        raise ValueError("新route必须使用已准入的3D Tiles candidate-003")
    for field in (
        "library", "version", "strategy", "manifest_path", "manifest_sha256",
        "embedding_model", "vector_dimension", "canonical_corpus_sha256",
        "normalization_profile",
    ):
        if replacement.get(field) != old_target.get(field):
            raise ValueError(f"新旧3D Tiles目标的{field}不一致")
    old_corpus_id = old_target.get("corpus_id") or Path(old_target["manifest_path"]).parent.name
    new_corpus_id = replacement.get("corpus_id") or Path(replacement["manifest_path"]).parent.name
    if new_corpus_id != old_corpus_id:
        raise ValueError("新旧3D Tiles目标的corpus_id身份不一致")
    new_route = json.loads(json.dumps(old_route))
    new_route["candidates"][index] = replacement
    if [item for i, item in enumerate(new_route["candidates"]) if i != index] != [
        item for i, item in enumerate(old_route["candidates"]) if i != index
    ]:
        raise ValueError("除3D Tiles外的route候选发生变化")
    return {
        "baseline_route": old_route,
        "candidate_route": new_route,
        "replaced_target": {"old": old_target, "new": replacement},
    }


def validate_candidate_location(
    candidate: dict, source_line_map: dict[tuple[str, int | None], list[str]]
) -> dict[str, Any]:
    """检查候选正文是否精确等于其声明的来源连续切片；PDF只验证页存在。"""
    path = candidate.get("source_path")
    page = candidate.get("page_number")
    if page is not None:
        present = (path, page) in source_line_map
        return {
            "status": "page_level_only" if present else "invalid",
            "valid": present,
            "reason": None if present else "source_page_missing",
        }
    lines = source_line_map.get((path, None))
    if lines is None:
        return {"status": "invalid", "valid": False, "reason": "source_lines_missing"}
    start, end = candidate.get("start_line"), candidate.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return {"status": "invalid", "valid": False, "reason": "invalid_line_range"}
    if end > len(lines):
        return {"status": "invalid", "valid": False, "reason": "line_range_out_of_source"}
    expected = "".join(lines[start - 1 : end])
    valid = candidate.get("text") == expected
    return {
        "status": "verified" if valid else "invalid",
        "valid": valid,
        "reason": None if valid else "text_does_not_match_declared_source_slice",
    }


def contract_metrics(
    candidates: list[dict],
    spans: list[dict],
    source_line_map: dict[tuple[str, int | None], list[str]],
    *,
    k: int = 5,
    blank_lines: dict[str, frozenset[int]] | None = None,
) -> dict[str, Any]:
    """按可信来源切片覆盖文本行；PDF证据仅报告物理页存在性。"""
    blank_lines = blank_lines or {}
    top = candidates[:k]
    locations = [validate_candidate_location(item, source_line_map) for item in top]
    invalid_count = sum(item["status"] == "invalid" for item in locations)
    line_total = line_hit = page_total = page_present = uncheckable = 0
    details = []
    for span_index, span in enumerate(spans):
        path, page = span["path"], span.get("page")
        if page is not None:
            page_total += 1
            present = (path, page) in source_line_map and any(
                candidate.get("source_path") == path and candidate.get("page_number") == page
                and location["status"] == "page_level_only"
                for candidate, location in zip(top, locations)
            )
            page_present += int(present)
            details.append({"span_index": span_index, "level": "page", "hit": present})
            continue

        source_lines = source_line_map.get((path, None))
        if source_lines is None:
            uncheckable += 1
            details.append({"span_index": span_index, "level": "text", "hit": False,
                            "uncheckable": "source_lines_missing"})
            continue
        start, end = span.get("start_line"), span.get("end_line")
        if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(source_lines):
            uncheckable += 1
            details.append({"span_index": span_index, "level": "text", "hit": False,
                            "uncheckable": "invalid_span_range"})
            continue
        required = {
            number for number in range(start, end + 1)
            if number not in blank_lines.get(path, frozenset())
            and source_lines[number - 1].strip()
        }
        if not required:
            uncheckable += 1
            details.append({"span_index": span_index, "level": "text", "hit": False,
                            "uncheckable": "span_has_no_content_lines"})
            continue
        line_total += 1
        covered = set()
        for candidate, location in zip(top, locations):
            if (
                location["status"] == "verified"
                and candidate.get("page_number") is None
                and candidate.get("source_path") == path
                and isinstance(candidate.get("start_line"), int)
                and isinstance(candidate.get("end_line"), int)
            ):
                covered.update(required & set(range(candidate["start_line"], candidate["end_line"] + 1)))
        hit = required <= covered
        line_hit += int(hit)
        details.append({"span_index": span_index, "level": "text", "hit": hit,
                        "required_lines": len(required), "covered_lines": len(covered),
                        "missing_line_numbers": sorted(required - covered)})
    total = line_total + page_total
    hit_total = line_hit + page_present
    return {
        "k": k,
        "line_spans_total": line_total,
        "line_spans_hit": line_hit,
        "line_span_recall": line_hit / line_total if line_total else None,
        "page_spans_total": page_total,
        "page_spans_present": page_present,
        "page_presence_rate": page_present / page_total if page_total else None,
        "all_text_spans_hit": (
            line_total > 0 and line_hit == line_total and uncheckable == 0
        ) if line_total else None,
        "all_locator_spans_hit": total > 0 and hit_total == total and uncheckable == 0,
        "invalid_evidence_count": invalid_count,
        "uncheckable_span_count": uncheckable,
        "details": details,
        "locator_metrics": locator_metrics(top, spans, k=k, blank_lines=blank_lines) if spans else None,
    }


def make_question_record(row: dict, route_args: dict, baseline: dict, candidate: dict) -> dict:
    """统一保存50题原始输入和两臂未裁剪候选、预算后证据。"""
    input_fields = (
        "id", "split", "library", "version", "query", "answer_rubric",
        "expected_status", "evidence", "evidence_policy",
    )
    return {
        "input": {key: row[key] for key in input_fields if key in row},
        "route_args": dict(route_args),
        "arms": {"baseline": baseline, "candidate_003": candidate},
    }


def _route_arguments(library: str, version: str) -> dict[str, str | None]:
    from wenjin.retrieval.router import normalize_library

    match = _VERSION_PREFIX.match(version.strip())
    return {
        "library": normalize_library(library),
        "version": match.group(0) if match else None,
    }


def _load_source_line_map(route: dict, rows: list[dict]) -> tuple[dict, dict, dict]:
    """用生产manifest loader解析一次证据来源，缓存行/page映射及空行。"""
    from wenjin.ingestion.multiformat import load_retrieval_documents

    parsed: dict[tuple[str, int | None], list[str]] = {}
    blank_lines: dict[str, frozenset[int]] = {}
    source_corpora: dict[str, str] = {}
    for target in route["candidates"]:
        manifest_path = ROOT / target["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        corpus_id = manifest.get("corpus_id") or manifest_path.parent.name
        for document in load_retrieval_documents(manifest_path, ROOT / "raw_data"):
            path = document["source_path"]
            page = document.get("page_number")
            lines = document["text"].splitlines(keepends=True)
            parsed[(path, page)] = lines
            source_corpora[path] = corpus_id
            if page is None:
                blank_lines[path] = frozenset(
                    number for number, line in enumerate(lines, start=1) if not line.strip()
                )
    missing = [
        span for row in rows for span in row["evidence"]
        if (span["path"], span.get("page")) not in parsed
    ]
    if missing:
        raise ValueError(f"validation证据来源无法解析：{len(missing)} spans")
    return parsed, blank_lines, source_corpora


def _annotate_candidates(candidates: list[dict], source_line_map: dict, evidence_paths: set[str]) -> list[dict]:
    output = []
    for candidate in candidates:
        copied = dict(candidate)
        if (copied.get("source_path"), copied.get("page_number")) in source_line_map or (
            copied.get("source_path"), None
        ) in source_line_map:
            copied["source_location"] = validate_candidate_location(copied, source_line_map)
        else:
            copied["source_location"] = {"status": "source_map_unavailable", "valid": None}
        copied["is_gold_source"] = copied.get("source_path") in evidence_paths
        output.append(copied)
    return output


def _context_drops(ranked: list[dict], selected: list[dict], *, top_k: int, budget: int) -> list[dict]:
    selected_ids = {item["chunk_id"] for item in selected}
    used = 0
    drops = []
    for rank, item in enumerate(ranked[:top_k], start=1):
        length = len(item["text"])
        if item["chunk_id"] in selected_ids:
            used += length
        else:
            drops.append({
                "chunk_id": item["chunk_id"],
                "rank": rank,
                "text_chars": length,
                "reason": "single_evidence_exceeds_budget" if length > budget
                else "remaining_context_budget_exceeded",
            })
    return drops


def _scope_metrics(spans: list[dict], corpus_id: str, source_corpora: dict[str, str]) -> list[dict]:
    return [
        span for span in spans
        if span.get("corpus_id") == corpus_id
        or (span.get("corpus_id") is None and source_corpora.get(span["path"]) == corpus_id)
    ]


def _arm_result(search_result: dict, retrieval: dict, spans: list[dict], scoped_spans: list[dict],
                source_line_map: dict, blank_lines: dict, *, budget: int) -> dict:
    from wenjin.retrieval.service import select_context

    stage_keys = {
        "dense": "dense_candidates",
        "bm25": "bm25_candidates",
        "fused": "fused_candidates",
        "ranked": "ranked_candidates",
    }
    evidence_paths = {span["path"] for span in spans}
    stages = {}
    for name, key in stage_keys.items():
        raw_candidates = retrieval[key]
        candidates = _annotate_candidates(raw_candidates, source_line_map, evidence_paths)
        stages[name] = {
            "candidates": candidates,
            "locator_metrics": {
                f"at_{k}": locator_metrics(raw_candidates, spans, k=k, blank_lines=blank_lines)
                for k in (5, 20)
            },
            "contract_metrics": {
                f"at_{k}": {
                    "all_spans": contract_metrics(raw_candidates, spans, source_line_map,
                                                   k=k, blank_lines=blank_lines),
                    "in_scope": contract_metrics(raw_candidates, scoped_spans, source_line_map,
                                                  k=k, blank_lines=blank_lines),
                }
                for k in (5, 20)
            },
        }
    selected = _annotate_candidates(search_result["evidence"], source_line_map, evidence_paths)
    ranked = retrieval["ranked_candidates"]
    actual_selection = select_context(
        ranked, top_k=5, char_budget=budget,
    )
    actual_ids = [item["chunk_id"] for item in actual_selection]
    returned_ids = [item["chunk_id"] for item in search_result["evidence"]]
    return {
        "stages": stages,
        "selected_context": {
            "evidence": selected,
            "context_chars_used": search_result["context_chars_used"],
            "context_char_budget": budget,
            "returned_count": len(selected),
            "select_context_ids": actual_ids,
            "returned_ids_match_select_context": returned_ids == actual_ids,
            "budget_excluded": _context_drops(ranked, actual_selection, top_k=5, budget=budget),
            "attachment_dropped": search_result.get("evidence_dropped", {}),
            "locator_metrics": locator_metrics(search_result["evidence"], spans, k=5,
                                                blank_lines=blank_lines),
            "contract_metrics": {
                "all_spans": contract_metrics(search_result["evidence"], spans, source_line_map,
                                               k=5, blank_lines=blank_lines),
                "in_scope": contract_metrics(search_result["evidence"], scoped_spans,
                                              source_line_map, k=5, blank_lines=blank_lines),
            },
        },
        "retrieval": retrieval,
        "timing_seconds": retrieval["timing_seconds"],
    }


def main() -> None:
    import argparse
    import os
    from threading import Lock

    parser = argparse.ArgumentParser(description="paired 3D Tiles line-preserving validation对照")
    parser.add_argument("--output-dir", type=Path,
                        default=ROOT / "artifacts/line-preserving-validation-2026-09-29")
    args = parser.parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset_info = validate_validation_dataset(
        ROOT / "evaluation/datasets/rag-validation-001.jsonl",
        ROOT / "artifacts/corpus-acceptance-001/dataset-seals.json",
        ROOT / "evaluation/datasets/rag-validation-001.sha256",
    )
    route_paths = validate_paired_routes(
        ROOT / "configs/indexes/multicorpus-candidates-002.json",
        ROOT / "configs/indexes/ogc-3dtiles-1.1-blocks-split-candidate-003.json",
    )
    baseline_route_path = output_dir / "baseline-route-002.json"
    candidate_route_path = output_dir / "candidate-route-003.json"
    baseline_route_path.write_text(
        json.dumps(route_paths["baseline_route"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    candidate_route_path.write_text(
        json.dumps(route_paths["candidate_route"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rows = dataset_info["rows"]
    source_line_map, blank_lines, source_corpora = _load_source_line_map(
        route_paths["baseline_route"], rows,
    )
    report_path = output_dir / "results.json"
    report: dict[str, Any] = {
        "status": "running",
        "dataset": {"path": "evaluation/datasets/rag-validation-001.jsonl",
                    "sha256": dataset_info["sha256"], "questions": 50, "split": "validation"},
        "controls": {"candidate_k": 20, "top_k": 5, "rrf_k": 60, "rerank_pool": 30,
                     "context_char_budget": 8000, "reranker_max_length": 1024,
                     "reranker_batch_size": 2, "execution": "sequential paired, shared models"},
        "route_control": route_paths["replaced_target"],
        "route_copies": {
            "baseline": {"path": baseline_route_path.relative_to(ROOT).as_posix(),
                         "sha256": _sha256_file(baseline_route_path)},
            "candidate_003": {"path": candidate_route_path.relative_to(ROOT).as_posix(),
                              "sha256": _sha256_file(candidate_route_path)},
            "other_four_targets_identical": True,
        },
        "source_map": {"source_files": len({path for path, _ in source_line_map}),
                       "line_files": len(blank_lines)},
        "details": [],
    }

    def write_progress() -> None:
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report_path)

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
        report["models"] = {
            "embedding": settings.embedding_model_path.name,
            "reranker": settings.reranker_model_path.name,
        }
        device = "cuda" if torch.cuda.is_available() else "cpu"
        reranker = Qwen3Reranker(
            settings.reranker_model_path, device=device, max_length=1024, batch_size=2,
        )
        shared_lock = Lock()

        routers = []
        for arm_name, route_path in (
            ("baseline", baseline_route_path), ("candidate_003", candidate_route_path),
        ):
            router = MultiCorpusRouter.from_route_file(
                route_path, root=ROOT, qdrant_client=client, embedder=embedder,
                reranker=reranker, search_lock=shared_lock,
            )
            # 将已构造路由换成捕获子类会重建候选资产，故实例上只包装retrieve方法。
            original_retrieve = router.retrieve
            def capture_retrieve(*a, _original=original_retrieve, _owner=router, **kw):
                result = _original(*a, **kw)
                _owner.last_retrieval = result
                return result
            router.retrieve = capture_retrieve
            routers.append((arm_name, router))

        config = RouteConfig(
            candidate_k=20, top_k=5, rrf_k=60, max_libraries=5,
            rerank_pool=30, context_char_budget=8000,
        )
        for row in rows:
            current_id = row["id"]
            route_args = _route_arguments(row["library"], row["version"])
            library, version = route_args["library"], route_args["version"]
            arm_results = {}
            spans = row["evidence"]
            for arm_name, router in routers:
                index = router.resolve(library, version)[0]
                scope = _scope_metrics(spans, index.corpus_identity, source_corpora)
                started = perf_counter()
                search_result = router.search(
                    row["query"], library=library, version=version, config=config,
                )
                retrieval = router.last_retrieval
                arm = _arm_result(
                    search_result, retrieval, spans, scope, source_line_map, blank_lines,
                    budget=config.context_char_budget,
                )
                arm.update({
                    "target_id": index.target_id,
                    "routed_corpus_id": index.corpus_identity,
                    "cross_library_scope": len({
                        span.get("corpus_id") or source_corpora.get(span["path"])
                        for span in spans
                    }) > 1,
                    "in_scope_span_indices": [
                        i for i, span in enumerate(spans)
                        if span in scope
                    ],
                    "total_search_seconds": round(perf_counter() - started, 6),
                })
                arm_results[arm_name] = arm
            record = make_question_record(row, route_args,
                                          arm_results["baseline"], arm_results["candidate_003"])
            report["details"].append(record)
            write_progress()
            print(f"[{len(report['details'])}/50] {current_id} baseline+candidate完成", flush=True)

        report["status"] = "completed"
        report["question_count"] = len(report["details"])
        write_progress()
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = {"question_id": current_id, "error_type": type(error).__name__,
                             "detail": str(error)}
        write_progress()
        raise
    finally:
        if reranker is not None:
            reranker.close()
        if client is not None:
            client.close()
    print(f"paired结果：{report_path.relative_to(ROOT).as_posix()}（{len(report['details'])}题）")


if __name__ == "__main__":
    main()
