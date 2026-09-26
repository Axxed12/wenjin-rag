"""父子片段验证的探测与判定原语：只看 `Chunk` 的公共字段和块间的行区间关系。

验证方不假定实现用了哪个字段名、哪个函数名，所以这里：

1. 切片函数按签名特征在切片模块里**自动发现**——实现新增一个父子切片函数后，
   本套检查把它自动纳入，不需要改测试；
2. 父子关系有两条**互相独立**的判据：**行区间包含**（结构性，任何实现都成立）
   和**显式 parent 字段**（探测到才用）。两者若同时可得，必须彼此自洽；
3. 唯一的真值是原文：`text` 必须**逐字**等于它声明的 `[start_line, end_line]` 切片
   （行号 1-based、左闭右闭，见 `wenjin.ingestion.chunking._make_chunk`）。
   归一化后相等但逐字不等也算缺陷，单独归一类——空白折叠会把"父块少了一行的换行"
   抹平，而调用方拿到的就是原样的 `text`。

判定函数只返回违规清单，不抛异常：报告要能一次说清所有问题，而不是撞到第一条就停。
"""

import inspect
from dataclasses import fields
from typing import Any, Callable, Iterable

from wenjin.coverage import normalize
from wenjin.ingestion.chunking import Chunk

# 切片函数所在模块。实现若把父子切片放进别的模块，把它加进来即可。
PRODUCER_MODULES = ("wenjin.ingestion.chunking",)
# 生产者函数必须接受的第一个参数名；用它把 `_make_chunk` 之类排除掉。
DOCUMENT_PARAMETER = "document"


def _module(name: str):
    import importlib

    return importlib.import_module(name)


def discovered_producers() -> list[tuple[str, Callable]]:
    """切片模块里所有 `f(document, ...) -> list[Chunk]` 形态的公共函数。"""
    found: list[tuple[str, Callable]] = []
    for module_name in PRODUCER_MODULES:
        module = _module(module_name)
        for name, member in inspect.getmembers(module, inspect.isfunction):
            if name.startswith("_"):
                continue
            parameters = list(inspect.signature(member).parameters)
            if parameters and parameters[0] == DOCUMENT_PARAMETER:
                found.append((f"{module_name}.{name}", member))
    return sorted(found)


def producer_variants(member: Callable) -> list[dict[str, Any]]:
    """该生产者值得跑的参数组合。

    默认参数一组；另外把名字里带 `parent` 的开关打开跑一组——父子切片的开关
    很可能就叫这个名字，而它默认可能是关的（保持旧基线可复现）。
    """
    parameters = inspect.signature(member).parameters
    variants: list[dict[str, Any]] = [{}]
    if "split_oversized" in parameters:
        variants.append({"split_oversized": True})
    for name, parameter in parameters.items():
        if "parent" in name.lower() and isinstance(parameter.default, bool) and not parameter.default:
            variants.append({name: True})
    return variants


def call_producer(
    member: Callable, document: dict, variant: dict | None = None, **overrides: Any
) -> list[Chunk]:
    """调用生产者并把结果摊平成 Chunk 列表；元组/字典返回也没关系。

    `overrides` 里生产者不认识的参数会被丢掉——实现换了参数名不该让测试报错，
    那样报出来的是"测试跟不上实现"，不是缺陷。
    """
    accepted = set(inspect.signature(member).parameters)
    arguments = {**(variant or {}), **overrides}
    result = member(document, **{key: value for key, value in arguments.items() if key in accepted})
    return list(_flatten_chunks(result))


def _flatten_chunks(value: Any) -> Iterable[Chunk]:
    if isinstance(value, Chunk):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _flatten_chunks(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _flatten_chunks(item)


# --- 原文切片：唯一的真值来源 ---


def source_lines(document: dict) -> list[str]:
    """文档解析后文本的行，保留行尾——`_make_chunk` 就是这么切片的。"""
    return document["text"].splitlines(keepends=True)


def exact_slice(document: dict, start_line: int, end_line: int) -> str:
    """`[start_line, end_line]`（1-based、左闭右闭）对应的原文。"""
    return "".join(source_lines(document)[start_line - 1 : end_line])


def text_violations(document: dict, chunks: list[Chunk]) -> dict[str, list[dict]]:
    """`text` 与它声明的行区间是否逐字一致。

    - `text_conflict`：既不是该切片，也不是它的连续子串（**拼接产物**、串行都会落到这里）。
      父块文本如果是把子块文本直接拼起来的，一旦子块之间有被跳过的空行，就会在这里现形。
    - `text_whitespace_drift`：归一化后相等但逐字不等。比 conflict 轻，但仍是缺陷：
      调用方拿到的证据正文与行号指向的原文不是同一串字节。
    - `partial_line_piece`：单行被硬切产生的半行片段。`Chunk` 没有字符偏移量，
      无法证明它盖住了整行，因此**单独列出、不计入覆盖**（见 `wenjin.coverage`）。
    """
    lines = source_lines(document)
    result: dict[str, list[dict]] = {
        "text_conflict": [],
        "text_whitespace_drift": [],
        "partial_line_piece": [],
    }
    for chunk in chunks:
        expected = "".join(lines[chunk.start_line - 1 : chunk.end_line])
        if chunk.text == expected:
            continue
        record = {"chunk_id": chunk.chunk_id, "start_line": chunk.start_line,
                  "end_line": chunk.end_line}
        if chunk.text and chunk.text in expected:
            # 只有单行硬切才允许出现"是子串"：多行片段短于原文说明漏了内容。
            if chunk.start_line == chunk.end_line:
                result["partial_line_piece"].append(record)
            else:
                result["text_conflict"].append(record)
        elif normalize(chunk.text) == normalize(expected):
            result["text_whitespace_drift"].append(record)
        else:
            result["text_conflict"].append(record)
    return result


def range_violations(document: dict, chunks: list[Chunk]) -> dict[str, list[dict]]:
    """行号区间自身是否合法、是否落在原文范围内。"""
    total = len(source_lines(document))
    problems: list[dict] = []
    for chunk in chunks:
        record = {"chunk_id": chunk.chunk_id, "start_line": chunk.start_line,
                  "end_line": chunk.end_line}
        if not isinstance(chunk.start_line, int) or not isinstance(chunk.end_line, int):
            problems.append({**record, "detail": "行号不是整数"})
        elif chunk.start_line < 1 or chunk.end_line < chunk.start_line:
            problems.append({**record, "detail": "区间非法"})
        elif chunk.page_number is None and chunk.end_line > total:
            problems.append({**record, "detail": f"超出原文共{total}行"})
    return {"range_invalid": problems}


def source_kind_violations(
    document: dict, chunks: list[Chunk], *, declared_parser: str | None = None
) -> dict[str, list[dict]]:
    """按页来源（PDF）与文本来源的位置口径必须与 manifest 的声明一致。

    审计的 `_position_problems` 就是按来源类型分流的：按页来源必须带物理页码，
    文本来源必须有合法行号。

    **口径澄清**：按页来源的块**同时**带物理页码和行号，只是那个行号是**页内**行号
    （`_make_chunk` 对页文本切片的正常结果）。所以"PDF 的块不带行号"在这个代码库里
    不成立；真正的不变量是"带不带页码由 manifest 声明的解析器决定，且一个解析单元内
    页码唯一"。`declared_parser` 不给时只做单元内的页码一致性检查。
    """
    from wenjin.parser_names import PAGE_PARSERS

    problems: list[dict] = []
    expected_page = document.get("page_number")
    for chunk in chunks:
        if chunk.page_number != expected_page:
            problems.append({
                "chunk_id": chunk.chunk_id, "page_number": chunk.page_number,
                "detail": f"与所在解析单元的页码不一致：应为 {expected_page!r}",
            })
    if declared_parser in PAGE_PARSERS and expected_page is None:
        problems.append({
            "chunk_id": chunks[0].chunk_id if chunks else None, "page_number": None,
            "detail": f"manifest 声明按页解析（{declared_parser}），但单元没有物理页码",
        })
    if (declared_parser is not None and declared_parser not in PAGE_PARSERS
            and expected_page is not None):
        problems.append({
            "chunk_id": chunks[0].chunk_id if chunks else None, "page_number": expected_page,
            "detail": f"manifest 声明非按页解析（{declared_parser}），但单元带了物理页码",
        })
    return {"source_kind_inconsistent": problems}


# --- 父子关系：两条独立判据 ---


def contains(outer: Chunk, inner: Chunk) -> bool:
    """`outer` 的行区间**严格包含** `inner` 的。

    区间完全相同**不算**包含：`blocks-split` 对单行做字符级硬切时，同一行的多个片段
    共享 `(start_line, end_line)`，把这种同胞关系当成父子会让结构判定凭空造出父块。
    "父块与子块区间相同"（单 child 文档）这种情形无法用结构判据区分，
    只能靠显式 parent 字段——见 `test_every_child_parent_reference_resolves`。
    """
    if outer.chunk_id == inner.chunk_id:
        return False
    if (outer.start_line, outer.end_line) == (inner.start_line, inner.end_line):
        return False
    return outer.start_line <= inner.start_line and outer.end_line >= inner.end_line


def parent_field_names() -> list[str]:
    """`Chunk` 上名字里带 parent 的公共字段；实现没加时返回空列表。"""
    return [field.name for field in fields(Chunk) if "parent" in field.name.lower()]


def structural_parent_map(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    """按行区间包含关系推导 `父chunk_id -> 子块列表`；不依赖任何字段名。"""
    parents: dict[str, list[Chunk]] = {}
    for outer in chunks:
        children = [
            inner for inner in chunks
            if inner.source_path == outer.source_path and inner.page_number == outer.page_number
            and contains(outer, inner)
        ]
        if children:
            parents[outer.chunk_id] = sorted(children, key=lambda item: item.start_line)
    return parents


def explicit_parent_map(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    """按显式 parent 字段推导 `父引用 -> 子块列表`；字段不存在时返回空。

    字段值可能指向 `chunk_id`，也可能是别的东西。这里按**值**分组，能否落到真实块上
    由 `unresolvable_parent_links` 判断。
    """
    parents: dict[str, list[Chunk]] = {}
    for name in parent_field_names():
        for chunk in chunks:
            reference = getattr(chunk, name)
            if reference is None:
                continue
            parents.setdefault(str(reference), []).append(chunk)
    return parents


def call_producer_parts(
    member: Callable, document: dict, variant: dict | None = None, **overrides: Any
) -> tuple[list[Chunk], list[Chunk]]:
    """调用生产者，返回 `(全部块, 生产者自己声明的父块)`。

    返回 `(children, parents)` 这种形状时保留父子分界——**结构判据抓不到"父块与子块
    区间完全相同"**（文档只有一个 child 时父块就等于子块），只有生产者自己交出的
    父块列表能证明"这确实是父块，不是硬切同胞"。其它形状把父块列表留空。
    """
    accepted = set(inspect.signature(member).parameters)
    arguments = {**(variant or {}), **overrides}
    result = member(document, **{key: value for key, value in arguments.items() if key in accepted})
    if isinstance(result, tuple) and len(result) == 2:
        first = list(_flatten_chunks(result[0]))
        second = list(_flatten_chunks(result[1]))
        if all(isinstance(item, Chunk) for item in first + second):
            return first + second, second
    return list(_flatten_chunks(result)), []


def explicit_parent_children(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    """按 `parent_id` 把 child 分组；不依赖区间包含关系。"""
    groups: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        reference = getattr(chunk, "parent_id", None)
        if reference:
            groups.setdefault(str(reference), []).append(chunk)
    return {key: sorted(value, key=lambda item: item.start_line) for key, value in groups.items()}


def explicit_parent_coverage_violations(chunks: list[Chunk]) -> list[dict]:
    """父块区间必须**恰好等于**它所有 child 的并集，且 child 之间无洞。

    这条比结构判据强：它连"父块与唯一 child 区间相同"的情形也能查（见
    `call_producer_parts`）。父块多声明一行就是虚报原文，少声明一行就是丢内容。
    允许 child 之间重叠（`fixed` 策略本就重叠），但不允许断开——断开意味着父块
    声明了一段没有任何 child 覆盖的区间。
    """
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    problems: list[dict] = []
    for parent_id, children in explicit_parent_children(chunks).items():
        parent = by_id.get(parent_id)
        if parent is None:
            continue  # 悬空引用由 unresolvable_parent_links 报，不在这里重复
        if parent.start_line != children[0].start_line or parent.end_line != children[-1].end_line:
            problems.append({
                "parent_id": parent_id,
                "detail": f"父块声明 {parent.start_line}-{parent.end_line}，"
                          f"子块并集是 {children[0].start_line}-{children[-1].end_line}",
            })
            continue
        for previous, current in zip(children, children[1:]):
            if current.start_line > previous.end_line + 1:
                problems.append({
                    "parent_id": parent_id,
                    "detail": f"子块在 {previous.end_line + 1}-{current.start_line - 1} 处断开，"
                              "父块却声明覆盖了这一段",
                })
                break
    return problems


def declared_parent_violations(
    chunks: list[Chunk], declared_parents: list[Chunk]
) -> list[dict]:
    """生产者交出的父块列表本身也要自洽。

    - 声明了父块，就不能有 child 漏挂 `parent_id`：漏挂的 child 命中后会以 child 身份
      返回，同一段正文于是既能以父块、又能以 child 出现在证据里；
    - 每个被声明的父块都必须至少有一个 child：没有 child 的父块检索永远命不中，
      只会让人以为那段原文已经覆盖到了。
    """
    if not declared_parents:
        return []
    problems: list[dict] = []
    declared_ids = {parent.chunk_id for parent in declared_parents}
    children = [chunk for chunk in chunks if chunk.chunk_id not in declared_ids]
    orphans = [
        chunk.chunk_id for chunk in children
        if not getattr(chunk, "parent_id", None)
    ]
    if orphans:
        problems.append({
            "detail": f"{len(orphans)} 个 child 没有 parent_id，命中后会以 child 身份返回",
            "child_ids": orphans[:5],
        })
    referenced = set(explicit_parent_children(chunks))
    unused = sorted(declared_ids - referenced)
    if unused:
        problems.append({
            "detail": f"{len(unused)} 个声明的父块没有任何 child 引用",
            "parent_ids": unused[:5],
        })
    return problems


def unresolvable_parent_links(chunks: list[Chunk]) -> list[dict]:
    """每个非空的 parent 引用都必须能落到本批块里真实存在的父块上。

    落不到就是悬空引用：检索命中子块后取不到父块，用户会拿到半截证据。
    """
    if not parent_field_names():
        return []
    known = {chunk.chunk_id for chunk in chunks}
    dangling: list[dict] = []
    for name in parent_field_names():
        for chunk in chunks:
            reference = getattr(chunk, name)
            if reference is not None and str(reference) not in known:
                dangling.append({
                    "chunk_id": chunk.chunk_id,
                    "field": name,
                    "reference": str(reference),
                    "detail": "parent 引用的块不在本批产出里",
                })
    return dangling


def parent_coverage_violations(chunks: list[Chunk]) -> list[dict]:
    """父块的行区间必须被它的子块**无洞**盖满，且两端对齐。

    这是本设计最容易出错的地方：父块若只是"相邻 child 合并"，合并规则一旦漏掉
    中间的空行块、或把不同来源的块并进来，父块就会声明一段它其实没盖住的范围。
    """
    problems: list[dict] = []
    for parent_id, children in structural_parent_map(chunks).items():
        parent = next(chunk for chunk in chunks if chunk.chunk_id == parent_id)
        if children[0].start_line != parent.start_line or children[-1].end_line != parent.end_line:
            problems.append({
                "parent_id": parent_id,
                "detail": f"子块两端未对齐父块 {parent.start_line}-{parent.end_line}："
                          f"{children[0].start_line}-{children[-1].end_line}",
            })
            continue
        holes = []
        for previous, current in zip(children, children[1:]):
            # 允许重叠（fixed 策略本就重叠），但不允许断开。
            if current.start_line > previous.end_line + 1:
                holes.append((previous.end_line + 1, current.start_line - 1))
        if holes:
            problems.append({"parent_id": parent_id, "detail": f"子块之间有洞：{holes}"})
    return problems


def parent_self_reference_violations(chunks: list[Chunk]) -> list[dict]:
    """父块不能把自己算成自己的子块，也不能出现"父块的父块"。"""
    problems: list[dict] = []
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    for name in parent_field_names():
        for chunk in chunks:
            reference = getattr(chunk, name)
            if reference is None or str(reference) not in by_id:
                continue
            parent = by_id[str(reference)]
            if parent.chunk_id == chunk.chunk_id:
                problems.append({"chunk_id": chunk.chunk_id, "detail": f"{name} 指向自身"})
            elif getattr(parent, name, None) is not None:
                problems.append({
                    "chunk_id": chunk.chunk_id,
                    "detail": f"{name} 的父块自己也带 {name}：多层父子未定义",
                })
    return problems


# --- 检索结果层：去重与名额占用 ---


def evidence_violations(evidence: list[dict]) -> dict[str, list[dict]]:
    """返回结果里的父子去重与名次完整性；只依赖 evidence 的公共字段。

    `evidence` 是 `RetrievalService.search` 逐条返回的可引用证据，字段名与
    `evidence_record` 一致。这里不假设父块叫什么，只用位置元组和它们之间的包含关系判断。

    **位置必须带上物理页码**：按页来源（PDF）的行号是**页内**行号，不同页上的
    `(1, 41)` 是完全不同的两段正文。只用 `(source_path, start_line, end_line)` 做键
    会在多页 PDF 上产生成片假阳性——这正是第一版校准在 OGC WMS 那些题上暴露的问题。

    最要命的一类缺陷就在这里：同一个父块被多个 child 命中时，如果不去重，
    它会占掉多个 top-5 名额，把本可以进来的其它证据挤出去——**证据更少，看起来却没变差**。
    """
    problems: dict[str, list[dict]] = {
        "duplicate_evidence": [],
        "same_position_different_text": [],
        "containment_within_result": [],
        "rank_sequence_broken": [],
    }
    ranks = [item.get("rank") for item in evidence]
    if ranks != list(range(1, len(evidence) + 1)):
        problems["rank_sequence_broken"].append({"ranks": ranks, "detail": "名次不连续或不是从1开始"})

    def position(item: dict) -> tuple:
        """位置键：同一物理页上的同一行区间才算同一个位置。"""
        return (item["source_path"], item.get("page_number"),
                item["start_line"], item["end_line"])

    seen: dict[tuple, int] = {}
    for index, item in enumerate(evidence):
        key = position(item)
        if key in seen:
            first = evidence[seen[key]]
            same_text = first.get("text") == item.get("text")
            problems["duplicate_evidence" if same_text
                     else "same_position_different_text"].append({
                "first_index": seen[key], "second_index": index,
                "source_path": item["source_path"],
                "page_number": item.get("page_number"),
                "start_line": item["start_line"], "end_line": item["end_line"],
                "note": "同一位置同一正文被返回两次" if same_text
                        else "同一行区间、不同正文：很可能是单行硬切的同胞片段，不是父子去重失败",
            })
        else:
            seen[key] = index

    for outer_index, outer in enumerate(evidence):
        for inner_index, inner in enumerate(evidence):
            if outer_index == inner_index or position(outer) == position(inner):
                continue
            if (outer["source_path"], outer.get("page_number")) != \
               (inner["source_path"], inner.get("page_number")):
                continue
            if (outer["start_line"] <= inner["start_line"]
                    and outer["end_line"] >= inner["end_line"]):
                problems["containment_within_result"].append({
                    "outer": {"start_line": outer["start_line"], "end_line": outer["end_line"]},
                    "inner": {"start_line": inner["start_line"], "end_line": inner["end_line"]},
                    "page_number": outer.get("page_number"),
                    "detail": "同一来源同一页的两条证据一段包含另一段："
                              "父块与它的命中子块同时占了名额",
                })
    return problems


def document_report(
    document: dict, chunks: list[Chunk], *, declared_parser: str | None = None
) -> dict[str, Any]:
    """把上面所有文档级判定合起来，供测试和校准脚本共用。"""
    report: dict[str, list] = {}
    report.update(text_violations(document, chunks))
    report.update(range_violations(document, chunks))
    report.update(source_kind_violations(document, chunks, declared_parser=declared_parser))
    report["parent_coverage"] = parent_coverage_violations(chunks)
    report["explicit_parent_coverage"] = explicit_parent_coverage_violations(chunks)
    report["unresolvable_parent_links"] = unresolvable_parent_links(chunks)
    report["parent_self_reference"] = parent_self_reference_violations(chunks)
    return report


def violation_total(report: dict) -> int:
    return sum(len(items) for items in report.values() if isinstance(items, list))
