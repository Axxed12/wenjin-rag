"""兼容入口：MCP 实现位于 wenjin.mcp 包。"""

from wenjin.mcp.download import (
    MAX_CONCURRENT_DOWNLOADS,
    DownloadSlot,
    SlotHoldingResponse,
    serve_download,
)
from wenjin.mcp.runtime import (
    EXPECTED_PROTOCOL_VERSION,
    ROOT,
    RuntimeIdentity,
    create_default_service,
    create_router,
    create_runtime,
    identity_from_registry,
)
from wenjin.mcp.server import build_mcp_server, main
from wenjin.mcp.tools import client_identity, log_identity_problems, policy_unavailable

__all__ = [
    "EXPECTED_PROTOCOL_VERSION",
    "ROOT",
    "RuntimeIdentity",
    "create_default_service",
    "create_router",
    "create_runtime",
    "identity_from_registry",
    "build_mcp_server",
    "main",
    "client_identity",
    "log_identity_problems",
    "policy_unavailable",
    "MAX_CONCURRENT_DOWNLOADS",
    "DownloadSlot",
    "SlotHoldingResponse",
    "serve_download",
]

if __name__ == "__main__":
    main()