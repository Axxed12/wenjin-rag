"""兼容入口；权威实现位于 scripts.operations.fetch_corpus。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.fetch_corpus import (  # noqa: E402
    DEFAULT_MANIFEST,
    ROOT,
    Path,
    argparse,
    download_bytes,
    github_raw_url,
    hashlib,
    json,
    main,
    materialize,
    resolve_raw_root,
    sys,
    time,
    urllib,
)

__all__ = [
    "DEFAULT_MANIFEST",
    "Path",
    "ROOT",
    "argparse",
    "download_bytes",
    "github_raw_url",
    "hashlib",
    "json",
    "main",
    "materialize",
    "resolve_raw_root",
    "sys",
    "time",
    "urllib",
]

if __name__ == "__main__":
    main()
