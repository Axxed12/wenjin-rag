from __future__ import annotations

import json
import os

import pytest

from evaluation.compare_3dtiles_long_lines import _checkpoint_report, _resume_checkpoint


def _expected_report() -> dict:
    return {
        "status": "running",
        "experiment_scope": "supplemental_validation_only",
        "dataset": {"sha256": "frozen-sha", "count": 24},
        "controls": {"candidate_k": 20, "context_char_budget": 8000},
        "target_pair": {"baseline": "002", "candidate": "003"},
        "details": [],
    }


def _rows() -> list[dict]:
    return [{"id": f"q-{i:02d}", "query": f"query-{i}", "evidence": [{"line": i}]}
            for i in range(1, 25)]


def _detail(question_id: str) -> dict:
    source = next(row for row in _rows() if row["id"] == question_id)
    return {
        "input": {"id": question_id, "query": source["query"], "evidence": source["evidence"]},
        "arms": {"baseline": {"result": question_id}, "candidate_003": {"result": question_id}},
    }


def test_checkpoint_retries_transient_windows_permission_error(tmp_path) -> None:
    path = tmp_path / "paired-results.json"
    path.write_text('{"old": true}', encoding="utf-8")
    real_replace = os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary Windows sharing lock")
        real_replace(source, destination)

    report = _expected_report()
    report["details"] = [_detail("q-01")]
    _checkpoint_report(path, report, replace=flaky_replace)

    assert attempts == 3
    assert json.loads(path.read_text(encoding="utf-8"))["details"] == report["details"]
    assert not path.with_suffix(".tmp").exists()


def test_resume_keeps_completed_pairs_and_lists_only_remaining_questions(tmp_path) -> None:
    path = tmp_path / "paired-results.json"
    previous = _expected_report()
    previous["status"] = "failed"
    previous["failure"] = {"question_id": "q-17", "error_type": "PermissionError"}
    previous["details"] = [_detail(f"q-{i:02d}") for i in range(1, 17)]
    path.write_text(json.dumps(previous), encoding="utf-8")

    expected, completed, recovery = _resume_checkpoint(path, _expected_report(), _rows())

    assert completed == {f"q-{i:02d}" for i in range(1, 17)}
    assert expected["details"] == previous["details"]
    assert recovery["previous_failure"]["error_type"] == "PermissionError"
    assert recovery["remaining_question_ids"] == [f"q-{i:02d}" for i in range(17, 25)]


def test_resume_rejects_a_different_frozen_dataset(tmp_path) -> None:
    path = tmp_path / "paired-results.json"
    previous = _expected_report()
    previous["dataset"]["sha256"] = "different-sha"
    previous["details"] = []
    path.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(ValueError, match="dataset"):
        _resume_checkpoint(path, _expected_report(), _rows())


def test_resume_rejects_completed_run_instead_of_repeating_gpu_work(tmp_path) -> None:
    path = tmp_path / "paired-results.json"
    previous = _expected_report()
    previous["status"] = "completed"
    previous["details"] = [_detail(row["id"]) for row in _rows()]
    path.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(ValueError, match="完整结果"):
        _resume_checkpoint(path, _expected_report(), _rows())


@pytest.mark.parametrize("corruption", ["query", "evidence", "empty_arm"])
def test_resume_rejects_corrupted_question_or_arm_detail(tmp_path, corruption: str) -> None:
    path = tmp_path / "paired-results.json"
    previous = _expected_report()
    previous["details"] = [_detail("q-01")]
    detail = previous["details"][0]
    if corruption == "query":
        detail["input"]["query"] = "changed query"
    elif corruption == "evidence":
        detail["input"]["evidence"] = []
    else:
        detail["arms"]["candidate_003"] = {}
    path.write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(ValueError, match="不完整"):
        _resume_checkpoint(path, _expected_report(), _rows())
