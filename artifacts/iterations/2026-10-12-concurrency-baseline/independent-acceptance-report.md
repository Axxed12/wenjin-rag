# Independent acceptance report — 2026-10-12 concurrency baseline

## Scope and verdict

This report independently reviews run `20261012T070604Z-2f798c` (`wenjin-concurrency-27d37399`). The main shared/managed HTTP matrix is acceptable as an isolated exploratory baseline: all 132 recorded matrix/first/warm-up requests passed their per-request correctness checks, including the exact managed version assertion. This does not establish an SLO or production capacity: each concurrency bucket has only 16 requests, the p99 is therefore the sample maximum, and the run is a single Docker-host observation.

Controlled probes are reported separately. The shared+managed collision produced an upstream managed-path HTTP 503 while the shared request was in flight, consistent with the model busy gate; cancellation produced a retained MCP session crash traceback; the managed lifecycle reached successful replace, revoke, and regrant reads, but delete completion was not independently recorded in main run 070604; the separate 071733 follow-up later completed and verified it. These controlled outcomes do not invalidate the isolated 132-request matrix. Treat the matrix baseline as valid for this run; record each controlled probe as a separate outcome. `summary.json` says `stopped_by_safety_gate` while `stop_reason` is null; this label does not describe the matrix and should be treated as a reporting defect.

No services were started and no requests were generated for this independent review. The only executable checks were offline harness/mock checks in `check_concurrency_harness_offline.py`; the live data below was read from the parent-run artifacts.

## Provenance, isolation, and cleanup

- The run manifest records source attestation for management, MCP, and model-node importing `/app/src/wenjin/__init__.py`; the source hash is `6e9b9c316e0ac2a50355b432d57502365f3218a4ba2fb63b0759517ab996658a`. The Docker image identities and model asset hashes are retained in `run-manifest.json`.
- The isolated project is `wenjin-concurrency-27d37399`. The fixture target is `synthetic-concurrency-1.0-target`; the manifest records its digest. The managed fixture is identified by file ID in the manifest, and expected version `8b4053cdbfb64cb39abc87e0357401c2` is present on all 64 managed matrix rows (16 first/warm-up plus 64 matrix; 16 collision/lifecycle-related requests are outside that count).
- The identity probe passed: shared A received its fixture marker and identity, shared B received `forbidden` and no evidence; managed A received its marker and version identity, managed B received `file_not_available` and no evidence. The sessions have distinct SHA-256 identifiers. This demonstrates the tested fixture authorization boundary for these probes, not a general tenant-isolation proof.
- `cleanup.json` reports compose down completed, zero containers/volumes/networks remaining, `resources_remaining=false`, and temporary root removed.
- Resource sampling contains 44 records from `07:06:27.797768Z` to `07:08:50.172717Z`. Actual sampling intervals vary (about 1.2–3.6 seconds, median about 3.56 seconds); it is not a one-second trace. The observed minimum host free memory was 6,623 MiB and minimum GPU free memory was 6,926 MiB. The manifest reports model/Qdrant container startup of 36.919943 s and management/MCP startup of 8.646640 s, but does not establish a cold Docker VM or filesystem cache. The first matrix `single-first-request` phases were recorded only after the shared and managed authorized A/B identity probes; therefore those phase latencies are already warm and must not be presented as cold-request latency.
- `service-log-diagnostics.json` retains redacted service log lines and the cancellation traceback. It reports query/credential redaction; the sampled content reviewed contains no bearer values. Temporary managed source paths and opaque IDs remain as diagnostic metadata.

## Independently checked request matrix

The 132 request rows in `requests.jsonl` were grouped by phase/link/concurrency and checked against `summary.json`. Independent counts and recomputed latency percentiles, elapsed windows, throughput, response status, MCP error flag, expected marker, evidence, and managed version match the summary and raw rows. Every matrix request returned HTTP 200, `mcp_is_error=false`, non-empty evidence, and the expected marker. Shared evidence resolved to the synthetic shared target; managed evidence resolved to the manifest file/version.

| Link | Concurrency | Requests | Success | p50 runner nearest-rank (s) | p95 / p99 (s) | Successful req/s |
|---|---:|---:|---:|---:|---:|---:|
| shared | 1 | 16 | 16 | 0.473419 | 0.563002 / 0.563002 | 2.111114 |
| shared | 2 | 16 | 16 | 0.457757 | 0.575296 / 0.575296 | 4.146935 |
| shared | 4 | 16 | 16 | 0.547161 | 0.790664 / 0.790664 | 6.605230 |
| shared | 8 | 16 | 16 | 0.794837 | 1.377946 / 1.377946 | 7.509070 |
| managed | 1 | 16 | 16 | 0.583093 | 0.689692 / 0.689692 | 1.684488 |
| managed | 2 | 16 | 16 | 0.751961 | 0.813455 / 0.813455 | 2.625632 |
| managed | 4 | 16 | 16 | 1.153470 | 1.381416 / 1.381416 | 3.399629 |
| managed | 8 | 16 | 16 | 2.099804 | 2.436719 / 2.436719 | 3.533939 |

At n=16 nearest-rank empirical p50 selects the lower middle observation; the standard median is the mean of the middle two and is separately recorded in `independent-metrics.json`. Nearest-rank p95 and p99 both equal the maximum observation. The phase named `single-first-request` measured shared 0.542753 s and managed 0.629827 s; warm-up measured shared 0.430318 s and managed 0.559936 s. These are single observations taken after identity probes, so they are neither cold starts nor suitable cold-latency estimates. The actual first observed reranker load is in the earlier `shared-authorized-A` identity probe: raw RPC `retrieval.timing_seconds.rerank=14.858546` s and `total=14.911713` s; the corresponding MCP audit elapsed time is about 15 s. This is evidence of the first observed call’s long reranking cost, not an isolated measurement of model-load time. The matrix trend shows increasing latency with concurrency, especially on managed retrieval; it is not sufficient to estimate tail behavior or a stable capacity ceiling.

## Controlled probes and unresolved behavior

### Shared + managed collision

The shared call returned HTTP 200 with the expected marker/identity. For managed, the MCP HTTP request returned 200 but its tool result contained no evidence and failed the identity check. The log timeline provides supporting evidence for an upstream busy rejection: model-node returned HTTP 503 for the managed caller at `07:08:45.146Z`; management `/api/mcp/search` returned 503 at `07:08:45.174Z`; MCP audit for request `5be3672a40e348f595d495874f2f944c` records `ManagedFileServiceError`, status `error`, and zero returned chunks. This is an observed collision failure on the managed path, consistent with model-gate rejection, not evidence of cross-identity leakage. The model implementation returns `inference_busy` when its inference gate is already held, but the run did not retain the model 503 response body; therefore the exact 503 discriminator cannot be independently confirmed. The collision JSON also loses the MCP tool error and labels the response `collision_evidence_identity_mismatch`. The 070604 model-node 503, management search 503, and MCP audit error provide the observed chain; this review does not require a second collision.

### Managed replace → revoke → regrant → delete

The retained service logs show the replacement PUT accepted (202), the replace job polled to completion, the active file was re-read, and the subsequent managed search returned v2 evidence with the new file/version identity. Revocation was accepted (200), followed by an MCP audit `KeyError` with zero returned chunks; this is consistent with denied access, although a more explicit authorization error is preferable for observability. Regrant was accepted (200) and the next MCP search returned the v2 evidence again.

Delete was accepted (202), and the delete job was polled through `07:08:51.328Z`; Qdrant logs show the managed collections being deleted. The scenario failure was recorded at `07:08:51.325Z`, before any file GET-after-delete or post-delete MCP search appears in the service logs. Therefore the current evidence does not establish whether the job API reported a non-completed/failed status or whether the scenario stopped while polling. It also does not establish the final externally visible file state or post-delete evidence denial. Do not call the lifecycle probe passed; obtain the control-only response/body and finish the post-delete checks in a new isolated fixture.

### MCP cancellation and session reuse

The cancellation probe used a 1 ms delay, sent a stateful `notifications/cancelled`, then issued same-session search calls. Audits record successful retrieval completions for request IDs `1778e0dead084f5c9edf967bbd59d511` and `ddd354b3b9874a9eb61c5ce24ce6ef86` at `07:08:52.231Z` and `07:08:52.315Z`. Immediately after the latter, the MCP session crashed with an AnyIO/FastMCP `ExceptionGroup` caused by `AssertionError: Request already responded to` in `mcp/shared/session.py` while handling a response. This establishes an observable session/server error during the controlled cancellation/reuse sequence; it does not prove that downstream synchronous work stopped, nor isolate whether the cancellation notification, same-session request overlap, or response race triggered the assertion. The run correctly leaves `server_work_stopped` unknown. A control-only rerun should preserve the original request response, cancellation HTTP response, next request response, IDs, and server log window, and should not claim cancellation effectiveness without downstream work instrumentation.

## Control-only supplement — 2026-10-12 run 071733

A separate controls-only run `20261012T071733Z-321359` (`wenjin-concurrency-10f59e4d`) completed without repeating the matrix. It recorded four single/warm-up rows, skipped matrix execution, matched the host source hash for management/MCP/model-node, and cleaned up with zero remaining containers, volumes, networks, or temporary root. All four rows returned HTTP 200, `mcp_is_error=false`, the expected marker, and the expected shared target or managed file/version identity. The 30 resource samples showed minimum host free memory 7,812 MiB and GPU free memory 6,962 MiB. The manifest reports model/Qdrant startup 41.976084 s and management/MCP startup 8.680582 s; these remain container/process startup timings, not cold host/cache measurements.

The managed lifecycle probe passed all four checks. Replacement job completed and the post-replace search returned the new v2 marker and expected version. The replacement-overlap request returned the old v1 evidence, while its request completion followed the management action; this is consistent with an old-version result around transition but does not establish server-side overlap. Revocation returned HTTP 200; both overlap and post-revoke searches returned `file_not_available` with zero evidence. Regrant returned HTTP 200 and the post-regrant search returned the expected v2 marker/version. Delete job ended with status `deleted`, reported 9 cleanup verifications and two versions cleaned; file GET returned `status=deleted`, and post-delete MCP search returned `file_not_available` with zero evidence. The delete-overlap request returned `invalid_target` with zero evidence. The report marks each overlap as “possible”; no claim of simultaneous server execution is made.

Cancellation was sent on an initialized stateful MCP session after 1 ms. The notification returned HTTP 202. The original call returned HTTP 200 with JSON-RPC error `{code: 0, message: "Request cancelled"}` and no evidence. The harness closed the original session, then a distinct initialized session completed a valid shared retrieval with the expected marker and identity. Service logs again contain a crash for the original MCP session: FastMCP/AnyIO raised `AssertionError: Request already responded to` at `07:19:19.352925Z` while handling a response. The same class of crash appeared in run 070604, so it is reproducible under this cancellation sequence. It does not establish whether the synchronous worker/model operation stopped; later model-node inference requests appear in the same short window, but they are not correlated to the canceled MCP request ID.

The `summary.json` control-only status is internally inconsistent: top-level `status=controls_only_completed`, `benchmark_status=skipped_controls_only`, but `controls_status=failed_or_incomplete`. The lifecycle report passed. The cancellation report has no `passed` field, so the summary's missing-field default counts it as false even though the cancellation protocol response and fresh-session follow-up were observed. Report the actual cancellation observations above rather than interpreting this aggregate as a failed lifecycle or benchmark.

Evidence: `runs/20261012T071733Z-321359/run-manifest.json`, `controlled-managed-lifecycle.json`, `controlled-cancellation.json`, `service-log-diagnostics.json`, `summary.json`, and `cleanup.json`. The control-only run has no collision record; the 070604 logs retain the model-node 503 → management `/api/mcp/search` 503 → MCP `ManagedFileServiceError` evidence for that separate observation.

## Harness validation and follow-up evidence required

`check_concurrency_harness_offline.py` was run offline and passed 6 targeted checks, including HTTP/MCP failure accounting, session initialization/header separation, nested `tools/list` extraction, close behavior, and timeout latency/timestamp summary behavior. These validate helper logic only; they do not substitute for the live HTTP observations above.

For the control-only follow-up, retain the same resource/project isolation and do not repeat the matrix. Persist the lifecycle HTTP/job responses before assertions, including post-delete file GET and MCP response with zero evidence. For cancellation, retain every MCP response and session ID hash plus the relevant timestamped server traceback. Re-run cleanup and record zero remaining project resources. Do not overwrite the 070604 run artifacts.

## Evidence paths

- `runs/20261012T070604Z-2f798c/run-manifest.json`
- `runs/20261012T070604Z-2f798c/requests.jsonl`
- `runs/20261012T070604Z-2f798c/summary.json`
- `runs/20261012T070604Z-2f798c/resources.jsonl`
- `runs/20261012T070604Z-2f798c/controlled-unprivileged-identity.json`
- `runs/20261012T070604Z-2f798c/collision.json`
- `runs/20261012T070604Z-2f798c/controlled-managed-lifecycle-failure.json`
- `runs/20261012T070604Z-2f798c/controlled-cancellation-failure.json`
- `runs/20261012T070604Z-2f798c/service-log-diagnostics.json`
- `runs/20261012T070604Z-2f798c/cleanup.json`
- `runs/20261012T071733Z-321359/run-manifest.json`
- `runs/20261012T071733Z-321359/controlled-managed-lifecycle.json`
- `runs/20261012T071733Z-321359/controlled-cancellation.json`
- `runs/20261012T071733Z-321359/service-log-diagnostics.json`
- `runs/20261012T071733Z-321359/summary.json`
- `runs/20261012T071733Z-321359/cleanup.json`
- `independent-metrics.json` (standard median, runner nearest-rank quantiles, raw-window throughput, error counts, and resource peaks)
- `offline-harness-check-results.md`
