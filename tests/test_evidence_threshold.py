from evaluation.evaluate_evidence_threshold import (
    candidate_thresholds,
    classification_at_threshold,
)


def test_threshold_metrics_count_confusion_matrix():
    rows = [
        {"target_supported": True, "top_score": 0.9},
        {"target_supported": False, "top_score": 0.8},
        {"target_supported": False, "top_score": 0.2},
    ]
    result = classification_at_threshold(rows, 0.85)
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (1, 0, 0, 2)
    assert result["balanced_accuracy"] == 1.0


def test_candidate_thresholds_include_midpoints():
    rows = [{"top_score": 0.2}, {"top_score": 0.8}]
    assert candidate_thresholds(rows) == [0.0, 0.5, 1.0]
