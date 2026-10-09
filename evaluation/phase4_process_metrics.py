"""旧 evaluation.phase4_process_metrics 路径兼容入口；权威实现位于 tests.support.phase4_process_metrics。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.support.phase4_process_metrics import (  # noqa: E402
    Any,
    _loaded_torch_cuda_memory,
    _windows_process_memory,
    collect_process_metrics,
    math,
    os,
    platform,
    sys,
    time,
)

__all__ = [
    'math',
    'os',
    'platform',
    'sys',
    'time',
    'Any',
    '_windows_process_memory',
    '_loaded_torch_cuda_memory',
    'collect_process_metrics',
]
