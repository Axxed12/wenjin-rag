"""旧 evaluation.compare_3dtiles_candidate_depth 模块/命令的兼容入口；权威实现位于 evaluation.experiments.compare_3dtiles_candidate_depth。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.compare_3dtiles_candidate_depth import (  # noqa: E402
    BASE_ROUTE,
    OUTPUT,
    PAIRED,
    QUESTION_IDS,
    QUESTION_SHA256,
    QUESTIONS,
    REPORT,
    ROOT,
    SUPPLEMENT,
    TARGETS,
    Any,
    Path,
    _matches,
    _run,
    _save_checkpoint,
    _selected,
    _sha,
    _stage_target_ranks,
    _write_report,
    assemble_condition,
    hashlib,
    json,
    main,
    os,
    preflight,
    saved_pair_summary,
    sys,
    time,
)

__all__ = [
    'hashlib',
    'json',
    'os',
    'sys',
    'time',
    'Path',
    'Any',
    'ROOT',
    'SUPPLEMENT',
    'PAIRED',
    'QUESTIONS',
    'QUESTION_SHA256',
    'BASE_ROUTE',
    'OUTPUT',
    'REPORT',
    'QUESTION_IDS',
    'TARGETS',
    '_sha',
    '_matches',
    'preflight',
    '_stage_target_ranks',
    '_selected',
    'assemble_condition',
    'saved_pair_summary',
    '_save_checkpoint',
    '_run',
    '_write_report',
    'main',
]

if __name__ == "__main__":
    main()
