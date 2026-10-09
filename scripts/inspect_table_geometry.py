"""兼容入口；权威实现位于 scripts.labs.inspect_table_geometry。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.inspect_table_geometry import (  # noqa: E402
    CONFIGS,
    PDF_PATH,
    Path,
    main,
    pdfplumber,
    show_table,
)

__all__ = [
    "CONFIGS",
    "PDF_PATH",
    "Path",
    "main",
    "pdfplumber",
    "show_table",
]

if __name__ == "__main__":
    main()
