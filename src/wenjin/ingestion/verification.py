"""对 manifest 声明的原始字节、规范内容和语料总指纹做完整校验。"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from wenjin.content_hashing import (
    BINARY_IDENTITY_V1,
    TEXT_NEWLINE_LF_V1,
    hash_content,
    manifest_content_fingerprint,
)
from wenjin.ingestion.manifest_documents import resolve_raw_root


def verify_manifest_hashes(
    manifest_path: Path, *, raw_data_root: Path | None = None
) -> dict:
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    raw_root = resolve_raw_root(manifest_path, manifest, raw_data_root)
    profiles: Counter[str] = Counter()
    for entry in manifest["files"]:
        source_path = entry["path"]
        path = (raw_root / source_path).resolve()
        if not path.is_relative_to(raw_root):
            raise ValueError(f"文件路径超出语料目录：{source_path}")
        if not path.is_file():
            raise FileNotFoundError(f"清单中的语料文件不存在：{path}")
        raw_bytes = path.read_bytes()
        normalization = entry.get(
            "normalization",
            manifest.get("default_normalization", TEXT_NEWLINE_LF_V1),
        )
        encoding = None if normalization == BINARY_IDENTITY_V1 else entry.get(
            "encoding", "utf-8"
        )
        hashed = hash_content(
            raw_bytes, normalization=normalization, encoding=encoding
        )
        expected_raw = entry.get("raw_sha256") or entry.get("sha256")
        if not expected_raw or hashed.raw_sha256 != expected_raw.lower():
            raise ValueError(f"原始字节Hash不匹配：{source_path}")
        expected_canonical = entry.get("canonical_sha256")
        if manifest.get("schema_version", 1) >= 3 and not expected_canonical:
            raise ValueError(f"schema v3文件缺少canonical_sha256：{source_path}")
        if expected_canonical and hashed.canonical_sha256 != expected_canonical.lower():
            raise ValueError(f"规范内容Hash不匹配：{source_path}")
        if entry.get("bytes") is not None and len(raw_bytes) != entry["bytes"]:
            raise ValueError(f"原始字节数不匹配：{source_path}")
        profiles[normalization] += 1

    fingerprint = manifest_content_fingerprint(manifest)
    expected_fingerprint = manifest.get("canonical_corpus_sha256")
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise ValueError("语料canonical总指纹不匹配")
    return {
        "corpus_id": manifest.get("corpus_id"),
        "files": len(manifest["files"]),
        "profiles": dict(profiles),
        "canonical_corpus_sha256": fingerprint,
        "status": "PASS",
    }
