"""兼容入口；权威实现位于 scripts.labs.evaluate_multiformat_chunking。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.labs.evaluate_multiformat_chunking import (  # noqa: E402
    DEFAULT_OUTPUT,
    HISTOGRAM_BUCKETS,
    MAX_CHARS,
    METRIC_SEMANTICS,
    PAGE_BASIS,
    ROOT,
    TEXT_BASIS,
    Counter,
    Path,
    argparse,
    block_chunks,
    evaluate_strategy,
    histogram,
    json,
    length_stats,
    load_retrieval_documents,
    main,
    normalize,
    re,
    statistics,
)

__all__ = [
    "Counter",
    "DEFAULT_OUTPUT",
    "HISTOGRAM_BUCKETS",
    "MAX_CHARS",
    "METRIC_SEMANTICS",
    "PAGE_BASIS",
    "Path",
    "ROOT",
    "TEXT_BASIS",
    "argparse",
    "block_chunks",
    "evaluate_strategy",
    "histogram",
    "json",
    "length_stats",
    "load_retrieval_documents",
    "main",
    "normalize",
    "re",
    "statistics",
]

if __name__ == "__main__":
    main()
