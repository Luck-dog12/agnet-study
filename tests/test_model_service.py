"""Offline availability probe, automatic recovery and input-boundary tests."""

from contextlib import ExitStack
import io
from pathlib import Path
from queue import Queue
import tempfile
from threading import Event
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.model_guard import ModelServiceMonitor, probe_model_service


class ModelProbeTests(unittest.TestCase):
    def test_probe_uses_configured_model_and_bounded_inference_without_tools(self):
        settings = Settings(api_key="offline-key", model="configured-model", max_retries=4)
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(role="assistant", content="OK", tool_calls=None)),
        ])
        with patch("app.model_guard.create_client", return_value=client) as create:
            self.assertTrue(probe_model_service(settings))
        bounded = create.call_args.args[0]
        self.assertEqual(bounded.base_url, settings.base_url)
        self.assertEqual(bounded.api_key, settings.api_key)
        self.assertEqual((bounded.timeout_seconds, bounded.max_retries), (10.0, 0))
        self.assertEqual(settings.max_retries, 4)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], settings.model)
        self.assertEqual(len(request["messages"]), 1)
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertFalse(request["stream"])
        self.assertLessEqual(request["max_tokens"], 16)
        self.assertNotIn("tools", request)
        client.models.list.assert_not_called()
        client.close.assert_called_once()

    def test_probe_failure_or_invalid_response_is_false_and_closes_client(self):
        for response in (
            RuntimeError("private-provider-detail"), {"choices": []},
            {"choices": [{"message": {"role": "assistant", "content": " "}}]},
            {"choices": [{"message": {"role": "assistant", "content": "OK", "tool_calls": [1]}}]},
        ):
            with self.subTest(response_type=type(response).__name__):
                client = Mock()
                if isinstance(response, Exception):
                    client.chat.completions.create.side_effect = response
                else:
                    client.chat.completions.create.return_value = response
                with patch("app.model_guard.create_client", return_value=client):
                    self.assertFalse(probe_model_service(Settings(api_key="offline")))
                client.close.assert_called_once()


class ModelMonitorTests(unittest.TestCase):
    def test_failed_startup_recovers_automatically_and_healthy_state_does_not_poll(self):
        unavailable, ready = Event(), Event()
        states = []
        calls = []

        def probe(_settings):
            calls.append(True)
            return len(calls) > 1

        def changed(state):
            states.append(state)
            if state == "unavailable":
                unavailable.set()
            elif state == "ready":
                ready.set()

        monitor = ModelServiceMonitor(
            Settings(api_key="offline"), on_change=changed, probe=probe, retry_interval=0.02,
        )
        try:
            monitor.start()
            self.assertTrue(unavailable.wait(2))
            self.assertTrue(ready.wait(2))
            self.assertEqual(states, ["checking", "unavailable", "ready"])
            Event().wait(0.08)  # several retry intervals; ready must not probe
            self.assertEqual(len(calls), 2)
            monitor.mark_unavailable()
            ready.clear()
            self.assertTrue(ready.wait(2))
            self.assertEqual(len(calls), 3)
        finally:
            monitor.close()

    def test_old_probe_success_cannot_override_a_newer_runtime_failure(self):
        entered = [Event(), Event()]
        releases = [Event(), Event()]
        ready, calls = Event(), []

        def probe(_settings):
            index = len(calls)
            calls.append(True)
            entered[index].set()
            if not releases[index].wait(3):
                raise AssertionError("probe not released")
            return True

        monitor = ModelServiceMonitor(
            Settings(api_key="offline"), probe=probe, retry_interval=0.01,
            on_change=lambda state: ready.set() if state == "ready" else None,
        )
        try:
            monitor.start()
            self.assertTrue(entered[0].wait(2))
            monitor.mark_unavailable()
            releases[0].set()
            self.assertTrue(entered[1].wait(2))
            self.assertEqual(monitor.state, "unavailable")
            self.assertFalse(ready.is_set())
            releases[1].set()
            self.assertTrue(ready.wait(2))
        finally:
            for release in releases:
                release.set()
            monitor.close()

    def test_close_never_waits_for_a_blocked_probe_or_publishes_its_late_result(self):
        entered, release = Event(), Event()
        states = []

        def probe(_settings):
            entered.set()
            release.wait(3)
            return True

        monitor = ModelServiceMonitor(Settings(api_key="offline"), probe=probe, on_change=states.append)
        try:
            monitor.start()
            self.assertTrue(entered.wait(2))
            start = monotonic()
            monitor.close()
            self.assertLess(monotonic() - start, 1)
            release.set()
            monitor._thread.join(2)
            self.assertFalse(monitor._thread.is_alive())
            self.assertEqual(states, ["checking"])
        finally:
            release.set()
            monitor.close()


class BrokerAvailabilityTests(unittest.TestCase):
    def test_input_submitted_while_unavailable_never_survives_recovery(self):
        from app.main import _INPUT_DISCARDED, _InputBroker

        delivered = Event()
        broker = _InputBroker(reader=lambda *_args: "MUST_BE_DROPPED")
        original_put = broker._lines.put

        def put(line, *args, **kwargs):
            self.assertIs(line.value, _INPUT_DISCARDED)
            original_put(line, *args, **kwargs)
            delivered.set()

        broker.set_model_available(False)
        with patch.object(broker._lines, "put", side_effect=put):
            broker.start()
            try:
                broker.request_line()
                self.assertTrue(delivered.wait(2))
                broker.set_model_available(True)
                self.assertIs(broker.get_line(timeout=2), _INPUT_DISCARDED)
            finally:
                broker.close()

    def test_first_new_submission_on_an_older_reader_is_accepted_after_recovery(self):
        from app.main import _InputBroker

        entered = Event()
        incoming = Queue()

        def reader(*_args):
            entered.set()
            return incoming.get(timeout=3)

        broker = _InputBroker(reader=reader)
        broker.set_model_available(False)
        broker.start()
        try:
            broker.request_line()
            self.assertTrue(entered.wait(2))
            broker.set_model_available(True)
            incoming.put("FIRST_FRESH_REQUEST")
            self.assertEqual(broker.get_line(timeout=2), "FIRST_FRESH_REQUEST")
        finally:
            incoming.put("exit")
            broker.close()


class StartupCliTests(unittest.TestCase):
    def test_exit_during_check_does_not_wait_or_create_user_events_or_runtime(self):
        from app import main as main_module
        from app.workflow import SessionEventWorkflow

        entered, release = Event(), Event()
        monitors = []

        def probe(_settings):
            entered.set()
            release.wait(5)
            return True

        def factory(settings, *, on_change):
            monitor = ModelServiceMonitor(settings, on_change=on_change, probe=probe)
            monitors.append(monitor)
            return monitor

        def reader(*_args):
            self.assertTrue(entered.wait(2))
            return "exit"

        with tempfile.TemporaryDirectory() as root, ExitStack() as stack:
            sessions = Path(root) / "sessions"
            for context in (
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="offline")),
                patch.object(main_module, "create_model_service_monitor", side_effect=factory),
                patch("builtins.input", side_effect=reader),
                patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()),
            ):
                stack.enter_context(context)
            client = stack.enter_context(patch.object(main_module, "create_client"))
            runtime = stack.enter_context(patch.object(main_module, "create_execution_runtime"))
            agent = stack.enter_context(patch.object(main_module, "CodingAgent"))
            try:
                start = monotonic()
                self.assertEqual(main_module.main([
                    "--new-session", "check-exit", "--plain", "--execution-backend", "local",
                ]), 0)
                self.assertLess(monotonic() - start, 2)
                client.assert_not_called()
                runtime.assert_not_called()
                agent.assert_not_called()
                with SessionEventWorkflow(storage_directory=sessions / "check-exit") as workflow:
                    self.assertEqual(workflow.events, ())
            finally:
                release.set()
                for monitor in monitors:
                    monitor.close()
                    monitor._thread.join(2)

    def test_help_and_session_listing_do_not_start_a_model_check(self):
        from app import main as main_module

        with tempfile.TemporaryDirectory() as root, \
                patch.object(main_module, "_SESSIONS_ROOT", Path(root)), \
                patch.object(main_module, "create_model_service_monitor") as factory, \
                patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                main_module.main(["--help"])
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(main_module.main(["--list-sessions"]), 0)
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
