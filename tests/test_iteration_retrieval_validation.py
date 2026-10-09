"""五库迭代runner的CPU合同：锁题盲选、来源定位proxy与可恢复记录。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation.experiments import run_iteration_retrieval_validation as runner


def _span(start: int, end: int, *, path: str = "raw_data/lib/1/source.html") -> dict:
    return {
        "path": path,
        "library": "lib",
        "version": "1",
        "locator": {"start_line": start, "end_line": end},
    }


def _row() -> dict:
    return {
        "case_id": "case-1",
        "query": "按指定版本解释A与B。",
        "library": "lib",
        "version": "1",
        "expected_status": "supported",
        "source_groups": [
            {"source_group_id": "joint", "source_spans": [_span(10, 12), _span(20, 20)]},
            {"source_group_id": "alternative", "source_spans": [_span(30, 30)]},
        ],
        "reference_facts": [
            {"fact_id": "fact-1", "supporting_source_group_ids": ["joint", "alternative"]}
        ],
    }


def _candidate(start: int, end: int, *, path: str = "raw_data/lib/1/source.html") -> dict:
    return {
        "chunk_id": f"{path}:{start}-{end}",
        "source_path": path,
        "start_line": start,
        "end_line": end,
        "page_number": None,
        "text": f"complete source lines {start}-{end}",
    }


def test_locator_proxy_treats_source_groups_as_or_and_spans_as_and() -> None:
    row = _row()
    # 两个chunk联合覆盖joint组的第一段，另一required span仍缺失；
    # alternative组完整，因此fact通过替代组定位proxy覆盖。
    candidates = [_candidate(10, 11), _candidate(12, 12), _candidate(30, 30)]

    result = runner.locator_coverage_proxy(candidates, row)

    assert result["source_groups_total"] == 2
    assert result["source_groups_locator_proxy_covered"] == 1
    assert result["reference_facts_total"] == 1
    assert result["reference_facts_locator_proxy_covered"] == 1
    assert result["kind"] == "deterministic_locator_proxy_not_semantic_judgment"


@pytest.mark.parametrize(
    "ranges",
    [
        [(10, 10), (12, 12)],  # 缺一行，不得把两个overlap误说成完整来源跨度
        [(9, 10)],             # 只碰到span左边界
        [(12, 14)],            # 只碰到span右边界
    ],
)
def test_locator_proxy_requires_full_contiguous_union_of_span_lines(ranges) -> None:
    row = {
        "source_groups": [{"source_group_id": "one", "source_spans": [_span(10, 12)]}],
        "reference_facts": [],
    }
    candidates = [_candidate(start, end) for start, end in ranges]

    result = runner.locator_coverage_proxy(candidates, row)

    assert result["source_groups_locator_proxy_covered"] == 0


def test_locator_proxy_accepts_adjacent_chunks_that_cover_the_entire_span() -> None:
    row = {"source_groups": [{"source_group_id": "one", "source_spans": [_span(10, 12)]}],
           "reference_facts": []}

    result = runner.locator_coverage_proxy([_candidate(10, 11), _candidate(12, 12)], row)

    assert result["source_groups_locator_proxy_covered"] == 1


def test_pdf_locator_requires_physical_page_and_uses_page_local_line_ranges() -> None:
    row = {
        "source_groups": [{
            "source_group_id": "pdf-page-lines",
            "source_spans": [{
                "path": "raw_data/lib/1/manual.pdf",
                "library": "lib",
                "version": "1",
                "locator": {"page": 7, "page_start_line": 4, "page_end_line": 6},
            }],
        }],
        "reference_facts": [],
    }
    on_wrong_page = {"source_path": "raw_data/lib/1/manual.pdf", "page_number": 8,
                     "start_line": 4, "end_line": 6, "text": "wrong physical page"}
    partial_page_range = {**on_wrong_page, "page_number": 7, "start_line": 6, "end_line": 8}

    wrong_page = runner.locator_coverage_proxy([on_wrong_page], row)
    partial = runner.locator_coverage_proxy([partial_page_range], row)

    assert wrong_page["source_groups_locator_proxy_covered"] == 0
    assert partial["source_groups_locator_proxy_covered"] == 0
    full = runner.locator_coverage_proxy(
        [{**partial_page_range, "start_line": 4, "end_line": 5},
         {**partial_page_range, "start_line": 6, "end_line": 6}], row,
    )
    assert full["source_groups_locator_proxy_covered"] == 1


def test_pdf_chunk_retention_uses_physical_page_and_page_local_lines(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    database_path = tmp_path / "chunks.sqlite"
    with sqlite3.connect(database_path) as db:
        db.execute("CREATE TABLE chunks (source_path TEXT, logical_document_id TEXT, source_raw_sha256 TEXT, page_number INTEGER, start_line INTEGER, end_line INTEGER)")
        db.executemany(
            "INSERT INTO chunks VALUES (?, 'corp:source.pdf', 'raw-hash', ?, ?, ?)",
            [
                ("raw_data/lib/1/manual.pdf", 7, 1, 5),
                ("raw_data/lib/1/manual.pdf", 7, 6, 10),
                ("raw_data/lib/1/manual.pdf", 8, 4, 8),
            ],
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        '{"corpus_id":"corp","files":[{"path":"source.pdf","raw_sha256":"raw-hash"}]}',
        encoding="utf-8",
    )
    index = SimpleNamespace(
        sources=SimpleNamespace(match_chunk_path=lambda path: SimpleNamespace(relative="source.pdf")),
        target=SimpleNamespace(manifest_path="manifest.json"),
        bm25_path=database_path,
    )
    span = {"source_id": "corp:source.pdf", "path": "raw_data/lib/1/manual.pdf",
            "manifest_relative_path": "source.pdf", "source_sha256": "raw-hash",
            "locator": {"page": 7, "page_start_line": 4, "page_end_line": 6}}

    present, retained = runner._span_presence_in_chunks(index, span)

    assert present is True
    assert retained is True


def test_source_presence_uses_production_path_and_raw_hash_when_bm25_ids_are_merged(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    source_path = "raw_data/ogc-3dtiles/1.1/22-025r4.html"
    raw_hash = "actual-source-raw-sha"
    database_path = tmp_path / "chunks.sqlite"
    with sqlite3.connect(database_path) as db:
        db.execute(
            "CREATE TABLE chunks (source_path TEXT, logical_document_id TEXT, "
            "source_raw_sha256 TEXT, page_number INTEGER, start_line INTEGER, end_line INTEGER)"
        )
        db.executemany(
            "INSERT INTO chunks VALUES (?, 'ogc:22-025r4', ?, NULL, ?, ?)",
            # 正确原文的chunk覆盖不完整；错误hash的合并文档ID恰好覆盖完整。
            # presence必须取正确source_path+原文hash，不能靠logical_document_id或同path任一版本误判。
            [(source_path, raw_hash, 1730, 1735),
             (source_path, raw_hash, 1740, 1742),
             (source_path, "different-source-hash", 1730, 1740)],
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"corpus_id": "ogc-3dtiles-1.1", "files": [
            {"path": "22-025r4.html", "raw_sha256": raw_hash}
        ]}), encoding="utf-8"
    )
    index = SimpleNamespace(
        sources=SimpleNamespace(match_chunk_path=lambda path: SimpleNamespace(relative="22-025r4.html")),
        target=SimpleNamespace(manifest_path="manifest.json"),
        bm25_path=database_path,
    )
    span = {
        "source_id": "ogc-3dtiles-1.1:22-025r4.html",
        "path": source_path,
        "manifest_relative_path": "22-025r4.html",
        "source_sha256": raw_hash,
        "locator": {"start_line": 1734, "end_line": 1738},
    }

    present, retained = runner._span_presence_in_chunks(index, span)

    assert present is True
    assert retained is False


def test_case_identity_binds_dataset_route_controls_and_model() -> None:
    row = _row()
    args = {"dataset_sha256": "d1", "route_sha256": "r1", "controls": {"top_k": 5}, "model": {"id": "m1"}}
    identity = runner.build_case_identity(row, **args)

    assert runner.build_case_identity(row, **args) == identity
    assert runner.build_case_identity(row, **{**args, "dataset_sha256": "d2"}) != identity
    assert runner.build_case_identity(row, **{**args, "route_sha256": "r2"}) != identity
    assert runner.build_case_identity(row, **{**args, "model": {"id": "m2"}}) != identity
    assert runner.build_case_identity(
        row, **args, runtime_fingerprint={"code_sha256": {"source_context.py": "c1"}}
    ) != identity


def test_checkpoint_roundtrip_checks_identity_hash_and_payload(tmp_path: Path) -> None:
    path = tmp_path / "case.json"
    record = {"case_identity": "identity-1", "case_id": "case-1", "stages": {"selected": []}}

    runner.write_case_checkpoint(path, record)
    recovered = runner.load_case_checkpoint(path, expected_identity="identity-1")

    assert recovered is not None
    assert recovered["record_sha256"] == runner.checkpoint_sha256(recovered)
    with pytest.raises(ValueError, match="identity mismatch"):
        runner.load_case_checkpoint(path, expected_identity="identity-2")

    payload = path.read_text(encoding="utf-8").replace('"case-1"', '"case-2"')
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="record hash mismatch"):
        runner.load_case_checkpoint(path, expected_identity="identity-1")


@pytest.mark.parametrize(
    ("presence", "fused", "ranked", "selected", "expected"),
    [
        ([(False, False)], 0, 0, 0, "source_absent"),
        ([(True, False)], 0, 0, 0, "chunk_retention"),
        ([(True, True)], 0, 0, 0, "retrieval"),
        ([(True, True)], 1, 0, 0, "rerank"),
        ([(True, True)], 1, 1, 0, "budget"),
        ([(True, True)], 1, 1, 1, None),
    ],
)
def test_locator_failure_classification_uses_stage_locators_not_query_words(
    presence, fused, ranked, selected, expected
) -> None:
    row = {"source_groups": [{"source_group_id": "one", "source_spans": [_span(10, 10)]}]}
    source_presence = [presence]
    def proxy(count):
        return {"source_groups_locator_proxy_covered": count}
    stages = {"fused": proxy(fused), "ranked": proxy(ranked), "selected": proxy(selected)}

    assert runner.classify_locator_failure(row, source_presence=source_presence, stage_proxies=stages) == expected


def test_negative_case_does_not_claim_automatic_refusal() -> None:
    row = {
        "case_id": "negative-1", "query": "某库中不存在的概念是什么？",
        "library": "lib", "version": "1", "expected_status": "unsupported",
        "service_behavior": "not_generated", "source_groups": [], "reference_facts": [],
    }
    retrieval = {
        "fused_candidates": [], "dense_candidates": [], "bm25_candidates": [],
        "routed_libraries": [], "bm25_terms": [], "per_library": [],
        "timing_seconds": {},
    }
    router = SimpleNamespace(retrieve=lambda *args, **kwargs: retrieval, indices=[])
    reranker = SimpleNamespace(score=lambda query, frames: [])
    config = SimpleNamespace(top_k=5, context_char_budget=8000, candidate_k=20,
                             rrf_k=60, rerank_pool=30)

    record = runner.paired_case(router, row, reranker=reranker, config=config, case_identity="id")

    assert record["no_answer_behavior"] == "not_evaluated"
    assert record["expected_status"] == "unsupported"
    assert record["coverage_locator_proxy"]["selected"]["reference_facts_total"] == 0
    assert record["diagnosis"]["semantic_review"] == "not_performed"


@pytest.mark.parametrize(
    ("failure_phase", "expected_phase"),
    [("retrieve", "retrieval"), ("rerank", "rerank"), ("budget", "budget_selection")],
)
def test_runtime_failures_are_tagged_by_pipeline_stage_without_query_overlap(
    failure_phase, expected_phase, monkeypatch
) -> None:
    candidate = {
        **_candidate(10, 10), "chunk_id": "one", "target_id": "target-1",
        "library": "lib", "version": "1", "page_number": None,
    }
    retrieval = {
        "dense_candidates": [candidate], "bm25_candidates": [], "fused_candidates": [candidate],
        "routed_libraries": [], "bm25_terms": [], "per_library": [], "timing_seconds": {},
    }
    router = SimpleNamespace(indices=[SimpleNamespace(target_id="target-1", source_context_cache=None)])
    if failure_phase == "retrieve":
        router.retrieve = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("retrieve failed"))
    else:
        router.retrieve = lambda *args, **kwargs: retrieval

    class FakeReranker:
        def score(self, query, frames):
            if failure_phase == "rerank":
                raise RuntimeError("rerank failed")
            return [1.0]

    if failure_phase == "budget":
        from wenjin.retrieval import service
        monkeypatch.setattr(service, "select_context", lambda *args, **kwargs: [candidate] * 6)
    config = SimpleNamespace(candidate_k=20, rrf_k=60, rerank_pool=30, top_k=5, context_char_budget=8000)

    with pytest.raises(runner.IterationPhaseError) as error:
        runner.paired_case(router, _row() | {"source_groups": []}, reranker=FakeReranker(),
                           config=config, case_identity="id")

    assert error.value.phase == expected_phase
    assert failure_phase in error.value.cause.args[0]


@pytest.mark.parametrize(
    ("scores", "message"),
    [([1.0, 2.0], "count"), ([float("nan")], "non-finite"), ([float("inf")], "non-finite")],
)
def test_paired_case_rejects_bad_reranker_scores_as_rerank_phase(scores, message) -> None:
    candidate = {
        **_candidate(10, 10), "chunk_id": "one", "target_id": "target-1",
        "library": "lib", "version": "1", "page_number": None,
    }
    retrieval = {
        "dense_candidates": [candidate], "bm25_candidates": [], "fused_candidates": [candidate],
        "routed_libraries": [], "bm25_terms": [], "per_library": [], "timing_seconds": {},
    }
    router = SimpleNamespace(
        retrieve=lambda *args, **kwargs: retrieval,
        indices=[SimpleNamespace(target_id="target-1", source_context_cache=None)],
    )
    reranker = SimpleNamespace(score=lambda query, frames: scores)
    config = SimpleNamespace(candidate_k=20, rrf_k=60, rerank_pool=30, top_k=5, context_char_budget=8000)

    with pytest.raises(runner.IterationPhaseError) as error:
        runner.paired_case(router, _row() | {"source_groups": []}, reranker=reranker,
                           config=config, case_identity="bad-scores")

    assert error.value.phase == "rerank"
    assert message in str(error.value.cause)


def test_paired_case_uses_one_shared_retrieval_pool_and_keeps_full_selected_bodies(tmp_path: Path, monkeypatch) -> None:
    source_path = "raw_data/lib/1/source.html"
    first = {
        **_candidate(10, 10, path=source_path), "chunk_id": "first", "target_id": "target-1",
        "library": "osgearth", "version": "1", "page_number": None, "rrf_score": 0.02,
    }
    second = {
        **_candidate(20, 20, path=source_path), "chunk_id": "second", "target_id": "target-1",
        "library": "osgearth", "version": "1", "page_number": None, "rrf_score": 0.01,
    }
    database_path = tmp_path / "chunks.sqlite"
    with sqlite3.connect(database_path) as db:
        db.execute("CREATE TABLE chunks (source_path TEXT, page_number INTEGER, start_line INTEGER, end_line INTEGER)")
        db.executemany(
            "INSERT INTO chunks VALUES (?, NULL, ?, ?)",
            [(source_path, 10, 10), (source_path, 20, 20)],
        )
    index = SimpleNamespace(
        target_id="target-1", library="osgearth", version="1", bm25_path=database_path,
        sources=SimpleNamespace(match_chunk_path=lambda path: object()),
        source_context_cache=SimpleNamespace(
            document_for_candidate=lambda candidate: ("source context\n" + candidate["text"], "test-context")
        ),
    )
    retrieval_calls = []
    pool = [first, second]
    retrieval = {
        "dense_candidates": pool[:1], "bm25_candidates": pool[1:], "fused_candidates": pool,
        "routed_libraries": [{"library": "lib", "version": "1", "target_id": "target-1"}],
        "bm25_terms": ["identifier"], "per_library": [], "timing_seconds": {},
    }

    def retrieve(query, **kwargs):
        retrieval_calls.append((query, kwargs))
        return retrieval

    router = SimpleNamespace(retrieve=retrieve, indices=[index])
    presence_calls = []
    monkeypatch.setattr(
        runner, "_span_presence_in_chunks",
        lambda routed_index, span: (presence_calls.append((routed_index, span)) or (True, True)),
    )
    reranker_calls = []

    class SpyReranker:
        def score(self, query, frames):
            reranker_calls.append((query, list(frames)))
            return [0.1, 0.9]

    row = {
        "case_id": "paired", "query": "按指定版本说明。", "library": "osgEarth", "version": "1",
        "source_groups": [{"source_group_id": "gold-only", "source_spans": [
            {**_span(10, 10, path=source_path), "library": "osgEarth"}
        ]}],
        "reference_facts": [{"fact_id": "gold-fact", "supporting_source_group_ids": ["gold-only"]}],
    }
    config = SimpleNamespace(candidate_k=20, rrf_k=60, rerank_pool=30, top_k=5, context_char_budget=8000)

    record = runner.paired_case(router, row, reranker=SpyReranker(), config=config, case_identity="paired-id")

    assert len(retrieval_calls) == 1
    assert retrieval_calls[0][0] == row["query"]
    assert "source_groups" not in retrieval_calls[0][1]
    assert "reference_facts" not in retrieval_calls[0][1]
    assert len(presence_calls) == 1
    assert presence_calls[0][0] is index, "dataset display library须归一后接到唯一RoutedIndex"
    assert len(reranker_calls) == 1
    assert reranker_calls[0][0] == row["query"]
    assert all("gold-only" not in frame and "gold-fact" not in frame for frame in reranker_calls[0][1])
    assert record["retrieval"]["dense_bm25_fused_candidate_ids_identical_across_arms"] is True
    assert [item["chunk_id"] for item in record["stages"]["fused"]] == ["first", "second"]
    assert {item["chunk_id"] for item in record["stages"]["ranked"]} == {"first", "second"}
    selected = record["stages"]["selected"]
    assert len(selected) <= 5
    assert sum(len(item["text"]) for item in selected) <= 8000
    by_id = {item["chunk_id"]: item["text"] for item in pool}
    assert all(item["text"] == by_id[item["chunk_id"]] for item in selected)
    assert all(
        item["content_sha256"] == hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        for stage in record["stages"].values() for item in stage
    )


def test_asset_fingerprint_uses_actual_registry_path_and_tracks_manifest_sources(tmp_path: Path, monkeypatch) -> None:
    route_path = tmp_path / "route.json"
    manifest_path = tmp_path / "manifest.json"
    bm25_path = tmp_path / "bm25.sqlite"
    registry_path = tmp_path / "data/indexes/index-registry.json"
    source_path = tmp_path / "raw_data/lib/1/source.html"
    for path in (route_path, manifest_path, bm25_path, registry_path, source_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stable fixture")
    manifest_path.write_text(
        '{"raw_data_subdir":"lib/1","files":[{"path":"source.html","raw_sha256":"declared"}]}',
        encoding="utf-8",
    )
    route = {"candidates": [{"target_id": "target", "dense_collection": "collection",
                              "bm25_path": "bm25.sqlite", "manifest_path": "manifest.json",
                              "manifest_sha256": "declared"}]}
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    fingerprint = runner._asset_fingerprint(route_path, route)

    assert fingerprint["active_registry"]["exists"] is True
    assert fingerprint["active_registry"]["sha256"] == runner._sha256_file(registry_path)
    target = fingerprint["targets"][0]
    assert target["source_files"][0]["exists"] is True
    assert target["source_files"][0]["sha256"] == runner._sha256_file(source_path)


def test_locked_dataset_gate_accepts_scope_negative_without_refusal_claim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"corpus_id":"corp","raw_data_subdir":"lib/1","files":[]}', encoding="utf-8")
    manifest_sha = runner._sha256_file(manifest_path)
    row = {
        "case_id": "negative-1", "lock_state": "locked",
        "dataset_revision": "developer-validation-2026-10-04", "dataset_type": "developer_validation",
        "library": "lib", "version": "1", "query_family": "out_of_scope_version",
        "query": "請依照不存在於本語料庫版本範圍的文件說明。",
        "source_groups": [], "reference_facts": [], "expected_status": "unsupported",
        "service_behavior": "not_generated", "case_type": "negative_scope",
        "source_manifest_sha256": manifest_sha,
    }
    dataset_path = tmp_path / "developer-validation-2026-10-04.jsonl"
    payload = (runner.canonical_json_bytes(row) + b"\n")
    dataset_path.write_bytes(payload)
    dataset_path.with_suffix(".sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {dataset_path.name}\n", encoding="utf-8",
    )
    route = {"candidates": [{"library": "lib", "version": "1", "manifest_path": "manifest.json",
                              "manifest_sha256": manifest_sha}]}

    actual = runner._validate_locked_dataset(dataset_path, [row], route)

    assert actual == hashlib.sha256(payload).hexdigest()
    assert row["expected_status"] == "unsupported"
    assert row["service_behavior"] == "not_generated"


def test_summary_keeps_case_library_family_group_and_fact_denominators_separate() -> None:
    rows = [
        {"case_id": "a", "library": "lib-a", "query_family": "joint", "source_groups": [{}, {}],
         "reference_facts": [{}, {}], "expected_status": "supported"},
        {"case_id": "b", "library": "lib-a", "query_family": "negative", "source_groups": [],
         "reference_facts": [], "expected_status": "unsupported"},
    ]
    proxy = {
        "source_groups_total": 2,
        "source_groups_locator_proxy_covered": 1,
        "reference_facts_total": 2,
        "reference_facts_locator_proxy_covered": 1,
    }
    records = [{
        "case_id": "a", "library": "lib-a", "query_family": "joint",
        "no_answer_behavior": "not_generated",
        "diagnosis": {"locator_failure_stage": "retrieval", "source_groups_expected": 2,
                      "reference_facts_expected": 2},
        "coverage_locator_proxy": {
            stage: proxy for stage in ("dense", "bm25", "fused", "ranked", "baseline_selected", "selected")
        },
    }]

    summary = runner._summary(records, rows)

    assert summary["cases_total"] == 2
    assert summary["cases_completed"] == 1
    assert summary["semantic_quality"].startswith("not_assessed")
    assert summary["by_query_family"]["joint"]["cases"] == 1
    assert summary["by_query_family"]["joint"]["source_groups_denominator"] == 2
    assert summary["by_query_family"]["joint"]["reference_facts_denominator"] == 2
    assert summary["by_query_family"]["negative"]["unsupported_cases"] == 1
    assert summary["by_query_family"]["joint"]["completed_cases"] == 1
    assert summary["overall_locator_proxy_by_arm"]["selected"]["source_groups_numerator"] == 1
    assert summary["overall_locator_proxy_by_arm"]["selected"]["reference_facts_numerator"] == 1
