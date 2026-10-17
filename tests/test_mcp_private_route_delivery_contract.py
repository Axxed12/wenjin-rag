"""Synthetic MCP contracts for explicit private shared routes.

Run this module once with the project's legacy interpreter and once with the
isolated stateless interpreter. Each run starts only a loopback test server and
uses a temporary project root, a deterministic embedder, and synthetic source
bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from threading import RLock, Thread

import httpx
import pytest
import uvicorn
from qdrant_client import QdrantClient
from starlette.middleware import Middleware

from tests.test_private_catalog_identity import (
    _EMBEDDING_MODEL,
    _build_private_target,
    _MarkerEmbedder,
    _write_private_route,
)
from wenjin.access import AccessPolicy, write_policy
from wenjin.audit import SearchAudit
from wenjin.file_management import FileManagementService
from wenjin.identity import IdentityStore
from wenjin.identity.mcp_auth import McpIdentityMiddleware
from wenjin.index_registry import IndexTarget
from wenjin.management_http.app import create_app as create_management_app
from wenjin.mcp.managed_client import ManagedFileHTTPClient
from wenjin.mcp.runtime import RuntimeIdentity, create_release_resolver, create_router
from wenjin.retrieval.search_policy import SearchPolicy

_API_KEY = "SyntheticPrivateRouteBearerKey_20261017"
_CLIENT_ID = "synthetic-private-reader"
_MODERN_PROTOCOL = "2026-07-28"


def _start_server(app) -> tuple[uvicorn.Server, Thread, str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="on")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("synthetic private MCP loopback server failed to start")
    return server, thread, f"http://127.0.0.1:{port}"


def _stop_server(server: uvicorn.Server | None, thread: Thread | None) -> None:
    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=10)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        assert not thread.is_alive(), "synthetic private MCP server failed to stop"


def _protocol_major() -> int:
    return int(version("mcp").split(".", 1)[0])


def _download_path(corpus_id: str, relative_path: str) -> str:
    prefix = "/mcp-stateless" if _protocol_major() == 2 else ""
    return f"{prefix}/files/{corpus_id}/{relative_path}"


def _attach_test_resolver(app, resolver) -> None:
    state = getattr(app, "state", None)
    if state is not None:
        state.synthetic_release_resolver = resolver
    else:
        app.synthetic_release_resolver = resolver


def _test_resolver(app):
    state = getattr(app, "state", None)
    return (
        state.synthetic_release_resolver
        if state is not None
        else app.synthetic_release_resolver
    )


def _private_target(
    root: Path, qdrant, library: str, version_label: str, *, snapshot: bool
):
    target, corpus_id, marker, relative = _build_private_target(
        root, qdrant, library, version_label
    )
    original = root / "raw_data" / library / version_label / relative
    source_bytes = original.read_bytes()
    if snapshot:
        snapshot_name = f"raw-snapshots/{library}-{version_label.replace('.', '-')}"
        snapshot_root = root / snapshot_name / library / version_label
        snapshot_root.mkdir(parents=True)
        snapshot_source = snapshot_root / relative
        snapshot_source.write_bytes(source_bytes)
        target = replace(target, raw_snapshot_root=snapshot_name)
    else:
        snapshot_source = original
    (root / "configs" / "indexes-private" / f"{target.target_id}.json").write_text(
        json.dumps(target.__dict__), encoding="utf-8"
    )
    return target, corpus_id, marker, relative, source_bytes


def _write_route_target(route_path: Path, target: IndexTarget) -> None:
    route_path.write_text(
        json.dumps({"schema_version": 1, "candidates": [target.__dict__]}),
        encoding="utf-8",
    )


def _make_app(
    *, root: Path, qdrant, target: IndexTarget, bearer: str,
    identity_store: IdentityStore | None = None,
    managed_bridge: ManagedFileHTTPClient | None = None,
):
    router = create_router(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        reranker=None,
        search_lock=RLock(),
    )
    assert router is not None
    assert [item.target_id for item in router.indices].count(target.target_id) == 1
    resolver = create_release_resolver(
        root=root,
        qdrant_client=qdrant,
        embedder=router.embedder,
        reranker=router.reranker,
        legacy_router=router,
        search_lock=router.search_lock,
        expected_embedding_model=target.embedding_model,
    )
    assert resolver.runtime["embedder"] is router.embedder
    assert resolver.runtime["reranker"] is router.reranker
    assert resolver.runtime["search_lock"] is router.search_lock

    service = type("SyntheticService", (), {})()
    service.qdrant_client = qdrant
    service.reranker = None
    service.search_lock = router.search_lock
    service.search_policy = SearchPolicy(reranker_enabled=False)

    def search(query, *, config):
        routed_config = service.search_policy.route_config(
            config.top_k, context_char_budget=config.context_char_budget
        )
        return router.search(query, config=routed_config)

    service.search = search

    identity = RuntimeIdentity(
        corpus_id=target.corpus_identity(),
        corpus_id_source="synthetic-private-route",
        target_id=target.target_id,
        embedding_model=target.embedding_model,
    )
    if _protocol_major() == 1:
        from wenjin.auth import BearerAuthMiddleware
        from wenjin.mcp.server import build_mcp_server

        server = build_mcp_server(
            service=service,
            audit=SearchAudit(root / "artifacts" / "private-route-audit.jsonl"),
            access_dir=root / "configs" / "access",
            bearer_enabled=identity_store is None,
            identity=identity,
            corpus_router=router,
            release_resolver=resolver,
            identity_store=identity_store,
            managed_bridge=managed_bridge,
            raw_root=root / "raw_data",
            project_root=root,
        )
        middleware = (
            [Middleware(McpIdentityMiddleware, identity_store=identity_store)]
            if identity_store is not None
            else [Middleware(BearerAuthMiddleware, api_key=bearer, client_id=_CLIENT_ID)]
        )
        app = server.http_app(
            path="/mcp",
            middleware=middleware,
            stateless_http=False,
            json_response=True,
        )
        _attach_test_resolver(app, resolver)
        return app, "/mcp"

    if _protocol_major() == 2:
        from wenjin.mcp.stateless_server import (
            build_stateless_mcp_server,
            create_stateless_app,
        )

        server = build_stateless_mcp_server(
            service=service,
            audit=SearchAudit(root / "artifacts" / "private-route-audit.jsonl"),
            access_dir=root / "configs" / "access",
            bearer_enabled=identity_store is None,
            identity=identity,
            corpus_router=router,
            release_resolver=resolver,
            identity_store=identity_store,
            managed_bridge=managed_bridge,
            raw_root=root / "raw_data",
            project_root=root,
        )
        if identity_store is None:
            app = create_stateless_app(
                server=server,
                api_key=bearer,
                client_id=_CLIENT_ID,
                allowed_hosts=["127.0.0.1", "127.0.0.1:*"],
                project_root=root,
            )
        else:
            app = create_stateless_app(
                server=server,
                identity_store=identity_store,
                allowed_hosts=["127.0.0.1", "127.0.0.1:*"],
                project_root=root,
            )
        _attach_test_resolver(app, resolver)
        return app, "/mcp-stateless"

    pytest.skip(f"unsupported MCP SDK major version: {_protocol_major()}")


def _modern_node_client_dir() -> Path:
    configured = os.environ.get("MCP_MODERN_NODE_CLIENT_DIR")
    if not configured:
        pytest.skip("set MCP_MODERN_NODE_CLIENT_DIR to the pinned synthetic SDK client directory")
    directory = Path(configured).resolve()
    script = directory / "client.mjs"
    package = directory / "node_modules" / "@modelcontextprotocol" / "client" / "package.json"
    if not script.is_file() or not package.is_file():
        pytest.skip("the pinned official TypeScript MCP SDK client is not installed in this directory")
    return directory


def _modern_call(
    *, url: str, tool: str, arguments: dict, expect_error: bool = False,
    token: str = _API_KEY,
) -> dict:
    directory = _modern_node_client_dir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "MCP_STATELESS_URL": url,
        "MCP_STATELESS_TOKEN": token,
        "MCP_STATELESS_TOOL": tool,
        "MCP_STATELESS_ARGUMENTS_JSON": json.dumps(arguments, separators=(",", ":")),
        "MCP_STATELESS_EXPECT_TOOL_ERROR": "true" if expect_error else "false",
    }
    result = subprocess.run(
        ["node", str(directory / "client.mjs")],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    payload = json.loads(result.stdout)["callResult"]["structuredContent"]
    assert payload["mcp_protocol"] == _MODERN_PROTOCOL
    return payload


async def _legacy_call(
    *, url: str, tool: str, arguments: dict, token: str = _API_KEY
) -> dict:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    async with Client(transport, timeout=15) as client:
        tools = await client.list_tools()
        assert {item.name for item in tools} >= {"list_corpora", "search_docs"}
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        assert result.structured_content is not None
        return result.structured_content


def _call(
    *, url: str, tool: str, arguments: dict, expect_error: bool = False,
    token: str = _API_KEY,
) -> dict:
    if _protocol_major() == 1:
        return asyncio.run(_legacy_call(url=url, tool=tool, arguments=arguments, token=token))
    return _modern_call(
        url=url,
        tool=tool,
        arguments=arguments,
        expect_error=expect_error,
        token=token,
    )


@pytest.mark.integration
def test_private_route_discovery_search_source_acl_revoke_and_version_restart(tmp_path):
    """Both SDK eras must observe one private route, exact source bytes, and live ACL changes."""
    root = tmp_path / "synthetic-private-mcp-root"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    mcp_server = None
    mcp_thread = None
    mcp_url = None
    try:
        target_a, corpus_a, marker_a, relative_a, bytes_a = _private_target(
            root, qdrant, "private-wire", "1.0", snapshot=True
        )
        target_b, corpus_b, marker_b, relative_b, bytes_b = _private_target(
            root, qdrant, "private-wire", "2.0", snapshot=False
        )
        # The active target's trusted original is the pinned snapshot tree. A stale
        # mutable raw_data copy must not become the source returned by MCP download.
        (root / "raw_data" / "private-wire" / "1.0" / relative_a).write_bytes(
            b"synthetic wrong mutable source; never serve this copy\n"
        )
        route = _write_private_route(root, "private-wire", target_a)
        access_dir = root / "configs" / "access"
        # The private library is initially not authorized. Discovery and denied calls
        # must not disclose the corpus, target, source path, or source text.
        write_policy(access_dir, AccessPolicy(corpus_a, "internal", ()))
        write_policy(access_dir, AccessPolicy(corpus_b, "internal", ()))

        app, path = _make_app(root=root, qdrant=qdrant, target=target_a, bearer=_API_KEY)
        mcp_server, mcp_thread, mcp_url = _start_server(app)
        endpoint = f"{mcp_url}{path}"

        denied_catalog = _call(url=endpoint, tool="list_corpora", arguments={"include_files": True})
        assert corpus_a not in {item.get("corpus_id") for item in denied_catalog["corpora"]}
        denied_query = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_a, "library": target_a.library, "version": target_a.version},
            expect_error=_protocol_major() == 2,
        )
        assert denied_query["error"] == "forbidden"
        assert denied_query["corpus_id"] is None
        assert denied_query["evidence"] == []
        assert marker_a not in json.dumps(denied_query)

        denied_download = httpx.get(
            f"{mcp_url}{_download_path(corpus_a, relative_a)}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert denied_download.status_code == 403
        assert marker_a.encode() not in denied_download.content

        write_policy(access_dir, AccessPolicy(corpus_a, "internal", (_CLIENT_ID,)))
        write_policy(access_dir, AccessPolicy(corpus_b, "internal", (_CLIENT_ID,)))
        granted_catalog = _call(url=endpoint, tool="list_corpora", arguments={"include_files": True})
        discovered = next(item for item in granted_catalog["corpora"] if item["corpus_id"] == corpus_a)
        assert discovered["index_state"] == "usable"
        assert target_a.target_id in discovered["index_usable_targets"]

        query_a = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_a, "library": target_a.library, "version": target_a.version},
        )
        assert query_a.get("error") is None
        assert query_a["routed_libraries"] == [{
            "library": target_a.library,
            "version": target_a.version,
            "target_id": target_a.target_id,
        }]
        evidence_a = next(item for item in query_a["evidence"] if marker_a in item["text"])
        assert evidence_a["corpus_id"] == corpus_a
        assert evidence_a["index_target_id"] == target_a.target_id
        assert evidence_a["download_path"] == _download_path(corpus_a, relative_a)
        source_a = httpx.get(
            f"{mcp_url}{evidence_a['download_path']}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert source_a.status_code == 200, source_a.text
        assert source_a.content == bytes_a
        assert hashlib.sha256(source_a.content).hexdigest() == hashlib.sha256(bytes_a).hexdigest()

        # Route switching is a startup configuration change: stop the first synthetic
        # server, select target B, then verify the new process cannot serve A's old path.
        _stop_server(mcp_server, mcp_thread)
        mcp_server = mcp_thread = None
        _write_route_target(route, target_b)
        app_b, path_b = _make_app(root=root, qdrant=qdrant, target=target_b, bearer=_API_KEY)
        mcp_server, mcp_thread, mcp_url_b = _start_server(app_b)
        endpoint_b = f"{mcp_url_b}{path_b}"
        query_b = _call(
            url=endpoint_b,
            tool="search_docs",
            arguments={"query": marker_b, "library": target_b.library, "version": target_b.version},
        )
        assert query_b.get("error") is None
        evidence_b = next(item for item in query_b["evidence"] if marker_b in item["text"])
        assert evidence_b["corpus_id"] == corpus_b
        assert evidence_b["index_target_id"] == target_b.target_id
        assert evidence_b["download_path"] == _download_path(corpus_b, relative_b)
        source_b = httpx.get(
            f"{mcp_url_b}{evidence_b['download_path']}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert source_b.status_code == 200, source_b.text
        assert source_b.content == bytes_b
        assert hashlib.sha256(source_b.content).hexdigest() == hashlib.sha256(bytes_b).hexdigest()

        stale_source = httpx.get(
            f"{mcp_url_b}{evidence_a['download_path']}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert stale_source.status_code in {404, 409}
        assert marker_a.encode() not in stale_source.content

        write_policy(access_dir, AccessPolicy(corpus_b, "internal", ()))
        revoked_catalog = _call(url=endpoint_b, tool="list_corpora", arguments={})
        assert corpus_b not in {item.get("corpus_id") for item in revoked_catalog["corpora"]}
        revoked_query = _call(
            url=endpoint_b,
            tool="search_docs",
            arguments={"query": marker_b, "library": target_b.library, "version": target_b.version},
            expect_error=_protocol_major() == 2,
        )
        assert revoked_query["error"] == "forbidden"
        assert revoked_query["corpus_id"] is None
        assert revoked_query["evidence"] == []
        revoked_source = httpx.get(
            f"{mcp_url_b}{evidence_b['download_path']}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert revoked_source.status_code == 403
        assert marker_b.encode() not in revoked_source.content
    finally:
        _stop_server(mcp_server, mcp_thread)
        qdrant.close()


@pytest.mark.integration
def test_runtime_router_ignores_private_candidates_ambiguity_and_lost_release_pointer(
    tmp_path, monkeypatch
):
    """Only one explicit private route target can enter MCP; a lost release pointer fails closed."""
    from wenjin.releases.catalog import ReleaseCatalog
    from wenjin.releases.resolver import release_catalog_path
    from wenjin.retrieval.router import MultiCorpusRouter

    root = tmp_path / "synthetic-private-router-selection"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    observed = []

    def capture(cls, targets, **kwargs):
        del cls, kwargs
        observed.append(tuple(targets))
        return type("RouterSnapshot", (), {"indices": tuple(targets)})()

    monkeypatch.setattr(MultiCorpusRouter, "from_targets", classmethod(capture))
    target_a, corpus_a, _marker_a, _relative_a = _build_private_target(
        root, qdrant, "private-router", "1.0"
    )
    target_b, _corpus_b, _marker_b, _relative_b = _build_private_target(
        root, qdrant, "private-router", "2.0"
    )
    kwargs = {
        "root": root,
        "qdrant_client": qdrant,
        "embedder": _MarkerEmbedder(),
    }
    try:
        # Standalone private target JSON is a candidate, not an implicit MCP route.
        assert create_router(**kwargs) is None

        route = _write_private_route(root, "private-router", target_a)
        selected = create_router(**kwargs)
        assert [item.target_id for item in selected.indices] == [target_a.target_id]

        _write_private_route(root, "private-router-second", target_b)
        ambiguous = create_router(**kwargs)
        assert ambiguous is None

        route.unlink()
        (root / "configs" / "indexes-private" / "private-router-second-route.json").unlink()
        _write_private_route(root, "private-router-release", target_a)
        catalog = ReleaseCatalog(release_catalog_path(root))
        catalog.register({
            "release_id": "private-router-release-a",
            "library_id": "private-router",
            "status": "preparing",
            "created_at": "2026-10-17T00:00:00+00:00",
        })
        catalog.mark_ready(
            "private-router-release-a",
            seal={
                "target": target_a.__dict__,
                "release_digest": "a" * 64,
                "source_hashes": {},
                "validation": {"status": "valid"},
            },
        )
        catalog.activate(
            "private-router",
            "private-router-release-a",
            expected_revision=0,
            reason="synthetic private release activation",
        )
        with __import__("sqlite3").connect(release_catalog_path(root)) as database:
            database.execute(
                "UPDATE libraries SET active_release_id=NULL WHERE library_id=?",
                ("private-router",),
            )
        assert create_router(**kwargs) is None
    finally:
        qdrant.close()


@pytest.mark.integration
def test_private_raw_snapshot_source_matches_the_routed_target(tmp_path):
    """A target-bound raw snapshot, not a mutable raw_data fallback, backs evidence downloads."""
    root = tmp_path / "synthetic-private-snapshot-source"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    mcp_server = mcp_thread = None
    try:
        target, corpus_id, marker, relative, expected = _private_target(
            root, qdrant, "snapshot-source", "7.4", snapshot=True
        )
        _write_private_route(root, target.library, target)
        mutable_copy = root / "raw_data" / target.library / target.version / relative
        mutable_copy.write_bytes(b"synthetic stale mutable copy\n")
        write_policy(
            root / "configs" / "access",
            AccessPolicy(corpus_id, "internal", (_CLIENT_ID,)),
        )
        app, path = _make_app(root=root, qdrant=qdrant, target=target, bearer=_API_KEY)
        mcp_server, mcp_thread, base = _start_server(app)
        result = _call(
            url=f"{base}{path}",
            tool="search_docs",
            arguments={"query": marker, "library": target.library, "version": target.version},
        )
        evidence = next(item for item in result["evidence"] if marker in item["text"])
        assert evidence["corpus_id"] == corpus_id
        assert evidence["index_target_id"] == target.target_id
        downloaded = httpx.get(
            f"{base}{evidence['download_path']}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == expected
        assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(expected).hexdigest()
        assert downloaded.content != mutable_copy.read_bytes()
    finally:
        _stop_server(mcp_server, mcp_thread)
        qdrant.close()


@pytest.mark.integration
def test_private_route_switch_is_consistent_between_live_management_and_mcp(tmp_path):
    """A running MCP process must not keep serving a startup target after route A→B."""
    from wenjin.retrieval.legacy_catalog import LegacySearchService

    root = tmp_path / "synthetic-private-live-route-switch"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    mcp_server = mcp_thread = None
    try:
        target_a, corpus_a, marker_a, _relative_a = _build_private_target(
            root, qdrant, "live-route", "1.0"
        )
        target_b, corpus_b, marker_b, _relative_b = _build_private_target(
            root, qdrant, "live-route", "2.0"
        )
        bytes_b = (root / "raw_data" / "live-route" / "2.0" / "private-guide.txt").read_bytes()
        route = _write_private_route(root, "live-route", target_a)
        access_dir = root / "configs" / "access"
        write_policy(access_dir, AccessPolicy(corpus_a, "internal", (_CLIENT_ID,)))
        write_policy(access_dir, AccessPolicy(corpus_b, "internal", (_CLIENT_ID,)))
        app, path = _make_app(root=root, qdrant=qdrant, target=target_a, bearer=_API_KEY)
        mcp_server, mcp_thread, base = _start_server(app)
        endpoint = f"{base}{path}"

        old_mcp_catalog = _call(url=endpoint, tool="list_corpora", arguments={})
        assert corpus_a in {item.get("corpus_id") for item in old_mcp_catalog["corpora"]}
        _write_route_target(route, target_b)

        # The management service reads its current private route selection per call.
        management = LegacySearchService(
            root,
            qdrant,
            _MarkerEmbedder(),
            expected_embedding_model=_EMBEDDING_MODEL,
            search_policy=SearchPolicy(reranker_enabled=False),
        )
        catalog = management.catalog()
        source_b = next(
            item
            for library in catalog["libraries"]
            for item in library["sources"]
            if item.get("corpus_id") == corpus_b
        )
        assert source_b["is_active"] and source_b["searchable"]
        management_result = management.search(
            library_id="live-route",
            corpus_id=corpus_b,
            version_id=source_b["version_id"],
            query=marker_b,
            index_target_id=target_b.target_id,
        )
        assert any(marker_b in item["text"] for item in management_result["evidence"])

        refreshed = _test_resolver(app).begin_request()
        assert refreshed.router is not None
        assert target_b.target_id in {item.target_id for item in refreshed.router.indices}
        assert target_a.target_id not in {item.target_id for item in refreshed.router.indices}

        current_mcp_query = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_b, "library": target_b.library, "version": target_b.version},
        )
        assert current_mcp_query.get("error") is None
        assert current_mcp_query["routed_libraries"] == [{
            "library": target_b.library,
            "version": target_b.version,
            "target_id": target_b.target_id,
        }]
        evidence_b = next(item for item in current_mcp_query["evidence"] if marker_b in item["text"])
        assert evidence_b["corpus_id"] == corpus_b
        assert evidence_b["index_target_id"] == target_b.target_id
        assert evidence_b["download_path"] == _download_path(corpus_b, "private-guide.txt")
        source_b = httpx.get(
            f"{base}{evidence_b['download_path']}",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            timeout=10,
        )
        assert source_b.status_code == 200, source_b.text
        assert source_b.content == bytes_b
        assert hashlib.sha256(source_b.content).hexdigest() == hashlib.sha256(bytes_b).hexdigest()

        stale_a = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_a, "library": target_a.library, "version": target_a.version},
            expect_error=_protocol_major() == 2,
        )
        assert stale_a["error"] == "forbidden"
        assert stale_a["corpus_id"] is None
        assert stale_a["evidence"] == []
        assert marker_a not in json.dumps(stale_a)

        current_mcp_catalog = _call(url=endpoint, tool="list_corpora", arguments={})
        current_sources = {
            item.get("corpus_id"): item for item in current_mcp_catalog["corpora"]
        }
        # No restart occurs: discovery and query must follow the same current route as
        # management and must not silently continue to serve the captured A snapshot.
        assert target_b.target_id in current_sources[corpus_b]["index_usable_targets"]
        assert target_a.target_id not in current_sources[corpus_a]["index_usable_targets"]
    finally:
        _stop_server(mcp_server, mcp_thread)
        qdrant.close()


@pytest.mark.integration
def test_private_account_bridge_acl_discovery_query_download_and_revoke(tmp_path):
    """Personal MCP tokens use the live management catalog and current library grant."""
    root = tmp_path / "synthetic-private-account-bridge"
    root.mkdir()
    qdrant = QdrantClient(":memory:")
    manager = FileManagementService(
        root=root,
        qdrant_client=qdrant,
        embedder=_MarkerEmbedder(),
        embedding_model=_EMBEDDING_MODEL,
    )
    store = IdentityStore(root / "data" / "identity" / "identity.sqlite")
    store.initialize()
    store.bootstrap_first_admin("private-contract-admin", "Synthetic-Admin-Password-2026!")
    admin = store.login("private-contract-admin", "Synthetic-Admin-Password-2026!").principal
    temp_a = "Synthetic-User-A-Temp-2026!"
    temp_b = "Synthetic-User-B-Temp-2026!"
    final_a = "Synthetic-User-A-Final-2026!"
    final_b = "Synthetic-User-B-Final-2026!"
    store.create_user(admin, "private-contract-a", temp_a, role="reader")
    store.create_user(admin, "private-contract-b", temp_b, role="reader")
    first_a = store.login("private-contract-a", temp_a)
    first_b = store.login("private-contract-b", temp_b)
    store.change_password(first_a.token, temp_a, final_a)
    store.change_password(first_b.token, temp_b, final_b)
    user_a = store.login("private-contract-a", final_a).principal
    user_b = store.login("private-contract-b", final_b).principal
    token_a = store.issue_mcp_credential(user_a, label="synthetic-private-user-a").token
    token_b = store.issue_mcp_credential(user_b, label="synthetic-private-user-b").token
    target, corpus_id, marker, relative, source_bytes = _private_target(
        root, qdrant, "account-private", "3.2", snapshot=False
    )
    route = _write_private_route(root, target.library, target)
    target_b, corpus_b, marker_b, relative_b, source_bytes_b = _private_target(
        root, qdrant, "account-private", "4.0", snapshot=False
    )
    # Private route files carry the selected targets. Do not let a matching
    # <target_id>.json filename become an accidental discovery prerequisite.
    target_config_dir = root / "configs" / "indexes-private"
    (target_config_dir / f"{target.target_id}.json").rename(
        target_config_dir / "synthetic-detached-config-a.json"
    )
    (target_config_dir / f"{target_b.target_id}.json").rename(
        target_config_dir / "synthetic-detached-config-b.json"
    )
    store.synchronize_resource("library", target.library, active=True)
    grant = store.set_grant(
        admin,
        resource_kind="library",
        resource_id=target.library,
        user_id=user_a.user_id,
        allowed=True,
        expected_revision=0,
    )
    management_app = create_management_app(
        root, manager=manager, identity_db=store.db_path
    )
    management_server = management_thread = None
    mcp_server = mcp_thread = None
    bridge_http = None
    try:
        management_server, management_thread, management_url = _start_server(management_app)
        bridge_http = httpx.Client(timeout=10, trust_env=False)
        bridge = ManagedFileHTTPClient(
            management_url, "", account_mode=True, client=bridge_http
        )
        app, path = _make_app(
            root=root,
            qdrant=qdrant,
            target=target,
            bearer="",
            identity_store=store,
            managed_bridge=bridge,
        )
        mcp_server, mcp_thread, mcp_url = _start_server(app)
        endpoint = f"{mcp_url}{path}"

        # The same personal credentials drive the management catalog and MCP tools.
        direct_a = httpx.get(
            f"{management_url}/api/mcp/libraries",
            headers={"Authorization": f"Bearer {token_a}"},
            timeout=10,
        )
        direct_b = httpx.get(
            f"{management_url}/api/mcp/libraries",
            headers={"Authorization": f"Bearer {token_b}"},
            timeout=10,
        )
        assert direct_a.status_code == direct_b.status_code == 200
        assert corpus_id in json.dumps(direct_a.json())
        assert corpus_id not in json.dumps(direct_b.json())

        visible = _call(url=endpoint, tool="list_corpora", arguments={}, token=token_a)
        assert corpus_id in {item.get("corpus_id") for item in visible["corpora"]}
        hidden = _call(url=endpoint, tool="list_corpora", arguments={}, token=token_b)
        assert corpus_id not in {item.get("corpus_id") for item in hidden["corpora"]}

        result_a = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker, "library": target.library, "version": target.version},
            token=token_a,
        )
        assert result_a.get("error") is None
        evidence = next(item for item in result_a["evidence"] if marker in item["text"])
        assert evidence["corpus_id"] == corpus_id
        assert evidence["index_target_id"] == target.target_id
        downloaded = httpx.get(
            f"{mcp_url}{evidence['download_path']}",
            headers={"Authorization": f"Bearer {token_a}"},
            timeout=10,
        )
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == source_bytes
        assert hashlib.sha256(downloaded.content).hexdigest() == hashlib.sha256(source_bytes).hexdigest()

        # Keep user A's library grant unchanged while changing the live private
        # route. The current personal request must follow B; knowing A's previous
        # version or target id cannot retrieve the stale target.
        _write_route_target(route, target_b)
        routed_catalog = _call(url=endpoint, tool="list_corpora", arguments={}, token=token_a)
        routed_entries = {
            item.get("corpus_id"): item for item in routed_catalog["corpora"]
        }
        assert target_b.target_id in routed_entries[corpus_b]["index_usable_targets"]
        assert target.target_id not in {
            target_id
            for item in routed_entries.values()
            for target_id in item.get("index_usable_targets", [])
        }
        current_management_catalog = httpx.get(
            f"{management_url}/api/mcp/libraries",
            headers={"Authorization": f"Bearer {token_a}"},
            timeout=10,
        )
        assert current_management_catalog.status_code == 200
        assert target_b.target_id in json.dumps(current_management_catalog.json())
        switched = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_b, "library": target_b.library, "version": target_b.version},
            token=token_a,
        )
        evidence_b = next(item for item in switched["evidence"] if marker_b in item["text"])
        assert evidence_b["corpus_id"] == corpus_b
        assert evidence_b["index_target_id"] == target_b.target_id
        source_b = httpx.get(
            f"{mcp_url}{evidence_b['download_path']}",
            headers={"Authorization": f"Bearer {token_a}"},
            timeout=10,
        )
        assert source_b.status_code == 200, source_b.text
        assert source_b.content == source_bytes_b
        assert hashlib.sha256(source_b.content).hexdigest() == hashlib.sha256(source_bytes_b).hexdigest()
        for old_target_args in (
            {"query": marker, "library": target.library, "version": target.version},
            {"query": marker, "target_id": target.target_id},
        ):
            stale = _call(
                url=endpoint,
                tool="search_docs",
                arguments=old_target_args,
                token=token_a,
                expect_error=_protocol_major() == 2,
            )
            assert stale["error"] in {"forbidden", "invalid_target"}
            assert stale["corpus_id"] is None
            assert stale["evidence"] == []
            assert marker not in json.dumps(stale)
            assert target.target_id not in json.dumps(stale)
        stale_default = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker},
            token=token_a,
            expect_error=_protocol_major() == 2,
        )
        assert stale_default.get("evidence", []) == []
        assert stale_default.get("corpus_id") != corpus_id
        assert all(
            item.get("corpus_id") != corpus_id
            and item.get("index_target_id") != target.target_id
            and marker not in item.get("text", "")
            for item in stale_default.get("evidence", [])
        )
        assert store.load_access_snapshot(user_a).allows("library", target.library)
        stale_source = httpx.get(
            f"{mcp_url}{evidence['download_path']}",
            headers={"Authorization": f"Bearer {token_a}"},
            timeout=10,
        )
        assert stale_source.status_code in {404, 409}
        assert marker.encode() not in stale_source.content

        denied_b = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_b, "library": target_b.library, "version": target_b.version},
            token=token_b,
            expect_error=_protocol_major() == 2,
        )
        assert denied_b["error"] == "forbidden"
        assert denied_b["corpus_id"] is None
        assert denied_b["evidence"] == []
        denied_download_b = httpx.get(
            f"{mcp_url}{evidence_b['download_path']}",
            headers={"Authorization": f"Bearer {token_b}"},
            timeout=10,
        )
        assert denied_download_b.status_code == 403
        assert marker_b.encode() not in denied_download_b.content

        store.set_grant(
            admin,
            resource_kind="library",
            resource_id=target.library,
            user_id=user_a.user_id,
            allowed=False,
            expected_revision=grant.revision,
        )
        revoked = _call(url=endpoint, tool="list_corpora", arguments={}, token=token_a)
        assert corpus_id not in {item.get("corpus_id") for item in revoked["corpora"]}
        assert corpus_b not in {item.get("corpus_id") for item in revoked["corpora"]}
        revoked_query = _call(
            url=endpoint,
            tool="search_docs",
            arguments={"query": marker_b, "library": target_b.library, "version": target_b.version},
            token=token_a,
            expect_error=_protocol_major() == 2,
        )
        assert revoked_query["error"] == "forbidden"
        assert revoked_query["corpus_id"] is None
        assert revoked_query["evidence"] == []
        revoked_download = httpx.get(
            f"{mcp_url}{evidence_b['download_path']}",
            headers={"Authorization": f"Bearer {token_a}"},
            timeout=10,
        )
        assert revoked_download.status_code == 403
        assert marker.encode() not in revoked_download.content
    finally:
        _stop_server(mcp_server, mcp_thread)
        _stop_server(management_server, management_thread)
        if bridge_http is not None:
            bridge_http.close()
        manager.close()
        qdrant.close()
