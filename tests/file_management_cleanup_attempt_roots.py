"""旧 tests.file_management_cleanup_attempt_roots 路径兼容入口；权威实现位于 tests.support.file_management_cleanup_attempt_roots。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.support.file_management_cleanup_attempt_roots import (  # noqa: E402
    ROOTS,
    Path,
    QdrantClient,
    Settings,
    datetime,
    free_port,
    httpx,
    json,
    main,
    os,
    re,
    socket,
    subprocess,
    sys,
    time,
    timezone,
)

__all__ = [
    'json',
    'os',
    're',
    'socket',
    'subprocess',
    'sys',
    'time',
    'datetime',
    'timezone',
    'Path',
    'httpx',
    'QdrantClient',
    'Settings',
    'ROOTS',
    'free_port',
    'main',
]

if __name__ == "__main__":
    raise SystemExit(main())
