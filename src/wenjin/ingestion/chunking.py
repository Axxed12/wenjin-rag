"""将完整源码文档切成可检索 chunk。

当前提供固定字符窗口、按空行形成代码块后合并（blocks）、带硬上限的
blocks-split，以及在此基础上再做父子化的 blocks-split-parented 四种策略。
它们用于理解和对照实验，不是最终的 C++ AST 切分器。

`blocks` 的默认参数（max_chars=1600、split_oversized=False）是 OSG active 和
001 候选的可复现契约，改动它会让已有索引无法按原样重建。
"""

from dataclasses import dataclass, replace

from wenjin.content_hashing import sha256_hex
from wenjin.parser_names import is_ocr_derived

# 父子化索引的策略名：child 负责命中，父块负责给证据足够上下文。
PARENTED_STRATEGY = "blocks-split-parented"
# 父块 ID 在 chunk_id 里的段名：让"这是父块"在 ID 上就能看出来，也不必依赖
# "child 的 ID 段名恰好还不是 PARENTED_STRATEGY"这个巧合（两者的序号各自从 0 开始）。
PARENT_ID_SEGMENT = "parent-"


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
    # 父子化索引里这个 child 所属的父块 ID。None 表示它就是父块本身，或者这份索引
    # 没有父子结构（fixed/blocks/blocks-split）。父块的 ID 也进索引：命中 child 后
    # 要能取回父块的原文行区间与正文。
    parent_id: str | None = None

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
    id_prefix: str = "",
) -> Chunk:
    """从原文行切片；start/end 使用 Python 切片的左闭右开下标。

    text 只在单行内部硬切时传入：此时多个片段共享同一原始行号，无法由行下标重建文本。
    chunk_id 里的 index 在单个文档（PDF 为单页）内递增，因此片段仍然拥有唯一 ID。
    id_prefix 用来区分同一份文档里编号体系不同的片段（父块与 child）。
    """
    text = "".join(lines[start:end]) if text is None else text
    return Chunk(
        chunk_id=(
            f"{document['version']}:{document['source_path']}"
            f"{':page-' + str(document['page_number']) if document.get('page_number') else ''}"
            f":{strategy}:{id_prefix}{index:04d}"
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


def _close_parent(
    document: dict, lines: list[str], group: list[Chunk], index: int
) -> tuple[Chunk, list[Chunk]]:
    """把一个 child 组收成一个父块，并给组内 child 打上 parent_id。

    父块范围取 [组内第一个 child 的 start_line, 最后一个 child 的 end_line]，正文按
    **这段范围重新从原文切片**：相邻 child 之间可能夹着空行（那段原文属于父块声明的
    区间，却不属于任何 child），拼接 child 文本会漏掉它们，父块就不再逐字等于它声明
    的行区间。重新切片还顺带保证了父块与 child 共用同一条证据契约。

    child 的 chunk_id 里保留它被切出来时的段名 `blocks-split`：这个 id 是稳定标识，
    同一份语料再用 blocks-split 建索引时 child ID 能一一对上；只有 `strategy` 字段
    需要等于索引目标声明的策略。
    """
    parent = _make_chunk(
        document=document,
        lines=lines,
        start=group[0].start_line - 1,
        end=group[-1].end_line,
        strategy=PARENTED_STRATEGY,
        index=index,
        id_prefix=PARENT_ID_SEGMENT,
    )
    members = [
        replace(child, strategy=PARENTED_STRATEGY, parent_id=parent.chunk_id)
        for child in group
    ]
    return parent, members


def block_chunks_parented(
    document: dict,
    *,
    max_chars: int = 1600,
    max_children: int = 3,
    max_parent_chars: int = 4800,
) -> tuple[list[Chunk], list[Chunk]]:
    """切出 child，再把**相邻连续**的 child 合并成父块；返回 `(children, parents)`。

    child 就是 blocks-split 的片段，照常进 dense 与 BM25 负责命中率；父块只作为
    证据返回，让引用带上足够上下文。父子化的收益与代价都在这里定死：

    - 父块的行区间是 `[第一个 child 的 start_line, 最后一个 child 的 end_line]`，
      正文是这段原文的逐字切片（见 `_close_parent`），因此父块同样满足"证据正文
      等于它声明的原文区间"这条契约；
    - 只在**同一份解析单元**（同一 source_path 且同一物理页）内合并：跨页或跨文件
      合并会得到一个横跨两份原文的区间；
    - 两个上限（child 个数、字符数）都只是目标。单个 child 自己就超过
      max_parent_chars 时（只有超长单行被字符级硬切才会发生），父块仍然包含它，
      于是该父块会超过上限——与 `blocks` 的 max_chars 同一口径；
    - 相邻 child 之间**断开**时（连续空行会这样：那段空行不属于任何 child）另起一个
      父块。硬把断开的 child 并进同一个父块，父块就会声明一段它没有被子块盖满的
      区间，下游按行号取原文会拿到父块声称覆盖、实际没人覆盖的行；
    - **同一行的多个片段不拆开**：单行超长被字符级硬切时，这些片段共享同一行号，
      父块的行区间必然是整行。把它们分到两个父块，两个父块会声明同一段区间，而各自
      的 child 都没盖满它。于是这一行的父块可能超过 max_children——与上面的字符上限
      同理，上限是目标而不是保证。

    两个上限默认值和调用方的 context_char_budget（服务默认 8000 字符）相互竞争：
    预算内能放下的父块数比 child 少，调用方要么接受更少的证据条数，要么调小上限。
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if max_children < 1:
        raise ValueError("max_children必须大于0")
    if max_parent_chars <= 0:
        raise ValueError("max_parent_chars必须为正")
    lines = document["text"].splitlines(keepends=True)
    children = block_chunks(document, max_chars=max_chars, split_oversized=True)

    parents: list[Chunk] = []
    grouped: list[Chunk] = []
    current: list[Chunk] = []
    current_chars = 0

    def close() -> None:
        nonlocal current, current_chars
        if not current:
            return
        parent, members = _close_parent(document, lines, current, len(parents))
        parents.append(parent)
        grouped.extend(members)
        current, current_chars = [], 0

    for child in children:
        # 行区间有重叠 = 同一原始行的多个硬切片段，必须同属一个父块（见函数说明）。
        same_line = bool(current) and child.start_line <= current[-1].end_line
        if current and not same_line and (
            len(current) >= max_children
            or child.start_line > current[-1].end_line + 1
            or current_chars + len(child.text) > max_parent_chars
        ):
            close()
        current.append(child)
        current_chars += len(child.text)
    close()
    return grouped, parents
