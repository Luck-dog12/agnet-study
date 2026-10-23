"""文件审批状态机的一次性绑定与拒绝路径测试。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.schemas import FileApprovalState, FileApprovalStatus
from app.workflow import FileApprovalWorkflow, WorkflowApprovalDenied


class FileApprovalWorkflowTests(unittest.TestCase):
    def test_new_file_state_defaults_to_unapproved(self):
        state = FileApprovalState(path="app/new.py")
        self.assertEqual(state.status, FileApprovalStatus.UNAPPROVED)
        self.assertIsNone(state.request_id)
        workflow = FileApprovalWorkflow()
        self.assertEqual(
            workflow.state_for("app/new.py").status,
            FileApprovalStatus.UNAPPROVED,
        )

    def test_pending_approved_and_final_reset_are_observable(self):
        workflow = FileApprovalWorkflow()
        observed = []

        def confirm(request):
            state = workflow.state_for("app/file.py")
            observed.append(("confirm", state.status, state.request_id, request.request_id))
            self.assertEqual(request.tool_name, "editor_file")
            self.assertEqual(request.operation, "edit")
            return True

        def action():
            state = workflow.state_for("app/file.py")
            observed.append(("action", state.status, state.request_id, state.request_id))
            return "done"

        result = workflow.run_approved(
            tool_name="editor_file",
            operation="edit",
            paths=("app/file.py",),
            description="编辑一个项目文件",
            confirmation_callback=confirm,
            action=action,
        )
        self.assertEqual(result, "done")
        self.assertEqual(observed[0][1], FileApprovalStatus.PENDING)
        self.assertEqual(observed[0][2], observed[0][3])
        self.assertEqual(observed[1][1], FileApprovalStatus.APPROVED)
        self.assertEqual(
            workflow.state_for("app/file.py").status,
            FileApprovalStatus.UNAPPROVED,
        )

    def test_missing_rejected_or_raising_confirmation_never_executes(self):
        for callback in (None, Mock(return_value=False), Mock(return_value="yes"), Mock(side_effect=RuntimeError)):
            with self.subTest(callback=callback):
                workflow = FileApprovalWorkflow()
                action = Mock()
                with self.assertRaises(WorkflowApprovalDenied):
                    workflow.run_approved(
                        tool_name="write_file",
                        operation="write_create",
                        paths=("new.py",),
                        description="创建文件",
                        confirmation_callback=callback,
                        action=action,
                    )
                action.assert_not_called()
                self.assertEqual(
                    workflow.state_for("new.py").status,
                    FileApprovalStatus.UNAPPROVED,
                )

    def test_approval_is_exact_and_cannot_be_reused(self):
        workflow = FileApprovalWorkflow()
        callback = Mock(return_value=True)
        first = Mock(return_value="first")
        self.assertEqual(
            workflow.run_approved(
                tool_name="write_file",
                operation="write_create",
                paths=("one.py",),
                description="创建文件",
                confirmation_callback=callback,
                action=first,
            ),
            "first",
        )
        second = Mock()
        with self.assertRaises(WorkflowApprovalDenied):
            workflow.run_approved(
                tool_name="editor_file",
                operation="edit",
                paths=("two.py",),
                description="编辑文件",
                confirmation_callback=None,
                action=second,
            )
        second.assert_not_called()
        self.assertEqual(callback.call_count, 1)

    def test_pre_execute_verification_failure_resets_without_action(self):
        workflow = FileApprovalWorkflow()
        action = Mock()
        with self.assertRaisesRegex(RuntimeError, "changed"):
            workflow.run_approved(
                tool_name="write_file",
                operation="write_overwrite",
                paths=("data.txt",),
                description="覆盖文件",
                confirmation_callback=lambda request: True,
                verify_before_execute=Mock(side_effect=RuntimeError("changed")),
                action=action,
            )
        action.assert_not_called()
        self.assertEqual(
            workflow.state_for("data.txt").status,
            FileApprovalStatus.UNAPPROVED,
        )

    def test_diff_approval_has_bound_states_and_requires_a_second_true(self):
        workflow = FileApprovalWorkflow()
        seen = []
        staged = object()

        def confirm(request):
            state = workflow.state_for("app/file.py")
            seen.append((request.phase, state.status, state.pending_file_id, state.diff_sha256))
            return True

        def prepare(pending_file_id):
            self.assertEqual(
                workflow.state_for("app/file.py").status,
                FileApprovalStatus.APPROVED,
            )
            return staged, "--- before\n+++ after\n-old\n+new\n"

        def commit(value):
            self.assertIs(value, staged)
            self.assertEqual(
                workflow.state_for("app/file.py").status,
                FileApprovalStatus.DIFF_APPROVED,
            )
            return "done"

        self.assertEqual(
            workflow.run_diff_approved(
                tool_name="editor_file",
                operation="edit_replace_text",
                paths=("app/file.py",),
                description="编辑文件",
                confirmation_callback=confirm,
                prepare=prepare,
                commit=commit,
            ),
            "done",
        )
        self.assertEqual(seen[0][0:2], ("operation", FileApprovalStatus.PENDING))
        self.assertEqual(seen[1][0:2], ("diff", FileApprovalStatus.DIFF_PENDING))
        self.assertRegex(seen[1][2], r"^[0-9a-f]{32}$")
        self.assertRegex(seen[1][3], r"^[0-9a-f]{64}$")
        self.assertEqual(
            workflow.state_for("app/file.py").status,
            FileApprovalStatus.UNAPPROVED,
        )

    def test_diff_rejection_keeps_staged_value_and_never_commits(self):
        workflow = FileApprovalWorkflow()
        staged = object()
        commit = Mock()
        discard = Mock()
        callbacks = iter((True, False))
        with self.assertRaises(WorkflowApprovalDenied):
            workflow.run_diff_approved(
                tool_name="write_file",
                operation="write_create",
                paths=("new.py",),
                description="创建文件",
                confirmation_callback=lambda request: next(callbacks),
                prepare=lambda pending_id: (staged, "diff"),
                commit=commit,
                discard_after_commit=discard,
            )
        commit.assert_not_called()
        discard.assert_not_called()

    def test_invalid_pending_override_is_rejected_before_prepare(self):
        for invalid in ("f" * 64, "G" * 32, "f" * 31, "f" * 33):
            with self.subTest(invalid=invalid):
                workflow = FileApprovalWorkflow()
                prepare = Mock()
                with self.assertRaises(WorkflowApprovalDenied):
                    workflow.run_diff_approved(
                        tool_name="write_file",
                        operation="write_create",
                        paths=("new.py",),
                        description="创建文件",
                        confirmation_callback=lambda _request: True,
                        prepare=prepare,
                        commit=Mock(),
                        pending_file_id_override=invalid,
                    )
                prepare.assert_not_called()
                self.assertEqual(
                    workflow.state_for("new.py").status,
                    FileApprovalStatus.UNAPPROVED,
                )


if __name__ == "__main__":
    unittest.main()
