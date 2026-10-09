"""旧 tests.file_management_http_acceptance 路径兼容入口；权威实现位于 tests.acceptance.file_management_http_acceptance。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.acceptance.file_management_http_acceptance import (  # noqa: E402
    Acceptance,
    Path,
    QdrantClient,
    Settings,
    ThreadPoolExecutor,
    argparse,
    datetime,
    fitz,
    hashlib,
    httpx,
    json,
    main,
    os,
    quote,
    socket,
    subprocess,
    sys,
    time,
    timezone,
)

__all__ = [
    'argparse',
    'hashlib',
    'json',
    'os',
    'socket',
    'subprocess',
    'sys',
    'time',
    'ThreadPoolExecutor',
    'datetime',
    'timezone',
    'Path',
    'quote',
    'httpx',
    'fitz',
    'QdrantClient',
    'Settings',
    'Acceptance',
    'main',
]

if __name__ == "__main__":
    main()
