"""第四批：PDF 解析路径、表格保真、OCR 来源标记与缺页报告。

每条测试都对应一个已复现的缺陷，不是"跑一遍看看"：

- D1 普通数字 PDF 只因夹了一页空白就被整份重跑 OCR，用识别文本盖掉本来完好的文本层；
- D2 表格块的字段是 `table_body` 而没有 `text`，只读 `text` 会把整张表丢成空串，
  且因为块类型在保留名单里，连"丢弃了几个块"的计数都不会涨；
- D3 OCR 标记只写在 manifest 里，没有进 chunk 和索引，证据无法说明正文是识别来的；
- D4 MinerU 少返回一页、或返回超出 PDF 页数的页码，都会被静默接受。

字段名（`page_idx` / `table_body` / `table_caption` / `text_level`）来自本机
MinerU 3.4.5 的真实响应，见 `artifacts/corpus-acceptance-004/`。
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pymupdf
import pytest

from wenjin.ingestion import ocr as ocr_module
from wenjin.ingestion.chunking import block_chunks
from wenjin.ingestion.intake import PdfPlan, plan_corpus
from wenjin.ingestion.ocr import (
    PdfInspection,
    choose_pdf_parser,
    decide_ocr,
    describe_scan_need,
    inspect_pdf,
    parse_pdf_hybrid,
    skipped_scan_pages,
)
from wenjin.parser_names import (
    HYBRID_PAGE_PARSER,
    OCR_PAGE_PARSER,
    TEXT_PAGE_PARSER,
    allowed_unit_parsers,
    is_ocr_derived,
)

ONLINE = ocr_module.MineruStatus(True, "http://127.0.0.1:8000", "服务在线")


# --------------------------------------------------------------------------- 合成PDF


def _text_page(document, text: str):
    page = document.new_page()
    page.insert_text((72, 100), text, fontsize=14)
    return page


def _image_page(document, *, width: float = 595, height: float = 842):
    """整页位图、无文字层：这就是"扫描页"的最小形态。"""
    page = document.new_page(width=width, height=height)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80))
    pixmap.set_rect(pixmap.irect, (240, 240, 240))
    page.insert_image(page.rect, pixmap=pixmap)
    return page


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("pdfs")

    document = pymupdf.open()
    _text_page(document, "Chapter one text layer")
    document.new_page()  # 真正的空白页：无文字、无图形、无图片
    _text_page(document, "Chapter two text layer")
    document.save(directory / "normal_with_blank.pdf")
    document.close()

    document = pymupdf.open()
    _image_page(document)
    _image_page(document)
    document.save(directory / "scanned.pdf")
    document.close()

    document = pymupdf.open()
    _text_page(document, "Mixed page one")
    _image_page(document)
    _text_page(document, "Mixed page three")
    document.save(directory / "mixed.pdf")
    document.close()

    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(100, 100, 400, 400), color=(0, 0, 0), width=2)
    document.save(directory / "vector_figure.pdf")
    document.close()
    return {path.stem: path for path in directory.glob("*.pdf")}


# ------------------------------------------------------------------- D1 三态判定


def test_blank_page_in_digital_pdf_does_not_trigger_ocr(pdfs):
    """原始缺陷：中间夹一页空白 → 整份被判定需要 OCR，文本层被识别文本替换。"""
    inspection = inspect_pdf(pdfs["normal_with_blank"])
    assert inspection.pages == 3
    assert inspection.text_pages == 2
    assert inspection.scan_pages == ()
    assert inspection.no_text_pages == (2,)
    assert inspection.needs_ocr is False


def test_vector_figure_page_is_not_a_scan(pdfs):
    """纯矢量图页没有文字层，但也没有整页图像，送 OCR 也取不到内容。"""
    inspection = inspect_pdf(pdfs["vector_figure"])
    assert inspection.scan_pages == ()
    assert inspection.no_text_pages == (1,)
    assert inspection.needs_ocr is False


def test_fully_scanned_pdf_is_detected(pdfs):
    inspection = inspect_pdf(pdfs["scanned"])
    assert inspection.scan_pages == (1, 2)
    assert inspection.text_pages == 0
    assert inspection.fully_scanned is True
    assert inspection.needs_ocr is True


def test_mixed_pdf_separates_text_pages_from_scan_pages(pdfs):
    inspection = inspect_pdf(pdfs["mixed"])
    assert inspection.text_pages == 2
    assert inspection.scan_pages == (2,)
    assert inspection.mixed is True


def test_scan_description_states_facts_not_conclusions(pdfs):
    """提示语只能陈述检测到的事实。

    旧措辞把"没有文本层"直接说成"是扫描件"，而空白页同样没有文本层；
    用户是在这段描述上点同意的，措辞必须和实际依据一致。
    """
    lines = describe_scan_need([inspect_pdf(pdfs["normal_with_blank"])])
    assert lines == ()  # 没有扫描页就不该出现在确认名单里


def test_scan_description_reports_image_basis_for_real_scan(pdfs):
    line = describe_scan_need([inspect_pdf(pdfs["scanned"])])[0]
    assert "整页图像" in line
    assert "没有可用文字层" in line
    assert "是扫描件" not in line


def test_mixed_description_says_text_layer_is_kept(pdfs):
    line = describe_scan_need([inspect_pdf(pdfs["mixed"])])[0]
    assert "保留文本层" in line


def test_decide_ocr_ignores_pdfs_without_scan_pages(pdfs):
    """没有扫描页的文件不该被牵连进"是否启用OCR"的名单和排除列表。"""
    decision = decide_ocr(
        [inspect_pdf(pdfs["normal_with_blank"]), inspect_pdf(pdfs["vector_figure"])],
        status=ONLINE,
    )
    assert decision.enabled is False
    assert decision.excluded == {}


def test_decide_ocr_only_excludes_files_that_need_it(pdfs):
    decision = decide_ocr(
        [inspect_pdf(pdfs["normal_with_blank"]), inspect_pdf(pdfs["scanned"])],
        status=ONLINE,
        mode="never",
    )
    assert set(decision.excluded) == {pdfs["scanned"]}


# --------------------------------------------------- D1b 按页取来源而不是整份重跑


def test_parser_choice_keeps_text_layer_when_possible(pdfs):
    assert choose_pdf_parser(inspect_pdf(pdfs["mixed"]), enabled=True) == HYBRID_PAGE_PARSER
    assert choose_pdf_parser(inspect_pdf(pdfs["scanned"]), enabled=True) == OCR_PAGE_PARSER
    # 没有扫描页时无论用户怎么选都不该走 OCR。
    for name in ("normal_with_blank", "vector_figure"):
        assert choose_pdf_parser(inspect_pdf(pdfs[name]), enabled=True) == TEXT_PAGE_PARSER


def test_declining_ocr_reports_the_pages_that_will_not_be_indexed(pdfs):
    inspection = inspect_pdf(pdfs["mixed"])
    parser = choose_pdf_parser(inspection, enabled=False)
    assert parser == TEXT_PAGE_PARSER
    assert skipped_scan_pages(inspection, parser) == [2]
    # 走了 OCR 的文件不算"被跳过的扫描页"。
    assert skipped_scan_pages(inspection, HYBRID_PAGE_PARSER) == []


def test_hybrid_parse_takes_each_page_from_its_own_source(pdfs, monkeypatch):
    """混合件必须逐页取来源：整份重跑 OCR 会毁掉本来完好的文本层。"""
    fake = _fake_mineru({"mixed.pdf": [
        {"type": "text", "text": "第二页的OCR识别结果", "page_idx": 1},
    ]})
    monkeypatch.setattr(ocr_module.httpx, "post", fake)
    units = parse_pdf_hybrid(pdfs["mixed"], "raw_data/x/mixed.pdf")

    assert [unit.page_number for unit in units] == [1, 2, 3]
    assert [unit.parser for unit in units] == [
        TEXT_PAGE_PARSER, OCR_PAGE_PARSER, TEXT_PAGE_PARSER
    ]
    assert units[0].text.strip() == "Mixed page one"
    assert units[1].text.strip() == "第二页的OCR识别结果"
    assert units[2].text.strip() == "Mixed page three"
    assert [unit.ocr_derived for unit in units] == [False, True, False]


def test_hybrid_parse_covers_every_page(pdfs, monkeypatch):
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"mixed.pdf": [
        {"type": "text", "text": "ocr", "page_idx": 1},
    ]}))
    units = parse_pdf_hybrid(pdfs["mixed"], "raw_data/x/mixed.pdf")
    assert [unit.page_number for unit in units] == [1, 2, 3]


# --------------------------------------------------------------------- D2 表格保真

# 真实 MinerU 3.4.5 的表格块：有 table_body，**没有** text 字段。
REAL_TABLE_BLOCK = {
    "type": "table",
    "page_idx": 0,
    "bbox": [0, 0, 100, 100],
    "img_path": "images/table.jpg",
    "table_caption": ["Table 1: Firearm background checks"],
    "table_body": (
        "<table><tr><td>State / Territory</td><td>Handgun</td><td>Long Gun</td></tr>"
        "<tr><td>Alabama</td><td>12345</td><td>6789</td></tr>"
        "<tr><td>Alaska</td><td>4321</td><td>987</td></tr></table>"
    ),
    "table_footnote": ["*Other: frames and receivers"],
}


def test_table_block_has_no_text_field_which_is_why_it_was_lost():
    """先固定住"为什么旧代码会丢表"，否则这条修复很容易被改回去。"""
    assert "text" not in REAL_TABLE_BLOCK
    assert REAL_TABLE_BLOCK["table_body"]


def test_table_body_is_extracted_with_row_structure():
    text = ocr_module._block_text(REAL_TABLE_BLOCK)
    lines = text.splitlines()
    # 表名、表头、两行数据、脚注都要在。
    assert "Firearm background checks" in text
    assert "State / Territory | Handgun | Long Gun" in text
    assert "Alabama | 12345 | 6789" in text
    assert "Alaska | 4321 | 987" in text
    assert "*Other: frames and receivers" in text
    assert len(lines) >= 5


def test_table_cells_stay_searchable():
    text = ocr_module._block_text(REAL_TABLE_BLOCK)
    for probe in ("Alabama", "Alaska", "12345", "Handgun"):
        assert probe in text


def test_table_caption_accepts_plain_string():
    """MinerU 的 caption 有时是字符串、有时是列表，两种都要接受。"""
    block = {**REAL_TABLE_BLOCK, "table_caption": "Table 2: 单字符串表名"}
    assert "Table 2: 单字符串表名" in ocr_module._block_text(block)


def test_unknown_block_type_with_table_body_is_not_dropped():
    """MinerU 改版换了类型名时，不能又变回静默丢表。"""
    block = {**REAL_TABLE_BLOCK, "type": "table_v2"}
    assert "Alabama" in ocr_module._block_text(block)


def test_non_table_html_falls_back_to_visible_text():
    """不是标准表格结构时也要取出文字，宁可少结构也不能返回空。"""
    text = ocr_module.table_html_to_text("<div><p>无表格结构的正文</p></div>")
    assert text == "无表格结构的正文"


@pytest.mark.parametrize("kind", ["header", "footer", "page_number", "image"])
def test_noise_blocks_are_still_dropped(kind):
    block = {"type": kind, "text": "OSG 3.6.5 Reference 12", "page_idx": 0}
    assert ocr_module._block_text(block) == ""


def test_header_and_page_number_do_not_enter_body(pdfs, monkeypatch):
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"scanned.pdf": [
        {"type": "header", "text": "OSG Reference", "page_idx": 0},
        {"type": "text", "text": "第一页正文", "page_idx": 0},
        {"type": "page_number", "text": "1", "page_idx": 0},
        {"type": "text", "text": "第二页正文", "page_idx": 1},
    ]}))
    units = ocr_module.ocr_pdf_pages(pdfs["scanned"], "raw_data/x/scanned.pdf")
    assert "第一页正文" in units[0].text
    assert "OSG Reference" not in units[0].text
    assert "1" not in units[0].text.replace("第一页正文", "")


def test_block_accounting_is_reported(pdfs, monkeypatch):
    """丢弃计数必须区分"按类型丢"和"取不到正文"，否则丢表看不出来。"""
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"scanned.pdf": [
        {"type": "header", "text": "h", "page_idx": 0},
        {"type": "text", "text": "第一页正文", "page_idx": 0},
        {"type": "text", "text": "   ", "page_idx": 1},
        {"type": "text", "text": "第二页正文", "page_idx": 1},
    ]}))
    units = ocr_module.ocr_pdf_pages(pdfs["scanned"], "raw_data/x/scanned.pdf")
    warnings = units[0].warnings
    assert "1 non-body blocks dropped" in warnings
    assert "1 body blocks yielded no text" in warnings


# ----------------------------------------------------------- D4 缺页与页码越界


def test_missing_scan_page_is_rejected_not_silently_dropped(pdfs, monkeypatch):
    """原始缺陷：少返回一页，那一页就无声消失。"""
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"scanned.pdf": [
        {"type": "text", "text": "只有第一页", "page_idx": 0},
    ]}))
    with pytest.raises(RuntimeError, match="OCR 未返回第 2 页"):
        ocr_module.ocr_pdf_pages(pdfs["scanned"], "raw_data/x/scanned.pdf")


def test_out_of_range_page_index_is_rejected(pdfs, monkeypatch):
    """原始缺陷：2 页的 PDF 返回第 7 页，错误页码会写进索引。"""
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"scanned.pdf": [
        {"type": "text", "text": "a", "page_idx": 0},
        {"type": "text", "text": "b", "page_idx": 1},
        {"type": "text", "text": "c", "page_idx": 6},
    ]}))
    with pytest.raises(RuntimeError, match="超出来源PDF页数"):
        ocr_module.ocr_pdf_pages(pdfs["scanned"], "raw_data/x/scanned.pdf")


def test_page_counting_stays_contiguous_when_a_page_has_no_content(pdfs, monkeypatch):
    """空页可以没有正文，但页码位必须保留，否则后续页码会整体前移。"""
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"scanned.pdf": [
        {"type": "text", "text": "第一页正文", "page_idx": 0},
        # 第 2 页没有任何块
    ]}))
    # scanned.pdf 两页都有整页图像，第 2 页没有结果就是缺页，必须报错。
    with pytest.raises(RuntimeError, match="OCR 未返回第 2 页"):
        ocr_module.ocr_pdf_pages(pdfs["scanned"], "raw_data/x/scanned.pdf")


def test_blank_page_without_ocr_result_is_allowed(pdfs, monkeypatch):
    """真正空白页本来就没有可识别内容，不该被当成缺页。"""
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"normal_with_blank.pdf": [
        {"type": "text", "text": "Chapter one text layer", "page_idx": 0},
        {"type": "text", "text": "Chapter two text layer", "page_idx": 2},
    ]}))
    units = ocr_module.ocr_pdf_pages(pdfs["normal_with_blank"], "raw_data/x/n.pdf")
    assert [unit.page_number for unit in units] == [1, 2, 3]
    assert units[1].text == ""
    assert "page_2_no_ocr_text" in units[1].warnings


def test_empty_content_list_is_rejected(pdfs, monkeypatch):
    monkeypatch.setattr(ocr_module.httpx, "post", _fake_mineru({"scanned.pdf": []}))
    with pytest.raises(RuntimeError, match="空的 content_list"):
        ocr_module.ocr_pdf_pages(pdfs["scanned"], "raw_data/x/scanned.pdf")


# --------------------------------------------------------- D3 OCR 标记进 chunk/索引


def test_chunk_carries_parser_and_ocr_marker():
    document = {
        "source_path": "raw_data/x/scan.pdf", "version": "1.0",
        "logical_document_id": "x:scan.pdf", "text": "第一页正文\n",
        "page_number": 1, "parser": OCR_PAGE_PARSER,
    }
    chunk = block_chunks(document, max_chars=1600)[0]
    assert chunk.parser == OCR_PAGE_PARSER
    assert chunk.ocr_derived is True

    document["parser"] = TEXT_PAGE_PARSER
    assert block_chunks(document, max_chars=1600)[0].ocr_derived is False


def test_ocr_marker_comes_from_parser_not_from_caller():
    assert is_ocr_derived(OCR_PAGE_PARSER) is True
    assert is_ocr_derived(HYBRID_PAGE_PARSER) is False
    assert is_ocr_derived(TEXT_PAGE_PARSER) is False
    assert is_ocr_derived(None) is False


def test_allowed_unit_parsers_cover_the_hybrid_case():
    assert allowed_unit_parsers(HYBRID_PAGE_PARSER) == {
        TEXT_PAGE_PARSER, OCR_PAGE_PARSER
    }
    assert allowed_unit_parsers(TEXT_PAGE_PARSER) == {TEXT_PAGE_PARSER}
    assert allowed_unit_parsers(None) is None


def test_bm25_round_trips_the_ocr_marker(tmp_path):
    from wenjin.indexing.bm25 import bm25_query, build_bm25_index
    from wenjin.ingestion.chunking import Chunk

    chunk = Chunk(
        chunk_id="1.0:raw_data/x/scan.pdf:page-1:blocks-split:0000",
        source_path="raw_data/x/scan.pdf", version="1.0", start_line=1, end_line=2,
        text="NICS background check table Alabama 12345\n", strategy="blocks-split",
        page_number=1, parser=OCR_PAGE_PARSER,
    )
    path = tmp_path / "bm25.sqlite"
    build_bm25_index(path, [chunk])
    with closing(sqlite3.connect(path)) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(chunks)")}
    assert {"parser", "ocr_derived"} <= columns

    hits = bm25_query(path, "Alabama", top_k=5)
    assert hits[0]["parser"] == OCR_PAGE_PARSER
    assert hits[0]["ocr_derived"] is True
    assert hits[0]["page_number"] == 1


def test_legacy_bm25_without_marker_columns_reads_unknown_not_false(tmp_path):
    """旧索引没有这两列时必须是 None："无法核验"不等于"确认不是OCR"。"""
    from wenjin.indexing.bm25 import bm25_query

    path = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(path)) as database:
        database.execute("""
            CREATE VIRTUAL TABLE chunks USING fts5(
                chunk_id UNINDEXED, source_path UNINDEXED, version UNINDEXED,
                start_line UNINDEXED, end_line UNINDEXED, strategy UNINDEXED,
                content_sha256 UNINDEXED, source_raw_sha256 UNINDEXED,
                source_canonical_sha256 UNINDEXED, normalization UNINDEXED,
                page_number UNINDEXED, logical_document_id UNINDEXED,
                retrieval_text, raw_text UNINDEXED, tokenize='unicode61')
        """)
        database.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("c1", "a/b.cpp", "1.0", 1, 1, "blocks", "h", "h", "h",
             "text-newline-lf-v1", None, "id", "Alabama 12345", "Alabama 12345"),
        )
        database.commit()
    hit = bm25_query(path, "Alabama", top_k=5)[0]
    assert hit["parser"] is None
    assert hit["ocr_derived"] is None


def test_evidence_record_exposes_the_ocr_marker():
    from wenjin.retrieval.service import evidence_record

    record = evidence_record(
        {
            "chunk_id": "c1", "version": "1.0", "source_path": "raw_data/x/scan.pdf",
            "start_line": 1, "end_line": 2, "text": "正文", "parser": OCR_PAGE_PARSER,
            "ocr_derived": True,
        },
        {},
        rank=1,
    )
    assert record["parser"] == OCR_PAGE_PARSER
    assert record["ocr_derived"] is True


def test_evidence_record_marks_unknown_for_legacy_index():
    from wenjin.retrieval.service import evidence_record

    record = evidence_record(
        {"chunk_id": "c1", "version": "1.0", "source_path": "a/b.cpp",
         "start_line": 1, "end_line": 2, "text": "正文"},
        {},
        rank=1,
    )
    assert record["parser"] is None
    assert record["ocr_derived"] is None


# ------------------------------------------------- PdfPlan：决定与事实一起进manifest


def test_pdf_plan_reports_why_ocr_was_or_was_not_used(pdfs):
    inspection = inspect_pdf(pdfs["mixed"])
    plan = PdfPlan(
        parser=HYBRID_PAGE_PARSER, report=inspection.to_report(),
        skipped_scan_pages=tuple(skipped_scan_pages(inspection, HYBRID_PAGE_PARSER)),
    )
    assert plan.status == "mineru_ocr_hybrid"
    assert plan.report["scan_pages"] == [2]
    assert plan.report["text_pages"] == 2
    assert plan.report["thresholds"]["scan_image_coverage"] == 0.5


def test_pdf_plan_status_for_each_path(pdfs):
    normal = inspect_pdf(pdfs["normal_with_blank"])
    assert PdfPlan(TEXT_PAGE_PARSER, normal.to_report()).status == "pending"
    assert PdfPlan(OCR_PAGE_PARSER, inspect_pdf(pdfs["scanned"]).to_report()).status == (
        "mineru_ocr"
    )
    declined = PdfPlan(TEXT_PAGE_PARSER, inspect_pdf(pdfs["mixed"]).to_report(),
                       skipped_scan_pages=(2,))
    assert declined.status == "text_layer_only"


def test_declined_mixed_pdf_records_skipped_pages_in_the_plan(tmp_path):
    """不静默：哪些页不会被索引，要在入库时就写成可复核的记录。"""
    source = tmp_path / "docs"
    source.mkdir()
    (source / "scan.pdf").write_bytes(b"%PDF-1.4 fake")
    plan = plan_corpus(
        source=source, library="lib", version="1.0", corpus_id="lib-1.0",
        pdf_plans={
            source / "scan.pdf": PdfPlan(
                TEXT_PAGE_PARSER, {"pages": 3, "scan_pages": [2]}, skipped_scan_pages=(2,)
            )
        },
    )
    item = plan.files[0]
    assert item.parser == TEXT_PAGE_PARSER
    assert item.parser_status == "text_layer_only"
    assert any("第 [2] 页" in note for note in item.notes)
    assert item.parse_report["scan_pages"] == [2]


def test_parse_report_reaches_the_manifest(tmp_path):
    from wenjin.ingestion.intake import build_manifest

    source = tmp_path / "docs"
    source.mkdir()
    (source / "scan.pdf").write_bytes(b"%PDF-1.4 fake")
    plan = plan_corpus(
        source=source, library="lib", version="1.0", corpus_id="lib-1.0",
        pdf_plans={source / "scan.pdf": PdfPlan(
            OCR_PAGE_PARSER, {"pages": 2, "scan_pages": [1, 2]}
        )},
    )
    manifest = build_manifest(plan)
    provenance = manifest["files"][0]["provenance"]
    assert provenance["parse_report"]["scan_pages"] == [1, 2]
    assert manifest["files"][0]["parser"] == OCR_PAGE_PARSER


# ------------------------------------------------------ 解析器一致性 / 逐页覆盖


def test_inspection_without_scan_pages_needs_no_ocr():
    assert PdfInspection(Path("x.pdf"), 1, 1, (), ()).needs_ocr is False


def test_unit_parser_outside_declared_set_is_rejected(tmp_path, monkeypatch):
    """文件级说是文本层解析，页级却报 OCR：这种不一致必须拦下来。"""
    from wenjin.ingestion import multiformat

    manifest_path, raw_root = _write_single_file_corpus(
        tmp_path, parser=TEXT_PAGE_PARSER, content="class Node;\n"
    )
    monkeypatch.setattr(
        multiformat, "parse_file",
        lambda *args, **kwargs: [
            ocr_module.ParsedUnit("p", OCR_PAGE_PARSER, "x", page_number=1)
        ],
    )
    with pytest.raises(ValueError, match="解析器与manifest不一致"):
        multiformat.load_retrieval_documents(manifest_path, raw_root)


def test_load_retrieval_documents_propagates_the_ocr_marker(tmp_path, monkeypatch):
    """manifest 声明 OCR 的文件，其 chunk 必须带着 OCR 标记进索引。"""
    from wenjin.ingestion import multiformat

    manifest_path, raw_root = _write_single_file_corpus(
        tmp_path, parser=OCR_PAGE_PARSER, content="scan body\n"
    )
    monkeypatch.setattr(
        multiformat, "parse_file",
        lambda *args, **kwargs: [
            ocr_module.ParsedUnit("p", OCR_PAGE_PARSER, "scan body\n", page_number=1)
        ],
    )
    documents = multiformat.load_retrieval_documents(manifest_path, raw_root)
    assert documents[0]["parser"] == OCR_PAGE_PARSER
    assert documents[0]["ocr_derived"] is True
    chunk = block_chunks(documents[0], max_chars=1600)[0]
    assert chunk.ocr_derived is True


def test_page_coverage_rejects_missing_page():
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=1),
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=3),
    ]
    with pytest.raises(ValueError, match="未完整覆盖 1..3"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, 3)


# --- 预期页数必须来自来源 PDF，而不是解析器自己的输出 ---
#
# `expected = set(range(1, len(units) + 1))` 是拿输出当标准答案：只能发现中间缺页，
# 空结果、尾页丢失、连续多出页全部漏过。


def test_page_coverage_rejects_empty_result_for_a_page_based_source():
    from wenjin.ingestion.multiformat import _check_page_coverage

    with pytest.raises(ValueError, match="未完整覆盖 1..2"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, [], 2)


def test_page_coverage_rejects_dropped_last_page():
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=1)]
    with pytest.raises(ValueError, match="缺 \\[2\\]"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, 2)


def test_page_coverage_rejects_extra_trailing_pages():
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=page)
             for page in (1, 2, 3)]
    with pytest.raises(ValueError, match="超出 \\[3\\]"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, 2)


def test_page_coverage_accepts_a_legitimately_blank_page():
    """空白页可以没有正文，但必须保留单元——否则后续页码会整体前移。"""
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "body", page_number=1),
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "", page_number=2),
    ]
    _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, 2)


def test_page_coverage_needs_a_real_page_count():
    """拿不到来源 PDF 页数时不能默认通过：那等于门禁没成立。"""
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=1)]
    with pytest.raises(ValueError, match="无法确定来源PDF的页数"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, None)


def test_loader_reads_the_page_count_from_the_source_pdf(tmp_path, monkeypatch):
    """加载入口必须把**来源 PDF 的实际页数**传进覆盖检查。

    这里让解析器只返回 1 页，来源 PDF 有 2 页——尾页丢失必须被加载层拦下。
    旧的 `expected = range(1, len(units)+1)` 在这里会算成 1..1，直接放行。
    """
    from wenjin.ingestion import multiformat

    manifest_path, raw_root = _write_pdf_corpus(tmp_path, pages=2)
    monkeypatch.setattr(
        multiformat, "parse_file",
        lambda *args, **kwargs: [
            ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "only page one", page_number=1)
        ],
    )
    with pytest.raises(ValueError, match="未完整覆盖 1..2"):
        multiformat.load_retrieval_documents(manifest_path, raw_root)


def test_loader_accepts_a_complete_page_based_parse(tmp_path):
    """对照：正常解析必须通过，否则上面的拦截可能只是"永远失败"。"""
    from wenjin.ingestion import multiformat

    manifest_path, raw_root = _write_pdf_corpus(tmp_path, pages=2)
    documents = multiformat.load_retrieval_documents(manifest_path, raw_root)
    assert [doc["page_number"] for doc in documents] == [1, 2]


def test_page_coverage_rejects_duplicate_page():
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=1),
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=1),
    ]
    with pytest.raises(ValueError, match="重复页码"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, 3)


def test_page_coverage_rejects_invalid_page_number():
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=0)]
    with pytest.raises(ValueError, match="非法页码"):
        _check_page_coverage("a.pdf", TEXT_PAGE_PARSER, units, 3)


def test_line_based_source_skips_page_coverage():
    """行映射来源没有页码，不该被按页覆盖检查误伤。"""
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [ocr_module.ParsedUnit("p", "text-line-v1", "x", start_line=1, end_line=9)]
    _check_page_coverage("a.txt", "text-line-v1", units, None)


def test_hybrid_declaration_allows_both_unit_parsers():
    from wenjin.ingestion.multiformat import _check_page_coverage

    units = [
        ocr_module.ParsedUnit("p", TEXT_PAGE_PARSER, "x", page_number=1),
        ocr_module.ParsedUnit("p", OCR_PAGE_PARSER, "x", page_number=2),
    ]
    _check_page_coverage("a.pdf", HYBRID_PAGE_PARSER, units, 2)


# --------------------------------------------------------------------------- 工具


def _write_single_file_corpus(tmp_path: Path, *, parser: str, content: str):
    """造一份最小可用的新语料：raw_data 里一个文件 + 一份绑定了双 Hash 的 manifest。"""
    from wenjin.content_hashing import hash_content, manifest_content_fingerprint

    raw_root = tmp_path / "raw_data"
    target = raw_root / "lib" / "1.0" / "doc.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    hashed = hash_content(target.read_bytes(), normalization="text-newline-lf-v1",
                          encoding="utf-8")
    manifest = {
        "schema_version": 3,
        "corpus_id": "lib-1.0",
        "library": "lib",
        "version": "1.0",
        "raw_data_subdir": "lib/1.0",
        "files": [{
            "path": "doc.txt",
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "normalization": "text-newline-lf-v1",
            "encoding": "utf-8",
            "retrieval_eligible": True,
            "parser": parser,
            "logical_document_id": "lib-1.0:doc.txt",
        }],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest_path = tmp_path / "corpora" / "lib-1.0" / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, raw_root


def _write_pdf_corpus(tmp_path: Path, *, pages: int):
    """造一份最小 PDF 语料：raw_data 里一个 N 页 PDF + 绑定了双 Hash 的 manifest。"""
    from wenjin.content_hashing import manifest_content_fingerprint

    raw_root = tmp_path / "raw_data"
    target = raw_root / "lib" / "1.0" / "doc.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    document = pymupdf.open()
    for index in range(pages):
        document.new_page().insert_text((72, 100), f"page {index + 1} body", fontsize=12)
    document.save(target)
    document.close()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    manifest = {
        "schema_version": 3, "corpus_id": "lib-1.0", "library": "lib", "version": "1.0",
        "raw_data_subdir": "lib/1.0", "license": "test",
        "files": [{
            "path": "doc.pdf", "raw_sha256": digest, "canonical_sha256": digest,
            "normalization": "binary-identity-v1", "encoding": None,
            "retrieval_eligible": True, "parser": TEXT_PAGE_PARSER,
            "logical_document_id": "lib-1.0:doc.pdf",
        }],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest_path = tmp_path / "corpora" / "lib-1.0" / "source-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, raw_root


def _fake_mineru(by_file: dict[str, list[dict]]):
    """构造 MinerU `/file_parse` 的响应；`content_list` 用真实字段名。"""

    def post(url, **kwargs):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": {
                    name: {"content_list": blocks} for name, blocks in by_file.items()
                }}

        return Response()

    return post


def test_saved_real_mineru_response_now_yields_text():
    """用仓库里保存的真实 MinerU 响应重放适配器。

    这份响应的唯一内容块就是一张表——旧适配器在这里抛
    "OCR 后没有可用正文"，整页内容丢失。
    """
    raw = Path("artifacts/runs/parse-lab-001/mineru-raw.json")
    if not raw.is_file():
        pytest.skip("没有保存的 MinerU 原始响应")
    payload = json.loads(raw.read_text(encoding="utf-8"))
    blocks = next(iter(payload["results"].values()))["content_list"]
    if isinstance(blocks, str):
        blocks = json.loads(blocks)
    tables = [block for block in blocks if block.get("type") == "table"]
    assert tables, "这份响应里应当有表格块"
    assert all("text" not in block for block in tables)
    text = ocr_module._block_text(tables[0])
    assert len(text) > 500
    assert "Handgun" in text


# --- 当前行为：拒绝 OCR 时混合件整份只归档 ---


def test_declining_ocr_excludes_the_whole_mixed_file(pdfs):
    """拒绝 OCR → 整份混合件只归档，**文字层页也不入库**。

    文档按**当前行为**写，不按设计意图写。`choose_pdf_parser(enabled=False)` 会返回
    文本层解析器、`skipped_scan_pages` 会列出扫描页，但那套分支只在文件**没有**被
    excluded 时才走到；而 `decide_ocr` 拒绝时一定把整份文件放进 `excluded`，
    因此 `PdfPlan.text_layer_only` 与 `skipped_scan_pages` 在集成路径上**不可达**。
    单元测试直接调用这些函数，所以没暴露这一点。
    """
    inspection = inspect_pdf(pdfs["mixed"])
    decision = decide_ocr([inspection], status=ONLINE, mode="never")
    assert decision.enabled is False
    assert set(decision.excluded) == {pdfs["mixed"]}

    plan = plan_corpus(
        source=pdfs["mixed"].parent, library="lib", version="1.0", corpus_id="lib-1.0",
        excluded=decision.excluded,
    )
    item = next(entry for entry in plan.files if entry.path.name == "mixed.pdf")
    assert item.eligible is False
    assert item.parser is None
    assert item.parser_status == "not_indexed"


def test_text_layer_only_status_is_unreachable_on_the_decline_path(pdfs):
    """把"不可达"本身固定成断言，避免以后有人以为它已经生效。"""
    from wenjin.ingestion.ocr import choose_pdf_parser, skipped_scan_pages

    inspection = inspect_pdf(pdfs["mixed"])
    parser = choose_pdf_parser(inspection, enabled=False)
    # 函数本身按设计工作……
    assert parser == TEXT_PAGE_PARSER
    assert skipped_scan_pages(inspection, parser) == [2]
    # ……但决定"是否排除整份文件"的是 decide_ocr，它把文件整个排除掉了。
    assert decide_ocr([inspection], status=ONLINE, mode="never").enabled is False
    assert PdfPlan(TEXT_PAGE_PARSER, inspection.to_report(),
                   skipped_scan_pages=(2,)).status == "text_layer_only"
