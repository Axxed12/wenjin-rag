"""Start an isolated, long-lived synthetic private MCP fixture for CLI/tool agents.

The fixture stores only generated content and a generated bearer token in a unique
temporary directory. Create `<state-dir>/stop` to stop it and remove that directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from importlib.metadata import version as package_version
from pathlib import Path

import uvicorn
from qdrant_client import QdrantClient

from tests.test_private_catalog_identity import _EMBEDDING_MODEL, _MarkerEmbedder
from wenjin.access import AccessPolicy, write_policy
from wenjin.content_hashing import TEXT_NEWLINE_LF_V1, hash_content
from wenjin.indexing.build import build_candidate_index
from tests.test_mcp_private_route_delivery_contract import (
    _CLIENT_ID,
    _make_app,
    _write_private_route,
)


SYNTHETIC_DOCUMENT = """岚湾档案管理指南（合成资料）
版本：2.3

已批准的业务档案默认保留三十六个月，自项目验收日期起算。
临时导出副本应在九十天内清除；它不改变正式档案的保留期限。
发生保存期限争议时，档案管理员应在五个工作日内提交复核记录。
本指南不适用于个人身份信息或法定诉讼保全资料。
"""
NATURAL_QUERY = "根据岚湾档案管理指南，正式业务档案的默认保留期是多少？临时导出副本有什么不同？请引用证据。"


def _create_fixture_root() -> tuple[Path, QdrantClient, object, str, str]:
    temp_parent = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix="wenjin-team-delivery-mcp-", dir=temp_parent)).resolve()
    qdrant = QdrantClient(":memory:")
    library = "synthetic-lanwan-archive"
    version = "2.3"
    corpus_id = f"{library}-{version}"
    relative = "records/retention-guide.txt"
    source = SYNTHETIC_DOCUMENT.encode("utf-8")
    source_path = root / "raw_data" / library / version / relative
    manifest_path = root / "corpora-private" / corpus_id / "source-manifest.json"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source)
    hashed = hash_content(
        source,
        normalization=TEXT_NEWLINE_LF_V1,
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 3,
        "corpus_id": corpus_id,
        "library": library,
        "version": version,
        "license": "synthetic-test-only",
        "source_url": f"urn:synthetic:{corpus_id}",
        "raw_data_subdir": f"{library}/{version}",
        "files": [{
            "path": relative,
            "raw_sha256": hashed.raw_sha256,
            "canonical_sha256": hashed.canonical_sha256,
            "normalization": TEXT_NEWLINE_LF_V1,
            "encoding": "utf-8",
            "bytes": len(source),
            "indexable": True,
            "source_url": f"urn:synthetic:{corpus_id}/{relative}",
        }],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    embedder = _MarkerEmbedder()
    target = build_candidate_index(
        manifest_path=manifest_path,
        target_id="synthetic-detached-target-2-3",
        strategy="blocks-split",
        dense_collection="private_synthetic_lanwan_archive_2_3",
        bm25_path=root / "data" / "indexes-private" / "lanwan-detached.sqlite",
        qdrant_client=qdrant,
        embedder=embedder,
        embedding_model=_EMBEDDING_MODEL,
        project_root=root,
    )
    config_dir = root / "configs" / "indexes-private"
    config_dir.mkdir(parents=True, exist_ok=True)
    # Route JSON, not a target-id filename convention, is the MCP routing authority.
    (config_dir / "synthetic-detached-target.json").write_text(
        json.dumps(target.__dict__), encoding="utf-8"
    )
    _write_private_route(root, library, target)
    write_policy(
        root / "configs" / "access",
        AccessPolicy(corpus_id, "internal", (_CLIENT_ID,)),
    )
    return root, qdrant, target, corpus_id, hashlib.sha256(source).hexdigest()


def main() -> int:
    configured_state_dir = os.environ.get("WENJIN_FIXTURE_STATE_DIR", "").strip()
    if not configured_state_dir:
        raise SystemExit("Set WENJIN_FIXTURE_STATE_DIR to an explicit temporary state directory")
    state_dir = Path(configured_state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    root, qdrant, target, corpus_id, source_sha256 = _create_fixture_root()
    bearer = "SyntheticTeamDelivery_" + hashlib.sha256(os.urandom(48)).hexdigest()
    app, path = _make_app(root=root, qdrant=qdrant, target=target, bearer=bearer)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started:
            raise RuntimeError("synthetic MCP fixture did not start")
        descriptor = {
            "pid": os.getpid(),
            "root": str(root),
            "endpoint": base_url + path,
            "protocol_major": int(package_version("mcp").split(".", 1)[0]),
            "bearer": bearer,
            "library": target.library,
            "version": target.version,
            "target_id": target.target_id,
            "corpus_id": corpus_id,
            "source_sha256": source_sha256,
            "query": NATURAL_QUERY,
            "fixture_classification": "synthetic MCP/tool evidence only",
        }
        descriptor_path = state_dir / "fixture.json"
        descriptor_path.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
        print(f"READY {descriptor_path}", flush=True)
        stop_path = state_dir / "stop"
        while not stop_path.exists() and thread.is_alive():
            time.sleep(0.25)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        qdrant.close()
        shutil.rmtree(root, ignore_errors=True)
        for name in ("fixture.json", "stop"):
            try:
                (state_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            state_dir.rmdir()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
