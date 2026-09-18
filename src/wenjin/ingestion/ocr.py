"""扫描件检测与 MinerU OCR。

文本层 PDF 走 `pymupdf-page-v1`，保留"一条物理页对应一条记录"的契约。扫描件没有文本层，
继续走原路径只会得到空页并**静默丢失**，所以这里做两件事：

1. `inspect_pdf` 在导入前判定"哪些页没有文本层"，把结果交给调用方决定；
2. `ocr_pdf_pages` 用本机 MinerU 的 `/file_parse` 取 `content_list`，按 `page_idx` 还原
   物理页码，产出和文本层 PDF 同样结构的 ParsedUnit。

OCR 文本不是原文，必须显式标注：解析器名固定为 `mineru-ocr-page-v1`，调用方据此判断
证据来自 OCR 而不是文本层。
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymupdf

from wenjin.ingestion.multiformat import ParsedUnit

MINERU_DEFAULT_URL = "http://127.0.0.1:8000"
OCR_PARSER = "mineru-ocr-page-v1"
# 页眉、页脚和页码对检索是噪声（页码尤其会污染"第几页"这类查询）；正文和表格保留。
OCR_TEXT_TYPES = {"text", "table"}


@dataclass(frozen=True)
class MineruStatus:
    available: bool
    base_url: str
    detail: str


@dataclass(frozen=True)
class PdfInspection:
    """一个 PDF 的文本层情况；empty_pages 为 1-based 物理页号。"""

    path: Path
    pages: int
    text_pages: int
    empty_pages: tuple[int, ...]

    @property
    def needs_ocr(self) -> bool:
        return bool(self.empty_pages)

    @property
    def fully_scanned(self) -> bool:
        return self.pages > 0 and self.text_pages == 0


@dataclass(frozen=True)
class OcrDecision:
    """是否启用OCR，以及被排除的文件和各条给用户看的说明。"""

    enabled: bool
    excluded: dict[Path, str] = field(default_factory=dict)
    messages: tuple[str, ...] = ()


def describe_scan_need(inspections: list[PdfInspection]) -> tuple[str, ...]:
    return tuple(
        f"{item.path.name}：{item.pages} 页，其中 {len(item.empty_pages)} 页没有文本层"
        + ("（整份都是扫描件）" if item.fully_scanned else "（部分页是扫描件）")
        for item in inspections
    )


def decide_ocr(
    inspections: list[PdfInspection],
    *,
    status: MineruStatus,
    mode: str = "ask",
    assume_yes: bool = False,
    ask: Callable[[str], bool] | None = None,
) -> OcrDecision:
    """决定是否对扫描件启用OCR。

    只做决策，不打印也不读输入：`ask`由调用方注入（CLI用input，服务用别的确认方式，
    测试用桩函数），所以这段策略可以在任何入口复用。
    """
    if not inspections:
        return OcrDecision(False, messages=("没有检测到需要OCR的文件",))
    if mode == "never":
        return OcrDecision(
            False,
            excluded={item.path: "扫描件，未启用OCR：只归档、不参与检索" for item in inspections},
            messages=("已按 --ocr never 跳过OCR，扫描件只归档",),
        )
    if not status.available:
        reason = f"MinerU 服务不可用（{status.base_url}：{status.detail}）"
        if mode == "always":
            raise RuntimeError(f"{reason}；无法按 --ocr always 执行。请先启动 MinerU。")
        return OcrDecision(
            False,
            excluded={item.path: f"扫描件，{reason}：只归档、不参与检索" for item in inspections},
            messages=(reason, "扫描件改为只归档、不参与检索"),
        )
    if mode == "always" or assume_yes:
        return OcrDecision(True, messages=(f"MinerU 服务在线（{status.base_url}），已启用OCR",))
    if ask is None:
        raise RuntimeError(
            "需要用户确认是否启用OCR，但调用方没有提供确认方式。"
            "请用 --ocr always / --ocr never，或加 --yes。"
        )
    prompt = "\n".join((
        "检测到需要OCR的扫描件：",
        *(f"  - {line}" for line in describe_scan_need(inspections)),
        f"MinerU 服务在线（{status.base_url}），OCR每页约需1秒。",
        "是否启用 MinerU OCR 处理这些文件？[y/N] ",
    ))
    if ask(prompt):
        return OcrDecision(True, messages=("用户确认启用OCR",))
    return OcrDecision(
        False,
        excluded={item.path: "扫描件，用户选择不启用OCR：只归档、不参与检索"
                  for item in inspections},
        messages=("用户选择不启用OCR，扫描件只归档",),
    )


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


def inspect_pdf(path: Path) -> PdfInspection:
    """统计每个物理页的文本层字符数，找出没有文本层的页。"""
    empty: list[int] = []
    text_pages = 0
    with pymupdf.open(path) as pdf:
        total = len(pdf)
        for index, page in enumerate(pdf, start=1):
            if page.get_text().strip():
                text_pages += 1
            else:
                empty.append(index)
    return PdfInspection(path, total, text_pages, tuple(empty))


def ocr_pdf_pages(
    path: Path,
    source_path: str,
    *,
    base_url: str = MINERU_DEFAULT_URL,
    timeout: float = 900.0,
    lang: str = "ch",
) -> list[ParsedUnit]:
    """调用 MinerU 做 OCR，按物理页返回解析单元。

    只保留正文与表格块；页眉/页脚/页码被丢弃并计入 warnings，便于复核。
    """
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
    for block in blocks:
        page_index = block.get("page_idx")
        if not isinstance(page_index, int):
            dropped += 1
            continue
        if block.get("type") not in OCR_TEXT_TYPES:
            dropped += 1
            continue
        text = (block.get("text") or "").strip()
        if text:
            by_page.setdefault(page_index, []).append(text)

    units = []
    for page_index in sorted(by_page):
        text = "\n".join(by_page[page_index])
        units.append(ParsedUnit(
            source_path=source_path,
            parser=OCR_PARSER,
            text=text + "\n",
            page_number=page_index + 1,  # MinerU 的 page_idx 从 0 开始，这里统一成 1-based
            warnings=[f"ocr_derived; {dropped} non-body blocks dropped"],
        ))
    if not units:
        raise RuntimeError(f"OCR 后没有可用正文：{path.name}")
    return units
