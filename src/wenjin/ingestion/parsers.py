"""两个可独立对比的 PDF 解析器；本模块不提供 OCR。

保留纯文本与表格二维结构，避免把“提取到了文本”和“还原了关系”混为一谈。
"""

import hashlib
from pathlib import Path

import pdfplumber
import pymupdf

from wenjin.schemas import ParsedPage, Table


def parse_pdf(path: Path, backend: str, *, table_settings: dict | None = None) -> list[ParsedPage]:
    """读取 PDF 的所有页；扫描页保留并标记，不静默删除。

    pymupdf：提取原生文字层。
    pdfplumber：同时提取文字及 table_settings 指定方式的表格。
    两者都不会因为存在图片而自动 OCR。
    """
    if backend not in {"pymupdf", "pdfplumber"}:
        raise ValueError(f"Unsupported PDF parser: {backend}")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = []
    if backend == "pymupdf":
        with pymupdf.open(path) as pdf:
            for index, page in enumerate(pdf):
                text = page.get_text(sort=True)
                result.append(
                    ParsedPage(
                        path.as_posix(),
                        sha,
                        index + 1,
                        backend,
                        text,
                        image_count=len(page.get_images()),
                        warnings=[] if text.strip() else ["no_extracted_text; inspect OCR need"],
                    )
                )
    else:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                tables = [
                    Table(tuple(table.bbox), table.extract())
                    for table in page.find_tables(table_settings or {})
                ]
                result.append(
                    ParsedPage(
                        path.as_posix(),
                        sha,
                        page.page_number,
                        backend,
                        text,
                        tables,
                        image_count=len(page.images),
                        warnings=[] if text.strip() else ["no_extracted_text; inspect OCR need"], #判断能否提取文字，发现异常时先记录，不要未经确认就替用户做不可逆决定
                    )
                )
    return result
