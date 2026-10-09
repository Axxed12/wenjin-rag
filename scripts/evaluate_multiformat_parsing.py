"""兼容入口；权威实现位于 scripts.labs.evaluate_multiformat_parsing。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.evaluate_multiformat_parsing import (  # noqa: E402
    Counter,
    Path,
    argparse,
    evaluate,
    json,
    main,
    normalized,
    parse_file,
)

__all__ = [
    "Counter",
    "Path",
    "argparse",
    "evaluate",
    "json",
    "main",
    "normalized",
    "parse_file",
]

if __name__ == "__main__":
    main()
