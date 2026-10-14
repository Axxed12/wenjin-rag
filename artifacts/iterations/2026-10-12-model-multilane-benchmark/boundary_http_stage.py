"""Real HTTP lifetime probes; CPU holds and real GPU disconnect evidence stay separate."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from pathlib import Path

import httpx


def run_boundary_stage(state, output, *, model_token, lane_count, stop_event):
    output = Path(output)
    trace = Path(state["trace_file"])
    url = state["model_base_url"]
    rows = []
    result = {"passed": False, "lane_count": lane_count,
              "scope": "CPU hold real HTTP admission; separate real GPU active disconnect",
              "settings": {"max_waiting": 1, "max_wait_seconds": .2}, "checks": {}, "rows": rows}

    def events(request_id=None):
        found = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
        return [event for event in found if not request_id or event.get("request_id") == request_id]

    async def wait_event(request_id, name, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise RuntimeError("resource_stop_during_boundary")
            found = [event for event in events(request_id) if event["event"] == name]
            if found:
                return found[-1]
            await asyncio.sleep(.01)
        raise RuntimeError(f"boundary_event_missing_{name}")

    async def execute():
        async with httpx.AsyncClient(base_url=url, trust_env=False,
                                     timeout=30, limits=httpx.Limits(max_connections=16)) as client:
            identity_response = await client.get("/v1/identity", headers={"Authorization": "Bearer " + model_token})
            identity_response.raise_for_status()
            identity = identity_response.json()
            if identity.get("admission") != {"active": lane_count, "max_waiting": 1, "max_wait_seconds": .2}:
                raise RuntimeError("boundary_configuration_identity_mismatch")

            async def send(request_id, route, payload, label):
                begin = time.perf_counter()
                row = {"request_id": request_id, "label": label, "path": route, "input": payload,
                       "begin_monotonic": begin, "status": None, "body": None, "error": None}
                try:
                    response = await client.post(route, json=payload, headers={"Authorization": "Bearer " + model_token,
                                                                             "X-Experiment-Request-Id": request_id})
                    row.update(status=response.status_code, body=response.json(),
                               response_request_id=response.headers.get("X-Experiment-Request-Id"))
                except asyncio.CancelledError:
                    row["error"] = "client_cancelled_http_transport"
                    raise
                except Exception as exc:
                    row["error"] = type(exc).__name__
                finally:
                    row["elapsed_seconds"] = time.perf_counter() - begin
                    rows.append(row)
                    with (output / "boundary-requests.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                return row

            def hold(seconds, label):
                rid = uuid.uuid4().hex
                task = asyncio.create_task(send(rid, "/_experiment/boundary/hold",
                                                {"hold_seconds": seconds}, label))
                return rid, task

            async def full_active(seconds, label):
                jobs = [hold(seconds, label) for _ in range(lane_count)]
                await asyncio.gather(*(wait_event(rid, "operation_enter") for rid, _ in jobs))
                return jobs

            async def settle(jobs):
                values = await asyncio.gather(*(task for _, task in jobs), return_exceptions=True)
                await asyncio.gather(*(wait_event(rid, "lane_drained") for rid, _ in jobs))
                return values

            active = await full_active(1.2, "queue-full-active-CPU")
            queued_id, queued = hold(.05, "queue-timeout-waiter-CPU")
            await wait_event(queued_id, "admission_enqueued")
            overflow_id, overflow = hold(.05, "queue-full-overflow-CPU")
            full_row = await overflow
            timeout_row = await queued
            active_rows = await settle(active)
            result["checks"]["full_and_timeout"] = {
                "passed": full_row["status"] == 503 and full_row["body"].get("error") == "model_queue_full"
                          and timeout_row["status"] == 503 and timeout_row["body"].get("error") == "model_queue_timeout"
                          and all(isinstance(row, dict) and row["status"] == 200 for row in active_rows)
                          and not any(e["event"] == "operation_enter" for e in events(queued_id)),
                "overflow_id": overflow_id, "timeout_id": queued_id, "scope": "real HTTP / CPU hold"}

            active = await full_active(1.2, "queued-cancel-active-CPU")
            cancel_id, cancelled = hold(.05, "queued-cancel-CPU")
            await wait_event(cancel_id, "admission_enqueued")
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            cancel_event = await wait_event(cancel_id, "queued_cancel")
            replacement_id, replacement = hold(.05, "queued-cancel-replacement-CPU")
            replacement_row = await replacement
            await settle(active)
            result["checks"]["queued_cancel"] = {
                "passed": cancel_event.get("queue_depth") == 0
                          and not any(e["event"] == "operation_enter" for e in events(cancel_id))
                          and replacement_row["status"] == 503
                          and replacement_row["body"].get("error") == "model_queue_timeout",
                "cancel_id": cancel_id, "replacement_id": replacement_id,
                "replacement_note": "slot accepted after cancellation, then intentional .2s wait expired"}

            active = await full_active(1.2, "active-disconnect-CPU")
            disconnect_id, disconnected = active[0]
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)
            disconnected_event = await wait_event(disconnect_id, "active_disconnect")
            follower_id, follower = hold(.05, "active-disconnect-follower-CPU")
            follower_row = await follower
            await settle(active)
            exited = await wait_event(disconnect_id, "operation_exit")
            drained = await wait_event(disconnect_id, "lane_drained")
            fresh_id, fresh = hold(.05, "post-active-disconnect-CPU")
            fresh_row = await fresh
            result["checks"]["active_disconnect_CPU"] = {
                "passed": disconnected_event["monotonic_ns"] < exited["monotonic_ns"] <= drained["monotonic_ns"]
                          and follower_row["status"] == 503 and follower_row["body"].get("error") == "model_queue_timeout"
                          and fresh_row["status"] == 200,
                "active_id": disconnect_id, "follower_id": follower_id, "fresh_id": fresh_id,
                "timeline": [disconnected_event, exited, drained], "scope": "CPU hold, not GPU"}

            # Keep the other lanes occupied by CPU holds. Exactly one real GPU inference is disconnected.
            other_jobs = [hold(3, "GPU-disconnect-other-lanes-CPU") for _ in range(lane_count - 1)]
            await asyncio.gather(*(wait_event(rid, "operation_enter") for rid, _ in other_jobs))
            model = identity["embedding"]
            payload = {"protocol": identity["protocol"], "model_id": model["model_id"],
                       "asset_sha256": model["asset_sha256"], "implementation_sha256": identity["implementation_sha256"],
                       "input_kind": "query", "inputs": [
                           (f"Synthetic GPU disconnect query {i}. " + "model queue evidence " * 150) for i in range(12)]}
            gpu_id = uuid.uuid4().hex
            gpu_task = asyncio.create_task(send(gpu_id, "/v1/embeddings", payload, "active-disconnect-GPU"))
            dispatch = await wait_event(gpu_id, "forward_dispatch")
            gpu_task.cancel()
            await asyncio.gather(gpu_task, return_exceptions=True)
            # An observed disconnect before operation_exit is essential; transport closure alone is insufficient.
            gpu_disconnect = await wait_event(gpu_id, "active_disconnect")
            gpu_exit = await wait_event(gpu_id, "operation_exit")
            gpu_drained = await wait_event(gpu_id, "lane_drained")
            forward_ends = [e for e in events(gpu_id) if e["event"] == "forward_end"]
            await settle(other_jobs)
            small = payload | {"inputs": ["Synthetic post-disconnect query"]}
            after_id = uuid.uuid4().hex
            after = await send(after_id, "/v1/embeddings", small, "post-active-disconnect-GPU")
            vectors = (after.get("body") or {}).get("data", [])
            valid = (after["status"] == 200 and len(vectors) == 1 and len(vectors[0]) == model["dimensions"]
                     and all(math.isfinite(v) for v in vectors[0]) and abs(math.hypot(*vectors[0]) - 1) < .01)
            result["checks"]["active_disconnect_GPU"] = {
                "passed": bool(forward_ends) and any(e["monotonic_ns"] > gpu_disconnect["monotonic_ns"] for e in forward_ends) and dispatch["monotonic_ns"] <= gpu_disconnect["monotonic_ns"]
                          < gpu_exit["monotonic_ns"] <= gpu_drained["monotonic_ns"] and valid
                          and all(e["monotonic_ns"] <= gpu_exit["monotonic_ns"] for e in forward_ends),
                "active_id": gpu_id, "new_request_id": after_id,
                "timeline": [dispatch, gpu_disconnect, *forward_ends, gpu_exit, gpu_drained],
                "scope": "real model HTTP disconnect; original GPU work completed before lane release",
                "immediate_GPU_stop_claim": False, "post_disconnect_request_valid": valid}
            result["passed"] = all(check["passed"] for check in result["checks"].values())

    try:
        asyncio.run(execute())
    except BaseException as exc:
        result["failure_type"] = type(exc).__name__
        result["safe_reason"] = str(exc) if isinstance(exc, RuntimeError) else "inspect_boundary_raw_rows"
    finally:
        result["actual_HTTP_request_count"] = len(rows)
        (output / "boundary-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
