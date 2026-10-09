"""blocks-split 保留整行位置契约；响应选择器独立执行上下文字符预算。"""

from wenjin.content_hashing import normalize_newlines
from wenjin.ingestion.chunking import block_chunks
from wenjin.retrieval.service import select_context


def _document(text: str, **metadata) -> dict:
    return {
        "source_path": "spec/example.txt",
        "version": "1.0",
        "text": text,
        **metadata,
    }


def test_oversized_line_is_one_exact_chunk_with_its_declared_line_slice():
    text = "prefix\n" + "x" * 451 + "\nsuffix\n"

    chunks = block_chunks(_document(text), max_chars=100, split_oversized=True)

    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [(1, 1), (2, 2), (3, 3)]
    assert [chunk.text for chunk in chunks] == ["prefix\n", "x" * 451 + "\n", "suffix\n"]
    source_lines = text.splitlines(keepends=True)
    for chunk in chunks:
        assert chunk.text == "".join(source_lines[chunk.start_line - 1 : chunk.end_line])
    assert "".join(chunk.text for chunk in chunks) == text


def test_regular_lines_still_obey_chunk_target_and_keep_order():
    text = "".join(f"line-{index:02d}-{'z' * 17}\n" for index in range(12))

    chunks = block_chunks(_document(text), max_chars=60, split_oversized=True)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 60 for chunk in chunks)
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 12
    assert all(left.end_line + 1 == right.start_line for left, right in zip(chunks, chunks[1:]))
    assert "".join(chunk.text for chunk in chunks) == text


def test_crlf_canonical_text_and_final_line_without_newline_are_preserved():
    raw = "first\r\n" + "q" * 151 + "\r\nlast without newline"
    canonical = normalize_newlines(raw)

    chunks = block_chunks(_document(canonical), max_chars=50, split_oversized=True)

    source_lines = canonical.splitlines(keepends=True)
    assert chunks[-1].end_line == 3
    assert chunks[-1].text == "last without newline"
    assert "".join(chunk.text for chunk in chunks) == canonical
    for chunk in chunks:
        assert chunk.text == "".join(source_lines[chunk.start_line - 1 : chunk.end_line])


def test_pdf_page_identity_stays_in_chunk_ids_for_full_long_lines():
    first = _document("p" * 121 + "\n", page_number=4)
    second = _document("p" * 121 + "\n", page_number=5)

    chunks = [
        block_chunks(first, max_chars=40, split_oversized=True)[0],
        block_chunks(second, max_chars=40, split_oversized=True)[0],
    ]

    assert [chunk.page_number for chunk in chunks] == [4, 5]
    assert chunks[0].chunk_id != chunks[1].chunk_id
    assert ":page-4:" in chunks[0].chunk_id
    assert ":page-5:" in chunks[1].chunk_id
    assert all(chunk.text == "p" * 121 + "\n" for chunk in chunks)


def test_context_budget_skips_full_long_chunk_without_truncating_it():
    long_text = "L" * 120
    short_text = "short"
    candidates = [
        {"chunk_id": "long", "text": long_text},
        {"chunk_id": "short", "text": short_text},
    ]

    selected = select_context(candidates, top_k=2, char_budget=10)

    assert [item["chunk_id"] for item in selected] == ["short"]
    assert selected[0]["text"] == short_text
    assert len(selected[0]["text"]) <= 10
    assert select_context(candidates[:1], top_k=1, char_budget=10) == []


def test_context_budget_allows_complete_long_chunk_when_user_budget_covers_it():
    full_text = "C" * 120
    selected = select_context(
        [{"chunk_id": "long", "text": full_text}], top_k=1, char_budget=120
    )

    assert len(selected) == 1
    assert selected[0]["text"] == full_text

