from pathlib import Path

from evaluation.run_frozen_eval import DATASET, load_and_validate_dataset


def test_frozen_dataset_evidence_is_in_current_manifest():
    seeds, fingerprint = load_and_validate_dataset(DATASET)
    assert len(seeds) == 8
    assert len(fingerprint) == 64


def test_frozen_dataset_is_distinct_from_dev_evidence():
    import json

    dev_path = Path(__file__).resolve().parents[1] / "corpora/osg-3.6.5/dev-seed.jsonl"
    dev = [json.loads(line) for line in dev_path.read_text(encoding="utf-8").splitlines()]
    frozen, _ = load_and_validate_dataset(DATASET)
    dev_spans = {(s["path"], s["start_line"], s["end_line"])
                 for question in dev for s in question["evidence"]}
    frozen_spans = {(s["path"], s["start_line"], s["end_line"])
                    for question in frozen for s in question["evidence"]}
    assert not dev_spans & frozen_spans
