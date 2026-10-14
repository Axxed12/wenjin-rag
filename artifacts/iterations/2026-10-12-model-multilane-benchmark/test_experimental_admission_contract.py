"""CPU-only acceptance contracts for the isolated benchmark admission copy.

These tests do not import/load model weights, torch, or CUDA. They verify only
lane/queue/cancellation/shutdown behavior with deterministic blocking calls.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

EXPERIMENTAL_SOURCE = Path(__file__).parent / "experimental-source"
sys.path.insert(0, str(EXPERIMENTAL_SOURCE))

from wenjin.model_api.experiment_trace import (  # noqa: E402
    operation_kind_var,
    request_id_var,
)

from wenjin.model_api.admission import ModelAdmission, ModelAdmissionError  # noqa: E402


def _wait_event(event: threading.Event, timeout: float = 3.0) -> bool:
    return event.wait(timeout)


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return bool(predicate())


def _code(error: ModelAdmissionError) -> str:
    return error.code


@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_active_capacity_is_excluded_from_waiting_limit_and_overflow_is_rejected(lanes: int) -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=2, max_wait_seconds=4, lane_count=lanes)
        release = threading.Event()
        all_active = threading.Event()
        state_lock = threading.Lock()
        active = 0
        peak_active = 0

        def blocking() -> str:
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
                if active == lanes:
                    all_active.set()
            assert release.wait(4), "blocking operation was not released"
            with state_lock:
                active -= 1
            return "active"

        active_tasks = [asyncio.create_task(admission.submit(blocking)) for _ in range(lanes)]
        assert await asyncio.to_thread(_wait_event, all_active)

        queued_started = threading.Event()

        def queued_operation() -> str:
            queued_started.set()
            return "queued"

        queued_tasks = [
            asyncio.create_task(admission.submit(queued_operation)) for _ in range(2)
        ]
        assert await _wait_until(lambda: admission._queue_depth() == 2)
        with pytest.raises(ModelAdmissionError) as caught:
            await admission.submit(lambda: "overflow")
        assert _code(caught.value) == "model_queue_full"
        assert not queued_started.is_set()

        release.set()
        assert await asyncio.gather(*active_tasks) == ["active"] * lanes
        assert await asyncio.gather(*queued_tasks) == ["queued", "queued"]
        assert peak_active == lanes
        await admission.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_waiting_deadline_times_out_without_running_operation(lanes: int) -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=1, max_wait_seconds=0.08, lane_count=lanes)
        release = threading.Event()
        all_active = threading.Event()
        active_count = 0
        lock = threading.Lock()
        expired_called = threading.Event()

        def blocking() -> str:
            nonlocal active_count
            with lock:
                active_count += 1
                if active_count == lanes:
                    all_active.set()
            assert release.wait(3), "blocking operation was not released"
            return "done"

        active = [asyncio.create_task(admission.submit(blocking)) for _ in range(lanes)]
        assert await asyncio.to_thread(_wait_event, all_active)
        await asyncio.sleep(0.01)

        def expired_operation() -> str:
            expired_called.set()
            return "must-not-run"

        with pytest.raises(ModelAdmissionError) as caught:
            await admission.submit(expired_operation)
        assert _code(caught.value) == "model_queue_timeout"
        assert not expired_called.is_set()
        assert admission._queue_depth() == 0

        release.set()
        assert await asyncio.gather(*active) == ["done"] * lanes
        await admission.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_queued_cancel_removes_item_and_frees_waiting_capacity(lanes: int) -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=1, max_wait_seconds=3, lane_count=lanes)
        release = threading.Event()
        all_active = threading.Event()
        active_count = 0
        lock = threading.Lock()
        cancelled_called = threading.Event()
        replacement_started = threading.Event()

        def blocking() -> str:
            nonlocal active_count
            with lock:
                active_count += 1
                if active_count == lanes:
                    all_active.set()
            assert release.wait(3), "blocking operation was not released"
            return "done"

        active = [asyncio.create_task(admission.submit(blocking)) for _ in range(lanes)]
        assert await asyncio.to_thread(_wait_event, all_active)

        def cancelled_operation() -> str:
            cancelled_called.set()
            return "cancelled"

        cancelled = asyncio.create_task(admission.submit(cancelled_operation))
        assert await _wait_until(lambda: admission._queue_depth() == 1)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert admission._queue_depth() == 0

        def replacement_operation() -> str:
            replacement_started.set()
            return "replacement"

        replacement = asyncio.create_task(admission.submit(replacement_operation))
        assert await _wait_until(lambda: admission._queue_depth() == 1)
        assert not cancelled_called.is_set()
        assert not replacement_started.is_set()

        release.set()
        assert await asyncio.gather(*active) == ["done"] * lanes
        assert await replacement == "replacement"
        assert replacement_started.is_set()
        assert not cancelled_called.is_set()
        await admission.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_active_caller_cancel_keeps_lane_until_sync_operation_finishes(lanes: int) -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=1, max_wait_seconds=3, lane_count=lanes)
        release = threading.Event()
        all_active = threading.Event()
        active_count = 0
        lock = threading.Lock()
        waiting_called = threading.Event()

        def blocking() -> str:
            nonlocal active_count
            with lock:
                active_count += 1
                if active_count == lanes:
                    all_active.set()
            assert release.wait(3), "blocking operation was not released"
            return "done"

        active = [asyncio.create_task(admission.submit(blocking)) for _ in range(lanes)]
        assert await asyncio.to_thread(_wait_event, all_active)
        active[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await active[0]

        def waiting_operation() -> str:
            waiting_called.set()
            return "next"

        waiting = asyncio.create_task(admission.submit(waiting_operation))
        assert await _wait_until(lambda: admission._queue_depth() == 1)
        assert not waiting_called.is_set()

        release.set()
        assert await asyncio.gather(*active[1:]) == ["done"] * (lanes - 1)
        assert await waiting == "next"
        assert waiting_called.is_set()
        await admission.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_close_rejects_waiters_and_drains_active_sync_operations(lanes: int) -> None:
    async def scenario() -> None:
        admission = ModelAdmission(max_waiting=1, max_wait_seconds=4, lane_count=lanes)
        release = threading.Event()
        all_active = threading.Event()
        active_count = 0
        lock = threading.Lock()
        queued_called = threading.Event()

        def blocking() -> str:
            nonlocal active_count
            with lock:
                active_count += 1
                if active_count == lanes:
                    all_active.set()
            assert release.wait(4), "blocking operation was not released"
            return "done"

        active = [asyncio.create_task(admission.submit(blocking)) for _ in range(lanes)]
        assert await asyncio.to_thread(_wait_event, all_active)

        def queued_operation() -> str:
            queued_called.set()
            return "must-not-run"

        queued = asyncio.create_task(admission.submit(queued_operation))
        assert await _wait_until(lambda: admission._queue_depth() == 1)
        close_task = asyncio.create_task(admission.close())
        with pytest.raises(ModelAdmissionError) as caught:
            await queued
        assert _code(caught.value) == "model_service_shutting_down"
        assert not close_task.done()
        assert not any(task.done() for task in active)
        assert not queued_called.is_set()

        release.set()
        assert await asyncio.gather(*active) == ["done"] * lanes
        await close_task
        assert not queued_called.is_set()

    asyncio.run(scenario())


def test_active_disconnect_event_keeps_request_identity(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        trace_path = tmp_path / "events.jsonl"
        monkeypatch.setenv("WENJIN_MODEL_TRACE_PATH", str(trace_path))
        request_token = request_id_var.set("acceptance-active-disconnect")
        kind_token = operation_kind_var.set("embedding")
        try:
            admission = ModelAdmission(max_waiting=1, max_wait_seconds=3, lane_count=1)
            release = threading.Event()
            entered = threading.Event()

            def blocking() -> str:
                entered.set()
                assert release.wait(3), "blocking operation was not released"
                return "done"

            active = asyncio.create_task(admission.submit(blocking))
            assert await asyncio.to_thread(_wait_event, entered)
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            # Caller cancellation must not release the worker lane or its sync call.
            assert admission._active
            assert not next(iter(admission._active.values())).result.done()
            release.set()
            await admission.close()

            records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
            disconnects = [row for row in records if row["event"] == "active_disconnect"]
            assert len(disconnects) == 1
            assert disconnects[0]["request_id"] == "acceptance-active-disconnect"
            assert disconnects[0]["operation_kind"] == "embedding"
            assert any(row["event"] == "lane_drained" for row in records)
        finally:
            operation_kind_var.reset(kind_token)
            request_id_var.reset(request_token)

    asyncio.run(scenario())
