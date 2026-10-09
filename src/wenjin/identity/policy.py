"""身份与授权的稳定数据类型。MCP协议session ID不属于这些身份。"""

from __future__ import annotations

from dataclasses import dataclass, field

ROLES = frozenset({"admin", "reader"})
RESOURCE_KINDS = frozenset({"library", "managed_file", "managed_library"})


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    revision: int
    must_change_password: bool = False


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    username: str
    role: str
    active: bool
    must_change_password: bool
    revision: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IssuedCredential:
    credential_id: str
    token: str = field(repr=False)
    token_prefix: str
    created_at: str
    expires_at: str | None


@dataclass(frozen=True)
class LoginSession:
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: Principal
    expires_at: str
    must_change_password: bool


@dataclass(frozen=True)
class GrantRecord:
    resource_kind: str
    resource_id: str
    principal_kind: str
    principal_id: str
    allowed: bool
    revision: int
    updated_at: str
    updated_by: str


class IdentityError(RuntimeError):
    """本地身份域操作错误基类。"""


class AuthenticationFailed(IdentityError):
    """账号/密码/令牌不可用；外部响应应保持一致。"""


class CurrentPasswordIncorrect(AuthenticationFailed):
    """当前密码验证失败；页面可给出中文纠正提示，不应误当作过期会话。"""


class Forbidden(IdentityError):
    """操作者无权执行该身份域操作。"""


class Conflict(IdentityError):
    """CAS版本冲突、唯一约束冲突或最后管理员保护。"""


class NotFound(IdentityError):
    """用户、凭据、资源或授权记录不存在。"""
