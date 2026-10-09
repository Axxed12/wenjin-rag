"""旧 evaluation.index_assets 模块/命令的兼容入口；权威实现位于 evaluation.quality.index_assets。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from evaluation.quality.index_assets import (  # noqa: E402
    IndexRegistryStore,
    IndexTarget,
    Path,
    active_index_assets,
)

__all__ = [
    'Path',
    'IndexRegistryStore',
    'IndexTarget',
    'active_index_assets',
]
