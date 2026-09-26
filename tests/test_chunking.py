from wenjin.ingestion.chunking import block_chunks, fixed_chunks


def document(text: str) -> dict:
    return {"source_path": "include/osg/Test", "version": "3.6.5", "text": text}


def test_fixed_chunks_keep_source_lines_and_overlap():
    chunks = fixed_chunks(document("a\nb\nc\nd\n"), size=4, overlap=1)
    assert chunks
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 4
    assert all(chunk.strategy == "fixed" for chunk in chunks)


def test_block_chunks_do_not_drop_non_empty_blocks():
    chunks = block_chunks(document("comment\n\nvoid addDrawable();\n\nreturn true;\n"), max_chars=100)
    assert "comment" in "".join(chunk.text for chunk in chunks)
    assert "addDrawable" in "".join(chunk.text for chunk in chunks)
    assert "return true" in "".join(chunk.text for chunk in chunks)
    assert all(chunk.start_line <= chunk.end_line for chunk in chunks)


def test_pdf_page_chunk_ids_do_not_collide():
    first = {**document("one\n"), "page_number": 1}
    second = {**document("two\n"), "page_number": 2}
    a, b = block_chunks(first)[0], block_chunks(second)[0]
    assert a.chunk_id != b.chunk_id
    assert a.page_number == 1 and b.page_number == 2


def test_legacy_blocks_still_emit_oversized_block_whole():
    """旧基线行为被固定：默认参数下超长块仍整块成片，001候选因此可复现。"""
    chunks = block_chunks(document("x" * 50 + "\n"), max_chars=10)
    assert [len(chunk.text) for chunk in chunks] == [51]
    assert chunks[0].strategy == "blocks"


def test_blocks_split_caps_multi_line_block_and_keeps_line_numbers():
    text = "".join(f"line-{index:03d}-{'y' * 20}\n" for index in range(40))
    chunks = block_chunks(document(text), max_chars=200, split_oversized=True)
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 200 for chunk in chunks)
    assert all(chunk.strategy == "blocks-split" for chunk in chunks)
    # 片段按原文顺序连续排列，行号单调且能覆盖全文。
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 40
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start_line == previous.end_line + 1
    assert "".join(chunk.text for chunk in chunks) == text


def test_blocks_split_hard_splits_one_overlong_line_with_unique_ids():
    line = "".join(chr(ord("a") + index) * 25 for index in range(10)) + "\n"
    chunks = block_chunks(document(line), max_chars=100, split_oversized=True)
    assert [len(chunk.text) for chunk in chunks] == [100, 100, 51]
    # 同一原始行的多个片段共享行号，但必须拥有不同 chunk ID 和不同内容 Hash。
    assert {chunk.start_line for chunk in chunks} == {1}
    assert {chunk.end_line for chunk in chunks} == {1}
    assert len({chunk.chunk_id for chunk in chunks}) == 3
    assert len({chunk.content_sha256 for chunk in chunks}) == 3
    assert "".join(chunk.text for chunk in chunks) == line


def test_blocks_split_counts_blank_lines_inside_merged_chunk():
    """合并相邻块时块间空行也占预算；若不计数，这里会合并成80字符的chunk。"""
    text = "a" * 19 + "\n" + "\n" * 40 + "b" * 19 + "\n"
    chunks = block_chunks(document(text), max_chars=50, split_oversized=True)
    assert len(chunks) == 2
    assert all(len(chunk.text) <= 50 for chunk in chunks)
    # 分开的chunk会丢掉块间空行，但任何非空白字符都不能丢失。
    assert "".join(chunk.text for chunk in chunks).replace("\n", "") == text.replace("\n", "")


def test_blocks_split_keeps_page_number_and_source_hash():
    page = {**document("q" * 120 + "\n"), "page_number": 7, "raw_sha256": "a" * 64}
    chunks = block_chunks(page, max_chars=50, split_oversized=True)
    assert all(chunk.page_number == 7 for chunk in chunks)
    assert all(chunk.source_raw_sha256 == "a" * 64 for chunk in chunks)
    assert all(chunk.chunk_id.startswith("3.6.5:include/osg/Test:page-7:") for chunk in chunks)


def test_fixed_chunks_keep_a_last_line_without_a_trailing_newline():
    """末行没有换行时必须照样入库。

    `fixed` 用换行符个数当排他结束行下标；文件末尾没换行时它比行数少一个，
    末行就永远切不出来——而循环随即因已到文件末尾退出，所以既不报错也不计数。
    旧样例都带末尾换行，正好绕开了这个条件。
    """
    chunks = fixed_chunks(document("first\nlast"), size=1200, overlap=120)
    assert [chunk.text for chunk in chunks] == ["first\nlast"]
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 2)


def test_fixed_chunks_reach_the_last_line_across_several_windows():
    """多窗口时同样：最后一个 chunk 的行区间必须盖到末行。"""
    text = "\n".join(f"line-{index}" for index in range(40))
    chunks = fixed_chunks(document(text), size=60, overlap=10)
    assert chunks[-1].end_line == 40
    assert "line-39" in chunks[-1].text
