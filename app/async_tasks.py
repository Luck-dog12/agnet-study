"""Bounded in-process asynchronous tool task execution.

The manager owns worker scheduling and volatile task state only.  Durable session
events remain the responsibility of the injected ``event_sink``.  The sink is
called synchronously and must return only after accepting the event into the
session event workflow; an exception therefore fails closed instead of silently
starting work whose lifecycle was not recorded.

Workers are deliberately not restored after a process restart.  ``from_events``
reconstructs queryable state and marks an accepted-only task ``not_started`` and
a task which had started without a durable terminal event ``outcome_unknown``.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import math
import re
from threading import Condition, Event, RLock
from time import monotonic
from typing import Callable, Iterable, Literal
from uuid import uuid4

from app.schemas import ToolResult
from app.daemon_executor import DaemonExecutor, start_daemon_cleanup


_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MAX_PHASE_LENGTH = 128
_MAX_MESSAGE_LENGTH = 16_384
_MAX_REASON_LENGTH = 16_384
_MAX_REQUEST_BYTES = 1 * 1024 * 1024
_MAX_RESULT_BYTES = 8 * 1024 * 1024

AsyncTaskEventKind = Literal[
    "async/task/accepted",
    "async/task/started",
    "async/task/progress",
    "async/task/completed",
    "async/task/failed",
    "async/task/cancel_requested",
    "async/task/cancelled",
    "async/task/late_result",
]
AsyncResultStatus = Literal["success", "error", "denied"]
RetrySafety = Literal["safe", "inspect_external"]


class AsyncTaskStatus(str, Enum):
    """Runtime and recovered states exposed to the host."""

    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NOT_STARTED = "not_started"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AsyncTaskError(RuntimeError):
    """Base error for invalid task lifecycle operations."""


class AsyncTaskNotFound(AsyncTaskError):
    """The task does not exist in the requesting session."""


class AsyncTaskEventSinkError(AsyncTaskError):
    """A lifecycle event could not be handed to durable persistence."""


class AsyncTaskManagerClosed(AsyncTaskError):
    """The manager is shutting down and no longer accepts tasks."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _require_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是 32 位小写十六进制标识。")
    return value


def _require_text(value: str, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} 必须是非空且长度不超过 {maximum} 的文本。")
    if "\x00" in value:
        raise ValueError(f"{label} 不能包含 NUL。")
    return value


def _canonical_json(raw: str, *, maximum: int, label: str) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > maximum:
        raise ValueError(f"{label} 不是有界 JSON 快照。")
    try:
        value = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"不允许 {value}")
        ))
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(f"{label} 必须是可无损保存的严格 JSON。") from None
    if len(canonical.encode("utf-8")) > maximum:
        raise ValueError(f"{label} 超过大小限制。")
    return canonical


@dataclass(frozen=True, slots=True)
class AsyncTaskRequest:
    """Immutable binding between one model tool call and one background task."""

    session_id: str
    turn_id: str
    step: int
    call_id: str
    tool_name: str
    request_json: str
    resource_kind: str
    cancellable: bool = False
    retry_safety: RetrySafety = "safe"

    def __post_init__(self) -> None:
        _require_id(self.session_id, "session_id")
        _require_id(self.turn_id, "turn_id")
        if isinstance(self.step, bool) or not isinstance(self.step, int) or self.step < 0:
            raise ValueError("step 必须是非负整数。")
        _require_text(self.call_id, "call_id", 512)
        _require_text(self.tool_name, "tool_name", 128)
        _require_text(self.resource_kind, "resource_kind", 128)
        if not isinstance(self.cancellable, bool):
            raise TypeError("cancellable 必须是布尔值。")
        if self.retry_safety not in {"safe", "inspect_external"}:
            raise ValueError("retry_safety 无效。")
        object.__setattr__(
            self,
            "request_json",
            _canonical_json(
                self.request_json, maximum=_MAX_REQUEST_BYTES, label="request_json"
            ),
        )


@dataclass(frozen=True, slots=True)
class AsyncTaskExecutionResult:
    """Validated result returned by a prepared tool runner."""

    status: AsyncResultStatus
    result_json: str
    error: str | None = None

    def __post_init__(self) -> None:
        canonical = _canonical_json(
            self.result_json, maximum=_MAX_RESULT_BYTES, label="result_json"
        )
        result = ToolResult.model_validate_json(canonical)
        if result.status != self.status:
            raise ValueError("结果状态与 result_json 不一致。")
        if self.status == "success" and self.error is not None:
            raise ValueError("成功结果不能包含 error。")
        if self.status != "success":
            if self.error is None:
                object.__setattr__(self, "error", result.error)
            else:
                _require_text(self.error, "error", 32_768)
        object.__setattr__(self, "result_json", canonical)

    @classmethod
    def from_tool_result(cls, result: ToolResult) -> "AsyncTaskExecutionResult":
        validated = ToolResult.model_validate(result.model_dump())
        raw = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(status=validated.status, result_json=raw, error=validated.error)


@dataclass(frozen=True, slots=True)
class AsyncTaskEvent:
    """Frozen internal candidate which the integration layer maps to SessionEvent."""

    kind: AsyncTaskEventKind
    task_id: str
    session_id: str
    occurred_at: str
    turn_id: str | None = None
    step: int | None = None
    call_id: str | None = None
    tool_name: str | None = None
    request_json: str | None = None
    retry_safety: RetrySafety | None = None
    resource_kind: str | None = None
    cancellable: bool | None = None
    phase: str | None = None
    completed: int | None = None
    total: int | None = None
    progress: float | None = None
    message: str | None = None
    reason: str | None = None
    error: str | None = None
    result_status: AsyncResultStatus | None = None
    result_json: str | None = None
    late_status: Literal["completed", "failed"] | None = None
    recovery_status: Literal["recorded", "not_started", "outcome_unknown"] = "recorded"


@dataclass(frozen=True, slots=True)
class AsyncTaskSnapshot:
    """Safe, immutable task state returned by query/list/cancel."""

    task_id: str
    session_id: str
    turn_id: str
    step: int
    call_id: str
    tool_name: str
    resource_kind: str
    cancellable: bool
    retry_safety: RetrySafety
    status: AsyncTaskStatus
    accepted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    phase: str | None = None
    completed: int | None = None
    total: int | None = None
    progress: float | None = None
    message: str | None = None
    reason: str | None = None
    error: str | None = None
    result_status: AsyncResultStatus | None = None
    result_json: str | None = None
    late_result_status: AsyncResultStatus | None = None
    late_result_json: str | None = None


@dataclass(slots=True)
class _TaskRecord:
    task_id: str
    request: AsyncTaskRequest
    status: AsyncTaskStatus
    accepted_at: str
    runner: Callable[["AsyncTaskContext"], AsyncTaskExecutionResult] | None
    cancel_hook: Callable[[], None] | None
    started_at: str | None = None
    finished_at: str | None = None
    phase: str | None = None
    completed: int | None = None
    total: int | None = None
    progress: float | None = None
    message: str | None = None
    reason: str | None = None
    error: str | None = None
    result_status: AsyncResultStatus | None = None
    result_json: str | None = None
    late_result_status: AsyncResultStatus | None = None
    late_result_json: str | None = None
    terminal_seen: bool = False
    last_progress_at: float | None = None
    closed_phases: set[str] = field(default_factory=set)
    future: Future[None] | None = None


def _project_restored_records(
    events: Iterable[AsyncTaskEvent],
) -> dict[str, _TaskRecord]:
    """Pure durable-event projection shared by recovery and startup checks."""
    records: dict[str, _TaskRecord] = {}
    for event in events:
        if not isinstance(event, AsyncTaskEvent):
            raise TypeError("恢复只接受 AsyncTaskEvent。")
        task_id = _require_id(event.task_id, "task_id")
        if event.kind == "async/task/accepted":
            if task_id in records:
                raise ValueError("同一 task_id 不能重复 accepted。")
            if None in {
                event.turn_id,
                event.step,
                event.call_id,
                event.tool_name,
                event.request_json,
                event.retry_safety,
                event.resource_kind,
                event.cancellable,
            }:
                raise ValueError("accepted 事件缺少任务绑定字段。")
            request = AsyncTaskRequest(
                session_id=event.session_id,
                turn_id=event.turn_id,  # type: ignore[arg-type]
                step=event.step,  # type: ignore[arg-type]
                call_id=event.call_id,  # type: ignore[arg-type]
                tool_name=event.tool_name,  # type: ignore[arg-type]
                request_json=event.request_json,  # type: ignore[arg-type]
                retry_safety=event.retry_safety,  # type: ignore[arg-type]
                resource_kind=event.resource_kind,  # type: ignore[arg-type]
                cancellable=event.cancellable,  # type: ignore[arg-type]
            )
            records[task_id] = _TaskRecord(
                task_id=task_id,
                request=request,
                status=AsyncTaskStatus.ACCEPTED,
                accepted_at=event.occurred_at,
                runner=None,
                cancel_hook=None,
            )
            continue
        record = records.get(task_id)
        if record is None or record.request.session_id != event.session_id:
            raise ValueError("任务事件没有对应的 accepted 事件。")
        if record.terminal_seen and event.kind != "async/task/late_result":
            raise ValueError("任务终态后不能追加普通生命周期事件。")
        if event.kind == "async/task/started":
            if record.status != AsyncTaskStatus.ACCEPTED:
                raise ValueError("started 只能跟在 accepted 后。")
            record.status = AsyncTaskStatus.RUNNING
            record.started_at = event.occurred_at
        elif event.kind == "async/task/progress":
            if record.status != AsyncTaskStatus.RUNNING:
                raise ValueError("progress 只能跟在 started 后。")
            record.phase = event.phase
            record.completed = event.completed
            record.total = event.total
            record.progress = event.progress
            record.message = event.message
        elif event.kind == "async/task/completed":
            record.status = AsyncTaskStatus.COMPLETED
            record.terminal_seen = True
            record.result_status = event.result_status
            record.result_json = event.result_json
            record.finished_at = event.occurred_at
        elif event.kind == "async/task/failed":
            record.status = (
                AsyncTaskStatus(event.recovery_status)
                if event.recovery_status != "recorded"
                else AsyncTaskStatus.FAILED
            )
            record.terminal_seen = True
            record.error = event.error
            record.result_status = event.result_status
            record.result_json = event.result_json
            record.finished_at = event.occurred_at
        elif event.kind == "async/task/cancel_requested":
            record.reason = event.reason
        elif event.kind == "async/task/cancelled":
            record.status = AsyncTaskStatus.CANCELLED
            record.terminal_seen = True
            record.reason = event.reason
            record.finished_at = event.occurred_at
        elif event.kind == "async/task/late_result":
            if record.status != AsyncTaskStatus.CANCELLED:
                raise ValueError("late_result 只允许跟在 cancelled 后。")
            record.late_result_status = event.result_status
            record.late_result_json = event.result_json
        else:  # pragma: no cover - Literal protects normal construction
            raise ValueError("未知异步任务事件。")
    for record in records.values():
        if record.status == AsyncTaskStatus.ACCEPTED:
            record.status = AsyncTaskStatus.NOT_STARTED
        elif record.status == AsyncTaskStatus.RUNNING:
            record.status = AsyncTaskStatus.OUTCOME_UNKNOWN
    return records


def has_started_without_terminal_task(events: Iterable[AsyncTaskEvent]) -> bool:
    """Return whether durable history has a started task without a terminal.

    This is a pure read-only predicate.  It intentionally shares the exact
    projector used by ``AsyncTaskManager.from_events`` so accepted-only tasks
    remain ``not_started`` and a durable recovery failure is still recognized
    as a terminal rather than being confused with its ``outcome_unknown``
    status value.
    """
    return any(
        record.started_at is not None and not record.terminal_seen
        for record in _project_restored_records(events).values()
    )


EventSink = Callable[[AsyncTaskEvent], None]


class AsyncTaskContext:
    """Cooperative progress/cancellation view passed to a task runner."""

    __slots__ = ("_manager", "_task_id", "_session_id")

    def __init__(self, manager: "AsyncTaskManager", record: _TaskRecord) -> None:
        self._manager = manager
        self._task_id = record.task_id
        self._session_id = record.request.session_id

    @property
    def cancellation_requested(self) -> bool:
        snapshot = self._manager.query(self._session_id, self._task_id)
        return snapshot.status == AsyncTaskStatus.CANCELLED

    def report_progress(
        self,
        *,
        phase: str,
        completed: int,
        total: int,
        message: str,
    ) -> bool:
        """Record monotonic progress; returns False when rate-limited.

        A phase cannot be revisited after moving to another phase.  The derived
        ``completed / total`` fraction must also be globally monotonic so the
        model never observes progress moving backwards.
        """

        return self._manager._report_progress(
            self._session_id,
            self._task_id,
            phase=phase,
            completed=completed,
            total=total,
            message=message,
        )


class AsyncTaskManager:
    """Concurrency-limited asynchronous task scheduler with durable event hooks."""

    def __init__(
        self,
        *,
        event_sink: EventSink,
        max_workers: int = 2,
        progress_interval_seconds: float = 0.1,
    ) -> None:
        if not callable(event_sink):
            raise TypeError("event_sink 必须可调用。")
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
            raise ValueError("max_workers 必须是 1..32。")
        if (
            isinstance(progress_interval_seconds, bool)
            or not isinstance(progress_interval_seconds, (int, float))
            or not math.isfinite(float(progress_interval_seconds))
            or not 0 <= float(progress_interval_seconds) <= 60
        ):
            raise ValueError("progress_interval_seconds 必须是 0..60 的有限数。")
        self._event_sink = event_sink
        self._executor = DaemonExecutor(
            max_workers=max_workers, thread_name_prefix="agent-async-tool"
        )
        self._progress_interval = float(progress_interval_seconds)
        self._lock = RLock()
        self._sink_lock = RLock()
        self._records: dict[str, _TaskRecord] = {}
        self._changed = Condition(self._lock)
        self._closed = False
        self._sink_failed = False
        self._shutdown_requested = Event()

    def accept(
        self,
        request: AsyncTaskRequest,
        runner: Callable[[AsyncTaskContext], AsyncTaskExecutionResult],
        *,
        cancel_hook: Callable[[], None] | None = None,
    ) -> AsyncTaskSnapshot:
        """Durably accept a task without making it visible to a worker yet."""

        if not isinstance(request, AsyncTaskRequest):
            raise TypeError("request 必须是 AsyncTaskRequest。")
        if not callable(runner):
            raise TypeError("runner 必须可调用。")
        if cancel_hook is not None and not callable(cancel_hook):
            raise TypeError("cancel_hook 必须可调用。")
        if cancel_hook is not None and not request.cancellable:
            raise ValueError("不可取消的工具不能注册 cancel_hook。")
        task_id = uuid4().hex
        accepted_at = _utc_now()
        record = _TaskRecord(
            task_id=task_id,
            request=request,
            status=AsyncTaskStatus.ACCEPTED,
            accepted_at=accepted_at,
            runner=runner,
            cancel_hook=cancel_hook,
        )
        event = self._event_for(record, "async/task/accepted")
        with self._lock:
            self._ensure_accepting()
            # The accepted fact must exist before the executor can run it.
            self._emit(event)
            self._records[task_id] = record
            self._changed.notify_all()
            return self._snapshot(record)

    def start(self, session_id: str, task_id: str) -> AsyncTaskSnapshot:
        """Schedule an accepted task after its placeholder result is durable."""
        with self._lock:
            self._ensure_accepting()
            record = self._get_record(session_id, task_id)
            if record.status != AsyncTaskStatus.ACCEPTED:
                return self._snapshot(record)
            try:
                record.future = self._executor.submit(self._run_task, task_id)
            except Exception:
                record.status = AsyncTaskStatus.NOT_STARTED
                record.error = "后台执行器未能启动任务。"
                self._changed.notify_all()
                raise AsyncTaskError("后台执行器未能启动任务。") from None
            return self._snapshot(record)

    def submit(
        self,
        request: AsyncTaskRequest,
        runner: Callable[[AsyncTaskContext], AsyncTaskExecutionResult],
        *,
        cancel_hook: Callable[[], None] | None = None,
    ) -> AsyncTaskSnapshot:
        """Compatibility helper; integrations needing a placeholder use accept/start."""
        accepted = self.accept(request, runner, cancel_hook=cancel_hook)
        return self.start(accepted.session_id, accepted.task_id)

    def query(self, session_id: str, task_id: str) -> AsyncTaskSnapshot:
        _require_id(session_id, "session_id")
        _require_id(task_id, "task_id")
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.request.session_id != session_id:
                raise AsyncTaskNotFound("当前会话中不存在该异步任务。")
            return self._snapshot(record)

    def list(self, session_id: str) -> tuple[AsyncTaskSnapshot, ...]:
        _require_id(session_id, "session_id")
        with self._lock:
            return tuple(
                self._snapshot(record)
                for record in self._records.values()
                if record.request.session_id == session_id
            )

    def cancel(
        self,
        session_id: str,
        task_id: str,
        *,
        reason: str = "用户请求取消后台任务。",
    ) -> AsyncTaskSnapshot:
        """Logically cancel immediately and attempt physical cancellation."""

        _require_text(reason, "reason", _MAX_REASON_LENGTH)
        cancel_hook: Callable[[], None] | None = None
        future: Future[None] | None = None
        with self._lock:
            record = self._get_record(session_id, task_id)
            if record.status in {
                AsyncTaskStatus.COMPLETED,
                AsyncTaskStatus.FAILED,
                AsyncTaskStatus.CANCELLED,
                AsyncTaskStatus.NOT_STARTED,
                AsyncTaskStatus.OUTCOME_UNKNOWN,
            }:
                return self._snapshot(record)
            record.status = AsyncTaskStatus.CANCELLED
            record.reason = reason
            record.finished_at = _utc_now()
            # Terminal state is fixed before emitting.  Even if persistence now
            # fails, a late worker result cannot be promoted to completed.
            try:
                self._emit(self._event_for(record, "async/task/cancel_requested"))
                self._emit(self._event_for(record, "async/task/cancelled"))
            finally:
                cancel_hook = record.cancel_hook if record.request.cancellable else None
                future = record.future
            snapshot = self._snapshot(record)
            self._changed.notify_all()
        if future is not None:
            future.cancel()
        if cancel_hook is not None:
            try:
                cancel_hook()
            except Exception:
                # Cancellation is already logically final.  Physical termination
                # is explicitly best-effort and cannot reverse that decision.
                pass
        return snapshot

    def has_active(self, session_id: str) -> bool:
        return any(
            item.status in {AsyncTaskStatus.ACCEPTED, AsyncTaskStatus.RUNNING}
            for item in self.list(session_id)
        )

    def wait_for_change(
        self,
        session_id: str,
        previous: tuple[tuple[str, AsyncTaskStatus], ...],
        *,
        timeout: float | None = None,
    ) -> tuple[AsyncTaskSnapshot, ...]:
        """Wait until the session's compact task-state vector changes."""
        _require_id(session_id, "session_id")
        with self._changed:
            def current() -> tuple[tuple[str, AsyncTaskStatus], ...]:
                return tuple(
                    (record.task_id, record.status)
                    for record in self._records.values()
                    if record.request.session_id == session_id
                )

            self._changed.wait_for(lambda: current() != previous, timeout=timeout)
            return tuple(
                self._snapshot(record)
                for record in self._records.values()
                if record.request.session_id == session_id
            )

    def shutdown(self, *, wait: bool = True) -> None:
        self._shutdown_requested.set()
        if wait:
            with self._lock:
                if self._closed:
                    return
                self._closed = True
            self._executor.shutdown(wait=True, cancel_futures=True)
            return

        # Do not let a worker which is blocked in synchronous event persistence
        # keep the caller waiting for the task-state lock.  Executor shutdown
        # fences queued work immediately; state cleanup and physical cancel
        # hooks are best-effort daemon work when the lock is contended.
        self._executor.shutdown(wait=False, cancel_futures=True)
        if not self._lock.acquire(timeout=0.05):
            start_daemon_cleanup(
                self._finish_emergency_shutdown,
                name="agent-async-state-close",
            )
            return
        try:
            cancellations = self._mark_emergency_cancelled_locked()
        finally:
            self._lock.release()
        if cancellations:
            start_daemon_cleanup(
                lambda: self._cancel_handles(cancellations),
                name="agent-async-process-cancel",
            )

    def _finish_emergency_shutdown(self) -> None:
        with self._lock:
            cancellations = self._mark_emergency_cancelled_locked()
        self._cancel_handles(cancellations)

    def _mark_emergency_cancelled_locked(
        self,
    ) -> tuple[tuple[Callable[[], None] | None, Future[None] | None], ...]:
        if self._closed:
            return ()
        self._closed = True
        cancellations: list[
            tuple[Callable[[], None] | None, Future[None] | None]
        ] = []
        for record in self._records.values():
            if record.status not in {
                AsyncTaskStatus.ACCEPTED,
                AsyncTaskStatus.RUNNING,
            }:
                continue
            record.status = AsyncTaskStatus.CANCELLED
            record.reason = "Agent 紧急退出，后台任务已取消。"
            record.finished_at = _utc_now()
            cancellations.append((record.cancel_hook, record.future))
        self._changed.notify_all()
        return tuple(cancellations)

    @staticmethod
    def _cancel_handles(
        cancellations: tuple[
            tuple[Callable[[], None] | None, Future[None] | None], ...
        ],
    ) -> None:
        for cancel_hook, future in cancellations:
            if cancel_hook is not None:
                try:
                    cancel_hook()
                except Exception:
                    pass
            if future is not None:
                future.cancel()

    def __enter__(self) -> "AsyncTaskManager":
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown(wait=True)

    @classmethod
    def from_events(
        cls,
        events: Iterable[AsyncTaskEvent],
        *,
        event_sink: EventSink,
        max_workers: int = 2,
        progress_interval_seconds: float = 0.1,
    ) -> "AsyncTaskManager":
        """Restore query state without recreating or resuming any worker."""

        manager = cls(
            event_sink=event_sink,
            max_workers=max_workers,
            progress_interval_seconds=progress_interval_seconds,
        )
        try:
            manager._restore(events)
        except Exception:
            manager.shutdown(wait=False)
            raise
        return manager

    def _restore(self, events: Iterable[AsyncTaskEvent]) -> None:
        restored = _project_restored_records(events)
        with self._lock:
            if self._records:
                raise RuntimeError("异步任务恢复只能执行一次。")
            self._records.update(restored)

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            record = self._records[task_id]
            if self._shutdown_requested.is_set() or record.status == AsyncTaskStatus.CANCELLED:
                return
            if record.status != AsyncTaskStatus.ACCEPTED or record.runner is None:
                return
            record.status = AsyncTaskStatus.RUNNING
            record.started_at = _utc_now()
            try:
                self._emit(self._event_for(record, "async/task/started"))
            except AsyncTaskEventSinkError:
                # The executor definitely did not run.  Durable history contains
                # accepted only, which recovery maps to not_started.
                record.status = AsyncTaskStatus.NOT_STARTED
                record.error = "任务开始事件无法可靠保存，执行已拒绝。"
                record.runner = None
                self._changed.notify_all()
                return
            if self._shutdown_requested.is_set():
                # The started fact may already be durable, but shutdown won
                # while its sink was blocked.  Do not begin user/tool code
                # after the emergency caller has returned; recovery will map
                # the durable started-without-terminal history conservatively.
                record.status = AsyncTaskStatus.CANCELLED
                record.reason = "Agent 紧急退出，后台任务未启动。"
                record.finished_at = _utc_now()
                record.runner = None
                self._changed.notify_all()
                return
            self._changed.notify_all()
            runner = record.runner
        context = AsyncTaskContext(self, record)
        if self._shutdown_requested.is_set():
            return
        try:
            outcome = runner(context)
            if not isinstance(outcome, AsyncTaskExecutionResult):
                raise TypeError("runner 必须返回 AsyncTaskExecutionResult。")
        except Exception:
            outcome = None

        with self._lock:
            record = self._records[task_id]
            if record.status == AsyncTaskStatus.CANCELLED:
                if outcome is not None:
                    record.late_result_status = outcome.status
                    record.late_result_json = outcome.result_json
                    event = self._event_for(
                        record,
                        "async/task/late_result",
                        late_status=(
                            "completed" if outcome.status == "success" else "failed"
                        ),
                    )
                else:
                    event = self._event_for(
                        record,
                        "async/task/late_result",
                        late_status="failed",
                        error="取消后的后台执行以异常结束。",
                    )
                try:
                    self._emit(event)
                except AsyncTaskEventSinkError:
                    pass
                self._changed.notify_all()
                return

            record.finished_at = _utc_now()
            if outcome is not None and outcome.status == "success":
                record.result_status = outcome.status
                record.result_json = outcome.result_json
                try:
                    self._emit(self._event_for(record, "async/task/completed"))
                except AsyncTaskEventSinkError:
                    record.status = AsyncTaskStatus.OUTCOME_UNKNOWN
                    record.error = "任务结果无法可靠保存。"
                    self._changed.notify_all()
                    return
                record.status = AsyncTaskStatus.COMPLETED
                self._changed.notify_all()
                return

            record.status = AsyncTaskStatus.FAILED
            record.result_status = outcome.status if outcome is not None else None
            record.result_json = outcome.result_json if outcome is not None else None
            record.error = (
                outcome.error
                if outcome is not None and outcome.error
                else "异步任务执行失败。"
            )
            try:
                self._emit(self._event_for(record, "async/task/failed"))
            except AsyncTaskEventSinkError:
                record.status = AsyncTaskStatus.OUTCOME_UNKNOWN
                record.error = "任务失败结果无法可靠保存。"
            self._changed.notify_all()

    def _report_progress(
        self,
        session_id: str,
        task_id: str,
        *,
        phase: str,
        completed: int,
        total: int,
        message: str,
    ) -> bool:
        _require_text(phase, "phase", _MAX_PHASE_LENGTH)
        _require_text(message, "message", _MAX_MESSAGE_LENGTH)
        if (
            isinstance(completed, bool)
            or isinstance(total, bool)
            or not isinstance(completed, int)
            or not isinstance(total, int)
            or total <= 0
            or completed < 0
            or completed > total
        ):
            raise ValueError("进度必须满足 0 <= completed <= total 且 total > 0。")
        progress = completed / total
        now = monotonic()
        with self._lock:
            record = self._get_record(session_id, task_id)
            if record.status != AsyncTaskStatus.RUNNING:
                raise AsyncTaskError("只有运行中的任务可以报告进度。")
            if phase in record.closed_phases:
                raise ValueError("已经结束的 phase 不能再次使用。")
            if record.phase == phase:
                if record.total != total or (
                    record.completed is not None and completed < record.completed
                ):
                    raise ValueError("同一 phase 的 total 必须固定且 completed 单调递增。")
            elif record.phase is not None:
                record.closed_phases.add(record.phase)
            if record.progress is not None and progress < record.progress:
                raise ValueError("任务总进度不能倒退。")
            record.phase = phase
            record.completed = completed
            record.total = total
            record.progress = progress
            record.message = message
            should_emit = (
                record.last_progress_at is None
                or now - record.last_progress_at >= self._progress_interval
                or completed == total
            )
            if not should_emit:
                return False
            self._emit(self._event_for(record, "async/task/progress"))
            record.last_progress_at = now
            return True

    def _get_record(self, session_id: str, task_id: str) -> _TaskRecord:
        _require_id(session_id, "session_id")
        _require_id(task_id, "task_id")
        record = self._records.get(task_id)
        if record is None or record.request.session_id != session_id:
            raise AsyncTaskNotFound("当前会话中不存在该异步任务。")
        return record

    def _ensure_accepting(self) -> None:
        if self._closed or self._shutdown_requested.is_set():
            raise AsyncTaskManagerClosed("异步任务管理器已经关闭。")
        if self._sink_failed:
            raise AsyncTaskEventSinkError("事件接收器此前已经失败。")

    def _emit(self, event: AsyncTaskEvent) -> None:
        with self._sink_lock:
            try:
                self._event_sink(event)
            except Exception:
                self._sink_failed = True
                raise AsyncTaskEventSinkError("异步任务事件无法可靠提交。") from None

    def _event_for(
        self,
        record: _TaskRecord,
        kind: AsyncTaskEventKind,
        *,
        late_status: Literal["completed", "failed"] | None = None,
        error: str | None = None,
    ) -> AsyncTaskEvent:
        request = record.request
        return AsyncTaskEvent(
            kind=kind,
            task_id=record.task_id,
            session_id=request.session_id,
            occurred_at=_utc_now(),
            turn_id=request.turn_id if kind == "async/task/accepted" else None,
            step=request.step if kind == "async/task/accepted" else None,
            call_id=request.call_id if kind == "async/task/accepted" else None,
            tool_name=request.tool_name if kind == "async/task/accepted" else None,
            request_json=request.request_json if kind == "async/task/accepted" else None,
            retry_safety=request.retry_safety if kind == "async/task/accepted" else None,
            resource_kind=request.resource_kind if kind == "async/task/accepted" else None,
            cancellable=request.cancellable if kind == "async/task/accepted" else None,
            phase=record.phase if kind == "async/task/progress" else None,
            completed=record.completed if kind == "async/task/progress" else None,
            total=record.total if kind == "async/task/progress" else None,
            progress=record.progress if kind == "async/task/progress" else None,
            message=record.message if kind == "async/task/progress" else None,
            reason=(
                record.reason
                if kind in {"async/task/cancel_requested", "async/task/cancelled"}
                else None
            ),
            error=(record.error if kind == "async/task/failed" else error),
            result_status=(
                record.result_status
                if kind in {"async/task/completed", "async/task/failed"}
                else record.late_result_status
                if kind == "async/task/late_result"
                else None
            ),
            result_json=(
                record.result_json
                if kind in {"async/task/completed", "async/task/failed"}
                else record.late_result_json
                if kind == "async/task/late_result"
                else None
            ),
            late_status=late_status,
        )

    @staticmethod
    def _snapshot(record: _TaskRecord) -> AsyncTaskSnapshot:
        request = record.request
        return AsyncTaskSnapshot(
            task_id=record.task_id,
            session_id=request.session_id,
            turn_id=request.turn_id,
            step=request.step,
            call_id=request.call_id,
            tool_name=request.tool_name,
            resource_kind=request.resource_kind,
            cancellable=request.cancellable,
            retry_safety=request.retry_safety,
            status=record.status,
            accepted_at=record.accepted_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            phase=record.phase,
            completed=record.completed,
            total=record.total,
            progress=record.progress,
            message=record.message,
            reason=record.reason,
            error=record.error,
            result_status=record.result_status,
            result_json=record.result_json,
            late_result_status=record.late_result_status,
            late_result_json=record.late_result_json,
        )
