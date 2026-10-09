"""兼容入口；权威实现位于 scripts.operations.import_verified_corpus。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.import_verified_corpus import (  # noqa: E402
    Path,
    argparse,
    import_package,
    json,
    main,
    safe_path,
    sha256_hex,
    shutil,
    verify,
    verify_manifest_hashes,
)

__all__ = [
    "Path",
    "argparse",
    "import_package",
    "json",
    "main",
    "safe_path",
    "sha256_hex",
    "shutil",
    "verify",
    "verify_manifest_hashes",
]

if __name__ == "__main__":
    main()
