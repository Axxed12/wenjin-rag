"""Authenticated health probe for the model API; never prints its bearer token."""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    try:
        token = Path("/run/secrets/model_api_token").read_text(encoding="utf-8").strip()
        request = urllib.request.Request(
            f"{base.rstrip('/')}/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except (OSError, urllib.error.URLError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
