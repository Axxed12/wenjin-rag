"""旧 tests.cleanup_managed_mcp_acceptance 路径兼容入口；权威实现位于 tests.support.cleanup_managed_mcp_acceptance。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.support.cleanup_managed_mcp_acceptance import (  # noqa: E402
    HELPER,
    REPO,
    Path,
    QdrantClient,
    Settings,
    argparse,
    create_qdrant_client,
    datetime,
    free_port,
    httpx,
    json,
    main,
    quote,
    secrets,
    socket,
    subprocess,
    sys,
    time,
    timezone,
)

__all__ = [
    'argparse',
    'json',
    'secrets',
    'socket',
    'subprocess',
    'sys',
    'time',
    'datetime',
    'timezone',
    'Path',
    'quote',
    'httpx',
    'QdrantClient',
    'create_qdrant_client',
    'Settings',
    'REPO',
    'HELPER',
    'free_port',
    'main',
]

if __name__ == "__main__":
    raise SystemExit(main())
