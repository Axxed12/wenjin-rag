"""兼容入口：受管文件 MCP HTTP 客户端位于 wenjin.mcp.managed_client。"""

from wenjin.mcp.managed_client import (
    DEFAULT_MANAGEMENT_URL,
    ManagedFile,
    ManagedFileConflict,
    ManagedFileHTTPClient,
    ManagedFileServiceError,
    managed_file_corpus_id,
    require_managed_file_access,
    visible_managed_files,
)

__all__ = [
    "DEFAULT_MANAGEMENT_URL",
    "ManagedFile",
    "ManagedFileConflict",
    "ManagedFileHTTPClient",
    "ManagedFileServiceError",
    "managed_file_corpus_id",
    "require_managed_file_access",
    "visible_managed_files",
]