"""原始字节与规范内容的双Hash契约。

raw SHA256用于传输和供应链审计；canonical SHA256用于去重、切片和索引稳定性。
规范化策略必须带版本，规则变化时应构建新的候选索引。
"""

import hashlib
import json
from dataclasses import dataclass

TEXT_NEWLINE_LF_V1 = "text-newline-lf-v1"
BINARY_IDENTITY_V1 = "binary-identity-v1"
SUPPORTED_PROFILES = {TEXT_NEWLINE_LF_V1, BINARY_IDENTITY_V1}


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def normalize_newlines(text: str) -> str:
    """只统一CRLF/CR为LF；不改空白、Unicode、末尾换行或正文内容。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class HashedContent:
    raw_sha256: str
    canonical_sha256: str
    normalization: str
    encoding: str | None
    canonical_bytes: bytes

    def canonical_text(self) -> str:
        if self.encoding is None:
            raise TypeError("二进制内容没有可供文本切片的canonical_text")
        return self.canonical_bytes.decode("utf-8")


def hash_content(
    raw_bytes: bytes,
    *,
    normalization: str,
    encoding: str | None = None,
) -> HashedContent:
    """根据显式profile生成双Hash；未知策略直接拒绝，不能猜测。"""
    if normalization not in SUPPORTED_PROFILES:
        raise ValueError(f"不支持的内容规范化策略：{normalization}")
    raw_sha256 = sha256_hex(raw_bytes)
    if normalization == BINARY_IDENTITY_V1:
        if encoding is not None:
            raise ValueError("binary-identity-v1不能声明文本编码")
        canonical_bytes = raw_bytes
    else:
        if not encoding:
            raise ValueError("文本规范化必须声明encoding")
        # 严格解码；不使用errors='ignore'，避免损坏API名称和证据。
        canonical_text = normalize_newlines(raw_bytes.decode(encoding))
        canonical_bytes = canonical_text.encode("utf-8")
    return HashedContent(
        raw_sha256=raw_sha256,
        canonical_sha256=sha256_hex(canonical_bytes),
        normalization=normalization,
        encoding=encoding,
        canonical_bytes=canonical_bytes,
    )


def manifest_content_fingerprint(manifest: dict) -> str:
    """对可索引文件的路径、规范Hash和profile求指纹，不受JSON排版影响。"""
    rows = []
    for entry in manifest["files"]:
        if entry.get("indexable") is False:
            continue
        content_hash = (
            entry.get("canonical_sha256")
            or entry.get("raw_sha256")
            or entry.get("sha256")
        )
        if not content_hash:
            raise ValueError(f"manifest文件缺少内容Hash：{entry.get('path')}")
        rows.append(
            {
                "path": entry["path"],
                "canonical_sha256": content_hash.lower(),
                "normalization": entry.get("normalization", "legacy-raw-v1"),
            }
        )
    payload = json.dumps(
        sorted(rows, key=lambda row: row["path"]),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(payload)
