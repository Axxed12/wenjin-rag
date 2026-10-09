"""旧 evaluation.run_frozen_eval 模块/命令的兼容入口；权威实现位于 evaluation.quality.run_frozen_eval。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.run_frozen_eval import (  # noqa: E402
    DATASET,
    ROOT,
    Path,
    argparse,
    gc,
    hashlib,
    json,
    load_and_validate_dataset,
    main,
    os,
    sys,
    time,
)

__all__ = [
    'argparse',
    'gc',
    'hashlib',
    'json',
    'os',
    'sys',
    'time',
    'Path',
    'ROOT',
    'DATASET',
    'load_and_validate_dataset',
    'main',
]

if __name__ == "__main__":
    main()
