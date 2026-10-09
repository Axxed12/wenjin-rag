"""兼容入口；权威实现位于 scripts.operations.verify_corpus_hashes。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.verify_corpus_hashes import (  # noqa: E402
    DEFAULT_MANIFEST,
    Path,
    argparse,
    json,
    main,
    verify_manifest_hashes,
)

__all__ = [
    "DEFAULT_MANIFEST",
    "Path",
    "argparse",
    "json",
    "main",
    "verify_manifest_hashes",
]

if __name__ == "__main__":
    main()
