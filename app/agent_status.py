"""Read-only host status projected from a committed session-event prefix.

This module never flushes events or inspects worker locks. Pending progress may
lag behind execution; ``last_event_seq`` identifies the observed durable prefix.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas import (
    AsyncTaskAcceptedEventData, AsyncTaskCancelledEventData,
    AsyncTaskCompletedEventData, AsyncTaskFailedEventData,
    AsyncTaskProgressEventData, AsyncTaskStartedEventData,
    RecoveryResolutionEventData, SessionEvent, StepStartEventData,
    ToolCallEventData, ToolResultEventData, TurnEndEventData, TurnStartEventData,
)


RunPhase = Literal["idle", "running", "waiting_approval", "waiting_plan", "waiting_async"]
RunState = Literal[
    "idle", "running", "waiting_approval", "waiting_plan", "waiting_async",
    "stopped", "failed",
]
AgentMode = Literal["act", "plan"]
TaskState = Literal[
    "accepted", "running", "completed", "failed", "cancelled", "not_started",
    "outcome_unknown",
]


class _StatusModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TurnStatus(_StatusModel):
    turn_id: str
    started_at: str
    step: int | None = None
    kind: Literal["completed", "interrupted", "error"] | None = None
    finished_at: str | None = None


class TaskStatus(_StatusModel):
    task_id: str
    turn_id: str
    tool_name: str
    state: TaskState
    cancellable: bool
    progress: float | None = None


class TaskCounts(_StatusModel):
    total: int = 0
    active: int = 0
    accepted: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    not_started: int = 0
    outcome_unknown: int = 0


class AgentStatus(_StatusModel):
    """Host snapshot; completion describes a turn, not goal verification.

    ``run_state`` is the foreground phase. It can be idle while previously
    dispatched background tasks are still active. Plan has no durable turn until
    its successful final commit, so ``current_turn`` can be absent during planning.
    """

    session_id: str
    run_state: RunState
    mode: AgentMode | None
    current_turn: TurnStatus | None
    last_turn: TurnStatus | None
    tasks: tuple[TaskStatus, ...]
    task_counts: TaskCounts
    pending_recovery_count: int
    last_event_seq: int


def project_agent_status(
    session_id: str,
    events: tuple[SessionEvent, ...],
    *,
    phase: RunPhase = "idle",
    mode: AgentMode | None = None,
    failed: bool = False,
    stopped: bool = False,
) -> AgentStatus:
    """Project already validated events without changing execution or history.

    Unlike crash recovery, this preserves live accepted/running task states.
    Results, prompts, paths and free-form error/progress messages are not exposed.
    """
    current: TurnStatus | None = None
    last: TurnStatus | None = None
    tasks: dict[str, TaskStatus] = {}
    calls: dict[tuple[str, int, str], str] = {}
    async_calls: dict[str, tuple[str, int, str]] = {}
    unknown: set[tuple[str, int, str]] = set()
    resolved: set[tuple[str, int, str]] = set()
    for event in events:
        if event.session_id != session_id:
            raise ValueError("状态事件不属于当前会话。")
        data = event.data
        if isinstance(data, TurnStartEventData):
            current = TurnStatus(turn_id=data.turn_id, started_at=event.time)
        elif isinstance(data, StepStartEventData) and current is not None:
            current = current.model_copy(update={"step": data.step})
        elif isinstance(data, TurnEndEventData) and current is not None:
            last = current.model_copy(update={"kind": data.kind, "finished_at": event.time})
            current = None
        elif isinstance(data, ToolCallEventData):
            calls[(data.turn_id, data.step, data.call.id)] = data.retry_safety
        elif isinstance(data, ToolResultEventData):
            key = (data.turn_id, data.step, data.call_id)
            if data.recovery_status == "outcome_unknown" and calls.get(key) == "inspect_external":
                unknown.add(key)
        elif isinstance(data, AsyncTaskAcceptedEventData):
            tasks[data.task_id] = TaskStatus(
                task_id=data.task_id, turn_id=data.turn_id, tool_name=data.tool_name,
                state="accepted", cancellable=data.cancellable,
            )
            async_calls[data.task_id] = (data.turn_id, data.step, data.call_id)
        elif isinstance(data, AsyncTaskStartedEventData):
            tasks[data.task_id] = tasks[data.task_id].model_copy(update={"state": "running"})
        elif isinstance(data, AsyncTaskProgressEventData):
            tasks[data.task_id] = tasks[data.task_id].model_copy(update={"progress": data.progress})
        elif isinstance(data, AsyncTaskCompletedEventData):
            tasks[data.task_id] = tasks[data.task_id].model_copy(update={"state": "completed"})
        elif isinstance(data, AsyncTaskCancelledEventData):
            tasks[data.task_id] = tasks[data.task_id].model_copy(update={"state": "cancelled"})
        elif isinstance(data, AsyncTaskFailedEventData):
            state = "failed" if data.recovery_status == "recorded" else data.recovery_status
            tasks[data.task_id] = tasks[data.task_id].model_copy(update={"state": state})
            key = async_calls[data.task_id]
            if data.recovery_status == "outcome_unknown" and calls.get(key) == "inspect_external":
                unknown.add(key)
        elif isinstance(data, RecoveryResolutionEventData):
            resolved.add((data.turn_id, data.step, data.call_id))
        # Cancellation requests and late results do not replace a terminal.

    counts = {name: 0 for name in TaskCounts.model_fields}
    for task in tasks.values():
        counts[task.state] += 1
    counts["total"] = len(tasks)
    counts["active"] = counts["accepted"] + counts["running"]
    return AgentStatus(
        session_id=session_id,
        run_state="failed" if failed else "stopped" if stopped else phase,
        mode=mode,
        current_turn=current,
        last_turn=last,
        tasks=tuple(tasks.values()),
        task_counts=TaskCounts(**counts),
        pending_recovery_count=len(unknown - resolved),
        last_event_seq=events[-1].seq if events else 0,
    )
