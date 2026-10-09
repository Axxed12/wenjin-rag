"""兼容入口；权威实现位于 scripts.operations.manage_indexes。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.manage_indexes import (  # noqa: E402
    REGISTRY_PATH,
    ROOT,
    Path,
    argparse,
    json,
    main,
    sys,
    validate_with_client,
)

__all__ = [
    "Path",
    "REGISTRY_PATH",
    "ROOT",
    "argparse",
    "json",
    "main",
    "sys",
    "validate_with_client",
]

if __name__ == "__main__":
    main()
