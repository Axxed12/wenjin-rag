"""扫描件检测与 MinerU OCR。

文本层 PDF 走 `pymupdf-page-v1`，保留"一条物理页对应一条记录"的契约。扫描页没有
文本层，继续走原路径只会得到空页并**静默丢失**，所以这里做四件事：

1. `inspect_pdf` 在导入前逐页判定状态，把结果交给调用方决定；
2. `ocr_pdf_pages` 用本机 MinerU 的 `/file_parse` 取 `content_list`，按 `page_idx` 还原
   物理页码，产出和文本层 PDF 同样结构的 ParsedUnit；
3. `parse_pdf_hybrid` 让同一份 PDF 里有文本层的页保留文本层，只有扫描页用 OCR；
4. 逐页核对 OCR 输出与原始 PDF 页数，缺页或页码越界**直接报错**，不静默丢页。

OCR 文本不是原文，必须显式标注：解析器名固定为 `mineru-ocr-page-v1`，调用方据此判断
证据来自 OCR 而不是文本层。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymupdf
from bs4 import BeautifulSoup

from wenjin.ingestion.multiformat import ParsedUnit, parse_pdf_pages
from wenjin.parser_names import (
    HYBRID_PAGE_PARSER,
    OCR_PAGE_PARSER,
    TEXT_PAGE_PARSER,
)

MINERU_DEFAULT_URL = "http://127.0.0.1:8000"
OCR_PARSER = OCR_PAGE_PARSER  # 兼容既有调用方；新代码请直接用 multiformat 里的常量。

# 判定一页"疑似扫描"的图像覆盖率下限，写进 manifest 的检测报告便于复核。
# 整页扫描件的图像通常铺满页面；页眉、页脚、logo 达不到这个比例。
#
# 这里**只按"文本层是否为空"判断有没有文字层**，不再引入"文字要覆盖多少面积"这类
# 阈值：那会把标题页、只有一行图注的页误报成"没有文字层"，而这是比它想解决的问题
# 更常见的事实错误。代价写在 ocr.py 的模块说明和 README 里：扫描页如果自带一行
# 文字层（例如只有页眉被识别过），会被当成文本页处理，正文不会送 OCR。
SCAN_IMAGE_COVERAGE = 0.5

# 页眉、页脚和页码对检索是噪声（页码尤其会污染"第几页"这类查询）；图片没有可检索文本。
DROPPED_BLOCK_TYPES = {"header", "footer", "page_number", "image"}


@dataclass(frozen=True)
class MineruStatus:
    available: bool
    base_url: str
    detail: str


@dataclass(frozen=True)
class PdfInspection:
    """一个 PDF 的逐页检测结果；页号都是 1-based 物理页号。

    三态而不是两态：`scan_pages`（无可用文字层但有整页图像）才需要 OCR；
    `no_text_pages`（既无文字层也无整页图像）是空白页或纯矢量图页，送 OCR 也取不到
    内容。把两者混在一起，会让"中间夹了一页空白"的正常数字 PDF 被整份重跑 OCR，
    用识别文本替换掉本来完好的文本层。
    """

    path: Path
    pages: int
    text_pages: int
    scan_pages: tuple[int, ...]
    no_text_pages: tuple[int, ...]

    @property
    def needs_ocr(self) -> bool:
        return bool(self.scan_pages)

    @property
    def fully_scanned(self) -> bool:
        return self.pages > 0 and self.text_pages == 0 and bool(self.scan_pages)

    @property
    def mixed(self) -> bool:
        return self.text_pages > 0 and bool(self.scan_pages)

    def to_report(self) -> dict:
        """写进 manifest 的检测报告：记录判定事实和阈值，便于之后复核同一结论。"""
        return {
            "pages": self.pages,
            "text_pages": self.text_pages,
            "scan_pages": list(self.scan_pages),
            "no_text_pages": list(self.no_text_pages),
            "thresholds": {"scan_image_coverage": SCAN_IMAGE_COVERAGE},
        }


@dataclass(frozen=True)
class OcrDecision:
    """是否启用OCR，以及被排除的文件和各条给用户看的说明。"""

    enabled: bool
    excluded: dict[Path, str] = field(default_factory=dict)
    messages: tuple[str, ...] = ()


def _page_range(pages: tuple[int, ...], *, limit: int = 12) -> str:
    """把页号列表压成可读区间；太长时截断，避免提示和manifest被页号淹没。"""
    if len(pages) <= limit:
        return "、".join(str(page) for page in pages)
    head = "、".join(str(page) for page in pages[:limit])
    return f"{head} 等共{len(pages)}页"


def describe_scan_need(inspections: list[PdfInspection]) -> tuple[str, ...]:
    """只陈述检测到的事实，不替用户下"这是扫描件"的结论。

    之前的措辞把"没有文本层"直接说成"是扫描件"，但空白页同样没有文本层。
    用户是在这个描述上做同意的，措辞必须和实际依据一致。
    """
    lines = []
    for item in inspections:
        if not item.needs_ocr:
            continue
        parts = [
            f"{item.path.name}：共 {item.pages} 页",
            f"{len(item.scan_pages)} 页没有可用文字层但有整页图像"
            f"（第 {_page_range(item.scan_pages)} 页）",
        ]
        if item.no_text_pages:
            parts.append(
                f"{len(item.no_text_pages)} 页既无文字层也无整页图像"
                f"（第 {_page_range(item.no_text_pages)} 页，空白或纯图形，OCR 也无内容可取）"
            )
        parts.append(
            "整份都没有可用文字层"
            if item.fully_scanned
            else f"其余 {item.text_pages} 页有文字层，将保留文本层不重跑 OCR"
        )
        lines.append("；".join(parts))
    return tuple(lines)


def decide_ocr(
    inspections: list[PdfInspection],
    *,
    status: MineruStatus,
    mode: str = "ask",
    assume_yes: bool = False,
    ask: Callable[[str], bool] | None = None,
) -> OcrDecision:
    """决定是否对扫描页启用OCR。

    只做决策，不打印也不读输入：`ask`由调用方注入（CLI用input，服务用别的确认方式，
    测试用桩函数），所以这段策略可以在任何入口复用。

    只有 `needs_ocr` 的文件参与决策和排除：没有扫描页的 PDF 不该因为别的文件需要 OCR
    而被牵连，也不该出现在"是否启用OCR"的名单里。
    """
    pending = [item for item in inspections if item.needs_ocr]
    if not pending:
        return OcrDecision(False, messages=("没有检测到需要OCR的文件",))
    if mode == "never":
        return OcrDecision(
            False,
            excluded={item.path: "扫描页，未启用OCR：只归档、不参与检索" for item in pending},
            messages=("已按 --ocr never 跳过OCR，含扫描页的文件只归档",),
        )
    if not status.available:
        reason = f"MinerU 服务不可用（{status.base_url}：{status.detail}）"
        if mode == "always":
            raise RuntimeError(f"{reason}；无法按 --ocr always 执行。请先启动 MinerU。")
        return OcrDecision(
            False,
            excluded={item.path: f"扫描页，{reason}：只归档、不参与检索" for item in pending},
            messages=(reason, "含扫描页的文件改为只归档、不参与检索"),
        )
    if mode == "always" or assume_yes:
        return OcrDecision(True, messages=(f"MinerU 服务在线（{status.base_url}），已启用OCR",))
    if ask is None:
        raise RuntimeError(
            "需要用户确认是否启用OCR，但调用方没有提供确认方式。"
            "请用 --ocr always / --ocr never，或加 --yes。"
        )
    prompt = "\n".join((
        "检测到没有可用文字层的页：",
        *(f"  - {line}" for line in describe_scan_need(pending)),
        f"MinerU 服务在线（{status.base_url}），OCR每页约需1秒。",
        "是否启用 MinerU OCR 处理这些文件的扫描页？[y/N] ",
    ))
    if ask(prompt):
        return OcrDecision(True, messages=("用户确认启用OCR",))
    return OcrDecision(
        False,
        excluded={item.path: "扫描页，用户选择不启用OCR：只归档、不参与检索"
                  for item in pending},
        messages=("用户选择不启用OCR，含扫描页的文件只归档",),
    )


def choose_pdf_parser(inspection: PdfInspection, *, enabled: bool) -> str:
    """按检测结果和OCR决策选文件级解析器名。

    只有在确实要处理扫描页时才用 OCR 相关的名字；没扫描页的 PDF 一律是文本层解析，
    这样 manifest 上的解析器名就能区分"这份走过 OCR"和"这份没有"。
    """
    if enabled and inspection.needs_ocr:
        # 有文本层可保留时用混合流程，页级来源由 ParsedUnit.parser 如实记录。
        return HYBRID_PAGE_PARSER if inspection.text_pages else OCR_PAGE_PARSER
    return TEXT_PAGE_PARSER


def skipped_scan_pages(inspection: PdfInspection, parser: str) -> list[int]:
    """选了文本层解析但文件里仍有扫描页时，列出这些不会被索引的页。"""
    if parser != TEXT_PAGE_PARSER:
        return []
    return list(inspection.scan_pages)


def check_mineru(base_url: str = MINERU_DEFAULT_URL, *, timeout: float = 5.0) -> MineruStatus:
    """探测 MinerU 是否在线；不通就返回不可用，而不是抛异常。"""
    url = f"{base_url.rstrip('/')}/openapi.json"
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as error:
        return MineruStatus(False, base_url, f"连接失败：{type(error).__name__}")
    if response.status_code != 200:
        return MineruStatus(False, base_url, f"HTTP {response.status_code}")
    return MineruStatus(True, base_url, "服务在线")


def _page_area(page) -> float:
    return abs(page.rect.width * page.rect.height)


def _covering_image_ratio(page) -> float:
    """页面被单张图像覆盖的最大比例；取最大而不是求和，重叠图不应累加成"铺满"。"""
    area = _page_area(page)
    if area <= 0:
        return 0.0
    covered = 0.0
    for info in page.get_image_info():
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        covered = max(covered, abs(rect.get_area()) / area)
    return covered


def inspect_pdf(path: Path) -> PdfInspection:
    """逐页判定：有文字层 / 有整页图像但无文字层 / 两者都没有。"""
    scan: list[int] = []
    no_text: list[int] = []
    text_pages = 0
    with pymupdf.open(path) as pdf:
        total = len(pdf)
        for index, page in enumerate(pdf, start=1):
            if page.get_text().strip():
                text_pages += 1
            elif _covering_image_ratio(page) >= SCAN_IMAGE_COVERAGE:
                scan.append(index)
            else:
                no_text.append(index)
    return PdfInspection(path, total, text_pages, tuple(scan), tuple(no_text))


def _as_lines(value) -> list[str]:
    """MinerU 的 caption/footnote 有时是字符串、有时是列表，两种都要接受。"""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def table_html_to_text(html_text: str) -> str:
    """把 MinerU 的 table_body（HTML）转成保留行结构的可检索文本。

    不能直接取整段可见文字：那样单元格会连成一片，`|` 分隔和逐行换行是表格类查询
    （"某个州某年的数值"）能命中的前提。
    """
    soup = BeautifulSoup(html_text, "lxml")
    rows = []
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if any(cells):
            rows.append(" | ".join(cells))
    if rows:
        return "\n".join(rows)
    # 不是标准表格结构时退回整段可见文字：宁可少一点结构，也不能返回空把内容丢掉。
    return soup.get_text(" ", strip=True)


def _block_text(block: dict) -> str:
    """按块类型取正文。

    表格块的字段是 `table_body` 而**没有** `text`：只读 `text` 会把整张表丢成空串，
    而块类型已经在保留名单里，连"丢弃了几个块"的计数都不会涨，等于无声丢失。
    """
    kind = block.get("type")
    if kind == "table":
        parts = _as_lines(block.get("table_caption"))
        body = block.get("table_body")
        if body:
            parts.append(table_html_to_text(str(body)))
        parts.extend(_as_lines(block.get("table_footnote")))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if kind in DROPPED_BLOCK_TYPES:
        return ""
    text = block.get("text")
    if text:
        return str(text).strip()
    # 未知类型但带表格正文时同样取出来，避免 MinerU 改版后表格再次静默消失。
    body = block.get("table_body")
    return table_html_to_text(str(body)).strip() if body else ""


def ocr_pdf_pages(
    path: Path,
    source_path: str,
    *,
    base_url: str = MINERU_DEFAULT_URL,
    timeout: float = 900.0,
    lang: str = "ch",
) -> list[ParsedUnit]:
    """调用 MinerU 做 OCR，**按物理页逐个返回**解析单元。

    只保留正文与表格块；页眉/页脚/页码被丢弃并计入 warnings，便于复核。
    输出必须覆盖 1..N 全部页：缺页（该页有整页图像却没识别出内容）和超出 PDF 页数的
    `page_idx` 都直接报错——前者会让扫描页无声消失，后者会把错误页码写进索引。
    """
    inspection = inspect_pdf(path)
    by_page, dropped, empty_blocks = _fetch_ocr_blocks(
        path, base_url=base_url, timeout=timeout, lang=lang
    )
    out_of_range = sorted(page for page in by_page if page < 1 or page > inspection.pages)
    if out_of_range:
        raise RuntimeError(
            f"MinerU 返回的页码超出来源PDF页数：{path.name} 共 {inspection.pages} 页，"
            f"却返回第 {out_of_range} 页"
        )
    missing = [page for page in inspection.scan_pages if not by_page.get(page)]
    if missing:
        raise RuntimeError(
            f"OCR 未返回第 {_page_range(tuple(missing))} 页的内容：{path.name}。"
            "这些页有整页图像却没有识别结果，不能当成空页跳过。"
        )

    units = []
    for page in range(1, inspection.pages + 1):
        texts = by_page.get(page, [])
        notes = []
        if not texts:
            notes.append(f"page_{page}_no_ocr_text")
        units.append(ParsedUnit(
            source_path=source_path,
            parser=OCR_PAGE_PARSER,
            text=("\n".join(texts) + "\n") if texts else "",
            page_number=page,
            warnings=[
                "ocr_derived",
                f"{dropped} non-body blocks dropped",
                f"{empty_blocks} body blocks yielded no text",
                *notes,
            ],
        ))
    return units


def _fetch_ocr_blocks(
    path: Path, *, base_url: str, timeout: float, lang: str
) -> tuple[dict[int, list[str]], int, int]:
    """提交一次 OCR 请求，返回 (页号→正文块, 丢弃块数, 取不到正文的正文块数)。"""
    url = f"{base_url.rstrip('/')}/file_parse"
    with path.open("rb") as handle:
        response = httpx.post(
            url,
            files={"files": (path.name, handle, "application/pdf")},
            data={
                "lang_list": lang,
                "return_md": "false",
                "return_content_list": "true",
                "parse_method": "ocr",
            },
            timeout=timeout,
        )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or {}
    entry = results.get(path.name) or next(iter(results.values()), None)
    if entry is None:
        raise RuntimeError(f"MinerU 未返回 {path.name} 的解析结果")
    blocks = entry.get("content_list")
    if isinstance(blocks, str):
        blocks = json.loads(blocks)
    if not blocks:
        raise RuntimeError(f"MinerU 返回空的 content_list：{path.name}")

    by_page: dict[int, list[str]] = {}
    dropped = 0
    empty_blocks = 0
    for block in blocks:
        page_index = block.get("page_idx")
        if not isinstance(page_index, int):
            dropped += 1
            continue
        if block.get("type") in DROPPED_BLOCK_TYPES:
            dropped += 1
            continue
        text = _block_text(block)
        if text:
            # MinerU 的 page_idx 从 0 开始，这里就统一成 1-based。
            by_page.setdefault(page_index + 1, []).append(text)
        else:
            empty_blocks += 1
    return by_page, dropped, empty_blocks


def parse_pdf_hybrid(
    path: Path,
    source_path: str,
    *,
    base_url: str = MINERU_DEFAULT_URL,
    timeout: float = 900.0,
    lang: str = "ch",
) -> list[ParsedUnit]:
    """文本层页保留文本层，只有扫描页用 OCR 结果。

    整份重跑 OCR 会把本来完好的文本层换成识别文本（表格、公式和排版都会退化），
    因此混合件必须逐页取来源，并让每一页的 parser 如实反映它来自哪里。
    """
    inspection = inspect_pdf(path)
    ocr_units = {unit.page_number: unit for unit in ocr_pdf_pages(
        path, source_path, base_url=base_url, timeout=timeout, lang=lang
    )}
    text_units = {unit.page_number: unit for unit in parse_pdf_pages(path, source_path)}

    merged = []
    for page in range(1, inspection.pages + 1):
        if page in inspection.scan_pages:
            merged.append(ocr_units[page])
            continue
        unit = text_units.get(page)
        if unit is None:  # 文本层没产出这一页：只能用 OCR 结果，绝不静默丢页。
            merged.append(ocr_units[page])
            continue
        merged.append(unit)
    return merged
