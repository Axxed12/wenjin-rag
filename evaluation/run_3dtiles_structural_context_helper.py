"""旧 evaluation.run_3dtiles_structural_context_helper 模块/命令的兼容入口；权威实现位于 evaluation.experiments.run_3dtiles_structural_context_helper。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.run_3dtiles_structural_context_helper import (  # noqa: E402
    CPU_PATH,
    FULL_PATH,
    MANIFEST,
    OUT_DIR,
    PAIRED,
    PILOT_PATH,
    QIDS,
    QUESTION_SHA256,
    QUESTIONS,
    ROOT,
    TARGET_ID,
    TARGETS,
    Any,
    Path,
    _candidate_match,
    _checkpoint,
    _covered,
    _fixture,
    _load_frozen_inputs,
    _rank_outcome,
    _read_verified_sources,
    _sha,
    _span_hit,
    _structures,
    _validate_inputs,
    _write_summary,
    argparse,
    hashlib,
    json,
    main,
    os,
    sys,
    time,
)

__all__ = [
    'argparse',
    'hashlib',
    'json',
    'os',
    'sys',
    'time',
    'Path',
    'Any',
    'ROOT',
    'MANIFEST',
    'PAIRED',
    'QIDS',
    'QUESTION_SHA256',
    'QUESTIONS',
    'TARGET_ID',
    'TARGETS',
    '_candidate_match',
    '_read_verified_sources',
    '_sha',
    '_validate_inputs',
    'OUT_DIR',
    'CPU_PATH',
    'PILOT_PATH',
    'FULL_PATH',
    '_checkpoint',
    '_load_frozen_inputs',
    '_structures',
    '_fixture',
    '_rank_outcome',
    '_span_hit',
    '_covered',
    '_write_summary',
    'main',
]

if __name__ == "__main__":
    main()
