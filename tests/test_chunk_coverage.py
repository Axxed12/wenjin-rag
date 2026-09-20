"""覆盖率口径：按来源位置合并，不按字符串去重。

两种口径结论可以不同，报告必须说清用的是哪一种。这里把三类构造固定下来：

1. **有重叠且完整**——重叠片段不能因为拼接重复文字就被判成缺口；
2. **中间确实缺字**——位置并集盖不满证据区间，必须报出来；
3. 弱口径（行号相交）在前两种情形下都会说"命中"，所以不能用它判断覆盖。
"""

import pytest

from scripts.evaluate_chunk_coverage import evaluate
from wenjin.coverage import evidence_coverage
from wenjin.ingestion.chunking import Chunk

PATH = "include/osg/Test"


def make(chunk_id: str, start: int, end: int, text: str) -> Chunk:
    return Chunk(chunk_id, PATH, "3.6.5", start, end, text, "fixed")


# --- 审核给出的最小反例：重叠片段拼接会重复文字 ---

OVERLAP_SOURCE = "A\nB\nC\nD\nE\n"
# 片段一覆盖 A B C，片段二覆盖 C D E，证据是 B C D——实际完整覆盖。
# 直接拼接得到 "A B C C D E"，"B C D" 不再是连续子串，旧实现判成未覆盖。
OVERLAP_CHUNKS = [
    make("one", 1, 3, "A\nB\nC\n"),
    make("two", 3, 5, "C\nD\nE\n"),
]


def test_overlapping_chunks_that_fully_cover_are_not_a_gap():
    verdict = evidence_coverage(OVERLAP_CHUNKS, OVERLAP_SOURCE.splitlines(keepends=True), 2, 4)
    assert verdict["covered"] is True, verdict
    assert verdict["missing_lines"] == []
    assert verdict["text_range_conflicts"] == []


def test_naive_concatenation_would_have_reported_a_false_gap():
    """把错误做法固定下来，避免以后有人"简化"回拼接实现。"""
    joined = "".join(chunk.text for chunk in OVERLAP_CHUNKS).replace("\n", " ")
    assert "B C D" not in joined, "拼接确实会重复重叠部分——这正是旧实现的缺陷"


def test_overlapping_chunks_are_reported_as_covered_by_the_script():
    seed = [{"id": "q", "evidence": [{"path": PATH, "start_line": 2, "end_line": 4}]}]
    result = evaluate(OVERLAP_CHUNKS, {PATH: OVERLAP_SOURCE}, seed)
    assert result["all_evidence_text_covered"] == 1


# --- 对照：中间确实缺字 ---

GAP_SOURCE = "l1\nl2\nAAA\nBBB\nl5\nl6\n"
# chunk a 覆盖第 1-3 行、chunk b 覆盖第 5-6 行；证据取第 3-5 行，中间缺了第 4 行。
GAP_CHUNKS = [
    make("a", 1, 3, "l1\nl2\nAAA\n"),
    make("b", 5, 6, "l5\nl6\n"),
]


def test_real_gap_is_still_reported():
    seed = [{"id": "q", "evidence": [{"path": PATH, "start_line": 3, "end_line": 5}]}]
    result = evaluate(GAP_CHUNKS, {PATH: GAP_SOURCE}, seed)
    assert result["all_evidence_text_covered"] == 0
    span = result["details"][0]["spans"][0]
    assert span["missing_lines"] == [4]


def test_gap_verdict_names_the_missing_lines():
    verdict = evidence_coverage(GAP_CHUNKS, GAP_SOURCE.splitlines(keepends=True), 3, 5)
    assert verdict["covered"] is False
    assert verdict["missing_lines"] == [4]


def test_line_intersection_says_hit_even_when_there_is_a_gap():
    """弱口径在缺字时也说"命中"——所以不能用它判断覆盖。"""
    seed = [{"id": "q", "evidence": [{"path": PATH, "start_line": 3, "end_line": 5}]}]
    result = evaluate(GAP_CHUNKS, {PATH: GAP_SOURCE}, seed)
    assert result["all_evidence_spans_hit"] == 1
    assert result["all_evidence_text_covered"] == 0


# --- 文本与声明的行范围对不上 ---

def test_chunk_text_from_another_range_is_flagged():
    """片段文本不是来自它声明的范围：一致性检查要报出来。"""
    chunks = [
        make("a", 1, 3, "l1\nl2\nAAA\n"),
        make("b", 4, 6, "完全不属于这里的文字\n"),
    ]
    verdict = evidence_coverage(chunks, GAP_SOURCE.splitlines(keepends=True), 4, 5)
    assert verdict["text_range_conflicts"] == ["b"]
    assert verdict["covered"] is False


def test_hard_split_pieces_are_not_a_conflict_but_also_not_coverage():
    """单行被硬切：片段不是冲突（它们确实来自该行），但**也不算整行覆盖**。

    `Chunk` 没有字符偏移量，无法证明这两片拼起来正好是该行（顺序、是否缺中间段都不可知），
    所以只能报"无法判定"。审核明确要求：不支持字符位置验证时返回未覆盖或不可判定。
    """
    chunks = [
        make("a", 3, 3, "AA"),
        make("b", 3, 3, "A"),
    ]
    verdict = evidence_coverage(chunks, GAP_SOURCE.splitlines(keepends=True), 3, 3)
    assert verdict["text_range_conflicts"] == []      # 不是冲突
    assert verdict["partial_line_chunks"] == ["a", "b"]
    assert verdict["undecidable_lines"] == [3]        # 但也不算覆盖
    assert verdict["covered"] is False


def test_empty_chunk_does_not_count_as_coverage():
    chunks = [make("a", 1, 6, "   ")]
    verdict = evidence_coverage(chunks, GAP_SOURCE.splitlines(keepends=True), 3, 5)
    assert verdict["covered"] is False
    assert verdict["missing_lines"] == [3, 4, 5]


def test_report_labels_which_basis_each_number_uses():
    seed = [{"id": "q", "evidence": [{"path": PATH, "start_line": 2, "end_line": 4}]}]
    result = evaluate(OVERLAP_CHUNKS, {PATH: OVERLAP_SOURCE}, seed)
    assert result["coverage_basis"] == {
        "all_evidence_spans_hit": "line_intersection",
        "all_evidence_spans_in_one_chunk": "line_intersection",
        "all_evidence_text_covered": "evidence_text",
    }


# --- 半行片段与虚报范围：都不能算作整行覆盖 ---


def test_partial_line_chunk_does_not_count_as_full_line_coverage():
    """来源第 1 行 ABCDEF，chunk 只有 ABC——它没有证明盖住了整行。"""
    chunks = [make("half", 1, 1, "ABC")]
    verdict = evidence_coverage(chunks, "ABCDEF\n".splitlines(keepends=True), 1, 1)
    assert verdict["covered"] is False
    assert verdict["undecidable_lines"] == [1]
    assert verdict["partial_line_chunks"] == ["half"]
    assert verdict["missing_lines"] == []


def test_chunk_claiming_three_lines_but_containing_one_is_rejected():
    """声称覆盖第 1-3 行、实际只有 B：多报的行必须落到"无法判定"而不是覆盖。"""
    chunks = [make("over", 1, 3, "B\n")]
    verdict = evidence_coverage(chunks, "A\nB\nC\n".splitlines(keepends=True), 1, 3)
    assert verdict["covered"] is False
    assert verdict["undecidable_lines"] == [1, 2, 3]


def test_complete_lines_covering_the_same_range_still_pass():
    """对照：完整行片段盖满同一区间时必须通过。"""
    chunks = [make("full", 1, 3, "A\nB\nC\n")]
    verdict = evidence_coverage(chunks, "A\nB\nC\n".splitlines(keepends=True), 1, 3)
    assert verdict["covered"] is True
    assert verdict["undecidable_lines"] == []


def test_partial_piece_alongside_a_complete_chunk_is_still_covered():
    """同一行既有完整片段又有半行片段时，完整片段已经证明了覆盖。"""
    chunks = [make("full", 1, 1, "ABCDEF\n"), make("half", 1, 1, "ABC")]
    verdict = evidence_coverage(chunks, "ABCDEF\n".splitlines(keepends=True), 1, 1)
    assert verdict["covered"] is True
    assert verdict["partial_line_chunks"] == ["half"]


def test_chunk_with_text_from_elsewhere_is_a_conflict_not_a_partial():
    """正文既不是这段来源、也不是它的一部分：报冲突，不是"无法判定"。"""
    chunks = [make("foreign", 1, 1, "完全无关的文字")]
    verdict = evidence_coverage(chunks, "ABCDEF\n".splitlines(keepends=True), 1, 1)
    assert verdict["covered"] is False
    assert verdict["text_range_conflicts"] == ["foreign"]
    assert verdict["undecidable_lines"] == []
    assert verdict["missing_lines"] == [1]


def test_classify_chunk_names_the_three_kinds():
    from wenjin.coverage import COMPLETE, CONFLICT, PARTIAL, classify_chunk

    lines = "ABCDEF\n".splitlines(keepends=True)
    assert classify_chunk(make("a", 1, 1, "ABCDEF\n"), lines) == COMPLETE
    assert classify_chunk(make("b", 1, 1, "ABC"), lines) == PARTIAL
    assert classify_chunk(make("c", 1, 1, "XYZ"), lines) == CONFLICT
    assert classify_chunk(make("d", 1, 1, "   "), lines) == PARTIAL


# --- 空行不算证据 ---
#
# 切片按空行分块，连续空行里靠后的那些不属于任何 chunk（这是切片正确行为）。
# 早期实现要求证据区间**每一行**都被覆盖，于是"内容全在、中间隔了个空行"被判成未覆盖。
# 实例：raster_data_model.html 证据 199-209，chunk 是 194-204 与 206-222，
# 缺的第 205 行是 `</div>`，解析后为空。


def blank_line_source() -> list[str]:
    """构造与上述实例同形的来源：第 4、5 行解析后为空。"""
    lines = [f"line{number}\n" for number in range(1, 13)]
    lines[3] = "\n"   # 第 4 行
    lines[4] = "\n"   # 第 5 行 —— 落在两个 chunk 之间
    return lines


def test_blank_line_gap_is_not_a_missing_line():
    src = blank_line_source()
    hits = [
        make("a", 1, 4, "".join(src[0:4])),    # 覆盖 1-4
        make("b", 6, 12, "".join(src[5:12])),  # 覆盖 6-12，跳过第 5 行
    ]
    verdict = evidence_coverage(hits, src, 2, 8)
    assert verdict["covered"] is True, verdict
    assert verdict["missing_lines"] == []


def test_real_content_gap_is_still_reported():
    """对照：跳过的恰好是**有内容**的行，必须报出来。

    `blank_line_source` 里第 4、5 行都为空。这里换一个只在第 4 行为空的来源，
    让 chunk 跳过第 5 行——那一行有内容，是真缺口，不能因为有空行的存在就一起放过。
    """
    src = [f"line{number}\n" for number in range(1, 13)]
    src[3] = "\n"                                  # 只有第 4 行是空行
    hits = [
        make("a", 1, 4, "".join(src[0:4])),
        make("b", 6, 12, "".join(src[5:12])),      # 跳过第 5 行（有内容）
    ]
    verdict = evidence_coverage(hits, src, 2, 8)
    assert verdict["covered"] is False
    assert verdict["missing_lines"] == [5]


def test_span_made_only_of_blank_lines_is_not_covered():
    """整段证据都是空行/纯标记：登记问题，不能报成已覆盖。"""
    src = ["\n", "\n", "\n", "\n"]
    verdict = evidence_coverage([make("a", 1, 4, "\n")], src, 1, 4)
    assert verdict["covered"] is False
    assert verdict["empty_evidence_span"] is True


def test_content_lines_helper_skips_blanks():
    from wenjin.coverage import content_lines

    src = blank_line_source()
    assert 4 not in content_lines(src, 1, 12)
    assert 5 not in content_lines(src, 1, 12)
    assert content_lines(src, 1, 3) == {1, 2, 3}


def test_retrieval_eval_covers_span_agrees_with_coverage_module():
    """两个入口的空行语义必须一致，否则同一份数据会得出两个数。"""
    from evaluation.run_retrieval_eval import covers_span

    span = {"path": "a.html", "start_line": 2, "end_line": 8}
    results = [
        {"source_path": "a.html", "start_line": 1, "end_line": 4},
        {"source_path": "a.html", "start_line": 6, "end_line": 12},
    ]
    assert covers_span(results, span) is False                              # 严格：缺第 5 行
    assert covers_span(results, span, blank_lines=frozenset({5})) is True   # 第 5 行是空行


def test_retrieval_eval_metrics_rejects_page_level_spans():
    """按页证据没有行号，必须由调用方先分流，不能被静默当成"未覆盖"。"""
    from evaluation.run_retrieval_eval import metrics

    with pytest.raises(ValueError, match="只处理行级证据"):
        metrics([], [{"path": "a.pdf", "page": 3, "start_line": None, "end_line": None}], k=5)
