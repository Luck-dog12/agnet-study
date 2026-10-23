"""Physical process-tree cancellation protocol tests."""

from __future__ import annotations

import os
import subprocess
import sys
from time import monotonic
import unittest
from unittest.mock import patch

from app.async_tasks import AsyncTaskManager, AsyncTaskRequest, AsyncTaskStatus
from app.schemas import ToolCall
from tool.process_cancel import ProcessCancellationHandle, WindowsJob
from tool.registry import prepare_async_tool_call


class ProcessCancellationTests(unittest.TestCase):
    def _sleeping_process(self) -> tuple[subprocess.Popen[str], WindowsJob | None]:
        options: dict[str, object] = {}
        job: WindowsJob | None = None
        if os.name == "nt":
            job = WindowsJob()
            options["creationflags"] = 0x08000000
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            shell=False,
            **options,
        )
        if job is not None:
            job.assign(process)
        return process, job

    def test_cancel_before_bind_is_sticky_and_kills_new_process(self):
        handle = ProcessCancellationHandle()
        handle.cancel()
        process, job = self._sleeping_process()
        try:
            handle.bind(process, windows_job=job)
            self.assertIsNotNone(process.wait(timeout=5))
            self.assertTrue(handle.cancellation_requested)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            handle.release(process)
            if job is not None:
                job.close()

    def test_cancel_after_bind_kills_running_process(self):
        handle = ProcessCancellationHandle()
        process, job = self._sleeping_process()
        try:
            handle.bind(process, windows_job=job)
            handle.cancel()
            self.assertIsNotNone(process.wait(timeout=5))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            handle.release(process)
            if job is not None:
                job.close()

    def test_prepared_runner_cancel_before_start_never_spawns(self):
        prepared = prepare_async_tool_call(ToolCall(
            id="cancel_before_start",
            name="code_interpreter",
            arguments={"code": "1 + 1"},
        ))
        prepared.cancel_hook()

        class Context:
            def report_progress(self, **_values: object) -> bool:
                return True

        with patch(
            "tool.code_interpreter.subprocess.Popen",
            side_effect=AssertionError("取消先于绑定时不得启动进程"),
        ) as popen:
            result = prepared.runner(Context())  # type: ignore[arg-type]
        self.assertEqual(result.status, "error")
        popen.assert_not_called()

    def test_task_manager_keeps_cancelled_terminal_while_process_stops(self):
        events = []
        manager = AsyncTaskManager(event_sink=events.append)
        prepared = prepare_async_tool_call(ToolCall(
            id="physical_cancel",
            name="code_interpreter",
            arguments={
                "code": "for item in range(10 ** 100):\n    pass",
                "timeout_seconds": 10,
            },
        ))
        request = AsyncTaskRequest(
            session_id="a" * 32,
            turn_id="b" * 32,
            step=0,
            call_id="physical_cancel",
            tool_name="code_interpreter",
            request_json=prepared.arguments_json,
            resource_kind=prepared.metadata.resource_kind,
            cancellable=True,
        )
        accepted = manager.accept(
            request, prepared.runner, cancel_hook=prepared.cancel_hook
        )
        manager.start(accepted.session_id, accepted.task_id)
        manager.wait_for_change(
            accepted.session_id,
            ((accepted.task_id, AsyncTaskStatus.ACCEPTED),),
            timeout=2,
        )
        started = monotonic()
        snapshot = manager.cancel(
            accepted.session_id, accepted.task_id, reason="test cancellation"
        )
        self.assertEqual(snapshot.status, AsyncTaskStatus.CANCELLED)
        manager.shutdown(wait=True)
        self.assertLess(monotonic() - started, 5)
        self.assertEqual(
            manager.query(accepted.session_id, accepted.task_id).status,
            AsyncTaskStatus.CANCELLED,
        )
        self.assertIn("async/task/cancelled", [event.kind for event in events])


if __name__ == "__main__":
    unittest.main()
