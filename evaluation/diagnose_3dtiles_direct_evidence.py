"""旧 evaluation.diagnose_3dtiles_direct_evidence 模块/命令的兼容入口；权威实现位于 evaluation.experiments.diagnose_3dtiles_direct_evidence。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.diagnose_3dtiles_direct_evidence import (  # noqa: E402
    DIRECT_EVIDENCE_INSTRUCTION,
    FROZEN_QUESTION_PATH,
    FROZEN_QUESTION_SHA256,
    OUTPUT,
    QUESTION_IDS,
    REPORT,
    RESULTS,
    ROOT,
    TARGETS,
    Any,
    Path,
    _candidate_matches,
    _rank_summary,
    _selected_summary,
    _sha256_text,
    _write_outputs,
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
    'RESULTS',
    'OUTPUT',
    'REPORT',
    'FROZEN_QUESTION_PATH',
    'FROZEN_QUESTION_SHA256',
    'DIRECT_EVIDENCE_INSTRUCTION',
    'QUESTION_IDS',
    'TARGETS',
    '_sha256_text',
    '_candidate_matches',
    '_rank_summary',
    '_selected_summary',
    '_write_outputs',
    'main',
]

if __name__ == "__main__":
    main()
