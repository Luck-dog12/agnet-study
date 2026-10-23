"""Priority-aware batching in front of the append-only session workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event, RLock, Timer
from typing import Callable

from pydantic import BaseModel

from app.async_tasks import AsyncTaskEvent
from app.daemon_executor import start_daemon_cleanup
from app.schemas import (
    AsyncTaskAcceptedEventData,
    AsyncTaskCancelRequestedEventData,
    AsyncTaskCancelledEventData,
    AsyncTaskCompletedEventData,
    AsyncTaskFailedEventData,
    AsyncTaskLateResultEventData,
    AsyncTaskProgressEventData,
    AsyncTaskStartedEventData,
    SessionEvent,
    ToolResult,
)
from app.workflow import (
    SessionEventWorkflow,
    canonical_json_snapshot,
    tool_result_snapshot,
)


class EventQueueError(RuntimeError):
    """The dispatcher can no longer guarantee ordered event delivery."""


@dataclass(slots=True)
class EventReceipt:
    """Completion handle for one queued normal-priority event."""

    _ready: Event = field(default_factory=Event)
    _event: SessionEvent | None = None
    _error: BaseException | None = None

    @property
    def done(self) -> bool:
        return self._ready.is_set()

    def wait(self, timeout: float | None = None) -> SessionEvent:
        if not self._ready.wait(timeout):
            raise TimeoutError("等待事件批量提交超时。")
        if self._error is not None:
            raise EventQueueError("事件批量提交失败。") from self._error
        assert self._event is not None
        return self._event

    def _succeed(self, event: SessionEvent) -> None:
        self._event = event
        self._ready.set()

    def _fail(self, error: BaseException) -> None:
        self._error = error
        self._ready.set()


@dataclass(frozen=True, slots=True)
class _Pending:
    event_type: str
    data: BaseModel
    receipt: EventReceipt
    size: int


class EventDispatcher:
    """Batch normal events while preserving barriers and urgent ordering.

    Normal events flush at 32 items, 256 KiB, or 100 ms. A barrier first
    drains older normal events and then appends itself. An urgent event first
    invokes the generation-cancel callback, then commits the detached normal
    prefix and the urgent event in one append batch with the urgent event last.
    """

    MAX_EVENTS = 32
    MAX_BYTES = 256 * 1024
    MAX_DELAY_SECONDS = 0.1

    def __init__(
        self,
        workflow: SessionEventWorkflow,
        *,
        cancel_generation: Callable[[], None] | None = None,
    ) -> None:
        self._workflow = workflow
        self._cancel_generation = cancel_generation
        self._lock = RLock()
        self._pending: list[_Pending] = []
        self._pending_bytes = 0
        self._timer: Timer | None = None
        self._closed = False
        self._failed = False

    def set_cancel_generation(self, callback: Callable[[], None] | None) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("cancel_generation 必须可调用。")
        with self._lock:
            self._cancel_generation = callback

    def submit_normal(self, event_type: str, data: BaseModel) -> EventReceipt:
        try:
            # Include a conservative allowance for the SessionEvent envelope
            # (type, session id, sequence and timestamp), not only its data.
            size = len(canonical_json_snapshot(data).encode("utf-8")) + 512
        except Exception:
            raise EventQueueError("普通事件无法生成无损 JSON 快照。") from None
        receipt = EventReceipt()
        with self._lock:
            self._ensure_open()
            self._pending.append(_Pending(event_type, data, receipt, size))
            self._pending_bytes += size
            if len(self._pending) >= self.MAX_EVENTS or self._pending_bytes >= self.MAX_BYTES:
                self._flush_locked()
            elif self._timer is None:
                self._timer = Timer(self.MAX_DELAY_SECONDS, self._timer_flush)
                self._timer.daemon = True
                self._timer.start()
        return receipt

    def submit_barrier(self, event_type: str, data: BaseModel) -> SessionEvent:
        with self._lock:
            self._ensure_open()
            self._flush_locked()
            try:
                return self._workflow.append(event_type, data)
            except Exception as exc:
                self._poison_locked(exc)
                raise EventQueueError("屏障事件无法可靠提交。") from None

    def submit_barrier_batch(
        self, pending: tuple[tuple[str, BaseModel], ...]
    ) -> tuple[SessionEvent, ...]:
        """排空旧普通事件后，把一个完整逻辑单元作为预检批次提交。"""
        if not pending:
            return ()
        with self._lock:
            self._ensure_open()
            self._flush_locked()
            try:
                return self._workflow.append_batch(pending)
            except Exception as exc:
                self._poison_locked(exc)
                raise EventQueueError("屏障事件批次无法可靠提交。") from None

    def submit_urgent(self, event_type: str, data: BaseModel) -> SessionEvent:
        with self._lock:
            self._ensure_open()
            callback = self._cancel_generation
            if callback is not None:
                try:
                    callback()
                except Exception:
                    # The urgent fact still has to be appended even if a host
                    # stream adapter cannot be closed physically.
                    pass
            pending = self._detach_locked()
            batch = [(item.event_type, item.data) for item in pending]
            batch.append((event_type, data))
            try:
                committed = self._workflow.append_batch(batch)
            except Exception as exc:
                for item in pending:
                    item.receipt._fail(exc)
                self._poison_locked(exc)
                raise EventQueueError("紧急事件无法可靠提交。") from None
            for item, event in zip(pending, committed[:-1], strict=True):
                item.receipt._succeed(event)
            return committed[-1]

    def flush(self) -> tuple[SessionEvent, ...]:
        with self._lock:
            self._ensure_open()
            return self._flush_locked()

    def accept_task_event(self, event: AsyncTaskEvent) -> SessionEvent:
        """Map a frozen runtime event into the durable SessionEvent schema."""
        event_type, data = _map_task_event(event)
        if event_type in {
            "async/task/accepted",
            "async/task/started",
            "async/task/cancel_requested",
            "async/task/cancelled",
        }:
            return self.submit_barrier(event_type, data)
        # Normal task events participate in batching, but the producing worker
        # does not publish a terminal runtime state until persistence completes.
        return self.submit_normal(event_type, data).wait()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if not self._failed:
                self._flush_locked()
            self._closed = True
            self._cancel_timer_locked()

    def emergency_close(self) -> None:
        """Close without flushing normal events during bounded shutdown."""
        if not self._lock.acquire(timeout=0.1):
            start_daemon_cleanup(
                self._emergency_close_blocking,
                name="agent-event-queue-close",
            )
            return
        try:
            self._emergency_close_locked()
        finally:
            self._lock.release()

    def _emergency_close_blocking(self) -> None:
        with self._lock:
            self._emergency_close_locked()

    def _emergency_close_locked(self) -> None:
        if self._closed:
            return
        pending = self._detach_locked()
        error = EventQueueError("Agent 已紧急退出，未提交的普通事件已丢弃。")
        for item in pending:
            item.receipt._fail(error)
        self._closed = True
        self._cancel_timer_locked()

    def _timer_flush(self) -> None:
        with self._lock:
            self._timer = None
            if self._closed or self._failed or not self._pending:
                return
            try:
                self._flush_locked()
            except EventQueueError:
                return

    def _flush_locked(self) -> tuple[SessionEvent, ...]:
        pending = self._detach_locked()
        if not pending:
            return ()
        try:
            committed = self._workflow.append_batch(
                (item.event_type, item.data) for item in pending
            )
        except Exception as exc:
            for item in pending:
                item.receipt._fail(exc)
            self._poison_locked(exc)
            raise EventQueueError("普通事件批量提交失败。") from None
        for item, event in zip(pending, committed, strict=True):
            item.receipt._succeed(event)
        return committed

    def _detach_locked(self) -> list[_Pending]:
        self._cancel_timer_locked()
        pending = self._pending
        self._pending = []
        self._pending_bytes = 0
        return pending

    def _cancel_timer_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _poison_locked(self, error: BaseException) -> None:
        self._failed = True
        self._cancel_timer_locked()
        for item in self._pending:
            item.receipt._fail(error)
        self._pending = []
        self._pending_bytes = 0

    def _ensure_open(self) -> None:
        if self._closed or self._failed:
            raise EventQueueError("事件分发器已经关闭。")


def _result_snapshot(raw: str | None):
    if raw is None:
        return None
    return tool_result_snapshot(ToolResult.model_validate_json(raw))


def _map_task_event(event: AsyncTaskEvent) -> tuple[str, BaseModel]:
    if event.kind == "async/task/accepted":
        return event.kind, AsyncTaskAcceptedEventData(
            task_id=event.task_id,
            turn_id=event.turn_id,
            step=event.step,
            call_id=event.call_id,
            tool_name=event.tool_name,
            request_json=event.request_json,
            retry_safety=event.retry_safety,
            resource_kind=event.resource_kind,
            cancellable=event.cancellable,
        )
    if event.kind == "async/task/started":
        return event.kind, AsyncTaskStartedEventData(task_id=event.task_id)
    if event.kind == "async/task/progress":
        return event.kind, AsyncTaskProgressEventData(
            task_id=event.task_id,
            phase=event.phase,
            completed=event.completed,
            total=event.total,
            progress=event.progress,
            message=event.message,
        )
    if event.kind == "async/task/completed":
        return event.kind, AsyncTaskCompletedEventData(
            task_id=event.task_id, result=_result_snapshot(event.result_json)
        )
    if event.kind == "async/task/failed":
        return event.kind, AsyncTaskFailedEventData(
            task_id=event.task_id,
            error=event.error or "异步任务执行失败。",
            result=_result_snapshot(event.result_json),
        )
    if event.kind == "async/task/cancel_requested":
        return event.kind, AsyncTaskCancelRequestedEventData(
            task_id=event.task_id, reason=event.reason or "请求取消后台任务。"
        )
    if event.kind == "async/task/cancelled":
        return event.kind, AsyncTaskCancelledEventData(
            task_id=event.task_id, reason=event.reason or "后台任务已取消。"
        )
    if event.kind == "async/task/late_result":
        return event.kind, AsyncTaskLateResultEventData(
            task_id=event.task_id,
            status=event.late_status or "failed",
            result=_result_snapshot(event.result_json),
        )
    raise EventQueueError("未知异步任务事件。")
