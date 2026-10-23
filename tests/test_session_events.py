"""SessionEvent 持久化、投影、压缩和崩溃恢复的离线测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.schemas import (
    AssistantMessage,
    AssistantMessageEventData,
    AssistantStreamSnapshot,
    AsyncTaskAcceptedEventData,
    AsyncTaskAcceptedOutput,
    AsyncTaskCompletedEventData,
    AsyncTaskStartedEventData,
    ControlHostEventData,
    NormalHostEventData,
    SessionEvent,
    SessionAssistantMessageSnapshot,
    SessionToolCallSnapshot,
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
    UrgentHostEventData,
)
from app.workflow import (
    SessionEventError,
    SessionEventWorkflow,
    assistant_message_snapshot,
    canonical_json_snapshot,
    tool_call_snapshot,
    tool_result_snapshot,
    tool_retry_safety,
)


NOW = datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)


class SessionEventWorkflowTests(unittest.TestCase):
    def directory(self, root: str) -> Path:
        return Path(root) / "data" / "sessions" / "current"

    def append_completed_turn(
        self, workflow: SessionEventWorkflow, prompt: str, answer: str
    ) -> tuple[int, int]:
        turn_id = ("a" if not workflow.events else "b") * 32
        start = workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
        workflow.append(
            "user/message",
            UserMessageEventData(
                turn_id=turn_id,
                message=SessionUserMessageSnapshot(content=prompt),
            ),
        )
        workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
        message = AssistantMessage(content=answer)
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=turn_id,
                step=0,
                message=assistant_message_snapshot(message),
                stream=(
                    AssistantStreamSnapshot(
                        index=0,
                        received_at=NOW.isoformat(timespec="microseconds"),
                        chunk_json=canonical_json_snapshot(
                            {"choices": [{"delta": {"content": answer}}]}
                        ),
                    ),
                ),
            ),
        )
        workflow.append(
            "step/end", StepEndEventData(turn_id=turn_id, step=0, kind="completed")
        )
        end = workflow.append(
            "turn/end", TurnEndEventData(turn_id=turn_id, kind="completed")
        )
        return start.seq, end.seq

    def append_open_tool_group(
        self,
        workflow: SessionEventWorkflow,
        *,
        turn_id: str,
        calls: tuple[ToolCall, ...],
    ) -> None:
        workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
        workflow.append(
            "user/message",
            UserMessageEventData(
                turn_id=turn_id,
                message=SessionUserMessageSnapshot(content="执行工具"),
            ),
        )
        workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=turn_id,
                step=0,
                message=assistant_message_snapshot(
                    AssistantMessage(tool_calls=list(calls))
                ),
                stream=(),
            ),
        )
        for call in calls:
            workflow.append(
                "tool/call",
                ToolCallEventData(
                    turn_id=turn_id,
                    step=0,
                    call=tool_call_snapshot(call),
                    retry_safety=tool_retry_safety(call),
                ),
            )

    def append_unchecked_event(
        self,
        workflow: SessionEventWorkflow,
        event_type: str,
        data: object,
    ) -> None:
        """Inject one envelope to test projection's independent fail-closed checks."""
        workflow._events.append(SessionEvent(  # type: ignore[attr-defined]
            session_id=workflow.session_id,
            seq=len(workflow.events) + 1,
            time=NOW.isoformat(timespec="microseconds"),
            type=event_type,
            data=data,
        ))

    def test_events_are_contiguous_persistent_and_deeply_frozen(self):
        with tempfile.TemporaryDirectory(prefix="session-event-tests-") as root:
            directory = self.directory(root)
            workflow = SessionEventWorkflow(storage_directory=directory, clock=lambda: NOW)
            first, last = self.append_completed_turn(workflow, "问题", "回答")
            session_id = workflow.session_id
            self.assertEqual((first, last), (1, 6))
            event = workflow.events[3]
            with self.assertRaises(ValidationError):
                event.seq = 99
            with self.assertRaises(ValidationError):
                event.data.message.content = "篡改"
            workflow.close()

            restored = SessionEventWorkflow(storage_directory=directory, clock=lambda: NOW)
            try:
                self.assertEqual(restored.session_id, session_id)
                self.assertEqual([event.seq for event in restored.events], list(range(1, 7)))
                projected = restored.project_messages("系统")
                self.assertEqual([message.role for message in projected], ["system", "user", "assistant"])
                self.assertEqual(projected[-1].content, "回答")
                self.assertEqual(restored.events[3].data.stream[0].index, 0)
            finally:
                restored.close()

    def test_lossless_snapshot_rejects_non_json_keys_values_and_numbers(self):
        self.assertEqual(canonical_json_snapshot({"乙": [2], "甲": 1}), '{"乙":[2],"甲":1}')
        for value in ({1: "changed key"}, {"bad": object()}, {"bad": float("nan")}):
            with self.subTest(value=type(next(iter(value.values()))).__name__):
                with self.assertRaises(SessionEventError):
                    canonical_json_snapshot(value)

    def test_corrupt_truncated_gap_and_hard_linked_segments_fail_closed(self):
        mutators = ("truncated", "gap", "hard_link")
        for mutator in mutators:
            with self.subTest(mutator=mutator), tempfile.TemporaryDirectory(
                prefix="session-corruption-tests-"
            ) as root:
                directory = self.directory(root)
                workflow = SessionEventWorkflow(storage_directory=directory, clock=lambda: NOW)
                workflow.append("turn/start", TurnStartEventData(turn_id="a" * 32))
                workflow.close()
                first = directory / "0000000000000001.json"
                if mutator == "truncated":
                    first.write_bytes(first.read_bytes()[:10])
                elif mutator == "gap":
                    first.rename(directory / "0000000000000002.json")
                else:
                    try:
                        os.link(first, directory / "linked-copy.json")
                    except OSError as exc:
                        self.skipTest(f"当前文件系统不支持硬链接测试：{type(exc).__name__}")
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=directory, clock=lambda: NOW)

    def test_failed_publish_does_not_assign_or_expose_sequence(self):
        with tempfile.TemporaryDirectory(prefix="session-publish-tests-") as root:
            workflow = SessionEventWorkflow(
                storage_directory=self.directory(root), clock=lambda: NOW
            )
            with patch.object(workflow, "_atomic_publish", side_effect=OSError("SECRET")):
                with self.assertRaisesRegex(SessionEventError, "可靠提交") as caught:
                    workflow.append("turn/start", TurnStartEventData(turn_id="a" * 32))
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertEqual(workflow.events, ())
            workflow.close()

    def test_crash_recovery_records_not_started_unknown_and_boundaries(self):
        with tempfile.TemporaryDirectory(prefix="session-recovery-tests-") as root:
            directory = self.directory(root)
            workflow = SessionEventWorkflow(
                storage_directory=directory, clock=lambda: NOW, recover=False
            )
            turn_id = "c" * 32
            read_call = ToolCall(id="read", name="read_file", arguments={"path": "a.py"})
            write_call = ToolCall(
                id="write", name="write_file", arguments={"path": "a.py", "content": "x", "mode": "overwrite"}
            )
            not_started = ToolCall(id="later", name="grep", arguments={"pattern": "x"})
            workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
            workflow.append(
                "user/message",
                UserMessageEventData(
                    turn_id=turn_id,
                    message=SessionUserMessageSnapshot(content="执行工具"),
                ),
            )
            workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
            assistant = AssistantMessage(tool_calls=[read_call, write_call, not_started])
            workflow.append(
                "assistant/message",
                AssistantMessageEventData(
                    turn_id=turn_id,
                    step=0,
                    message=assistant_message_snapshot(assistant),
                    stream=(),
                ),
            )
            for call in (read_call, write_call):
                workflow.append(
                    "tool/call",
                    ToolCallEventData(
                        turn_id=turn_id,
                        step=0,
                        call=tool_call_snapshot(call),
                        retry_safety=tool_retry_safety(call),
                    ),
                )
            workflow.close()

            restored = SessionEventWorkflow(storage_directory=directory, clock=lambda: NOW)
            try:
                states = {item.call_id: item for item in restored.recovery_report.repaired}
                self.assertEqual(states["read"].status, "outcome_unknown")
                self.assertEqual(states["read"].retry_safety, "safe")
                self.assertEqual(states["write"].retry_safety, "inspect_external")
                self.assertEqual(states["later"].status, "not_started")
                self.assertEqual(restored.recovery_report.interrupted_turns, (turn_id,))
                self.assertFalse(restored.requires_external_resolution(read_call))
                self.assertTrue(restored.requires_external_resolution(write_call))
                pending_projection = restored.project_messages("系统")
                pending_notice = pending_projection[-1].content
                self.assertIn(f"turn_id={turn_id}", pending_notice)
                self.assertIn("step=0", pending_notice)
                self.assertIn("call_id=write", pending_notice)
                self.assertIn("inspect_recovery", pending_notice)
                restored.resolve_tool_outcome("write", applied=False, evidence="宿主已确认目标仍为原始状态")
                self.assertFalse(restored.requires_external_resolution(write_call))
                projected = restored.project_messages("系统")
                self.assertEqual(
                    [message.role for message in projected],
                    ["system", "user", "assistant", "tool", "tool", "tool", "user"],
                )
            finally:
                restored.close()

    def test_retry_classification_is_conservative(self):
        safe = (
            ToolCall(id="1", name="read_file", arguments={"path": "a"}),
            ToolCall(id="2", name="code_interpreter", arguments={"code": "1+1"}),
            ToolCall(id="3", name="bash_shell", arguments={"command": "compileall", "paths": ["app"]}),
            ToolCall(id="4", name="bash_shell", arguments={"command": "validate_json", "paths": ["x.json"]}),
        )
        unsafe = (
            ToolCall(id="5", name="bash_shell", arguments={"command": "unittest", "paths": ["tests"]}),
            ToolCall(id="6", name="bash_shell", arguments={"command": "clear_file", "paths": ["a"]}),
            ToolCall(id="7", name="editor_file", arguments={"path": "a"}),
        )
        self.assertTrue(all(tool_retry_safety(call) == "safe" for call in safe))
        self.assertTrue(all(tool_retry_safety(call) == "inspect_external" for call in unsafe))

    def test_projection_keeps_all_tool_results_adjacent_before_interleaved_host_event(self):
        """Regression for the persisted assistant + urgent + ten results shape."""
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        turn_id = "d" * 32
        calls = tuple(
            ToolCall(
                id=f"call_{index}",
                name="read_file",
                arguments={"path": f"file-{index}.txt"},
            )
            for index in range(10)
        )
        workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
        workflow.append(
            "user/message",
            UserMessageEventData(
                turn_id=turn_id,
                message=SessionUserMessageSnapshot(content="同时读取十个文件"),
            ),
        )
        workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=turn_id,
                step=0,
                message=assistant_message_snapshot(AssistantMessage(tool_calls=list(calls))),
                stream=(),
            ),
        )
        for call in calls:
            workflow.append(
                "tool/call",
                ToolCallEventData(
                    turn_id=turn_id,
                    step=0,
                    call=tool_call_snapshot(call),
                    retry_safety=tool_retry_safety(call),
                ),
            )
        urgent = workflow.append(
            "host/urgent",
            UrgentHostEventData(
                source="user",
                message="停止旧回答并重新评估",
                details_json=canonical_json_snapshot({"reason": "interrupt"}),
            ),
        )
        for index, call in enumerate(calls):
            workflow.append(
                "tool/result",
                ToolResultEventData(
                    turn_id=turn_id,
                    step=0,
                    call_id=call.id,
                    result=tool_result_snapshot(
                        ToolResult(status="success", output=f"result-{index}")
                    ),
                ),
            )
        workflow.append(
            "step/end", StepEndEventData(turn_id=turn_id, step=0, kind="interrupted")
        )
        workflow.append(
            "turn/end", TurnEndEventData(turn_id=turn_id, kind="interrupted")
        )

        projected = workflow.project_messages("系统")
        self.assertEqual(
            [message.role for message in projected],
            ["system", "user", "assistant", *("tool" for _ in calls), "user"],
        )
        assistant_index = next(
            index for index, message in enumerate(projected) if message.role == "assistant"
        )
        projected_results = projected[assistant_index + 1 : assistant_index + 1 + len(calls)]
        self.assertEqual(
            [message.tool_call_id for message in projected_results],
            [call.id for call in calls],
        )
        self.assertIn(urgent.display_marker, projected[-1].content)
        # The append-only source remains in its original linearized order.
        self.assertLess(
            next(event.seq for event in workflow.events if event.type == "host/urgent"),
            next(event.seq for event in workflow.events if event.type == "tool/result"),
        )

    def test_projection_rejects_missing_tool_result_instead_of_crossing_tail(self):
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        turn_id = "e" * 32
        call = ToolCall(id="missing", name="read_file", arguments={"path": "a.txt"})
        workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
        workflow.append(
            "user/message",
            UserMessageEventData(
                turn_id=turn_id,
                message=SessionUserMessageSnapshot(content="读取"),
            ),
        )
        workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=turn_id,
                step=0,
                message=assistant_message_snapshot(AssistantMessage(tool_calls=[call])),
                stream=(),
            ),
        )
        workflow.append(
            "tool/call",
            ToolCallEventData(
                turn_id=turn_id,
                step=0,
                call=tool_call_snapshot(call),
                retry_safety=tool_retry_safety(call),
            ),
        )
        workflow.append(
            "host/urgent",
            UrgentHostEventData(
                source="user", message="中断", details_json="{}"
            ),
        )
        with self.assertRaisesRegex(SessionEventError, "未闭合"):
            workflow.project_messages("系统")

    def test_live_projection_hides_only_open_tool_tail_until_group_is_complete(self):
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        turn_id = "f" * 32
        calls = (
            ToolCall(id="first", name="read_file", arguments={"path": "a.txt"}),
            ToolCall(id="second", name="read_file", arguments={"path": "b.txt"}),
        )
        self.append_open_tool_group(workflow, turn_id=turn_id, calls=calls)

        prefix = workflow.project_messages(
            "系统", allow_incomplete_tool_tail=True
        )
        self.assertEqual([message.role for message in prefix], ["system", "user"])
        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=turn_id,
                step=0,
                call_id="first",
                result=tool_result_snapshot(ToolResult(status="success", output="A")),
            ),
        )
        deferred = workflow.append(
            "host/normal",
            NormalHostEventData(source="runtime", message="稍后显示"),
        )
        still_prefix = workflow.project_messages(
            "系统", allow_incomplete_tool_tail=True
        )
        self.assertEqual(still_prefix, prefix)

        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=turn_id,
                step=0,
                call_id="second",
                result=tool_result_snapshot(ToolResult(status="success", output="B")),
            ),
        )
        complete = workflow.project_messages(
            "系统", allow_incomplete_tool_tail=True
        )
        self.assertEqual(
            [message.role for message in complete],
            ["system", "user", "assistant", "tool", "tool", "user"],
        )
        self.assertIn(deferred.display_marker, complete[-1].content)

    def test_projection_defers_host_control_and_async_terminal_in_source_order(self):
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        background_turn = "1" * 32
        background_task = "2" * 32
        background_call = ToolCall(
            id="background",
            name="code_interpreter",
            arguments={"code": "1+1"},
        )
        self.append_open_tool_group(
            workflow,
            turn_id=background_turn,
            calls=(background_call,),
        )
        workflow.append(
            "async/task/accepted",
            AsyncTaskAcceptedEventData(
                task_id=background_task,
                turn_id=background_turn,
                step=0,
                call_id=background_call.id,
                tool_name=background_call.name,
                request_json=canonical_json_snapshot(background_call.arguments),
                retry_safety=tool_retry_safety(background_call),
                resource_kind=background_call.name,
                cancellable=False,
            ),
        )
        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=background_turn,
                step=0,
                call_id=background_call.id,
                result=tool_result_snapshot(ToolResult(
                    status="accepted",
                    output=AsyncTaskAcceptedOutput(
                        task_id=background_task,
                        cancellable=False,
                    ),
                )),
            ),
        )
        workflow.append(
            "async/task/started", AsyncTaskStartedEventData(task_id=background_task)
        )
        workflow.append(
            "step/end",
            StepEndEventData(turn_id=background_turn, step=0, kind="interrupted"),
        )
        workflow.append(
            "turn/end",
            TurnEndEventData(turn_id=background_turn, kind="interrupted"),
        )

        turn_id = "3" * 32
        calls = (
            ToolCall(id="one", name="read_file", arguments={"path": "one"}),
            ToolCall(id="two", name="read_file", arguments={"path": "two"}),
        )
        self.append_open_tool_group(workflow, turn_id=turn_id, calls=calls)
        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=turn_id,
                step=0,
                call_id="one",
                result=tool_result_snapshot(ToolResult(status="success", output="1")),
            ),
        )
        delayed = (
            workflow.append(
                "host/normal",
                NormalHostEventData(source="runtime", message="普通事件"),
            ),
            workflow.append(
                "host/urgent",
                UrgentHostEventData(source="user", message="紧急事件"),
            ),
            workflow.append(
                "host/control",
                ControlHostEventData(
                    source="runtime",
                    command="cancel_generation",
                    message="控制事件",
                ),
            ),
            workflow.append(
                "async/task/completed",
                AsyncTaskCompletedEventData(
                    task_id=background_task,
                    result=tool_result_snapshot(
                        ToolResult(status="success", output="2")
                    ),
                ),
            ),
        )
        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=turn_id,
                step=0,
                call_id="two",
                result=tool_result_snapshot(ToolResult(status="success", output="2")),
            ),
        )
        workflow.append(
            "step/end", StepEndEventData(turn_id=turn_id, step=0, kind="interrupted")
        )
        workflow.append(
            "turn/end", TurnEndEventData(turn_id=turn_id, kind="interrupted")
        )

        projected = workflow.project_messages("系统")
        assistant_index = max(
            index for index, message in enumerate(projected)
            if message.role == "assistant"
        )
        self.assertEqual(
            [message.role for message in projected[assistant_index:]],
            ["assistant", "tool", "tool", "user", "user", "user", "user"],
        )
        self.assertEqual(
            [
                next(
                    event.type for event in delayed
                    if event.display_marker in message.content
                )
                for message in projected[-4:]
            ],
            [event.type for event in delayed],
        )

    def test_projection_rejects_unsafe_boundaries_and_bad_results_even_live(self):
        calls = (
            ToolCall(id="one", name="read_file", arguments={"path": "one"}),
            ToolCall(id="two", name="read_file", arguments={"path": "two"}),
        )
        cases = (
            (
                "user",
                "新用户消息",
                "user/message",
                UserMessageEventData(
                    turn_id="4" * 32,
                    message=SessionUserMessageSnapshot(content="越界用户消息"),
                ),
            ),
            (
                "assistant",
                "新模型消息",
                "assistant/message",
                AssistantMessageEventData(
                    turn_id="4" * 32,
                    step=0,
                    message=assistant_message_snapshot(AssistantMessage(content="越界回答")),
                    stream=(),
                ),
            ),
            (
                "step_end",
                "不可安全重排",
                "step/end",
                StepEndEventData(turn_id="4" * 32, step=0, kind="interrupted"),
            ),
            (
                "mismatched_result",
                "不匹配",
                "tool/result",
                ToolResultEventData(
                    turn_id="4" * 32,
                    step=0,
                    call_id="other",
                    result=tool_result_snapshot(
                        ToolResult(status="success", output="bad")
                    ),
                ),
            ),
        )
        for name, error, event_type, data in cases:
            with self.subTest(name=name):
                workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
                self.append_open_tool_group(
                    workflow, turn_id="4" * 32, calls=calls
                )
                self.append_unchecked_event(workflow, event_type, data)
                with self.assertRaisesRegex(SessionEventError, error):
                    workflow.project_messages(
                        "系统", allow_incomplete_tool_tail=True
                    )

        duplicate = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        self.append_open_tool_group(duplicate, turn_id="5" * 32, calls=calls)
        first_result = ToolResultEventData(
            turn_id="5" * 32,
            step=0,
            call_id="one",
            result=tool_result_snapshot(ToolResult(status="success", output="1")),
        )
        duplicate.append("tool/result", first_result)
        self.append_unchecked_event(duplicate, "tool/result", first_result)
        with self.assertRaisesRegex(SessionEventError, "重复"):
            duplicate.project_messages("系统", allow_incomplete_tool_tail=True)

    def test_projection_without_tool_group_preserves_ordinary_message_order(self):
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        turn_id = "6" * 32
        workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
        workflow.append(
            "user/message",
            UserMessageEventData(
                turn_id=turn_id,
                message=SessionUserMessageSnapshot(content="问题"),
            ),
        )
        normal = workflow.append(
            "host/normal",
            NormalHostEventData(source="runtime", message="普通宿主事件"),
        )
        workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
        workflow.append(
            "assistant/message",
            AssistantMessageEventData(
                turn_id=turn_id,
                step=0,
                message=assistant_message_snapshot(AssistantMessage(content="回答")),
                stream=(),
            ),
        )
        workflow.append(
            "step/end", StepEndEventData(turn_id=turn_id, step=0, kind="completed")
        )
        workflow.append(
            "turn/end", TurnEndEventData(turn_id=turn_id, kind="completed")
        )
        control = workflow.append(
            "host/control",
            ControlHostEventData(
                source="runtime", command="idle", message="轮外控制事件"
            ),
        )
        projected = workflow.project_messages("系统")
        self.assertEqual(
            [message.role for message in projected],
            ["system", "user", "user", "assistant", "user"],
        )
        self.assertIn(normal.display_marker, projected[2].content)
        self.assertIn(control.display_marker, projected[4].content)

    def test_compaction_appends_summary_and_replaces_only_projection(self):
        workflow = SessionEventWorkflow.in_memory(clock=lambda: NOW)
        first_start, first_end = self.append_completed_turn(workflow, "旧问题", "旧回答")
        self.append_completed_turn(workflow, "新问题", "新回答")
        original_count = len(workflow.events)
        summary_event = workflow.append_compaction_summary(
            summary="旧轮已完成。",
            replace_start_seq=first_start,
            replace_end_seq=first_end,
        )
        self.assertEqual(summary_event.seq, original_count + 1)
        self.assertEqual(len(workflow.events), original_count + 1)
        projected = workflow.project_messages("系统")
        self.assertEqual(
            [getattr(message, "content", None) for message in projected],
            ["系统", "此前会话摘要：\n旧轮已完成。", "新问题", "新回答"],
        )
        with self.assertRaises(SessionEventError):
            workflow.append_compaction_summary(
                summary="非法部分轮", replace_start_seq=first_start + 1, replace_end_seq=first_end
            )


if __name__ == "__main__":
    unittest.main()
