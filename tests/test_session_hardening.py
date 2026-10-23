"""持久化故障、状态关联及恢复保护的回归测试。"""

from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.schemas import (
    AssistantMessage, AssistantMessageEventData, SessionUserMessageSnapshot,
    RecoveryResolutionEventData, ResolveRecoveryOutput,
    SessionToolCallSnapshot, SessionToolResultSnapshot, StepStartEventData, StepEndEventData,
    ToolCall, ToolCallEventData, ToolResult, ToolResultEventData,
    TurnStartEventData, TurnEndEventData, UserMessageEventData,
)
from app.workflow import (
    SessionEventError, SessionEventWorkflow, assistant_message_snapshot,
    canonical_json_snapshot, tool_call_snapshot, tool_result_snapshot, tool_retry_safety,
)


def begin(workflow, calls=(), content=None):
    turn = uuid4().hex
    start = workflow.append("turn/start", TurnStartEventData(turn_id=turn)).seq
    workflow.append("user/message", UserMessageEventData(
        turn_id=turn, message=SessionUserMessageSnapshot(content="任务")))
    workflow.append("step/start", StepStartEventData(turn_id=turn, step=0))
    workflow.append("assistant/message", AssistantMessageEventData(
        turn_id=turn, step=0, stream=(), message=assistant_message_snapshot(
            AssistantMessage(content=content, tool_calls=list(calls)))))
    return turn, start


def dispatch(workflow, turn, call):
    workflow.append("tool/call", ToolCallEventData(
        turn_id=turn, step=0, call=tool_call_snapshot(call), retry_safety=tool_retry_safety(call)))


def completed(workflow):
    turn, start = begin(workflow, content="回答")
    workflow.append("step/end", StepEndEventData(turn_id=turn, step=0, kind="completed"))
    end = workflow.append("turn/end", TurnEndEventData(turn_id=turn, kind="completed")).seq
    return start, end


class SessionHardeningTests(unittest.TestCase):
    def test_single_writer_lock_released_on_close(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            first = SessionEventWorkflow(storage_directory=path)
            try:
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=path)
                first.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
            finally:
                first.close()
            with SessionEventWorkflow(storage_directory=path) as restored:
                self.assertEqual(restored.events[-1].type, "turn/end")

    def test_publish_then_error_poisoned_until_replay(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            workflow = SessionEventWorkflow(storage_directory=path)
            original = workflow._atomic_publish

            def uncertain(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError("SECRET after publication")

            with patch.object(workflow, "_atomic_publish", side_effect=uncertain):
                with self.assertRaises(SessionEventError):
                    workflow.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
            self.assertFalse(workflow.events)
            with self.assertRaises(SessionEventError):
                workflow.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
            workflow.close()
            with SessionEventWorkflow(storage_directory=path) as restored:
                self.assertEqual([event.seq for event in restored.events], [1, 2])
                self.assertEqual(restored.events[-1].data.kind, "interrupted")

    def test_no_replace_publication_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / "source", Path(root) / "target"
            source.write_bytes(b"new")
            target.write_bytes(b"keep")
            with self.assertRaises(OSError):
                SessionEventWorkflow._publish_no_replace(source, target)
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertEqual(source.read_bytes(), b"new")

    def test_symlink_storage_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as root:
            real, linked = Path(root) / "real", Path(root) / "linked"
            real.mkdir()
            try:
                linked.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("当前主机不支持创建符号链接")
            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(storage_directory=linked / "session")
            self.assertEqual(list(real.iterdir()), [])

    def test_live_directory_replacement_is_blocked_or_detected(self):
        with tempfile.TemporaryDirectory() as root:
            path, moved = Path(root) / "session", Path(root) / "moved"
            with SessionEventWorkflow(storage_directory=path) as workflow:
                try:
                    path.rename(moved)
                except PermissionError:
                    self.assertEqual(os.name, "nt")
                else:
                    path.mkdir()
                    with self.assertRaises(SessionEventError):
                        workflow.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
                    self.assertFalse(list(path.iterdir()))

    def test_json_snapshot_rejects_lossy_values_and_cycles(self):
        class Custom(BaseModel):
            value: object

        class DictSubclass(dict):
            pass

        recursive = []
        recursive.append(recursive)
        for value in [(), b"text", Decimal("1.2"), datetime.now(timezone.utc),
                      {1: "key"}, DictSubclass(key="value"), recursive,
                      Custom(value=Decimal("1.2")), "\ud800"]:
            with self.subTest(kind=type(value).__name__), self.assertRaises(SessionEventError):
                canonical_json_snapshot(value)

    def test_snapshot_schemas_reject_non_object_arguments_and_bad_results(self):
        with self.assertRaises(ValidationError):
            SessionToolCallSnapshot(id="x", name="read_file", arguments_json="[]")
        with self.assertRaises(ValidationError):
            SessionToolResultSnapshot(status="success", result_json=canonical_json_snapshot(
                ToolResult(status="error", error="失败")))
        with self.assertRaises(ValidationError):
            SessionToolResultSnapshot(status="success", result_json='{"status":"success","unknown":1}')

    def test_completed_boundaries_require_an_answer(self):
        workflow = SessionEventWorkflow.in_memory()
        turn = uuid4().hex
        workflow.append("turn/start", TurnStartEventData(turn_id=turn))
        workflow.append("user/message", UserMessageEventData(
            turn_id=turn, message=SessionUserMessageSnapshot(content="任务")))
        workflow.append("step/start", StepStartEventData(turn_id=turn, step=0))
        with self.assertRaises(SessionEventError):
            workflow.append("step/end", StepEndEventData(turn_id=turn, step=0, kind="completed"))
        workflow.recover_incomplete()
        with self.assertRaises(SessionEventError):
            workflow.append("turn/start", TurnStartEventData(turn_id=turn))

    def test_reused_call_id_recovers_the_correct_turn_and_blocks_all_mutations(self):
        workflow = SessionEventWorkflow.in_memory()
        call = ToolCall(id="same", name="write_file", arguments={"path": "a", "content": "x", "mode": "create"})
        first, _ = begin(workflow, [call])
        dispatch(workflow, first, call)
        workflow.recover_incomplete()
        workflow.resolve_tool_outcome(call.id, turn_id=first, step=0, applied=False, evidence="检查一")
        second, _ = begin(workflow, [call])
        dispatch(workflow, second, call)
        repaired = workflow.recover_incomplete()
        self.assertEqual(repaired.repaired[0].turn_id, second)
        changed = ToolCall(id="different", name="bash_shell", arguments={"command": "clear_file", "paths": ["b"]})
        self.assertTrue(workflow.requires_external_resolution(changed))
        self.assertFalse(workflow.requires_external_resolution(ToolCall(id="read", name="read_file", arguments={"path": "a"})))
        self.assertEqual(workflow.unresolved_external_calls()[0].turn_id, second)
        workflow.resolve_tool_outcome(call.id, turn_id=second, step=0, applied=True, evidence="检查二")
        self.assertFalse(workflow.unresolved_external_calls())
        self.assertIn("检查二", workflow.project_messages("系统")[-1].content)
        before = len(workflow.events)
        workflow.recover_incomplete()
        self.assertEqual(len(workflow.events), before)

    def test_low_level_recovery_resolution_still_requires_boolean_and_evidence(self):
        workflow = SessionEventWorkflow.in_memory()
        call = ToolCall(id="x", name="editor_file", arguments={"path": "a"})
        turn, _ = begin(workflow, [call])
        dispatch(workflow, turn, call)
        workflow.recover_incomplete()
        with self.assertRaises(SessionEventError):
            workflow.resolve_tool_outcome("x", applied=True, evidence=" ")
        with self.assertRaises(SessionEventError):
            workflow.resolve_tool_outcome("x", applied="yes", evidence="记录")

    def test_in_turn_resolution_requires_matching_open_resolver_and_projects_after_tool_result(self):
        workflow = SessionEventWorkflow.in_memory()
        old_call = ToolCall(
            id="old-write", name="write_file",
            arguments={"path": "a", "content": "x", "mode": "create"},
        )
        old_turn, _ = begin(workflow, [old_call])
        dispatch(workflow, old_turn, old_call)
        workflow.recover_incomplete()

        resolver = ToolCall(
            id="resolver", name="resolve_recovery",
            arguments={
                "turn_id": old_turn,
                "step": 0,
                "call_id": old_call.id,
                "inspection_token": "a" * 64,
                "resolution": "not_applied",
                "evidence": "目标仍为原状。",
            },
        )
        current_turn, _ = begin(workflow, [resolver])
        dispatch(workflow, current_turn, resolver)
        workflow.append(
            "recovery/resolution",
            RecoveryResolutionEventData(
                turn_id=old_turn,
                step=0,
                call_id=old_call.id,
                resolution="confirmed_not_applied",
                evidence="Agent只读核验后确认未应用。",
            ),
        )
        # resolver 的 tool/result 落盘前，实时投影仍只公布完整 SDK 前缀。
        incomplete = workflow.project_messages("系统", allow_incomplete_tool_tail=True)
        self.assertNotEqual(getattr(incomplete[-1], "tool_call_id", None), "resolver")
        workflow.append(
            "tool/result",
            ToolResultEventData(
                turn_id=current_turn,
                step=0,
                call_id=resolver.id,
                result=tool_result_snapshot(ToolResult(
                    status="success",
                    output=ResolveRecoveryOutput(
                        turn_id=old_turn,
                        step=0,
                        call_id=old_call.id,
                        resolution="confirmed_not_applied",
                    ),
                )),
            ),
        )
        projected = workflow.project_messages("系统", allow_incomplete_tool_tail=True)
        self.assertEqual(projected[-2].tool_call_id, "resolver")
        self.assertIn("Agent 外部状态核验", projected[-1].content)
        self.assertFalse(workflow.unresolved_external_calls())

    def test_in_turn_resolution_rejects_missing_or_mismatched_resolver_call(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch):
                workflow = SessionEventWorkflow.in_memory()
                old_call = ToolCall(
                    id="old-write", name="write_file",
                    arguments={"path": "a", "content": "x", "mode": "create"},
                )
                old_turn, _ = begin(workflow, [old_call])
                dispatch(workflow, old_turn, old_call)
                workflow.recover_incomplete()
                resolver = ToolCall(
                    id="resolver",
                    name="resolve_recovery" if mismatch else "inspect_recovery",
                    arguments=(
                        {
                            "turn_id": "f" * 32,
                            "step": 0,
                            "call_id": old_call.id,
                            "inspection_token": "a" * 64,
                            "resolution": "not_applied",
                            "evidence": "错误目标。",
                        }
                        if mismatch
                        else {"turn_id": old_turn, "step": 0, "call_id": old_call.id}
                    ),
                )
                current_turn, _ = begin(workflow, [resolver])
                dispatch(workflow, current_turn, resolver)
                with self.assertRaises(SessionEventError):
                    workflow.append(
                        "recovery/resolution",
                        RecoveryResolutionEventData(
                            turn_id=old_turn,
                            step=0,
                            call_id=old_call.id,
                            resolution="confirmed_not_applied",
                            evidence="不应接受。",
                        ),
                    )

    def test_restart_after_resolution_before_resolver_result_keeps_old_call_resolved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            workflow = SessionEventWorkflow(storage_directory=path)
            old_call = ToolCall(
                id="old-write", name="write_file",
                arguments={"path": "a", "content": "x", "mode": "create"},
            )
            old_turn, _ = begin(workflow, [old_call])
            dispatch(workflow, old_turn, old_call)
            workflow.recover_incomplete()
            resolver = ToolCall(
                id="resolver", name="resolve_recovery",
                arguments={
                    "turn_id": old_turn,
                    "step": 0,
                    "call_id": old_call.id,
                    "inspection_token": "b" * 64,
                    "resolution": "not_applied",
                    "evidence": "目标仍为原状。",
                },
            )
            current_turn, _ = begin(workflow, [resolver])
            dispatch(workflow, current_turn, resolver)
            workflow.append(
                "recovery/resolution",
                RecoveryResolutionEventData(
                    turn_id=old_turn,
                    step=0,
                    call_id=old_call.id,
                    resolution="confirmed_not_applied",
                    evidence="Agent已完成双重只读检查。",
                ),
            )
            workflow.close()
            with SessionEventWorkflow(storage_directory=path) as restored:
                self.assertFalse(restored.unresolved_external_calls())
                repaired = {
                    item.call_id: item for item in restored.recovery_report.repaired
                }
                self.assertEqual(repaired["resolver"].status, "outcome_unknown")
                self.assertEqual(repaired["resolver"].retry_safety, "safe")
                self.assertTrue(any(
                    event.type == "recovery/resolution"
                    and event.data.call_id == old_call.id
                    for event in restored.events
                ))

    def test_compaction_rejects_partial_overlap_and_can_replace_old_summary(self):
        workflow = SessionEventWorkflow.in_memory()
        first = completed(workflow)
        second = completed(workflow)
        third = completed(workflow)
        workflow.append_compaction_summary(summary="旧摘要", replace_start_seq=first[0], replace_end_seq=second[1])
        with self.assertRaises(SessionEventError):
            workflow.append_compaction_summary(summary="部分覆盖", replace_start_seq=second[0], replace_end_seq=third[1])
        workflow.append_compaction_summary(summary="完整新摘要", replace_start_seq=first[0], replace_end_seq=third[1])
        projected = workflow.project_messages("系统")
        self.assertEqual(len(projected), 2)
        self.assertIn("完整新摘要", projected[1].content)
        self.assertTrue(any(event.type == "assistant/message" for event in workflow.events))

    def test_compaction_cannot_hide_exact_pending_recovery_identity(self):
        workflow = SessionEventWorkflow.in_memory()
        call = ToolCall(
            id="old-write", name="write_file",
            arguments={"path": "a", "content": "x", "mode": "create"},
        )
        turn, start = begin(workflow, [call])
        dispatch(workflow, turn, call)
        workflow.recover_incomplete()
        end = next(
            event.seq for event in workflow.events
            if isinstance(event.data, TurnEndEventData) and event.data.turn_id == turn
        )
        workflow.append_compaction_summary(
            summary="旧轮已摘要。", replace_start_seq=start, replace_end_seq=end
        )
        projected = workflow.project_messages("系统")
        self.assertIn("旧轮已摘要", projected[-2].content)
        self.assertIn(f"turn_id={turn}", projected[-1].content)
        self.assertIn("call_id=old-write", projected[-1].content)
        self.assertIn("inspect_recovery", projected[-1].content)

    def test_unpublished_partial_temporary_file_is_not_an_event(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(storage_directory=path) as workflow:
                completed(workflow)
            orphan = path / (".0000000000000007." + "a" * 32 + ".tmp")
            orphan.write_bytes(b'{"partial":')
            with SessionEventWorkflow(storage_directory=path) as workflow:
                self.assertEqual(len(workflow.events), 6)
                self.assertEqual(completed(workflow)[0], 7)
            self.assertEqual(orphan.read_bytes(), b'{"partial":')


if __name__ == "__main__":
    unittest.main()
