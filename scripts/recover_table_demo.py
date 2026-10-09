"""兼容入口；权威实现位于 scripts.labs.recover_table_demo。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.recover_table_demo import (  # noqa: E402
    COLUMNS,
    OUTPUT_PATH,
    PDF_PATH,
    ROOT,
    Path,
    group_words_into_rows,
    json,
    main,
    pdfplumber,
    recover_first_five,
    words_in_column,
)

__all__ = [
    "COLUMNS",
    "OUTPUT_PATH",
    "PDF_PATH",
    "Path",
    "ROOT",
    "group_words_into_rows",
    "json",
    "main",
    "pdfplumber",
    "recover_first_five",
    "words_in_column",
]

if __name__ == "__main__":
    main()
