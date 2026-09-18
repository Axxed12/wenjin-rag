"""让评测脚本与线上检索读取同一个active索引注册表。"""

from pathlib import Path

from wenjin.index_registry import IndexRegistryStore, IndexTarget


def active_index_assets(root: Path) -> tuple[IndexTarget, Path]:
    target = IndexRegistryStore(root / "data/indexes/index-registry.json").active()
    return target, root / target.bm25_path
