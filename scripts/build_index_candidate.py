"""兼容入口；权威实现位于 scripts.operations.build_index_candidate。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.build_index_candidate import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    asdict,
    json,
    main,
    sys,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "asdict",
    "json",
    "main",
    "sys",
]

if __name__ == "__main__":
    main()
