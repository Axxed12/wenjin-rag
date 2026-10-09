"""兼容入口；权威实现位于 scripts.labs.run_mineru_lab。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.run_mineru_lab import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    hashlib,
    httpx,
    json,
    main,
    time,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "hashlib",
    "httpx",
    "json",
    "main",
    "time",
]

if __name__ == "__main__":
    main()
