"""兼容入口；权威实现位于 scripts.demos.demo_index_switch。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.demos.demo_index_switch import (  # noqa: E402
    ROOT,
    Path,
    asdict,
    clone_collection,
    json,
    main,
    replace,
    shutil,
    sys,
    time,
    uuid4,
)

__all__ = [
    "Path",
    "ROOT",
    "asdict",
    "clone_collection",
    "json",
    "main",
    "replace",
    "shutil",
    "sys",
    "time",
    "uuid4",
]

if __name__ == "__main__":
    main()
