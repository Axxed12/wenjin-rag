"""兼容入口；权威实现位于 scripts.demos.query_hybrid。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.demos.query_hybrid import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    format_results,
    json,
    main,
    qdrant_points_to_dicts,
    sys,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "format_results",
    "json",
    "main",
    "qdrant_points_to_dicts",
    "sys",
]

if __name__ == "__main__":
    main()
