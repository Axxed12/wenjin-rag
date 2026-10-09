"""兼容入口；权威实现位于 scripts.labs.format_table_chunks。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.format_table_chunks import (  # noqa: E402
    INPUT,
    OUTPUT,
    ROOT,
    Path,
    json,
    main,
    records_to_chunks,
)

__all__ = [
    "INPUT",
    "OUTPUT",
    "Path",
    "ROOT",
    "json",
    "main",
    "records_to_chunks",
]

if __name__ == "__main__":
    main()
