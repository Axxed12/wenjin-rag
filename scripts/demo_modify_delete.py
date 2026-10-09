"""兼容入口；权威实现位于 scripts.demos.demo_modify_delete。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.demos.demo_modify_delete import (  # noqa: E402
    DELETED_PATH,
    MODIFIED_PATH,
    ROOT,
    SENTINEL,
    TEXT_NEWLINE_LF_V1,
    Path,
    asdict,
    hash_content,
    json,
    main,
    make_package,
    run_case,
    shutil,
    source_counts,
    sqlite3,
    sys,
    time,
    uuid4,
)

__all__ = [
    "DELETED_PATH",
    "MODIFIED_PATH",
    "Path",
    "ROOT",
    "SENTINEL",
    "TEXT_NEWLINE_LF_V1",
    "asdict",
    "hash_content",
    "json",
    "main",
    "make_package",
    "run_case",
    "shutil",
    "source_counts",
    "sqlite3",
    "sys",
    "time",
    "uuid4",
]

if __name__ == "__main__":
    main()
