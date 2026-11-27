"""A model outage must not manufacture terminal states for live tool jobs."""

from threading import Event
import unittest
from unittest.mock import Mock, patch

from app.agent import AgentRequestError, CodingAgent
from app.async_tasks import AsyncTaskExecutionResult, AsyncTaskStatus
from app.config import Settings
from app.observability import Observability
from app.schemas import ToolCall, ToolResult
from app.workflow import SessionEventWorkflow
from tests.test_agent import FakeClient, reply
from tool.registry import PreparedAsyncToolCall, ToolExecutionMetadata


class ModelFailureAsyncTests(unittest.TestCase):
    def test_running_tool_keeps_its_real_terminal_after_model_failure(self):
        started = Event()
        release = Event()
        session = SessionEventWorkflow.in_memory()

        def runner(_context):
            started.set()
            if not release.wait(3):
                raise RuntimeError("test runner release timed out")
            return AsyncTaskExecutionResult.from_tool_result(
                ToolResult(status="success", output="actual completed result")
            )

        def outage():
            self.assertTrue(started.wait(2))
            raise RuntimeError("synthetic backend failure")
            yield  # Make this a failing streaming response.

        call = ToolCall(id="running-call", name="code_interpreter", arguments={"code": "1+1"})
        prepared = PreparedAsyncToolCall(
            call=call,
            metadata=ToolExecutionMetadata(True, "code_interpreter", True),
            arguments_json='{"code":"1+1"}',
            runner=runner,
            cancel_hook=release.set,
        )
        client = FakeClient([
            reply(calls=[(call.id, call.name, '{"code":"1+1"}')]),
            outage(),
            reply(content="new task only"),
        ])
        with patch("app.agent.build_skill_catalog_message", return_value=None):
            agent = CodingAgent(
                client, Settings(api_key="fake"), system_prompt="Test agent.",
                session_workflow=session, observability=Mock(spec=Observability),
            )
        try:
            with patch("app.agent.prepare_async_tool_call", return_value=prepared) as prepare:
                with self.assertRaises(AgentRequestError):
                    agent.run("old-failed-command-sentinel")
                task = agent.list_async_tasks()[0]
                self.assertEqual(task.status, AsyncTaskStatus.RUNNING)
                self.assertFalse(any(event.type == "async/task/failed" for event in session.events))
                self.assertEqual(len(client.completions.requests), 2)

                release.set()
                changed = agent._tasks.wait_for_change(
                    session.session_id, ((task.task_id, AsyncTaskStatus.RUNNING),), timeout=3,
                )
                self.assertEqual(changed[0].status, AsyncTaskStatus.COMPLETED)
                agent._dispatcher.flush()
                terminals = [
                    event for event in session.events
                    if event.type in {"async/task/completed", "async/task/failed"}
                ]
                self.assertEqual([event.type for event in terminals], ["async/task/completed"])
                self.assertEqual(agent.run("new full request"), "new task only")
                self.assertEqual(prepare.call_count, 1)
                messages = client.completions.requests[-1]["messages"]
                self.assertNotIn("old-failed-command-sentinel", str(messages))
                self.assertIn("actual completed result", str(messages))
        finally:
            release.set()
            agent.close()


if __name__ == "__main__":
    unittest.main()
