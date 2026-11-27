from __future__ import annotations

import json
import io
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
from unittest.mock import patch

from app.permissions import is_sensitive_mutation_path, route_mutation_approval
from app.security_domain import (
    path_is_sealed_secret,
    route_account_action_authorization,
)
from app.schemas import ReadFileOutput, ToolMessage
from app.workflow import FileApprovalWorkflow, SessionEventWorkflow
from app.agent import AgentError, CodingAgent
from app.config import Settings
from tool import file_access
from tool.policy import PolicyLoadError, load_tool_policy, require_allowed_relative_path
from tool.registry import call_tool
from tests.test_agent import FakeClient, reply


class PermissionModeTests(unittest.TestCase):
    def setUp(self):
        from tests.model_service_helpers import ready_model_service_patch

        self.enterContext(ready_model_service_patch())
        self.temporary = tempfile.TemporaryDirectory(prefix="permission-tests-", dir="tests")
        base = Path(self.temporary.name)
        self.root = base / "项目"
        self.outside = base / "宿主目录"
        self.root.mkdir()
        self.outside.mkdir()
        self.root_patch = patch.object(file_access, "_PROJECT_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def invoke(self, name, arguments, **kwargs):
        kwargs.setdefault("permission_mode", "partial-control")
        raw = call_tool({"id": "permission", "name": name, "arguments": arguments}, **kwargs)
        return ToolMessage.model_validate_json(raw).result

    def test_sensitive_classifier_and_central_decision(self):
        for path in (
            ".env", ".env.local", "private/server.key", "tls/site.pem", ".agent/x",
            "code.md", "AGENTS.md", "data/sessions/s", "data/logs/a.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_sensitive_mutation_path(path, project_root=self.root))
        self.assertFalse(is_sensitive_mutation_path("app/main.py", project_root=self.root))
        self.assertFalse(route_mutation_approval(
            "partial-control", ("app/main.py",), project_root=self.root
        ).requires_approval)
        self.assertTrue(route_mutation_approval(
            "partial-control", ("code.md",), project_root=self.root
        ).requires_approval)
        self.assertFalse(route_mutation_approval(
            "full-access", ("code.md",), project_root=self.root
        ).requires_approval)

    def test_partial_control_skips_ordinary_approval_but_requires_sensitive_approval(self):
        seen = []
        ordinary = self.invoke(
            "write_file",
            {"path": "ordinary.txt", "content": "ok", "mode": "create"},
            confirmation_callback=lambda request: seen.append(request) or False,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(ordinary.status, "success")
        self.assertEqual(seen, [])

        denied = self.invoke(
            "write_file",
            {"path": "AGENTS.md", "content": "blocked", "mode": "create"},
            confirmation_callback=lambda request: seen.append(request) or False,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(denied.status, "denied")
        self.assertEqual(len(seen), 1)
        self.assertFalse((self.root / "AGENTS.md").exists())

        seen.clear()
        approved = self.invoke(
            "write_file",
            {"path": "AGENTS.md", "content": "approved", "mode": "create"},
            confirmation_callback=lambda request: seen.append(request.phase) or True,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(approved.status, "success")
        self.assertEqual(seen, ["diff"])

    def test_partial_control_honors_configured_protected_paths(self):
        policy = load_tool_policy()
        protected = policy.model_copy(deep=True)
        protected.file_access.protected_relative_paths.append("custom-sensitive.txt")
        seen = []
        with patch("tool.registry.load_tool_policy", return_value=protected):
            denied = self.invoke(
                "write_file",
                {
                    "path": "custom-sensitive.txt",
                    "content": "blocked",
                    "mode": "create",
                },
                confirmation_callback=lambda request: seen.append(request) or False,
                workflow=FileApprovalWorkflow(),
            )
        self.assertEqual(denied.status, "denied")
        self.assertEqual(len(seen), 1)
        self.assertFalse((self.root / "custom-sensitive.txt").exists())

    def test_full_access_unicode_absolute_ordinary_file_never_asks_approval(self):
        target = self.outside / "中文说明.txt"
        host_path = target.as_posix()
        seen = []
        written = self.invoke(
            "write_file",
            {"path": host_path, "content": "secret", "mode": "create"},
            permission_mode="full-access",
            confirmation_callback=lambda request: seen.append(request) or False,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(written.status, "success")
        self.assertEqual(written.output.path, host_path)
        self.assertEqual(seen, [])

        read = self.invoke(
            "read_file", {"path": host_path}, permission_mode="full-access"
        )
        self.assertEqual(read.status, "success")
        self.assertIsInstance(read.output, ReadFileOutput)
        self.assertEqual(read.output.content, "secret")

        removed = self.invoke(
            "bash_shell",
            {"command": "remove_file", "paths": [host_path]},
            permission_mode="full-access",
            confirmation_callback=lambda request: seen.append(request) or False,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(removed.status, "success")
        self.assertFalse(target.exists())
        self.assertEqual(seen, [])

    def test_sealed_secret_paths_are_denied_even_in_full_access(self):
        relative_paths = (
            "tls/中文证书.key",
            ".ENV",
            ".envrc",
            "credentials",
            "nested/token",
        )
        callback = unittest.mock.Mock(return_value=True)
        for index, relative in enumerate(relative_paths):
            with self.subTest(path=relative):
                target = self.outside / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("keep", encoding="utf-8")
                host_path = target.as_posix()
                self.assertTrue(path_is_sealed_secret(host_path))

                read = self.invoke(
                    "read_file", {"path": host_path}, permission_mode="full-access"
                )
                write = self.invoke(
                    "write_file",
                    {"path": host_path, "content": "changed", "mode": "overwrite"},
                    permission_mode="full-access",
                    confirmation_callback=callback,
                    workflow=FileApprovalWorkflow(),
                )
                edited = self.invoke(
                    "editor_file",
                    {
                        "path": host_path,
                        "operation": "replace_text",
                        "old_text": "keep",
                        "new_text": "changed",
                        "expected_replacements": 1,
                    },
                    permission_mode="full-access",
                    confirmation_callback=callback,
                    workflow=FileApprovalWorkflow(),
                )
                cleared = self.invoke(
                    "bash_shell",
                    {"command": "clear_file", "paths": [host_path]},
                    permission_mode="full-access",
                    confirmation_callback=callback,
                    workflow=FileApprovalWorkflow(),
                )
                removed = self.invoke(
                    "bash_shell",
                    {"command": "remove_file", "paths": [host_path]},
                    permission_mode="full-access",
                    confirmation_callback=callback,
                    workflow=FileApprovalWorkflow(),
                )
                self.assertEqual(read.status, "denied")
                self.assertEqual(write.status, "denied")
                self.assertEqual(edited.status, "denied")
                self.assertEqual(cleared.status, "denied")
                self.assertEqual(removed.status, "denied")
                self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        callback.assert_not_called()

        # Mutation internals may bypass configured protected paths only after a
        # trusted workflow transition; that flag never bypasses sealed secrets.
        with self.assertRaises(PolicyLoadError):
            require_allowed_relative_path(".envrc", allow_protected=True)

    def test_full_access_scan_and_grep_hide_and_cannot_target_sealed_files(self):
        (self.root / "normal.txt").write_text("needle", encoding="utf-8")
        (self.root / ".envrc").write_text("needle-secret", encoding="utf-8")
        (self.root / "server.key").write_text("needle-key", encoding="utf-8")

        listed = self.invoke(
            "list_files", {"pattern": "**/*"}, permission_mode="full-access"
        )
        searched = self.invoke(
            "grep",
            {"pattern": "needle", "file_pattern": "**/*"},
            permission_mode="full-access",
        )
        self.assertEqual(listed.status, "success")
        self.assertEqual([entry.path for entry in listed.output.entries], ["normal.txt"])
        self.assertEqual(searched.status, "success")
        self.assertEqual([match.path for match in searched.output.matches], ["normal.txt"])

        explicit_list = self.invoke(
            "list_files", {"pattern": ".env*"}, permission_mode="full-access"
        )
        explicit_grep = self.invoke(
            "grep",
            {"pattern": "needle", "file_pattern": "**/*.key"},
            permission_mode="full-access",
        )
        self.assertEqual(explicit_list.status, "denied")
        self.assertEqual(explicit_grep.status, "denied")

    def test_account_actions_require_independent_approval_in_every_mode(self):
        for mode in ("partial-control", "full-access"):
            with self.subTest(mode=mode):
                denied = route_account_action_authorization(mode)
                self.assertFalse(denied.authorized)
                self.assertTrue(denied.requires_independent_trusted_approval)
                approved = route_account_action_authorization(
                    mode, independent_trusted_approval=True
                )
                self.assertTrue(approved.authorized)
                self.assertFalse(approved.requires_independent_trusted_approval)

    def test_model_cannot_set_permission_mode_in_tool_arguments(self):
        result = self.invoke(
            "read_file", {"path": "x.txt", "permission_mode": "full-access"}
        )
        self.assertEqual(result.status, "error")

    def test_full_access_agent_mode_crosses_cli_style_executor_thread(self):
        target = self.outside / "跨线程.txt"
        client = FakeClient([
            reply(calls=[(
                "write", "write_file",
                json.dumps({
                    "path": target.as_posix(), "content": "thread", "mode": "create"
                }, ensure_ascii=False),
            )]),
            reply(content="done"),
        ])
        agent = CodingAgent(
            client,
            Settings(api_key="fake"),
            permission_mode="full-access",
            session_workflow=SessionEventWorkflow.in_memory(),
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                self.assertEqual(executor.submit(agent.run, "write").result(timeout=2), "done")
            self.assertEqual(target.read_text(encoding="utf-8"), "thread")
        finally:
            agent.close()

    def test_shutdown_fences_late_model_completion_from_session_writes(self):
        started = Event()
        release = Event()
        failures = []

        def blocked():
            started.set()
            release.wait(2)
            yield from ()

        workflow = SessionEventWorkflow.in_memory()
        agent = CodingAgent(
            FakeClient([blocked()]),
            Settings(api_key="fake"),
            permission_mode="partial-control",
            session_workflow=workflow,
        )

        def run() -> None:
            try:
                agent.run("blocked")
            except BaseException as exc:
                failures.append(exc)

        worker = Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(started.wait(1))
        agent.cancel_for_shutdown()
        agent.close(wait=False)
        events_at_close = workflow.events
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(workflow.events, events_at_close)
        self.assertTrue(failures)

    def test_host_setter_rejects_active_turn_and_switches_when_idle(self):
        started = Event()
        release = Event()

        def blocked():
            started.set()
            release.wait(2)
            yield from ()

        agent = CodingAgent(
            FakeClient([blocked()]),
            Settings(api_key="fake"),
            session_workflow=SessionEventWorkflow.in_memory(),
        )
        failures = []

        def run() -> None:
            try:
                agent.run("正在执行")
            except BaseException as exc:
                failures.append(exc)

        worker = Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(started.wait(1))
        with self.assertRaises(AgentError):
            agent.set_permission_mode("full-access")
        self.assertEqual(agent.permission_mode, "partial-control")
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(agent.set_permission_mode("full-access"), "full-access")
        self.assertEqual(agent.permission_mode, "full-access")
        self.assertEqual(agent.set_permission_mode("partial-control"), "partial-control")
        with self.assertRaises(ValueError):
            agent.set_permission_mode("model-controlled")  # type: ignore[arg-type]
        agent.close()

    def test_cli_permission_switch_requires_yes_and_can_downgrade(self):
        from app import main as main_module

        output = io.StringIO()
        error = io.StringIO()
        inputs = iter((
            "/permissions",
            "/permissions full-access", "no",
            "/permissions full-access", KeyboardInterrupt(),
            "/permissions full-access", EOFError(),
            "/permissions full-access", "yes please",
            "/permissions",
            "/permissions full-access", "yes",
            "/permissions",
            "/permissions partial-control",
            "/permissions",
            "exit",
        ))

        def read(*_args):
            item = next(inputs)
            if isinstance(item, BaseException):
                raise item
            return item

        with tempfile.TemporaryDirectory(dir="tests") as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "CodingAgent") as agent_factory,
                patch("builtins.input", side_effect=read),
                patch("sys.stdout", output),
                patch("sys.stderr", error),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "permission-switch", "--plain"]),
                    0,
                )
        rendered = output.getvalue() + error.getvalue()
        self.assertIn("权限提升已取消", rendered)
        self.assertIn("已切换到 full-access", rendered)
        self.assertIn("已立即降级到 partial-control", rendered)
        self.assertGreaterEqual(rendered.count("permission_mode=partial-control"), 2)
        self.assertIn("permission_mode=full-access", rendered)
        self.assertIn("用法：/permissions partial-control|full-access", rendered)
        agent_factory.assert_not_called()

    def test_cli_switch_updates_an_initialized_agent(self):
        from app import main as main_module

        modes = []
        created = []
        initialized = Event()
        input_failures = []
        output, error = io.StringIO(), io.StringIO()
        original_submit = main_module.DaemonExecutor.submit

        def submit(executor, *args, **kwargs):
            future = original_submit(executor, *args, **kwargs)
            future.add_done_callback(lambda _future: initialized.set())
            return future

        class RecordingAgent(CodingAgent):
            def set_permission_mode(self, permission_mode):
                result = super().set_permission_mode(permission_mode)
                modes.append(result)
                return result

        def factory(*args, **kwargs):
            instance = RecordingAgent(*args, **kwargs)
            created.append(instance)
            return instance

        client = FakeClient([reply(content="ready")])
        inputs = iter((
            "initialize",
            "/permissions full-access", "yes",
            "/permissions partial-control",
            "exit",
        ))

        def read(*_args):
            item = next(inputs)
            # Wait for the actual completed turn, not its early ready stream
            # chunk: otherwise yes can become a pre-read deferred command.
            if item == "/permissions full-access" and not initialized.wait(3):
                input_failures.append(f"Timed out awaiting UI before {item!r}")
                return "exit"
            return item

        with tempfile.TemporaryDirectory(dir="tests") as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "CodingAgent", side_effect=factory),
                patch.object(main_module, "Observability", return_value=unittest.mock.Mock()),
                patch.object(main_module, "MaintenanceService", return_value=unittest.mock.Mock()),
                patch.object(main_module.DaemonExecutor, "submit", new=submit),
                patch("builtins.input", side_effect=read),
                patch("sys.stdout", output),
                patch("sys.stderr", error),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "permission-live", "--plain"]),
                    0,
                )
        diagnostics = f"stdout={output.getvalue()!r}\nstderr={error.getvalue()!r}"
        self.assertEqual(input_failures, [], diagnostics)
        self.assertEqual(modes, ["full-access", "partial-control"], diagnostics)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].permission_mode, "partial-control")


if __name__ == "__main__":
    unittest.main()
