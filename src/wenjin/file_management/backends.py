"""文件管理使用的索引资源适配层。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class IndexAdapter(Protocol):
    """让管理器只依赖构建、校验、移除和计数契约，不绑定索引实现细节。"""

    def build(
        self,
        *,
        manifest_path: Path,
        target_id: str,
        collection: str,
        bm25_path: Path,
        strategy: str,
    ) -> Any: ...

    def verify(self, target: Any) -> dict: ...

    def delete(self, resource_id: str) -> None: ...

    def exists(self, resource_id: str) -> bool: ...

    def count(self, resource_id: str) -> int: ...


class QdrantBm25Adapter:
    """生产适配器：复用现有真实Embedding、Qdrant和SQLite FTS5构建路径。"""

    def __init__(
        self, *, root: Path, qdrant_client, embedder, embedding_model: str,
        managed_raw_data_root: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.managed_raw_data_root = Path(managed_raw_data_root or (self.root / "raw_data")).resolve()
        self.qdrant_client = qdrant_client
        self.embedder = embedder
        self.embedding_model = embedding_model

    def build(
        self,
        *,
        manifest_path: Path,
        target_id: str,
        collection: str,
        bm25_path: Path,
        strategy: str,
    ):
        from wenjin.indexing.build import build_candidate_index

        return build_candidate_index(
            manifest_path=manifest_path,
            target_id=target_id,
            strategy=strategy,
            dense_collection=collection,
            bm25_path=bm25_path,
            qdrant_client=self.qdrant_client,
            embedder=self.embedder,
            embedding_model=self.embedding_model,
            project_root=self.root,
            raw_data_root=self.managed_raw_data_root,
        )

    def verify(self, target) -> dict:
        from wenjin.index_audit import admit_index

        return admit_index(target, root=self.root, qdrant_client=self.qdrant_client)

    def delete(self, resource_id: str) -> None:
        if self.qdrant_client.collection_exists(resource_id):
            self.qdrant_client.delete_collection(resource_id)

    def exists(self, resource_id: str) -> bool:
        return bool(self.qdrant_client.collection_exists(resource_id))

    def count(self, resource_id: str) -> int:
        return int(self.qdrant_client.get_collection(resource_id).points_count)
