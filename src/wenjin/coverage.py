"""证据覆盖率的判定原语：按**来源位置**合并，不按字符串去重。

覆盖率有三种判定依据，数值不可互相比较：

- `line_intersection`：chunk 的行号区间与证据行区间**相交**。最弱——只要沾到一行就算命中，
  而且两侧片段各占一半时也算命中；
- `page_present`：按页来源（PDF）只登记物理页、没有行号或锚点，只能判"该页有 chunk"；
- `evidence_text`：**完整行片段**的行范围并集必须盖满证据区间。所谓完整行片段，指正文与
  它声明的整段来源行逐字一致；单行被硬切产生的半行片段**不计入覆盖**（没有字符偏移量，
  无法证明它盖住了哪一段），单独报出来。

**为什么不能直接拼接文本再找子串**：相邻片段可能重叠（`fixed` 有 120 字符重叠），
拼接会把重叠部分重复一遍，本来完整的证据就不再是连续子串：

    片段一 A B C，片段二 C D E → 拼接 "A B C C D E"，证据 "B C D" 明明在里面却被判未覆盖。

反过来，也**不能按字符串相似度去重**：那会把"中间真的缺字"一起抹平，而缺字恰恰是
最需要报出来的缺口。所以这里只做位置合并，并单独报出文本与行范围对不上的片段。
"""

import re

LINE_BASIS = "line_intersection"
PAGE_BASIS = "page_present"
TEXT_BASIS = "evidence_text"


def normalize(text: str) -> str:
    """折叠空白；只用于比较，不改变证据文本本身。"""
    return re.sub(r"\s+", " ", text).strip()


def line_intersects(chunk, start: int, end: int) -> bool:
    """弱判据：行号区间相交。保留它是为了能和历史报告对照，不用来判断覆盖是否成立。"""
    return chunk.start_line <= end and chunk.end_line >= start


COMPLETE = "complete"
PARTIAL = "partial"
CONFLICT = "conflict"


def classify_chunk(chunk, source_lines: list[str]) -> str:
    """片段相对它**声明的行范围**是哪一类。

    - `complete`：正文与声明的整段来源行逐字一致（折叠空白后）→ 它的行范围可以计入覆盖；
    - `partial`：正文只是这段来源的一部分（单行被硬切）→ **不能**当作整行覆盖。
      `Chunk` 没有字符偏移量，无法定位这一段落在行的哪个位置，所以计入覆盖就是把
      "覆盖了整行"当成已知事实，而它并没有被证明；
    - `conflict`：正文既不是这段来源，也不是它的一部分（含"只有一部分却声明了多行"、
      "正文比声明的范围还多"）→ 片段与 manifest 登记的内容对不上。

    注意 `partial` 与 `conflict` 的区别只在于"是不是这段来源的一部分"：
    `ABC` 相对 `ABCDEF` 是 partial，相对 `XYZ` 是 conflict。
    """
    if not chunk.text.strip():
        return PARTIAL  # 空正文不承载内容，同样不能计入覆盖
    expected = normalize("".join(source_lines[chunk.start_line - 1 : chunk.end_line]))
    actual = normalize(chunk.text)
    if not expected:
        return CONFLICT
    if actual == expected:
        return COMPLETE
    if actual in expected:
        return PARTIAL
    return CONFLICT


def evidence_coverage(hits, source_lines: list[str], start: int, end: int) -> dict:
    """证据是否完整进入命中片段集合。

    判定 = **完整行片段**的行范围并集盖满证据区间，且没有与登记内容冲突的片段。

    只做位置合并，不做字符串相似度去重（见模块说明）；也**不把子串当完整性证明**：
    半行片段没有字符偏移量，无法证明它盖住了哪一段，因此单独报出来而不计入覆盖。
    """
    covered_lines: set[int] = set()
    claimed_by_partial: set[int] = set()
    partial_chunks: list[str] = []
    conflicts: list[str] = []

    for chunk in hits:
        lo, hi = max(chunk.start_line, start), min(chunk.end_line, end)
        if lo > hi:
            continue
        if not chunk.text.strip():
            # 空正文不承载内容：既不算覆盖，也不算"被它占了位置"。
            # 那几行是真的没有文字，报 missing 而不是 undecidable 更准确。
            continue
        kind = classify_chunk(chunk, source_lines)
        if kind == COMPLETE:
            covered_lines.update(range(lo, hi + 1))
        elif kind == PARTIAL:
            partial_chunks.append(chunk.chunk_id)
            claimed_by_partial.update(range(lo, hi + 1))
        else:
            conflicts.append(chunk.chunk_id)

    wanted = set(range(start, end + 1))
    missing = sorted(wanted - covered_lines - claimed_by_partial)
    # 只被半行片段沾到、又没有完整片段覆盖的行：**无法判定**，不能算覆盖。
    undecidable = sorted((wanted & claimed_by_partial) - covered_lines)
    return {
        "covered": not missing and not undecidable and not conflicts,
        "missing_lines": missing,
        "undecidable_lines": undecidable,
        "partial_line_chunks": partial_chunks,
        "text_range_conflicts": conflicts,
        "basis": TEXT_BASIS,
    }
