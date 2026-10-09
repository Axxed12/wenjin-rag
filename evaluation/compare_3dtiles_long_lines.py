"""旧 evaluation.compare_3dtiles_long_lines 模块/命令的兼容入口；权威实现位于 evaluation.experiments.compare_3dtiles_long_lines。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.compare_3dtiles_long_lines import (  # noqa: E402
    ARTIFACTS,
    DATASET,
    DATASET_SHA,
    INDEPENDENT_REVIEW,
    NEW_TARGET,
    OLD_ROUTE,
    RESULTS,
    ROOT,
    SOURCE_REVIEW,
    SUPPLEMENT,
    Any,
    Lock,
    Path,
    _arm_result,
    _checkpoint_report,
    _load_reviewed_questions,
    _load_source_line_map,
    _resume_checkpoint,
    _sha256,
    hashlib,
    json,
    main,
    make_question_record,
    os,
    sys,
    time,
    validate_paired_routes,
)

__all__ = [
    'hashlib',
    'json',
    'os',
    'sys',
    'time',
    'Path',
    'Lock',
    'Any',
    'ROOT',
    '_arm_result',
    '_load_source_line_map',
    'make_question_record',
    'validate_paired_routes',
    'ARTIFACTS',
    'SUPPLEMENT',
    'DATASET',
    'DATASET_SHA',
    'SOURCE_REVIEW',
    'INDEPENDENT_REVIEW',
    'OLD_ROUTE',
    'NEW_TARGET',
    'RESULTS',
    '_sha256',
    '_load_reviewed_questions',
    '_checkpoint_report',
    '_resume_checkpoint',
    'main',
]

if __name__ == "__main__":
    main()
