from __future__ import annotations

from threading import Event
from time import monotonic
import unittest

from app.async_tasks import (
    AsyncTaskEvent,
    AsyncTaskExecutionResult,
    AsyncTaskManager,
    AsyncTaskRequest,
    AsyncTaskStatus,
    has_started_without_terminal_task,
)
from app.schemas import ToolMessage, ToolResult
from tool.registry import call_tool, get_tool_execution_metadata


class AsyncTaskManagerTests(unittest.TestCase):
    def request(self, call_id: str = "call_1") -> AsyncTaskRequest:
        return AsyncTaskRequest(
            session_id="a" * 32,
            turn_id="b" * 32,
            step=0,
            call_id=call_id,
            tool_name="code_interpreter",
            request_json='{"code":"1+1"}',
            resource_kind="code_interpreter",
        )

    def test_started_without_terminal_predicate_reuses_recovery_projection(self):
        request = self.request()
        task_id = "c" * 32
        accepted = AsyncTaskEvent(
            kind="async/task/accepted",
            task_id=task_id,
            session_id=request.session_id,
            occurred_at="2026-01-01T00:00:00Z",
            turn_id=request.turn_id,
            step=request.step,
            call_id=request.call_id,
            tool_name=request.tool_name,
            request_json=request.request_json,
            retry_safety=request.retry_safety,
            resource_kind=request.resource_kind,
            cancellable=request.cancellable,
        )
        started = AsyncTaskEvent(
            kind="async/task/started",
            task_id=task_id,
            session_id=request.session_id,
            occurred_at="2026-01-01T00:00:01Z",
        )
        completed = AsyncTaskEvent(
            kind="async/task/completed",
            task_id=task_id,
            session_id=request.session_id,
            occurred_at="2026-01-01T00:00:02Z",
            result_status="success",
            result_json='{"output":"done","status":"success"}',
        )
        recovered_unknown = AsyncTaskEvent(
            kind="async/task/failed",
            task_id=task_id,
            session_id=request.session_id,
            occurred_at="2026-01-01T00:00:02Z",
            error="fixed recovery fact",
            recovery_status="outcome_unknown",
        )

        self.assertFalse(has_started_without_terminal_task((accepted,)))
        self.assertTrue(
            has_started_without_terminal_task((accepted, started))
        )
        self.assertFalse(
            has_started_without_terminal_task((accepted, started, completed))
        )
        self.assertFalse(
            has_started_without_terminal_task(
                (accepted, started, recovered_unknown)
            )
        )

    def test_accept_and_start_are_two_distinct_phases(self):
        events = []
        ran = Event()
        manager = AsyncTaskManager(event_sink=events.append)

        def runner(_context):
            ran.set()
            return AsyncTaskExecutionResult.from_tool_result(
                ToolResult(status="success", output="done")
            )

        accepted = manager.accept(self.request(), runner)
        self.assertEqual(accepted.status, AsyncTaskStatus.ACCEPTED)
        self.assertFalse(ran.is_set())
        self.assertEqual([item.kind for item in events], ["async/task/accepted"])
        manager.start(accepted.session_id, accepted.task_id)
        changed = manager.wait_for_change(
            accepted.session_id,
            ((accepted.task_id, AsyncTaskStatus.ACCEPTED),),
            timeout=2,
        )
        if changed[0].status == AsyncTaskStatus.RUNNING:
            changed = manager.wait_for_change(
                accepted.session_id,
                ((accepted.task_id, AsyncTaskStatus.RUNNING),),
                timeout=2,
            )
        self.assertEqual(changed[0].status, AsyncTaskStatus.COMPLETED)
        self.assertEqual(events[0].kind, "async/task/accepted")
        self.assertEqual(events[1].kind, "async/task/started")
        self.assertEqual(events[-1].kind, "async/task/completed")
        manager.shutdown()

    def test_two_long_running_tasks_can_run_in_parallel(self):
        events = []
        first_started = Event()
        second_started = Event()
        release = Event()
        manager = AsyncTaskManager(event_sink=events.append, max_workers=2)

        def runner(started: Event):
            def execute(_context):
                started.set()
                release.wait(2)
                return AsyncTaskExecutionResult.from_tool_result(
                    ToolResult(status="success", output="done")
                )

            return execute

        first = manager.accept(self.request("call_1"), runner(first_started))
        second = manager.accept(self.request("call_2"), runner(second_started))
        manager.start(first.session_id, first.task_id)
        manager.start(second.session_id, second.task_id)
        try:
            self.assertTrue(first_started.wait(2))
            self.assertTrue(second_started.wait(2))
        finally:
            release.set()
            manager.shutdown()

        self.assertEqual(
            manager.query(first.session_id, first.task_id).status,
            AsyncTaskStatus.COMPLETED,
        )
        self.assertEqual(
            manager.query(second.session_id, second.task_id).status,
            AsyncTaskStatus.COMPLETED,
        )

    def test_logical_cancel_is_terminal_and_late_result_is_audit_only(self):
        events = []
        release = Event()
        manager = AsyncTaskManager(event_sink=events.append)

        def runner(_context):
            release.wait(2)
            return AsyncTaskExecutionResult.from_tool_result(
                ToolResult(status="success", output="late")
            )

        accepted = manager.accept(self.request(), runner)
        manager.start(accepted.session_id, accepted.task_id)
        manager.wait_for_change(
            accepted.session_id,
            ((accepted.task_id, AsyncTaskStatus.ACCEPTED),),
            timeout=2,
        )
        cancelled = manager.cancel(accepted.session_id, accepted.task_id, reason="stop")
        self.assertEqual(cancelled.status, AsyncTaskStatus.CANCELLED)
        release.set()
        manager.shutdown()
        self.assertEqual(manager.query(accepted.session_id, accepted.task_id).status, AsyncTaskStatus.CANCELLED)
        self.assertIn("async/task/late_result", [item.kind for item in events])

    def test_registry_metadata_and_session_bound_control_tools(self):
        self.assertTrue(get_tool_execution_metadata(
            "bash_shell", {"command": "compileall", "paths": ["app"]}
        ).run_async)
        self.assertFalse(get_tool_execution_metadata(
            "bash_shell", {"command": "remove_file", "paths": ["x.txt"]}
        ).run_async)
        manager = AsyncTaskManager(event_sink=lambda _event: None)
        accepted = manager.accept(
            self.request(),
            lambda _context: AsyncTaskExecutionResult.from_tool_result(
                ToolResult(status="success", output="done")
            ),
        )
        raw = call_tool(
            {"id": "query", "name": "get_async_task", "arguments": {"task_id": accepted.task_id}},
            async_task_manager=manager,
            session_id=accepted.session_id,
        )
        queried = ToolMessage.model_validate_json(raw)
        self.assertEqual(queried.result.output.state, "accepted")
        denied_raw = call_tool(
            {"id": "query2", "name": "get_async_task", "arguments": {"task_id": accepted.task_id}},
            async_task_manager=manager,
            session_id="f" * 32,
        )
        self.assertEqual(ToolMessage.model_validate_json(denied_raw).result.status, "denied")
        cancelled_raw = call_tool(
            {"id": "cancel", "name": "cancel_async_task", "arguments": {"task_id": accepted.task_id}},
            async_task_manager=manager,
            session_id=accepted.session_id,
        )
        self.assertEqual(ToolMessage.model_validate_json(cancelled_raw).result.output.state, "cancelled")
        manager.shutdown()

    def test_emergency_shutdown_is_bounded_when_started_event_sink_is_stuck(self):
        sink_entered = Event()
        release_sink = Event()
        ran = Event()

        def sink(event):
            if event.kind == "async/task/started":
                sink_entered.set()
                release_sink.wait(2)

        manager = AsyncTaskManager(event_sink=sink)

        def runner(_context):
            ran.set()
            return AsyncTaskExecutionResult.from_tool_result(
                ToolResult(status="success", output="done")
            )

        accepted = manager.accept(
            self.request(),
            runner,
        )
        manager.start(accepted.session_id, accepted.task_id)
        self.assertTrue(sink_entered.wait(1))
        started_at = monotonic()
        try:
            manager.shutdown(wait=False)
        finally:
            elapsed = monotonic() - started_at
            release_sink.set()
        self.assertLess(elapsed, 0.3)
        self.assertFalse(ran.wait(0.2))


if __name__ == "__main__":
    unittest.main()
