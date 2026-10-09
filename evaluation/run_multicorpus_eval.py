"""旧 evaluation.run_multicorpus_eval 模块/命令的兼容入口；权威实现位于 evaluation.quality.run_multicorpus_eval。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.run_multicorpus_eval import (  # noqa: E402
    LEADING_VERSION,
    ROOT,
    Path,
    argparse,
    blank_line_map,
    chunks_required,
    closing,
    evidence_libraries,
    evidence_metrics,
    json,
    main,
    metrics,
    normalize_library,
    os,
    page_span_hit,
    re,
    route_arguments,
    sqlite3,
    statistics,
    summarize,
    sys,
    time,
)

__all__ = [
    'argparse',
    'json',
    'os',
    're',
    'sqlite3',
    'statistics',
    'sys',
    'time',
    'closing',
    'Path',
    'ROOT',
    'metrics',
    'summarize',
    'normalize_library',
    'LEADING_VERSION',
    'route_arguments',
    'evidence_libraries',
    'chunks_required',
    'page_span_hit',
    'evidence_metrics',
    'blank_line_map',
    'main',
]

if __name__ == "__main__":
    main()
