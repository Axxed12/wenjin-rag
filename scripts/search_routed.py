"""兼容入口；权威实现位于 scripts.operations.search_routed。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.search_routed import (  # noqa: E402
    DEFAULT_MULTI_CORPUS_ROUTE,
    ROOT,
    Path,
    argparse,
    build_parser,
    json,
    main,
    os,
    sys,
)

__all__ = [
    "DEFAULT_MULTI_CORPUS_ROUTE",
    "Path",
    "ROOT",
    "argparse",
    "build_parser",
    "json",
    "main",
    "os",
    "sys",
]

if __name__ == "__main__":
    main()
