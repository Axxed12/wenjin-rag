"""旧 evaluation.diagnose_helper_q017_rrf 模块/命令的兼容入口；权威实现位于 evaluation.experiments.diagnose_helper_q017_rrf。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.diagnose_helper_q017_rrf import (  # noqa: E402
    CPU_FIXTURE,
    DATASET,
    EXPECTED_DATASET_SHA,
    HELPER_PAIR,
    OUTPUT,
    PAIRED,
    Q003,
    Q005,
    Q017,
    REPORT,
    ROOT,
    TARGET_017,
    Path,
    _has_all_spans,
    _locator_hit,
    _rrf_rank,
    _self_describing_schema,
    _sha,
    hashlib,
    json,
    main,
    sys,
)

__all__ = [
    'hashlib',
    'json',
    'sys',
    'Path',
    'ROOT',
    'DATASET',
    'PAIRED',
    'HELPER_PAIR',
    'CPU_FIXTURE',
    'OUTPUT',
    'REPORT',
    'EXPECTED_DATASET_SHA',
    'Q017',
    'Q003',
    'Q005',
    'TARGET_017',
    '_sha',
    '_locator_hit',
    '_has_all_spans',
    '_self_describing_schema',
    '_rrf_rank',
    'main',
]

if __name__ == "__main__":
    main()
