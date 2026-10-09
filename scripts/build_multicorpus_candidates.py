"""兼容入口；权威实现位于 scripts.operations.build_multicorpus_candidates。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.build_multicorpus_candidates import (  # noqa: E402
    CORPORA,
    ROOT,
    Path,
    Settings,
    argparse,
    asdict,
    build_candidate_index,
    create_qdrant_client,
    json,
    load_target,
    main,
    safe_name,
    validate_target,
)

__all__ = [
    "CORPORA",
    "Path",
    "ROOT",
    "Settings",
    "argparse",
    "asdict",
    "build_candidate_index",
    "create_qdrant_client",
    "json",
    "load_target",
    "main",
    "safe_name",
    "validate_target",
]

if __name__ == "__main__":
    main()
