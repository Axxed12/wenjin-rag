"""新语料的多格式解析基线。

文本格式保持“一条源文件行对应一条解析行”，便于证据继续引用原始行号；PDF按物理页
生成记录。解析结果仍是候选输入，不等于已经适合建索引。
"""

import html
import json
import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from wenjin.content_hashing import BINARY_IDENTITY_V1, hash_content


@dataclass(frozen=True)
class ParsedUnit:
    source_path: str
    parser: str
    text: str
    start_line: int | None = None
    end_line: int | None = None
    page_number: int | None = None
    warnings: list[str] = field(default_factory=list)


def _html_line(line: str) -> str:
    """只抽取当前源代码行的可见文字，并保留源行的位置。"""
    # 单行代码、URL或文件名会触发BeautifulSoup的启发式警告；这里输入本来就是源码行。
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(line, "lxml")
    for tag in soup(["script", "style", "svg"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _xml_line(line: str) -> str:
    """保留XML元素名、属性和值；这些结构本身是可检索证据。"""
    text = re.sub(r"<\?xml[^>]*\?>", "", line)
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"<([^!?][^>]*)>", r" \1 ", text)
    return " ".join(html.unescape(text).split())


def _roff_line(line: str) -> str:
    """移除常见man控制符，但保留参数名和值。"""
    text = re.sub(r"^\.[A-Z][A-Z0-9]*\s*", "", line)
    text = re.sub(r"\\f[BRIP]", "", text)
    text = text.replace("\\-", "-").replace("\\&", "")
    text = re.sub(r"\\%<[^>]*>", "", text)
    return " ".join(text.split())


def parse_line_mapped(path: Path, source_path: str, parser: str) -> ParsedUnit:
    """解析文本并保持行数完全不变。"""
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    if parser == "html-line-v1":
        parsed = [_html_line(line) for line in raw_lines]
    elif parser == "xml-line-v1":
        parsed = [_xml_line(line) for line in raw_lines]
    elif parser == "man-roff-line-v1":
        parsed = [_roff_line(line) for line in raw_lines]
    elif parser in {"text-line-v1", "rst-line-v1"}:
        parsed = raw_lines
    else:
        raise ValueError(f"不支持的行映射解析器：{parser}")
    return ParsedUnit(
        source_path=source_path,
        parser=parser,
        text="\n".join(parsed) + ("\n" if raw_lines else ""),
        start_line=1 if raw_lines else None,
        end_line=len(raw_lines) if raw_lines else None,
        warnings=[] if any(line.strip() for line in parsed) else ["no_extracted_text"],
    )


def parse_pdf_pages(path: Path, source_path: str) -> list[ParsedUnit]:
    """用PyMuPDF按物理页解析；不隐藏OCR，也不把页码换算成印刷页码。"""
    units = []
    with pymupdf.open(path) as pdf:
        for index, page in enumerate(pdf):
            text = page.get_text(sort=True)
            units.append(ParsedUnit(
                source_path=source_path,
                parser="pymupdf-page-v1",
                text=text,
                page_number=index + 1,
                warnings=[] if text.strip() else ["no_extracted_text; inspect OCR need"],
            ))
    return units


def parser_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "html-line-v1",
        ".xsd": "xml-line-v1",
        ".xml": "xml-line-v1",
        ".1": "man-roff-line-v1",
        ".rst": "rst-line-v1",
    }.get(suffix, "text-line-v1")


def parse_file(
    path: Path,
    source_path: str,
    *,
    parser: str | None = None,
    mineru_url: str | None = None,
    mineru_timeout: float = 900.0,
) -> list[ParsedUnit]:
    """按manifest记录的parser解析；未指定时按后缀推断。

    `parser="mineru-ocr-page-v1"` 表示这是扫描件，必须走OCR：光有文本层判定不够，
    因为重建索引时也要复现同一份解析结果。
    """
    if parser == "mineru-ocr-page-v1":
        # 延迟导入：ocr模块要复用本模块的ParsedUnit，模块级导入会形成循环。
        from wenjin.ingestion.ocr import MINERU_DEFAULT_URL, ocr_pdf_pages

        return ocr_pdf_pages(
            path,
            source_path,
            base_url=mineru_url or MINERU_DEFAULT_URL,
            timeout=mineru_timeout,
        )
    if path.suffix.lower() == ".pdf":
        return parse_pdf_pages(path, source_path)
    return [parse_line_mapped(path, source_path, parser or parser_for_path(path))]


def load_retrieval_documents(manifest_path: Path, raw_data_root: Path) -> list[dict]:
    """读取manifest的精确白名单，校验双Hash，再返回可供切片的解析单元。"""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_root = (raw_data_root / manifest["raw_data_subdir"]).resolve()
    documents = []
    for entry in manifest["files"]:
        if not entry.get("retrieval_eligible"):
            continue
        path = (raw_root / entry["path"]).resolve()
        if not path.is_relative_to(raw_root) or not path.is_file():
            raise ValueError(f"语料路径无效：{entry['path']}")
        raw = path.read_bytes()
        normalization = entry["normalization"]
        hashed = hash_content(
            raw,
            normalization=normalization,
            encoding=None if normalization == BINARY_IDENTITY_V1 else entry.get("encoding", "utf-8"),
        )
        if hashed.raw_sha256 != entry["raw_sha256"] or (
            hashed.canonical_sha256 != entry["canonical_sha256"]
        ):
            raise ValueError(f"双Hash不匹配：{entry['path']}")
        source_path = f"raw_data/{manifest['raw_data_subdir']}/{entry['path']}"
        units = parse_file(path, source_path, parser=entry.get("parser"))
        expected_parser = entry.get("parser")
        if expected_parser and any(unit.parser != expected_parser for unit in units):
            raise ValueError(f"解析器与manifest不一致：{entry['path']}")
        for unit in units:
            documents.append({
                "source_path": source_path,
                "version": manifest["version"],
                "library": manifest["library"],
                "text": unit.text,
                "page_number": unit.page_number,
                "parser": unit.parser,
                # warnings必须传下去：不传的话扫描件会以0 chunk静默消失，没人知道。
                "warnings": unit.warnings,
                "raw_sha256": hashed.raw_sha256,
                "canonical_sha256": hashed.canonical_sha256,
                "normalization": hashed.normalization,
                "logical_document_id": entry["logical_document_id"],
            })
    return documents
