"""解析器名的唯一定义。

这批字符串写在 manifest 里、写进索引、又出现在证据输出中；一旦在多处各写一份，
改名时漏掉一处就会让"这段正文是不是 OCR 来的"在不同环节得到不同答案。

本模块**不导入任何东西**：chunking 和索引审计只需要一个字符串常量，
不应为此拉进 PyMuPDF、httpx 或 BeautifulSoup。判定逻辑（而不是名字本身）
放在 `wenjin.ingestion.multiformat` 和 `wenjin.ingestion.ocr`。
"""

# 文本层按物理页解析。
TEXT_PAGE_PARSER = "pymupdf-page-v1"
# 整份 PDF 都走 MinerU OCR。
OCR_PAGE_PARSER = "mineru-ocr-page-v1"
# 同一份 PDF 里文本层页保留文本层、扫描页走 OCR 时，文件级记录这个流程名。
# 页级真实来源仍由每个 ParsedUnit 的 parser 表示。
HYBRID_PAGE_PARSER = "mixed-text-ocr-page-v1"

PAGE_PARSERS = frozenset({TEXT_PAGE_PARSER, OCR_PAGE_PARSER, HYBRID_PAGE_PARSER})

# 文件级 parser → 允许出现在该文件各页上的页级 parser。
UNIT_PARSERS: dict[str, frozenset[str]] = {
    TEXT_PAGE_PARSER: frozenset({TEXT_PAGE_PARSER}),
    OCR_PAGE_PARSER: frozenset({OCR_PAGE_PARSER}),
    HYBRID_PAGE_PARSER: frozenset({TEXT_PAGE_PARSER, OCR_PAGE_PARSER}),
}

# 正文由 OCR 识别而来、不是原文文本的页级解析器。
OCR_DERIVED_PARSERS = frozenset({OCR_PAGE_PARSER})

# **文件级**概念：这些文件级解析器意味着该文件含 OCR 生成的正文，索引里必须带
# 逐页来源标记，否则识别文本会以原文的样子进入证据。
#
# 它和 `is_ocr_derived` 是两件事，不能互相替代：
#   - `is_ocr_derived` 看的是**一个 chunk** 的正文是不是识别来的；
#   - `requires_ocr_marker` 看的是**一份文件**是否必须交出可核验的页级标记。
# 混用会让混合件（文件级是混合解析器、页级才是 OCR）绕过强制核验。
OCR_BEARING_PARSERS = frozenset({OCR_PAGE_PARSER, HYBRID_PAGE_PARSER})


def is_ocr_derived(parser: str | None) -> bool:
    """**页级**：这个 chunk 的正文是否来自 OCR。由解析器名推导，不允许各自判断。"""
    return parser in OCR_DERIVED_PARSERS


def requires_ocr_marker(declared: str | None) -> bool:
    """**文件级**：这份文件的索引是否必须带逐页来源标记才能核验。"""
    return declared in OCR_BEARING_PARSERS


def allowed_unit_parsers(declared: str | None) -> frozenset[str] | None:
    """文件级 parser 允许的页级 parser；None 表示未声明，调用方自行决定是否拒绝。

    这只是**粗筛**："允许集合"里同时含文本层和 OCR，单靠它无法判断某个具体页
    应该是哪一种。精确判定要用 manifest 记录的逐页解析计划。
    """
    if declared is None:
        return None
    return UNIT_PARSERS.get(declared, frozenset({declared}))
