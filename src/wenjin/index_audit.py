"""索引逐条审计：把每个 chunk 的**两侧**元数据都绑定回 manifest 的来源记录。

启动校验、候选验收、切换与回退全部走同一份实现，避免"同一份坏资产在不同入口得到
不同结论"。审计覆盖：

1. 两侧 chunk ID 唯一且集合一致，正文逐字节一致；
2. 两侧的正文 `content_sha256` **各自**重新算一遍；
3. 两侧的 `source_path` **各自**能唯一映射回 manifest 的一条检索范围内记录；
4. 两侧的来源双 Hash、normalization、逻辑文档 ID 与 manifest 一致；
5. 两侧的行号与页码一致，且落在来源文件的实际范围内；
6. 版本与切片策略与目标配置一致；
7. 旧 schema 缺列只在 manifest 未声明该元数据时才容忍。

只读：不修改任何索引、配置或语料。
"""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from qdrant_client import QdrantClient

from wenjin import manifest as manifest_module
from wenjin.content_hashing import BINARY_IDENTITY_V1
from wenjin.index_registry import (
    IndexTarget,
    resolve_project_path,
    validate_target,
)
from wenjin.parser_names import (
    HYBRID_PAGE_PARSER,
    OCR_PAGE_PARSER,
    PAGE_PARSERS,
    TEXT_PAGE_PARSER,
    allowed_unit_parsers,
    is_ocr_derived,
    requires_ocr_marker,
)
from wenjin.paths import UnsafePathError, resolve_within
from wenjin.serving import CorpusSources, SourceNotFound

# 报告里保留的示例条数：够定位问题即可，不把整库差异塞进证据文件。
MAX_EXAMPLES = 20
# 位置范围校验要读来源文件行数；超过这个大小就不读（避免为审计拉入大文件）。
MAX_LINES_CHECK_BYTES = 8 * 1024 * 1024

# 期望在两侧都比对的字段，以及不一致时归到哪一类问题。
COMPARED_FIELDS = {
    "source_path": "source_path_unmapped",
    "version": "version_mismatch",
    "strategy": "strategy_mismatch",
    "content_sha256": "content_hash_mismatch",
    "source_raw_sha256": "source_hash_mismatch",
    "source_canonical_sha256": "source_hash_mismatch",
    "normalization": "normalization_mismatch",
    "logical_document_id": "logical_document_id_mismatch",
    "start_line": "position_mismatch",
    "end_line": "position_mismatch",
    "page_number": "position_mismatch",
    "parser": "parser_mismatch",
    "ocr_derived": "ocr_marker_mismatch",
}


def read_bm25_rows(path: Path) -> tuple[list[dict], set[str]]:
    """读取BM25行，并返回实际存在的列名。

    旧索引没有 page_number/logical_document_id 列，按NULL补齐以兼容；
    是否**允许**缺失由调用方按 manifest 声明判断，不能静默放过。
    """
    with closing(sqlite3.connect(path)) as database:
        database.row_factory = sqlite3.Row
        columns = {row[1] for row in database.execute("PRAGMA table_info(chunks)")}
        page_column = "page_number" if "page_number" in columns else "NULL AS page_number"
        logical_column = (
            "logical_document_id" if "logical_document_id" in columns
            else "NULL AS logical_document_id"
        )
        parser_column = "parser" if "parser" in columns else "NULL AS parser"
        ocr_column = "ocr_derived" if "ocr_derived" in columns else "NULL AS ocr_derived"
        rows = [dict(row) for row in database.execute(
            "SELECT chunk_id, source_path, version, start_line, end_line, strategy, "
            "content_sha256, source_raw_sha256, source_canonical_sha256, normalization, "
            f"{page_column}, {logical_column}, {parser_column}, {ocr_column}, raw_text "
            "FROM chunks"
        )]
    # SQLite 没有布尔类型，存的是 0/1；NULL 保留为 None，表示"这一列不存在"，
    # 不能折成 False——那会把"无法核验"说成"确认不是OCR"。
    for row in rows:
        row["ocr_derived"] = None if row["ocr_derived"] is None else bool(row["ocr_derived"])
    return rows, columns


def read_qdrant_payloads(client: QdrantClient, collection: str) -> list[dict]:
    payloads: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection, limit=512, offset=offset, with_payload=True, with_vectors=False
        )
        payloads.extend(point.payload or {} for point in points)
        if offset is None:
            return payloads


def _duplicates(values: list) -> list:
    seen, repeated = set(), set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return sorted(repeated, key=str)


def _position_problems(
    row: dict,
    record: dict,
    relative: str,
    corpus_root: Path,
    line_counts: dict[str, int | None],
    page_counts: dict[str, int | None],
) -> str | None:
    """位置合理性，按**来源类型**判定。

    - 按页解析的来源（PDF）：必须有物理页码，且 1 ≤ 页码 ≤ 实际页数。
      只看"列存在"和"页码为正"是不够的：两侧同时缺页码、或同时写成 999 都会漏过。
    - 文本来源：行号区间必须合法，且不超过来源文件的实际行数。
    """
    parser = record.get("parser")
    page = row.get("page_number")
    if parser in PAGE_PARSERS:
        if not isinstance(page, int) or page < 1:
            return f"按页来源必须带物理页码，实际为{page!r}"
        total_pages = _pdf_page_count(relative, corpus_root, page_counts)
        if total_pages is not None and page > total_pages:
            return f"页码超出来源PDF页数：{page} > {total_pages}"
        return None
    start, end = row.get("start_line"), row.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start:
        return f"行号区间非法：start={start} end={end}"
    if page is not None and (not isinstance(page, int) or page < 1):
        return f"页码非法：{page}"
    _count_source_lines(relative, corpus_root, line_counts)
    total = line_counts.get(relative)
    if total is not None and end > total:
        return f"行号超出来源文件行数：end={end} 文件共{total}行"
    return None


def _pdf_page_count(
    relative: str, corpus_root: Path, cache: dict[str, int | None]
) -> int | None:
    """来源PDF的物理页数；每份文件只读一次。读不到返回None（不做范围判定）。"""
    if relative in cache:
        return cache[relative]
    total: int | None = None
    try:
        path = resolve_within(corpus_root, relative)
    except UnsafePathError:
        path = None
    if path is not None and path.is_file() and path.suffix.lower() == ".pdf":
        try:
            import pymupdf

            with pymupdf.open(path) as pdf:
                total = len(pdf)
        except Exception:  # noqa: BLE001 - 打不开就不做页数范围判定，不掩盖其他检查
            total = None
    cache[relative] = total
    return total


def _count_source_lines(relative: str, raw_root: Path, cache: dict[str, int | None]) -> int | None:
    """来源文件的行数；读不到或太大时返回None（不做范围判定）。"""
    if relative in cache:
        return cache[relative]
    total: int | None = None
    try:
        path = resolve_within(raw_root, relative)
    except UnsafePathError:
        path = None
    if path is not None and path.is_file() and path.stat().st_size <= MAX_LINES_CHECK_BYTES:
        try:
            total = len(path.read_text(encoding="utf-8", errors="strict").splitlines())
        except (UnicodeDecodeError, OSError):
            total = None  # 二进制或读不了：不做行数范围判定
    cache[relative] = total
    return total


def _scan_pages(manifest_record: dict) -> list[int] | None:
    """manifest 记录的逐页解析计划里，哪些页是扫描页。

    None 表示**没有记录**（与"记录为空列表"不同）。这是混合件的判定依据：
    文件级 parser 只说"用了哪套流程"，具体哪一页走 OCR 必须看这份计划。
    """
    provenance = manifest_record.get("provenance")
    if not isinstance(provenance, dict):
        return None
    report = provenance.get("parse_report")
    if not isinstance(report, dict) or "scan_pages" not in report:
        return None
    pages = report["scan_pages"]
    return [page for page in pages if isinstance(page, int)] if isinstance(pages, list) else None


def expected_unit_parser(manifest_record: dict, page_number) -> str | None:
    """按 manifest 的逐页计划，这一页**应该**由哪个解析器产出。

    返回 None 表示无法判定（非按页来源，或计划没记录）。
    只看"parser 属于允许的两个名称"是不够的：那样把扫描页标成文本层解析器
    也会通过，识别文本就洗掉了 OCR 身份。
    """
    declared = manifest_record.get("parser")
    if declared == TEXT_PAGE_PARSER:
        return TEXT_PAGE_PARSER
    if declared == OCR_PAGE_PARSER:
        return OCR_PAGE_PARSER
    if declared == HYBRID_PAGE_PARSER:
        scan_pages = _scan_pages(manifest_record)
        if scan_pages is None or not isinstance(page_number, int):
            return None
        return OCR_PAGE_PARSER if page_number in scan_pages else TEXT_PAGE_PARSER
    return None


def _check_parser_marker(
    side: str, side_row: dict, manifest_record: dict, chunk_id, note
) -> bool:
    """核验 chunk 上的页级来源标记；返回这一侧是否**完整**核验过。

    三件事都要成立，缺一不可：

    1. 页级 parser 与 manifest 的逐页计划一致（不是只查"在允许集合里"）；
    2. `ocr_derived` 与 parser 推导结果一致，不能由写入方自己声明；
    3. manifest 说这份文件含 OCR 正文时，索引侧必须有标记可查——
       缺失本身就是问题，不能让"查不到"变成"通过"。
    """
    actual = side_row.get("parser")
    marker = side_row.get("ocr_derived")
    declared = manifest_record.get("parser")
    must_mark = requires_ocr_marker(declared)

    if actual is None and marker is None:
        if must_mark:
            note("ocr_marker_mismatch", chunk_id,
                 f"{side}缺少parser/ocr_derived标记，但manifest声明该文件含OCR正文"
                 f"（{declared!r}）：识别文本会以原文的样子进入证据")
        # 旧索引两侧都没有这一列时不算失败：它没有"识别文本冒充原文"的风险。
        # 覆盖范围由报告的 parser_marker 字段按**实际核验到的 chunk 数**说明。
        return False

    verified = True
    if actual is None:
        note("parser_mismatch", chunk_id,
             f"{side}有ocr_derived标记但缺少parser，无法判断来源")
        return False

    expected = expected_unit_parser(manifest_record, side_row.get("page_number"))
    if expected is not None:
        if actual != expected:
            note("parser_mismatch", chunk_id,
                 f"{side}的parser={actual!r}与manifest记录的逐页计划不符："
                 f"第{side_row.get('page_number')}页应为{expected!r}")
    else:
        allowed = allowed_unit_parsers(declared)
        if allowed is not None and actual not in allowed:
            note("parser_mismatch", chunk_id,
                 f"{side}的parser={actual!r}不在manifest声明{declared!r}允许的范围内")
        if must_mark:
            # 混合件却没有逐页计划：判定依据缺失，不能当作"核验通过"。
            note("parser_mismatch", chunk_id,
                 f"{side}无法核对页级来源：manifest声明{declared!r}，"
                 "但缺少 provenance.parse_report.scan_pages 逐页计划")

    if marker is None:
        if must_mark:
            note("ocr_marker_mismatch", chunk_id,
                 f"{side}有parser={actual!r}但缺少ocr_derived标记："
                 "无法确认这段正文是不是识别来的")
            verified = False
    elif bool(marker) != is_ocr_derived(actual):
        note("ocr_marker_mismatch", chunk_id,
             f"{side}的ocr_derived={marker!r}与parser={actual!r}不符")
        verified = False
    return verified


def audit_index(target: IndexTarget, *, root: Path, qdrant_client: QdrantClient) -> dict:
    """对单个候选或 active 目标做完整审计；返回可直接写进证据文件的报告。"""
    # 1) 共享的三资产校验（manifest字节绑定、按记录重算的指纹、行数/点数/维度/状态）。
    validation = validate_target(target, root=root, qdrant_client=qdrant_client)

    raw_data_root = root / "raw_data"
    manifest_path = resolve_project_path(root, target.manifest_path)
    # 结构校验必须包含 subdir：导入拒绝的配置，验收也不能接受。
    manifest = manifest_module.validate_manifest(manifest_path, raw_data_root=raw_data_root)
    # 来源文件在 raw_data/<subdir>/ 之下；行数校验必须用**语料根**，
    # 直接拿 raw_data 作根会解析到不存在的路径，静默跳过范围判定。
    corpus_root = manifest_module.trusted_raw_root(manifest, raw_data_root)
    records = {record["path"]: record for record in manifest_module.file_records(manifest)}
    in_scope = set(manifest_module.scope_files(manifest))
    sources = CorpusSources.from_manifest(
        manifest_path, raw_root=raw_data_root, expected_corpus_id=target.corpus_identity()
    )

    rows, bm25_columns = read_bm25_rows(resolve_project_path(root, target.bm25_path))
    payloads = read_qdrant_payloads(qdrant_client, target.dense_collection)

    problems: dict[str, list] = {
        "duplicate_chunk_ids": [],
        "duplicate_manifest_paths": [],
        "empty_chunk_ids": [],
        "id_sets_differ": [],
        "legacy_schema_mismatch": [],
        "text_mismatch": [],
        "content_hash_mismatch": [],
        "source_path_unmapped": [],
        "outside_retrieval_scope": [],
        "source_hash_mismatch": [],
        "normalization_mismatch": [],
        "logical_document_id_mismatch": [],
        "source_path_mismatch": [],
        "position_mismatch": [],
        "parser_mismatch": [],
        "ocr_marker_mismatch": [],
        "version_mismatch": [],
        "strategy_mismatch": [],
    }

    def note(kind: str, chunk_id, detail) -> None:
        if len(problems[kind]) < MAX_EXAMPLES:
            problems[kind].append({"chunk_id": chunk_id, "detail": detail})

    # 2) 唯一性：先查重复，再建字典；否则重复键会被静默覆盖，集合比较也看不出。
    for chunk_id in _duplicates([row["chunk_id"] for row in rows]):
        note("duplicate_chunk_ids", chunk_id, "BM25中重复出现")
    for chunk_id in _duplicates([payload.get("chunk_id") for payload in payloads]):
        note("duplicate_chunk_ids", chunk_id, "Qdrant中重复出现")
    for chunk_id in [row["chunk_id"] for row in rows] + [
        payload.get("chunk_id") for payload in payloads
    ]:
        if not chunk_id:
            note("empty_chunk_ids", None, "存在空的chunk_id")
            break

    # 3) 旧schema缺列：只有manifest没声明该元数据时才容忍。
    if any(record.get("logical_document_id") for record in records.values()):
        if "logical_document_id" not in bm25_columns:
            note("legacy_schema_mismatch", None,
                 "manifest声明了逻辑文档ID，但BM25缺少logical_document_id列")
    needs_pages = any(
        record.get("normalization") == BINARY_IDENTITY_V1
        and (record.get("retrieval_eligible") or record.get("indexable"))
        for record in records.values()
    )
    if needs_pages and "page_number" not in bm25_columns:
        note("legacy_schema_mismatch", None,
             "manifest含二进制(按页)来源，但BM25缺少page_number列")
    # OCR标记是唯一"必须进索引"的解析元数据：只有它能防止把识别文本当成原文引用。
    # 这里判的是**文件级**要求（`requires_ocr_marker`），不是"这个 chunk 是不是 OCR"——
    # 混合件整体都是 OCR 相关文件，用页级谓词会整份漏掉。
    # 纯文本层来源的parser只是溯源信息，manifest已经通过内容Hash绑定到chunk，
    # 因此那时缺列只记录为覆盖缺口，不算失败（下面的parser_marker字段）。
    ocr_sources = sorted(
        path for path, record in records.items()
        if path in in_scope and requires_ocr_marker(record.get("parser"))
    )
    if ocr_sources and "parser" not in bm25_columns:
        note("legacy_schema_mismatch", None,
             f"manifest含OCR来源（{ocr_sources[:3]}），但BM25缺少parser列，无法核验OCR标记")

    by_id_bm25 = {row["chunk_id"]: row for row in rows}
    by_id_qdrant = {payload.get("chunk_id"): payload for payload in payloads}
    bm25_ids, qdrant_ids = set(by_id_bm25), set(by_id_qdrant)
    for chunk_id in sorted(bm25_ids ^ qdrant_ids):
        note("id_sets_differ", chunk_id,
             "只在BM25" if chunk_id in bm25_ids else "只在Qdrant")

    line_counts: dict[str, int | None] = {}
    page_counts: dict[str, int | None] = {}
    ocr_source_chunks = 0
    ocr_marker_verified_chunks = 0
    for chunk_id in sorted(bm25_ids & qdrant_ids):
        row, payload = by_id_bm25[chunk_id], by_id_qdrant[chunk_id]

        # 4) 两侧逐字段比较。除正文外的字段直接比；正文单独比并各自重算Hash。
        for field, kind in COMPARED_FIELDS.items():
            if field in {"content_sha256", "source_path"}:
                continue  # 这两个单独处理（正文与路径）
            left, right = row.get(field), payload.get(field)
            if left != right:
                note(kind, chunk_id, f"{field}：BM25={left!r} Qdrant={right!r}")

        text_bm25, text_qdrant = row.get("raw_text") or "", payload.get("text") or ""
        if text_bm25 != text_qdrant:
            note("text_mismatch", chunk_id, "BM25与Qdrant正文不一致")
            continue

        # 5) 两侧的正文Hash各自重算。
        recomputed = hashlib.sha256(text_qdrant.encode("utf-8")).hexdigest()
        for side, value in (("Qdrant", payload.get("content_sha256")),
                            ("BM25", row.get("content_sha256"))):
            if value != recomputed:
                note("content_hash_mismatch", chunk_id,
                     f"{side}的content_sha256与正文不符：{value!r}")

        # 6) 两侧的来源路径**各自解析并绑定 manifest**，再确认两侧映射到同一条记录。
        #    只比较hash是不够的：两个内容相同的文件会得到相同hash，但来源不同，
        #    检索结果就会引用到另一个文件和另一条URL。
        side_rows = {"Qdrant": payload, "BM25": row}
        resolved_sources: dict[str, str] = {}
        marker_sides = 0
        chunk_is_ocr_source = False
        for side, source_path in (("Qdrant", payload.get("source_path")),
                                  ("BM25", row.get("source_path"))):
            try:
                record = sources.match_chunk_path(str(source_path or ""))
            except SourceNotFound as error:
                note("source_path_unmapped", chunk_id, f"{side}：{error}")
                continue
            resolved_sources[side] = record.relative
            if record.relative not in in_scope:
                # "只归档、不检索"的文件不允许出现在索引里。
                note("outside_retrieval_scope", chunk_id,
                     f"{side}：{record.relative} 不在检索白名单内")
                continue
            manifest_record = records.get(record.relative)
            if manifest_record is None:
                note("source_path_unmapped", chunk_id, f"{side}：未登记路径 {record.relative}")
                continue
            expected = {
                "source_raw_sha256": manifest_record.get("_content_hash"),
                "source_canonical_sha256": manifest_record.get("canonical_sha256"),
                "normalization": manifest_record.get("normalization"),
                "logical_document_id": manifest_record.get("logical_document_id"),
            }
            # **用对应侧的数据**比对；此前无论哪一侧都读 row，等于只校验了 BM25。
            side_row = side_rows[side]
            for field, want in expected.items():
                if want is None:
                    continue
                actual = side_row.get(field)
                if actual != want:
                    note(COMPARED_FIELDS.get(field, "source_hash_mismatch"), chunk_id,
                         f"{side}的{field}与manifest不符：{actual!r} != {want!r}")
            if _check_parser_marker(side, side_row, manifest_record, chunk_id, note):
                marker_sides += 1
            if requires_ocr_marker(manifest_record.get("parser")):
                chunk_is_ocr_source = True

        # 只有**两侧都完整核验过**才算这个 chunk 被核验过：parser 与 manifest 的
        # 逐页计划一致，且 OCR 标记齐全并自洽。按"列在不在"统计会把
        # "列在但全是 NULL"和"parser 在但 ocr_derived 缺失"都算成已核验。
        if chunk_is_ocr_source:
            ocr_source_chunks += 1
            if marker_sides == 2:
                ocr_marker_verified_chunks += 1

        if len(set(resolved_sources.values())) > 1:
            note("source_path_mismatch", chunk_id,
                 f"两侧指向不同来源记录：{resolved_sources}")

        # 7) 位置与页码：按来源类型判定，两侧各查一次。
        for side, relative in resolved_sources.items():
            manifest_record = records.get(relative)
            if manifest_record is None:
                continue
            detail = _position_problems(
                side_rows[side], manifest_record, relative, corpus_root,
                line_counts, page_counts,
            )
            if detail:
                note("position_mismatch", chunk_id, f"{side}：{detail}")

        # 版本与策略以目标配置为准，两侧都必须一致（上面已比过两侧相等）。
        if row.get("version") != target.version:
            note("version_mismatch", chunk_id,
                 f"BM25={row.get('version')!r} 目标={target.version!r}")
        if row.get("strategy") != target.strategy:
            note("strategy_mismatch", chunk_id,
                 f"BM25={row.get('strategy')!r} 目标={target.strategy!r}")

    for path in _duplicates([row["path"] for row in manifest_module.file_records(manifest)]):
        note("duplicate_manifest_paths", None, f"manifest重复登记：{path}")

    failed = {kind: items for kind, items in problems.items() if items}
    return {
        "target_id": target.target_id,
        "library": target.library,
        "version": target.version,
        "strategy": target.strategy,
        "dense_collection": target.dense_collection,
        "bm25_path": target.bm25_path,
        "chunk_count": target.chunk_count,
        "vector_dimension": validation["vector_dimension"],
        "collection_status": validation["collection_status"],
        "bm25_columns": sorted(bm25_columns),
        "manifest_records": len(records),
        "retrieval_scope_files": len(in_scope),
        # 解析器标记的可核验范围。单列出来是为了不让"这些chunk核验过了"和
        # "这份索引根本没有这一列、无从核验"混成同一句话。
        # `verifiable` 按**实际核验到的 chunk 数**判定，不按"列在不在"：
        # 列存在但值全是 NULL 时，什么都没核验到。
        "parser_marker": {
            "column_present": "parser" in bm25_columns,
            "ocr_sources_in_manifest": ocr_sources,
            "ocr_source_chunks": ocr_source_chunks,
            "ocr_marker_verified_chunks": ocr_marker_verified_chunks,
            "verifiable": not ocr_sources or ocr_marker_verified_chunks == ocr_source_chunks,
        },
        "problems": problems,
        "status": "PASS" if not failed else "FAIL",
    }


# 预检失败的原因码。只给码不给路径：这些字段可能回给无权查看该语料的调用方。
PREFLIGHT_MANIFEST_MISSING = "manifest_missing"
PREFLIGHT_MANIFEST_CHANGED = "manifest_changed"
PREFLIGHT_BM25_MISSING = "bm25_missing"
PREFLIGHT_BM25_UNREADABLE = "bm25_unreadable"
PREFLIGHT_BM25_ROW_MISMATCH = "bm25_row_mismatch"
PREFLIGHT_COLLECTION_MISSING = "collection_missing"
PREFLIGHT_POINT_MISMATCH = "point_count_mismatch"


def preflight_target(target: IndexTarget, *, root: Path, qdrant_client) -> dict:
    """**廉价**可用性预检：这份索引现在还能不能服务查询。

    刻意不做逐 chunk 审计：那是 `admit_index`，几千个 chunk 每次列目录都跑一遍不现实。
    这里只查四件**直接导致无法服务**的事——manifest 字节变了、BM25 不在或读不开、
    collection 不在、点数与配置不符。

    因此 `usable=True` 只说明这几项通过，**不等于**完整准入通过。
    完整准入以 `admit_index` 的结论为准（`scripts/verify_candidate_indexes.py`）。

    返回的是原因码而不是异常：列目录要能一次报出所有语料的状态。
    """
    def fail(reason: str) -> dict:
        return {"usable": False, "reason": reason}

    try:
        manifest_path = resolve_project_path(root, target.manifest_path)
        bm25_path = resolve_project_path(root, target.bm25_path)
    except (UnsafePathError, ValueError):
        return fail(PREFLIGHT_MANIFEST_MISSING)

    if not manifest_path.is_file():
        return fail(PREFLIGHT_MANIFEST_MISSING)
    if manifest_module.manifest_sha256(manifest_path) != target.manifest_sha256:
        return fail(PREFLIGHT_MANIFEST_CHANGED)
    if not bm25_path.is_file():
        return fail(PREFLIGHT_BM25_MISSING)
    try:
        with closing(sqlite3.connect(bm25_path)) as database:
            rows = database.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    except sqlite3.Error:
        return fail(PREFLIGHT_BM25_UNREADABLE)
    if rows != target.chunk_count:
        return fail(PREFLIGHT_BM25_ROW_MISMATCH)

    try:
        if not qdrant_client.collection_exists(target.dense_collection):
            return fail(PREFLIGHT_COLLECTION_MISSING)
        points = qdrant_client.count(
            target.dense_collection, count_filter=None, exact=True
        ).count
    except Exception:  # noqa: BLE001 - Qdrant不可达也算"现在不能服务"
        return fail(PREFLIGHT_COLLECTION_MISSING)
    if points != target.chunk_count:
        return fail(PREFLIGHT_POINT_MISMATCH)
    return {"usable": True, "reason": None}


class IndexAdmissionError(RuntimeError):
    """索引未通过完整准入审计。"""


def admit_index(target: IndexTarget, *, root: Path, qdrant_client: QdrantClient) -> dict:
    """**统一准入入口**：完整审计不通过就抛异常。

    启动、路由、候选验收、切换与回退都必须调用这个，而不是各自挑一套检查——
    否则同一份坏资产会在不同入口得到不同结论。
    """
    report = audit_index(target, root=root, qdrant_client=qdrant_client)
    if report["status"] != "PASS":
        failed = sorted(kind for kind, items in report["problems"].items() if items)
        raise IndexAdmissionError(
            f"索引未通过准入审计（{target.target_id}）：{failed}"
        )
    return report
