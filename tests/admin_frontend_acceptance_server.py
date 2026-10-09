"""旧 tests.admin_frontend_acceptance_server 路径兼容入口；权威实现位于 tests.acceptance.admin_frontend_acceptance_server。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.acceptance.admin_frontend_acceptance_server import (  # noqa: E402
    FileManagementService,
    MarkerEmbedder,
    Path,
    QdrantClient,
    argparse,
    create_app,
    hashlib,
    main,
    uvicorn,
    validate_listen_security,
)

__all__ = [
    'argparse',
    'hashlib',
    'Path',
    'uvicorn',
    'QdrantClient',
    'FileManagementService',
    'create_app',
    'validate_listen_security',
    'MarkerEmbedder',
    'main',
]

if __name__ == "__main__":
    main()
