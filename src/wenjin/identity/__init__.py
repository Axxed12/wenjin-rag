"""本地小团队账号、个人MCP凭据与稳定资源授权。"""

from wenjin.identity.policy import (
    AuthenticationFailed,
    Conflict,
    Forbidden,
    GrantRecord,
    IdentityError,
    IssuedCredential,
    LoginSession,
    NotFound,
    Principal,
    UserRecord,
)
from wenjin.identity.store import IdentityStore

__all__ = [
    "AuthenticationFailed",
    "Conflict",
    "Forbidden",
    "GrantRecord",
    "IdentityError",
    "IdentityStore",
    "IssuedCredential",
    "LoginSession",
    "NotFound",
    "Principal",
    "UserRecord",
]
