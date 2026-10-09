"""兼容入口；权威实现位于 scripts.demos.query_bm25_index。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.demos.query_bm25_index import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    bm25_query,
    format_human_result,
    json,
    main,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "bm25_query",
    "format_human_result",
    "json",
    "main",
]

if __name__ == "__main__":
    main()
