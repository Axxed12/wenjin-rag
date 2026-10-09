"""兼容入口；权威实现位于 scripts.demos.query_dense_index。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.demos.query_dense_index import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    format_human_result,
    json,
    main,
    sys,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "format_human_result",
    "json",
    "main",
    "sys",
]

if __name__ == "__main__":
    main()
