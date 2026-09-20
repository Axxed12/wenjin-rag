"""将完整源码文档切成可检索 chunk。

当前提供固定字符窗口、按空行形成代码块后合并（blocks）以及带硬上限的
blocks-split 三种策略。它们用于理解和对照实验，不是最终的 C++ AST 切分器。
"""

from dataclasses import dataclass

from wenjin.content_hashing import sha256_hex
from wenjin.parser_names import is_ocr_derived


@dataclass(frozen=True)
class Chunk:
    """一个可检索片段及其原文定位信息。"""

    chunk_id: str
    source_path: str
    version: str
    start_line: int  # 从 1 开始，便于用户和 IDE 定位。
    end_line: int
    text: str
    strategy: str
    content_sha256: str = ""
    source_raw_sha256: str | None = None
    source_canonical_sha256: str | None = None
    normalization: str | None = None
    page_number: int | None = None
    logical_document_id: str | None = None
    # 片段真实来自哪套解析。它必须进索引：只写在 manifest 里的话，检索结果本身
    # 无法说明"这段正文是 OCR 识别来的"，用户会把它当成原文引用。
    parser: str | None = None

    @property
    def ocr_derived(self) -> bool:
        return is_ocr_derived(self.parser)


def _make_chunk(
    *,
    document: dict,
    lines: list[str],
    start: int,
    end: int,
    strategy: str,
    index: int,
    text: str | None = None,
) -> Chunk:
    """从原文行切片；start/end 使用 Python 切片的左闭右开下标。

    text 只在单行内部硬切时传入：此时多个片段共享同一原始行号，无法由行下标重建文本。
    chunk_id 里的 index 在单个文档（PDF 为单页）内递增，因此片段仍然拥有唯一 ID。
    """
    text = "".join(lines[start:end]) if text is None else text
    return Chunk(
        chunk_id=(
            f"{document['version']}:{document['source_path']}"
            f"{':page-' + str(document['page_number']) if document.get('page_number') else ''}"
            f":{strategy}:{index:04d}"
        ),
        source_path=document["source_path"],
        version=document["version"],
        start_line=start + 1,
        end_line=end,
        text=text,
        strategy=strategy,
        content_sha256=sha256_hex(text.encode("utf-8")),
        source_raw_sha256=document.get("raw_sha256"),
        source_canonical_sha256=document.get("canonical_sha256"),
        normalization=document.get("normalization"),
        page_number=document.get("page_number"),
        logical_document_id=document.get("logical_document_id"),
        parser=document.get("parser"),
    )


def fixed_chunks(document: dict, *, size: int = 1200, overlap: int = 120) -> list[Chunk]:
    """按字符数切分文档，保留 overlap 字符作为相邻上下文。

    这里用字符而不是 token，是为了让第一版边界容易观察；生产实验应再记录 token 数。
    overlap 必须小于 size，否则窗口无法向前移动。
    """
    if size <= 0 or overlap < 0 or overlap >= size:
        raise ValueError("size must be positive and overlap must satisfy 0 <= overlap < size")
    text = document["text"]
    chunks = []
    start = 0
    index = 0
    while start < len(text):
        end = min(start + size, len(text))
        # 字符窗口可能切断一行；行号取覆盖到的完整行，文本也保留完整行。
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        line_end = len(text) if line_end == -1 else line_end + 1
        lines = text.splitlines(keepends=True)
        before = text[:line_start].count("\n")
        after = text[:line_end].count("\n")
        chunks.append(
            _make_chunk(
                document=document,
                lines=lines,
                start=before,
                end=after,
                strategy="fixed",
                index=index,
            )
        )
        index += 1
        if line_end >= len(text):
            break
        start = max(line_end - overlap, start + 1)
    return chunks


def _split_long_block(
    lines: list[str], start: int, end: int, max_chars: int
) -> list[tuple[int, int, str | None]]:
    """把一个超过 max_chars 的连续块切成若干片段，返回 (start, end, text)。

    片段之间不重叠，按原文顺序连起来就是整块，因此联合覆盖可以由文本精确重建。
    单行本身超过 max_chars 时按字符硬切，这些片段共享同一原始行号，必须显式携带 text。
    """
    pieces: list[tuple[int, int, str | None]] = []
    piece_start = start
    piece_chars = 0
    index = start
    while index < end:
        line = lines[index]
        if len(line) > max_chars:
            if piece_chars:
                pieces.append((piece_start, index, None))
                piece_chars = 0
            for offset in range(0, len(line), max_chars):
                pieces.append((index, index + 1, line[offset : offset + max_chars]))
            piece_start = index + 1
        else:
            if piece_chars + len(line) > max_chars:
                pieces.append((piece_start, index, None))
                piece_start = index
                piece_chars = 0
            piece_chars += len(line)
        index += 1
    if piece_chars:
        pieces.append((piece_start, end, None))
    return pieces


def block_chunks(
    document: dict, *, max_chars: int = 1600, split_oversized: bool = False
) -> list[Chunk]:
    """先按空行分成代码/注释块，再合并到 max_chars 附近。

    这比固定字符窗口更可能保留一段注释和紧随其后的声明，但不能理解 C++ 语法。
    `split_oversized=False` 保留旧基线行为：超过 max_chars 的单个块整块成片，
    所以上限只是目标而不是保证；该默认值是 OSG active 和 001 候选的可复现契约。
    `split_oversized=True`（策略名 `blocks-split`）改为硬上限：超长块按行、必要时
    按字符切成不超过 max_chars 的连续片段，并保留原始行号/物理页码和唯一 chunk ID。
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    strategy = "blocks-split" if split_oversized else "blocks"
    lines = document["text"].splitlines(keepends=True)
    blocks: list[tuple[int, int]] = []
    start = 0
    for index, line in enumerate(lines):
        if not line.strip():
            if start < index:
                blocks.append((start, index + 1))
            start = index + 1
    if start < len(lines):
        blocks.append((start, len(lines)))

    chunks: list[Chunk] = []
    pending_start: int | None = None
    pending_end = 0
    pending_chars = 0
    index = 0
    for block_start, block_end in blocks:
        block_chars = sum(len(line) for line in lines[block_start:block_end])
        # 合并相邻块时，块之间的空行也会进入同一个 chunk 文本。
        # 旧基线沿用“只统计块内字符”的估算以保持001/OSG可复现；blocks-split 是硬上限，
        # 必须把这段间隔一起计入，否则合并出的 chunk 仍会超过 max_chars。
        gap_chars = (
            sum(len(line) for line in lines[pending_end:block_start])
            if split_oversized and pending_start is not None
            else 0
        )
        if split_oversized and block_chars > max_chars:
            # 超长块不能整块成片；先结清已累积的块，再输出受上限约束的片段。
            if pending_start is not None:
                chunks.append(
                    _make_chunk(
                        document=document,
                        lines=lines,
                        start=pending_start,
                        end=pending_end,
                        strategy=strategy,
                        index=index,
                    )
                )
                index += 1
                pending_start = None
                pending_chars = 0
            for piece_start, piece_end, piece_text in _split_long_block(
                lines, block_start, block_end, max_chars
            ):
                chunks.append(
                    _make_chunk(
                        document=document,
                        lines=lines,
                        start=piece_start,
                        end=piece_end,
                        strategy=strategy,
                        index=index,
                        text=piece_text,
                    )
                )
                index += 1
            continue
        if pending_start is not None and pending_chars + gap_chars + block_chars > max_chars:
            chunks.append(
                _make_chunk(
                    document=document,
                    lines=lines,
                    start=pending_start,
                    end=pending_end,
                    strategy=strategy,
                    index=index,
                )
            )
            index += 1
            pending_start = None
            pending_chars = 0
        if pending_start is None:
            pending_start = block_start
        pending_end = block_end
        # 被吸收的空行同样占用 chunk 预算，必须保留在累计值里（旧基线的 gap_chars 恒为0）。
        pending_chars += gap_chars + block_chars
    if pending_start is not None:
        chunks.append(
            _make_chunk(
                document=document,
                lines=lines,
                start=pending_start,
                end=pending_end,
                strategy=strategy,
                index=index,
            )
        )
    return chunks
