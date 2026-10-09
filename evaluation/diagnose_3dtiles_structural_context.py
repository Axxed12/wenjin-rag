"""旧 evaluation.diagnose_3dtiles_structural_context 模块/命令的兼容入口；权威实现位于 evaluation.experiments.diagnose_3dtiles_structural_context。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.diagnose_3dtiles_structural_context import (  # noqa: E402
    MANIFEST,
    OUTPUT,
    PAIRED,
    QIDS,
    QUESTION_SHA256,
    QUESTIONS,
    REPORT,
    ROOT,
    TARGET_ID,
    TARGETS,
    Any,
    Path,
    _build_result_fixture,
    _candidate_match,
    _document,
    _html_structure,
    _html_visible,
    _metadata,
    _pdf_structure,
    _rank_targets,
    _read_verified_sources,
    _save,
    _selected,
    _sha,
    _validate_inputs,
    _write_report,
    hashlib,
    json,
    main,
    os,
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
    'PAIRED',
    'QUESTIONS',
    'MANIFEST',
    'OUTPUT',
    'REPORT',
    'QUESTION_SHA256',
    'QIDS',
    'TARGET_ID',
    'TARGETS',
    '_sha',
    '_html_visible',
    '_read_verified_sources',
    '_html_structure',
    '_pdf_structure',
    '_metadata',
    '_document',
    '_candidate_match',
    '_validate_inputs',
    '_build_result_fixture',
    '_save',
    '_rank_targets',
    '_selected',
    '_write_report',
    'main',
]

if __name__ == "__main__":
    main()
