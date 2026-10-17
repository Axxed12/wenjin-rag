# Windows delivery and recovery implementation log

Date: 2026-10-17
Scope: isolated worktree delivery tooling and synthetic recovery contracts. No live service, real identity database, private credential or original business document was opened.

## Implemented

- Added a Windows 10/11 Docker offline asset manifest with explicit host/Desktop/Engine versions, per-asset versions, sizes and SHA256; sensitive path patterns and unsafe relative paths are refused.
- Added a hash-indexed ZIP backup and verify/restore flow. The archive records the source project root and the detected managed raw layout. Restore validates every member before writing to a new destination, uses same-volume staging and atomic rename, performs SQLite quick checks, and rebases the managed version ledger only for the two layouts present in `deploy/compose.base.yaml` and `deploy/compose.stateless.yaml`: `raw_data` and `data/file-management/raw_data`.
- Added owned-container diagnostics requiring the exact delivery run label, with allowlisted inspect fields and bounded/redacted optional logs.
- Added fixed management and legacy-MCP loopback launch wrappers that record only service identity, safe bind/config identity, PID, start/end UTC and exit code. The wrapper does not attach to or stop existing processes, and never saves argv or environment.
- Added tests under `tests/test_team_delivery.py`; no MCP/retrieval or business implementation files were changed.
- Linked the root-provided read-only local model/image inventory from the operator guide. That inventory is not a portable delivery bundle and does not represent target-machine startup evidence.

## Failure evidence and correction

1. First recovery fixture used a hand-sealed release row. Root review found its target and validation were synthetic metadata rather than a usable sealed release. Replaced it with the product `ReleaseService.prepare` + `activate` path, then added restored `ReleaseService.validate`, `RequestReleaseResolver` materialization/search, and resolver source retrieval.
2. The first real release restore failed validation because SQLite Backup API rewrote immutable release BM25 bytes, changing the byte-level seal hash. Backup now uses SQLite Backup API for mutable databases and copies immutable BM25 SQLite files byte-for-byte. The sealed release validates after restore.
3. The initial restore rebasing logic accepted only `raw_data`. Added a source-root/layout binding in the backup manifest and bounded rebasing for the native and Docker managed-raw layouts. Both synthetic layouts now recover into a new root.
4. The first identity exercise checked the store directly. Replaced it with management HTTP `/api/auth/login`, `/api/auth/me` and `/api/admin/search`; authorized reader succeeds on v2, a stale version ID receives 409, and a separate reader without a grant receives 403.

## Validation

Command:

```powershell
$env:PYTHONPATH='C:\Users\Onebiter\.codex\worktrees\team-delivery-20261017\rag-project\src'
& 'E:\日常任务\rag-project\.venv\Scripts\python.exe' -m pytest -q tests/test_team_delivery.py
```

Result: `13 passed, 9 warnings in 3.91s`. Warnings are existing Starlette `BlockingPortal` and Qdrant local client `recreate_collection` deprecations. The two full recovery parametrizations cover native and Docker-layout managed raw storage; each uses synthetic identity/users/files/versions, real release service sealing and activation, local persistent Qdrant, HTTP authorization/search, stale version rejection, release resolver search and source read.

Ruff check passed for the launcher, compatibility shim and delivery tests; `git diff --check` passed for owned files; direct `scripts/team_delivery.py --help` listed all seven commands. The test does not represent an independent Docker Qdrant named-volume restore, clean target-machine Docker Desktop offline import/start, GPU model startup or production semantic acceptance.

## Existing evidence reused

- `docs/dual-deployment.md`: existing Docker named-volume backup snippets; its round-trip marker check is not a full application recovery proof.
- `docs/file-management.md`: existing file manager artifacts include identity/manager state, version manifests and indexes; it does not provide this restore flow.
- `artifacts/iterations/2026-10-14-mcp-entry-control-ui-deploy/development-log.md`: prior native 18777/18765 stopped-state observation and restore evidence did not establish exit root cause. This round adds a bounded future-start logger; it does not infer a historical cause.

## Remaining acceptance

- Clean Windows 10/11 Docker Desktop offline import/start remains pending and must be run on the target machine.
- Independent Docker Qdrant named-volume restore followed by temporary management/MCP API business request remains pending. The implemented E2E closes the filesystem/embedded Qdrant Local path only.
- Candidate client questions and AI answer human review remain with the root/test workstream; test transport or HTTP success is not answer-quality approval.

## Follow-up delivery preparation

- Corrected the operator example to use the manifest tool's POSIX relative path syntax. Clarified that mutable identity/file-manager/release coordination SQLite files use SQLite Backup API snapshots, while sealed BM25/index SQLite assets are byte-copied so the release seal hash remains valid.
- Fixed `prepare_docker_delivery_fixture.py`: a supplied freeze hash validates staged content but no longer implicitly runs Docker; Docker build requires explicit `--build` plus the expected source hash.
- Added `build_delivery_images.py` to prepare source-bound image contexts for the inventoried backend, model-node and stateless-MCP base images. It has an explicit `--build` gate, checks the three base image IDs against the read-only inventory, uses `docker build --network=none`, and records resulting image IDs when built. The current turn only ran its context-staging smoke against temporary source files; no Docker command, image build or container startup was run.
- Ruff and Python compile checks passed for the added Docker preparation scripts and synthetic model API. A temporary fixture check confirmed that supplying a matching freeze hash to `prepare_docker_delivery_fixture.py` does not invoke Docker unless `build=True`.
- Parsed the generated temporary Compose YAML and checked its internal-only network shape; the temporary random-secret fixture directory was removed at test exit. This is syntax/structure evidence only, not `docker compose config` or runtime validation.
- Updated the Windows operator guide with the source-bound image preparation/export workflow. All image tags and measured target Docker Desktop/Engine values remain placeholders until the core source freeze and actual measurement. Clean Windows 10/11 offline import/start and the Docker/Qdrant Server snapshot restore business round-trip remain pending the parent's freeze signal.
- Corrected the backup manifest's `sqlite_scope` description and added a focused contract asserting both manifest scope and the mutable-manager/immutable-sealed-BM25 per-file methods. Targeted test passed.
- The source-bound Dockerfile copies frozen source to `/app/src` and scripts to `/app/scripts`; the intended post-freeze image smoke will assert `wenjin.mcp.runtime.ROOT == /app`, verify the runtime module's SHA256 against the staged source row, then run management or stateless-MCP `--help` with `--network=none`. This smoke is defined but not run.
- Added a `.dockerignore` to the synthetic Docker build context so generated secret files and Compose/runtime fixture metadata are excluded from the Docker build context.
- Extended the generated Compose fixture with a `restore` profile containing a second Qdrant volume, restored management service and restored legacy-MCP service. The application volume mounts keep the product root at `/app`; the restored services read the recovered project under the temporary shared snapshot volume and connect only to the new Qdrant service. A temporary YAML staging check verified the internal-only network, service roots, and secret exclusion. No Docker command was run.
- Added a narrow Qdrant Server snapshot export/import helper using the matching Qdrant client and HTTP snapshot download. It records per-snapshot size/hash, verifies those before upload into a new server, and checks the recovered collection names. Only Python compile, Ruff and `--help` were run; upload API compatibility and real snapshot recovery remain pending the isolated Docker run.
- Tightened the Qdrant helper so its inventory identity rejects URL credentials and non-origin paths/query/fragment; the restore validates every manifest path and hash before creating a Qdrant client. Added quick tests for these contracts. `pytest tests/test_qdrant_server_snapshot.py tests/test_team_delivery.py -q`: 18 passed, 9 deprecation warnings; targeted Ruff and `git diff --check` passed. No Qdrant Server or Docker was contacted.
- Fixed management launcher wiring to match existing CLI configuration semantics: it passes `WENJIN_IDENTITY_DB` when configured, otherwise leaves account mode disabled (`identity_db=None`), and passes `WENJIN_FILE_ADMIN_TOKEN` only from the child environment. A focused monkeypatched launcher test confirms root/identity argument wiring and that token values are absent from argv and evidence.
- Core added the legacy MCP CLI `--project-root` flag while preserving its default root; the launcher now passes that explicit root, and a monkeypatched contract checks it while keeping identity/API key values out of argv and logs. No environment mutation or copied source workaround was used.
- Final scoped checks for this metadata/launcher pass: three targeted delivery contracts passed (management env identity, legacy MCP root forwarding, and SQLite scope/method metadata), MCP CLI `--help` shows `--project-root`, Ruff, compileall and `git diff --check` passed. The historical full 13-test suite was not rerun because the implementation changes are covered by focused contracts.

## Isolated Docker delivery and restore drill

Date: 2026-10-17. Core source was frozen before this drill; no product source was changed. All service calls used generated synthetic users/documents and a deterministic synthetic model API on an internal-only Compose network.

- Built and smoke-checked three source-bound images from source SHA256 `639794c5b660851559d2fc0894d80356f9528f381e90cfe027e58c33463a2ab6` (207 source files). Backend ID: `sha256:4b512b9089f7aaf2fec408299d126131b6992e0ba79d5110cc9678853b461782`; model-node ID: `sha256:a30f3508fb90117fdb99748e101f3f94c4f50c3d2e0522a4684eaff676f09373`; stateless-MCP ID: `sha256:3e684d3b0ba20a3745a4ac0dfeb5c31ea51c338580572f416da05f016dbf9ff6`. Backend/stateless MCP smoke verified `/app` runtime root, module path/hash and CLI `--help`; model-node smoke was import-only, with no inference/GPU load.
- A first MCP start failed with `FileNotFoundError` for restored `data/indexes/index-registry.json`. Added only temporary synthetic OSG registry and real `ReleaseService.prepare`/`activate` assets with matching deterministic embedding model ID/dimension, then exported a fresh Qdrant snapshot and regenerated the backup. The initial failure log is preserved under `docker-evidence/`.
- Final backup SHA256 `1fcad69eef302da2072890549cfc7253611b04b2588f8eb692aca6c133a82d77` contained 42 verified files / 1,313,320 bytes; restore recovered 42 files and rebased two managed raw versions. Source and restored identity databases had equal password scheme/salt/hash fields for each of the three synthetic users (only per-user equality booleans were retained). First restored API probe omitted `Origin` and received `403 request_rejected` / same-origin required before authentication; adding `Origin: http://127.0.0.1:8778` fixed the harness. Reader and denied-reader seed passwords were derived in memory by appending the seed's `-final` suffix; no database password reset occurred.
- Qdrant Server snapshot manifest SHA256 is `8ff95c1f92c5358e5106b8bb095bfe198356df04bb01fe4555d92e8b44c3b839`. The newly created restore server was checked empty (`[]`) before import; five snapshots restored five collections, one synthetic point each. The helper is not an overwrite workflow for an existing Qdrant server.
- Restored HTTP evidence: health 200; admin/reader/denied-reader login succeeded; granted reader active managed search succeeded; stale version returned 409; reader without a library grant received 403. Actual FastMCP Streamable HTTP calls listed `list_corpora`, `list_managed_files`, and `search_docs`; the reader token retrieved managed v2 only, while an admin token retrieved activated OSG release A only. No OSG grant was added to the reader. This was internal Docker networking only; host-published-port/cross-machine access, clean target-machine offline start, real model inference and GPU operation remain unverified.
- Cleanup used the exact Compose project/run ID. The default-profile `down -v` initially left restore-profile containers/volumes in use; repeating with `--profile restore down -v --remove-orphans` removed all owned containers, network and volumes, verified absent afterward. Removed only the synthetic fixture image; retained the three source-bound delivery images for export. Existing project services and cached base images were untouched. Deletion of temporary `.env` and three generated credential files under `C:\Users\Onebiter\AppData\Local\Temp\team-delivery-e2e-8a762c8e` was blocked by automatic approval policy; no alternate deletion method was attempted and no values were recorded. Exact paths are listed in the acceptance JSON.
- Sanitized results and the snapshot manifest are in `docker-evidence/docker-acceptance.json`, `docker-evidence/restored-business-report.json`, and `docker-evidence/qdrant-server-snapshot-manifest.json`. No password, token, raw SQL, real backup or company source document is stored there.
