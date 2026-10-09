"""兼容入口；权威实现位于 scripts.operations.audit_external_corpus。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.audit_external_corpus import (  # noqa: E402
    ExitStack,
    Path,
    SequenceMatcher,
    argparse,
    audit,
    defaultdict,
    hashes,
    json,
    main,
    normalized,
    pymupdf,
    safe_path,
    sha256_hex,
    tarfile,
    zipfile,
)

__all__ = [
    "ExitStack",
    "Path",
    "SequenceMatcher",
    "argparse",
    "audit",
    "defaultdict",
    "hashes",
    "json",
    "main",
    "normalized",
    "pymupdf",
    "safe_path",
    "sha256_hex",
    "tarfile",
    "zipfile",
]

if __name__ == "__main__":
    main()
