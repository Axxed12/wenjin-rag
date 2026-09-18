from evaluation.run_retrieval_eval import metrics

EVIDENCE = [
    {"path": "a", "start_line": 10, "end_line": 12},
    {"path": "b", "start_line": 20, "end_line": 21},
]


def test_metrics_distinguish_partial_and_complete_evidence():
    results = [
        {"source_path": "a", "start_line": 10, "end_line": 15},
        {"source_path": "x", "start_line": 1, "end_line": 2},
        {"source_path": "b", "start_line": 18, "end_line": 21},
    ]
    at_1 = metrics(results, EVIDENCE, k=1)
    at_3 = metrics(results, EVIDENCE, k=3)
    assert at_1["span_recall"] == 0.5 and not at_1["all_spans_hit"]
    assert at_3["span_recall"] == 1.0 and at_3["all_spans_hit"]
    assert at_3["reciprocal_rank"] == 1.0


def test_touching_only_part_of_evidence_does_not_count_as_hit():
    partial = [{"source_path": "a", "start_line": 11, "end_line": 15}]
    assert metrics(partial, [EVIDENCE[0]], k=1)["any_span_hit"] is False


def test_two_chunks_can_jointly_cover_one_source_span():
    evidence = [{"path": "a", "start_line": 10, "end_line": 15}]
    split = [
        {"source_path": "a", "start_line": 10, "end_line": 12},
        {"source_path": "a", "start_line": 13, "end_line": 15},
    ]
    at_one = metrics(split, evidence, k=1)
    at_two = metrics(split, evidence, k=2)
    assert at_one["span_recall"] == 0.0
    assert at_two["span_recall"] == 1.0
    assert at_two["single_chunk_span_rate"] == 0.0


def test_gap_between_two_chunks_is_not_full_coverage():
    evidence = [{"path": "a", "start_line": 10, "end_line": 15}]
    split = [
        {"source_path": "a", "start_line": 10, "end_line": 12},
        {"source_path": "a", "start_line": 14, "end_line": 15},
    ]
    assert metrics(split, evidence, k=2)["span_recall"] == 0.0
