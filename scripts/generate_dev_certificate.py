"""兼容入口；权威实现位于 scripts.support.generate_dev_certificate。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.support.generate_dev_certificate import (  # noqa: E402
    NameOID,
    Path,
    argparse,
    datetime,
    hashes,
    ipaddress,
    main,
    rsa,
    serialization,
    timedelta,
    timezone,
    x509,
)

__all__ = [
    "NameOID",
    "Path",
    "argparse",
    "datetime",
    "hashes",
    "ipaddress",
    "main",
    "rsa",
    "serialization",
    "timedelta",
    "timezone",
    "x509",
]

if __name__ == "__main__":
    main()
