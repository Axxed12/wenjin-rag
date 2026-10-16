"""逐请求向同源管理服务验证MCP身份；本模块不持久化token或权限快照。"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from wenjin.identity.policy import RESOURCE_KINDS, AuthenticationFailed, Principal
from wenjin.mcp.managed_client import (
    DEFAULT_MANAGEMENT_URL,
    _bridge_timeout_from_environment,
)

_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_MCP_BEARER_RE = re.compile(r"^wjm_[A-Za-z0-9_-]{16,256}$")


class RemoteIdentityUnavailable(RuntimeError):
    """身份权威不可用或响应不符合固定合同。"""


@dataclass(frozen=True)
class RemoteAccessSnapshot:
    """管理端计算后的可读活动项；不含授权记录或被拒资源。"""

    principal: Principal
    readable_resources: frozenset[tuple[str, str]]
    readable_managed_files: frozenset[tuple[str, str]]

    def allows(self, resource_kind: str, resource_id: str) -> bool:
        return (resource_kind, resource_id) in self.readable_resources

    def allows_managed_file(self, library_id: str, file_id: str) -> bool:
        return (library_id, file_id) in self.readable_managed_files


class RemoteIdentityStore:
    """IdentityStore兼容的远端身份适配器，只保留共享HTTP client。"""

    def __init__(
        self,
        base_url: str,
        *,
        allow_private_http: bool,
        timeout: httpx.Timeout | float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        parts = urlsplit(normalized)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError("身份权威地址配置无效")
        if parts.query or parts.fragment:
            raise ValueError("身份权威地址不能包含query或fragment")
        if parts.scheme != "https":
            hostname = (parts.hostname or "").lower()
            try:
                loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = False
            if not loopback and not allow_private_http:
                raise ValueError("非本机身份桥必须使用HTTPS或显式允许私网HTTP")
        self.base_url = normalized
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout if timeout is not None else _bridge_timeout_from_environment(),
            trust_env=False,
            follow_redirects=False,
        )

    @classmethod
    def from_environment(cls) -> "RemoteIdentityStore":
        import os

        base_url = os.getenv("WENJIN_FILE_MANAGEMENT_URL", DEFAULT_MANAGEMENT_URL)
        allow_private_http = (
            os.getenv("WENJIN_FILE_MANAGEMENT_ALLOW_PRIVATE_HTTP", "").strip() == "1"
        )
        try:
            return cls(
                base_url,
                allow_private_http=allow_private_http,
                timeout=_bridge_timeout_from_environment(),
            )
        except (ValueError, RuntimeError) as exc:
            raise RemoteIdentityUnavailable("身份桥接配置无效") from exc

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def authenticate_request(self, bearer: str) -> tuple[Principal, RemoteAccessSnapshot]:
        """每次HTTP请求都用当前Bearer向身份权威查询，不按用户缓存。"""
        if not isinstance(bearer, str) or not _MCP_BEARER_RE.fullmatch(bearer):
            raise AuthenticationFailed("MCP凭据不可用")
        try:
            with self._client.stream(
                "GET",
                f"{self.base_url}/api/mcp/identity/snapshot",
                headers={"Authorization": f"Bearer {bearer}"},
            ) as response:
                if response.status_code == 401:
                    raise AuthenticationFailed("MCP凭据不可用")
                if response.status_code != 200:
                    raise RemoteIdentityUnavailable("身份权威暂不可用")
                chunks = []
                length = 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > 2_097_152:
                        raise RemoteIdentityUnavailable("身份权威响应无效")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except (AuthenticationFailed, RemoteIdentityUnavailable):
            raise
        except httpx.HTTPError as exc:
            raise RemoteIdentityUnavailable("身份权威暂不可用") from exc
        try:
            payload = httpx.Response(200, content=body).json()
            principal_data = payload["principal"]
            access_data = payload["access"]
            if payload.get("schema_version") != 1:
                raise ValueError
            principal = Principal(
                user_id=_resource_id(principal_data["user_id"]),
                username=_safe_username(principal_data["username"]),
                role=principal_data["role"],
                revision=_positive_revision(principal_data["revision"]),
                must_change_password=principal_data["must_change_password"],
            )
            if (
                principal.role not in {"admin", "reader"}
                or principal.must_change_password is not False
            ):
                raise ValueError
            resources = frozenset(
                (_resource_kind(item["resource_kind"]), _resource_id(item["resource_id"]))
                for item in _list_of_dicts(access_data["readable_resources"])
            )
            managed_files = frozenset(
                (_resource_id(item["library_id"]), _resource_id(item["file_id"]))
                for item in _list_of_dicts(access_data["readable_managed_files"])
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RemoteIdentityUnavailable("身份权威响应无效") from exc
        snapshot = RemoteAccessSnapshot(principal, resources, managed_files)
        return principal, snapshot


def _resource_kind(value: object) -> str:
    if not isinstance(value, str) or value not in RESOURCE_KINDS:
        raise ValueError
    return value


def _resource_id(value: object) -> str:
    if not isinstance(value, str) or not _RESOURCE_ID_RE.fullmatch(value):
        raise ValueError
    return value


def _safe_username(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError
    return value


def _positive_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError
    return value


def _list_of_dicts(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100_000:
        raise ValueError
    if any(not isinstance(item, dict) for item in value):
        raise ValueError
    return value
