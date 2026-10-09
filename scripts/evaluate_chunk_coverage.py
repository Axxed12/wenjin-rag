"""兼容入口；权威实现位于 scripts.labs.evaluate_chunk_coverage。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.evaluate_chunk_coverage import (  # noqa: E402
    LINE_BASIS,
    MANIFEST,
    OUTPUT,
    ROOT,
    SEED_PATH,
    TEXT_BASIS,
    Path,
    covered,
    evaluate,
    evidence_coverage,
    json,
    main,
    sys,
)

__all__ = [
    "LINE_BASIS",
    "MANIFEST",
    "OUTPUT",
    "Path",
    "ROOT",
    "SEED_PATH",
    "TEXT_BASIS",
    "covered",
    "evaluate",
    "evidence_coverage",
    "json",
    "main",
    "sys",
]

if __name__ == "__main__":
    main()
