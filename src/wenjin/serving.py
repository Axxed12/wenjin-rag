"""原文取件：把证据里的来源路径解析成语料目录下的真实文件并校验字节。

只做定位、路径适配和校验，**不碰HTTP**：MCP端点、CLI或将来的独立服务都复用同一套。

三类路径容易混淆，这里集中处理：
1. chunk里的`source_path`：新语料是`raw_data/<library>/<version>/...`，OSG是裸的
   `include/osg/Node`——同一份语料两种写法，靠`CorpusSources.match_chunk_path`统一；
2. manifest里的登记路径：始终是语料内相对路径，是取件的唯一权威索引；
3. 磁盘路径：由`raw_data_subdir + 登记路径`得到，且必须落在可信目录内。

安全解析统一走 `wenjin.paths.resolve_within`，不在这里另写一套。
"""

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from wenjin.catalog import load_manifest
from wenjin.corpus_identity import resolve_identity
from wenjin.paths import UnsafePathError, resolve_within

# manifest里的内容Hash必须是64位小写十六进制。
_HASH_FORMAT = re.compile(r"^[0-9a-f]{64}$")
# 读取内容Hash时接受的字段，按优先级排列。
# `sha256`是OSG早期manifest的字段名，保留兼容；两个字段都没有就必须拒绝。
_HASH_FIELDS = ("raw_sha256", "sha256")


def content_hash_of(entry: dict) -> str:
    """取文件记录里的内容Hash，并校验格式。

    兼容规则是明确的：先看`raw_sha256`，再看旧字段`sha256`；都没有、空串或格式非法
    一律拒绝。**不做"取不到就跳过校验"的降级**——那等于把完整性承诺悄悄取消。
    """
    for field in _HASH_FIELDS:
        value = entry.get(field)
        if value is None or value == "":
            continue
        text = str(value).strip().lower()
        if not _HASH_FORMAT.fullmatch(text):
            raise SourceUnverified(
                f"文件记录的内容Hash格式非法（字段{field}）：{entry.get('path')}"
            )
        return text
    raise SourceUnverified(
        f"文件记录缺少内容Hash，无法校验取回的是否为被引用的原文：{entry.get('path')}"
    )

RAW_PREFIX = "raw_data/"
DOWNLOAD_PREFIX = "/files"
# 单次取件的体积上限。取件把整个文件读进内存并校验，"校验的字节"和"送出的字节"是同一份，
# 不留"校验后又按路径重开"的竞态。
#
# 上限按真实语料定：检索白名单里最大的文件是 OGC 3D Tiles 规范（HTML 50.4MB / PDF 48.7MB），
# 归档件里最大的是 osgEarth 源码包 122.5MB。256MB 覆盖全部登记文件并留出余量。
# 代价是单个请求峰值内存≈文件大小；十人内网、带鉴权的前提下可接受。
# 若将来并发取大文件成为问题，再改成"同一文件句柄先流式校验、再 seek 回起点送出"的方案。
MAX_SERVE_BYTES = 256 * 1024 * 1024

# 这些类型让浏览器直接打开而不是下载；其余按附件处理更安全。
INLINE_TYPES = {"application/pdf", "text/plain", "text/markdown",
                "image/png", "image/jpeg", "image/gif"}
# HTML可以内联展示（用户就是要在浏览器里看原文档），但**必须**带CSP sandbox：
# 语料是第三方内容，还支持用户自己上传，内联渲染等于让它们的脚本跑在服务端源上。
# sandbox不带allow-scripts → 不执行脚本；不带allow-same-origin → 拿不到本站凭据。
SANDBOXED_INLINE_TYPES = {"text/html", "application/xhtml+xml"}
SANDBOX_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"
# XML可能带XSLT，各浏览器行为不一致，按附件处理而不是内联。


class SourceNotFound(FileNotFoundError):
    """语料里没有登记这个文件，或路径不合法。"""


class SourceModified(RuntimeError):
    """文件字节与manifest登记的raw_sha256不符——语料被改动过，不能作为原文送出。"""


class SourceUnverified(RuntimeError):
    """manifest没有为这个文件留下可用的内容Hash，无法证明取回的是被引用的那份原文。

    这种情况必须拒绝，不能降级成"无校验下载"：整个取件的价值就是"你拿到的确实是
    证据引用的那份字节"，没有Hash这个承诺就不成立。
    """


class SourceTooLarge(RuntimeError):
    """超过单次取件上限。"""


@dataclass(frozen=True)
class SourceRecord:
    """manifest登记的一个文件；取件的权威依据。"""

    relative: str
    raw_sha256: str | None
    size: int | None
    retrieval_eligible: bool
    indexable: bool
    encoding: str | None = None
    canonical_sha256: str | None = None
    normalization: str | None = None
    parser: str | None = None
    multiformat_input: bool = False

    @property
    def is_text(self) -> bool:
        """manifest里声明了文本编码就是文本。

        OSG的头文件没有扩展名（`include/osg/Node`），按mimetypes只能猜到
        octet-stream，浏览器会当二进制下载；manifest里的encoding才是权威依据。
        """
        return bool(self.encoding)

    @property
    def has_content_hash(self) -> bool:
        """是否有可用的内容Hash。

        归档件也能下载（`retrieval_eligible`只决定是否进检索），但**没有Hash就不能下载**：
        取件的前提是能证明"这份字节就是证据引用的那份"。
        """
        return bool(self.raw_sha256 and _HASH_FORMAT.fullmatch(self.raw_sha256))


@dataclass(frozen=True)
class VerifiedSource:
    """已通过校验、可以直接送出的原文。"""

    record: SourceRecord
    content: bytes
    media_type: str
    inline: bool
    sandboxed: bool

    def response_headers(self) -> dict[str, str]:
        """取件响应头；sandbox标记决定要不要加CSP。

        文件名按RFC 5987编码，避免把中文或引号直接拼进响应头。
        """
        name = Path(self.record.relative).name
        headers = {
            "Content-Disposition": (
                f'{"inline" if self.inline else "attachment"}; '
                f"filename*=UTF-8''{quote(name, safe='')}"
            ),
            "Content-Type": self.media_type,
            "Content-Length": str(len(self.content)),
            "X-Content-Type-Options": "nosniff",
        }
        if self.sandboxed:
            headers["Content-Security-Policy"] = SANDBOX_CSP
        return headers


def guess_media_type(name: str) -> str:
    media_type, _ = mimetypes.guess_type(name)
    # man手册这类无后缀文件按纯文本处理，浏览器才不会当二进制下载。
    return media_type or "application/octet-stream"


def encode_relative_path(relative: str) -> str:
    """逐段编码语料内相对路径，保留分隔符。"""
    return "/".join(quote(part, safe="") for part in relative.split("/"))


def download_path_for(
    corpus_id: str, relative: str, *, release_id: str | None = None
) -> str:
    """生成取件URL；`relative`必须是manifest登记路径。

    只做拼接、不做鉴权：鉴权发生在请求真正打向端点的时候。
    """
    path = f"{DOWNLOAD_PREFIX}/{quote(corpus_id, safe='')}/{encode_relative_path(relative)}"
    if release_id:
        return f"{path}?release_id={quote(release_id, safe='')}"
    return path


class CorpusSources:
    """一个语料的原文视图：登记记录、可信目录、路径适配与字节校验。"""

    def __init__(
        self,
        *,
        corpus_id: str,
        raw_data_subdir: str,
        raw_root: Path,
        records: dict[str, SourceRecord],
    ) -> None:
        self.corpus_id = corpus_id
        self.raw_data_subdir = raw_data_subdir.strip("/")
        self.raw_root = raw_root
        self.records = records

    @classmethod
    def from_manifest(
        cls, manifest_path: Path, *, raw_root: Path, expected_corpus_id: str
    ) -> "CorpusSources":
        """从manifest建立原文视图。

        身份用**目录名 + manifest声明 + 请求身份**三方一致的结果，和索引目标、语料目录
        共用同一个判定（`resolve_identity`）。只比较"请求身份 vs manifest声明"是不够的：
        那会让一个放在`private-1/`却声明成`public-1`的语料顶着公开身份被取件。

        每一条文件记录都必须有合法内容Hash，否则整份manifest拒绝加载——不能只让
        "碰巧缺Hash的那一条"降级成无校验下载。
        """
        manifest = load_manifest(manifest_path)
        identity = resolve_identity(
            directory=manifest_path.parent.name,
            declared=manifest.get("corpus_id"),
            explicit=expected_corpus_id,
            context=f"（{manifest_path}）",
        )
        records = {
            str(entry["path"]): SourceRecord(
                relative=str(entry["path"]),
                raw_sha256=content_hash_of(entry),
                size=entry.get("bytes"),
                retrieval_eligible=bool(
                    entry.get("retrieval_eligible", entry.get("indexable", False))
                ),
                indexable=bool(entry.get("indexable", False)),
                encoding=entry.get("encoding"),
                canonical_sha256=(str(entry["canonical_sha256"]).lower()
                                  if entry.get("canonical_sha256") else None),
                normalization=entry.get("normalization"),
                parser=str(entry["parser"]) if entry.get("parser") else None,
                multiformat_input="retrieval_eligible" in entry,
            )
            for entry in manifest.get("files", [])
            if entry.get("path")
        }
        return cls(
            corpus_id=identity.corpus_id,
            raw_data_subdir=str(manifest.get("raw_data_subdir", "")),
            raw_root=raw_root,
            records=records,
        )

    def match_chunk_path(self, source_path: str) -> SourceRecord:
        """路径适配层：把chunk里的source_path映射到manifest登记记录。

        新语料的source_path带`raw_data/<subdir>/`前缀，OSG的是裸相对路径；
        两种都要支持，而且**不改chunk ID和source_path**——旧索引不需要重建。
        """
        normalized = source_path.replace("\\", "/").lstrip("/")
        if normalized.startswith(RAW_PREFIX):
            rest = normalized[len(RAW_PREFIX):]
            prefix = f"{self.raw_data_subdir}/" if self.raw_data_subdir else ""
            if not prefix or not rest.startswith(prefix):
                raise SourceNotFound(f"证据来源不属于该语料：{source_path}")
            candidate = rest[len(prefix):]
        else:
            candidate = normalized
        record = self.records.get(candidate)
        if record is None:
            raise SourceNotFound(f"文件未登记在manifest中：{source_path}")
        return record

    def locate(self, relative: str) -> Path:
        """登记路径 → 可信目录内的磁盘路径。"""
        if relative not in self.records:
            raise SourceNotFound(f"文件未登记在manifest中：{relative}")
        combined = f"{self.raw_data_subdir}/{relative}" if self.raw_data_subdir else relative
        try:
            return resolve_within(self.raw_root, combined)
        except UnsafePathError as error:
            raise SourceNotFound(str(error)) from error

    def read_verified(self, relative: str, *, max_bytes: int = MAX_SERVE_BYTES) -> VerifiedSource:
        """读取并校验字节；返回的就是被校验的那一份，不再按路径重开。

        「先校验、再按路径打开另一份」存在竞态：两次之间文件可能被替换。这里把字节
        读进内存、校验、原样送出，校验对象和送出对象是同一份。
        """
        record = self.records.get(relative)
        if record is None:
            raise SourceNotFound(f"文件未登记在manifest中：{relative}")
        path = self.locate(relative)
        if not path.is_file():
            raise SourceNotFound(f"语料里没有这个文件：{relative}")
        if record.size is not None and record.size > max_bytes:
            raise SourceTooLarge(f"文件超过单次取件上限：{relative}")
        if path.stat().st_size > max_bytes:
            raise SourceTooLarge(f"文件超过单次取件上限：{relative}")
        if not record.has_content_hash:
            # 这里再挡一次：即使某条记录绕过了from_manifest，也不能降级成无校验下载。
            raise SourceUnverified(f"文件记录缺少可用的内容Hash，拒绝取件：{relative}")
        content = path.read_bytes()
        actual = hashlib.sha256(content).hexdigest()
        if actual != record.raw_sha256:
            raise SourceModified(f"文件字节与manifest登记的Hash不符：{relative}")
        media_type = guess_media_type(path.name)
        if media_type == "application/octet-stream" and record.is_text:
            # 无扩展名的文本（如OSG头文件）：按纯文本内联，浏览器直接可读。
            media_type = "text/plain"
        return VerifiedSource(
            record=record,
            content=content,
            media_type=media_type,
            inline=media_type in INLINE_TYPES or media_type in SANDBOXED_INLINE_TYPES,
            sandboxed=media_type in SANDBOXED_INLINE_TYPES,
        )

    def listing(self) -> list[SourceRecord]:
        """下载清单：manifest里**全部**登记文件，不只retrieval_eligible。"""
        return sorted(self.records.values(), key=lambda item: item.relative)


# 丢弃原因码：可以安全地给客户端看，不含语料名、路径或chunk ID。
DROP_FOREIGN_CORPUS = "foreign_corpus"
DROP_UNREGISTERED = "unregistered_file"
DROP_NO_SOURCE_PATH = "missing_source_path"


@dataclass(frozen=True)
class AttachmentResult:
    """证据补充取件链接的结果。

    `dropped` 是**服务端内部**的完整明细（含身份与路径），只用于审计和排查；
    给客户端时必须用 `dropped_summary()`——否则虽然正文没给，语料名和文件名仍会泄漏，
    而"某个私有语料里有个叫X的文件"本身就是机密。
    """

    evidence: list[dict]
    dropped: list[dict]

    def dropped_summary(self) -> dict:
        """客户端可见的摘要：只有数量和原因码。"""
        counts: dict[str, int] = {}
        for item in self.dropped:
            code = item.get("code", "unknown")
            counts[code] = counts.get(code, 0) + 1
        return {"count": len(self.dropped), "reasons": counts}


def attach_download_paths(
    evidence: list[dict],
    sources: CorpusSources,
    *,
    release_id: str | None = None,
) -> AttachmentResult:
    """给每条证据补上取件链接。

    两条硬约束：
    - **不覆盖证据身份**：已经带`corpus_id`的条目原样保留，不重标成当前语料；
    - 映射不到本语料的证据**丢弃并报告**，不能悄悄贴上当前语料的名字。

    只做拼接、不做鉴权——鉴权发生在请求真正打向端点的时候。
    """
    kept: list[dict] = []
    dropped: list[dict] = []
    for item in evidence:
        copied = dict(item)
        own = copied.get("corpus_id")
        if own is not None and own != sources.corpus_id:
            dropped.append({
                "code": DROP_FOREIGN_CORPUS,
                "chunk_id": copied.get("chunk_id"),
                "source_path": copied.get("source_path"),
                "corpus_id": own,
                "reason": f"证据属于语料 {own}，不属于当前语料 {sources.corpus_id}",
            })
            continue
        source_path = copied.get("source_path")
        if not source_path:
            dropped.append({"code": DROP_NO_SOURCE_PATH, "chunk_id": copied.get("chunk_id"),
                            "reason": "证据缺少source_path"})
            continue
        try:
            record = sources.match_chunk_path(source_path)
        except SourceNotFound as error:
            dropped.append({
                "code": DROP_UNREGISTERED,
                "chunk_id": copied.get("chunk_id"),
                "source_path": source_path,
                "reason": str(error),
            })
            continue
        copied["corpus_id"] = sources.corpus_id
        copied["source_relative_path"] = record.relative
        copied["download_path"] = download_path_for(
            sources.corpus_id, record.relative, release_id=release_id
        )
        kept.append(copied)
    return AttachmentResult(kept, dropped)
