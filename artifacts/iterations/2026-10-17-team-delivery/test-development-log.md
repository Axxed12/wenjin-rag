# Independent delivery contract tests — 2026-10-17

## Scope and test inputs

Independent synthetic tests are in `tests/test_mcp_private_route_delivery_contract.py` and `tests/test_managed_delivery_rerank_contract.py`. Test data is generated into pytest temporary directories; Qdrant is in-memory; the MCP tests use loopback only; no private source, credential, running application service, or existing authorization record is used. The official Python FastMCP client covers the legacy SDK. The pinned official TypeScript SDK client covers MCP SDK 2.x with protocol `2026-07-28`.

The modern interpreter was located without package changes at `C:\Users\Onebiter\.codex\worktrees\integrate-mcp-stateless\rag-project\mcp-stateless\.venv\Scripts\python.exe`. Each invocation sets `PYTHONPATH` to this test worktree's `src` and runs with this worktree as cwd. The Python project interpreter is `E:\日常任务\rag-project\.venv\Scripts\python.exe`.

## Contracts added

- Both MCP eras: private route discovery, exact-version query, target/source binding, ACL deny/revoke, stale-A path after a process restart onto B, and snapshot-root SHA retrieval.
- Runtime routing: standalone candidate excluded, only one explicit route selected, ambiguous library excluded, and previously activated release with lost active pointer fails closed.
- Managed search: one shared reranker invocation reorders the actual candidate pool into top-1; policy parameters are reflected in `retrieval_config`; disabled policy doesn't invoke an instance; policy/instance mismatch fails closed; `ModelProviderError.code` remains stable.
- Upload/query overlap: query A blocks inside the reranker while replacement B reaches activation. A's result and source hash remain A-bound; replacement then completes and subsequent query and source retrieval bind B.
- Management/MCP live route: change a synthetic route file from A to B after process startup, compare live management catalog/search with MCP discovery/query. The fixture now attaches the production lazy resolver and asserts it shares the router's exact embedder, reranker, and search lock.

## Initial failures preserved

Exact initial pytest excerpts and synthetic source-hash evidence are in [`failures/private-route-initial-failures.md`](failures/private-route-initial-failures.md). Two issues reproduced against the frozen service code: denied search returned a corpus id, and a target-bound `raw_snapshot_root` download tried the mutable `raw_data` tree and returned 409 `source_modified`. Delivery core reports these two exact unchanged contracts pass after a narrow source/ACL fix; the two-version loopback rerun is underway in both SDK environments.

The live A→B test has now been repeated with the production lazy resolver. A fresh resolver request moves from A to B, MCP search returns B evidence and the exact B source bytes/SHA, and an A request returns `invalid_target` with empty evidence. However, MCP `list_corpora` still marks A's target as usable. This is a real directory-state mismatch: the tool falls back to preflighting manifest/config candidates when a corpus has no target in the current request router. Core is fixing that branch. Static low-level raw-source coverage remains separate.

## Runs so far

- Main interpreter: `tests/test_mcp_private_route_delivery_contract.py -k runtime_router` — **1 passed**, covering explicit route selection/ambiguity/release pointer loss.
- Main interpreter: `tests/test_managed_delivery_rerank_contract.py` — **3 passed**, covering managed candidate-to-final rerank, disabled/mismatched policy, stable provider exception, and overlapping upload/query version identity.
- Main interpreter before ACL/source fixes: private discovery failed because forbidden response exposed `private-wire-1.0`; raw snapshot fetch failed with HTTP 409 `source_modified`. Preserved verbatim excerpt and source hash in the failures artifact.
- Main interpreter after the core ACL/source correction: unchanged two focused contracts — **2 passed** (legacy SDK), including raw snapshot exact bytes/SHA and ordinary raw root.
- Modern interpreter after deriving the stateless `/mcp-stateless/files/...` path: focused private discovery/ACL/revoke plus raw-snapshot tests — **2 passed** (official Node TypeScript SDK), on this worktree's source with no install or upgrade.
- Production-resolver A→B run: B evidence/source SHA and stale-A rejection pass; route test still fails on old A advertised as MCP-usable. Awaiting the targeted `list_corpora` correction.
- Loopback test runs show only existing deprecation warnings from Starlette `BlockingPortal` and Qdrant `recreate_collection`; neither changes assertions.

## Pending

- Managed reranker failure now has a true loopback contract: the production management `/api/search` route feeds a deterministic failing reranker, `ManagedFileHTTPClient` reads that endpoint, and MCP tool calls cross the bridge. Main FastMCP and MCP SDK 2.x runs both passed; direct HTTP was 503 with `model_queue_full` / `not_retrieved`, and MCP returned `isError=true`, the same stable code, empty evidence, and no upstream sentinel or source text.
- After core's latest stale-private-route handling, the live A→B management/MCP test passes in both SDK environments. An inactive A query returns `forbidden` with no corpus id, evidence, or A marker; current B discovery/query/source bytes and SHA remain bound to B. The account-route extension keeps the user's same library grant active while switching the live route, detaches both target JSON filenames from target ids, checks the current management catalog bridge, probes A by library/version and target id, tests default routing, and verifies B retrieval/source download. It is awaiting rerun because core's cancellation wiring was being edited concurrently.
- Resolver regression checks: `test_library_release_runtime.py -k prepared_only_catalog` passed in the modern interpreter, including prepare-only legacy fallback and ever-activated lost-pointer fail-closed. A full run reached 3 passed / 1 failed: the old `test_explicit_embedding_model_id_matches_management_and_mcp_runtime` fixture replaced `Settings` with a lambda although current runtime calls `Settings.for_project_root`. Updated that test fixture to patch the current API; rerun is deferred until core's full freeze. The stateless identity-release test's old hard-coded Node SDK location was likewise made env-overridable (`MCP_MODERN_NODE_CLIENT_DIR`) without changing its default; rerun is deferred.
- The generated-only long-lived fixture is running for the delegated fresh-agent test: PID `40540`, endpoint `http://127.0.0.1:65145/mcp`, descriptor (including its synthetic bearer) at `%TEMP%\wenjin-team-delivery-fixture-state-8d4bdcf9a2b546fb80334cb635d5510c\fixture.json`, stop file `%TEMP%\wenjin-team-delivery-fixture-state-8d4bdcf9a2b546fb80334cb635d5510c\stop`. Give the fresh answer agent only endpoint, bearer, and query; do not expose fixture source, descriptor metadata, or expected answer.
- OpenCode 1.19.3 `mcp list --pure` connected to the temporary endpoint and reported `synthetic-wenjin connected` (exit 0). Claude Code 2.1.296 had two outcomes with the CLI override mechanism: an empty `--setting-sources` value was rejected; valid `project,local` reported no configured servers. Following root's precise correction, one isolated project `.mcp.json` plus temporary `.claude/settings.local.json` (`enableAllProjectMcpServers=true`) reached the endpoint but reported `Pending approval (run claude to approve)`. This is partial discovery only, not a connected/approved session; no further attempt is planned without authorized model use. The checker now labels this outcome partial even when CLI exit is zero.
- Exact CLI output, with the temporary bearer omitted: OpenCode: `✓ synthetic-wenjin connected` at `http://127.0.0.1:65145/mcp`; Claude corrected attempt: `Checking MCP server health…` then `synthetic-wenjin: http://127.0.0.1:65145/mcp (HTTP) - ⏸ Pending approval (run claude to approve)`. The initial invalid-source error and no-config output are captured in the test run above; no credentials were emitted.
- Fixture cleanup: descriptor PID `39836` was alive before stop, the parent launcher PID `40540` was observed separately. Creating the fixture's stop marker removed the descriptor and synthetic corpus; PID `39836` was no longer alive and port `65145` had no listening socket. Parent launcher PID `40540` also exited. Root independently checked the fresh-agent source SHA `13f27219e639223f5ca01e72de4f9f6a8449b98585a94a52e03ed6350cab5097` against the descriptor; no expected answer or fixture source was sent to that agent.
- Frozen private-route source hashes supplied by core: `runtime.py` `654CCEDD2040B94791C02EA682655DB87588DFC73DF4FBFBC51D3E9B13AF7963`; `tools.py` `78A6CD057F26ED6C284B420BA79ADF36A7B25516163309BA040D16422225FCCB`; `server.py` `5F7D8A2D30174109A9E484E7F3F3E2E916DF882B5A1985152F625CDD5337BB0F`; `releases/resolver.py` `996BF3FD7C3BF09F0E15B9648F8AF8C46A7324AED2CEF4153272E674F983CB75`.
- Current independent artifact SHA-256 (may change after the deferred final contracts): managed contract `tests/test_managed_delivery_rerank_contract.py` `10C626376D646A600EEB40EC51F556D658755BC768B58DA8FDD5A6E9183B60F1`; private delivery contract `tests/test_mcp_private_route_delivery_contract.py` `CBCC1944047AF30078AADDEB914DF18F291E1710F3028FA6D1985A75C0A27A9D`; fixture `tests/acceptance/team_delivery_mcp_fixture.py` `CAB792B1EB686CED99D1831A331536E0DFD78126F57A42C587BFC9827FC0510C`; CLI checker `tests/acceptance/run_team_delivery_cli_checks.py` `27854B7B50978E39A382D55DB3B4A6FBBDACA426D230DD5395273E3881987B48`.
- The expanded persistent-grant A→B account test passed discovery, B route usability, live management catalog B identity, B search and exact source bytes/SHA, and stale A version/target rejection before its default-route assertion failed because the returned payload echoes the caller's query string (with empty evidence). The assertion now treats echoed query text as request metadata and instead requires no A corpus/target/evidence; rerun after core's full cancellation freeze. This is a contract-fixture correction, not an observed A evidence leak.
- Python compilation passed for both fixture/checker scripts and all edited test files. No business source was changed by this test agent.

## Next targeted work

- Wait for core to declare the second cancellation/reranker batch frozen. Then rerun the account A→B test in both SDK environments, managed reranker wire test in both SDK environments, the focused route test in both SDK environments, `test_library_release_runtime.py`, and the selected modern stateless identity/release test. Do not run the broad historical suite.
- After core's full cancellation batch freeze, run the cancellation multi-batch barrier contract, persistent-grant account A→B contract in both SDK environments, managed reranker wire contract in both SDK environments, and only affected release tests. Do not rerun OpenCode or expand CLI troubleshooting.
- Record final test source hashes and business source hashes after the full freeze.

## Final frozen-source verification (supersedes pending items above)

The cancellation/reranker batch was frozen by core before this verification. The MCP route files retained the supplied frozen hashes: `runtime.py` `654CCEDD2040B94791C02EA682655DB87588DFC73DF4FBFBC51D3E9B13AF7963`, `tools.py` `78A6CD057F26ED6C284B420BA79ADF36A7B25516163309BA040D16422225FCCB`, `server.py` `5F7D8A2D30174109A9E484E7F3F3E2E916DF882B5A1985152F625CDD5337BB0F`, `releases/resolver.py` `996BF3FD7C3BF09F0E15B9648F8AF8C46A7324AED2CEF4153272E674F983CB75`. Final changed-source hashes used for the second batch: `file_management/service.py` `6F14E08CDBA4F57718BBCEB2B3E65C2231BF60A6F575E960F2AAE9BC89B26F73`; `versions.py` `9F99349A8BF2F944341FD9DBED5EA4EFAC247E5419B3A955FF5EE65329980C51`; `jobs.py` `C4411F9040D02455C6920FF0332D2E01A6CFB23A8285DC59C593D9048F27816A`; `backends.py` `8230BEDBAF29FDCE712594B58BD48D0B74C73AF80FDD4F21071C7C89C2296658`; `indexing/build.py` `5BCF1FB08E16CA5C06DEE9AB4CC97BE9EA7EA83C60C34BFC6EA241D14B982A79`; `management_http/jobs.py` `EBDC7B7AC4B61CBC9BC635C8E0BFD094E4CC3C12FB94D40AABA964B611E835DB`; `stateless_server.py` `89DCDEE2C1CA38C726A7ED05C59900228043C8532A3F91C0FCD695E57A4C7797`.

Focused primary-interpreter verification in the current worktree: the combined delivery selection reported **27 passed, 1 failed, 11 deselected**. The one failure was an outdated test double whose `Settings.embedding` lambda did not accept the current `root=` keyword; after adapting only that fixture, the affected test passed. A final focused batch then reported **16 passed** for cancellation, custom-adapter locking fallback, all `test_file_management_api_entry.py`, all `test_library_release_runtime.py`, backup/restore, and management/legacy-MCP `--project-root` launcher contracts. A separate HTTP queue-wait contract reported **1 passed**. These batches overlap; counts are not additive. No frozen dataset or full historical suite was run.

Focused modern-interpreter verification with `MCP_MODERN_NODE_CLIENT_DIR` set to the existing pinned SDK fixture directory reported **4 passed**: persistent account-grant private route A→B, live management/MCP route A→B, management HTTP → `ManagedFileHTTPClient` → stateless MCP reranker error, and the existing stateless account/version contract. The first modern attempt used a nonexistent nested SDK directory and produced a harness `NotADirectoryError`; the corrected existing directory passed. No dependency was installed or upgraded.

Cancellation observation: while the first embedding batch was blocked, loopback `POST /api/jobs/{job_id}/cancel` returned 202 with `status=running` and `cancel_requested=true`. Once released, a later embedding batch still ran; cancellation became terminal only after candidate build returned. The job then cleaned its candidate collection, BM25 file, and version directory; the previous active version remained searchable with its original source SHA. This verifies nonterminal request semantics and eventual cleanup, and records the current full-build cancellation delay. It does not establish per-batch cancellation.

HTTP scheduler timing uses the existing one-slot `worker_semaphore`, synthetic uploads, and instrumented `run_job` entry timestamps. The raw monotonic timeline is in [`http-queue-timeline.json`](http-queue-timeline.json): B was accepted at `251210.031`, A released the slot at `251210.156`, and B entered `run_job` at `251210.187`, for `0.15600000001722947` seconds from B acceptance to worker entry. This is one synthetic barrier observation, not a general queue-capacity or model/GPU latency conclusion.

The Claude 2.1.296 result remains **discovered / pending approval**; connection and tool invocation were not proven. The OpenCode 1.19.3 isolated check reported connected. The generated-only fixture was stopped, its descriptor deleted, its recorded server PID exited, and the loopback port closed. The corrected manual setup and cleanup guidance is in [`cli-agent-fixture.md`](cli-agent-fixture.md). A separate fresh GPT-6 Luna agent's answer/retrieval evidence is documented independently in `agent-answer-check.md`, not treated as CLI or golden-answer evidence.

Final independent test artifact hashes after ruff cleanup: `test_managed_delivery_rerank_contract.py` `504DD9CE8F024A2591CD3D2FA5DAE2B4664CA1007D3087636E9080D82EFCDD29`; `test_mcp_private_route_delivery_contract.py` `5C8B12DF1C09E96F34252B79B5B3D86DF047BE28F27C27994E3D652B043D6ADF`; `test_file_management_api_entry.py` `50A506FF9B835DED87185A7C670695B204E0D69BB44689EE4006C3549DD294E1`; `test_library_release_runtime.py` `DA2A97ECF4311D1C2144C12BE5792245270DD7790346C2EB9FC69BDFE3CED883`; `test_mcp_stateless_identity_release.py` `E1C492C27528037D07407AE94CDA487BD6957A0E8D6749086268449760774037`. Queue timeline SHA-256: `741DAC59CD6084F2251A857D706BE99587A70E28E0F67FD3E73E5BB27951AB82`; CLI fixture guide SHA-256: `98993364928E4CE9CF8006D26FB8D4F5336FF0AD6778288E7B232F59F916B727`. `ruff check`, `py_compile`, and `git diff --check` passed for the independent test/doc edits; no business code was changed by this agent.

## Final pytest command record

The complete stdout was not separately saved to a file. The commands and pass/fail summaries below are transcribed from this task's tool-session results; the broad historical output is not reconstructed.

Primary interpreter, combined delivery selection (**27 passed, 1 failed, 11 deselected**; the single failure was the subsequently corrected Settings test double):

```powershell
$env:PYTHONPATH='C:\Users\Onebiter\.codex\worktrees\team-delivery-20261017\rag-project\src'
& 'E:\日常任务\rag-project\.venv\Scripts\python.exe' -m pytest tests/test_mcp_private_route_delivery_contract.py tests/test_managed_delivery_rerank_contract.py tests/test_file_management_service.py tests/test_library_release_runtime.py tests/test_team_delivery.py -k "not offline_manifest and not unsafe_asset_paths and not launcher_logs_exit_and_redacts_inherited_secret_without_command_or_env and not docker_diagnostic and not tampered_and_unlisted and not sqlite_backup_api and not backup_manifest_describes" -q
```

Primary interpreter, corrected focused release/settings fixture plus cancellation, custom-adapter fallback, complete management API-entry and release-runtime files, backup/restore and launcher contracts (**16 passed**):

```powershell
$env:PYTHONPATH='C:\Users\Onebiter\.codex\worktrees\team-delivery-20261017\rag-project\src'
& 'E:\日常任务\rag-project\.venv\Scripts\python.exe' -m pytest tests/test_managed_delivery_rerank_contract.py::test_http_cancel_during_first_embedding_batch_waits_then_cleans_candidate_and_keeps_old_active tests/test_managed_delivery_rerank_contract.py::test_custom_index_adapter_without_batch_lock_capability_keeps_whole_build_lock tests/test_file_management_api_entry.py tests/test_library_release_runtime.py tests/test_team_delivery.py::test_backup_restore_recovers_login_acl_managed_version_release_and_local_qdrant tests/test_team_delivery.py::test_management_launcher_passes_project_and_identity_database tests/test_team_delivery.py::test_legacy_mcp_launcher_passes_project_root_without_exposing_secrets -q
```

Modern interpreter, corrected existing pinned Node SDK directory (**4 passed**):

```powershell
$env:PYTHONPATH='C:\Users\Onebiter\.codex\worktrees\team-delivery-20261017\rag-project\src'
$env:MCP_MODERN_NODE_CLIENT_DIR='C:\Users\Onebiter\.codex\worktrees\integrate-mcp-stateless\rag-project\tests\acceptance\mcp-stateless-client'
& 'C:\Users\Onebiter\.codex\worktrees\integrate-mcp-stateless\rag-project\mcp-stateless\.venv\Scripts\python.exe' -m pytest tests/test_mcp_private_route_delivery_contract.py::test_private_account_bridge_acl_discovery_query_download_and_revoke tests/test_mcp_private_route_delivery_contract.py::test_private_route_switch_is_consistent_between_live_management_and_mcp tests/test_managed_delivery_rerank_contract.py::test_reranker_provider_failure_crosses_management_http_bridge_and_both_mcp_eras tests/test_mcp_stateless_identity_release.py::test_stateless_mcp_rechecks_identity_grants_and_managed_versions -q
```

Primary interpreter, one HTTP scheduler queue-wait observation (**1 passed**; `-s` emitted the raw timeline stored in `http-queue-timeline.json`):

```powershell
$env:PYTHONPATH='C:\Users\Onebiter\.codex\worktrees\team-delivery-20261017\rag-project\src'
& 'E:\日常任务\rag-project\.venv\Scripts\python.exe' -m pytest tests/test_file_management_api_entry.py::test_http_scheduler_records_queued_job_wait_behind_single_worker -q -s
```
