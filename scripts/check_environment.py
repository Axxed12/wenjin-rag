"""兼容入口；权威实现位于 scripts.operations.check_environment。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.check_environment import (  # noqa: E402
    ROOT,
    BaseSettings,
    Path,
    Settings,
    SettingsConfigDict,
    argparse,
    check_basics,
    check_models,
    gc,
    importlib,
    io,
    json,
    main,
    os,
    sqlite3,
    uuid,
)

__all__ = [
    "BaseSettings",
    "Path",
    "ROOT",
    "Settings",
    "SettingsConfigDict",
    "argparse",
    "check_basics",
    "check_models",
    "gc",
    "importlib",
    "io",
    "json",
    "main",
    "os",
    "sqlite3",
    "uuid",
]

if __name__ == "__main__":
    main()
