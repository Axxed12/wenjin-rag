"""兼容入口；权威实现位于 scripts.operations.build_bm25_index。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.build_bm25_index import (  # noqa: E402
    DEFAULT_TARGET,
    ROOT,
    Path,
    argparse,
    json,
    main,
    os,
    sys,
    time,
)

__all__ = [
    "DEFAULT_TARGET",
    "Path",
    "ROOT",
    "argparse",
    "json",
    "main",
    "os",
    "sys",
    "time",
]

if __name__ == "__main__":
    main()
