"""Offline user-visible queue lifecycle through the real CLI and input broker."""

from contextlib import ExitStack
import io
from pathlib import Path
from queue import Queue
import tempfile
from threading import Condition, Event, Thread
import unittest
from unittest.mock import Mock, patch

from app.agent import CodingAgent
from app.config import Settings
from app.console_ui import ConsoleUI
from app.observability import Observability
from app.schemas import ToolMessage, ToolResult
from app.workflow import SessionEventWorkflow
from tests.model_service_helpers import ReadyModelService
from tests.test_agent import FakeClient, chunk, reply


class Transcript(io.StringIO):
    """Synchronize on visible feedback, rather than on scheduler delays."""

    def __init__(self):
        super().__init__()
        self.condition = Condition()

    def write(self, text):
        with self.condition:
            result = super().write(text)
            self.condition.notify_all()
            return result

    def getvalue(self):
        with self.condition:
            return super().getvalue()

    def wait_for(self, text):
        with self.condition:
            if not self.condition.wait_for(lambda: text in self.getvalue(), timeout=5):
                raise AssertionError(f"Missing visible feedback {text!r}: {self.getvalue()!r}")


class CommandQueueTests(unittest.TestCase):
    def run_cli(self, client, script, *, releases=(), max_tool_rounds=8):
        from app import main as main_module

        inputs, reads, completed = Queue(), Queue(), Queue()
        output = Transcript()
        services, agents, failures = [], [], []
        started = Event()
        original_submit = main_module.DaemonExecutor.submit

        class Service(ReadyModelService):
            def start(self):
                super().start()
                started.set()

        def monitor_factory(settings, *, on_change):
            service = Service(settings, on_change=on_change)
            services.append(service)
            return service

        def agent_factory(*args, **kwargs):
            agent = CodingAgent(*args, **kwargs, max_tool_rounds=max_tool_rounds)
            agents.append(agent)
            return agent

        def submit(executor, *args, **kwargs):
            future = original_submit(executor, *args, **kwargs)
            future.add_done_callback(completed.put)
            return future

        def reader(*_args):
            reads.put(True)
            return inputs.get(timeout=5)

        def send(value):
            reads.get(timeout=5)
            inputs.put(value)

        def produce():
            try:
                self.assertTrue(started.wait(5))
                script(send, output, completed, services[0], agents)
            except BaseException as error:
                failures.append(error)
                for release in releases:
                    release.set()
                inputs.put("exit")

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            sessions = Path(root) / "sessions"
            for context in (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "create_model_service_monitor", side_effect=monitor_factory),
                patch.object(main_module, "CodingAgent", side_effect=agent_factory),
                patch.object(main_module, "Observability", return_value=Mock(spec=Observability)),
                patch.object(main_module, "MaintenanceService", return_value=Mock()),
                patch.object(main_module.DaemonExecutor, "submit", new=submit),
                patch("app.agent.build_skill_catalog_message", return_value=None),
                patch("builtins.input", side_effect=reader),
                patch("sys.stdout", output), patch("sys.stderr", output),
            ):
                stack.enter_context(context)
            producer = Thread(target=produce, daemon=True)
            producer.start()
            try:
                code = main_module.main([
                    "--new-session", "queue", "--plain", "--execution-backend", "local",
                ])
            finally:
                for release in releases:
                    release.set()
                inputs.put("exit")
                producer.join(5)
            self.assertFalse(producer.is_alive(), output.getvalue())
            self.assertEqual(failures, [], output.getvalue())
            self.assertEqual(code, 0, output.getvalue())
            with SessionEventWorkflow(storage_directory=sessions / "queue") as workflow:
                prompts = [e.data.message.content for e in workflow.events if e.type == "user/message"]
        return prompts, output.getvalue()

    def test_queued_inputs_are_visible_before_execution_and_run_once_in_order(self):
        entered = [Event(), Event()]
        releases = [Event(), Event()]

        def blocked(index):
            yield chunk(content=f"ANSWER_{index}_PARTIAL")
            entered[index].set()
            if not releases[index].wait(5):
                raise AssertionError("response was not released")
            yield chunk(content=f"_FINISHED_{index}")

        # Preserve whitespace/newlines in the actual model request, while the
        # display uses a safe, concise preview.
        second = "  1\n按第一项执行  "
        client = FakeClient([blocked(0), blocked(1), reply(content="THIRD_FINISHED")])

        def script(send, output, completed, _service, agents):
            send("FIRST_REQUEST")
            self.assertTrue(entered[0].wait(5))
            send(second)
            output.wait_for("已排队 #1")
            send("如何？")
            output.wait_for("已排队 #2")
            output.wait_for("待执行列表（2 项）")
            send("/status")
            output.wait_for("run_state=running")
            output.wait_for("queued=2")
            self.assertEqual(len(client.completions.requests), 1)
            self.assertEqual([
                e.data.message.content for e in agents[0].session_events if e.type == "user/message"
            ], ["FIRST_REQUEST"])
            self.assertNotIn("开始执行 #1", output.getvalue())
            releases[0].set()
            self.assertTrue(entered[1].wait(5))
            output.wait_for("开始执行 #1")
            self.assertNotIn("开始执行 #2", output.getvalue())
            releases[1].set()
            for _ in range(3):
                completed.get(timeout=5).result()
            output.wait_for("THIRD_FINISHED")
            send("exit")

        prompts, output = self.run_cli(client, script, releases=releases)
        self.assertEqual(prompts, ["FIRST_REQUEST", second, "如何？"])
        for number in (1, 2):
            self.assertEqual(output.count(f"已排队 #{number}："), 1)
            self.assertEqual(output.count(f"开始执行 #{number}："), 1)
        self.assertLess(output.index("开始执行 #1"), output.index("开始执行 #2"))
        self.assertIn("待执行列表：空", output)
        self.assertIn("ANSWER_0_PARTIAL\n", output)
        self.assertNotIn("已排队", str(client.completions.requests))
        self.assertNotIn("/status", str(client.completions.requests))
        self.assertNotIn("\x1b", output)

    def test_failure_visibly_cancels_pending_inputs_and_never_replays_them(self):
        entered, release = Event(), Event()

        def fail():
            entered.set()
            if not release.wait(5):
                raise AssertionError("failure was not released")
            raise RuntimeError("offline service failure")
            yield

        client = FakeClient([fail(), reply(content="NEW_REQUEST_FINISHED")])

        def script(send, output, completed, service, _agents):
            send("FAILED_REQUEST")
            self.assertTrue(entered.wait(5))
            send("OLD_QUEUED_ONE")
            output.wait_for("已排队 #1")
            send("OLD_QUEUED_TWO")
            output.wait_for("已排队 #2")
            release.set()
            output.wait_for("已取消待执行命令（模型服务失败）")
            output.wait_for("待执行列表：空")
            cancelled = output.getvalue().split("已取消待执行命令（模型服务失败）", 1)[1]
            self.assertIn("#1 OLD_QUEUED_ONE", cancelled)
            self.assertIn("#2 OLD_QUEUED_TWO", cancelled)
            self.assertEqual(service.state, "unavailable")
            send("DURING_OUTAGE")
            output.wait_for("此条输入未被接受")
            service.set_state("ready")
            output.wait_for("模型服务可用")
            send("NEW_REQUEST")
            completed.get(timeout=5)  # failed first generation
            completed.get(timeout=5).result()
            output.wait_for("NEW_REQUEST_FINISHED")
            send("exit")

        prompts, output = self.run_cli(client, script, releases=(release,))
        self.assertEqual(prompts, ["FAILED_REQUEST", "NEW_REQUEST"])
        self.assertNotIn("开始执行 #", output)
        self.assertNotIn("DURING_OUTAGE", output)
        self.assertEqual(len(client.completions.requests), 2)
        self.assertNotIn("OLD_QUEUED", str(client.completions.requests))
        self.assertNotIn("FAILED_REQUEST", str(client.completions.requests[-1]))

    def test_tool_round_limit_keeps_pending_request_and_announces_its_start(self):
        entered, release = Event(), Event()

        def first():
            entered.set()
            if not release.wait(5):
                raise AssertionError("first tool call was not released")
            yield from reply(calls=[("one", "read_file", '{"path":"a"}')])

        client = FakeClient([
            first(), reply(calls=[("two", "read_file", '{"path":"b"}')]),
            reply(content="RETRY_FINISHED"),
        ])

        def script(send, output, completed, service, _agents):
            send("ORIGINAL_SEARCH")
            self.assertTrue(entered.wait(5))
            send("重新尝试")
            output.wait_for("已排队 #1")
            release.set()
            completed.get(timeout=5)
            completed.get(timeout=5).result()
            output.wait_for("RETRY_FINISHED")
            self.assertEqual(service.state, "ready")
            send("exit")

        result = ToolMessage(tool_call_id="one", result=ToolResult(status="success", output="ok"))
        with patch("app.agent.call_tool", return_value=result.model_dump_json()):
            prompts, output = self.run_cli(client, script, releases=(release,), max_tool_rounds=1)
        self.assertEqual(prompts, ["ORIGINAL_SEARCH", "重新尝试"])
        self.assertIn("模型工具调用超过最大轮次", output)
        self.assertIn("开始执行 #1：重新尝试", output)
        self.assertNotIn("已取消待执行命令", output)

    def test_status_during_approval_does_not_replace_approval_or_become_a_task(self):
        from app import main as main_module
        from app.workflow import ToolConfirmationRequest

        client = FakeClient([
            reply(calls=[("approval", "write_file", '{"path":"notes.txt","content":"sample"}')]),
            reply(content="APPROVAL_FINISHED"),
        ])
        decisions = []

        def tool_call(_call, *, confirmation_callback, **_kwargs):
            decisions.append(confirmation_callback(ToolConfirmationRequest(
                tool_name="write_file", operation="overwrite", paths=("notes.txt",),
                description="offline approval", request_id="a" * 32,
            )))
            return ToolMessage(
                tool_call_id="approval", result=ToolResult(status="success", output="simulated only"),
            ).model_dump_json()

        def script(send, output, completed, _service, _agents):
            send("REQUEST_WITH_APPROVAL")
            output.wait_for("APPROVAL_PROMPT")
            send("/status")
            output.wait_for("run_state=waiting_approval")
            self.assertEqual(decisions, [])
            send("yes")
            completed.get(timeout=5).result()
            output.wait_for("APPROVAL_FINISHED")
            send("exit")

        with patch("app.agent.call_tool", side_effect=tool_call), \
                patch.object(main_module, "_show_confirmation", return_value="APPROVAL_PROMPT"):
            prompts, output = self.run_cli(client, script)
        self.assertEqual(prompts, ["REQUEST_WITH_APPROVAL"])
        self.assertEqual(decisions, [True])
        self.assertNotIn("已排队", output)
        self.assertNotIn("/status", str(client.completions.requests))

    def test_exit_cancels_visible_queue_instead_of_executing_it(self):
        entered, release = Event(), Event()

        def blocked():
            entered.set()
            release.wait(5)
            yield chunk(content="cancelled generation")

        client = FakeClient([blocked()])

        def script(send, output, completed, _service, _agents):
            send("ACTIVE_REQUEST")
            self.assertTrue(entered.wait(5))
            send("PENDING_REQUEST")
            output.wait_for("已排队 #1")
            send("exit")
            output.wait_for("已取消待执行命令（用户退出）")
            release.set()
            completed.get(timeout=5)  # consume the interrupted generation

        prompts, output = self.run_cli(client, script, releases=(release,))
        self.assertEqual(prompts, ["ACTIVE_REQUEST"])
        self.assertNotIn("开始执行 #1", output)
        self.assertIn("#1 PENDING_REQUEST", output.split("已取消待执行命令（用户退出）", 1)[1])
        self.assertEqual(len(client.completions.requests), 1)


class QueueStreamPresentationTests(unittest.TestCase):
    def test_queue_notices_are_separate_from_stream_and_sanitize_display_only(self):
        output = io.StringIO()
        ui = ConsoleUI(plain=True, output=output, error=output)
        prompt = "next\nline\r\t\x1b[2J\x07\u2028\u2029\u202e"
        ui.stream_text("partial answer")
        ui.queue_received(1, prompt, ((1, prompt),))
        ui.stream_text("continued answer")
        ui.finish_assistant()
        rendered = output.getvalue()
        self.assertIn("partial answer\nstatus  已排队 #1：", rendered)
        self.assertIn("\n│ continued answer\n", rendered)
        self.assertIn(r"next\nline\r\t\x1b[2J\x07\u2028\u2029\u202e", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertNotIn("\u2029", rendered)

    def test_default_console_output_follows_the_active_terminal_proxy(self):
        original, redirected = io.StringIO(), io.StringIO()
        with patch("sys.stdout", original):
            ui = ConsoleUI(plain=True)
            with patch("sys.stdout", redirected):
                ui.status("QUEUE_NOTICE")
        self.assertNotIn("QUEUE_NOTICE", original.getvalue())
        self.assertIn("QUEUE_NOTICE", redirected.getvalue())

    def test_explicit_console_output_remains_fixed(self):
        target, redirected = io.StringIO(), io.StringIO()
        ui = ConsoleUI(plain=True, output=target)
        with patch("sys.stdout", redirected):
            ui.status("QUEUE_NOTICE")
        self.assertIn("QUEUE_NOTICE", target.getvalue())
        self.assertEqual(redirected.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
