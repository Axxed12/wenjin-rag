"""旧 evaluation.summarize_phase4_isolated_e2e 路径兼容入口；权威实现位于 tests.acceptance.summarize_phase4_isolated_e2e。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.acceptance.summarize_phase4_isolated_e2e import (  # noqa: E402
    ACTIVE_ID,
    MANIFEST_REL,
    OUT,
    REPORT,
    ROOT,
    RUNNER,
    TARGET_ID,
    Any,
    Path,
    file_sha,
    hashlib,
    json,
    main,
    process_exists,
    re,
    subprocess,
    write_markdown,
)

__all__ = [
    'hashlib',
    'json',
    're',
    'subprocess',
    'Path',
    'Any',
    'ROOT',
    'OUT',
    'REPORT',
    'RUNNER',
    'TARGET_ID',
    'ACTIVE_ID',
    'MANIFEST_REL',
    'file_sha',
    'process_exists',
    'main',
    'write_markdown',
]

if __name__ == "__main__":
    main()
