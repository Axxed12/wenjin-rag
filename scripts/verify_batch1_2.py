"""兼容入口；权威实现位于 scripts.operations.verify_batch1_2。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.verify_batch1_2 import (  # noqa: E402
    CHUNK_PATH_CASES,
    OUTPUT,
    ROOT,
    CorpusSources,
    IndexRegistryStore,
    Path,
    SourceModified,
    SourceNotFound,
    SourceTooLarge,
    UnsafePathError,
    check_archives,
    check_encoding,
    check_fetch,
    check_identity,
    check_negative,
    collect_failures,
    download_path_for,
    hashlib,
    json,
    load_target_from,
    main,
    resolve_corpus_identity,
    resolve_within,
    sources_for,
    sys,
)

__all__ = [
    "CHUNK_PATH_CASES",
    "CorpusSources",
    "IndexRegistryStore",
    "OUTPUT",
    "Path",
    "ROOT",
    "SourceModified",
    "SourceNotFound",
    "SourceTooLarge",
    "UnsafePathError",
    "check_archives",
    "check_encoding",
    "check_fetch",
    "check_identity",
    "check_negative",
    "collect_failures",
    "download_path_for",
    "hashlib",
    "json",
    "load_target_from",
    "main",
    "resolve_corpus_identity",
    "resolve_within",
    "sources_for",
    "sys",
]

if __name__ == "__main__":
    main()
