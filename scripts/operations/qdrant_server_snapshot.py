"""Export and restore Qdrant Server collection snapshots for one delivery drill."""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.request
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_snapshots(url: str, directory: Path) -> dict:
    """Create and download every collection snapshot into a new empty directory."""
    from qdrant_client import QdrantClient

    server_identity = urlsplit_host(url)
    directory = Path(directory)
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError("snapshot output must be a new or empty directory")
    directory.mkdir(parents=True, exist_ok=True)
    client = QdrantClient(url=url, timeout=60)
    records = []
    try:
        collection_names = sorted(item.name for item in client.get_collections().collections)
        for collection in collection_names:
            snapshot = client.create_snapshot(collection, wait=True)
            if snapshot is None or not snapshot.name:
                raise RuntimeError("Qdrant did not return a completed collection snapshot")
            if Path(snapshot.name).name != snapshot.name:
                raise RuntimeError("Qdrant returned an unsafe snapshot name")
            quoted_collection = urllib.parse.quote(collection, safe="")
            quoted_snapshot = urllib.parse.quote(snapshot.name, safe="")
            request = urllib.request.Request(
                f"{url.rstrip('/')}/collections/{quoted_collection}/snapshots/{quoted_snapshot}",
                method="GET",
            )
            filename = f"{hashlib.sha256(collection.encode()).hexdigest()[:16]}-{snapshot.name}"
            destination = directory / filename
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(request, timeout=300) as response, destination.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            records.append({
                "collection": collection,
                "snapshot": snapshot.name,
                "path": filename,
                "size_bytes": size,
                "sha256": digest.hexdigest(),
            })
        manifest = {"schema_version": 1, "qdrant_url_identity": server_identity, "snapshots": records}
        (directory / "snapshot-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {"snapshot_count": len(records), "snapshot_manifest_sha256": _sha256(directory / "snapshot-manifest.json")}
    finally:
        client.close()


def restore_snapshots(url: str, directory: Path) -> dict:
    """Hash-check the export then upload each snapshot to a new Qdrant Server."""
    from qdrant_client import QdrantClient

    urlsplit_host(url)
    directory = Path(directory).resolve(strict=True)
    manifest_path = directory / "snapshot-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported Qdrant snapshot manifest")
    rows = manifest.get("snapshots")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Qdrant snapshot manifest must contain collection snapshots")
    verified_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid Qdrant snapshot manifest item")
        filename = row.get("path")
        if not isinstance(filename, str) or Path(filename).name != filename or filename in {"", ".", ".."}:
            raise ValueError("unsafe Qdrant snapshot path in manifest")
        source = (directory / filename).resolve(strict=True)
        if not source.is_relative_to(directory) or not source.is_file():
            raise ValueError("Qdrant snapshot path escaped its restore directory")
        if source.stat().st_size != row.get("size_bytes") or _sha256(source) != row.get("sha256"):
            raise ValueError("Qdrant snapshot size or SHA256 mismatch")
        collection = row.get("collection")
        if not isinstance(collection, str) or not collection:
            raise ValueError("invalid collection name in snapshot manifest")
        verified_rows.append((collection, source))

    client = QdrantClient(url=url, timeout=300)
    restored = []
    try:
        api = client._client.openapi_client.snapshots_api
        for collection, source in verified_rows:
            with source.open("rb") as snapshot_stream:
                api.recover_from_uploaded_snapshot(
                    collection_name=collection,
                    wait=True,
                    snapshot=snapshot_stream,
                )
            restored.append(collection)
        visible = {item.name for item in client.get_collections().collections}
        if not set(restored).issubset(visible):
            raise RuntimeError("restored Qdrant collection set is incomplete")
        return {"restored_collection_count": len(restored)}
    finally:
        client.close()


def urlsplit_host(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Qdrant URL must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Qdrant URL must not contain credentials or a non-origin component")
    hostname = parsed.hostname
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{hostname}:{parsed.port}" if parsed.port is not None else hostname


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "restore"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (
            export_snapshots(args.url, args.directory)
            if args.action == "export" else restore_snapshots(args.url, args.directory)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Qdrant snapshot operation failed: {type(error).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
