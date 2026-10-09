"""兼容入口；权威实现位于 scripts.operations.manage_access。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.manage_access import (  # noqa: E402
    ACCESS_DIR,
    ROOT,
    WILDCARD,
    AccessPolicy,
    AccessRegistry,
    CorpusCatalog,
    Path,
    argparse,
    change_client,
    corpus_catalog_report,
    default_policy,
    list_policies,
    load_policy,
    main,
    print_identity_problems,
    sync_policies,
    sys,
    write_policy,
)

__all__ = [
    "ACCESS_DIR",
    "AccessPolicy",
    "AccessRegistry",
    "CorpusCatalog",
    "Path",
    "ROOT",
    "WILDCARD",
    "argparse",
    "change_client",
    "corpus_catalog_report",
    "default_policy",
    "list_policies",
    "load_policy",
    "main",
    "print_identity_problems",
    "sync_policies",
    "sys",
    "write_policy",
]

if __name__ == "__main__":
    main()
