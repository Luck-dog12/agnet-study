"""Model outages discard input and recover automatically, without reset commands."""

from contextlib import ExitStack
import io
from pathlib import Path
from queue import Queue
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from app.agent import AgentLoopLimitError, AgentProtocolError, AgentRequestError, CodingAgent
from app.config import Settings
from app.observability import Observability
from app.schemas import ControlHostEventData, ToolMessage, ToolResult
from app.workflow import SessionEventWorkflow
from tests.model_service_helpers import ReadyModelService
from tests.test_agent import FakeClient, chunk, reply


class FailedContextTests(unittest.TestCase):
    def make_agent(self, responses, *, workflow=None, **kwargs):
        client = FakeClient(responses)
        with patch("app.agent.build_skill_catalog_message", return_value=None):
            agent = CodingAgent(
                client, Settings(api_key="offline-test", model="offline-model"),
                system_prompt="You are the offline test agent.",
                session_workflow=workflow or SessionEventWorkflow.in_memory(),
                observability=Mock(spec=Observability), **kwargs,
            )
        self.addCleanup(agent.close)
        return agent, client

    def test_new_request_after_failure_has_no_manual_latch_or_old_prompt(self):
        agent, client = self.make_agent([RuntimeError("private-error"), reply(content="new answer")])
        with self.assertRaises(AgentRequestError):
            agent.run("FAILED_REQUEST_MUST_NOT_RUN")
        before = agent.session_events
        self.assertEqual(agent.run("NEW_COMPLETE_REQUEST"), "new answer")
        self.assertEqual(len(client.completions.requests), 2)
        self.assertNotIn("FAILED_REQUEST_MUST_NOT_RUN", str(client.completions.requests[-1]["messages"]))
        self.assertIn("FAILED_REQUEST_MUST_NOT_RUN", str(before))
        self.assertFalse(hasattr(agent, "reset_model_requests"))

    def test_legacy_pause_record_does_not_lock_reopened_session(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "session"
            agent, _ = self.make_agent(
                [RuntimeError("offline")], workflow=SessionEventWorkflow(storage_directory=directory),
            )
            with self.assertRaises(AgentRequestError):
                agent.run("PERSISTED_FAILED_REQUEST")
            agent._session.append("host/control", ControlHostEventData(
                source="model_guard", command="pause", message="legacy paused state",
            ))
            agent.close()
            reopened, client = self.make_agent(
                [reply(content="fresh")], workflow=SessionEventWorkflow(storage_directory=directory),
            )
            self.assertEqual(reopened.run("new independent task"), "fresh")
            self.assertNotIn("PERSISTED_FAILED_REQUEST", str(client.completions.requests))
            self.assertNotIn("legacy paused state", str(client.completions.requests))
            reopened.close()

    def test_failed_plan_leaves_no_user_or_manual_control_events(self):
        agent, client = self.make_agent([RuntimeError("private-plan-error"), reply(content="new")])
        with self.assertRaises(AgentRequestError):
            agent.plan("PRIVATE_FAILED_PLAN")
        self.assertEqual(agent.session_events, ())
        self.assertEqual(agent.run("new request"), "new")
        self.assertNotIn("PRIVATE_FAILED_PLAN", str(client.completions.requests[-1]))

    def test_tool_limit_preserves_task_and_accepts_a_normal_retry(self):
        agent, client = self.make_agent([
            reply(calls=[("one", "read_file", '{"path":"a"}')]),
            reply(calls=[("two", "read_file", '{"path":"b"}')]),
            reply(content="retried normally"),
        ], max_tool_rounds=1)
        tool_result = ToolMessage(
            tool_call_id="one", result=ToolResult(status="success", output="read already completed"),
        ).model_dump_json()
        with patch("app.agent.call_tool", return_value=tool_result) as tool:
            with self.assertRaises(AgentLoopLimitError):
                agent.run("ORIGINAL_TASK_REACHING_TOOL_LIMIT")
            self.assertEqual(agent.run("重新尝试"), "retried normally")
            tool.assert_called_once()
        self.assertIn("ORIGINAL_TASK_REACHING_TOOL_LIMIT", str(client.completions.requests[-1]))

    def test_model_failure_preserves_tool_facts_but_not_reviving_summary(self):
        agent, client = self.make_agent([
            reply(content="OLD_ASSISTANT_PLAN", calls=[
                ("written", "write_file", '{"path":"made.txt","content":"done"}'),
            ]), RuntimeError("offline"), reply(content="new work done"),
        ])
        result = ToolMessage(
            tool_call_id="written", result=ToolResult(status="success", output="FILE_ALREADY_WRITTEN"),
        ).model_dump_json()
        with patch("app.agent.call_tool", return_value=result) as tool:
            with self.assertRaises(AgentRequestError):
                agent.run("FAILED_WRITE_AND_CONTINUE")
            agent.compact_history(
                summary="Resume FAILED_WRITE_AND_CONTINUE immediately.",
                replace_start_seq=1, replace_end_seq=agent.session_events[-1].seq,
            )
            agent.run("NEW_TASK_ONLY")
            tool.assert_called_once()
        sent = client.completions.requests[-1]["messages"]
        self.assertNotIn("FAILED_WRITE_AND_CONTINUE", str(sent))
        self.assertNotIn("OLD_ASSISTANT_PLAN", str(sent))
        self.assertIn("FILE_ALREADY_WRITTEN", str(sent))
        tool_index = next(index for index, message in enumerate(sent) if message["role"] == "tool")
        self.assertEqual(sent[tool_index - 1]["tool_calls"][0]["id"], "written")

    def test_unknown_mutation_still_requires_existing_recovery_check(self):
        agent, _ = self.make_agent([
            reply(calls=[("unknown", "write_file", '{"path":"a","content":"x"}')]),
            reply(calls=[("again", "write_file", '{"path":"b","content":"y"}')]),
            reply(content="must inspect first"),
        ])
        with patch("app.agent.call_tool", return_value="invalid tool result") as tool:
            with self.assertRaises(AgentProtocolError):
                agent.run("original mutation")
            agent.run("new request")
            tool.assert_called_once()
        self.assertEqual(len(agent.pending_recovery()), 1)

    def test_failed_interrupt_reassessment_revokes_original_request_chain(self):
        started, release = Event(), Event()

        def interrupted_stream():
            started.set()
            yield chunk(content="old partial")
            if not release.wait(3):
                raise AssertionError("stream was not released")
            yield chunk(content="late")

        agent, client = self.make_agent([
            interrupted_stream(), RuntimeError("reassessment offline"), reply(content="fresh"),
        ])
        outcome = []

        def run():
            try:
                agent.run("ORIGINAL_INTERRUPTED_REQUEST")
            except BaseException as error:
                outcome.append(error)

        worker = Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(started.wait(3))
            agent.interrupt("stop current generation")
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], AgentRequestError)
        agent.run("new task")
        self.assertNotIn("ORIGINAL_INTERRUPTED_REQUEST", str(client.completions.requests[-1]))


class AvailabilityCliTests(unittest.TestCase):
    def run_script(self, client, producer_body, *, initial_state="ready", agent_factory=None):
        """Drive the real broker/main loop with a controlled, network-free monitor."""
        from app import main as main_module

        incoming, reads = Queue(), Queue()
        created, unavailable = Event(), Event()
        services, failures = [], []

        class ControlledService(ReadyModelService):
            def start(self):
                self.set_state(initial_state)
                created.set()

            def mark_unavailable(self):
                super().mark_unavailable()
                unavailable.set()

        def factory(settings, *, on_change):
            service = ControlledService(settings, on_change=on_change)
            services.append(service)
            return service

        def reader(*_args):
            reads.put(True)
            return incoming.get(timeout=5)

        def send(value):
            reads.get(timeout=5)
            incoming.put(value)

        def produce():
            try:
                if not created.wait(5):
                    raise AssertionError("startup check was not started")
                producer_body(send, reads, incoming, services[0], unavailable)
            except BaseException as error:
                failures.append(error)
                incoming.put("exit")

        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            sessions = Path(root) / "sessions"
            for context in (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "create_model_service_monitor", side_effect=factory),
                patch.object(main_module, "Observability", return_value=Mock(spec=Observability)),
                patch.object(main_module, "MaintenanceService", return_value=Mock()),
                patch("app.agent.build_skill_catalog_message", return_value=None),
                patch("builtins.input", side_effect=reader),
                patch("sys.stdout", stdout), patch("sys.stderr", stderr),
            ):
                stack.enter_context(context)
            if agent_factory is not None:
                stack.enter_context(patch.object(main_module, "CodingAgent", side_effect=agent_factory))
            producer = Thread(target=produce, daemon=True)
            producer.start()
            try:
                code = main_module.main(["--new-session", "health", "--plain"])
            finally:
                incoming.put("exit")
                producer.join(5)
            diagnostics = stdout.getvalue() + stderr.getvalue()
            self.assertFalse(producer.is_alive(), diagnostics)
            self.assertEqual(failures, [], diagnostics)
            self.assertEqual(code, 0, diagnostics)
            self.assertTrue(services[0].closed)
            with SessionEventWorkflow(storage_directory=sessions / "health") as workflow:
                events = workflow.events
        return events, stdout.getvalue(), stderr.getvalue()

    def test_startup_failure_discards_input_and_recovery_accepts_first_new_line(self):
        client = FakeClient([reply(content="fresh result")])

        def produce(send, reads, incoming, service, _unavailable):
            send("DURING_STARTUP_CHECK")
            send("/status")
            reads.get(timeout=5)  # status was handled before changing health
            service.set_state("unavailable")
            incoming.put("DURING_OUTAGE")
            send("继续执行")
            # A reader may span recovery. The first new submission must work.
            reads.get(timeout=5)
            service.set_state("ready")
            incoming.put("FIRST_NEW_REQUEST_AFTER_RECOVERY")
            send("exit")

        events, stdout, stderr = self.run_script(client, produce, initial_state="checking")
        prompts = [event.data.message.content for event in events if event.type == "user/message"]
        self.assertEqual(prompts, ["FIRST_NEW_REQUEST_AFTER_RECOVERY"])
        self.assertEqual(len(client.completions.requests), 1)
        self.assertIn("model_state=checking", stdout)
        self.assertIn("模型服务", stderr + stdout)
        self.assertNotIn("/model reset", stderr + stdout)

    def test_runtime_outage_discards_deferred_input_and_recovers_without_reset(self):
        started, fail = Event(), Event()

        def failing_stream():
            started.set()
            if not fail.wait(5):
                raise AssertionError("failure was not released")
            raise RuntimeError("offline provider")
            yield

        client = FakeClient([failing_stream(), reply(content="fresh result")])

        def produce(send, reads, incoming, service, unavailable):
            send("FAILED_CLI_REQUEST")
            if not started.wait(5):
                raise AssertionError("model did not start")
            send("QUEUED_CONTINUE")
            send("QUEUED_CONTINUE_AGAIN")
            reads.get(timeout=5)
            fail.set()
            if not unavailable.wait(5):
                raise AssertionError("service was not marked unavailable")
            incoming.put("DURING_OUTAGE")
            send("/status")
            send("继续")
            reads.get(timeout=5)
            service.set_state("ready")
            incoming.put("NEW_COMPLETE_REQUEST")
            send("exit")

        try:
            events, stdout, stderr = self.run_script(client, produce)
        finally:
            fail.set()
        prompts = [event.data.message.content for event in events if event.type == "user/message"]
        self.assertEqual(prompts, ["FAILED_CLI_REQUEST", "NEW_COMPLETE_REQUEST"])
        self.assertEqual(len(client.completions.requests), 2)
        self.assertNotIn("FAILED_CLI_REQUEST", str(client.completions.requests[-1]))
        self.assertIn("model_state=unavailable", stdout)
        self.assertNotIn("/model reset", stderr + stdout)

    def test_tool_round_limit_does_not_mark_service_down_or_drop_retry(self):
        from app import main as main_module

        retry_finished = Event()
        original_submit = main_module.DaemonExecutor.submit

        def submit(executor, runner, prompt, **kwargs):
            future = original_submit(executor, runner, prompt, **kwargs)
            if prompt == "重新尝试":
                future.add_done_callback(lambda _future: retry_finished.set())
            return future

        client = FakeClient([
            reply(calls=[("one", "read_file", '{"path":"a"}')]),
            reply(calls=[("two", "read_file", '{"path":"b"}')]),
            reply(content="retry accepted"),
        ])

        def factory(*args, **kwargs):
            return CodingAgent(*args, **kwargs, max_tool_rounds=1)

        def produce(send, _reads, _incoming, _service, unavailable):
            send("SEARCH_ORIGINAL_TASK")
            send("重新尝试")
            # An explicit exit while a request is active intentionally cancels
            # its queue. This test exercises retry, so await its real Future.
            self.assertTrue(retry_finished.wait(5))
            send("exit")
            self.assertFalse(unavailable.is_set())

        result = ToolMessage(tool_call_id="one", result=ToolResult(status="success", output="ok"))
        with patch("app.agent.call_tool", return_value=result.model_dump_json()), \
                patch.object(main_module.DaemonExecutor, "submit", new=submit):
            events, stdout, stderr = self.run_script(client, produce, agent_factory=factory)
        prompts = [event.data.message.content for event in events if event.type == "user/message"]
        self.assertEqual(prompts, ["SEARCH_ORIGINAL_TASK", "重新尝试"])
        self.assertEqual(len(client.completions.requests), 3)
        self.assertNotIn("/model reset", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
