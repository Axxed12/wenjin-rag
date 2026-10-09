"""兼容入口；权威实现位于 scripts.operations.prepare_corpus_import。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.prepare_corpus_import import (  # noqa: E402
    ARTIFACTS,
    PROJ_COMMIT,
    PROJECT,
    Path,
    argparse,
    copy,
    dump,
    hashes,
    json,
    main,
    manifest_content_fingerprint,
    prepare,
    safe_path,
    sha256_hex,
    tarfile,
)

__all__ = [
    "ARTIFACTS",
    "PROJECT",
    "PROJ_COMMIT",
    "Path",
    "argparse",
    "copy",
    "dump",
    "hashes",
    "json",
    "main",
    "manifest_content_fingerprint",
    "prepare",
    "safe_path",
    "sha256_hex",
    "tarfile",
]

if __name__ == "__main__":
    main()
