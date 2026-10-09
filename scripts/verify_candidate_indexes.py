"""兼容入口；权威实现位于 scripts.operations.verify_candidate_indexes。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.verify_candidate_indexes import (  # noqa: E402
    ROOT,
    IndexTarget,
    Path,
    ResponseHandlingException,
    argparse,
    audit_index,
    create_qdrant_client,
    json,
    main,
    sys,
)

__all__ = [
    "IndexTarget",
    "Path",
    "ROOT",
    "ResponseHandlingException",
    "argparse",
    "audit_index",
    "create_qdrant_client",
    "json",
    "main",
    "sys",
]

if __name__ == "__main__":
    main()
