import json
from pathlib import Path

import pytest

from ingest import load_documents, resolve_raw_root
from scripts.fetch_corpus import github_raw_url
from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content


def make_separated_corpus(tmp_path: Path) -> tuple[Path, Path]:
    raw_data = tmp_path / "raw_data"
    source = raw_data / "demo" / "1.0" / "doc.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified corpus\r\n")
    hashed = hash_content(
        source.read_bytes(), normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
    )
    manifest = tmp_path / "corpora" / "demo-1.0" / "source-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "schema_version": 3,
        "corpus_id": "demo-1.0",
        "library": "demo",
        "version": "1.0",
        "repository": "https://github.com/example/demo",
        "commit": "abc123",
        "raw_data_subdir": "demo/1.0",
        "files": [{
            "path": "doc.txt",
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "encoding": "utf-8",
            "normalization": TEXT_NEWLINE_LF_V1,
            "bytes": source.stat().st_size,
            "indexable": True,
            "source_url": "urn:test",
        }],
    }), encoding="utf-8")
    return manifest, raw_data


def test_manifest_and_raw_data_can_be_stored_separately(tmp_path: Path):
    manifest, raw_data = make_separated_corpus(tmp_path)
    documents = load_documents(manifest, raw_data_root=raw_data)
    assert documents[0]["text"] == "verified corpus\n"
    assert documents[0]["source_path"] == "doc.txt"
    assert documents[0]["raw_sha256"] != documents[0]["canonical_sha256"]


def test_raw_data_subdir_cannot_escape_trusted_root(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    manifest = {"raw_data_subdir": "../outside"}
    with pytest.raises(ValueError, match="超出raw_data根目录"):
        resolve_raw_root(manifest_path, manifest, tmp_path / "raw_data")


def test_github_download_url_is_pinned_to_commit():
    url = github_raw_url(
        {
            "repository": "https://github.com/openscenegraph/OpenSceneGraph",
            "commit": "a827840b",
        },
        "include/osg/Geode",
    )
    assert url == (
        "https://raw.githubusercontent.com/openscenegraph/OpenSceneGraph/"
        "a827840b/include/osg/Geode"
    )
