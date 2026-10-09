"""不同用途的高熵不透明令牌及其单向摘要。"""

from __future__ import annotations

import hashlib
import re
import secrets

_TOKEN_RE = re.compile(r"^(wjs|wjm)_[A-Za-z0-9_-]{40,60}$")


def issue_token(kind: str) -> tuple[str, str, str]:
    """签发仅返回一次的令牌，返回 (明文, SHA-256摘要, 可展示短前缀)。"""
    if kind not in {"session", "mcp"}:
        raise ValueError("未知令牌用途")
    prefix = "wjs_" if kind == "session" else "wjm_"
    token = prefix + secrets.token_urlsafe(32)
    return token, token_digest(token), token[:12]


def token_digest(token: str) -> str:
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ValueError("令牌格式无效")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def require_token_kind(token: str, kind: str) -> str:
    expected = "wjs_" if kind == "session" else "wjm_" if kind == "mcp" else None
    if expected is None or not isinstance(token, str) or not token.startswith(expected):
        raise ValueError("令牌用途不匹配")
    return token_digest(token)
