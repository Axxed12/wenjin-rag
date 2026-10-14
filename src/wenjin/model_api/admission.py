"""模型API的有界FIFO推理准入队列。

队列worker持有运行中的同步推理；HTTP任务取消只会让调用方离开，不能释放仍在执行的线程。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool


class ModelAdmissionError(RuntimeError):
    """可通过HTTP稳定传递的模型服务准入错误。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class _Job:
    operation: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    started: asyncio.Event
    result: asyncio.Future[Any]
    deadline: float


class ModelAdmission:
    """单推理worker，FIFO顺序由加入deque的事件循环顺序决定。"""

    def __init__(self, *, max_waiting: int = 8, max_wait_seconds: float = 30.0) -> None:
        if max_waiting < 0 or not math.isfinite(max_wait_seconds) or max_wait_seconds <= 0:
            raise ValueError("invalid model admission limits")
        self.max_waiting = max_waiting
        self.max_wait_seconds = max_wait_seconds
        self._waiting: deque[_Job] = deque()
        self._changed = asyncio.Event()
        self._accepting = True
        self._active: _Job | None = None
        self._worker = asyncio.create_task(self._run())

    async def submit(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """提交一次模型调用；wait_seconds只限制等槽时间，不限制实际推理时间。"""
        if not self._accepting:
            raise ModelAdmissionError("model_service_shutting_down")
        loop = asyncio.get_running_loop()
        job = _Job(
            operation,
            args,
            kwargs,
            asyncio.Event(),
            loop.create_future(),
            time.monotonic() + self.max_wait_seconds,
        )
        # 即使HTTP调用方在推理开始后断开，worker仍须收完结果；回调同时消费无接收方异常。
        job.result.add_done_callback(_consume_future_exception)
        if self._active is None and not self._waiting:
            # 预留空闲active槽，后续请求才计入N个waiting位。
            self._active = job
        else:
            if len(self._waiting) >= self.max_waiting:
                raise ModelAdmissionError("model_queue_full")
            self._waiting.append(job)
        self._changed.set()

        try:
            await asyncio.wait_for(job.started.wait(), timeout=self.max_wait_seconds)
        except TimeoutError:
            if self._remove_waiting(job):
                if not job.result.done():
                    job.result.cancel()
                raise ModelAdmissionError("model_queue_timeout") from None
            # worker已经取出本项，启动事件与超时恰好竞速；此后不再算排队超时。
        except asyncio.CancelledError:
            # 排队取消可立即移除；已开始的任务由worker继续持有实际推理槽。
            if self._remove_waiting(job) and not job.result.done():
                job.result.cancel()
            raise
        return await asyncio.shield(job.result)

    def _remove_waiting(self, job: _Job) -> bool:
        if self._active is job and not job.started.is_set():
            self._active = None
            self._changed.set()
            return True
        try:
            self._waiting.remove(job)
        except ValueError:
            return False
        return True

    async def _run(self) -> None:
        while True:
            if self._active is None:
                if self._waiting:
                    self._active = self._waiting.popleft()
                else:
                    self._changed.clear()
                    if not self._accepting:
                        return
                    await self._changed.wait()
                    continue

            job = self._active
            if job is None:
                continue
            if job.result.cancelled():
                self._active = None
                continue
            if not job.started.is_set():
                if time.monotonic() >= job.deadline:
                    self._active = None
                    job.started.set()
                    if not job.result.done():
                        job.result.set_exception(ModelAdmissionError("model_queue_timeout"))
                    continue
                # worker实际取得推理槽前，调用方取消仍可移除reserved项。
                job.started.set()
            try:
                work = asyncio.create_task(
                    run_in_threadpool(job.operation, *job.args, **job.kwargs)
                )
                try:
                    value = await asyncio.shield(work)
                except asyncio.CancelledError:
                    # worker若被外部取消，也先等不可取消的同步推理线程收尾。
                    await work
                    raise
            except BaseException as error:
                if not job.result.done():
                    job.result.set_exception(error)
            else:
                if not job.result.done():
                    job.result.set_result(value)
            self._active = None

            # 没有active时，任何新请求只能先占idle槽或排在队尾。
            if not self._waiting:
                self._changed.clear()

    async def close(self) -> None:
        """停止接单，拒绝尚未开始的工作，并等待当前线程真正结束。"""
        if self._accepting:
            self._accepting = False
            if self._active is not None and not self._active.started.is_set():
                reserved = self._active
                self._active = None
                if not reserved.result.done():
                    reserved.result.set_exception(
                        ModelAdmissionError("model_service_shutting_down")
                    )
                reserved.started.set()
            while self._waiting:
                job = self._waiting.popleft()
                if not job.result.done():
                    job.result.set_exception(ModelAdmissionError("model_service_shutting_down"))
                job.started.set()
            self._changed.set()
        await self._worker


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()
