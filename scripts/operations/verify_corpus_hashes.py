"""校验 manifest 的双 Hash；实现位于正式 ingestion package。"""

import argparse
import json
from pathlib import Path

from wenjin.ingestion.manifest_documents import DEFAULT_MANIFEST
from wenjin.ingestion.verification import verify_manifest_hashes


def main() -> None:
    parser = argparse.ArgumentParser(description="验证语料manifest的双Hash契约")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--raw-data-root", type=Path)
    args = parser.parse_args()
    print(json.dumps(
        verify_manifest_hashes(args.manifest, raw_data_root=args.raw_data_root),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
