"""原文取件：把chunk里的source_path解析成raw_data下的真实文件。

只做定位和媒介类型判断，**不碰HTTP**：MCP端点、将来的独立服务或CLI都可以复用。
安全解析统一走 `wenjin.paths.resolve_within`，不在这里另写一套。
"""

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from wenjin.paths import UnsafePathError, is_within, resolve_within

RAW_PREFIX = "raw_data/"
DOWNLOAD_PREFIX = "/files"

# 这些类型让浏览器直接打开而不是下载；其余按附件处理更安全。
INLINE_TYPES = {"application/pdf", "text/plain", "text/html", "text/markdown", "text/xml",
                "application/xml", "image/png", "image/jpeg", "image/gif"}


class SourceNotFound(FileNotFoundError):
    """语料里没有这个文件，或路径不合法。"""


@dataclass(frozen=True)
class ServedSource:
    path: Path
    media_type: str
    inline: bool


def strip_raw_prefix(source_path: str) -> str:
    """chunk里的source_path带`raw_data/`前缀，取件时要剥掉。"""
    normalized = source_path.replace("\\", "/").lstrip("/")
    if normalized.startswith(RAW_PREFIX):
        return normalized[len(RAW_PREFIX):]
    return normalized


def download_path_for(corpus_id: str, source_path: str) -> str:
    """生成给调用方用的取件路径；只是路径拼接，不做鉴权。

    路径是 raw_data 相对的（和chunk里的source_path一致），语料ID只用于鉴权；
    端点在放行后还会确认文件确实落在该语料自己的目录里。
    """
    relative = strip_raw_prefix(source_path)
    return f"{DOWNLOAD_PREFIX}/{corpus_id}/{relative}"


def guess_media_type(path: Path) -> str:
    media_type, _ = mimetypes.guess_type(path.name)
    # man手册这类无后缀文件按纯文本处理，浏览器才不会当二进制下载。
    return media_type or "application/octet-stream"


class SourceLibrary:
    """一个语料原文根目录的只读视图。根目录是 raw_data/，语料再按 library/version 分层。"""

    def __init__(self, raw_root: Path) -> None:
        self.raw_root = raw_root

    def resolve(self, source_path: str) -> ServedSource:
        """把chunk的source_path解析成可读文件；越界或不存在都抛 SourceNotFound。"""
        relative = strip_raw_prefix(source_path)
        try:
            path = resolve_within(self.raw_root, relative)
        except UnsafePathError as error:
            raise SourceNotFound(str(error)) from error
        if not path.is_file():
            raise SourceNotFound(f"语料里没有这个文件：{source_path}")
        media_type = guess_media_type(path)
        return ServedSource(path, media_type, media_type in INLINE_TYPES)

    def resolve_in_corpus(self, raw_data_subdir: str, file_path: str) -> ServedSource:
        """在指定语料自己的目录范围内取件。

        取件路径是 raw_data 相对的，所以必须再确认解析结果落在该语料的子目录里：
        否则拿 A 语料的授权就能读 B 语料，属于越权访问（IDOR）。
        """
        served = self.resolve(file_path)
        try:
            corpus_root = resolve_within(self.raw_root, raw_data_subdir)
        except UnsafePathError as error:
            raise SourceNotFound(f"语料目录不合法：{raw_data_subdir}") from error
        if not is_within(corpus_root, served.path):
            raise SourceNotFound(f"文件不属于该语料：{file_path}")
        return served

    def exists(self, source_path: str) -> bool:
        try:
            self.resolve(source_path)
        except SourceNotFound:
            return False
        return True


def attach_download_paths(evidence: list[dict], corpus_id: str) -> list[dict]:
    """给每条证据补上取件路径；不改动其他字段，便于调用方直接下载原文。

    只做拼接、不做鉴权——鉴权发生在请求真正打向端点的时候。
    """
    enriched = []
    for item in evidence:
        copied = dict(item)
        source_path = copied.get("source_path")
        if source_path:
            copied["download_path"] = download_path_for(corpus_id, source_path)
            copied["corpus_id"] = corpus_id
        enriched.append(copied)
    return enriched
