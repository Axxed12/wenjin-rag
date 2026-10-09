"""MCP 对统一文件管理服务的只读桥接。

桥接只通过现有管理HTTP服务读取实时状态；不能在MCP进程内再创建
FileManagementService，因为同一项目root的管理器受单进程锁保护。
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:8777"
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ManagedFileServiceError(RuntimeError):
    """管理服务不可用或返回了意外结果；不携带内部响应正文。"""


class ManagedFileConflict(ValueError):
    """版本或检索参数与当前active状态不匹配。"""


class ManagedFileForbidden(ManagedFileServiceError):
    """管理服务拒绝当前personal principal读取文件。"""


class ManagedFileUnauthorized(ManagedFileServiceError):
    """透传credential在桥接请求时已失效。"""


@dataclass(frozen=True)
class ManagedFile:
    """MCP可见的受管文件最小视图。"""

    file_id: str
    display_name: str
    version: str
    version_id: str
    chunk_count: int | None
    updated_at: str
    library_id: str = "managed-files"

    @classmethod
    def from_api(cls, value: object) -> "ManagedFile | None":
        if not isinstance(value, dict):
            return None
        file_id = value.get("file_id")
        version_id = value.get("active_version_id")
        version = value.get("version")
        display_name = value.get("display_name")
        if (
            value.get("managed") is not True
            or value.get("status") != "active"
            or not isinstance(file_id, str)
            or not _FILE_ID_RE.fullmatch(file_id)
            or not isinstance(version_id, str)
            or not version_id
            or not isinstance(version, str)
            or not version
            or not isinstance(display_name, str)
        ):
            return None
        chunk_count = value.get("chunk_count")
        if not isinstance(chunk_count, int) or isinstance(chunk_count, bool):
            chunk_count = None
        updated_at = value.get("updated_at")
        if not isinstance(updated_at, str):
            updated_at = ""
        return cls(
            file_id=file_id,
            display_name=display_name,
            version=version,
            version_id=version_id,
            chunk_count=chunk_count,
            updated_at=updated_at,
            library_id=(value.get("library_id") if isinstance(value.get("library_id"), str)
                        else "managed-files"),
        )

    def to_dict(self) -> dict:
        return {
            "file_id": self.file_id,
            "display_name": self.display_name,
            "version": self.version,
            "version_id": self.version_id,
            "chunk_count": self.chunk_count,
            "updated_at": self.updated_at,
            "library_id": self.library_id,
        }


def managed_file_corpus_id(file_id: str) -> str:
    """为一个文件生命周期生成稳定ACL身份；replace时不随version_id变化。"""
    if not isinstance(file_id, str) or not _FILE_ID_RE.fullmatch(file_id):
        raise ValueError("file_id不是有效的managed文件身份")
    return f"managed-file-{file_id}"


class ManagedFileHTTPClient:
    """带服务端管理凭据的只读HTTP client。"""

    def __init__(
        self,
        base_url: str,
        admin_token: str,
        *,
        account_mode: bool = False,
        allow_private_http: bool = False,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | float = 60.0,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("文件管理服务地址必须是HTTP(S) URL")
        parts = urlsplit(normalized)
        if account_mode and parts.scheme != "https":
            hostname = (parts.hostname or "").lower()
            try:
                loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = False
            if not loopback and not allow_private_http:
                raise ValueError("账户模式桥接到非本机管理服务必须使用HTTPS")
        if not account_mode and not admin_token.strip():
            raise ValueError("缺少文件管理服务Bearer token")
        self.base_url = normalized
        self.account_mode = account_mode
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout,
            trust_env=False,
            headers={} if account_mode else {"Authorization": f"Bearer {admin_token}"},
        )
        if client is not None and not account_mode:
            # 注入的HTTP client仍由调用方管理；认证头只存在于服务端client对象。
            self._client.headers.update({"Authorization": f"Bearer {admin_token}"})

    @classmethod
    def from_environment(cls, *, account_mode: bool = False) -> "ManagedFileHTTPClient":
        """按MCP服务端环境读取管理地址和凭据；缺token时不降级为匿名。"""
        token = os.getenv("WENJIN_FILE_ADMIN_TOKEN", "")
        if not account_mode and not token:
            raise ManagedFileServiceError("文件管理桥接未配置服务端凭据")
        base_url = os.getenv("WENJIN_FILE_MANAGEMENT_URL", DEFAULT_MANAGEMENT_URL)
        # 仅部署者显式声明内部私网拓扑时放行服务间明文HTTP；不根据hostname猜测可信。
        allow_private_http = (
            os.getenv("WENJIN_FILE_MANAGEMENT_ALLOW_PRIVATE_HTTP", "").strip() == "1"
        )
        try:
            return cls(
                base_url,
                token,
                account_mode=account_mode,
                allow_private_http=allow_private_http,
            )
        except ValueError as exc:
            raise ManagedFileServiceError("文件管理桥接配置无效") from exc

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(
        self, method: str, path: str, *, authorization: str | None = None, **kwargs
    ) -> httpx.Response:
        if self.account_mode:
            if not isinstance(authorization, str) or not authorization.startswith("Bearer wjm_"):
                raise ManagedFileServiceError("当前MCP请求缺少个人凭据")
            kwargs["headers"] = {**kwargs.get("headers", {}), "Authorization": authorization}
        try:
            response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise ManagedFileServiceError("文件管理服务暂不可用") from exc
        if response.status_code == 401:
            raise ManagedFileUnauthorized("当前个人MCP凭据不可用")
        if response.status_code == 403:
            raise ManagedFileForbidden("当前账号没有该managed文件的读取授权")
        if response.status_code == 404:
            raise KeyError("managed文件不存在或已不可用")
        if response.status_code == 409:
            raise ManagedFileConflict("managed文件版本或检索参数与当前状态不匹配")
        if response.status_code == 422:
            raise ManagedFileConflict("managed文件检索参数无效")
        if not response.is_success:
            raise ManagedFileServiceError("文件管理服务请求失败")
        return response

    def list_files(self, *, authorization: str | None = None) -> list[ManagedFile]:
        """实时读取active managed files，过滤legacy、pending及删除中记录。"""
        path = "/api/mcp/files" if self.account_mode else "/api/files"
        response = self._request("GET", path, authorization=authorization)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagedFileServiceError("文件管理服务返回了无效列表") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            raise ManagedFileServiceError("文件管理服务返回了无效列表")
        records = [ManagedFile.from_api(item) for item in payload["files"]]
        return [item for item in records if item is not None]

    def list_libraries(self, *, authorization: str | None = None) -> list[dict]:
        """读取当前个人授权可见目录；account bridge每次使用当前请求token。"""
        path = "/api/mcp/libraries" if self.account_mode else "/api/mcp/libraries"
        response = self._request("GET", path, authorization=authorization)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagedFileServiceError("文件管理服务返回了无效资料库列表") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("libraries"), list):
            raise ManagedFileServiceError("文件管理服务返回了无效资料库列表")
        return [item for item in payload["libraries"] if isinstance(item, dict)]

    def search_file(
        self,
        file_id: str,
        query: str,
        *,
        version: str | None = None,
        top_k: int = 5,
        context_char_budget: int = 8000,
        authorization: str | None = None,
    ) -> dict:
        """在管理服务的当前active版本上查询单个managed文件。"""
        managed_file_corpus_id(file_id)
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query必须是长度不超过2000字符的字符串")
        payload = {
            "file_id": file_id,
            "query": query,
            "top_k": top_k,
            "context_char_budget": context_char_budget,
        }
        if version is not None:
            payload["version"] = version
        if self.account_mode:
            current = next(
                (item for item in self.list_files(authorization=authorization)
                 if item.file_id == file_id),
                None,
            )
            if current is None:
                raise KeyError("managed文件不存在或当前账号无权访问")
            payload["library_id"] = current.library_id
            payload["source_type"] = "managed"
            payload["version_id"] = current.version_id
            path = "/api/mcp/search"
        else:
            path = "/api/search"
        response = self._request("POST", path, authorization=authorization, json=payload)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ManagedFileServiceError("文件管理服务返回了无效检索结果") from exc
        if not isinstance(payload, dict):
            raise ManagedFileServiceError("文件管理服务返回了无效检索结果")
        identity = payload.get("managed_file")
        if (
            not isinstance(identity, dict)
            or identity.get("file_id") != file_id
            or not isinstance(identity.get("version_id"), str)
            or not _FILE_ID_RE.fullmatch(identity.get("version_id", ""))
            or not isinstance(identity.get("version"), str)
            or not isinstance(identity.get("source_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity.get("source_sha256", ""))
            or payload.get("version") != identity.get("version")
        ):
            raise ManagedFileServiceError("文件管理服务未返回一致的active文件身份")
        if version is not None and identity["version"] != version:
            raise ManagedFileConflict("请求版本不是当前active版本")
        evidence = payload.get("evidence")
        if not isinstance(evidence, list) or any(
            not isinstance(item, dict)
            or item.get("file_id") != file_id
            or item.get("version_id") != identity["version_id"]
            or item.get("version") != identity["version"]
            or item.get("source_sha256") != identity["source_sha256"]
            for item in evidence
        ):
            raise ManagedFileServiceError("文件管理服务返回了身份不一致的证据")
        # 管理服务内部索引名不是MCP接口；原文下载另需active版本快照校验，不能伪装成链接。
        payload.pop("collection", None)
        payload.pop("index_target_id", None)
        payload["file_id"] = file_id
        for evidence in payload["evidence"]:
            source_url = evidence.get("source_url")
            if source_url:
                scheme = urlsplit(source_url).scheme.lower() if isinstance(source_url, str) else ""
                if scheme not in {"http", "https"}:
                    evidence.pop("source_url", None)
        return payload

    def download_source(
        self, file_id: str, version_id: str, *, authorization: str | None = None
    ) -> httpx.Response:
        """按检索证据携带的不可变version_id取managed原文。"""
        managed_file_corpus_id(file_id)
        if not re.fullmatch(r"[0-9a-f]{32,64}", version_id or ""):
            raise ValueError("version_id格式无效")
        response = self._request(
            "GET",
            f"/api/mcp/files/{encode_file_id(file_id)}/source",
            authorization=authorization,
            params={"version_id": version_id},
        )
        return response


def visible_managed_files(
    bridge: ManagedFileHTTPClient,
    access_registry,
    client_id: str | None,
) -> list[ManagedFile]:
    """按本次请求的ACL快照筛选列表；策略缺失时默认拒绝。"""
    from wenjin.access import AccessDenied

    visible = []
    for item in bridge.list_files():
        try:
            require_managed_file_access(access_registry, item.file_id, client_id)
        except AccessDenied:
            continue
        visible.append(item)
    return visible


def require_managed_file_access(access_registry, file_id: str, client_id: str) -> None:
    """精确策略优先；缺精确策略才继承namespace策略，授权先于服务访问。"""
    from wenjin.access import AccessDenied

    exact_id = managed_file_corpus_id(file_id)
    policy = access_registry.policy(exact_id)
    if policy is None:
        policy = access_registry.policy("managed-files")
        if policy is None:
            raise AccessDenied("managed文件没有访问策略，默认拒绝")
    # 内部策略必须使用可信client白名单；仅显式public + * 才能匿名访问。
    if policy.visibility == "internal" and "*" in policy.allowed_clients:
        raise AccessDenied("internal managed文件策略不能使用匿名通配规则")
    access_registry.require(policy.corpus_id, client_id)


def encode_file_id(file_id: str) -> str:
    """生成管理API路径段；校验后再编码，避免把任意路径带入服务请求。"""
    managed_file_corpus_id(file_id)
    return quote(file_id, safe="")
