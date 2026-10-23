"""宿主外部状态只读检查和 CLI 恢复命令测试。"""

from contextlib import contextmanager
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.main import _recovery_command
from app.recovery import RecoveryToolDenied, RecoveryToolRuntime, inspect_external_state
from app.schemas import (
    InspectRecoveryArguments, InspectRecoveryOutput, ResolveRecoveryArguments,
    ResolveRecoveryOutput, ToolCall, ToolCallEventData, ToolMessage,
)
from app.workflow import tool_call_snapshot, tool_retry_safety
from tool.registry import call_tool


def event(call: ToolCall) -> ToolCallEventData:
    return ToolCallEventData(
        turn_id="a" * 32, step=0, call=tool_call_snapshot(call),
        retry_safety=tool_retry_safety(call),
    )


class RecoveryInspectionTests(unittest.TestCase):
    def test_write_postcondition_is_read_only_and_never_auto_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            target = project / "answer.txt"
            target.write_text("完成", encoding="utf-8")
            before = (target.read_bytes(), target.stat().st_mtime_ns)
            call = event(ToolCall(
                id="write", name="write_file",
                arguments={"path": "answer.txt", "content": "完成", "mode": "overwrite"},
            ))
            with patch("tool.file_access._PROJECT_ROOT", project):
                result = inspect_external_state(call)
            self.assertEqual(result.state, "matches_requested")
            self.assertIn("不等同于", result.evidence)
            self.assertEqual((target.read_bytes(), target.stat().st_mtime_ns), before)

    def test_missing_delete_target_is_evidence_not_execution_proof(self):
        with tempfile.TemporaryDirectory() as root:
            call = event(ToolCall(
                id="remove", name="bash_shell",
                arguments={"command": "remove_file", "paths": ["gone.txt"]},
            ))
            with patch("tool.file_access._PROJECT_ROOT", Path(root)):
                result = inspect_external_state(call)
            self.assertEqual(result.state, "matches_requested")
            self.assertIn("不能证明", result.evidence)

    def test_editor_and_unittest_remain_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root)
            (project / "a.py").write_text("old\n", encoding="utf-8")
            calls = (
                ToolCall(id="edit", name="editor_file", arguments={
                    "path": "a.py", "operation": "replace_text", "old_text": "old",
                    "new_text": "new", "expected_replacements": 1,
                }),
                ToolCall(id="test", name="bash_shell", arguments={
                    "command": "unittest", "paths": ["tests"]}),
            )
            with patch("tool.file_access._PROJECT_ROOT", project):
                results = [inspect_external_state(event(call)) for call in calls]
            self.assertEqual([item.state for item in results], ["unknown", "unknown"])

    def test_unsafe_or_protected_path_returns_unknown_without_details(self):
        call = event(ToolCall(
            id="protected", name="write_file",
            arguments={"path": "data/sessions/current/x", "content": "x", "mode": "create"},
        ))
        result = inspect_external_state(call)
        self.assertEqual(result.state, "unknown")
        self.assertNotIn("data/sessions", result.evidence)


class RecoveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.item = event(ToolCall(
            id="write", name="write_file",
            arguments={"path": "answer.txt", "content": "x", "mode": "create"},
        ))
        self.agent = Mock()
        self.agent.pending_recovery.return_value = (self.item,)

    def test_recovery_lists_safe_inspection_without_resolving(self):
        inspection = Mock(state="missing", evidence="目标不存在")
        output = io.StringIO()
        with patch("app.main.inspect_external_state", return_value=inspection), patch("sys.stdout", output):
            self.assertTrue(_recovery_command(self.agent, "/recovery"))
        self.agent.resolve_recovery.assert_not_called()
        self.assertIn("write_file", output.getvalue())
        self.assertIn("目标不存在", output.getvalue())

    def test_resolve_command_is_disabled_without_reading_or_mutating(self):
        output = io.StringIO()
        reader = Mock(side_effect=AssertionError("不应读取人工证据"))
        with patch("sys.stdout", output):
            self.assertTrue(_recovery_command(self.agent, "/resolve 1 applied"))
        reader.assert_not_called()
        self.agent.pending_recovery.assert_not_called()
        self.assertIn("已停用", output.getvalue())

    def test_normal_prompt_is_not_consumed(self):
        self.assertFalse(_recovery_command(self.agent, "继续处理"))
        self.agent.pending_recovery.assert_not_called()


class RecoveryToolRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.call = event(ToolCall(
            id="write", name="write_file",
            arguments={"path": "answer.txt", "content": "x", "mode": "create"},
        ))
        self.resolutions: list[tuple[ResolveRecoveryArguments, str]] = []

    def runtime(self, pending=None) -> RecoveryToolRuntime:
        items = (self.call,) if pending is None else pending
        return RecoveryToolRuntime(
            session_id="b" * 32,
            pending_provider=lambda: items,
            resolution_sink=lambda arguments, evidence: self.resolutions.append(
                (arguments, evidence)
            ),
        )

    def inspect_arguments(self) -> InspectRecoveryArguments:
        return InspectRecoveryArguments(
            turn_id=self.call.turn_id, step=self.call.step, call_id=self.call.call.id
        )

    def test_inspect_then_resolve_uses_exact_target_and_single_use_token(self):
        with tempfile.TemporaryDirectory() as root, patch(
            "tool.file_access._PROJECT_ROOT", Path(root)
        ):
            runtime = self.runtime()
            inspected = runtime.inspect(self.inspect_arguments())
            self.assertEqual(inspected.state, "missing")
            arguments = ResolveRecoveryArguments(
                turn_id=self.call.turn_id,
                step=self.call.step,
                call_id=self.call.call.id,
                inspection_token=inspected.inspection_token,
                resolution="not_applied",
                evidence="目标仍缺失，且轨迹中没有成功结果。",
            )
            output = runtime.resolve(arguments)
            self.assertEqual(output.resolution, "confirmed_not_applied")
            self.assertFalse(output.old_call_replayed)
            self.assertEqual(len(self.resolutions), 1)
            self.assertIn("Agent只读检查", self.resolutions[0][1])
            with self.assertRaises(RecoveryToolDenied):
                runtime.resolve(arguments)

    def test_target_mismatch_consumes_token_and_never_resolves(self):
        with tempfile.TemporaryDirectory() as root, patch(
            "tool.file_access._PROJECT_ROOT", Path(root)
        ):
            runtime = self.runtime()
            inspected = runtime.inspect(self.inspect_arguments())
            mismatched = ResolveRecoveryArguments(
                turn_id=self.call.turn_id,
                step=99,
                call_id=self.call.call.id,
                inspection_token=inspected.inspection_token,
                resolution="not_applied",
                evidence="错误目标。",
            )
            with self.assertRaises(RecoveryToolDenied):
                runtime.resolve(mismatched)
            self.assertFalse(self.resolutions)

    def test_resolve_rechecks_and_rejects_changed_external_state(self):
        with tempfile.TemporaryDirectory() as root, patch(
            "tool.file_access._PROJECT_ROOT", Path(root)
        ):
            project = Path(root)
            runtime = self.runtime()
            inspected = runtime.inspect(self.inspect_arguments())
            (project / "answer.txt").write_text("x", encoding="utf-8")
            arguments = ResolveRecoveryArguments(
                turn_id=self.call.turn_id,
                step=self.call.step,
                call_id=self.call.call.id,
                inspection_token=inspected.inspection_token,
                resolution="applied",
                evidence="哈希已匹配。",
            )
            with self.assertRaises(RecoveryToolDenied):
                runtime.resolve(arguments)
            self.assertFalse(self.resolutions)

    def test_verified_state_rejects_contradictory_resolution_and_consumes_token(self):
        cases = (
            ("matches_requested", "not_applied", "x"),
            ("different", "applied", "different"),
            ("missing", "applied", None),
        )
        for expected_state, resolution, content in cases:
            with self.subTest(state=expected_state, resolution=resolution), tempfile.TemporaryDirectory() as root:
                project = Path(root)
                if content is not None:
                    (project / "answer.txt").write_text(content, encoding="utf-8")
                self.resolutions.clear()
                with patch("tool.file_access._PROJECT_ROOT", project):
                    runtime = self.runtime()
                    inspected = runtime.inspect(self.inspect_arguments())
                    self.assertEqual(inspected.state, expected_state)
                    arguments = ResolveRecoveryArguments(
                        turn_id=self.call.turn_id,
                        step=self.call.step,
                        call_id=self.call.call.id,
                        inspection_token=inspected.inspection_token,
                        resolution=resolution,
                        evidence="轨迹判断不能覆盖可验证事实。",
                    )
                    with self.assertRaisesRegex(RecoveryToolDenied, "外部状态矛盾"):
                        runtime.resolve(arguments)
                    with self.assertRaisesRegex(RecoveryToolDenied, "令牌无效"):
                        runtime.resolve(arguments)
                    self.assertFalse(self.resolutions)
                    refreshed = runtime.inspect(self.inspect_arguments())
                    consistent_resolution = (
                        "applied"
                        if expected_state == "matches_requested"
                        else "not_applied"
                    )
                    accepted = runtime.resolve(ResolveRecoveryArguments(
                        turn_id=self.call.turn_id,
                        step=self.call.step,
                        call_id=self.call.call.id,
                        inspection_token=refreshed.inspection_token,
                        resolution=consistent_resolution,
                        evidence="重新检查后提交与当前可验证事实一致的结论。",
                    ))
                self.assertEqual(len(self.resolutions), 1)
                self.assertEqual(
                    accepted.resolution,
                    "confirmed_applied"
                    if consistent_resolution == "applied"
                    else "confirmed_not_applied",
                )

    def test_unknown_state_allows_either_reasoned_resolution(self):
        unknown_call = event(ToolCall(
            id="test", name="bash_shell",
            arguments={"command": "unittest", "paths": ["tests"]},
        ))
        for resolution, expected in (
            ("applied", "confirmed_applied"),
            ("not_applied", "confirmed_not_applied"),
        ):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as root:
                resolved: list[tuple[ResolveRecoveryArguments, str]] = []
                runtime = RecoveryToolRuntime(
                    session_id="b" * 32,
                    pending_provider=lambda: (unknown_call,),
                    resolution_sink=lambda arguments, evidence: resolved.append(
                        (arguments, evidence)
                    ),
                )
                inspect_arguments = InspectRecoveryArguments(
                    turn_id=unknown_call.turn_id,
                    step=unknown_call.step,
                    call_id=unknown_call.call.id,
                )
                with patch("tool.file_access._PROJECT_ROOT", Path(root)):
                    inspected = runtime.inspect(inspect_arguments)
                    self.assertEqual(inspected.state, "unknown")
                    output = runtime.resolve(ResolveRecoveryArguments(
                        **inspect_arguments.model_dump(mode="python"),
                        inspection_token=inspected.inspection_token,
                        resolution=resolution,
                        evidence="结合持久轨迹作出判断。",
                    ))
                self.assertEqual(output.resolution, expected)
                self.assertEqual(len(resolved), 1)

    def test_inspect_rejects_non_pending_exact_target(self):
        runtime = self.runtime(pending=())
        with self.assertRaises(RecoveryToolDenied):
            runtime.inspect(self.inspect_arguments())

    def test_registry_requires_bound_runtime_and_returns_strict_outputs(self):
        inspect_call = ToolCall(
            id="inspect", name="inspect_recovery",
            arguments=self.inspect_arguments().model_dump(mode="python"),
        )
        unbound = ToolMessage.model_validate_json(call_tool(inspect_call))
        self.assertEqual(unbound.result.status, "denied")
        with tempfile.TemporaryDirectory() as root, patch(
            "tool.file_access._PROJECT_ROOT", Path(root)
        ):
            runtime = self.runtime()
            inspected_message = ToolMessage.model_validate_json(call_tool(
                inspect_call, recovery_runtime=runtime
            ))
            self.assertEqual(inspected_message.result.status, "success")
            self.assertIsInstance(inspected_message.result.output, InspectRecoveryOutput)
            token = inspected_message.result.output.inspection_token
            resolve_call = ToolCall(
                id="resolve", name="resolve_recovery",
                arguments={
                    **self.inspect_arguments().model_dump(mode="python"),
                    "inspection_token": token,
                    "resolution": "not_applied",
                    "evidence": "目标仍缺失。",
                },
            )
            resolved_message = ToolMessage.model_validate_json(call_tool(
                resolve_call, recovery_runtime=runtime
            ))
            self.assertEqual(resolved_message.result.status, "success")
            self.assertIsInstance(resolved_message.result.output, ResolveRecoveryOutput)
            self.assertEqual(len(self.resolutions), 1)


if __name__ == "__main__":
    unittest.main()
