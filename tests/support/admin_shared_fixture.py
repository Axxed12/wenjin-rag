"""Build one synthetic shared-corpus index for isolated admin UI acceptance.

This fixture creates all inputs beneath the caller's temporary project root and
uses the production candidate-index builder. Its results prove the shared query
path only; they do not represent real-corpus retrieval quality.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from wenjin.content_hashing import hash_content, manifest_content_fingerprint
from wenjin.index_registry import IndexRegistryStore
from wenjin.indexing.build import build_candidate_index


def build_admin_shared_fixture(root: Path, qdrant_client, embedder) -> dict:
    """Create a private synthetic legacy corpus plus a fully built active index."""
    root = Path(root).resolve()
    corpus_id = "admin-shared-smoke-1.0"
    library_id = "admin-shared-smoke"
    version = "1.0"
    relative_source = "shared-guide.txt"
    raw_subdir = f"{library_id}/{version}"
    manifest_relative = f"corpora-private/{corpus_id}/source-manifest.json"
    manifest_path = root / manifest_relative
    raw_root = root / "raw_data" / raw_subdir
    source_path = raw_root / relative_source
    registry_path = root / "data" / "indexes" / "index-registry.json"

    for path in (manifest_path, source_path, registry_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite isolated fixture input: {path}")

    text = (
        "Synthetic shared corpus for the admin acceptance screen. "
        "The sharedambermarker confirms a successful read-only shared query.\n"
    )
    content = text.encode("utf-8")
    hashed = hash_content(content, normalization="text-newline-lf-v1", encoding="utf-8")
    manifest = {
        "schema_version": 3,
        "corpus_id": corpus_id,
        "library": library_id,
        "version": version,
        "license": "synthetic-test-only",
        "source_url": "urn:synthetic:admin-shared-smoke",
        "raw_data_subdir": raw_subdir,
        "files": [
            {
                "path": relative_source,
                "raw_sha256": hashed.raw_sha256,
                "canonical_sha256": hashed.canonical_sha256,
                "normalization": "text-newline-lf-v1",
                "encoding": "utf-8",
                "bytes": len(content),
                "indexable": True,
                "parser": "text-line-v1",
                "source_url": "urn:synthetic:admin-shared-smoke/shared-guide.txt",
                "logical_document_id": f"{corpus_id}:{relative_source}",
            }
        ],
    }
    manifest["canonical_corpus_sha256"] = manifest_content_fingerprint(manifest)
    source_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    target = build_candidate_index(
        manifest_path=manifest_path,
        target_id="admin-shared-smoke",
        strategy="blocks-split",
        dense_collection="admin_shared_smoke",
        bm25_path=root / "data" / "indexes" / "admin-shared-smoke.sqlite",
        qdrant_client=qdrant_client,
        embedder=embedder,
        embedding_model="isolated-marker-test-only",
        project_root=root,
    )

    registry = IndexRegistryStore(registry_path)
    registry.initialize(target, reason="isolated synthetic admin UI acceptance fixture")
    return {
        "library_id": library_id,
        "corpus_id": corpus_id,
        "index_target_id": target.target_id,
        "document_id": "doc-" + hashlib.sha256(
            f"{corpus_id}\0{relative_source}".encode("utf-8")
        ).hexdigest()[:24],
        "query": "sharedambermarker",
        "manifest_path": manifest_relative,
        "source_path": source_path.relative_to(root).as_posix(),
        "classification": "synthetic-fixture-not-quality-validation",
    }
