"""兼容入口；权威实现位于 scripts.operations.test_mcp_client。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.test_mcp_client import (  # noqa: E402
    Client,
    Path,
    StreamableHttpTransport,
    argparse,
    asyncio,
    json,
    main,
    run,
)

__all__ = [
    "Client",
    "Path",
    "StreamableHttpTransport",
    "argparse",
    "asyncio",
    "json",
    "main",
    "run",
]

if __name__ == "__main__":
    main()
