"""验证证据契约，不把某一份资料的解析结果写死为通用规律。"""

from pathlib import Path

import pymupdf
import pytest

from wenjin.ingestion.parsers import parse_pdf


@pytest.mark.parametrize("backend", ["pymupdf", "pdfplumber"])
def test_preserve_empty_page_and_physical_page_number(tmp_path: Path, backend: str):
    path = tmp_path / "two-pages.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), "Evidence on page one")
        pdf.new_page()  # 空白页也必须保留，不能造成后面页码错位。
        pdf.save(path)
    pages = parse_pdf(path, backend)
    assert [p.page_number for p in pages] == [1, 2]
    assert "Evidence" in pages[0].text
    assert pages[1].text == "" and pages[1].warnings
    document = pages[0].to_document()
    assert document.metadata["source_sha256"] == pages[0].source_sha256
    assert document.metadata["page_number"] == 1


def test_no_hidden_ocr_for_image_only_pdf(tmp_path: Path):
    path = tmp_path / "scan.pdf"
    with pymupdf.open() as source:
        page = source.new_page()
        page.insert_text((50, 50), "Visible but not searchable")
        pix = page.get_pixmap()
        with pymupdf.open() as target:
            target.new_page().insert_image(page.rect, pixmap=pix)
            target.save(path)
    for backend in ["pymupdf", "pdfplumber"]:
        page = parse_pdf(path, backend)[0]
        assert page.image_count > 0 and not page.text.strip()
        assert page.warnings


def test_reject_unknown_backend(tmp_path: Path):
    with pytest.raises(ValueError, match="Unsupported"):
        parse_pdf(tmp_path / "not-present.pdf", "guessed-parser")
