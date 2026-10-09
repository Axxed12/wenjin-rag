"""Source-native retrieval context contracts for HTML and PDF locators."""

from __future__ import annotations

import pymupdf

from wenjin.retrieval.rerank import candidate_document
from wenjin.retrieval.source_context import (
    OGC_3DTILES_PDF_PROFILE,
    candidate_document_with_context,
    extract_html_structure,
    extract_pdf_structure,
    metadata_for_candidate,
)


def _pdf_fixture() -> bytes:
    document = pymupdf.open()
    for _ in range(6):
        document.new_page()

    # The A.8.5 destination is on the same physical page as A.9, and its visible
    # heading deliberately wraps across PDF text lines.
    document[3].insert_text((72, 72), "A.8.5. Previous")
    document[3].insert_text((72, 90), "section")
    document[3].insert_text((72, 120), "Table A.10 — Class Property Properties")
    document[3].insert_text((72, 140), "A.9. Class Property")
    document[4].insert_text((72, 72), "Table A.11 — First title")
    document[4].insert_text((72, 90), "Table A.12 — Second title")
    document[5].insert_text((72, 72), "Table A.13 — Single title")
    document.set_toc(
        [
            [1, "7. Tile Formats", 1],
            [2, "7.7. Point Cloud", 2],
            [1, "Annex A (normative) Properties Reference", 3],
            [2, "A.8.5. Previous section", 4],
            [2, "A.9. Class Property", 4],
        ]
    )
    return document.tobytes()


def _candidate(
    *,
    source_path: str,
    text: str,
    start_line: int | None = None,
    end_line: int | None = None,
    page_number: int | None = None,
) -> dict:
    return {
        "chunk_id": "fixture-chunk",
        "source_path": source_path,
        "text": text,
        "start_line": start_line,
        "end_line": end_line,
        "page_number": page_number,
    }


def test_unknown_pdf_profile_returns_no_structure_without_parsing_pdf() -> None:
    structure = extract_pdf_structure(b"not a PDF", profile="unregistered-profile")

    assert structure.kind == "pdf"
    assert structure.by_position == {}
    assert structure.diagnostics == {
        "supported": False,
        "reason": "unregistered_pdf_structure_profile",
    }


def test_html_metadata_is_omitted_for_cross_section_or_multiple_titles() -> None:
    html = "\n".join(
        [
            "<h1>Documentation</h1>",
            "<h2>A.9. Class Property</h2>",
            '<p class="TableTitle">Table A.10 — Class Property Properties</p>',
            "<p>Class Property rows.</p>",
            '<p class="TableTitle">Table A.11 — First</p><p class="TableTitle">Table A.12 — Second</p>',
            "<h2>A.20. Point Cloud</h2>",
            '<p class="TableTitle">Table A.20 — Point Cloud Feature Table</p>',
            "<p>Point Cloud rows.</p>",
        ]
    ).encode("utf-8")
    structure = extract_html_structure(html)

    scoped = _candidate(
        source_path="doc.html",
        text="Table A.10 — Class Property Properties\nClass Property rows.",
        start_line=2,
        end_line=4,
    )
    scoped_before = scoped.copy()
    scoped_metadata = metadata_for_candidate(scoped, {"doc.html": structure})
    assert scoped_metadata["section_path"] == ["Documentation", "A.9. Class Property"]
    assert scoped_metadata["table_title"] == "Table A.10 — Class Property Properties"
    assert scoped_metadata["metadata_status"] == "mapped"
    assert scoped == scoped_before
    assert candidate_document_with_context(scoped, scoped_metadata).endswith(scoped["text"])

    same_line_multiple_titles = _candidate(
        source_path="doc.html",
        text="Table A.11 — First Table A.12 — Second",
        start_line=5,
        end_line=5,
    )
    ambiguous_titles = metadata_for_candidate(same_line_multiple_titles, {"doc.html": structure})
    assert ambiguous_titles["table_title"] is None
    assert ambiguous_titles["ambiguous_table_titles"] is True

    cross_section = _candidate(
        source_path="doc.html",
        text="Class Property rows. Table A.20 — Point Cloud Feature Table Point Cloud rows.",
        start_line=4,
        end_line=8,
    )
    cross_metadata = metadata_for_candidate(cross_section, {"doc.html": structure})
    assert cross_metadata["section_path"] == []
    assert cross_metadata["table_title"] is None
    assert cross_metadata["metadata_status"] == "ambiguous_source_structure"
    assert cross_metadata["ambiguous_sections"] is True


def test_html_multiple_headings_on_one_source_line_are_ambiguous_only_for_that_line() -> None:
    html = (
        "<h1>4. Terms and definitions</h1>Definition prose. "
        "<h2>4.1 Availability</h2>\n"
        "Availability body."
    ).encode("utf-8")
    structure = extract_html_structure(html)
    same_line = _candidate(
        source_path="doc.html", text="definition and availability", start_line=1, end_line=1
    )
    next_line = _candidate(
        source_path="doc.html", text="Availability body.", start_line=2, end_line=2
    )

    ambiguous = metadata_for_candidate(same_line, {"doc.html": structure})
    assert ambiguous["section_path"] == []
    assert ambiguous["ambiguous_sections"] is True
    later = metadata_for_candidate(next_line, {"doc.html": structure})
    assert later["section_path"] == ["4. Terms and definitions", "4.1 Availability"]
    assert later["ambiguous_sections"] is False


def test_pdf_context_is_page_scoped_and_refuses_mixed_or_ambiguous_labels() -> None:
    structure = extract_pdf_structure(
        _pdf_fixture(), profile=OGC_3DTILES_PDF_PROFILE
    )
    assert structure.diagnostics["supported"] is True

    # A.8.5 is visibly split across lines on the same page as A.9. The outline
    # page context cannot safely label that whole page as A.9.
    mixed_page = _candidate(
        source_path="doc.pdf",
        text="A.8.5. Previous section\nTable A.10 — Class Property Properties\nA.9. Class Property",
        page_number=4,
    )
    mixed_metadata = metadata_for_candidate(mixed_page, {"doc.pdf": structure})
    assert mixed_metadata["section_scope_ambiguous"] is True
    assert mixed_metadata["page_context"] == []
    assert mixed_metadata["table_title"] is None
    assert mixed_metadata["metadata_status"] == "ambiguous_page_sections"

    multiple_titles = _candidate(
        source_path="doc.pdf",
        text="Table A.11 — First title\nTable A.12 — Second title",
        page_number=5,
    )
    title_metadata = metadata_for_candidate(multiple_titles, {"doc.pdf": structure})
    assert title_metadata["page_context"] == [
        "Annex A (normative) Properties Reference",
        "A.9. Class Property",
    ]
    assert title_metadata["table_title"] is None
    assert title_metadata["metadata_status"] == "ambiguous_page_table_titles"

    title_missing_from_candidate = _candidate(
        source_path="doc.pdf",
        text="generic continuation content",
        page_number=6,
    )
    no_title = metadata_for_candidate(title_missing_from_candidate, {"doc.pdf": structure})
    assert no_title["page_context"][-1] == "A.9. Class Property"
    assert no_title["table_title"] is None

    reranker_input = candidate_document_with_context(title_missing_from_candidate, no_title)
    assert "Source page context (derived from PDF outline; physical-page level)" in reranker_input
    assert reranker_input.endswith(title_missing_from_candidate["text"])
    assert title_missing_from_candidate["text"] == "generic continuation content"


def test_complete_self_describing_json_keeps_original_reranker_document() -> None:
    candidate = _candidate(
        source_path="schema.html",
        text='{"$schema":"https://json-schema.org/draft/2020-12/schema",'
        '"$id":"urn:example:property","required":["values"]}',
        start_line=10,
        end_line=10,
    )
    before = candidate.copy()
    metadata = {
        "section_path": ["Annex B", "B.23 Property Table Property"],
        "page_context": [],
        "table_title": None,
    }

    assert candidate_document_with_context(candidate, metadata) == candidate_document(candidate)
    assert candidate == before


def test_schema_words_in_prose_or_json_without_root_identity_do_not_bypass_context() -> None:
    metadata = {"section_path": ["Annex B"], "page_context": [], "table_title": None}
    candidates = [
        _candidate(source_path="doc.html", text="The $schema and $id fields identify a schema."),
        _candidate(source_path="doc.html", text='{"title":"$schema and $id"}'),
    ]

    for candidate in candidates:
        before = candidate.copy()
        document = candidate_document_with_context(candidate, metadata)
        assert document.startswith("HTML heading path: Annex B\n")
        assert document.endswith(candidate["text"])
        assert candidate == before
