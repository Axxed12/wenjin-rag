# Model admission independent test log — 2026-10-12

## Scope and frozen inputs

This iteration adds test-owned contracts and records; business source is owned by `/root/model_admission_dev`.

- Historical mixed-collision baseline: `artifacts/iterations/2026-10-12-concurrency-baseline/runs/20261012T070604Z-2f798c/` (source tree SHA-256 `6e9b9c316e0ac2a50355b432d57502365f3218a4ba2fb63b0759517ab996658a`). The baseline report attributes the managed zero-evidence response to model embedding HTTP 503 -> management bridge HTTP 503 and preserves the earlier incorrect collision classification separately.
- Frozen original source: `frozen-source/src/`, SHA-256 matches the baseline. The test runner copy is `run_concurrency_baseline.frozen.py` (SHA-256 `7a490e24df72c588eb984c4b4b59c9661ced43d10f254099aba966eb41f5facc`).
- A developer-created but initially unconnected `admission.py` was present when the current iteration's `prepare` ran. Its full-source tree hash was `eb1fa882b7172448e025e10e08a10b8b94257244313645b8c18e8db32a9adc0d`; the model API server source at initial freeze still matched the baseline copy.

## Repair-before reproduction

Run `20261012T080914Z-a44260` reused baseline `prepare`/`run_benchmark` helpers with `controls_only=True`, custom iteration output, fixed synthetic inputs, isolated Docker project and Qdrant volume, and the real read-only 0.6B embedding/reranker assets. It recorded single-request shared/managed results, authorization probes, a shared+managed collision, lifecycle probes, and cancellation; it did not run the 128-request matrix or frozen dataset. Four isolated services were cleaned with 0 containers/volumes/networks remaining and the temporary root removed.

The collision returned correct shared evidence; managed returned MCP HTTP 200 with `tool.error=unavailable`, empty evidence, and service logs showing model `/v1/embeddings` HTTP 503 followed by management `/api/mcp/search` HTTP 503. This is a failed managed request, not successful zero-evidence behavior. The shared request itself passed marker and index identity checks. The run's manifest identifies Qwen3 Embedding/Reranker 0.6B and the same asset hashes as the baseline.

**Limitation:** developer edits began after the containers had imported the original handlers but before cleanup; `summary.json` records `cleanup.source_changed_during_run=true`, and final `server.py` hash differs from the frozen pre-run hash. Treat this run as a narrow symptom reproduction, not a fully immutable source-attested acceptance. The original baseline report plus its source-attested logs remain the primary pre-change evidence. Raw artifacts for this limited reproduction are under `runs/20261012T080914Z-a44260/`; no original baseline evidence or runner was modified.

## Contract implementation and validation

The test-owned CPU contract is `tests/test_model_api_admission_contract.py`. It uses event-controlled blocking operations and verifies one active plus exact FIFO waiting capacity, `max_waiting=0`, queued cancellation, active caller cancellation without freeing the synchronous worker slot, immediate shutdown rejection and shutdown waiting for actual thread completion, absolute deadline expiry before operation dispatch, and cancellation after idle-slot reservation but before worker dispatch.

A synthetic model API listener verifies HTTP 503 `model_queue_full` and `model_queue_timeout` become typed `RemoteModelClient` errors. Two TCP disconnect cases exercise real Uvicorn/ASGI request disconnect handling: a queued request is removed and a later request succeeds; a raw-socket active request is disconnected while its fake synchronous inference remains blocked, and the subsequent inference does not enter until that first thread is released. A local FastMCP `Client` call verifies both busy codes arrive with `is_error=true`, structured error code, and explicit `evidence_status=not_retrieved`; this is an in-process MCP SDK check, not the cached Docker image check requested for final acceptance.

The initial idle-reservation contract failed against the first admission implementation because the canceled operation still ran. After the dev correction, all six CPU admission lifetime contracts passed. The first active TCP disconnect attempt also failed because the test had not closed its socket before waiting for disconnect; the corrected raw-socket scenario passes and now establishes the disconnect before checking resource lifetime.

Initial queue/TCP/MCP contract validation at 2026-10-12: `uv run --no-sync pytest tests/test_model_api_admission_contract.py -q` — 11 passed in 6.81 s; Ruff passed. This is the initial 11-case result, not the final expanded count.

The initial idle-reservation contract failed against the first admission implementation because the canceled operation still ran. After the dev correction, all six CPU admission lifetime contracts passed. The first active TCP disconnect attempt failed because the test had not closed its socket before waiting; the corrected raw-socket scenario passes and establishes disconnect before checking resource lifetime. The first MCP HTTP probe omitted the session header and received HTTP 400; it now asserts negotiated protocol `2025-11-25` and sends `MCP-Session-Id` plus `MCP-Protocol-Version` on follow-up requests.

The final expanded local contract file passed: `uv run --no-sync pytest tests/test_model_api_admission_contract.py -q` — 23 passed in 9.09 s. It includes queue lifetime, synthetic model HTTP/provider errors, bridge timeout guards, actual management HTTP-to-MCP propagation for queue-full/queue-timeout/bridge-read-timeout, and Streamable HTTP MCP error-wire assertions. Ruff and `py_compile` passed for the runner, Docker wire probe, and test files.

The cached Docker backend image was checked by running `verify_fastmcp_http_wire.py` with final frozen `src/` mounted read-only and networking disabled. A stateful 2025-11-25 Streamable HTTP session returned `result.isError=true`, `structuredContent.error=model_queue_full`, `evidence_status=not_retrieved`, and no evidence. The first Docker probe assumed JSON for `initialize` although the SDK returned SSE; the decoder was corrected and rerun successfully.

An existing narrow regression batch initially found 6 remote-provider tests whose synthetic model identity omitted the newly required `admission` fields. I updated only the test identity helper (including a 100 ms queue budget for its 250 ms read-timeout test) and aligned that test's expected typed code with the observed `model_provider_read_timeout`. The rerun of six selected remote-provider contracts plus `tests/test_managed_mcp_http_client.py` passed 24/24 in 11.07 s; Ruff passed. Two existing Qdrant deprecation warnings were emitted by the TCP bridge case. This is a synthetic local regression batch, not Docker/GPU acceptance.

## Final GPU/Docker targeted acceptance

The final runner command was `uv run --no-sync python artifacts/iterations/2026-10-12-model-admission/run_model_admission_targeted.py`. Run ID `20261012T084409Z-6be28c` completed with `passed=true`. It used frozen source SHA `c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674`, runner SHA `94158d6b49964c45ff2cec4044b13ec061feab5f3bb47e4de137b50443865625`, cached backend/model-node images `53e2dacc8798…` / `c9d4e5172eca…`, and read-only Qwen3 Embedding/Reranker 0.6B assets. Model identity reported the expected asset hashes and admission `{active:1,max_waiting:8,max_wait_seconds:30}`. Management, MCP, and model-node runtime source attestations all matched the frozen source hash. Settings were model read 90 s, 8 waiting, wait deadline 30 s, management bridge read 210 s.

The run passed single shared and single managed retrieval with positive marker and identity-correct evidence; one simultaneous shared+managed pair where both returned HTTP 200 with positive expected evidence; stale managed version rejection as `invalid_target` with no evidence; and unprivileged identity/default-deny checks. The synthetic ingest completed, and a subsequent MCP search returned its unique marker with matching managed-file ID and active version ID in both tool identity and evidence. The concurrent query/upload segment overlapped by 0.609 s at client-operation intervals, but no server admission timestamp or active/queue observation was recorded; therefore this is not evidence of model inference competition or queue wait duration (`model_queue_competition_observed=false`, `model_queue_wait_seconds_observed=false`).

Final output is in `runs/20261012T084409Z-6be28c/targeted-results.json`, `run-manifest.json`, `service-log-diagnostics.json`, `resources.jsonl`, and `cleanup.json`. Cleanup completed with zero containers, volumes, and networks for the temporary project; its runtime root and temporary credentials were removed; and the post-run source hash still matched the freeze. The existing real Qdrant service on port 6333 was not used or changed. No full test suite, 128-request matrix, or frozen evaluation set was run.

### Current boundary

This closes the authorized independent test slice for admission behavior, selected bridge/provider regressions, cached Docker MCP wire semantics, and the one final small real-model stack run. It does not measure server-side queue wait or prove real GPU inference contention between ingest and query. Only the deterministic CPU fake-backend contract establishes shared FIFO resource lifetime across query/document/rerank endpoints.


Read-only resource preflight at 2026-10-12: RTX 4070 SUPER total 12,282 MiB, free 10,425 MiB, 3% utilization. Cached images `wenjin-model-node:dual-smoke-20261008` (`c9d4e5172eca`) and `wenjin-backend:dual-smoke-20261008` (`53e2dacc8798`) are available. Existing `wenjin-rag-qdrant-1` is bound to real local port 6333 and remains untouched; other running Docker projects also remain untouched. An earlier source freeze `frozen-source-postfix/src/` at SHA `c9c8cada...` predates the bridge-read budget guard and is historical only; it was never mounted into the GPU stack. The final run identity is `frozen-source-final/src/`, 99 Python files, SHA-256 `c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674` using the baseline runner's exact path-plus-content hash function. Frozen compose YAML SHA values and algorithm are in `final-source-freeze.json`; all runtime services mount this source read-only at `/app/src`.

## Final targeted runner preparation and contracts

The test-owned runner was extended before any model stack start. The query/document/rerank endpoint contract blocks query inference with a deterministic event, observes the document request and then the rerank request in the actual shared admission waiting deque, releases the first call, and verifies all three endpoint responses and backend call counts. Command: `uv run --no-sync pytest tests/test_model_api_admission_contract.py -q` — 24 passed in 13.55 s. Ruff and `py_compile` passed after import ordering was corrected. This suite total is 24, including the earlier CPU lifetime, synthetic HTTP, disconnect, MCP, management bridge, and budget contracts; the historical 11-pass result is not added again.

The remote provider/client selected regression batch remains a separate 24/24 result, run with:

```powershell
uv run --no-sync pytest `
  tests/test_remote_model_provider_contract.py::test_remote_adapters_use_one_authenticated_http_contract_for_query_document_and_rerank `
  tests/test_remote_model_provider_contract.py::test_remote_identity_mismatch_and_upstream_failure_fail_closed_without_local_fallback `
  tests/test_remote_model_provider_contract.py::test_remote_provider_errors_do_not_echo_credentials_query_or_server_exception `
  tests/test_remote_model_provider_contract.py::test_remote_timeout_is_a_safe_provider_error `
  tests/test_remote_model_provider_contract.py::test_remote_requests_pin_model_and_implementation_hashes_after_handshake `
  tests/test_remote_model_provider_contract.py::test_stateful_mcp_over_tcp_uses_remote_provider_through_management_http_bridge `
  tests/test_managed_mcp_http_client.py -q
```

The cached-image FastMCP wire probe remains a separate protocol check with explicit initialize protocol version and follow-up `MCP-Protocol-Version`; it is not counted as a pytest case.

The targeted GPU runner now records the ingest HTTP start time before sending the upload, checks job completion, obtains the created file's active version, syncs/grants that exact managed-file identity, and calls MCP `search_docs` to assert the new marker, file ID, and version ID in tool/evidence identity. Its `passed` gate includes that readback. The concurrent query/upload portion reports only client scheduling and HTTP operation-interval overlap; it explicitly records `model_queue_competition_observed=false` and `model_queue_wait_seconds_observed=false`, because the service does not expose server admission timestamps in this run.

Two prepare-only checks (`20261012T083735Z-0011f4`, `20261012T083801Z-7caf0a`) started no services. The first dry compose validation incorrectly included `compose.model-node.yaml` without its TLS certificate environment; the runner now uses the required frozen `compose.base.yaml` + `compose.local.yaml` plus its temporary overlay, and the second `docker compose config --quiet` passed. For the second run, project `wenjin-concurrency-a551e06f` had no remaining labeled containers, volumes, or networks; its temporary parent had no child roots, and the credential-bearing temporary root was removed. The first attempt failed before a `prepared.json` was emitted and likewise created no containers. The old real Qdrant service on 6333 and other projects were not touched.

Final pre-run identities: source `c0aa7dfcdb151c0f7f957a66b889bb3ba1406c4ffa1f3d5e671d6b7154f5a674` (also computed by the runner's exact hash function); runner SHA-256 `94158d6b49964c45ff2cec4044b13ec061feab5f3bb47e4de137b50443865625`, recorded in `runner-freeze.json`. Read-only preflight: GPU free 10,259 MiB / 12,282 MiB, utilization 0%; host free RAM 11,693 MiB. No GPU services had started at this checkpoint.

## Independent CPU and synthetic TCP contracts

`tests/test_model_api_admission_contract.py` now covers exact FIFO capacity (one active plus N waiting), zero waiting capacity, queued cancellation, caller cancellation while a blocking synchronous inference remains active, prompt shutdown rejection/waiting for the active thread, an expired waiter never reaching the operation plus a successful following request, and cancellation after idle-slot reservation but before worker dispatch. The reservation test parks a worker subclass on an `asyncio.Event`, so it tests the scheduling boundary deterministically without timing guesses.

A synthetic TCP model API test uses a blocking fake embedding backend and `RemoteModelClient`: with zero waiting slots, a second request receives the typed `model_queue_full`; with a one-item queue and a 50 ms test-only deadline, the queued request receives typed `model_queue_timeout`. Both codes are mapped from explicit HTTP 503 responses. These are CPU/synthetic endpoint contracts, not GPU inference results.

Validation at 2026-10-12: `uv run --no-sync pytest tests/test_model_api_admission_contract.py -q` — 7 passed; `uv run --no-sync ruff check tests/test_model_api_admission_contract.py` — passed; `py_compile` — passed. The initial deterministic idle-reservation contract failed against the first implementation (cancelled operation still ran); after developer correction, all six admission-lifetime contracts pass. No full test suite was run.
