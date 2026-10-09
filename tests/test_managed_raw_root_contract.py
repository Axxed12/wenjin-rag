"""Configured managed raw-root confinement and reopen/delete contract."""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

from wenjin.file_management import FileManagementService

pytestmark = pytest.mark.integration


class _UnitEmbedder:
    dimension = 2

    def get_query_embedding(self, _query):
        return [1.0, 0.0]

    def get_text_embedding_batch(self, texts, *, show_progress=False):
        del show_progress
        return [[1.0, 0.0] for _ in texts]


def test_configured_managed_raw_root_survives_reopen_and_is_removed_by_delete(tmp_path):
    root = tmp_path / "synthetic-project"
    configured_raw_root = root / "configured" / "managed-raw"
    upload_path = tmp_path / "synthetic-upload.txt"
    content = b"Synthetic raw-root contract marker managed_raw_root_marker.\n"
    upload_path.write_bytes(content)
    qdrant = QdrantClient(":memory:")
    manager = None
    try:
        manager = FileManagementService(
            root=root,
            qdrant_client=qdrant,
            embedder=_UnitEmbedder(),
            embedding_model="synthetic-raw-root-embedding-v1",
            managed_raw_data_root=configured_raw_root,
        )
        submitted = manager.submit_import(
            upload_path,
            display_name="合成原文",
            original_filename="managed-raw-root.txt",
        )
        imported = manager.run_job(submitted.job_id)
        assert imported.status == "completed"
        file_id = imported.file_id
        version_id = imported.version_id
        expected_root = configured_raw_root / f"file-management-{file_id}" / "v1"
        assert expected_root.is_relative_to(root)
        assert list(expected_root.glob("*.txt"))
        assert manager.get_original(file_id, expected_version_id=version_id) == content

        manager.close()
        manager = FileManagementService(
            root=root,
            qdrant_client=qdrant,
            embedder=_UnitEmbedder(),
            embedding_model="synthetic-raw-root-embedding-v1",
            managed_raw_data_root=configured_raw_root,
        )
        reopened = manager.get_file(file_id)
        assert reopened.active_version_id == version_id
        assert manager.get_original(file_id, expected_version_id=version_id) == content

        deleting = manager.submit_delete(file_id)
        deleted = manager.run_job(deleting.job_id)
        assert deleted.status == "deleted"
        assert not expected_root.exists()
        with pytest.raises((KeyError, ValueError)):
            manager.get_original(file_id, expected_version_id=version_id)
    finally:
        if manager is not None:
            manager.close()
        qdrant.close()


def test_managed_raw_root_cannot_escape_project_root(tmp_path):
    root = tmp_path / "confined-project"
    outside = tmp_path / "sibling-outside"
    with pytest.raises(ValueError, match="必须位于项目root内"):
        FileManagementService(
            root=root,
            qdrant_client=object(),
            embedder=_UnitEmbedder(),
            embedding_model="synthetic-raw-root-embedding-v1",
            managed_raw_data_root=outside,
        )
    assert not root.exists()
    assert not outside.exists()
