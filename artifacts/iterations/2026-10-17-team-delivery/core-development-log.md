# 2026-10-17 team delivery core development log

## Scope and boundary

This is the isolated business-source pass in `C:\Users\Onebiter\.codex\worktrees\team-delivery-20261017\rag-project`. It addresses (1) native stateful/stateless MCP access to explicitly routed private indexes and (2) managed retrieval using the already configured reranker/search policy. It does not read real private configs, corpus files, credentials, identity data, running services, or GPU state. No code.md/index update, staging, or commit.

## Changes

- `LegacySearchService._private_config_view_for_root(root)` now exposes the existing private-route interpretation without constructing a service. `runtime.create_router()` adds only a single explicitly selected private target for a library that has not been taken over by release state. Bare candidates, ambiguous libraries, and release-owned libraries remain out of the legacy route. Full target admission remains delegated to `MultiCorpusRouter.from_targets`, which uses the existing Qdrant client, embedder, reranker, and model lock. Both MCP protocol adapters call the same router/runtime path.
- `list_corpora` already derives entries from `corpus_catalog_report`, which enumerates both `corpora/` and `corpora-private/`; with the private target now present in the request router, its existing active-target and ACL filters can discover the same private corpus that `search_docs` routes. No separate directory or ACL implementation was added.
- `FileManagementService.search` gained optional `reranker` and `search_policy` keyword arguments. Omitting them preserves existing callers and `SearchConfig` defaults. When supplied, the policy builds the request config and must agree with the passed reranker instance; the same manager `_model_lock` is injected into `RetrievalService`. `management_http.search` passes the app's existing shared reranker only when the policy enables it, and passes the existing policy to admin, reader, and MCP managed search. Policy metadata is added to the managed result. The app keeps ownership and shutdown of the shared model/provider; this change creates no second provider, reranker, or queue.
- Managed retrieval continues to bind file, library, active `version_id`, source SHA, and returned evidence under the same snapshot and second source check. Existing HTTP error mapping (`ModelProviderError` to 503 stable code), managed bridge decoding, and MCP typed provider error conversion remain on the request path.

## Checks and status

- Python `py_compile` passed for `mcp/runtime.py`, `mcp/tools.py`, `retrieval/legacy_catalog.py`, `file_management/service.py`, and `management_http/search.py`, using the supplied project Python and worktree `PYTHONPATH`.
- `git diff --check` exited 0. Git printed pre-existing CRLF-to-LF notices across the copied dirty checkout; no whitespace error was reported.
- No business tests were run in this pass; the independent test agent was given the planned contracts and file/signature changes. No real config, corpus, model, service, or GPU was accessed.
- Current status: implementation awaiting independent synthetic contracts. Next: incorporate test findings, then freeze business files and notify root/tester. Separate follow-up requested by root: measure query/build/mixed concurrency and examine cancellation/resource-release semantics; this pass does not change build/model lock boundaries.

## Follow-up implementation detail

- The request router's migrated-library set also includes catalog entries with `ever_activated=true` and no active pointer. These remain excluded from default/private legacy routes so a lost active pointer cannot silently restore an old index. `list_corpora` now uses the same request router's target identity for legacy/private `index_state`; this avoids reporting an explicitly routed private target as `unusable` merely because its config lives outside the public index-config scan.
- Repeated static verification after these changes: five-file `py_compile` passed, five-file `ruff check` passed, and `git diff --check` exited 0 (with repository-wide CRLF notices from the dirty copied checkout).

## Business-source freeze

At root direction, the five owned business files are frozen for independent synthetic review. SHA-256 at freeze:

- `src/wenjin/mcp/runtime.py`: `654CCEDD2040B94791C02EA682655DB87588DFC73DF4FBFBC51D3E9B13AF7963`
- `src/wenjin/mcp/tools.py`: `13041019C422B938BA4FB0723649C407D2AFFC60E7916F6BA48F84171B5B753D`
- `src/wenjin/retrieval/legacy_catalog.py`: `729FDA5E888E1B40909232F3ACC82A92BCC727C5C1B0D4E4B0F28002C158395A`
- `src/wenjin/file_management/service.py`: `58324537646A76FB845540CF03CB3BA38C0E26CD85483B5B26A791C829F40885`
- `src/wenjin/management_http/search.py`: `3E406A5EDE3CEE4387E73F52FA9E9290A1B6222548C0D66F46478BA4FC059014`

Independent test agent owns tests; do not edit these files during their review unless a specific failing contract identifies a concrete defect. The Markdown log is covered by the repository `*.md` ignore rule and is intentionally not staged.

## Second-batch freeze: cancellation, lock scope, CLI project root

The second batch is now wired. `POST /api/jobs/{job_id}/cancel` persists `cancel_requested`; queued imports transition into cleanup and report `cancelled` only after owned staging/candidate resources are removed. Running imports stay `running` with `cancel_requested=true` until the synchronous build/model work returns to a safe checkpoint. The worker then cleans only its owned candidate/staging state and marks the job cancelled; it does not interrupt a model call or remove the previous active version. Startup turns a running requested cancellation into retryable cleanup state, and cleanup failure remains retryable through another cancellation request/restart. Existing custom `IndexAdapter` implementations retain the previous full-build model lock unless they explicitly declare `supports_embedding_batch_lock`; the standard Qdrant/BM25 adapter declares that capability and locks each embedding batch. The MCP server CLI accepts `--project-root`, falling back to `WENJIN_PROJECT_ROOT` and then the existing runtime root.

Temporary synthetic cancellation checks passed for: queued cancellation; running cancellation held until a blocked build was released; old active pointer retained; candidate/staging cleanup; and cleanup-failure retry. The targeted API-entry test `test_upload_temp_cleanup_and_serial_worker_does_not_block_status` passed after adapting the existing `FakeManager.submit_import` fixture to accept `original_filename=None`; this was test-fixture compatibility only, with no production API/behavior change. Python `py_compile` passed on the owned MCP/release, file-management, management HTTP, indexing, and benchmark files; scoped `git diff --check` exited 0. The full independent contract suite is still pending; these are not end-to-end acceptance claims.

### Synthetic contention evidence

Matched synthetic runs use the same fixture SHA `e5db06c1e3e81e6f04d533fb40504c37390ac4b8a77dde510cd3dfc8350f56d4`, deterministic 30 ms per model operation, concurrency 4, two mixed queries, 3 rounds, candidate_k=10, rerank_pool=30, top_k=3, and production admission values max_waiting=8/max_wait_seconds=30. Before: `contention-before-final-3rounds/managed-contention.json` (`a12f3b5c0a694b0a9cb4c205dd009cce`), with lock behavior reverted in an isolated source overlay. After: `contention-after-final-3rounds/managed-contention.json` (`5030a293dac14f3c9bb54c64174da395`), with per-batch embedding lock. Each pair contains single/concurrent query, build-only, and mixed scenarios; all reached their barriers and returned `ok`.

Across the three mixed rounds, median old-query model-lock wait fell from about 49.6 ms to 24.7 ms, and that query's median elapsed fell from about 124 ms to 107 ms. The second query's manager read/write-lock wait fell from about 124 ms to 107 ms. Mixed wall time changed from about 328 ms to 317 ms; build-only median changed from about 238 ms to 240 ms. Treat this as synthetic causal evidence of reduced old-query blocking under a concurrent build, not a throughput or GPU performance claim. `job_queue_wait_seconds` is null because this harness calls `manager.run_job` directly and does not start the HTTP scheduler; stage timings overlap and must not be summed. The first exploratory harness attempt stalled and was excluded from the results; its original process/session identifier and a definitive cause were not preserved. The successful reruns have no lingering `measure_managed_contention.py` process.

### Freeze hashes

SHA-256 values at the second-batch freeze:

- `file_management/models.py`: `81C9529D10A54120FDE889B60CAA0AD2ACE6ACEFFB3181F9E9701A22E1B8C918`
- `file_management/repository.py`: `EAD99FB3BB30D9280E8E43E3457B601933754E2066C043A2C73CDCD1A21BC9BB`
- `file_management/jobs.py`: `C4411F9040D02455C6920FF0332D2E01A6CFB23A8285DC59C593D9048F27816A`
- `file_management/versions.py`: `9F99349A8BF2F944341FD9DBED5EA4EFAC247E5419B3A955FF5EE65329980C51`
- `file_management/service.py`: `6F14E08CDBA4F57718BBCEB2B3E65C2231BF60A6F575E960F2AAE9BC89B26F73`
- `file_management/backends.py`: `8230BEDBAF29FDCE712594B58BD48D0B74C73AF80FDD4F21071C7C89C2296658`
- `management_http/jobs.py`: `EBDC7B7AC4B61CBC9BC635C8E0BFD094E4CC3C12FB94D40AABA964B611E835DB`
- `management_http/app.py`: `FE3504747FB8410BA7E703D49C7E568182F62B5D4403A9AEEE61C3C23A010322`
- `indexing/dense.py`: `273E81CBCD77E302E10FE417474ED90696176DF0D2BA9A1387595EFCCC4DD851`
- `indexing/build.py`: `5BCF1FB08E16CA5C06DEE9AB4CC97BE9EA7EA83C60C34BFC6EA241D14B982A79`
- `mcp/runtime.py`: `654CCEDD2040B94791C02EA682655DB87588DFC73DF4FBFBC51D3E9B13AF7963`
- `mcp/tools.py`: `78A6CD057F26ED6C284B420BA79ADF36A7B25516163309BA040D16422225FCCB`
- `mcp/server.py`: `5F7D8A2D30174109A9E484E7F3F3E2E916DF882B5A1985152F625CDD5337BB0F`
- `releases/resolver.py`: `996BF3FD7C3BF09F0E15B9648F8AF8C46A7324AED2CEF4153272E674F983CB75`
- `scripts/experiments/measure_managed_contention.py`: `1088235D615E23BCD8AED9164C12F11615C6B88597D4FD661895EF714BCF3155`
