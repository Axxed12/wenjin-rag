"""旧 evaluation.run_answer_relevancy_eval 模块/命令的兼容入口；权威实现位于 evaluation.quality.run_answer_relevancy_eval。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.run_answer_relevancy_eval import (  # noqa: E402
    ANSWER_PROMPT,
    CONTROL_RESPONSE,
    DEFAULT_DATASET,
    DEFAULT_OUTPUT,
    DEFAULT_RETRIEVAL,
    DEFAULT_ROUTE,
    DEFAULT_SOURCE,
    ROOT,
    Path,
    argparse,
    asyncio,
    bm25_paths,
    build_embeddings,
    build_metric,
    build_samples,
    compare,
    describe,
    json,
    load_env,
    load_saved_answers,
    main,
    os,
    run_pass,
    statistics,
    sys,
    time,
    verdict_of,
)

__all__ = [
    'argparse',
    'asyncio',
    'json',
    'os',
    'statistics',
    'sys',
    'time',
    'Path',
    'ROOT',
    'ANSWER_PROMPT',
    'DEFAULT_DATASET',
    'DEFAULT_RETRIEVAL',
    'DEFAULT_ROUTE',
    'bm25_paths',
    'build_samples',
    'load_env',
    'DEFAULT_SOURCE',
    'DEFAULT_OUTPUT',
    'CONTROL_RESPONSE',
    'load_saved_answers',
    'describe',
    'build_embeddings',
    'build_metric',
    'run_pass',
    'compare',
    'verdict_of',
    'main',
]

if __name__ == "__main__":
    sys.exit(main())
