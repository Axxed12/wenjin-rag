import json
from pathlib import Path

from scripts.demo_modify_delete import (
    DELETED_PATH,
    MODIFIED_PATH,
    SENTINEL,
    make_package,
)


def test_modified_package_updates_manifest_and_source(tmp_path: Path):
    manifest_path = make_package(tmp_path / "modify", mode="modify")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    text = (manifest_path.parent / "raw" / MODIFIED_PATH).read_text(encoding="utf-8")
    assert SENTINEL in text
    entry = next(item for item in manifest["files"] if item["path"] == MODIFIED_PATH)
    assert entry["source_url"].startswith("urn:wenjin:test-fixture")
    assert entry["indexable"]


def test_deleted_package_removes_manifest_entry_and_file(tmp_path: Path):
    manifest_path = make_package(tmp_path / "delete", mode="delete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert DELETED_PATH not in {item["path"] for item in manifest["files"]}
    assert not (manifest_path.parent / "raw" / DELETED_PATH).exists()
