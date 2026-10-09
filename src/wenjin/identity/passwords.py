"""本地账户密码哈希。只使用标准库 scrypt，不保存或返回明文密码。"""

from __future__ import annotations

import hashlib
import hmac
import os
from functools import lru_cache

SCHEME = "scrypt-v1"
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_MIN_PASSWORD_CHARS = 12
_MAX_PASSWORD_CHARS = 256
_MAX_PASSWORD_BYTES = 1024
_DUMMY_PASSWORD = "wenjin-invalid-password-placeholder"


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("密码必须是字符串")
    encoded = password.encode("utf-8")
    if not _MIN_PASSWORD_CHARS <= len(password) <= _MAX_PASSWORD_CHARS:
        raise ValueError("密码长度必须为12到256个字符")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise ValueError("密码UTF-8编码不能超过1024字节")
    if not password.strip():
        raise ValueError("密码不能只包含空白字符")


def hash_password(password: str) -> tuple[str, bytes, bytes]:
    """返回版本、随机salt和派生值；派生值只用于验证，禁止写入API响应。"""
    validate_password(password)
    salt = os.urandom(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_KEY_BYTES,
    )
    return SCHEME, salt, derived


def verify_password(password: str, scheme: str, salt: bytes, expected: bytes) -> bool:
    if scheme != SCHEME or not isinstance(password, str):
        return False
    try:
        encoded = password.encode("utf-8")
    except UnicodeError:
        return False
    if len(encoded) > _MAX_PASSWORD_BYTES:
        return False
    actual = hashlib.scrypt(
        encoded, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        dklen=len(expected),
    )
    return hmac.compare_digest(actual, expected)


@lru_cache(maxsize=1)
def dummy_password_record() -> tuple[str, bytes, bytes]:
    """给未知用户名使用的进程内占位哈希，避免明显的账户存在性时序差异。"""
    return hash_password(_DUMMY_PASSWORD)
