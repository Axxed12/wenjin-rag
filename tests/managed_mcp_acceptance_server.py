"""旧 tests.managed_mcp_acceptance_server 路径兼容入口；权威实现位于 tests.acceptance.managed_mcp_acceptance_server。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.acceptance.managed_mcp_acceptance_server import (  # noqa: E402
    Path,
    argparse,
    fault_hook,
    json,
    main,
    run_manager,
    run_mcp,
    time,
    uvicorn,
)

__all__ = [
    'argparse',
    'json',
    'time',
    'Path',
    'uvicorn',
    'fault_hook',
    'run_manager',
    'run_mcp',
    'main',
]

if __name__ == "__main__":
    main()
