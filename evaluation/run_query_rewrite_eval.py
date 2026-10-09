"""旧 evaluation.run_query_rewrite_eval 模块/命令的兼容入口；权威实现位于 evaluation.quality.run_query_rewrite_eval。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.run_query_rewrite_eval import (  # noqa: E402
    CORPUS_HINT,
    ROOT,
    Path,
    argparse,
    fixture_rewrite,
    json,
    load_jsonl,
    main,
    retrieve_one_query,
    sys,
    time,
)

__all__ = [
    'argparse',
    'json',
    'sys',
    'time',
    'Path',
    'ROOT',
    'CORPUS_HINT',
    'load_jsonl',
    'retrieve_one_query',
    'fixture_rewrite',
    'main',
]

if __name__ == "__main__":
    main()
