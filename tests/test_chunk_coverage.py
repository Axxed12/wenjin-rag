from scripts.evaluate_chunk_coverage import evaluate
from wenjin.ingestion.chunking import Chunk


def test_coverage_distinguishes_overlap_from_full_containment():
    chunks = [
        Chunk("a", "include/osg/Test", "3.6.5", 1, 3, "one", "fixed"),
        Chunk("b", "include/osg/Test", "3.6.5", 4, 6, "two", "fixed"),
    ]
    seed = [{"id": "q", "evidence": [{"path": "include/osg/Test", "start_line": 3, "end_line": 4}]}]
    result = evaluate(chunks, seed)
    assert result["all_evidence_spans_hit"] == 1
    assert result["all_evidence_spans_in_one_chunk"] == 0
