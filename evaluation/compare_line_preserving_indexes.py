"""旧 evaluation.compare_line_preserving_indexes 模块/命令的兼容入口；权威实现位于 evaluation.experiments.compare_line_preserving_indexes。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.compare_line_preserving_indexes import (  # noqa: E402
    _EXPECTED_VALIDATION_SHA256,
    _VERSION_PREFIX,
    ROOT,
    Any,
    Path,
    _annotate_candidates,
    _arm_result,
    _context_drops,
    _load_source_line_map,
    _route_arguments,
    _scope_metrics,
    _sha256_file,
    contract_metrics,
    hashlib,
    json,
    locator_metrics,
    main,
    make_question_record,
    perf_counter,
    re,
    sys,
    validate_candidate_location,
    validate_paired_routes,
    validate_validation_dataset,
)

__all__ = [
    'hashlib',
    'json',
    're',
    'sys',
    'Path',
    'perf_counter',
    'Any',
    'ROOT',
    '_VERSION_PREFIX',
    '_EXPECTED_VALIDATION_SHA256',
    'locator_metrics',
    '_sha256_file',
    'validate_validation_dataset',
    'validate_paired_routes',
    'validate_candidate_location',
    'contract_metrics',
    'make_question_record',
    '_route_arguments',
    '_load_source_line_map',
    '_annotate_candidates',
    '_context_drops',
    '_scope_metrics',
    '_arm_result',
    'main',
]

if __name__ == "__main__":
    main()
