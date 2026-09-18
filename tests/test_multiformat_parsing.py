from pathlib import Path

import pymupdf

from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content
from wenjin.ingestion.multiformat import (
    load_retrieval_documents,
    parse_file,
    parse_line_mapped,
)


def test_html_keeps_source_line_numbers(tmp_path: Path):
    path = tmp_path / "doc.html"
    path.write_text("<h1>Title</h1>\n<script>noise()</script>\n<p>Evidence <b>value</b></p>\n")
    unit = parse_line_mapped(path, "doc.html", "html-line-v1")
    assert unit.text.splitlines() == ["Title", "", "Evidence value"]
    assert unit.start_line == 1 and unit.end_line == 3


def test_xml_keeps_element_attributes_and_line_numbers(tmp_path: Path):
    path = tmp_path / "schema.xsd"
    path.write_text('<element name="WMS_Capabilities">\n<attribute fixed="1.3.0"/>\n')
    unit = parse_line_mapped(path, "schema.xsd", "xml-line-v1")
    assert 'element name="WMS_Capabilities"' in unit.text.splitlines()[0]
    assert 'fixed="1.3.0"' in unit.text.splitlines()[1]


def test_man_keeps_option_names_on_original_line(tmp_path: Path):
    path = tmp_path / "tool.1"
    path.write_text(".TP\n.B \\-\\-bbox west,south,east,north\n")
    unit = parse_line_mapped(path, "tool.1", "man-roff-line-v1")
    assert unit.text.splitlines()[1] == "--bbox west,south,east,north"


def test_pdf_keeps_physical_pages(tmp_path: Path):
    path = tmp_path / "doc.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), "first")
        pdf.new_page().insert_text((50, 50), "second")
        pdf.save(path)
    units = parse_file(path, "doc.pdf")
    assert [u.page_number for u in units] == [1, 2]
    assert "second" in units[1].text


def test_manifest_loader_checks_hash_and_keeps_logical_identity(tmp_path: Path):
    raw = tmp_path / "raw_data/demo/1.0/doc.html"
    raw.parent.mkdir(parents=True)
    raw.write_text("<p>Evidence</p>\n", encoding="utf-8")
    hashed = hash_content(raw.read_bytes(), normalization=TEXT_NEWLINE_LF_V1,
                          encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(__import__("json").dumps({
        "library": "demo", "version": "1.0", "raw_data_subdir": "demo/1.0",
        "files": [{"path": "doc.html", "retrieval_eligible": True,
                   "raw_sha256": hashed.raw_sha256,
                   "canonical_sha256": hashed.canonical_sha256,
                   "normalization": TEXT_NEWLINE_LF_V1, "encoding": "utf-8",
                   "logical_document_id": "demo:doc", "parser": "html-line-v1"}],
    }), encoding="utf-8")
    document = load_retrieval_documents(manifest, tmp_path / "raw_data")[0]
    assert document["source_path"] == "raw_data/demo/1.0/doc.html"
    assert document["logical_document_id"] == "demo:doc"
    assert document["text"] == "Evidence\n"
