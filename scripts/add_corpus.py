"""兼容入口；权威实现位于 scripts.operations.add_corpus。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.add_corpus import (  # noqa: E402
    ROOT,
    Path,
    argparse,
    asdict,
    build_index,
    ensure_access_policy,
    intake,
    interactive_ask,
    json,
    main,
    parse_arguments,
    plan_pdfs,
    print_plan,
    resolve_paths,
    sys,
)

__all__ = [
    "Path",
    "ROOT",
    "argparse",
    "asdict",
    "build_index",
    "ensure_access_policy",
    "intake",
    "interactive_ask",
    "json",
    "main",
    "parse_arguments",
    "plan_pdfs",
    "print_plan",
    "resolve_paths",
    "sys",
]

if __name__ == "__main__":
    main()
