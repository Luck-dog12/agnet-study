"""Cloud-free CLI visibility and persistent lifecycle-budget assembly."""

from contextlib import ExitStack
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.sandbox_budget import SandboxBudget, SandboxBudgetError
from app.workflow import SessionEventWorkflow
from tests.model_service_helpers import ready_model_service_patch


class _Settings:
    timeout_seconds = 300
    idle_timeout_seconds = 120
    max_session_seconds = 3600

    def require_enabled(self):
        pass


class _Runtime:
    def __init__(self, *, snapshot_error=None):
        self.callback = None
        self.snapshot_calls = 0
        self.snapshot_error = snapshot_error

    def set_lifecycle_callback(self, callback):
        self.callback = callback

    def lifecycle_snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return {
            "state": "idle", "used_seconds": 121.5,
            "remaining_seconds": 3478.5, "timeout_seconds": 300,
            "idle_timeout_seconds": 120, "max_session_seconds": 3600,
        }

    def notify(self, message):
        if self.callback is not None:
            self.callback(message)

    def close(self):
        self.notify("CLOSE_NOTICE_MUST_BE_SUPPRESSED")


class MainSandboxLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(ready_model_service_patch())

    def test_factory_binds_persistent_budget_without_allocating_cloud(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            backend, runtime = Mock(), Mock()
            snapshot, initializer = object(), Mock()
            with (
                patch.object(main_module, "capture_workspace_seed", return_value=snapshot),
                patch.object(main_module, "create_workspace_seed_initializer", return_value=initializer),
                patch("app.execution_backend.AliyunSandboxExecutionBackend", return_value=backend) as backend_factory,
                patch("app.execution_runtime.ExecutionToolRuntime", return_value=runtime),
            ):
                result = main_module.create_execution_runtime(
                    "aliyun-sandbox", "a" * 32, _Settings(),
                    session_directory=directory,
                    session_cleanup_complete=True,
                )
            self.assertIs(result, runtime)
            budget = backend_factory.call_args.kwargs["budget"]
            self.assertIs(backend_factory.call_args.kwargs["session_cleanup_complete"], True)
            self.assertIsInstance(budget, SandboxBudget)
            self.assertEqual(budget.snapshot()["used_seconds"], 0)
            self.assertEqual(budget.snapshot()["remaining_seconds"], 3600)
            self.assertTrue((directory / "sandbox-budget.json").is_file())
            backend.ensure_ready.assert_not_called()

    def test_status_before_runtime_does_not_open_budget_or_create_cloud(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            sessions = Path(root) / "sessions"
            output = io.StringIO()
            forbidden = Mock(side_effect=AssertionError("status created a runtime"))
            for context in (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "load_sandbox_settings", return_value=_Settings()),
                patch.object(main_module, "create_execution_runtime", forbidden),
                patch.object(main_module, "cleanup_recovered_remote_session", forbidden),
                patch.object(main_module, "create_client", forbidden),
                patch("app.sandbox_budget.SandboxBudget", forbidden),
                patch("builtins.input", side_effect=["/status", "exit"]),
                patch("sys.stdout", output), patch("sys.stderr", output),
            ):
                stack.enter_context(context)
            self.assertEqual(main_module.main([
                "--new-session", "status", "--execution-backend", "aliyun-sandbox", "--plain",
            ]), 0)
            forbidden.assert_not_called()
            self.assertFalse((sessions / "status" / "sandbox-budget.json").exists())
            rendered = output.getvalue()
            self.assertIn("sandbox_state=not_created", rendered)
            self.assertIn("sandbox_used=待加载", rendered)
            self.assertIn("sandbox_remaining=待加载", rendered)
            self.assertIn("sandbox_ttl=300s", rendered)
            self.assertIn("sandbox_idle_limit=120s", rendered)
            self.assertIn("sandbox_session_limit=3600s", rendered)

    def test_corrupt_budget_reports_safe_error_before_cloud_or_model(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            sessions = Path(root) / "sessions"
            directory = sessions / "damaged-budget"
            with SessionEventWorkflow(storage_directory=directory, execution_backend_name="aliyun-sandbox"):
                pass
            ledger = directory / "sandbox-budget.json"
            ledger.write_text("CORRUPT_SECRET", encoding="utf-8")
            output = io.StringIO()
            forbidden = Mock(side_effect=AssertionError("damaged budget allocated resources"))
            for context in (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "load_sandbox_settings", return_value=_Settings()),
                patch.object(main_module, "capture_workspace_seed", return_value=object()),
                patch.object(main_module, "create_workspace_seed_initializer", return_value=Mock()),
                patch("app.execution_backend.AliyunSandboxExecutionBackend", forbidden),
                patch.object(main_module, "cleanup_recovered_remote_session", forbidden),
                patch.object(main_module, "create_client", forbidden),
                patch("builtins.input", return_value="start"),
                patch("sys.stdout", output), patch("sys.stderr", output),
            ):
                stack.enter_context(context)
            self.assertEqual(main_module.main(["--resume", "damaged-budget", "--plain"]), 2)
            forbidden.assert_not_called()
            self.assertIn("沙箱时长预算记录异常", output.getvalue())
            self.assertIn("预算不会自动清零", output.getvalue())
            self.assertNotIn("CORRUPT_SECRET", output.getvalue())
            self.assertEqual(ledger.read_text(encoding="utf-8"), "CORRUPT_SECRET")
            with SessionEventWorkflow(storage_directory=directory, execution_backend_name="aliyun-sandbox"):
                pass

    def test_runtime_notices_and_status_are_visible_but_exit_fences_callbacks(self):
        self._check_runtime_status_and_notices()

    def test_status_of_failed_budget_remains_available_without_leaking_error(self):
        self._check_runtime_status_and_notices(budget_failed=True)

    def _check_runtime_status_and_notices(self, *, budget_failed=False):
        from app import main as main_module

        runtime = _Runtime(snapshot_error=(
            SandboxBudgetError("BUDGET_SECRET") if budget_failed else None
        ))
        captured = {}

        class Agent:
            max_tool_rounds = 8
            max_plan_clarifications = 5

            def __init__(self, _client, _settings, **kwargs):
                self.workflow = kwargs["session_workflow"]
                captured["runtime"] = kwargs["execution_runtime"]

            def run(self, _prompt, *, on_text_delta=None):
                if on_text_delta is not None:
                    on_text_delta("PARTIAL_REPLY")
                runtime.notify("沙箱已因空闲回收；新工具将按需重建。")
                return "done"

            def get_status(self):
                from app.agent_status import project_agent_status

                return project_agent_status(self.workflow.session_id, self.workflow.events)

            def close(self):
                runtime.close()
                self.workflow.close()

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            sessions = Path(root) / "sessions"
            output = io.StringIO()
            for context in (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "load_sandbox_settings", return_value=_Settings()),
                patch.object(main_module, "create_execution_runtime", return_value=runtime),
                patch.object(main_module, "cleanup_recovered_remote_session"),
                patch.object(main_module, "create_client", return_value=Mock()),
                patch.object(main_module, "Observability", return_value=Mock()),
                patch.object(main_module, "MaintenanceService", return_value=Mock()),
                patch.object(main_module, "CodingAgent", side_effect=Agent),
                patch("builtins.input", side_effect=["start", "/status", "exit"]),
                patch("sys.stdout", output), patch("sys.stderr", output),
            ):
                stack.enter_context(context)
            self.assertEqual(main_module.main([
                "--new-session", "notices", "--execution-backend", "aliyun-sandbox", "--plain",
            ]), 0)
            self.assertIs(captured["runtime"], runtime)
            before = output.getvalue()
            runtime.notify("LATE_NOTICE_MUST_BE_SUPPRESSED")
            self.assertEqual(output.getvalue(), before)
            self.assertIn("PARTIAL_REPLY\nwarning  沙箱已因空闲回收", before)
            if budget_failed:
                self.assertIn("sandbox_state=unavailable", before)
                self.assertIn("sandbox_remaining=未知", before)
                self.assertIn("沙箱预算状态不可用", before)
                self.assertNotIn("BUDGET_SECRET", before)
            else:
                self.assertIn("sandbox_state=idle", before)
                self.assertIn("sandbox_used=121.5s", before)
                self.assertIn("sandbox_remaining=3478.5s", before)
            self.assertNotIn("CLOSE_NOTICE_MUST_BE_SUPPRESSED", before)
            self.assertGreater(runtime.snapshot_calls, 0)


if __name__ == "__main__":
    unittest.main()
