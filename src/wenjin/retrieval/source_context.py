"""从已校验的来源字节提取可供重排使用的结构上下文。

本模块不读文件、不验证manifest，也不改写候选正文；调用方必须先用manifest helper
取得并校验来源字节。未知PDF profile默认不生成section metadata。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from threading import RLock
from typing import Any

OGC_3DTILES_PDF_PROFILE = "ogc-3dtiles-numbered-outline-v1"
REVIEWED_3DTILES_TARGET = {
    "target_id": "ogc-3dtiles-1.1-blocks-split-candidate-003",
    "library": "ogc-3dtiles",
    "version": "1.1",
    "strategy": "blocks-split",
    "manifest_path": "corpora/ogc-3dtiles-1.1/source-manifest.json",
    "manifest_sha256": "fc4cc883fab579a9497dff0db7bd782670275205c1f7c2ad3845c2dd2c034db2",
    "canonical_corpus_sha256": "518fc35241954310a52dd948677fb3228dbd309a54679027f61bf8794b65f4a2",
}


class SourceContextIntegrityError(RuntimeError):
    """已登记目标的原文/Hash身份不匹配；不带路径或语料身份的安全错误。"""


@dataclass(frozen=True)
class SourceStructure:
    """source-native结构映射。HTML的key是物理源行，PDF的key是物理页。"""

    kind: str
    by_position: dict[int, dict[str, Any]]
    diagnostics: dict[str, Any]


def _visible_html(fragment: str) -> str:
    """按生产行映射解析器取可见标题，避免metadata和候选正文采用不同HTML规则。"""
    from wenjin.ingestion.multiformat import _html_line

    return _html_line(fragment).strip()


def extract_html_structure(content: bytes) -> SourceStructure:
    """提取HTML h1–h6层级与本行`TableTitle`；标题始终来自原始HTML本身。"""
    headings: dict[int, str] = {}
    by_position: dict[int, dict[str, Any]] = {}
    heading_count = 0
    table_count = 0
    for line_number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        line_headings = list(re.finditer(
            r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", line, re.IGNORECASE
        ))
        for match in line_headings:
            level = int(match.group(1))
            title = _visible_html(match.group(2))
            if title:
                headings[level] = title
                headings = {depth: value for depth, value in headings.items() if depth <= level}
                heading_count += 1
        found_titles = re.findall(
            r"<p\b[^>]*class=[\"'][^\"']*\bTableTitle\b[^\"']*[\"'][^>]*>(.*?)</p\s*>",
            line,
            re.IGNORECASE,
        )
        visible_titles = list(dict.fromkeys(_visible_html(value) for value in found_titles if _visible_html(value)))
        title = visible_titles[0] if len(visible_titles) == 1 else None
        table_count += len(found_titles)
        if headings or title or found_titles:
            by_position[line_number] = {
                "section_path": [headings[level] for level in sorted(headings)],
                # line locator无法拆开同一物理行里多个标题前后的正文范围。
                "section_ambiguous": len(line_headings) > 1,
                "table_title": title,
                "table_title_ambiguous": len(visible_titles) > 1,
                "structure_source": "html-heading-and-TableTitle",
            }
    return SourceStructure(
        kind="html",
        by_position=by_position,
        diagnostics={"heading_elements": heading_count, "table_titles": table_count},
    )


def _pdf_section_nodes(toc: list[tuple[int, str, int]]) -> list[tuple[int, int, str, int]]:
    """3D Tiles来源profile：保留章节root与二级编号章节，跳过表/图和property书签。"""
    nodes = []
    for index, (level, title, page) in enumerate(toc):
        normalized = " ".join(title.split())
        is_root = (
            level == 1
            and bool(re.search(r"[A-Za-z0-9]", normalized))
            and not normalized.lower().startswith(("list of ", "table ", "figure "))
        )
        is_section = (
            level == 2
            and bool(re.match(r"^(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)+)\.?\s", normalized))
            and not normalized.lower().startswith(("table ", "figure "))
        )
        if is_root or is_section:
            nodes.append((index, level, normalized, page))
    return nodes


def extract_pdf_structure(
    content: bytes, *, profile: str | None = None
) -> SourceStructure:
    """按明确profile提取PDF页上下文；未登记profile时返回空映射。

    当前profile只针对本项目审阅过的3D Tiles PDF。目录destination仅给物理页级上下文，
    不声称整页或每个段落都属于一个section；跨section页面丢弃唯一标题/section标签。
    """
    if profile != OGC_3DTILES_PDF_PROFILE:
        return SourceStructure("pdf", {}, {"supported": False, "reason": "unregistered_pdf_structure_profile"})

    import pymupdf

    by_page: dict[int, dict[str, Any]] = {}
    with pymupdf.open(stream=content, filetype="pdf") as pdf:
        toc = pdf.get_toc()
        nodes = _pdf_section_nodes(toc)
        ancestors: dict[int, str] = {}
        for node_index, (_, level, title, start_page) in enumerate(nodes):
            ancestors = {depth: value for depth, value in ancestors.items() if depth < level}
            ancestors[level] = title
            next_page = len(pdf) + 1
            for _, later_level, _, later_page in nodes[node_index + 1 :]:
                if later_level <= level:
                    next_page = later_page
                    break
            section_path = [ancestors[depth] for depth in sorted(ancestors)]
            for page_number in range(max(1, start_page), min(len(pdf), next_page - 1) + 1):
                by_page[page_number] = {
                    "page_context": section_path,
                    "section_scope_ambiguous": False,
                    "table_title": None,
                    "structure_source": "pdf-outline-page-context",
                }
        table_title_count = 0
        for page_number, page in enumerate(pdf, start=1):
            page_text = page.get_text(sort=True)
            titles = re.findall(
                r"\bTable\s+[A-Z]?\.?\d+(?:\.\d+)?\s*[—–·-][^\n]{2,160}",
                page_text,
            )
            normalized_titles = list(dict.fromkeys(" ".join(title.split()) for title in titles))
            current = by_page.get(page_number, {})
            current_path = current.get("page_context", [])
            current_section = current_path[-1:] if current_path else []
            current_family_match = re.match(r"^([A-Z]\.\d+|\d+\.\d+)", current_section[0]) if current_section else None
            current_family = current_family_match.group(1) if current_family_match else None
            other_section_on_page = False
            normalized_page_text = page_text.replace("\u200b", "")
            for level, title, start_page in toc:
                normalized_outline = " ".join(title.replace("\u200b", "").split())
                if start_page != page_number or level not in (2, 3):
                    continue
                if normalized_outline.lower().startswith(("table ", "figure ")):
                    continue
                family_match = re.match(r"^([A-Z]\.\d+|\d+\.\d+)", normalized_outline)
                if not family_match or family_match.group(1) == current_family:
                    continue
                compact_outline = re.sub(r"\s+", "", normalized_outline)
                compact_page_text = re.sub(r"\s+", "", normalized_page_text)
                if compact_outline in compact_page_text:
                    other_section_on_page = True
                    break
            if other_section_on_page:
                current["page_context"] = []
                current["section_scope_ambiguous"] = True
            if normalized_titles:
                current["table_title"] = normalized_titles[0] if len(normalized_titles) == 1 else None
                current["table_title_ambiguous"] = len(normalized_titles) > 1
                current["table_title_page"] = page_number
            current["structure_source"] = "pdf-outline-and-current-page-title"
            if current:
                by_page[page_number] = current
            table_title_count += len(normalized_titles)
    return SourceStructure(
        "pdf",
        by_page,
        {"supported": True, "profile": profile, "outline_entries": len(toc),
         "section_nodes_used": len(nodes), "page_table_titles": table_title_count,
         "physical_pages_mapped": len(by_page)},
    )


def _same_visible_title(text: str, title: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip()

    return normalize(title) in normalize(text)


def metadata_for_candidate(candidate: dict, structures: dict[str, SourceStructure]) -> dict[str, Any]:
    """按chunk locator附加上下文；scope跨界、page混合和caption缺失时拒绝强标签。"""
    source_path = candidate["source_path"]
    structure = structures.get(source_path)
    empty = {"section_path": [], "page_context": [], "table_title": None,
             "metadata_status": "source_structure_unavailable"}
    if structure is None:
        return empty
    if structure.kind == "pdf":
        page = candidate.get("page_number")
        raw = structure.by_position.get(page, {}) if isinstance(page, int) else {}
        mixed = bool(raw.get("section_scope_ambiguous"))
        title = raw.get("table_title")
        # Page-level caption只可作为reranker context，且必须真实存在于本candidate正文。
        if mixed or raw.get("table_title_ambiguous") or not title or not _same_visible_title(candidate.get("text", ""), title):
            title = None
        status = (
            "ambiguous_page_sections" if mixed
            else "ambiguous_page_table_titles" if raw.get("table_title_ambiguous")
            else "physical_page_context"
        )
        return {
            "section_path": [],
            "page_context": list(raw.get("page_context", [])) if not mixed else [],
            "table_title": title,
            "metadata_status": status,
            "section_scope_ambiguous": mixed,
            "structure_source": raw.get("structure_source"),
            "table_title_page": raw.get("table_title_page") if title else None,
        }

    start, end = candidate.get("start_line"), candidate.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int) or end < start:
        return {**empty, "metadata_status": "invalid_line_locator"}
    rows = [structure.by_position.get(line, {}) for line in range(start, end + 1)]
    # 空映射也要算一个独立scope，避免跨越未标记区段时沿用前一个heading。
    section_paths = {tuple(row.get("section_path", [])) for row in rows}
    nonempty_paths = [path for path in section_paths if path]
    section_ambiguous = (len(section_paths) > 1
                         or any(row.get("section_ambiguous") for row in rows))
    titles = {row.get("table_title") for row in rows if row.get("table_title")}
    title_ambiguous = len(titles) > 1 or any(row.get("table_title_ambiguous") for row in rows)
    title = next(iter(titles)) if len(titles) == 1 else None
    if section_ambiguous or title_ambiguous or not title or not _same_visible_title(candidate.get("text", ""), title):
        title = None
    return {
        "section_path": list(nonempty_paths[0]) if len(nonempty_paths) == 1 and not section_ambiguous else [],
        "page_context": [],
        "table_title": title,
        "metadata_status": "ambiguous_source_structure" if section_ambiguous or title_ambiguous else "mapped",
        "structure_source": "html-heading-and-TableTitle",
        "ambiguous_sections": section_ambiguous,
        "ambiguous_table_titles": title_ambiguous,
    }


def json_schema_document_classification(candidate: dict) -> str:
    """区分完整自描述Schema、其他完整JSON对象和非完整JSON对象。"""
    try:
        parsed = json.loads(candidate.get("text", ""))
    except (json.JSONDecodeError, TypeError):
        return "not_complete_json_object"
    if not isinstance(parsed, dict):
        return "complete_json_other"
    if isinstance(parsed.get("$schema"), str) and isinstance(parsed.get("$id"), str):
        return "complete_self_describing_json_schema"
    return "complete_json_other"


def is_complete_self_describing_json_schema(candidate: dict) -> bool:
    """仅当正文是完整JSON对象且root含字符串`$schema`、`$id`时返回True。"""
    return json_schema_document_classification(candidate) == "complete_self_describing_json_schema"


def reviewed_profile_for_target(target: Any) -> str | None:
    """仅为完全匹配本轮review target与manifest指纹的索引登记结构profile。"""
    if all(getattr(target, key, None) == expected
           for key, expected in REVIEWED_3DTILES_TARGET.items()):
        return OGC_3DTILES_PDF_PROFILE
    return None


def sealed_profile_for_target(target: Any, profile: str | None) -> str | None:
    """只继承sealed记录中的profile，并再次核对review过的内容身份。"""
    if profile != OGC_3DTILES_PDF_PROFILE:
        return None
    identity_fields = (
        "library", "version", "strategy", "manifest_sha256", "canonical_corpus_sha256",
    )
    if all(
        getattr(target, key, None) == REVIEWED_3DTILES_TARGET[key]
        for key in identity_fields
    ):
        return OGC_3DTILES_PDF_PROFILE
    return None


class VerifiedSourceContextCache:
    """按不可变双Hash身份缓存从source verified bytes解析的结构，不读mtime。"""

    def __init__(self, sources: Any, *, profile: str) -> None:
        if profile != OGC_3DTILES_PDF_PROFILE:
            raise ValueError("unregistered source context profile")
        self.sources = sources
        self.profile = profile
        self._structures: dict[tuple[str, str, str, str, str], SourceStructure] = {}
        self._lock = RLock()

    def document_for_candidate(self, candidate: dict) -> tuple[str, str]:
        """返回(模型输入,诊断码)；完整Schema使用无前缀输入。"""
        from wenjin.content_hashing import hash_content
        from wenjin.retrieval.rerank import candidate_document
        from wenjin.serving import SourceModified, SourceNotFound, SourceUnverified

        try:
            record = self.sources.match_chunk_path(candidate.get("source_path", ""))
        except (SourceNotFound, KeyError, TypeError) as error:
            raise SourceContextIntegrityError("登记来源身份校验失败，已停止重排") from error
        if (
            not record.raw_sha256
            or not record.canonical_sha256
            or not record.normalization
            or candidate.get("source_raw_sha256") != record.raw_sha256
            or candidate.get("source_canonical_sha256") != record.canonical_sha256
        ):
            raise SourceContextIntegrityError("登记来源双Hash身份不匹配，已停止重排")
        text = candidate.get("text")
        if not isinstance(text, str) or not candidate.get("content_sha256"):
            raise SourceContextIntegrityError("候选正文缺少不可变Hash身份，已停止重排")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != candidate["content_sha256"]:
            raise SourceContextIntegrityError("候选正文Hash不匹配，已停止重排")
        if is_complete_self_describing_json_schema(candidate):
            return candidate_document(candidate), "complete_self_describing_json_schema"

        suffix = record.relative.rsplit(".", 1)[-1].lower()
        kind = "pdf" if suffix == "pdf" else "html" if suffix in {"html", "htm"} else None
        if kind is None:
            return candidate_document(candidate), "unregistered_source_format"
        key = (record.raw_sha256, record.canonical_sha256, record.normalization,
               record.encoding or "", self.profile + ":" + kind)
        with self._lock:
            structure = self._structures.get(key)
            if structure is None:
                try:
                    verified = self.sources.read_verified(record.relative)
                    hashed = hash_content(
                        verified.content,
                        normalization=record.normalization,
                        encoding=record.encoding,
                    )
                except (SourceModified, SourceNotFound, SourceUnverified, ValueError, OSError) as error:
                    raise SourceContextIntegrityError(
                        "登记来源原文Hash校验失败，已停止重排"
                    ) from error
                if (hashed.raw_sha256 != record.raw_sha256
                        or hashed.canonical_sha256 != record.canonical_sha256):
                    raise SourceContextIntegrityError("登记来源双Hash校验失败，已停止重排")
                try:
                    structure = (
                        extract_pdf_structure(hashed.canonical_bytes, profile=self.profile)
                        if kind == "pdf" else extract_html_structure(hashed.canonical_bytes)
                    )
                except Exception:
                    # 已校验source但解析器不支持其结构时，明确退回baseline frame。
                    return candidate_document(candidate), "verified_source_structure_parse_failed"
                self._structures[key] = structure

        metadata = metadata_for_candidate(candidate, {candidate["source_path"]: structure})
        document = candidate_document_with_context(candidate, metadata)
        status = str(metadata.get("metadata_status", "unknown_metadata_status"))
        if metadata.get("section_path") or metadata.get("page_context") or metadata.get("table_title"):
            status = "context_applied:" + status
        return document, status


def candidate_document_with_context(candidate: dict, metadata: dict[str, Any]) -> str:
    """构造模型专用输入；完整自描述JSON保持原输入，证据正文和locator不变。"""
    from wenjin.retrieval.rerank import candidate_document

    # 完整Schema对象已自带身份；只识别可完整解析且root同时含字符串$schema/$id的JSON。
    # 不用query、gold或标题判断，避免把PDF片段/普通提及误当成自描述对象。
    if is_complete_self_describing_json_schema(candidate):
        return candidate_document(candidate)

    prefixes = []
    if metadata.get("section_path"):
        prefixes.append("HTML heading path: " + " > ".join(metadata["section_path"]))
    if metadata.get("page_context"):
        prefixes.append("Source page context (derived from PDF outline; physical-page level): "
                        + " > ".join(metadata["page_context"]))
    if metadata.get("table_title"):
        prefixes.append("Source title present in candidate: " + metadata["table_title"])
    base = candidate_document(candidate)
    return "\n".join(prefixes + [base])
