"""固定运行身份的检索策略对照runner。

Runner只接收已固定快照的router与服务端绑定的题目；reference/gold字段不参与检索。
每个case写入可校验checkpoint，恢复时只复用身份和内容hash均匹配的完整记录。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from wenjin.retrieval.fusion import rrf_fuse
from wenjin.retrieval.rerank import candidate_document, rank_candidates
from wenjin.retrieval.router import MultiCorpusRouter, RouteConfig, fuse_identity
from wenjin.retrieval.service import select_context
from wenjin.serving import SourceTooLarge

_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_PROFILES = ("baseline", "parent_child", "multi_query", "mmr")
_MAX_QUERIES = 4
_MMR_LAMBDA = 0.72


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def case_identity(row: Mapping[str, Any]) -> str:
    """checkpoint身份只依赖实际检索输入，不使用reference/gold字段。"""
    identity = {
        "case_id": row.get("case_id") or row.get("id"),
        "query": row.get("query"),
        "library": row.get("library"),
        "version": row.get("version"),
        "target_id": row.get("target_id"),
    }
    return _sha256(_canonical(identity))


def run_identity_sha256(run_identity: Mapping[str, Any]) -> str:
    clean = dict(run_identity)
    clean.pop("run_identity_sha256", None)
    return _sha256(_canonical(clean))


def case_identity_bindings(rows: list[Mapping[str, Any]]) -> dict[str, str]:
    """给任务创建快照提供case_id到输入身份的绑定，resume时可独立比对。"""
    bindings: dict[str, str] = {}
    for row in rows:
        case_id = str(row.get("case_id") or row.get("id") or "")
        if not _CASE_ID.fullmatch(case_id) or case_id in bindings:
            raise ValueError("题集case_id为空、重复或格式无效")
        bindings[case_id] = case_identity(row)
    return bindings


def checkpoint_record_hash(record: Mapping[str, Any]) -> str:
    clean = dict(record)
    clean.pop("record_sha256", None)
    return _sha256(_canonical(clean))


def validate_case_checkpoint(
    path: Path, *, row: Mapping[str, Any], run_identity: Mapping[str, Any]
) -> dict[str, Any] | None:
    """返回经完整hash验证的checkpoint；不匹配视为损坏而非静默重跑。"""
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"case checkpoint无法读取：{path.name}") from error
    if not isinstance(record, dict):
        raise ValueError(f"case checkpoint结构无效：{path.name}")
    expected_id = str(row.get("case_id") or row.get("id") or "")
    expected_run_hash = run_identity_sha256(run_identity)
    if (
        record.get("schema_version") != 1
        or record.get("case_id") != expected_id
        or record.get("case_identity") != case_identity(row)
        or record.get("run_identity_sha256") != expected_run_hash
        or record.get("record_sha256") != checkpoint_record_hash(record)
    ):
        raise ValueError(f"case checkpoint身份或hash不匹配：{path.name}")
    return record


def _candidate_hash(candidate: Mapping[str, Any]) -> str:
    return _sha256(str(candidate.get("text", "")).encode("utf-8"))


def _candidate_record(candidate: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(candidate)
    result["text_sha256"] = _candidate_hash(result)
    return result


def _timed_retrieve(router: Any, query: str, row: Mapping[str, Any], config: RouteConfig) -> dict[str, Any]:
    started = time.perf_counter()
    result = router.retrieve(
        query,
        library=row.get("library"),
        version=row.get("version"),
        target_id=row.get("target_id"),
        config=config,
    )
    result.setdefault("timing_seconds", {})["runner_total"] = round(time.perf_counter() - started, 6)
    return result


def _select_candidates(candidates: list[dict[str, Any]], config: RouteConfig) -> list[dict[str, Any]]:
    return select_context(candidates, top_k=config.top_k, char_budget=config.context_char_budget)


def _native_parent_span(lines: list[str], candidate: Mapping[str, Any]) -> tuple[int, int, str] | None:
    """只为源文件可识别的真实单位返回父区间，不用邻行窗口冒充章节。"""
    child_start, child_end = candidate.get("start_line"), candidate.get("end_line")
    if not isinstance(child_start, int) or not isinstance(child_end, int):
        return None
    path = str(candidate.get("source_path", "")).lower()
    if path.endswith((".html", ".htm")):
        # Sphinx/GDAL等选项条目通常是一个dt定义加其后dd说明；仅在物理行边界
        # 唯一且确实包住child时采用，避免解析器重排HTML后伪造行号。
        dt_rows = [i for i, line in enumerate(lines, 1) if re.search(r"<dt\b", line, re.I)]
        before = [i for i in dt_rows if i <= child_start]
        start = before[-1] if before else None
        if start is not None:
            next_dt_inside = any(start < line <= child_end for line in dt_rows)
            close_rows = [number for number, line in enumerate(lines, 1)
                          if number >= child_end and re.search(r"</dd\s*>", line, re.I)]
            end = close_rows[0] if close_rows else None
            if end is not None:
                exact_block = "\n".join(lines[start - 1:end])
                if (not next_dt_inside and end >= child_end
                        and exact_block.lower().count("<dt") == 1
                        and exact_block.lower().count("</dt>") == 1
                        and len(re.findall(r"<dd\b", exact_block, re.I)) == 1
                        and len(re.findall(r"</dd\s*>", exact_block, re.I)) == 1):
                    return start, end, "html_definition_list_dt_dd"
        # Heading sections are a second source-native HTML unit.
        headings: list[tuple[int, int]] = []
        for line_number, line in enumerate(lines, 1):
            found = list(re.finditer(r"<h([1-6])\b[^>]*>.*?</h\1\s*>", line, re.I))
            if len(found) == 1:
                headings.append((line_number, int(found[0].group(1))))
            elif len(found) > 1:
                headings.append((line_number, 0))
        applicable = [item for item in headings if item[0] <= child_start and item[1] > 0]
        if any(level == 0 and child_start <= number <= child_end for number, level in headings):
            return None
        for start, level in reversed(applicable):
            end = next((number - 1 for number, later_level in headings
                        if number > start and later_level > 0 and later_level <= level), len(lines))
            if end >= child_end:
                return start, end, "html_heading_section"
        return None
    if path.endswith((".md", ".markdown")):
        headings: list[tuple[int, int]] = []
        for line_number, line in enumerate(lines, 1):
            markdown = re.match(r"^\s{0,3}(#{1,6})\s+\S", line)
            if markdown:
                headings.append((line_number, len(markdown.group(1))))
        applicable = [item for item in headings if item[0] <= child_start]
        for start, level in reversed(applicable):
            end = next((number - 1 for number, later_level in headings
                        if number > start and later_level <= level), len(lines))
            if end >= child_end:
                return start, end, "markdown_heading_section"
    return None


def _parent_documents(router: Any, ranked: list[dict[str, Any]], config: RouteConfig) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """从verified source映射source-native parent；不合格时原样回退child。"""
    parents: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    diagnostics = {
        "expanded": 0, "fallback_child": 0, "fallback_reason": {},
        "non_text": 0, "too_large": 0, "unreadable": 0, "duplicate_source": 0,
    }
    for child in ranked:
        index = next((item for item in router.indices if item.target_id == child.get("target_id")), None)
        if index is None or index.sources is None:
            diagnostics["unreadable"] += 1
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["no_verified_source_view"] = diagnostics["fallback_reason"].get("no_verified_source_view", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        try:
            source_record = index.sources.match_chunk_path(str(child.get("source_path", "")))
            if not source_record.is_text:
                diagnostics["non_text"] += 1
                diagnostics["fallback_child"] += 1
                diagnostics["fallback_reason"]["source_not_text"] = diagnostics["fallback_reason"].get("source_not_text", 0) + 1
                parents.append({**child, "parent_kind": "child_fallback"})
                continue
            verified = index.sources.read_verified(source_record.relative, max_bytes=max(2_000_000, config.context_char_budget * 8))
            encoding = source_record.encoding or "utf-8"
            text = verified.content.decode(encoding, errors="strict")
        except SourceTooLarge:
            # 原文件超过可读取上限时不截断父正文，保留已索引child证据。
            diagnostics["unreadable"] += 1
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["source_too_large_to_read"] = diagnostics["fallback_reason"].get("source_too_large_to_read", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        lines = text.splitlines()
        from wenjin.ingestion.multiformat import parse_line_mapped_text, parser_for_path

        manifest_parser = source_record.parser
        candidate_parser = child.get("parser")
        if manifest_parser and candidate_parser and manifest_parser != candidate_parser:
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["candidate_parser_disagrees_with_manifest"] = diagnostics["fallback_reason"].get("candidate_parser_disagrees_with_manifest", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        parser = manifest_parser or candidate_parser
        if parser is None and not source_record.multiformat_input:
            # 与legacy admission相同：无多格式parser声明时才按source path扩展名推导。
            parser = parser_for_path(Path(source_record.relative))
        if not isinstance(parser, str) or parser in {
            "mineru-ocr-page-v1", "mixed-text-ocr-page-v1", "pymupdf-page-v1",
        }:
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["parser_identity_missing_or_nontext"] = diagnostics["fallback_reason"].get("parser_identity_missing_or_nontext", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        try:
            # 与admit_index使用同一逐物理行parser；HTML tag转可见文字但不改变raw行号。
            parsed_lines = parse_line_mapped_text(text, parser).splitlines()
        except ValueError:
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["unsupported_source_parser"] = diagnostics["fallback_reason"].get("unsupported_source_parser", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        if len(parsed_lines) != len(lines):
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["line_mapping_contract_mismatch"] = diagnostics["fallback_reason"].get("line_mapping_contract_mismatch", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        child_start, child_end = child.get("start_line"), child.get("end_line")
        if not isinstance(child_start, int) or not isinstance(child_end, int) or not (1 <= child_start <= child_end <= len(parsed_lines)):
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["child_locator_outside_verified_source"] = diagnostics["fallback_reason"].get("child_locator_outside_verified_source", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        replayed_child = "\n".join(parsed_lines[child_start - 1:child_end]).strip()
        indexed_child = str(child.get("text", "")).strip()
        if replayed_child != indexed_child:
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["child_body_differs_from_source_parser_replay"] = diagnostics["fallback_reason"].get("child_body_differs_from_source_parser_replay", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        span = _native_parent_span(lines, child)
        if span is None:
            # 短小无章节文本可以whole-file作为自然父单位；大文件没有可靠边界就回退child。
            if len(text) <= config.context_char_budget:
                start_line, end_line, parent_source = 1, max(1, len(lines)), "verified_whole_source_file"
            else:
                diagnostics["too_large"] += 1
                diagnostics["fallback_child"] += 1
                diagnostics["fallback_reason"]["no_source_native_parent_within_budget"] = diagnostics["fallback_reason"].get("no_source_native_parent_within_budget", 0) + 1
                parents.append({**child, "parent_kind": "child_fallback"})
                continue
        else:
            start_line, end_line, parent_source = span
        parent_text = "\n".join(parsed_lines[start_line - 1:end_line])
        if len(parent_text) > config.context_char_budget:
            diagnostics["too_large"] += 1
            diagnostics["fallback_child"] += 1
            diagnostics["fallback_reason"]["source_native_parent_exceeds_budget"] = diagnostics["fallback_reason"].get("source_native_parent_exceeds_budget", 0) + 1
            parents.append({**child, "parent_kind": "child_fallback"})
            continue
        parent_identity = (index.target_id, source_record.relative, start_line, end_line)
        existing_parent = seen.get(parent_identity)
        provenance = {
            "chunk_id": child.get("chunk_id"),
            "source_path": child.get("source_path"),
            "start_line": child.get("start_line"),
            "end_line": child.get("end_line"),
        }
        if existing_parent is not None:
            existing_parent["child_provenance"].append(provenance)
            diagnostics["duplicate_source"] += 1
            continue
        parent = {
            key: child.get(key)
            for key in (
                "library", "target_id", "version", "logical_document_id", "rerank_score",
                "rrf_score", "ranks", "original_scores",
            )
            if key in child
        }
        parent.update({
            "chunk_id": f"parent:{index.target_id}:{source_record.relative}:{start_line}-{end_line}",
            "parent_kind": parent_source,
            "child_chunk_id": child.get("chunk_id"),
            "child_locator": {
                "source_path": child.get("source_path"),
                "start_line": child.get("start_line"),
                "end_line": child.get("end_line"),
            },
            "source_path": child.get("source_path"),
            "start_line": start_line,
            "end_line": end_line,
            "text": parent_text,
            "text_sha256": _sha256(parent_text.encode("utf-8")),
            "source_sha256": source_record.raw_sha256,
            "parent_locator": {"source_path": child.get("source_path"), "start_line": start_line, "end_line": end_line},
            "child_provenance": [provenance],
        })
        seen[parent_identity] = parent
        parents.append(parent)
        diagnostics["expanded"] += 1
    return parents, diagnostics


def _embedding_vector(embedder: Any, text: str) -> list[float]:
    method = getattr(embedder, "get_text_embedding", None)
    if method is None:
        raise TypeError("当前Embedding实现不支持MMR文档向量接口")
    value = method(text)
    vector = [float(item) for item in value]
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("MMR文档向量包含NaN或Inf")
    return vector


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("MMR候选向量维度不一致")
    if not all(math.isfinite(item) for item in (*left, *right)):
        raise ValueError("MMR向量包含NaN或Inf")
    left_norm = math.sqrt(math.fsum(item * item for item in left))
    right_norm = math.sqrt(math.fsum(item * item for item in right))
    if not left_norm or not right_norm:
        return 0.0
    return math.fsum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


def _mmr_select(router: Any, query: str, candidates: list[dict[str, Any]], config: RouteConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    started = time.perf_counter()
    texts = [candidate_document(item) for item in candidates]
    with router.search_lock:
        doc_started = time.perf_counter()
        batch_method = getattr(router.embedder, "get_text_embedding_batch", None)
        if callable(batch_method) and texts:
            vectors = [[float(value) for value in vector] for vector in batch_method(texts)]
            if any(not all(math.isfinite(value) for value in vector) for vector in vectors):
                raise ValueError("MMR文档向量包含NaN或Inf")
        else:
            vectors = [_embedding_vector(router.embedder, text) for text in texts]
        doc_seconds = round(time.perf_counter() - doc_started, 6)
        query_method = getattr(router.embedder, "get_query_embedding", None)
        if query_method is None:
            raise TypeError("当前Embedding实现不支持MMR query向量接口")
        query_started = time.perf_counter()
        query_vector = [float(item) for item in query_method(query)]
        if not all(math.isfinite(item) for item in query_vector):
            raise ValueError("MMR query向量包含NaN或Inf")
        query_seconds = round(time.perf_counter() - query_started, 6)
    if len(vectors) != len(candidates):
        raise ValueError("MMR候选向量数不匹配")
    relevance = [_cosine(query_vector, vector) for vector in vectors]
    remaining = set(range(len(candidates)))
    chosen: list[int] = []
    while remaining and len(chosen) < max(config.top_k, config.rerank_pool):
        best_index = max(
            remaining,
            key=lambda index: (
                _MMR_LAMBDA * relevance[index]
                - (1 - _MMR_LAMBDA) * max((_cosine(vectors[index], vectors[other]) for other in chosen), default=0.0),
                -index,
            ),
        )
        chosen.append(best_index)
        remaining.remove(best_index)
    diversified = []
    for rank, index in enumerate(chosen, start=1):
        item = dict(candidates[index])
        item["mmr_score"] = round(
            _MMR_LAMBDA * relevance[index]
            - (1 - _MMR_LAMBDA) * max((_cosine(vectors[index], vectors[other]) for other in chosen[: rank - 1]), default=0.0),
            8,
        )
        item["mmr_rank"] = rank
        diversified.append(item)
    selected = _select_candidates(diversified, config)
    return selected, {
        "document_reembedding_same_model_seconds": doc_seconds,
        "query_embedding_seconds": query_seconds,
        "selection_seconds": round(time.perf_counter() - started - doc_seconds - query_seconds, 6),
        "vector_source": "same_configured_embedding_model_reencoded",
    }


def _multi_query(
    router: Any, query: str, row: Mapping[str, Any], config: RouteConfig, rewriter: Any | None
) -> tuple[dict[str, Any], str | None, list[str]]:
    rewrite_error = None
    queries = [query]
    if rewriter is not None:
        started = time.perf_counter()
        try:
            result = rewriter.rewrite(query, corpus_hint=str(row.get("library", "software documentation")), max_subqueries=3)
            queries = result.retrieval_queries(include_subqueries=True)[:_MAX_QUERIES]
        except Exception as error:
            rewrite_error = type(error).__name__
        rewrite_seconds = round(time.perf_counter() - started, 6)
    else:
        rewrite_seconds = 0.0
    if not queries:
        queries = [query]
    # 每个query分配总候选槽预算；只在query集合内部融合，不读取reference facts。
    # 每个query的候选数用商和余数分配，dense/BM25总槽位严格不超过baseline。
    queries = queries[:config.candidate_k]
    slots = [config.candidate_k // len(queries) + (1 if i < config.candidate_k % len(queries) else 0)
             for i in range(len(queries))]
    raw_router = MultiCorpusRouter(
        qdrant_client=router.qdrant_client,
        embedder=router.embedder,
        indices=router.indices,
        reranker=None,
        dense_search=router.dense_search,
        keyword_search=router.keyword_search,
        search_lock=router.search_lock,
    )
    query_results = []
    query_times = []
    for index, subquery in enumerate(queries):
        started = time.perf_counter()
        local_config = replace(
            config,
            candidate_k=slots[index],
            top_k=min(config.top_k, slots[index]),
            rerank_pool=max(config.top_k, min(config.rerank_pool, 200)),
        )
        result = raw_router.retrieve(
            subquery,
            library=row.get("library"), version=row.get("version"), target_id=row.get("target_id"),
            config=local_config,
        )
        query_times.append(round(time.perf_counter() - started, 6))
        query_results.append((index, result))
    result_sets: dict[str, list[dict[str, Any]]] = {}
    per_query = []
    for index, result in query_results:
        per_query.append({"query": queries[index], "dense": result["dense_candidates"], "bm25": result["bm25_candidates"]})
        for route_name, key in (("dense", "dense_candidates"), ("bm25", "bm25_candidates")):
            candidates = result[key]
            if candidates:
                result_sets[f"q{index}:{route_name}"] = candidates
    fused = rrf_fuse(result_sets, k=config.rrf_k, identity=fuse_identity)[:config.rerank_pool]
    rerank_started = time.perf_counter()
    if router.reranker is not None and fused:
        docs = []
        statuses: dict[str, int] = {}
        by_target = {index.target_id: index for index in router.indices}
        for candidate in fused:
            routed = by_target[candidate["target_id"]]
            if routed.source_context_cache is None:
                docs.append(candidate_document(candidate))
            else:
                document, status = routed.source_context_cache.document_for_candidate(candidate)
                docs.append(document)
                statuses[status] = statuses.get(status, 0) + 1
        with router.search_lock:
            scores = router.reranker.score(query, docs)
        ranked = rank_candidates(fused, scores)
    else:
        ranked, statuses = fused, {}
    return ({
        "dense_candidates": [item for _idx, result in query_results for item in result["dense_candidates"]],
        "bm25_candidates": [item for _idx, result in query_results for item in result["bm25_candidates"]],
        "fused_candidates": fused,
        "ranked_candidates": ranked,
        "query_results": per_query,
        "timing_seconds": {
            "rewrite": rewrite_seconds,
            "per_query_retrieval": query_times,
            "rerank": round(time.perf_counter() - rerank_started, 6),
            "total": round(sum(query_times) + rewrite_seconds + time.perf_counter() - rerank_started, 6),
        },
        "source_context_diagnostics": statuses,
        "reranker_used": router.reranker is not None,
    }, rewrite_error, queries)


def _arm_result(profile: str, query: str, retrieval: dict[str, Any], router: Any, config: RouteConfig) -> dict[str, Any]:
    ranked = list(retrieval.get("ranked_candidates", []))
    diagnostics = None
    extra_started = time.perf_counter()
    if profile == "parent_child":
        expanded, diagnostics = _parent_documents(router, ranked[:config.rerank_pool], config)
        selected = _select_candidates(expanded, config)
        ranked_for_output = expanded
    elif profile == "mmr":
        selected, mmr_timing = _mmr_select(router, query, ranked[:config.rerank_pool], config)
        retrieval.setdefault("timing_seconds", {}).update(mmr_timing)
        ranked_for_output = ranked
    else:
        selected = _select_candidates(ranked, config)
        ranked_for_output = ranked
    timing = dict(retrieval.get("timing_seconds", {}))
    base_seconds = float(timing.get("total", timing.get("runner_total", 0.0)))
    if profile == "parent_child":
        extra_seconds = round(time.perf_counter() - extra_started, 6)
        timing["parent_expansion_and_selection"] = extra_seconds
        timing["shared_baseline_retrieval_seconds"] = base_seconds
        timing["total"] = round(base_seconds + extra_seconds, 6)
    elif profile == "mmr":
        extra_seconds = round(
            float(timing.get("document_reembedding_same_model_seconds", 0.0))
            + float(timing.get("query_embedding_seconds", 0.0))
            + float(timing.get("selection_seconds", 0.0)),
            6,
        )
        timing["shared_baseline_retrieval_seconds"] = base_seconds
        timing["total"] = round(base_seconds + extra_seconds, 6)
    elif profile == "baseline":
        timing["total"] = base_seconds
    return {
        "timing_seconds": timing,
        "reranker_used": bool(retrieval.get("reranker_used")),
        "source_context_diagnostics": retrieval.get("source_context_diagnostics", {}),
        "query_count": len(retrieval.get("queries", [])),
        "rewrite_error": retrieval.get("rewrite_error"),
        "parent_diagnostics": diagnostics,
        "dense_candidates": [_candidate_record(item) for item in retrieval.get("dense_candidates", [])],
        "bm25_candidates": [_candidate_record(item) for item in retrieval.get("bm25_candidates", [])],
        "fused_candidates": [_candidate_record(item) for item in retrieval.get("fused_candidates", [])],
        "ranked_candidates": [_candidate_record(item) for item in ranked_for_output],
        "selected": [_candidate_record(item) for item in selected],
    }


def _summary(records: list[dict[str, Any]], profiles: tuple[str, ...], run_identity: Mapping[str, Any], assets_after: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, dict[str, float]] = {}
    for profile in profiles:
        cases = [record for record in records if profile in record.get("profiles", {})]
        per_case = [record["profiles"][profile] for record in cases]
        counts[profile] = {
            "cases": len(cases),
            "selected_evidence": sum(len(item.get("selected", [])) for item in per_case),
            "candidate_count": sum(len(item.get("ranked_candidates", [])) for item in per_case),
            "mean_total_seconds": round(
                sum(float(item.get("timing_seconds", {}).get("total", 0.0)) for item in per_case) / max(1, len(per_case)), 6
            ),
        }
    return {
        "schema_version": 1,
        "status": "completed",
        "case_count": len(records),
        "case_ids": [record["case_id"] for record in records],
        "valid_case_ids": sorted(record["case_id"] for record in records),
        "run_identity": dict(run_identity),
        "assets_before": dict(run_identity.get("assets", {})),
        "assets_after": dict(assets_after),
        "assets_unchanged": dict(run_identity.get("assets", {})) == dict(assets_after),
        "profiles": counts,
    }


def run_retrieval_experiment(
    *,
    rows: list[dict[str, Any]],
    router: Any,
    config: RouteConfig,
    rewriter: Any | None,
    checkpoint_dir: Path,
    emit_case: Callable[[dict[str, Any]], None],
    progress: Callable[[int, int, str, str], None],
    should_cancel: Callable[[], bool],
    run_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """顺序执行已启用策略臂，断点必须与创建时的题目和资产身份完全一致。"""
    config.validate()
    profiles = tuple(profile for profile in run_identity.get("profiles", _PROFILES) if profile in _PROFILES)
    if not profiles or len(set(profiles)) != len(profiles):
        raise ValueError("策略profile为空或重复")
    checkpoint_dir = Path(checkpoint_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    ids = [str(row.get("case_id") or row.get("id") or "") for row in rows]
    if not rows or any(not _CASE_ID.fullmatch(case_id) for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("题集case_id为空、重复或格式无效")
    run_hash = run_identity_sha256(run_identity)
    records: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=1):
        case_id = str(row.get("case_id") or row.get("id"))
        if should_cancel():
            break
        destination = checkpoint_dir / f"{case_id}.json"
        existing = validate_case_checkpoint(destination, row=row, run_identity=run_identity)
        if existing is not None:
            records.append(existing)
            progress(position, len(rows), case_id, "checkpoint_reused")
            continue
        query = str(row.get("query", "")).strip()
        if not query:
            raise ValueError(f"題目缺少query：{case_id}")
        profile_results: dict[str, Any] = {}
        baseline = _timed_retrieve(router, query, row, config)
        if "baseline" in profiles:
            profile_results["baseline"] = _arm_result("baseline", query, baseline, router, config)
        if "parent_child" in profiles:
            profile_results["parent_child"] = _arm_result("parent_child", query, baseline, router, config)
        rewrite_error = None
        if "multi_query" in profiles:
            multi, rewrite_error, queries = _multi_query(router, query, row, config, rewriter)
            multi["queries"] = queries
            multi["rewrite_error"] = rewrite_error
            result = _arm_result("multi_query", query, multi, router, config)
            result["queries"] = queries
            result["rewrite_error"] = rewrite_error
            profile_results["multi_query"] = result
        if "mmr" in profiles:
            profile_results["mmr"] = _arm_result("mmr", query, baseline, router, config)
        record = {
            "schema_version": 1,
            "case_id": case_id,
            "case_identity": case_identity(row),
            "run_identity_sha256": run_hash,
            "query": query,
            "library": row.get("library"),
            "version": row.get("version"),
            "target_id": row.get("target_id"),
            "profiles": profile_results,
        }
        record["record_sha256"] = checkpoint_record_hash(record)
        emit_case(record)
        records.append(record)
        progress(position, len(rows), case_id, "case_complete")
    # 运行中如有assets改变，本次结果保留但不能汇总为可比实验。
    verify = getattr(router, "verify_run_identity", None)
    assets_after = verify() if callable(verify) else dict(run_identity.get("assets", {}))
    expected_cases = set(ids)
    valid_cases = {record["case_id"] for record in records}
    assets_unchanged = dict(run_identity.get("assets", {})) == dict(assets_after)
    if not assets_unchanged:
        raise ValueError("运行期间检索资产指纹发生变化，拒绝产出可比summary")
    if valid_cases != expected_cases:
        return {
            "schema_version": 1, "status": "incomplete", "case_count": len(records),
            "expected_case_count": len(rows), "run_identity": dict(run_identity),
            "valid_case_ids": sorted(valid_cases),
            "assets_before": dict(run_identity.get("assets", {})), "assets_after": dict(assets_after),
            "assets_unchanged": assets_unchanged,
        }
    summary = _summary(records, profiles, run_identity, assets_after)
    if summary["case_count"] != len(rows) or not summary["assets_unchanged"]:
        raise ValueError("summary checkpoint/asset一致性不满足完成条件")
    return summary
