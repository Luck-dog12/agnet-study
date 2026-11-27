"""Offline status queries across real Agent lifecycle boundaries."""

import json
import io
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.agent import AgentPlanCancelledError, AgentRequestError, AgentSessionError, CodingAgent
from app.agent_status import project_agent_status
from app.config import Settings
from app.observability import Observability
from app.schemas import (
    AsyncTaskCancelRequestedEventData, AsyncTaskCancelledEventData,
    AsyncTaskLateResultEventData, AsyncTaskStartedEventData,
    ToolCall, ToolMessage, ToolResult,
)
from app.workflow import SessionEventError, SessionEventWorkflow, tool_result_snapshot
from tests.test_agent import FakeClient, chunk, final_plan_json, plan_question_json, reply
from tests.test_async_workflow import TASK, append_until_accepted


class AgentStatusTests(unittest.TestCase):
    def agent(self, responses=(), **kwargs):
        kwargs.setdefault("session_workflow", SessionEventWorkflow.in_memory())
        with patch("app.agent.build_skill_catalog_message", return_value=None):
            agent = CodingAgent(
                FakeClient(list(responses)), Settings(api_key="SECRET_API_KEY"),
                system_prompt="SECRET_PROMPT", observability=Mock(spec=Observability), **kwargs,
            )
        self.addCleanup(agent.close)
        return agent

    def start(self, action):
        outcome = []

        def invoke():
            try:
                outcome.append(action())
            except BaseException as error:
                outcome.append(error)

        worker = Thread(target=invoke, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        return worker, outcome

    def test_initial_snapshot_is_immutable_serializable_and_read_only(self):
        agent = self.agent()
        before = agent.session_events
        forbidden = Mock(side_effect=AssertionError("query attempted execution"))
        with (
            patch.object(agent._tasks, "list", forbidden),
            patch.object(agent._dispatcher, "flush", forbidden),
            patch.object(agent._session, "recover_incomplete", forbidden),
            patch.object(agent._client.chat.completions, "create", forbidden),
        ):
            status = agent.get_status()
            self.assertEqual(status.run_state, "idle")
            self.assertIsNone(status.current_turn)
            self.assertIsNone(status.last_turn)
            self.assertEqual(status.last_event_seq, 0)
            payload = status.model_dump(mode="json")
            payload["task_counts"]["running"] = 999
            self.assertEqual(agent.get_status().task_counts.running, 0)
            self.assertEqual(agent.session_events, before)
        with self.assertRaises(ValidationError):
            status.run_state = "running"
        with self.assertRaises(ValidationError):
            status.task_counts.running = 1
        self.assertNotIn("SECRET", json.dumps(payload))
        forbidden.assert_not_called()

    def test_running_query_then_completed_turn_is_distinct_from_idle(self):
        entered, release = Event(), Event()

        def stream():
            entered.set()
            release.wait(3)
            yield chunk(content="done")

        agent = self.agent([stream()])
        worker, outcome = self.start(lambda: agent.run("SECRET_REQUEST"))
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        status = agent.get_status()
        self.assertEqual((status.run_state, status.mode), ("running", "act"))
        self.assertIsNotNone(status.current_turn)
        self.assertIsNone(status.last_turn)
        self.assertNotIn("SECRET", status.model_dump_json())
        release.set()
        worker.join(3)
        self.assertEqual(outcome, ["done"])
        finished = agent.get_status()
        self.assertEqual(finished.run_state, "idle")
        self.assertIsNone(finished.current_turn)
        self.assertEqual(finished.last_turn.turn_id, status.current_turn.turn_id)
        self.assertEqual(finished.last_turn.kind, "completed")

    def test_model_error_ends_turn_but_agent_can_accept_next_request(self):
        agent = self.agent([RuntimeError("SECRET_ERROR"), reply(content="retry done")])
        with self.assertRaises(AgentRequestError):
            agent.run("first")
        status = agent.get_status()
        self.assertEqual(status.run_state, "idle")
        self.assertEqual(status.last_turn.kind, "error")
        self.assertNotIn("SECRET", status.model_dump_json())
        agent.run("retry")
        self.assertEqual(agent.get_status().last_turn.kind, "completed")

    def test_storage_failure_is_failed_even_after_close(self):
        agent = self.agent()
        with patch.object(agent._session, "append", side_effect=SessionEventError("disk")):
            with self.assertRaises(AgentSessionError):
                agent.run("task")
        self.assertEqual(agent.get_status().run_state, "failed")
        agent.close()
        self.assertEqual(agent.get_status().run_state, "failed")

    def test_approval_callback_observes_wait_and_shutdown_survives_finally(self):
        snapshots = []

        def confirmation(_request):
            snapshots.append(agent.get_status())
            agent.cancel_for_shutdown()
            self.assertEqual(agent.get_status().run_state, "stopped")
            return False

        agent = self.agent([
            reply(calls=[("write", "write_file", '{"path":"x.txt","content":"x"}')]),
        ], confirmation_callback=confirmation)

        def tool(call, **kwargs):
            self.assertFalse(kwargs["confirmation_callback"](object()))
            self.assertEqual(agent.get_status().run_state, "stopped")
            return ToolMessage(tool_call_id=call.id, result=ToolResult(status="denied", error="denied")).model_dump_json()

        with patch("app.agent.call_tool", side_effect=tool):
            with self.assertRaises(AgentSessionError):
                agent.run("write")
        self.assertEqual(snapshots[0].run_state, "waiting_approval")
        self.assertEqual(agent.get_status().run_state, "stopped")
        agent.close()
        self.assertEqual(agent.get_status().run_state, "stopped")

    def test_plan_wait_has_no_durable_turn_until_success(self):
        snapshots = []

        def clarification(_question):
            snapshots.append(agent.get_status())
            return 0

        agent = self.agent([
            reply(content=plan_question_json("scope", "范围？")),
            reply(content=final_plan_json("计划")),
        ], plan_clarification_callback=clarification)
        agent.plan("plan")
        status = snapshots[0]
        self.assertEqual((status.run_state, status.mode), ("waiting_plan", "plan"))
        self.assertIsNone(status.current_turn)
        self.assertEqual(status.last_event_seq, 0)
        self.assertEqual(agent.get_status().last_turn.kind, "completed")

    def test_cancelled_plan_preserves_previous_completed_turn(self):
        agent = self.agent([
            reply(content="done"), reply(content=plan_question_json("scope", "范围？")),
        ], plan_clarification_callback=lambda _question: None)
        agent.run("first")
        previous = agent.get_status().last_turn
        with self.assertRaises(AgentPlanCancelledError):
            agent.plan("cancel")
        self.assertEqual(agent.get_status().run_state, "idle")
        self.assertEqual(agent.get_status().last_turn, previous)

    def test_interrupt_continuation_reports_previous_interrupted_turn(self):
        started, release, reentered, finish = (Event() for _ in range(4))

        def original():
            started.set()
            release.wait(3)
            yield chunk(content="old")

        def continued():
            reentered.set()
            finish.wait(3)
            yield chunk(content="new")

        agent = self.agent([original(), continued()])
        worker, outcome = self.start(lambda: agent.run("first"))
        self.addCleanup(release.set)
        self.addCleanup(finish.set)
        self.assertTrue(started.wait(2))
        agent.interrupt("change")
        release.set()
        self.assertTrue(reentered.wait(2))
        status = agent.get_status()
        self.assertEqual(status.run_state, "running")
        self.assertEqual(status.last_turn.kind, "interrupted")
        self.assertNotEqual(status.current_turn.turn_id, status.last_turn.turn_id)
        finish.set()
        worker.join(3)
        self.assertEqual(outcome, ["new"])

    def test_async_wait_reports_live_task_without_taking_task_lock(self):
        running, release, waiting = Event(), Event(), Event()

        def execute(*_args, **_kwargs):
            running.set()
            release.wait(3)
            return ToolResult(status="success", output="SECRET_RESULT")

        agent = self.agent([
            reply(calls=[("code", "code_interpreter", '{"code":"1+1"}')]),
            reply(content="waiting"), reply(content="done"),
        ])
        original = agent._wait_for_turn_async_tasks

        def wait(*args, **kwargs):
            waiting.set()
            return original(*args, **kwargs)

        with (
            patch("tool.registry._execute", side_effect=execute),
            patch.object(agent, "_wait_for_turn_async_tasks", side_effect=wait),
        ):
            worker, outcome = self.start(lambda: agent.run("calculate"))
            self.addCleanup(release.set)
            self.assertTrue(running.wait(2))
            self.assertTrue(waiting.wait(2))
            # Simulate a task lock held by an unrelated worker/persistence sink.
            with agent._tasks._lock:
                queried = Event()
                snapshots = []

                def query():
                    snapshots.append(agent.get_status())
                    queried.set()

                reader = Thread(target=query, daemon=True)
                reader.start()
                self.assertTrue(queried.wait(1))
            reader.join(1)
            status = snapshots[0]
            self.assertEqual(status.run_state, "waiting_async")
            self.assertEqual(status.task_counts.running, 1)
            self.assertEqual(status.task_counts.active, 1)
            release.set()
            worker.join(3)
        self.assertEqual(outcome, ["done"])
        status = agent.get_status()
        self.assertEqual(status.task_counts.completed, 1)
        self.assertEqual(status.task_counts.active, 0)
        self.assertNotIn("SECRET_RESULT", status.model_dump_json())

    def test_recovered_unknown_task_and_recovery_count_use_committed_facts(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            workflow = SessionEventWorkflow(storage_directory=directory)
            append_until_accepted(workflow, call=ToolCall(
                id="test", name="bash_shell", arguments={"command": "unittest"},
            ))
            workflow.append("async/task/started", AsyncTaskStartedEventData(task_id=TASK))
            live = project_agent_status(workflow.session_id, workflow.events)
            self.assertEqual(live.task_counts.running, 1)
            workflow.close()
            restored = SessionEventWorkflow(storage_directory=directory)
            try:
                status = project_agent_status(restored.session_id, restored.events)
                self.assertEqual(status.task_counts.outcome_unknown, 1)
                self.assertEqual(status.pending_recovery_count, 1)
                self.assertEqual(status.last_turn.kind, "interrupted")
                restored.resolve_tool_outcome("test", applied=False, evidence="verified")
                self.assertEqual(project_agent_status(restored.session_id, restored.events).pending_recovery_count, 0)
            finally:
                restored.close()

    def test_cancelled_task_late_result_never_overwrites_terminal(self):
        workflow = SessionEventWorkflow.in_memory()
        self.addCleanup(workflow.close)
        append_until_accepted(workflow)
        workflow.append("async/task/cancel_requested", AsyncTaskCancelRequestedEventData(task_id=TASK, reason="stop"))
        workflow.append("async/task/cancelled", AsyncTaskCancelledEventData(task_id=TASK, reason="stop"))
        workflow.append("async/task/late_result", AsyncTaskLateResultEventData(
            task_id=TASK, status="completed",
            result=tool_result_snapshot(ToolResult(status="success", output="late")),
        ))
        status = project_agent_status(workflow.session_id, workflow.events)
        self.assertEqual(status.tasks[0].state, "cancelled")
        self.assertEqual(status.task_counts.cancelled, 1)
        self.assertEqual(status.task_counts.completed, 0)

    def test_cli_status_uses_agent_snapshot_and_stays_lazy_before_first_request(self):
        from app import main as main_module
        from tests.model_service_helpers import ready_model_service_patch

        queried = Mock()
        client_factory = Mock(return_value=Mock())

        class StatusAgent:
            max_tool_rounds = 8
            max_plan_clarifications = 5

            def __init__(self, _client, _settings, **kwargs):
                self.workflow = kwargs["session_workflow"]

            def run(self, *_args, **_kwargs):
                return "done"

            def get_status(self):
                queried()
                # Deliberately differs from CLI's idle state to prove the CLI
                # delegates to the public snapshot instead of guessing locally.
                return project_agent_status(
                    self.workflow.session_id, self.workflow.events,
                    phase="waiting_async", mode="act",
                )

            def close(self):
                self.workflow.close()

        with tempfile.TemporaryDirectory() as root:
            inputs = iter(["/status", "go", "/status", "exit"])
            output = io.StringIO()

            def read(_prompt=""):
                line = next(inputs)
                if line == "go":
                    client_factory.assert_not_called()
                    self.assertIn("run_state=idle", output.getvalue())
                return line

            with (
                ready_model_service_patch(),
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "create_client", client_factory),
                patch.object(main_module, "CodingAgent", side_effect=StatusAgent) as agent_factory,
                patch.object(main_module, "MaintenanceService", return_value=Mock()),
                patch.object(main_module, "Observability", return_value=Mock()),
                patch("builtins.input", side_effect=read),
                patch("sys.stdout", output), patch("sys.stderr", output),
            ):
                self.assertEqual(main_module.main(["--new-session", "status", "--plain"]), 0)
            agent_factory.assert_called_once()
            queried.assert_called_once()
            client_factory.assert_called_once()
            rendered = output.getvalue()
            self.assertIn("run_state=waiting_async", rendered)
            self.assertIn("last_turn=none", rendered)
            self.assertIn("tasks_active=0; tasks_total=0; recovery_pending=0", rendered)


if __name__ == "__main__":
    unittest.main()
