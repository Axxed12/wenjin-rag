"""旧 evaluation.diagnose_3dtiles_candidate_recall 模块/命令的兼容入口；权威实现位于 evaluation.experiments.diagnose_3dtiles_candidate_recall。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.experiments.diagnose_3dtiles_candidate_recall import (  # noqa: E402
    BM25_INDEX,
    OUTPUT,
    PAIRED,
    QUESTION_SHA256,
    QUESTIONS,
    REPORT,
    ROOT,
    TARGETS,
    Path,
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
    'PAIRED',
    'QUESTIONS',
    'QUESTION_SHA256',
    'BM25_INDEX',
    'OUTPUT',
    'REPORT',
    'TARGETS',
    'main',
]

if __name__ == "__main__":
    main()
