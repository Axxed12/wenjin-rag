import json
from pathlib import Path

import pytest

from scripts.verify_corpus_hashes import verify_manifest_hashes
from wenjin.content_hashing import (
    BINARY_IDENTITY_V1,
    TEXT_NEWLINE_LF_V1,
    hash_content,
    manifest_content_fingerprint,
)
from wenjin.ingestion.chunking import block_chunks


def test_crlf_and_lf_keep_raw_audit_but_share_canonical_identity():
    crlf = hash_content(
        b"alpha\r\nbeta\r\n", normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
    )
    lf = hash_content(
        b"alpha\nbeta\n", normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
    )
    assert crlf.raw_sha256 != lf.raw_sha256
    assert crlf.canonical_sha256 == lf.canonical_sha256
    assert crlf.canonical_text() == lf.canonical_text() == "alpha\nbeta\n"


def test_canonical_text_produces_identical_chunks_and_chunk_hashes():
    documents = []
    for raw in (b"comment\r\n\r\nvoid run();\r\n", b"comment\n\nvoid run();\n"):
        hashed = hash_content(
            raw, normalization=TEXT_NEWLINE_LF_V1, encoding="utf-8"
        )
        documents.append({
            "source_path": "include/Test",
            "version": "1.0",
            "text": hashed.canonical_text(),
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "normalization": hashed.normalization,
        })
    left, right = (block_chunks(document, max_chars=100) for document in documents)
    assert [(c.chunk_id, c.text, c.content_sha256) for c in left] == [
        (c.chunk_id, c.text, c.content_sha256) for c in right
    ]


def test_binary_profile_preserves_bytes_and_rejects_text_encoding():
    content = hash_content(b"%PDF\r\n\x00", normalization=BINARY_IDENTITY_V1)
    assert content.raw_sha256 == content.canonical_sha256
    assert content.canonical_bytes == b"%PDF\r\n\x00"
    with pytest.raises(ValueError, match="不能声明文本编码"):
        hash_content(
            b"binary", normalization=BINARY_IDENTITY_V1, encoding="utf-8"
        )


def test_unknown_profile_is_rejected_and_manifest_fingerprint_is_order_stable():
    with pytest.raises(ValueError, match="不支持"):
        hash_content(b"x", normalization="guessed-v1", encoding="utf-8")
    first = {"files": [
        {"path": "b", "canonical_sha256": "b" * 64, "normalization": TEXT_NEWLINE_LF_V1},
        {"path": "a", "canonical_sha256": "a" * 64, "normalization": TEXT_NEWLINE_LF_V1},
    ]}
    second = {"files": list(reversed(first["files"]))}
    assert manifest_content_fingerprint(first) == manifest_content_fingerprint(second)


def test_manifest_verifier_supports_text_and_binary_profiles(tmp_path: Path):
    raw_root = tmp_path / "raw_data"
    corpus = raw_root / "mixed" / "1.0"
    corpus.mkdir(parents=True)
    (corpus / "doc.txt").write_bytes(b"line1\r\nline2\r\n")
    (corpus / "sample.pdf").write_bytes(b"%PDF-1.4\r\n\x00")
    text = hash_content(
        (corpus / "doc.txt").read_bytes(),
        normalization=TEXT_NEWLINE_LF_V1,
        encoding="utf-8",
    )
    binary = hash_content(
        (corpus / "sample.pdf").read_bytes(), normalization=BINARY_IDENTITY_V1
    )
    manifest = {
        "schema_version": 3,
        "corpus_id": "mixed-1.0",
        "raw_data_subdir": "mixed/1.0",
        "files": [
            {
                "path": "doc.txt",
                "bytes": (corpus / "doc.txt").stat().st_size,
                "raw_sha256": text.raw_sha256,
                "canonical_sha256": text.canonical_sha256,
                "encoding": "utf-8",
                "normalization": TEXT_NEWLINE_LF_V1,
            },
            {
                "path": "sample.pdf",
                "bytes": (corpus / "sample.pdf").stat().st_size,
                "raw_sha256": binary.raw_sha256,
                "canonical_sha256": binary.canonical_sha256,
                "normalization": BINARY_IDENTITY_V1,
            },
        ],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    path = tmp_path / "corpora" / "mixed-1.0" / "source-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = verify_manifest_hashes(path, raw_data_root=raw_root)
    assert result["profiles"] == {
        TEXT_NEWLINE_LF_V1: 1,
        BINARY_IDENTITY_V1: 1,
    }
