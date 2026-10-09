"""旧 evaluation.evaluate_evidence_judge 模块/命令的兼容入口；权威实现位于 evaluation.quality.evaluate_evidence_judge。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.evaluate_evidence_judge import (  # noqa: E402
    ROOT,
    Path,
    json,
    main,
    sys,
    time,
)

__all__ = [
    'json',
    'sys',
    'time',
    'Path',
    'ROOT',
    'main',
]

if __name__ == "__main__":
    main()
