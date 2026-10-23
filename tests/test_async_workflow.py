from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.schemas import (
    AssistantMessage,
    AssistantMessageEventData,
    AsyncTaskAcceptedEventData,
    AsyncTaskAcceptedOutput,
    AsyncTaskCompletedEventData,
    AsyncTaskCancelRequestedEventData,
    AsyncTaskCancelledEventData,
    AsyncTaskFailedEventData,
    AsyncTaskStartedEventData,
    SessionUserMessageSnapshot,
    StepEndEventData,
    StepStartEventData,
    ToolCall,
    ToolCallEventData,
    ToolResult,
    ToolResultEventData,
    TurnEndEventData,
    TurnStartEventData,
    UserMessageEventData,
)
from app.workflow import (
    SessionEventError,
    SessionEventWorkflow,
    assistant_message_snapshot,
    canonical_json_snapshot,
    tool_call_snapshot,
    tool_result_snapshot,
    tool_retry_safety,
    has_started_without_terminal_async_task,
)


TURN = "1" * 32
TASK = "2" * 32
CALL = ToolCall(id="call_async", name="code_interpreter", arguments={"code": "1+1"})


def append_until_accepted(
    workflow: SessionEventWorkflow,
    *,
    placeholder: bool = True,
    call: ToolCall = CALL,
    task_id: str = TASK,
) -> None:
    workflow.append("turn/start", TurnStartEventData(turn_id=TURN))
    workflow.append(
        "user/message",
        UserMessageEventData(
            turn_id=TURN, message=SessionUserMessageSnapshot(content="run")
        ),
    )
    workflow.append("step/start", StepStartEventData(turn_id=TURN, step=0))
    workflow.append(
        "assistant/message",
        AssistantMessageEventData(
            turn_id=TURN,
            step=0,
            message=assistant_message_snapshot(
                AssistantMessage(content=None, tool_calls=[call])
            ),
            stream=(),
        ),
    )
    workflow.append(
        "tool/call",
        ToolCallEventData(
            turn_id=TURN,
            step=0,
            call=tool_call_snapshot(call),
            retry_safety=tool_retry_safety(call),
        ),
    )
    workflow.append(
        "async/task/accepted",
        AsyncTaskAcceptedEventData(
            task_id=task_id,
            turn_id=TURN,
            step=0,
            call_id=call.id,
            tool_name=call.name,
            request_json=canonical_json_snapshot(call.arguments),
            retry_safety=tool_retry_safety(call),
            resource_kind=call.name,
            cancellable=False,
        ),
    )
    if placeholder:
        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=TURN,
                step=0,
                call_id=call.id,
                result=tool_result_snapshot(ToolResult(
                    status="accepted",
                    output=AsyncTaskAcceptedOutput(task_id=task_id, cancellable=False),
                )),
            ),
        )


class AsyncWorkflowTests(unittest.TestCase):
    def test_session_event_predicate_distinguishes_accepted_started_and_terminal(self):
        workflow = SessionEventWorkflow.in_memory()
        append_until_accepted(workflow)
        self.assertFalse(
            has_started_without_terminal_async_task(workflow.events)
        )
        workflow.append(
            "async/task/started", AsyncTaskStartedEventData(task_id=TASK)
        )
        self.assertTrue(
            has_started_without_terminal_async_task(workflow.events)
        )
        workflow.append(
            "async/task/completed",
            AsyncTaskCompletedEventData(
                task_id=TASK,
                result=tool_result_snapshot(
                    ToolResult(status="success", output="done")
                ),
            ),
        )
        self.assertFalse(
            has_started_without_terminal_async_task(workflow.events)
        )

    def test_async_followup_stays_in_one_turn_with_explicit_waiting_boundary(self):
        workflow = SessionEventWorkflow.in_memory()
        append_until_accepted(workflow)
        workflow.append("async/task/started", AsyncTaskStartedEventData(task_id=TASK))
        workflow.append(
            "step/end", StepEndEventData(turn_id=TURN, step=0, kind="completed")
        )
        workflow.append("step/start", StepStartEventData(turn_id=TURN, step=1))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=TURN,
                step=1,
                message=assistant_message_snapshot(
                    AssistantMessage(content="任务仍在后台运行。")
                ),
                stream=(),
            ),
        )
        workflow.append(
            "step/end",
            StepEndEventData(turn_id=TURN, step=1, kind="awaiting_async"),
        )
        workflow.append(
            "async/task/completed",
            AsyncTaskCompletedEventData(
                task_id=TASK,
                result=tool_result_snapshot(ToolResult(status="success", output="2")),
            ),
        )
        workflow.append("step/start", StepStartEventData(turn_id=TURN, step=2))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=TURN,
                step=2,
                message=assistant_message_snapshot(
                    AssistantMessage(content="后台任务已完成。")
                ),
                stream=(),
            ),
        )
        workflow.append(
            "step/end", StepEndEventData(turn_id=TURN, step=2, kind="completed")
        )
        workflow.append("turn/end", TurnEndEventData(turn_id=TURN, kind="completed"))

        self.assertEqual(
            [event.type for event in workflow.events].count("turn/start"), 1
        )
        self.assertEqual(
            [event.type for event in workflow.events].count("user/message"), 1
        )

    def test_started_requires_placeholder_and_terminal_projects_with_marker(self):
        workflow = SessionEventWorkflow.in_memory()
        append_until_accepted(workflow, placeholder=False)
        with self.assertRaises(SessionEventError):
            workflow.append("async/task/started", AsyncTaskStartedEventData(task_id=TASK))

        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=TURN,
                step=0,
                call_id=CALL.id,
                result=tool_result_snapshot(ToolResult(
                    status="accepted",
                    output=AsyncTaskAcceptedOutput(task_id=TASK, cancellable=False),
                )),
            ),
        )
        workflow.append("async/task/started", AsyncTaskStartedEventData(task_id=TASK))
        completed = workflow.append(
            "async/task/completed",
            AsyncTaskCompletedEventData(
                task_id=TASK,
                result=tool_result_snapshot(ToolResult(status="success", output="2")),
            ),
        )
        workflow.append("step/end", StepEndEventData(turn_id=TURN, step=0, kind="completed"))
        workflow.append("turn/end", TurnEndEventData(turn_id=TURN, kind="interrupted"))
        projected = workflow.project_messages("system")
        self.assertTrue(any(
            message.role == "user" and completed.display_marker in message.content
            for message in projected
        ))
        self.assertEqual(workflow.active_async_task_ids(), ())

    def test_restart_closes_started_task_as_outcome_unknown(self):
        with tempfile.TemporaryDirectory(prefix="async-session-") as directory:
            path = Path(directory)
            workflow = SessionEventWorkflow(storage_directory=path, recover=False)
            append_until_accepted(workflow)
            workflow.append("async/task/started", AsyncTaskStartedEventData(task_id=TASK))
            workflow.close()

            restored = SessionEventWorkflow(storage_directory=path)
            failures = [
                event.data for event in restored.events
                if isinstance(event.data, AsyncTaskFailedEventData)
            ]
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0].recovery_status, "outcome_unknown")
            self.assertEqual(restored.active_async_task_ids(), ())
            restored.close()

    def test_compaction_cannot_cut_active_task_lifecycle(self):
        workflow = SessionEventWorkflow.in_memory()
        append_until_accepted(workflow)
        workflow.append("step/end", StepEndEventData(turn_id=TURN, step=0, kind="completed"))
        end = workflow.append("turn/end", TurnEndEventData(turn_id=TURN, kind="interrupted"))
        with self.assertRaises(SessionEventError):
            workflow.append_compaction_summary(
                summary="must not hide active task",
                replace_start_seq=1,
                replace_end_seq=end.seq,
            )

    def test_restart_finishes_durable_cancel_request_as_cancelled(self):
        with tempfile.TemporaryDirectory(prefix="async-cancel-session-") as directory:
            path = Path(directory)
            workflow = SessionEventWorkflow(storage_directory=path, recover=False)
            append_until_accepted(workflow)
            workflow.append(
                "async/task/cancel_requested",
                AsyncTaskCancelRequestedEventData(task_id=TASK, reason="stop"),
            )
            workflow.close()
            restored = SessionEventWorkflow(storage_directory=path)
            self.assertTrue(any(
                isinstance(event.data, AsyncTaskCancelledEventData)
                for event in restored.events
            ))
            self.assertEqual(restored.active_async_task_ids(), ())
            restored.close()

    def test_unknown_async_unittest_enters_external_recovery_gate(self):
        unsafe_call = ToolCall(
            id="call_unittest",
            name="bash_shell",
            arguments={"command": "unittest", "paths": ["tests"]},
        )
        unsafe_task = "3" * 32
        with tempfile.TemporaryDirectory(prefix="async-unsafe-session-") as directory:
            path = Path(directory)
            workflow = SessionEventWorkflow(storage_directory=path, recover=False)
            append_until_accepted(workflow, call=unsafe_call, task_id=unsafe_task)
            workflow.append(
                "async/task/started", AsyncTaskStartedEventData(task_id=unsafe_task)
            )
            workflow.close()
            restored = SessionEventWorkflow(storage_directory=path)
            pending = restored.unresolved_external_calls()
            self.assertEqual([item.call.id for item in pending], [unsafe_call.id])
            self.assertTrue(restored.requires_external_resolution(ToolCall(
                id="write", name="write_file",
                arguments={"path": "x.txt", "content": "x", "mode": "create"},
            )))
            restored.resolve_tool_outcome(
                unsafe_call.id,
                turn_id=TURN,
                step=0,
                applied=False,
                evidence="已检查工作区和外部状态。",
            )
            self.assertEqual(restored.unresolved_external_calls(), ())
            restored.close()


if __name__ == "__main__":
    unittest.main()
