"""CLI startup recovery for durable remote tasks, entirely offline."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.schemas import (
    AsyncTaskCompletedEventData,
    AsyncTaskFailedEventData,
    AsyncTaskStartedEventData,
    ToolResult,
)
from app.workflow import (
    SessionEventWorkflow,
    SessionStoreBusyError,
    tool_result_snapshot,
)
from tests.test_async_workflow import TASK, append_until_accepted


class _FakeSandboxSettings:
    def __init__(self) -> None:
        self.require_calls = 0

    def require_enabled(self) -> None:
        self.require_calls += 1


class MainRemoteTaskCleanupTests(unittest.TestCase):
    def _create_session(
        self,
        sessions: Path,
        name: str,
        *,
        backend: str,
        task_state: str,
    ) -> tuple[Path, str]:
        path = sessions / name
        with SessionEventWorkflow(
            storage_directory=path,
            execution_backend_name=backend,  # type: ignore[arg-type]
            recover=False,
        ) as workflow:
            if task_state != "none":
                append_until_accepted(workflow)
            if task_state in {"started", "completed", "failed"}:
                workflow.append(
                    "async/task/started",
                    AsyncTaskStartedEventData(task_id=TASK),
                )
            if task_state == "completed":
                workflow.append(
                    "async/task/completed",
                    AsyncTaskCompletedEventData(
                        task_id=TASK,
                        result=tool_result_snapshot(
                            ToolResult(status="success", output="done")
                        ),
                    ),
                )
            if task_state == "failed":
                workflow.append(
                    "async/task/failed",
                    AsyncTaskFailedEventData(
                        task_id=TASK,
                        error="durable recovery fact",
                        recovery_status="outcome_unknown",
                    ),
                )
            session_id = workflow.session_id
        return path, session_id

    def test_remote_started_task_is_cleaned_after_recovery_while_lock_is_held(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            path, session_id = self._create_session(
                sessions,
                "remote-started",
                backend="aliyun-sandbox",
                task_state="started",
            )
            sandbox_settings = _FakeSandboxSettings()
            cleanup_calls: list[tuple[object, str]] = []

            def cleanup(settings: object, observed_session_id: str) -> None:
                cleanup_calls.append((settings, observed_session_id))
                self.assertIs(settings, sandbox_settings)
                self.assertEqual(observed_session_id, session_id)
                event_types = [
                    json.loads(event_file.read_text(encoding="utf-8"))["type"]
                    for event_file in sorted(path.glob("[0-9]*.json"))
                ]
                self.assertNotIn("async/task/failed", event_types)
                with self.assertRaises(SessionStoreBusyError):
                    SessionEventWorkflow(
                        storage_directory=path,
                        recover=False,
                    )

            forbidden_runtime = Mock(
                side_effect=AssertionError("direct exit must not create runtime")
            )
            forbidden_client = Mock(
                side_effect=AssertionError("direct exit must not create client")
            )
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    side_effect=cleanup,
                ),
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    forbidden_runtime,
                ),
                patch.object(main_module, "create_client", forbidden_client),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-started", "--plain"]
                    ),
                    0,
                )

            self.assertEqual(cleanup_calls, [(sandbox_settings, session_id)])
            forbidden_runtime.assert_not_called()
            forbidden_client.assert_not_called()
            with SessionEventWorkflow(
                storage_directory=path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as restored:
                failures = [
                    event.data
                    for event in restored.events
                    if isinstance(event.data, AsyncTaskFailedEventData)
                ]
                self.assertEqual(len(failures), 1)
                self.assertEqual(
                    failures[0].recovery_status,
                    "outcome_unknown",
                )
                self.assertFalse(any(
                    event.type in {
                        "async/task/completed",
                        "async/task/cancelled",
                    }
                    for event in restored.events
                ))

            retry_cleanup = Mock()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    retry_cleanup,
                ),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-started", "--plain"]
                    ),
                    0,
                )
            retry_cleanup.assert_not_called()

    def test_cleanup_failure_is_secret_free_and_releases_session_lock(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            path, session_id = self._create_session(
                sessions,
                "remote-cleanup-failure",
                backend="aliyun-sandbox",
                task_state="started",
            )
            sandbox_settings = _FakeSandboxSettings()
            cleanup = Mock(
                side_effect=RuntimeError("provider-secret-must-not-escape")
            )
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    cleanup,
                ),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-cleanup-failure", "--plain"]
                    ),
                    2,
                )
            cleanup.assert_called_once_with(sandbox_settings, session_id)
            self.assertNotIn("provider-secret", stderr.getvalue())
            with SessionEventWorkflow(
                storage_directory=path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as restored:
                self.assertFalse(any(
                    isinstance(event.data, AsyncTaskFailedEventData)
                    for event in restored.events
                ))

            retry_cleanup = Mock()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    retry_cleanup,
                ),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-cleanup-failure", "--plain"]
                    ),
                    0,
                )
            retry_cleanup.assert_called_once_with(sandbox_settings, session_id)
            with SessionEventWorkflow(
                storage_directory=path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as restored:
                failures = [
                    event.data
                    for event in restored.events
                    if isinstance(event.data, AsyncTaskFailedEventData)
                ]
                self.assertEqual(len(failures), 1)
                self.assertEqual(failures[0].recovery_status, "outcome_unknown")

    def test_started_task_cleanup_is_not_repeated_before_first_runtime(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            _path, session_id = self._create_session(
                sessions,
                "remote-started-then-run",
                backend="aliyun-sandbox",
                task_state="started",
            )
            sandbox_settings = _FakeSandboxSettings()
            call_order: list[str] = []
            cleanup = Mock(side_effect=lambda *_args: call_order.append("cleanup"))
            runtime = Mock()

            def create_runtime(backend, observed_session, settings, **kwargs):
                call_order.append("runtime")
                self.assertEqual(backend, "aliyun-sandbox")
                self.assertEqual(observed_session, session_id)
                self.assertIs(settings, sandbox_settings)
                self.assertEqual(kwargs, {"collect_orphans": True})
                return runtime

            agent = Mock()
            agent.max_tool_rounds = 8
            agent.max_plan_clarifications = 5
            agent.pending_recovery.return_value = ()
            agent.run.return_value = "done"
            agent.plan.return_value = "done"

            def build_agent(_client, _settings, **kwargs):
                workflow = kwargs["session_workflow"]
                agent.close.side_effect = lambda **_close_kwargs: workflow.close()
                return agent

            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    cleanup,
                ),
                patch.object(
                    main_module,
                    "create_execution_runtime",
                    side_effect=create_runtime,
                ),
                patch.object(main_module, "create_client", return_value=Mock()),
                patch.object(main_module, "Observability", return_value=Mock()),
                patch.object(main_module, "CodingAgent", side_effect=build_agent),
                patch.object(main_module, "MaintenanceService", return_value=Mock()),
                patch("builtins.input", side_effect=["trigger", "exit"]),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(
                        ["--resume", "remote-started-then-run", "--plain"]
                    ),
                    0,
                )
            cleanup.assert_called_once_with(sandbox_settings, session_id)
            self.assertEqual(call_order, ["cleanup", "runtime"])

    def test_non_dispatched_terminal_new_and_local_sessions_do_not_cleanup(self):
        from app import main as main_module

        cases = (
            ("remote-empty", "aliyun-sandbox", "none"),
            ("remote-accepted", "aliyun-sandbox", "accepted"),
            ("remote-completed", "aliyun-sandbox", "completed"),
            ("remote-failed", "aliyun-sandbox", "failed"),
            ("local-started", "local", "started"),
        )
        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            for name, backend, task_state in cases:
                self._create_session(
                    sessions,
                    name,
                    backend=backend,
                    task_state=task_state,
                )

            for name, backend, _task_state in cases:
                with self.subTest(name=name):
                    sandbox_settings = _FakeSandboxSettings()
                    cleanup = Mock(
                        side_effect=AssertionError(
                            "non-dispatched or terminal session must not clean"
                        )
                    )
                    sandbox_loader = Mock(return_value=sandbox_settings)
                    with (
                        patch.object(main_module, "_SESSIONS_ROOT", sessions),
                        patch.object(
                            main_module,
                            "load_settings",
                            return_value=Settings(api_key="fake"),
                        ),
                        patch.object(
                            main_module,
                            "load_sandbox_settings",
                            sandbox_loader,
                        ),
                        patch.object(
                            main_module,
                            "cleanup_recovered_remote_session",
                            cleanup,
                        ),
                        patch("builtins.input", return_value="exit"),
                        patch("sys.stdout", io.StringIO()),
                        patch("sys.stderr", io.StringIO()),
                    ):
                        self.assertEqual(
                            main_module.main(
                                ["--resume", name, "--plain"]
                            ),
                            0,
                        )
                    cleanup.assert_not_called()
                    if backend == "local":
                        sandbox_loader.assert_not_called()
                    else:
                        sandbox_loader.assert_called_once_with()

            cleanup = Mock(
                side_effect=AssertionError("new session must not clean")
            )
            sandbox_settings = _FakeSandboxSettings()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="fake"),
                ),
                patch.object(
                    main_module,
                    "load_sandbox_settings",
                    return_value=sandbox_settings,
                ),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    cleanup,
                ),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main([
                        "--new-session",
                        "remote-new",
                        "--execution-backend",
                        "aliyun-sandbox",
                        "--plain",
                    ]),
                    0,
                )
            cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
