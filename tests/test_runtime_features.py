from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from queue import Queue
from threading import Event, Thread

from app.agent import AgentProtocolError, CodingAgent, _classify_request_error
from app.config import Settings
from app.workflow import SessionEventWorkflow
from tests.test_agent import FakeClient, reply


class RuntimeFeatureTests(unittest.TestCase):
    def _run_emergency_cli(self, client, inputs, *, session_name, patches=()):
        from app import main as main_module

        observer = unittest.mock.Mock()
        maintenance = unittest.mock.Mock()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"

            def read(*_args):
                item = inputs.get(timeout=3)
                if isinstance(item, BaseException):
                    raise item
                return item

            with ExitStack() as stack:
                for context in (
                    patch.object(main_module, "_SESSIONS_ROOT", sessions),
                    patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                    patch.object(main_module, "create_client", return_value=client),
                    patch.object(main_module, "Observability", return_value=observer),
                    patch.object(main_module, "MaintenanceService", return_value=maintenance),
                    patch("builtins.input", side_effect=read),
                    patch("sys.stdout", stdout),
                    patch("sys.stderr", stderr),
                    *patches,
                ):
                    stack.enter_context(context)
                started_at = time.monotonic()
                code = main_module.main(["--new-session", session_name, "--plain"])
                elapsed = time.monotonic() - started_at
            with SessionEventWorkflow(storage_directory=sessions / session_name) as recovered:
                self.assertIsNotNone(recovered.session_id)
            return code, elapsed, stdout.getvalue(), stderr.getvalue()

    def test_import_client_and_help_do_not_import_openai(self):
        project = Path(__file__).resolve().parent.parent
        scripts = (
            "import sys,app.client; assert 'openai' not in sys.modules",
            "import sys; from app.main import main; "
            "exec(\"try:\\n main(['--help'])\\nexcept SystemExit as error:\\n assert error.code == 0\"); "
            "assert not {'openai','e2b','prompt_toolkit','app.agent','app.observability','app.maintenance','app.sandbox_config','app.execution_backend','app.execution_runtime'} & sys.modules.keys()",
            "import sys,tempfile; from pathlib import Path; import app.main as entry; "
            "tmp=tempfile.TemporaryDirectory(); entry._SESSIONS_ROOT=Path(tmp.name)/'sessions'; "
            "assert entry.main(['--list-sessions']) == 0; "
            "assert not {'openai','e2b','prompt_toolkit','app.agent','app.observability','app.maintenance','app.sandbox_config','app.execution_backend','app.execution_runtime'} & sys.modules.keys(); tmp.cleanup()",
        )
        for script in scripts:
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, "-c", script], cwd=project,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                if "--help" in script:
                    self.assertNotIn("openai", completed.stderr)

    def test_console_ui_plain_non_tty_and_no_color_never_emit_ansi(self):
        from app.console_ui import ConsoleUI

        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        for plain, no_color, output in (
            (True, False, TtyBuffer()),
            (False, True, TtyBuffer()),
            (False, False, io.StringIO()),
        ):
            environment = {"NO_COLOR": "1"} if no_color else {}
            with self.subTest(plain=plain, no_color=no_color), patch.dict(
                os.environ, environment, clear=True
            ):
                ui = ConsoleUI(plain=plain, output=output, error=output)
                ui.banner("test")
                ui.status("ready")
                ui.start_assistant()
                ui.text("answer")
                self.assertNotIn("\x1b[", output.getvalue())

    def test_settings_direct_construction_rejects_runtime_bounds(self):
        for values in ({"timeout_seconds": 9}, {"timeout_seconds": 601}, {"max_retries": -1}, {"max_retries": 6}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(api_key="fake", **values)

    def test_plan_omits_tools_and_persists_normal_messages(self):
        plan = {
            "kind": "final",
            "plan": {
                "title": "计划", "objective": "只分析需求。",
                "in_scope": ["分析"], "out_of_scope": [], "assumptions": [],
                "steps": [{"number": 1, "title": "分析", "actions": ["检查需求"], "verification": "计划可读"}],
                "acceptance_criteria": ["步骤完整"], "risks": [], "open_questions": [],
            },
        }
        client = FakeClient([reply(content=json.dumps(plan, ensure_ascii=False))])
        workflow = SessionEventWorkflow.in_memory()
        agent = CodingAgent(client, Settings(api_key="fake"), system_prompt="system", session_workflow=workflow)
        try:
            self.assertIn("# 计划", agent.plan("只分析"))
            request = client.chat.completions.requests[0]
            self.assertNotIn("tools", request)
            self.assertNotIn("tool_choice", request)
            self.assertEqual(request["response_format"], {"type": "json_object"})
            self.assertIn("JSON Schema", request["messages"][-1]["content"])
            self.assertEqual([item.role for item in agent.messages], ["system", "user", "assistant"])
        finally:
            agent.close()

    def test_plan_refuses_compat_provider_tool_calls(self):
        client = FakeClient([reply(calls=[("x", "read_file", '{"path":"code.md"}')])])
        agent = CodingAgent(client, Settings(api_key="fake"), system_prompt="system", session_workflow=SessionEventWorkflow.in_memory())
        try:
            with self.assertRaisesRegex(AgentProtocolError, "计划模式拒绝"):
                agent.plan("只计划")
            self.assertEqual(agent.session_events, ())
        finally:
            agent.close()

    def test_request_error_classification_uses_safe_metadata(self):
        Timeout = type("APITimeoutError", (Exception,), {})
        Rate = type("RateLimitError", (Exception,), {"status_code": 429})
        Context = type("ContextLengthError", (Exception,), {})
        self.assertEqual(_classify_request_error(Timeout("SECRET")), "timeout")
        self.assertEqual(_classify_request_error(Rate("SECRET")), "rate_limit")
        self.assertEqual(_classify_request_error(Context("SECRET")), "context")

    def test_exit_before_first_request_does_not_construct_client(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            stdout = io.StringIO()
            client_factory = unittest.mock.Mock(side_effect=AssertionError("must not construct"))
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", client_factory),
                patch("builtins.input", return_value="exit"),
                patch("sys.stdout", stdout),
            ):
                self.assertEqual(main_module.main(["--new-session", "lazy-exit", "--plain"]), 0)
            client_factory.assert_not_called()
            with SessionEventWorkflow(storage_directory=Path(root) / "sessions" / "lazy-exit"):
                pass

    def test_cancelled_terminal_plan_does_not_construct_client_or_append_events(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            sessions = Path(root) / "sessions"
            client_factory = unittest.mock.Mock(side_effect=AssertionError("must not construct"))
            with (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", client_factory),
                patch("builtins.input", side_effect=["/plan", "", "exit"]),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(main_module.main(["--new-session", "plan-cancel", "--plain"]), 0)
            client_factory.assert_not_called()
            with SessionEventWorkflow(storage_directory=sessions / "plan-cancel") as workflow:
                self.assertEqual(workflow.events, ())

    def test_removed_mode_commands_only_warn_and_never_construct_runtime(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root:
            client_factory = unittest.mock.Mock(
                side_effect=AssertionError("removed mode command must not reach model")
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", client_factory),
                patch("builtins.input", side_effect=["/mode plan", "/mode act", "exit"]),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "removed-plan-mode", "--plain"]),
                    0,
                )
            client_factory.assert_not_called()
            self.assertNotIn("/mode plan  /mode act", stdout.getvalue())
            self.assertEqual(stderr.getvalue().count("模式切换命令已移除"), 2)

    def test_plan_choice_ctrl_c_eof_exit_and_interrupt_cancel_safely(self):
        from app import main as main_module
        from tests.test_agent import plan_question_json

        cases = {
            "ctrl_c": ["/plan 规划", KeyboardInterrupt()],
            "eof": ["/plan 规划", EOFError()],
            "exit": ["/plan 规划", "exit"],
            "interrupt": ["/plan 规划", "/interrupt", "exit"],
        }
        for name, inputs in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                client = FakeClient([
                    reply(content=plan_question_json("scope", "选择范围？"))
                ])
                stdout = io.StringIO()
                stderr = io.StringIO()
                with (
                    patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                    patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                    patch.object(main_module, "create_client", return_value=client),
                    patch.object(main_module, "Observability", return_value=unittest.mock.Mock()),
                    patch.object(main_module, "MaintenanceService", return_value=unittest.mock.Mock()),
                    patch("builtins.input", side_effect=inputs),
                    patch("sys.stdout", stdout),
                    patch("sys.stderr", stderr),
                ):
                    self.assertEqual(
                        main_module.main([
                            "--new-session", f"plan-cancel-{name}", "--plain"
                        ]),
                        0,
                    )
                self.assertEqual(len(client.completions.requests), 1)
                self.assertIn("Plan 需要确认：选择范围？", stdout.getvalue())
                if name == "exit":
                    self.assertIn("有界退出", stdout.getvalue())
                else:
                    self.assertIn("规划已取消", stderr.getvalue())

    def test_plain_terminal_plan_routes_numeric_choice_through_single_input_broker(self):
        from app import main as main_module
        from tests.test_agent import final_plan_json, plan_question_json

        client = FakeClient([
            reply(content=final_plan_json(
                "错误的未决计划", open_questions=["采用哪种范围？"]
            )),
            reply(content=plan_question_json("scope", "采用哪种范围？")),
            reply(content=final_plan_json("终端选择计划")),
        ])
        observer = unittest.mock.Mock()
        maintenance = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as root:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch("builtins.input", side_effect=["/plan 规划新功能", "2", "exit"]),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "plan-choice", "--plain"]),
                    0,
                )

            output = stdout.getvalue()
            self.assertIn("Plan 需要确认：采用哪种范围？", output)
            self.assertIn("1. 快速 [推荐]", output)
            self.assertIn("2. 平衡", output)
            self.assertIn("3. 完整", output)
            self.assertIn("# 终端选择计划", output)
            self.assertEqual(len(client.completions.requests), 3)
            self.assertIn(
                "open_questions 必须为 []",
                client.completions.requests[1]["messages"][-1]["content"],
            )
            answer = json.loads(client.completions.requests[2]["messages"][-1]["content"])
            self.assertEqual(answer["selected_option"]["index"], 2)
            self.assertNotIn("\x1b[", output)
            self.assertEqual(stderr.getvalue(), "")

    def test_plan_uses_dedicated_choice_read_without_act_read_ahead(self):
        from app import main as main_module
        from tests.test_agent import final_plan_json, plan_question_json

        inputs: Queue[str] = Queue()
        inputs.put("/plan 规划交互")
        calls: list[list[object]] = []
        read_started = [Event() for _ in range(3)]
        first_request_started = Event()
        second_request_started = Event()
        release_question = Event()
        release_final = Event()
        observations: dict[str, object] = {}

        def reader(prompt: str, complete_commands: bool) -> str:
            record: list[object] = [prompt, complete_commands, None]
            calls.append(record)
            index = len(calls) - 1
            if index < len(read_started):
                read_started[index].set()
            value = inputs.get(timeout=3)
            record[2] = value
            return value

        def delayed_question():
            first_request_started.set()
            release_question.wait(3)
            yield from reply(content=plan_question_json("scope", "选择实现范围？"))

        def delayed_final():
            second_request_started.set()
            release_final.wait(3)
            yield from reply(content=final_plan_json("专属交互计划"))

        client = FakeClient([
            delayed_question(),
            delayed_final(),
        ])
        observer = unittest.mock.Mock()
        maintenance = unittest.mock.Mock()

        def provide_choice_then_exit():
            observations["first_started"] = first_request_started.wait(2)
            time.sleep(0.12)
            observations["reads_before_question"] = len(calls)
            release_question.set()
            observations["choice_started"] = read_started[1].wait(2)
            inputs.put("2")
            observations["second_started"] = second_request_started.wait(2)
            time.sleep(0.12)
            observations["reads_after_choice"] = len(calls)
            release_final.set()
            observations["idle_started"] = read_started[2].wait(2)
            inputs.put("exit")

        producer = Thread(target=provide_choice_then_exit)
        producer.start()
        with tempfile.TemporaryDirectory() as root:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch("app.terminal_input.create_terminal_reader", return_value=reader),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "plan-choice-dedicated"]), 0
                )
        producer.join(2)
        self.assertFalse(producer.is_alive())
        self.assertEqual(
            observations,
            {
                "first_started": True,
                "reads_before_question": 1,
                "choice_started": True,
                "second_started": True,
                "reads_after_choice": 2,
                "idle_started": True,
            },
        )
        self.assertIn("Plan 需要确认：选择实现范围？", stdout.getvalue())
        self.assertIn("1. 快速 [推荐]", stdout.getvalue())
        self.assertIn("2. 平衡", stdout.getvalue())
        self.assertIn("3. 完整", stdout.getvalue())
        self.assertIn("# 专属交互计划", stdout.getvalue())
        self.assertEqual([record[2] for record in calls[:3]], [
            "/plan 规划交互", "2", "exit",
        ])
        self.assertEqual((calls[0][1], calls[2][1]), (True, True))
        self.assertEqual(calls[0][0], calls[2][0])
        self.assertTrue(str(calls[0][0]).endswith(" act "))
        self.assertEqual((calls[1][0], calls[1][1]), ("请选择 1、2 或 3：", False))
        answer = json.loads(client.completions.requests[1]["messages"][-1]["content"])
        self.assertEqual(answer["selected_option"]["index"], 2)
        self.assertEqual(stderr.getvalue(), "")

    def test_running_read_is_handed_to_next_idle_command_with_completion_enabled(self):
        from app import main as main_module

        class StubAgent:
            max_tool_rounds = 8
            max_plan_clarifications = 5

            def __init__(self):
                self.prompts: list[str] = []

            def run(self, prompt, *, on_text_delta=None):
                self.prompts.append(prompt)
                # Enter the active input-polling branch, then finish before
                # the second terminal line arrives.
                time.sleep(0.12)
                return "完成"

            def close(self):
                return None

        inputs: Queue[str] = Queue()
        inputs.put("处理任务")
        calls: list[list[object]] = []
        started = [Event() for _ in range(4)]

        def reader(prompt: str, complete_commands: bool) -> str:
            record: list[object] = [prompt, complete_commands, None]
            calls.append(record)
            index = len(calls) - 1
            if index < len(started):
                started[index].set()
            value = inputs.get(timeout=3)
            record[2] = value
            return value

        def provide_later_commands():
            self.assertTrue(started[1].wait(2))
            # The model future wins this race. The already-running read must
            # become the next idle command read, not an orphan plain input.
            time.sleep(0.15)
            inputs.put("/status")
            self.assertTrue(started[2].wait(2))
            inputs.put("/status")
            self.assertTrue(started[3].wait(2))
            inputs.put("exit")

        agent = StubAgent()
        client = unittest.mock.Mock()
        observer = unittest.mock.Mock()
        maintenance = unittest.mock.Mock()
        producer = Thread(target=provide_later_commands)
        producer.start()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch.object(main_module, "CodingAgent", side_effect=lambda *_args, **kwargs: (
                    kwargs["session_workflow"].close() or agent
                )),
                patch("app.terminal_input.create_terminal_reader", return_value=reader),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(
                    main_module.main(["--new-session", "repeat-completion"]), 0
                )
        producer.join(2)
        self.assertFalse(producer.is_alive())
        self.assertEqual(agent.prompts, ["处理任务"])
        self.assertEqual(
            [(record[2], record[1]) for record in calls[:4]],
            [
                ("处理任务", True),
                ("/status", True),
                ("/status", True),
                ("exit", True),
            ],
        )
        self.assertTrue(all(isinstance(record[0], str) and record[0] for record in calls[:4]))

    def test_exit_does_not_wait_for_model_future_that_never_returns(self):
        started = Event()
        release = Event()
        inputs: Queue[object] = Queue()
        inputs.put("永不返回")

        def blocked():
            started.set()
            release.wait(5)
            yield from ()

        client = FakeClient([blocked()])

        def request_exit():
            self.assertTrue(started.wait(2))
            inputs.put("exit")

        producer = Thread(target=request_exit)
        producer.start()
        try:
            code, elapsed, output, _error = self._run_emergency_cli(
                client, inputs, session_name="bounded-model"
            )
        finally:
            release.set()
        producer.join(2)
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)
        self.assertTrue(client.closed)

    def test_exit_unblocks_active_sensitive_approval_waiter(self):
        from app import main as main_module

        inputs: Queue[object] = Queue()
        inputs.put("修改敏感文件")
        approval_shown = Event()
        client = FakeClient([
            reply(calls=[("sensitive", "write_file", json.dumps({
                "path": "data/logs/runtime-feature-permission-exit.json",
                "content": "candidate",
                "mode": "create",
            }))]),
            reply(content="unused"),
        ])

        def show(_request):
            approval_shown.set()
            return "approve:"

        def request_exit():
            self.assertTrue(approval_shown.wait(2))
            inputs.put("exit")

        producer = Thread(target=request_exit)
        producer.start()
        code, elapsed, output, _error = self._run_emergency_cli(
            client,
            inputs,
            session_name="bounded-approval",
            patches=(patch.object(main_module, "_show_confirmation", side_effect=show),),
        )
        producer.join(2)
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)

    def test_repeated_ctrl_c_escalates_to_bounded_exit(self):
        release = Event()
        inputs: Queue[object] = Queue()
        inputs.put("卡住")
        inputs.put(KeyboardInterrupt())
        inputs.put(KeyboardInterrupt())

        def blocked():
            release.wait(5)
            yield from ()

        client = FakeClient([blocked()])
        try:
            code, elapsed, output, _error = self._run_emergency_cli(
                client, inputs, session_name="bounded-interrupt"
            )
        finally:
            release.set()
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)

    def test_real_main_thread_double_ctrl_c_uses_bounded_exit(self):
        from app import main as main_module

        started = Event()
        release = Event()
        inputs: Queue[object] = Queue()
        inputs.put("卡住")

        def blocked():
            started.set()
            release.wait(5)
            yield from ()

        def main_thread_interrupt(_future, timeout=None):
            self.assertTrue(started.wait(1))
            raise KeyboardInterrupt

        client = FakeClient([blocked()])
        try:
            with patch.object(
                main_module.Future,
                "result",
                new=main_thread_interrupt,
            ):
                code, elapsed, output, _error = self._run_emergency_cli(
                    client, inputs, session_name="bounded-real-sigint"
                )
        finally:
            release.set()
        self.assertTrue(started.wait(1))
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)

    def test_exit_is_consumed_after_first_real_main_thread_ctrl_c(self):
        from app import main as main_module

        started = Event()
        interrupted = Event()
        release = Event()
        inputs: Queue[object] = Queue()
        inputs.put("卡住")
        original_result = main_module.Future.result

        def blocked():
            started.set()
            release.wait(5)
            yield from ()

        def first_main_thread_interrupt(future, timeout=None):
            self.assertTrue(started.wait(1))
            if not interrupted.is_set():
                interrupted.set()
                raise KeyboardInterrupt
            return original_result(future, timeout=timeout)

        def request_exit():
            self.assertTrue(interrupted.wait(1))
            inputs.put("exit")

        producer = Thread(target=request_exit)
        producer.start()
        client = FakeClient([blocked()])
        try:
            with patch.object(
                main_module.Future,
                "result",
                new=first_main_thread_interrupt,
            ):
                code, elapsed, output, _error = self._run_emergency_cli(
                    client, inputs, session_name="bounded-sigint-then-exit"
                )
        finally:
            release.set()
        producer.join(1)
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)

    def test_main_thread_ctrl_c_releases_active_approval_before_exit(self):
        from app import main as main_module

        approval_shown = Event()
        interrupted = Event()
        release = Event()
        inputs: Queue[object] = Queue()
        inputs.put("修改敏感文件")
        original_get_line = main_module._InputBroker.get_line

        def blocked():
            release.wait(5)
            yield from ()

        client = FakeClient([
            reply(calls=[("sensitive", "write_file", json.dumps({
                "path": "data/logs/runtime-feature-permission-sigint.json",
                "content": "candidate",
                "mode": "create",
            }))]),
            blocked(),
        ])

        def show(_request):
            approval_shown.set()
            return "approve:"

        def interrupt_approval(broker, timeout=None):
            if approval_shown.is_set() and not interrupted.is_set():
                interrupted.set()
                raise KeyboardInterrupt
            return original_get_line(broker, timeout)

        def request_exit():
            self.assertTrue(interrupted.wait(1))
            inputs.put("exit")

        producer = Thread(target=request_exit)
        producer.start()
        try:
            code, elapsed, output, _error = self._run_emergency_cli(
                client,
                inputs,
                session_name="bounded-approval-sigint",
                patches=(
                    patch.object(main_module, "_show_confirmation", side_effect=show),
                    patch.object(
                        main_module._InputBroker,
                        "get_line",
                        new=interrupt_approval,
                    ),
                ),
            )
        finally:
            release.set()
        producer.join(1)
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)

    def test_second_main_thread_ctrl_c_inside_broker_poll_is_bounded(self):
        from app import main as main_module

        started = Event()
        first_interrupt = Event()
        second_interrupt = Event()
        release = Event()
        inputs: Queue[object] = Queue()
        inputs.put("卡住")
        original_result = main_module.Future.result
        original_poll_line = main_module._InputBroker.poll_line

        def blocked():
            started.set()
            release.wait(5)
            yield from ()

        def interrupt_future_once(future, timeout=None):
            self.assertTrue(started.wait(1))
            if not first_interrupt.is_set():
                first_interrupt.set()
                raise KeyboardInterrupt
            return original_result(future, timeout=timeout)

        def interrupt_helper_poll_once(broker, timeout=0.05):
            if first_interrupt.is_set() and not second_interrupt.is_set():
                second_interrupt.set()
                raise KeyboardInterrupt
            return original_poll_line(broker, timeout=timeout)

        client = FakeClient([blocked()])
        try:
            with (
                patch.object(
                    main_module.Future,
                    "result",
                    new=interrupt_future_once,
                ),
                patch.object(
                    main_module._InputBroker,
                    "poll_line",
                    new=interrupt_helper_poll_once,
                ),
            ):
                code, elapsed, output, _error = self._run_emergency_cli(
                    client, inputs, session_name="bounded-sigint-in-poll"
                )
        finally:
            release.set()
        self.assertTrue(second_interrupt.is_set())
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 2)
        self.assertIn("有界退出", output)


if __name__ == "__main__":
    unittest.main()
