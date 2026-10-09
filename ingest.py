"""兼容旧脚本和 import 路径；loader 的唯一实现位于 wenjin.ingestion。"""

from wenjin.ingestion.manifest_documents import (
    DEFAULT_MANIFEST,
    DEFAULT_RAW_DATA_ROOT,
    PROJECT_ROOT,
    load_documents,
    main,
    resolve_raw_root,
)

__all__ = [
    "DEFAULT_MANIFEST",
    "DEFAULT_RAW_DATA_ROOT",
    "PROJECT_ROOT",
    "load_documents",
    "resolve_raw_root",
    "main",
]


if __name__ == "__main__":
    main()
