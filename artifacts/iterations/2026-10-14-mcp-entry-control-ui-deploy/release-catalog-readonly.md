# Stateless release catalog read-only fix

## Change

Stateless MCP now opens `ReleaseCatalog` with SQLite URI `mode=ro` and `PRAGMA query_only=ON`. Read-only selection is threaded through startup runtime resolution, the initial multicorpus router snapshot, the lazy per-request release resolver, and release-pinned source downloads. The constructor does not create a missing database or parent directory in this mode. Existing stateful MCP and management write paths retain the default writable catalog behavior.

## Evidence

- `uv run pytest tests/test_library_release_domain.py tests/test_mcp_stateless_identity_release.py tests/test_mcp_release_identity_tcp.py -q`: **15 passed, 1 skipped**.
- `uv run ruff check src/wenjin/releases/catalog.py src/wenjin/mcp/runtime.py src/wenjin/releases/resolver.py src/wenjin/mcp/download.py src/wenjin/mcp/stateless_server.py tests/test_library_release_domain.py`: **passed**.
- `uv run pytest tests/test_mcp_stateless_protocol.py tests/test_mcp_stateless_launcher_runtime.py tests/test_mcp_stateless_era_isolation.py tests/test_mcp_stateless_container_tls.py -q`: **4 skipped** by existing environment/marker conditions; no protocol-container acceptance is claimed.
- Isolated SQLite test verifies reads succeed, writes raise `OperationalError: attempt to write a readonly database`, and a missing catalog path is not created.
- An isolated WAL fixture with an open writer and uncheckpointed WAL data was read successfully through the read-only catalog; database, `-wal`, and `-shm` sizes were unchanged by the query.
- The current project catalog was queried read-only: revision `1`, one library. Database size/mtime were unchanged and no `-wal`, `-shm`, or `-journal` files appeared. This observes the host-side current catalog only; it does not prove Windows-host to Linux-container WAL locking behavior.
- Broader targeted command `uv run pytest tests/test_remote_identity_bridge.py tests/test_mcp_stateless_identity_release.py tests/test_mcp_release_identity_tcp.py tests/test_library_release_runtime.py tests/test_default_multicorpus_route.py -q`: **8 passed, 1 skipped, 2 failed**. The failures are unrelated existing test assumptions: a `Settings` constructor monkeypatch drops `for_project_root`, and a multicorpus fixture writes `{}` where `create_router` requires its schema/candidate fields.
- Both failures were rerun from a temporary detached `HEAD` worktree at `159a4a4`: the same two node IDs again failed with the same errors (**2 failed**). The temporary worktree was removed afterward.
- The mixed stateless profile now reads the existing Qdrant API key through the already-installed `with-secrets` entrypoint. Compose requires `WENJIN_QDRANT_API_KEY_FILE` for the Docker secret source; no key value is emitted into the environment/config output. `docker compose ... config --quiet` passed with file paths supplied.

## Runtime boundary

No live service or container was started, stopped, or rebuilt by this agent. The deployment owner reported a fresh stateless image startup passed the read-only catalog initialization and then failed Qdrant admission with HTTP 401 due to the previously empty API key; the profile secret wiring addresses that reported configuration mismatch. Docker Desktop bind-mount/WAL behavior still needs a read-only probe in the disposable stateless container before claiming complete runtime acceptance. No release-directory permissions were widened and no catalog copy was made.
