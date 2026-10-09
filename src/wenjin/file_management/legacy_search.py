"""旧导入路径兼容层；legacy catalog 的权威实现位于 retrieval 包。"""

from wenjin.retrieval.legacy_catalog import LegacySearchService, _qdrant_fingerprint

__all__ = ["LegacySearchService", "_qdrant_fingerprint"]
