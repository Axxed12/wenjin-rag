"""外部语料审查/导入脚本共享的路径与内容检查。"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath

from wenjin.content_hashing import BINARY_IDENTITY_V1, TEXT_NEWLINE_LF_V1, hash_content


def safe_path(root: Path, value: str) -> Path:
    """先检查跨平台绝对路径，再检查解析后的真实路径边界。"""
    root = root.resolve()
    if PureWindowsPath(value).drive or value.startswith(("/", "\\")):
        raise ValueError(f"absolute path: {value}")
    path = (root / value.replace("\\", "/")).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path escape: {value}")
    return path


def hashes(raw: bytes, binary: bool = False) -> dict:
    content = hash_content(
        raw,
        normalization=BINARY_IDENTITY_V1 if binary else TEXT_NEWLINE_LF_V1,
        encoding=None if binary else "utf-8",
    )
    return {
        key: getattr(content, key)
        for key in ("raw_sha256", "canonical_sha256", "normalization", "encoding")
    }


def normalized(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())
