"""旧 evaluation.evaluate_evidence_threshold 模块/命令的兼容入口；权威实现位于 evaluation.quality.evaluate_evidence_threshold。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.evaluate_evidence_threshold import (  # noqa: E402
    ROOT,
    Path,
    candidate_thresholds,
    classification_at_threshold,
    json,
    main,
    sys,
)

__all__ = [
    'json',
    'sys',
    'Path',
    'ROOT',
    'classification_at_threshold',
    'candidate_thresholds',
    'main',
]

if __name__ == "__main__":
    main()
