"""旧 tests.file_management_acceptance_server 路径兼容入口；权威实现位于 tests.acceptance.file_management_acceptance_server。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.acceptance.file_management_acceptance_server import (  # noqa: E402
    FileManagementService,
    Path,
    Settings,
    _fault_hook,
    _LazyEmbedder,
    argparse,
    create_app,
    create_qdrant_client,
    json,
    main,
    time,
    uvicorn,
)

__all__ = [
    'argparse',
    'json',
    'time',
    'Path',
    'uvicorn',
    'FileManagementService',
    '_LazyEmbedder',
    'create_app',
    'create_qdrant_client',
    'Settings',
    '_fault_hook',
    'main',
]

if __name__ == "__main__":
    main()
