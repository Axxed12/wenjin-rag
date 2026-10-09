"""旧 evaluation.run_retrieval_eval 模块/命令的兼容入口；权威实现位于 evaluation.quality.run_retrieval_eval。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.run_retrieval_eval import (  # noqa: E402
    ROOT,
    Path,
    covers_span,
    json,
    main,
    metrics,
    overlaps,
    summarize,
    sys,
    time,
)

__all__ = [
    'json',
    'sys',
    'time',
    'Path',
    'ROOT',
    'overlaps',
    'covers_span',
    'metrics',
    'summarize',
    'main',
]

if __name__ == "__main__":
    main()
