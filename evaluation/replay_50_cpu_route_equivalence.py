"""旧 evaluation.replay_50_cpu_route_equivalence 模块/命令的兼容入口；权威实现位于 evaluation.experiments.replay_50_cpu_route_equivalence。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.replay_50_cpu_route_equivalence import (  # noqa: E402
    BASELINE_RESULT,
    DATASET,
    DATASET_SHA_FILE,
    EXPECTED_SHA,
    OUTPUT,
    REPORT,
    ROOT,
    ROUTE_002,
    ROUTE_003,
    Any,
    Path,
    _load_fixed_dataset,
    _main,
    _route_identity,
    _routes,
    _source_chunks,
    hashlib,
    json,
    sys,
)

__all__ = [
    'hashlib',
    'json',
    'sys',
    'Path',
    'Any',
    'ROOT',
    'DATASET',
    'DATASET_SHA_FILE',
    'BASELINE_RESULT',
    'ROUTE_002',
    'ROUTE_003',
    'OUTPUT',
    'REPORT',
    'EXPECTED_SHA',
    '_load_fixed_dataset',
    '_routes',
    '_route_identity',
    '_source_chunks',
    '_main',
]

if __name__ == "__main__":
    _main()
