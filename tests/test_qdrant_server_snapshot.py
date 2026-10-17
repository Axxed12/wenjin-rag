from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.operations import qdrant_server_snapshot as snapshot

pytestmark = pytest.mark.quick


def test_qdrant_url_identity_rejects_credentials_and_non_origin_parts() -> None:
    assert snapshot.urlsplit_host("http://qdrant:6333/") == "qdrant:6333"
    assert snapshot.urlsplit_host("http://[::1]:6333") == "[::1]:6333"
    for value in (
        "http://user:password@qdrant:6333",
        "http://qdrant:6333/api",
        "http://qdrant:6333/?token=secret",
    ):
        with pytest.raises(ValueError):
            snapshot.urlsplit_host(value)


def test_restore_hash_checks_snapshot_before_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "snapshots"
    directory.mkdir()
    snapshot_file = directory / "collection.snapshot"
    snapshot_file.write_bytes(b"synthetic snapshot")
    (directory / "snapshot-manifest.json").write_text(
        json.dumps({
            "schema_version": 1,
            "snapshots": [{
                "collection": "synthetic",
                "path": snapshot_file.name,
                "size_bytes": snapshot_file.stat().st_size,
                "sha256": hashlib.sha256(b"different bytes").hexdigest(),
            }],
        }),
        encoding="utf-8",
    )

    class NeverUpload:
        def __init__(self, **_kwargs: object) -> None:
            self._client = type("Remote", (), {"openapi_client": object()})()

        def close(self) -> None:
            pass

    monkeypatch.setattr("qdrant_client.QdrantClient", NeverUpload)
    with pytest.raises(ValueError, match="size or SHA256 mismatch"):
        snapshot.restore_snapshots("http://qdrant:6333", directory)
