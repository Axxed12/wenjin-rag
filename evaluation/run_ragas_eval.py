"""旧 evaluation.run_ragas_eval 模块/命令的兼容入口；权威实现位于 evaluation.quality.run_ragas_eval。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.run_ragas_eval import (  # noqa: E402
    ANSWER_PROMPT,
    DEFAULT_DATASET,
    DEFAULT_OUTPUT,
    DEFAULT_RETRIEVAL,
    DEFAULT_ROUTE,
    ROOT,
    Path,
    answer_model_config,
    argparse,
    asyncio,
    bm25_paths,
    build_samples,
    chunk_texts,
    closing,
    generate_answers,
    json,
    library_of,
    load_env,
    main,
    normalize_library,
    os,
    re,
    score,
    sqlite3,
    summarize,
    sys,
    time,
)

__all__ = [
    'argparse',
    'asyncio',
    'json',
    'os',
    're',
    'sqlite3',
    'sys',
    'time',
    'closing',
    'Path',
    'ROOT',
    'DEFAULT_RETRIEVAL',
    'DEFAULT_DATASET',
    'DEFAULT_ROUTE',
    'DEFAULT_OUTPUT',
    'ANSWER_PROMPT',
    'load_env',
    'normalize_library',
    'bm25_paths',
    'library_of',
    'chunk_texts',
    'build_samples',
    'answer_model_config',
    'generate_answers',
    'score',
    'summarize',
    'main',
]

if __name__ == "__main__":
    sys.exit(main())
