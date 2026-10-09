"""旧 evaluation.inspect_misses 模块/命令的兼容入口；权威实现位于 evaluation.experiments.inspect_misses。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.inspect_misses import (  # noqa: E402
    ROOT,
    Path,
    evidence_in_corpus,
    json,
    main,
    sys,
)

__all__ = [
    'json',
    'sys',
    'Path',
    'ROOT',
    'evidence_in_corpus',
    'main',
]

if __name__ == "__main__":
    main()
