"""兼容入口；权威实现位于 scripts.labs.measure_long_line_evidence。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.measure_long_line_evidence import (  # noqa: E402
    CONFIG,
    MAX_CHARS,
    ROOT,
    Counter,
    Path,
    hashlib,
    json,
    load_retrieval_documents,
    main,
    quote,
    sqlite3,
    time,
)

__all__ = [
    "CONFIG",
    "Counter",
    "MAX_CHARS",
    "Path",
    "ROOT",
    "hashlib",
    "json",
    "load_retrieval_documents",
    "main",
    "quote",
    "sqlite3",
    "time",
]

if __name__ == "__main__":
    main()
