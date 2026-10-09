"""兼容入口；权威实现位于 scripts.labs.run_parse_lab。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.run_parse_lab import (  # noqa: E402
    OUTPUT,
    REVISION,
    ROOT,
    SAMPLES,
    SOURCE,
    Path,
    asdict,
    hashlib,
    importlib,
    json,
    main,
    parse_pdf,
    pymupdf,
    time,
    write_json,
)

__all__ = [
    "OUTPUT",
    "Path",
    "REVISION",
    "ROOT",
    "SAMPLES",
    "SOURCE",
    "asdict",
    "hashlib",
    "importlib",
    "json",
    "main",
    "parse_pdf",
    "pymupdf",
    "time",
    "write_json",
]

if __name__ == "__main__":
    main()
