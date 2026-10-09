"""兼容入口；权威实现位于 scripts.labs.mark_parser_baselines。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.mark_parser_baselines import (  # noqa: E402
    ROOT,
    VALIDATION,
    Path,
    json,
    main,
    parser_for_path,
)

__all__ = [
    "Path",
    "ROOT",
    "VALIDATION",
    "json",
    "main",
    "parser_for_path",
]

if __name__ == "__main__":
    main()
