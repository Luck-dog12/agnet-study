"""多会话 CLI、单写者租约与 host 事件回放回归测试。"""

from __future__ import annotations

import errno
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.agent import AgentSessionError, CodingAgent
from app.config import Settings
from app.schemas import (
    NormalHostEventData,
    SessionEvent,
    SessionUserMessageSnapshot,
    StepStartEventData,
    TurnStartEventData,
    UrgentHostEventData,
    UserMessageEventData,
)
from app.workflow import (
    SessionEventError,
    SessionEventWorkflow,
    SessionStoreBusyError,
    canonical_json_snapshot,
)


class SessionPayloadReplayTests(unittest.TestCase):
    def test_normal_and_urgent_json_roundtrip_choose_exact_payload_model(self):
        common = {
            "version": 1,
            "session_id": "0" * 32,
            "seq": 1,
            "time": "2026-09-24T00:00:00.000000+00:00",
        }
        cases = (
            ("host/normal", NormalHostEventData, "normal"),
            ("host/urgent", UrgentHostEventData, "urgent"),
        )
        for event_type, model_type, message in cases:
            with self.subTest(event_type=event_type):
                event = SessionEvent(
                    **common,
                    type=event_type,
                    data=model_type(source="cli", message=message),
                )
                raw = canonical_json_snapshot(event)
                restored = SessionEvent.model_validate_json(raw)
                self.assertIs(type(restored.data), model_type)
                self.assertEqual(canonical_json_snapshot(restored), raw)

    def test_interrupted_session_containing_urgent_event_reopens(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "session"
            turn_id = uuid4().hex
            workflow = SessionEventWorkflow(storage_directory=directory, recover=False)
            workflow.append("turn/start", TurnStartEventData(turn_id=turn_id))
            workflow.append(
                "user/message",
                UserMessageEventData(
                    turn_id=turn_id,
                    message=SessionUserMessageSnapshot(content="question"),
                ),
            )
            workflow.append("step/start", StepStartEventData(turn_id=turn_id, step=0))
            workflow.append(
                "host/urgent",
                UrgentHostEventData(source="cli", message="stop"),
            )
            workflow.close()

            with SessionEventWorkflow(storage_directory=directory) as restored:
                urgent = next(event for event in restored.events if event.type == "host/urgent")
                self.assertIs(type(urgent.data), UrgentHostEventData)
                self.assertEqual(restored.events[-1].type, "turn/end")


class SessionLeaseTests(unittest.TestCase):
    def test_different_session_directories_can_hold_locks_together(self):
        with tempfile.TemporaryDirectory() as root:
            first = SessionEventWorkflow(storage_directory=Path(root) / "one")
            second = SessionEventWorkflow(storage_directory=Path(root) / "two")
            try:
                self.assertNotEqual(first.session_id, second.session_id)
            finally:
                second.close()
                first.close()

    def test_same_session_reports_precise_busy_error(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "same"
            first = SessionEventWorkflow(storage_directory=directory)
            try:
                with self.assertRaises(SessionStoreBusyError):
                    SessionEventWorkflow(storage_directory=directory)
            finally:
                first.close()

    def test_non_lock_storage_failure_is_not_mislabeled_busy(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("app.workflow._safe_regular_file", side_effect=OSError("secret")):
                with self.assertRaises(SessionEventError) as caught:
                    SessionEventWorkflow(storage_directory=Path(root) / "session")
            self.assertNotIsInstance(caught.exception, SessionStoreBusyError)

    @unittest.skipUnless(os.name == "nt", "msvcrt locking errno is Windows-only")
    def test_windows_locking_only_maps_contention_errno_to_busy(self):
        import msvcrt

        with tempfile.TemporaryDirectory() as root:
            with patch.object(
                msvcrt,
                "locking",
                side_effect=OSError(errno.EACCES, "mock contention"),
            ):
                with self.assertRaises(SessionStoreBusyError):
                    SessionEventWorkflow(storage_directory=Path(root) / "busy")

            with patch.object(
                msvcrt,
                "locking",
                side_effect=OSError(errno.EIO, "mock storage failure"),
            ):
                with self.assertRaises(SessionEventError) as caught:
                    SessionEventWorkflow(storage_directory=Path(root) / "broken")
            self.assertNotIsInstance(caught.exception, SessionStoreBusyError)


class SessionCliTests(unittest.TestCase):
    def setUp(self):
        from tests.model_service_helpers import ready_model_service_patch

        self.enterContext(ready_model_service_patch())

    def test_new_explicit_and_generated_names_are_unique(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "data" / "sessions"
            explicit = main_module._create_session_directory("work-1", sessions)
            generated = main_module._create_session_directory(None, sessions)
            self.assertEqual(explicit.name, "work-1")
            self.assertRegex(generated.name, r"^session-[0-9]{8}-[0-9]{6}-[0-9a-f]{10}$")
            self.assertNotEqual(explicit.directory, generated.directory)
            with self.assertRaisesRegex(ValueError, "已存在"):
                main_module._create_session_directory("work-1", sessions)

    def test_unsafe_and_reserved_names_are_rejected(self):
        from app import main as main_module

        for name in ("../escape", "/absolute", "current", ".hidden", "CON", "con.txt", "NUL.foo", "tail."):
            with self.subTest(name=name), self.assertRaises(ValueError):
                main_module._validate_session_name(name, allow_current=False)
        with (
            patch.object(
                main_module,
                "load_settings",
                side_effect=AssertionError("invalid names must fail before config"),
            ),
            patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(main_module.main(["--new-session", "../escape"]), 2)

    def test_resume_requires_existing_directory_with_regular_header(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            sessions.mkdir()
            (sessions / "empty").mkdir()
            with patch.object(main_module, "_SESSIONS_ROOT", sessions):
                empty_args = main_module._build_argument_parser().parse_args(["--resume", "empty"])
                with self.assertRaisesRegex(ValueError, "缺少"):
                    main_module._prepare_session_request(empty_args)
                with SessionEventWorkflow(storage_directory=sessions / "valid"):
                    pass
                valid_args = main_module._build_argument_parser().parse_args(["--resume", "valid"])
                request = main_module._prepare_session_request(valid_args)
                selected = request.choice
                self.assertIsNotNone(selected)
            self.assertEqual(selected.name, "valid")

    def test_list_only_reports_session_headers_without_loading_config(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            sessions.mkdir()
            (sessions / "empty").mkdir()
            with SessionEventWorkflow(storage_directory=sessions / "valid"):
                pass
            stdout = io.StringIO()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", side_effect=AssertionError("must not load")),
                patch("sys.stdout", stdout),
            ):
                self.assertEqual(main_module.main(["--list-sessions"]), 0)
            self.assertIn("valid", stdout.getvalue())
            self.assertNotIn("empty", stdout.getvalue())

    def test_mutually_exclusive_session_options_are_rejected(self):
        from app import main as main_module

        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            main_module._build_argument_parser().parse_args(
                ["--new-session", "one", "--resume", "two"]
            )

    def test_unsafe_parent_link_is_rejected_before_child_creation(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / "real"
            linked = Path(root) / "linked"
            real.mkdir()
            try:
                linked.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("当前主机不允许创建目录符号链接")
            with self.assertRaises(ValueError):
                main_module._create_session_directory("safe", linked / "sessions")
            self.assertFalse((real / "sessions").exists())

    def test_main_distinguishes_busy_from_damaged_session_and_closes_client(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            with SessionEventWorkflow(storage_directory=sessions / "valid"):
                pass
            for error, expected, absent in (
                (SessionStoreBusyError("secret"), "正在被其它 Agent 占用", "已损坏"),
                (SessionEventError("secret"), "已损坏或无法安全恢复", "正在被"),
            ):
                with self.subTest(error=type(error).__name__):
                    client = Mock()
                    observer = Mock()
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with (
                        patch.object(main_module, "_SESSIONS_ROOT", sessions),
                        patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                        patch.object(main_module, "create_client", return_value=client),
                        patch.object(main_module, "Observability", return_value=observer),
                        patch.object(main_module, "CodingAgent", side_effect=error),
                        patch("builtins.input", return_value="触发模型运行时"),
                        patch("sys.stdout", stdout),
                        patch("sys.stderr", stderr),
                    ):
                        self.assertEqual(main_module.main(["--resume", "valid"]), 2)
                    self.assertIn(expected, stderr.getvalue())
                    self.assertNotIn(absent, stderr.getvalue())
                    self.assertNotIn("secret", stderr.getvalue())
                    if isinstance(error, SessionStoreBusyError):
                        self.assertIn("agent --new-session [NAME]", stderr.getvalue())
                    client.close.assert_called_once_with()

    def test_new_session_waits_for_valid_config_before_creating_directory(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            stderr = io.StringIO()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", side_effect=ValueError("secret")),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main_module.main(["--new-session", "not-created"]), 2)
            self.assertFalse(sessions.exists())
            self.assertNotIn("secret", stderr.getvalue())

    def test_new_session_creation_failure_happens_before_client_creation(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            sessions.mkdir()
            (sessions / "duplicate").mkdir()
            client = Mock()
            observer = Mock()
            stderr = io.StringIO()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client) as client_factory,
                patch.object(main_module, "Observability", return_value=observer),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main_module.main(["--new-session", "duplicate"]), 2)
            self.assertIn("已存在", stderr.getvalue())
            client_factory.assert_not_called()

    def test_current_busy_message_suggests_independent_session(self):
        from app import main as main_module

        client = Mock()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=Mock()),
                patch.object(
                    main_module,
                    "CodingAgent",
                    side_effect=SessionStoreBusyError("secret"),
                ),
                patch("builtins.input", return_value="触发模型运行时"),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main_module.main([]), 2)
        self.assertIn("会话“current”", stderr.getvalue())
        self.assertIn("agent --new-session [NAME]", stderr.getvalue())
        client.close.assert_called_once_with()

    def test_banner_names_selected_new_session(self):
        from app import main as main_module

        fake_agent = Mock()
        fake_agent.pending_recovery.return_value = ()
        maintenance = Mock()
        client = Mock()
        observer = Mock()
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client) as client_factory,
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", return_value=fake_agent),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", stdout),
            ):
                self.assertEqual(main_module.main(["--new-session", "terminal-two"]), 0)
        self.assertIn("当前会话：terminal-two", stdout.getvalue())
        self.assertIn("--resume NAME", stdout.getvalue())
        client_factory.assert_not_called()
        fake_agent.close.assert_not_called()


class AgentInitializationCleanupTests(unittest.TestCase):
    def test_owned_session_lock_is_released_when_agent_initialization_fails(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "session"
            with patch("app.agent.AsyncTaskManager.from_events", side_effect=RuntimeError("secret")):
                with self.assertRaises(AgentSessionError):
                    CodingAgent(Mock(), Settings(api_key="fake"), session_storage_directory=directory)
            with SessionEventWorkflow(storage_directory=directory):
                pass


if __name__ == "__main__":
    unittest.main()
