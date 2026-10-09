"""兼容入口；权威实现位于 scripts.demos.demo_content_update。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.demos.demo_content_update import (  # noqa: E402
    NEW_PATH,
    ROOT,
    SENTINEL,
    TEXT_NEWLINE_LF_V1,
    Path,
    asdict,
    create_demo_package,
    hash_content,
    json,
    main,
    shutil,
    sys,
    time,
    uuid4,
)

__all__ = [
    "NEW_PATH",
    "Path",
    "ROOT",
    "SENTINEL",
    "TEXT_NEWLINE_LF_V1",
    "asdict",
    "create_demo_package",
    "hash_content",
    "json",
    "main",
    "shutil",
    "sys",
    "time",
    "uuid4",
]

if __name__ == "__main__":
    main()
