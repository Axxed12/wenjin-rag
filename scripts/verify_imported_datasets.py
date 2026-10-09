"""兼容入口；权威实现位于 scripts.operations.verify_imported_datasets。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.operations.verify_imported_datasets import (  # noqa: E402
    answer_atoms,
    main,
    mutation_checks,
    readable,
    verify,
)

__all__ = [
    "answer_atoms",
    "main",
    "mutation_checks",
    "readable",
    "verify",
]

if __name__ == "__main__":
    main()
