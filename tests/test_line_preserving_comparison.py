"""paired validation对照的控制变量、来源位置与预算口径回归。"""

import json
from pathlib import Path

import pytest

from evaluation import compare_line_preserving_indexes as comparison
from wenjin.retrieval.service import select_context


def route_target(library: str, version: str, target_id: str) -> dict:
    slug = library.replace("-", "_")
    return {
        "target_id": target_id,
        "library": library,
        "version": version,
        "strategy": "blocks-split",
        "manifest_path": f"corpora/{library}-{version}/source-manifest.json",
        "manifest_sha256": "a" * 64,
        "dense_collection": f"{slug}_{version.replace('.', '_')}_dense",
        "bm25_path": f"data/indexes/{slug}_{version.replace('.', '_')}.sqlite",
        "chunk_count": 10,
        "vector_dimension": 8,
        "embedding_model": "synthetic-embedding",
        "canonical_corpus_sha256": "c" * 64,
        "normalization_profile": "text-newline-lf-v1",
    }


def test_only_unsealed_exact_50_question_validation_is_accepted():
    dataset = comparison.ROOT / "evaluation/datasets/rag-validation-001.jsonl"
    seals = comparison.ROOT / "artifacts/corpus-acceptance-001/dataset-seals.json"
    sha_file = comparison.ROOT / "evaluation/datasets/rag-validation-001.sha256"

    result = comparison.validate_validation_dataset(dataset, seals, sha_file)

    assert result["sha256"] == comparison._EXPECTED_VALIDATION_SHA256
    assert result["question_count"] == 50
    assert all(row["split"] == "validation" for row in result["rows"])


def test_frozen_test_path_is_rejected_before_reading_dataset_bytes(monkeypatch):
    dataset = comparison.ROOT / "evaluation/datasets/rag-test-001.jsonl"
    seals = comparison.ROOT / "artifacts/corpus-acceptance-001/dataset-seals.json"
    sha_file = comparison.ROOT / "evaluation/datasets/rag-test-001.sha256"
    original_read_bytes = Path.read_bytes
    dataset_reads = 0

    def guard_test_dataset(path: Path) -> bytes:
        nonlocal dataset_reads
        if path.resolve() == dataset.resolve():
            dataset_reads += 1
            raise AssertionError("frozen test bytes must not be read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guard_test_dataset)

    with pytest.raises(ValueError, match="仅允许"):
        comparison.validate_validation_dataset(dataset, seals, sha_file)
    assert dataset_reads == 0


def test_paired_route_replaces_only_the_3d_tiles_target(tmp_path):
    old_candidates = [
        route_target("osgearth", "3.8", "osgearth-3.8-target"),
        route_target("gdal", "3.13.1", "gdal-3.13.1-target"),
        route_target("proj", "9.8.1", "proj-9.8.1-target"),
        route_target("ogc-3dtiles", "1.1", "ogc-3dtiles-1.1-blocks-split-candidate-002"),
        route_target("ogc-wms", "1.3.0", "ogc-wms-1.3.0-target"),
    ]
    old_route_path = tmp_path / "old-route.json"
    old_route_path.write_text(json.dumps({"schema_version": 1, "candidates": old_candidates}),
                              encoding="utf-8")
    replacement = route_target(
        "ogc-3dtiles", "1.1", "ogc-3dtiles-1.1-blocks-split-candidate-003"
    )
    replacement["dense_collection"] = "tiles-003-dense"
    replacement["bm25_path"] = "data/indexes/tiles-003.sqlite"
    replacement["chunk_count"] = 989
    new_config_path = tmp_path / "tiles-003.json"
    new_config_path.write_text(json.dumps(replacement), encoding="utf-8")

    paired = comparison.validate_paired_routes(old_route_path, new_config_path)

    baseline = paired["baseline_route"]["candidates"]
    candidate = paired["candidate_route"]["candidates"]
    replaced = next(i for i, item in enumerate(baseline) if item["library"] == "ogc-3dtiles")
    assert sum(left != right for left, right in zip(baseline, candidate)) == 1
    assert candidate[replaced] == replacement
    assert [item for i, item in enumerate(baseline) if i != replaced] == [
        item for i, item in enumerate(candidate) if i != replaced
    ]
    assert paired["replaced_target"]["old"] == baseline[replaced]


def test_paired_route_rejects_embedding_or_corpus_identity_change(tmp_path):
    old_route_path = tmp_path / "old-route.json"
    old_candidates = [
        route_target("osgearth", "3.8", "earth"),
        route_target("gdal", "3.13.1", "gdal"),
        route_target("proj", "9.8.1", "proj"),
        route_target("ogc-3dtiles", "1.1", "tiles-old"),
        route_target("ogc-wms", "1.3.0", "wms"),
    ]
    old_route_path.write_text(json.dumps({"candidates": old_candidates}), encoding="utf-8")
    replacement = route_target(
        "ogc-3dtiles", "1.1", "ogc-3dtiles-1.1-blocks-split-candidate-003"
    )
    replacement["embedding_model"] = "different-model"
    new_path = tmp_path / "tiles-003.json"
    new_path.write_text(json.dumps(replacement), encoding="utf-8")

    with pytest.raises(ValueError, match="embedding_model"):
        comparison.validate_paired_routes(old_route_path, new_path)


def test_repeated_text_at_wrong_line_does_not_count_as_evidence():
    path = "raw_data/ogc-3dtiles/1.1/repeated.html"
    source = ["DUPLICATE\n", "THE EVIDENCE LINE\n", "DUPLICATE\n"]
    wrong_location = {
        "source_path": path, "start_line": 2, "end_line": 2,
        "page_number": None, "text": "DUPLICATE\n",
    }
    span = {"path": path, "start_line": 2, "end_line": 2, "page": None}

    result = comparison.contract_metrics(
        [wrong_location], [span], {(path, None): source}, k=5,
    )

    assert result["locator_metrics"]["span_recall"] == 1.0  # 仅行号口径会误报命中
    assert result["line_spans_hit"] == 0
    assert result["invalid_evidence_count"] == 1
    assert result["all_text_spans_hit"] is False


def test_character_cut_sharing_a_line_number_is_invalid_evidence():
    path = "raw_data/ogc-3dtiles/1.1/long-line.html"
    full_line = "LONG-LINE-EVIDENCE-" + "x" * 1800 + "\n"
    hard_cut = {
        "source_path": path, "start_line": 1, "end_line": 1,
        "page_number": None, "text": full_line[:1600],
    }
    span = {"path": path, "start_line": 1, "end_line": 1, "page": None}

    result = comparison.contract_metrics(
        [hard_cut], [span], {(path, None): [full_line]}, k=5,
    )

    assert result["locator_metrics"]["span_recall"] == 1.0
    assert result["invalid_evidence_count"] == 1
    assert result["line_spans_hit"] == 0
    assert result["all_text_spans_hit"] is False


def test_valid_full_slice_and_blank_line_union_are_counted():
    path = "raw_data/ogc-3dtiles/1.1/complete.html"
    lines = ["first evidence\n", "\n", "last evidence\n"]
    candidates = [{
        "source_path": path, "start_line": 1, "end_line": 2,
        "page_number": None, "text": "".join(lines[:2]),
    }, {
        "source_path": path, "start_line": 3, "end_line": 3,
        "page_number": None, "text": lines[2],
    }]
    span = {"path": path, "start_line": 1, "end_line": 3, "page": None}

    result = comparison.contract_metrics(
        candidates, [span], {(path, None): lines},
        blank_lines={path: frozenset({2})},
    )

    assert result["invalid_evidence_count"] == 0
    assert result["line_spans_total"] == 1
    assert result["line_spans_hit"] == 1
    assert result["all_text_spans_hit"] is True


def test_pdf_match_is_reported_as_page_presence_not_text_verification():
    path = "raw_data/ogc-3dtiles/1.1/spec.pdf"
    page_lines = ["Rubric text is absent from this page body\n"]
    candidate = {
        "source_path": path, "page_number": 7,
        "start_line": 1, "end_line": 1, "text": "unrelated page text\n",
    }
    span = {"path": path, "page": 7, "start_line": None, "end_line": None}

    result = comparison.contract_metrics(
        [candidate], [span], {(path, 7): page_lines}, k=5,
    )

    assert result["line_spans_total"] == 0
    assert result["page_spans_total"] == 1
    assert result["page_spans_present"] == 1
    assert result["all_text_spans_hit"] is None
    assert result["all_locator_spans_hit"] is True
    assert result["details"][0]["level"] == "page"
    assert result["invalid_evidence_count"] == 0


def test_page_level_only_candidate_cannot_cover_a_text_span():
    path = "raw_data/ogc-3dtiles/1.1/spec.html"
    source = ["actual line evidence\n"]
    page_level_candidate = {
        "source_path": path, "page_number": 3,
        "start_line": 1, "end_line": 1, "text": "unverified page payload\n",
    }
    span = {"path": path, "start_line": 1, "end_line": 1, "page": None}

    result = comparison.contract_metrics(
        [page_level_candidate], [span],
        {(path, None): source, (path, 3): ["different PDF parser text\n"]},
    )

    assert result["line_spans_total"] == 1
    assert result["line_spans_hit"] == 0
    assert result["all_text_spans_hit"] is False


def test_cross_library_span_outside_the_selected_target_remains_a_miss():
    tiles = "raw_data/ogc-3dtiles/1.1/tiles.html"
    gdal = "raw_data/gdal/3.13.1/overview.md"
    candidate = {
        "source_path": tiles, "start_line": 1, "end_line": 1,
        "page_number": None, "text": "tile evidence\n",
    }
    spans = [
        {"path": tiles, "start_line": 1, "end_line": 1, "page": None},
        {"path": gdal, "start_line": 1, "end_line": 1, "page": None},
    ]
    source_map = {(tiles, None): ["tile evidence\n"], (gdal, None): ["gdal evidence\n"]}

    result = comparison.contract_metrics([candidate], spans, source_map)

    assert result["line_spans_total"] == 2
    assert result["line_spans_hit"] == 1
    assert result["all_text_spans_hit"] is False


def test_selected_context_metrics_are_recomputed_after_production_budget():
    big_path = "raw_data/ogc-3dtiles/1.1/large.html"
    small_path = "raw_data/gdal/3.13.1/small.md"
    large_text = "L" * 8001 + "\n"
    small_text = "small evidence\n"
    raw_candidates = [
        {"source_path": big_path, "start_line": 1, "end_line": 1,
         "page_number": None, "text": large_text},
        {"source_path": small_path, "start_line": 1, "end_line": 1,
         "page_number": None, "text": small_text},
    ]
    spans = [
        {"path": big_path, "start_line": 1, "end_line": 1, "page": None},
        {"path": small_path, "start_line": 1, "end_line": 1, "page": None},
    ]
    parsed = {(big_path, None): [large_text], (small_path, None): [small_text]}

    before_budget = comparison.contract_metrics(raw_candidates, spans, parsed, k=5)
    selected = select_context(raw_candidates, top_k=5, char_budget=8000)
    after_budget = comparison.contract_metrics(selected, spans, parsed, k=5)

    assert before_budget["line_spans_hit"] == 2
    assert [item["source_path"] for item in selected] == [small_path]
    assert after_budget["line_spans_total"] == 2
    assert after_budget["line_spans_hit"] == 1
    assert after_budget["all_text_spans_hit"] is False


def test_question_record_preserves_split_rubric_and_both_raw_arms():
    row = {
        "id": "validation-cross-lib-001", "split": "validation",
        "library": "ogc-3dtiles", "version": "1.1", "query": "question",
        "answer_rubric": "expected answer", "expected_status": "supported",
        "evidence": [{"path": "raw_data/gdal/3.13.1/a.md", "start_line": 1,
                      "end_line": 1, "page": None}],
        "evidence_policy": "listed spans jointly support the rubric",
    }
    route_args = {"library": "ogc-3dtiles", "version": "1.1"}
    baseline = {"stages": {"fused": [{"text": "raw baseline"}]}, "selected_context": []}
    candidate = {"stages": {"fused": [{"text": "raw candidate"}]}, "selected_context": []}

    result = comparison.make_question_record(row, route_args, baseline, candidate)

    assert result["input"]["split"] == "validation"
    assert result["input"]["answer_rubric"] == "expected answer"
    assert result["input"]["evidence"] == row["evidence"]
    assert result["arms"] == {"baseline": baseline, "candidate_003": candidate}
