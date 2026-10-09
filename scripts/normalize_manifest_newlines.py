"""兼容入口；权威实现位于 scripts.operations.normalize_manifest_newlines。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.normalize_manifest_newlines import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    hashlib,
    json,
    main,
    manifest_content_fingerprint,
    normalize,
    plan_manifests,
    reference_files,
    rewrite_references,
    text_bytes,
    verify_references,
    write_manifests,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "hashlib",
    "json",
    "main",
    "manifest_content_fingerprint",
    "normalize",
    "plan_manifests",
    "reference_files",
    "rewrite_references",
    "text_bytes",
    "verify_references",
    "write_manifests",
]

if __name__ == "__main__":
    main()
