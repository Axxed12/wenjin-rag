"""语料入库的核心逻辑：格式分类、双Hash、manifest构建、落盘和检索完整性检查。

依赖方向是单向的：本模块只依赖 content_hashing、chunking 和 multiformat，
**不导入 MCP、检索路由或 Qdrant**。CLI、批处理脚本和将来的服务入口都复用这里的函数，
所以这里不出现 argparse 和 print。

扫描件检测依赖 ocr 模块（读取 MinerU），但"要不要启用OCR"由调用方决定，
本模块只接受已经决定好的 `ocr_paths`。
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from shutil import copyfile

from wenjin.content_hashing import (
    BINARY_IDENTITY_V1,
    TEXT_NEWLINE_LF_V1,
    hash_content,
    manifest_content_fingerprint,
)
from wenjin.ingestion.chunking import block_chunks
from wenjin.ingestion.multiformat import parser_for_path
from wenjin.parser_names import (
    HYBRID_PAGE_PARSER,
    OCR_PAGE_PARSER,
    TEXT_PAGE_PARSER,
)
from wenjin.paths import is_within, resolve_within

# 按二进制处理的已知后缀；PDF 仍可检索，其余只归档。
BINARY_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".gz", ".tar",
                   ".7z", ".docx", ".pptx", ".xlsx", ".sqlite", ".bin"}
# 本项目解析器不覆盖这些容器格式，一律只归档，不猜内容。
ARCHIVE_SUFFIXES = {".zip", ".gz", ".tar", ".7z", ".docx", ".pptx", ".xlsx"}
PARSER_BY_SUFFIX = {".html": "html-line-v1", ".htm": "html-line-v1",
                    ".xml": "xml-line-v1", ".xsd": "xml-line-v1",
                    ".rst": "rst-line-v1", ".1": "man-roff-line-v1"}
# 解析器名的唯一定义在 multiformat；这里只做别名，避免同一批字符串多处各写一份。
OCR_PARSER = OCR_PAGE_PARSER
PDF_PARSER = TEXT_PAGE_PARSER

TEXT_KIND = "text"
PDF_KIND = "pdf"
ARCHIVE_KIND = "archive"


@dataclass(frozen=True)
class PdfPlan:
    """一个PDF的解析安排。

    解析器名只在这里决定一次，检测事实随文件写进manifest：入库时为什么选了这条路，
    之后必须能只凭manifest复核，而不是靠重跑一遍检测。
    """

    parser: str
    report: dict
    # 选了文本层解析但文件里仍有扫描页时，这些页不会被索引——必须显式记下来。
    skipped_scan_pages: tuple[int, ...] = ()

    @property
    def status(self) -> str:
        if self.parser == OCR_PARSER:
            return "mineru_ocr"
        if self.parser == HYBRID_PAGE_PARSER:
            return "mineru_ocr_hybrid"
        return "text_layer_only" if self.skipped_scan_pages else "pending"


@dataclass(frozen=True)
class PlannedFile:
    """一个待入库文件及其身份信息；尚未落盘。"""

    path: Path
    relative: str
    kind: str
    normalization: str
    encoding: str | None
    raw_sha256: str
    canonical_sha256: str
    size: int
    eligible: bool
    parser: str | None
    parser_status: str
    notes: list[str] = field(default_factory=list)
    parse_report: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CorpusPlan:
    corpus_id: str
    library: str
    version: str
    source: Path
    files: tuple[PlannedFile, ...]

    @property
    def eligible(self) -> tuple[PlannedFile, ...]:
        return tuple(item for item in self.files if item.eligible)

    @property
    def archived(self) -> tuple[PlannedFile, ...]:
        return tuple(item for item in self.files if not item.eligible)


def classify(path: Path) -> tuple[str, str, str | None]:
    """返回 (kind, normalization, encoding)；解码失败的文件按归档处理。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return PDF_KIND, BINARY_IDENTITY_V1, None
    if suffix in ARCHIVE_SUFFIXES or suffix in BINARY_SUFFIXES:
        return ARCHIVE_KIND, BINARY_IDENTITY_V1, None
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ARCHIVE_KIND, BINARY_IDENTITY_V1, None
    return TEXT_KIND, TEXT_NEWLINE_LF_V1, "utf-8"


def parser_for(kind: str, path: Path, *, pdf_plan: PdfPlan | None) -> str:
    """解析器名会写进manifest，重建索引时据此复现同一份解析结果。"""
    if kind == PDF_KIND:
        return pdf_plan.parser if pdf_plan else PDF_PARSER
    return PARSER_BY_SUFFIX.get(path.suffix.lower(), parser_for_path(path))


def file_relative_path(path: Path, source: Path) -> str:
    return (path.relative_to(source) if source.is_dir() else Path(path.name)).as_posix()


def scan_pdfs(paths: list[Path]):
    """只对PDF做文本层检测；其余格式不需要。"""
    from wenjin.ingestion.ocr import inspect_pdf

    return [inspect_pdf(path) for path in paths if classify(path)[0] == PDF_KIND]


def plan_corpus(
    *,
    source: Path,
    library: str,
    version: str,
    corpus_id: str,
    pdf_plans: dict[Path, PdfPlan] | None = None,
    excluded: dict[Path, str] | None = None,
) -> CorpusPlan:
    """读取每个文件的字节、算双Hash并决定是否可检索；不写盘。

    PDF 的解析安排由调用方先算好（`PdfPlan`）：本模块不导入 ocr，
    检测和决策留在 `wenjin.ingestion.ocr`，这里只消费结果。
    """
    if excluded is None:
        excluded = {}
    if pdf_plans is None:
        pdf_plans = {}
    files = []
    for path in sorted(collect_files(source)):
        kind, normalization, encoding = classify(path)
        raw = path.read_bytes()
        hashed = hash_content(raw, normalization=normalization, encoding=encoding)
        plan = pdf_plans.get(path)
        notes = []
        if path in excluded:
            notes.append(excluded[path])
        if kind == ARCHIVE_KIND:
            notes.append("容器或二进制格式：仅归档，不参与检索")
        if plan is not None and plan.skipped_scan_pages:
            # 不静默：这些页不会被索引，必须在入库时和manifest里都看得见。
            notes.append(
                f"第 {list(plan.skipped_scan_pages)} 页没有可用文字层且未启用OCR："
                "这些页不会被索引"
            )
        eligible = kind != ARCHIVE_KIND and path not in excluded
        files.append(PlannedFile(
            path=path,
            relative=file_relative_path(path, source),
            kind=kind,
            normalization=hashed.normalization,
            encoding=hashed.encoding,
            raw_sha256=hashed.raw_sha256,
            canonical_sha256=hashed.canonical_sha256,
            size=len(raw),
            eligible=eligible,
            parser=parser_for(kind, path, pdf_plan=plan) if eligible else None,
            parser_status=plan.status if (eligible and plan) else (
                "pending" if eligible else "not_indexed"
            ),
            notes=notes,
            parse_report=dict(plan.report) if (plan and kind == PDF_KIND) else {},
        ))
    if not files:
        raise ValueError(f"来源里没有可入库的文件：{source}")
    return CorpusPlan(corpus_id, library, version, source, tuple(files))


def collect_files(source: Path) -> list[Path]:
    """列出待入库文件；目录递归，隐藏文件和符号链接一律跳过。

    跳过符号链接是完整性要求而不是洁癖：一个指向语料目录之外的软链接会让复制进来的内容和
    manifest 声称的来源对不上，"这份内容属于该语料"的溯源契约就破了。目录软链接同样要挡，
    因为软链接目录里的文件本身并不是软链接。
    """
    if source.is_file():
        if source.is_symlink():
            raise ValueError(f"来源本身是符号链接，拒绝入库：{source}")
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(f"来源不存在：{source}")
    root = source.resolve()
    files = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if _crosses_symlink(source, relative):
            continue
        if not is_within(root, path):
            continue
        files.append(path)
    return sorted(files)


def _crosses_symlink(source: Path, relative: Path) -> bool:
    """路径的任何一段（含中间目录）是符号链接就返回True。"""
    current = source
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def build_manifest(
    plan: CorpusPlan,
    *,
    license_text: str = "unknown",
    source_url: str | None = None,
    intranet_url: str | None = None,
    scope: str = "手工添加的本地语料；逐文件记录来源与双Hash。",
) -> dict:
    """按现有 schema_version 3 生成manifest；字段格式与五个正式语料保持一致。"""
    entries = []
    for item in plan.files:
        provenance: dict[str, str] = {}
        if intranet_url:
            provenance["intranet_url"] = intranet_url
        if not item.eligible:
            provenance["excluded_reason"] = "；".join(item.notes) or "不可检索"
        if item.parse_report:
            # 检测事实随 manifest 长期保存：入库之后仍能只凭 manifest 复核
            # "这份 PDF 当时为什么走 OCR""哪些页没有被索引"。
            provenance["parse_report"] = item.parse_report
        if item.notes:
            provenance["notes"] = list(item.notes)
        entries.append({
            "path": item.relative,
            "raw_sha256": item.raw_sha256,
            "canonical_sha256": item.canonical_sha256,
            "normalization": item.normalization,
            "encoding": item.encoding,
            "bytes": item.size,
            "indexable": item.eligible,
            "retrieval_eligible": item.eligible,
            "source_url": source_url or f"urn:wenjin:local:{plan.corpus_id}/{item.relative}",
            "logical_document_id": f"{plan.corpus_id}:{item.relative}",
            "parser": item.parser,
            "parser_status": item.parser_status,
            "provenance": provenance,
        })
    entries.sort(key=lambda entry: entry["path"])
    manifest = {
        "schema_version": 3,
        "corpus_id": plan.corpus_id,
        "library": plan.library,
        "version": plan.version,
        "retrieved_on": date.today().isoformat(),
        "raw_data_subdir": f"{plan.library}/{plan.version}",
        "license": license_text,
        "source_url": source_url,
        "scope": scope,
        "files": entries,
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    manifest["retrieval_content_sha256"] = manifest_content_fingerprint({
        "files": [{**entry, "indexable": entry["retrieval_eligible"]} for entry in entries]
    })
    manifest["retrieval_scope_files"] = [
        entry["path"] for entry in entries if entry["retrieval_eligible"]
    ]
    return manifest


def materialize(plan: CorpusPlan, raw_root: Path) -> list[str]:
    """把原文复制进raw_data；已存在且Hash相同的文件跳过，冲突则拒绝。

    `raw_root` 是**语料根**（`raw_data/<library>/<version>`），不是 `raw_data`。
    它和 `retrieve_chunk_counts` / `load_retrieval_documents` 的同名参数不是一回事：
    那两个接的是 `raw_data` 根，会自己接上 manifest 的 `raw_data_subdir`。
    """
    import hashlib

    written = []
    for item in plan.files:
        # 落盘路径同样过一遍安全解析：不能只靠上游已经检查过。
        target = resolve_within(raw_root, item.relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != item.raw_sha256:
                raise FileExistsError(f"目标已存在且内容不同，拒绝覆盖：{target}")
            continue
        copyfile(item.path, target)
        written.append(item.relative)
    return written


def retrieve_chunk_counts(
    manifest_path: Path, raw_root: Path, *, strategy: str = "blocks-split"
) -> dict[str, int]:
    """按manifest重新解析并切片，统计每个文档单元的chunk数。"""
    from wenjin.ingestion.multiformat import load_retrieval_documents

    counts: dict[str, int] = {}
    for document in load_retrieval_documents(manifest_path, raw_root):
        chunks = block_chunks(
            document, max_chars=1600, split_oversized=strategy == "blocks-split"
        )
        counts[document["source_path"]] = counts.get(document["source_path"], 0) + len(chunks)
    return counts


def empty_retrieval_entries(manifest: dict, counts: dict[str, int]) -> list[str]:
    """标为可检索但没有产出任何chunk的文件——必须报错，不能静默丢弃。"""
    subdir = manifest["raw_data_subdir"]
    return [
        entry["path"] for entry in manifest["files"]
        if entry["retrieval_eligible"]
        and not counts.get(f"raw_data/{subdir}/{entry['path']}", 0)
    ]
