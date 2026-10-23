"""SessionEvent 统一派生视图与中断恢复投影测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from app.schemas import (
    AsyncTaskAcceptedEventData,
    AsyncTaskCancelRequestedEventData,
    AsyncTaskCancelledEventData,
    AsyncTaskCompletedEventData,
    AsyncTaskFailedEventData,
    AsyncTaskLateResultEventData,
    AsyncTaskProgressEventData,
    AsyncTaskStartedEventData,
    AssistantAttemptEventData,
    AssistantMessage,
    AssistantMessageEventData,
    AssistantStreamSnapshot,
    CompactionSummaryEventData,
    ControlHostEventData,
    NormalHostEventData,
    RecoveryResolutionEventData,
    SessionAssistantMessageSnapshot,
    SessionEvent,
    SessionToolCallSnapshot,
    SessionUserMessageSnapshot,
    StepEndEventData,
    StepStartEventData,
    ToolCallEventData,
    ToolResult,
    ToolResultEventData,
    TurnEndEventData,
    TurnStartEventData,
    UrgentHostEventData,
    UserMessageEventData,
)
from app.workflow import (
    SessionEventWorkflow,
    assistant_message_snapshot,
    canonical_json_snapshot,
    tool_result_snapshot,
)


NOW = datetime(2026, 9, 19, 8, 0, tzinfo=timezone.utc)
TURN_ID = "a" * 32
TASK_ID = "b" * 32


def make_event(event_type: str, data: object, seq: int) -> SessionEvent:
    return SessionEvent(
        session_id="c" * 32,
        seq=seq,
        time=NOW.isoformat(timespec="microseconds"),
        type=event_type,
        data=data,
    )


class SessionEventStructuredViewTests(unittest.TestCase):
    def test_every_v1_event_has_a_deterministic_frozen_structured_view(self):
        call = SessionToolCallSnapshot(
            id="call-1",
            name="read_file",
            arguments_json='{"path":"a.py"}',
        )
        success = tool_result_snapshot(ToolResult(status="success", output="ok"))
        attempt_stream = (
            AssistantStreamSnapshot(
                index=0,
                received_at=NOW.isoformat(timespec="microseconds"),
                chunk_json='{"choices":[{"delta":{"content":"partial-secret"}}]}',
            ),
        )
        cases = (
            ("turn/start", TurnStartEventData(turn_id=TURN_ID), "workflow", "lifecycle", "started"),
            ("turn/end", TurnEndEventData(turn_id=TURN_ID, kind="completed"), "workflow", "lifecycle", "completed"),
            ("step/start", StepStartEventData(turn_id=TURN_ID, step=0), "workflow", "lifecycle", "started"),
            ("step/end", StepEndEventData(turn_id=TURN_ID, step=0, kind="interrupted"), "workflow", "lifecycle", "interrupted"),
            (
                "user/message",
                UserMessageEventData(
                    turn_id=TURN_ID,
                    message=SessionUserMessageSnapshot(content="问题"),
                ),
                "user", "conversation", "问题",
            ),
            (
                "assistant/message",
                AssistantMessageEventData(
                    turn_id=TURN_ID,
                    step=0,
                    message=SessionAssistantMessageSnapshot(content="回答"),
                    stream=(),
                ),
                "assistant", "conversation", "回答",
            ),
            (
                "assistant/attempt",
                AssistantAttemptEventData(
                    turn_id=TURN_ID,
                    step=0,
                    stream=attempt_stream,
                    kind="interrupted",
                ),
                "assistant", "model_stream", "模型生成未完成；部分流仅保留用于审计。",
            ),
            (
                "tool/call",
                ToolCallEventData(
                    turn_id=TURN_ID,
                    step=0,
                    call=call,
                    retry_safety="safe",
                ),
                "assistant", "tool", call.arguments_json,
            ),
            (
                "tool/result",
                ToolResultEventData(
                    turn_id=TURN_ID,
                    step=0,
                    call_id=call.id,
                    result=success,
                ),
                "tool", "tool", success.result_json,
            ),
            (
                "recovery/resolution",
                RecoveryResolutionEventData(
                    turn_id=TURN_ID,
                    step=0,
                    call_id=call.id,
                    resolution="confirmed_not_applied",
                    evidence="目标仍为原始状态",
                ),
                "recovery", "recovery", "目标仍为原始状态",
            ),
            (
                "async/task/accepted",
                AsyncTaskAcceptedEventData(
                    task_id=TASK_ID,
                    turn_id=TURN_ID,
                    step=0,
                    call_id=call.id,
                    tool_name=call.name,
                    request_json=call.arguments_json,
                    retry_safety="safe",
                    resource_kind=call.name,
                    cancellable=True,
                ),
                "runtime", "background_task", call.arguments_json,
            ),
            ("async/task/started", AsyncTaskStartedEventData(task_id=TASK_ID), "runtime", "background_task", "started"),
            (
                "async/task/progress",
                AsyncTaskProgressEventData(
                    task_id=TASK_ID,
                    phase="run",
                    completed=1,
                    total=2,
                    message="一半",
                    progress=0.5,
                ),
                "runtime", "background_task", "一半",
            ),
            ("async/task/completed", AsyncTaskCompletedEventData(task_id=TASK_ID, result=success), "runtime", "background_task", success.result_json),
            ("async/task/failed", AsyncTaskFailedEventData(task_id=TASK_ID, error="失败"), "runtime", "background_task", "失败"),
            ("async/task/cancel_requested", AsyncTaskCancelRequestedEventData(task_id=TASK_ID, reason="停止"), "runtime", "background_task", "停止"),
            ("async/task/cancelled", AsyncTaskCancelledEventData(task_id=TASK_ID, reason="已停止"), "runtime", "background_task", "已停止"),
            ("async/task/late_result", AsyncTaskLateResultEventData(task_id=TASK_ID, status="completed", result=success), "runtime", "background_task", "completed"),
            ("host/normal", NormalHostEventData(source="cli", message="通知", details_json='{"key":"value"}'), "cli", "host", "通知"),
            ("host/urgent", UrgentHostEventData(source="user", message="中断", details_json='{"generation":2}'), "user", "urgent", "中断"),
            ("host/control", ControlHostEventData(source="queue", command="flush", message="刷新", details_json='{"count":3}'), "queue", "control", "刷新"),
            ("compaction/summary", CompactionSummaryEventData(summary="旧内容摘要", replace_start_seq=1, replace_end_seq=2), "workflow", "summary", "旧内容摘要"),
        )

        for seq, (event_type, data, source, channel, content) in enumerate(cases, 1):
            with self.subTest(event_type=event_type):
                event = make_event(event_type, data, seq)
                persisted_before = event.model_dump_json()
                first = event.structured_view
                second = event.structured_view
                self.assertEqual(first, second)
                self.assertEqual((first.source, first.channel, first.content), (source, channel, content))
                self.assertIsInstance(json.loads(first.context_json), dict)
                self.assertEqual(event.model_dump_json(), persisted_before)
                self.assertEqual(
                    set(event.model_dump()),
                    {"version", "session_id", "seq", "time", "type", "data"},
                )
                with self.assertRaises(ValidationError):
                    first.content = "tampered"

    def test_workflow_exposes_structured_views_without_persisting_them(self):
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        event = workflow.append("turn/start", TurnStartEventData(turn_id=TURN_ID))
        self.assertEqual(workflow.structured_events, (event.structured_view,))
        self.assertNotIn("structured_view", canonical_json_snapshot(event))


class InterruptedProjectionTests(unittest.TestCase):
    def _append_completed_turn(self, workflow: SessionEventWorkflow) -> None:
        old_turn = "d" * 32
        workflow.append("turn/start", TurnStartEventData(turn_id=old_turn))
        workflow.append(
            "user/message",
            UserMessageEventData(
                turn_id=old_turn,
                message=SessionUserMessageSnapshot(content="旧问题"),
            ),
        )
        workflow.append("step/start", StepStartEventData(turn_id=old_turn, step=0))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=old_turn,
                step=0,
                message=assistant_message_snapshot(AssistantMessage(content="旧回答")),
                stream=(),
            ),
        )
        workflow.append("step/end", StepEndEventData(turn_id=old_turn, step=0, kind="completed"))
        workflow.append("turn/end", TurnEndEventData(turn_id=old_turn, kind="completed"))

    def test_recovery_reprojects_history_and_user_but_not_partial_assistant_output(self):
        with tempfile.TemporaryDirectory(prefix="session-attempt-recovery-") as root:
            directory = Path(root) / "data" / "sessions" / "current"
            workflow = SessionEventWorkflow(
                storage_directory=directory,
                clock=lambda: NOW,
                recover=False,
            )
            self._append_completed_turn(workflow)
            workflow.append("turn/start", TurnStartEventData(turn_id=TURN_ID))
            workflow.append(
                "user/message",
                UserMessageEventData(
                    turn_id=TURN_ID,
                    message=SessionUserMessageSnapshot(content="中断前的原问题"),
                ),
            )
            workflow.append("step/start", StepStartEventData(turn_id=TURN_ID, step=0))
            workflow.append(
                "assistant/attempt",
                AssistantAttemptEventData(
                    turn_id=TURN_ID,
                    step=0,
                    stream=(
                        AssistantStreamSnapshot(
                            index=0,
                            received_at=NOW.isoformat(timespec="microseconds"),
                            chunk_json=canonical_json_snapshot(
                                {"choices": [{"delta": {"content": "partial-secret"}}]}
                            ),
                        ),
                    ),
                    kind="interrupted",
                ),
            )
            workflow.close()

            restored = SessionEventWorkflow(storage_directory=directory, clock=lambda: NOW)
            try:
                projected = restored.project_messages("系统")
                self.assertEqual(
                    [message.role for message in projected],
                    ["system", "user", "assistant", "user", "user"],
                )
                self.assertEqual(projected[1].content, "旧问题")
                self.assertEqual(projected[2].content, "旧回答")
                self.assertEqual(projected[3].content, "中断前的原问题")
                self.assertIn("部分流仅保留用于审计", projected[4].content)
                self.assertNotIn("partial-secret", "\n".join(message.content or "" for message in projected))
                self.assertEqual(restored.recovery_report.interrupted_turns, (TURN_ID,))
                self.assertEqual(restored.events[-3].data.kind, "interrupted")
                self.assertEqual(restored.events[-2].data.kind, "interrupted")
                self.assertEqual(restored.events[-1].type, "host/normal")
                self.assertEqual(restored.events[-1].data.source, "recovery")
                self.assertFalse(any(event.type == "compaction/summary" for event in restored.events))
                attempt = next(event for event in restored.events if event.type == "assistant/attempt")
                self.assertIn("partial-secret", attempt.data.stream[0].chunk_json)
                self.assertFalse(json.loads(attempt.structured_view.context_json)["partial_stream_replayed"])
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
