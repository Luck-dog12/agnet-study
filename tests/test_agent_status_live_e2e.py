"""Explicitly gated status checks using a real provider and local Python tool.

RUN_AGENT_STATUS_LIVE_E2E=1 enables three bounded tests. The transparent client
adapter limits tokens, request count and advertised tools; provider responses
and the permitted tool result are never fabricated. Unexpected tools or code
are rejected before dispatch. No cloud execution backend is constructed.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from app.agent import AgentSessionError, CodingAgent
from app.agent_status import project_agent_status
from app.client import create_client
from app.config import load_settings
from app.observability import Observability
from app.schemas import CodeInterpreterArguments, ToolCall
from app.workflow import SessionEventWorkflow
from tool.registry import prepare_async_tool_call


_CODE = "sum(range(10))"
_REPLY = "STATUS_LIVE_OK"
_PROMPT_MARKER = "status-live-private-request-marker"


class _BoundedLiveClient:
    """Forward unchanged real streams with explicit test-only request bounds."""

    def __init__(self, client: Any, *, tools: bool, request_limit: int) -> None:
        self._client = client
        self._tools = tools
        self._request_limit = request_limit
        self.request_count = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> Any:
        if self.request_count >= self._request_limit:
            raise RuntimeError("Live status test request limit reached.")
        self.request_count += 1
        request = dict(kwargs)
        advertised = request.pop("tools", ())
        request.pop("tool_choice", None)
        if self._tools and self.request_count == 1:
            request["tools"] = [
                item for item in advertised
                if item["function"]["name"] == "code_interpreter"
            ]
            request["tool_choice"] = {
                "type": "function", "function": {"name": "code_interpreter"},
            }
        request.update(
            max_tokens=512,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return self._client.chat.completions.create(**request)


@unittest.skipUnless(
    os.environ.get("RUN_AGENT_STATUS_LIVE_E2E") == "1",
    "Set RUN_AGENT_STATUS_LIVE_E2E=1 to run real provider status tests.",
)
class AgentStatusLiveE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.started_at = monotonic()
        try:
            self.settings = replace(load_settings(), timeout_seconds=60.0, max_retries=0)
            self.client = create_client(self.settings)
        except Exception as error:
            raise AssertionError(
                f"Live status setup failed: {type(error).__name__}"
            ) from None
        self.addCleanup(self._close_client)
        project = Path(self.enterContext(TemporaryDirectory(prefix="agent-status-live-")))
        self.enterContext(patch("app.observability._PROJECT_ROOT", project))
        self.enterContext(patch("app.observability._LOG_DIRECTORY", project / "data" / "log"))
        # This guard never supplies a fake result: unexpected calls abort before
        # registry dispatch, while the one allowed calculation uses the real tool.
        self.prepared_count = 0
        self.enterContext(patch("app.agent.call_tool", self._reject_sync_tool))
        self.enterContext(patch("app.agent.prepare_async_tool_call", self._prepare_calculation))

    def _close_client(self) -> None:
        try:
            self.client.close()
        except Exception as error:
            raise AssertionError(
                f"Live client cleanup failed: {type(error).__name__}"
            ) from None

    @staticmethod
    def _reject_sync_tool(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Live status test rejects synchronous tools.")

    def _prepare_calculation(self, call: ToolCall, **kwargs: Any) -> Any:
        if call.name != "code_interpreter" or self.prepared_count != 0:
            raise RuntimeError("Live status test rejects unapproved tool dispatch.")
        arguments = CodeInterpreterArguments.model_validate(call.arguments)
        if arguments.code.strip() != _CODE or arguments.timeout_seconds > 5:
            raise RuntimeError("Live status test rejects unapproved calculation.")
        self.prepared_count += 1
        return prepare_async_tool_call(call, **kwargs)

    def _agent(self, *, calculation: bool = False) -> CodingAgent:
        self.proxy = _BoundedLiveClient(
            self.client, tools=calculation, request_limit=3 if calculation else 1,
        )
        system = (
            "This is a bounded live integration test. Ignore unrelated skill catalogs. "
            f"Call code_interpreter exactly once with code exactly {json.dumps(_CODE)} "
            "and timeout_seconds=5. Do not use any other tool or any other code. "
            "After the calculation has completed, report its result briefly."
            if calculation else
            "This is a bounded live integration test. Do not call tools or load skills. "
            "Follow only the requested plain text response format."
        )
        agent = CodingAgent(
            self.proxy, self.settings, system_prompt=system,
            session_workflow=SessionEventWorkflow.in_memory(),
            observability=Observability(), max_tool_rounds=1 if calculation else 0,
        )
        # Emergency close is bounded even if a failed live request never returns.
        self.addCleanup(agent.close, wait=False)
        return agent

    def _run(
        self, agent: CodingAgent, prompt: str, *, on_text_delta: Any = None,
    ) -> tuple[object, list[str]]:
        outcome: list[object] = []
        finished = Event()

        def invoke() -> None:
            try:
                outcome.append(agent.run(prompt, on_text_delta=on_text_delta))
            except BaseException as error:
                # Retain only a safe classification, never a provider exception.
                outcome.append(type(error))
            finally:
                finished.set()

        worker = Thread(target=invoke, name="live-status-request", daemon=True)
        worker.start()
        observed: list[str] = []
        deadline = monotonic() + 195.0
        while not finished.wait(0.02):
            state = agent.get_status().run_state
            if not observed or observed[-1] != state:
                observed.append(state)
            if monotonic() >= deadline:
                agent.cancel_for_shutdown()
                self.fail("Live status request exceeded its bounded deadline.")
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive(), "Live request worker failed to exit.")
        self.assertEqual(len(outcome), 1)
        return outcome[0], observed

    def _assert_success(self, outcome: object) -> str:
        if isinstance(outcome, type):
            self.fail(f"Live Agent request failed: {outcome.__name__}")
        self.assertTrue(isinstance(outcome, str) and bool(outcome.strip()), "Missing final text.")
        return outcome

    def _assert_no_content(self, agent: CodingAgent, response: str = "") -> None:
        serialized = json.dumps(agent.get_status().model_dump(mode="json"))
        for sensitive in (self.settings.api_key, _PROMPT_MARKER, _REPLY, response):
            if sensitive:
                self.assertTrue(sensitive not in serialized, "Status exposed private content.")

    def _evidence(self, agent: CodingAgent, observed: list[str], **extra: Any) -> None:
        status = agent.get_status()
        print(json.dumps({
            "test": self._testMethodName,
            "model": self.settings.model,
            "request_count": self.proxy.request_count,
            "observed_run_states": observed,
            "final_run_state": status.run_state,
            "last_turn": None if status.last_turn is None else status.last_turn.kind,
            "task_counts": status.task_counts.model_dump(mode="json"),
            "last_event_seq": status.last_event_seq,
            "elapsed_seconds": round(monotonic() - self.started_at, 3),
            **extra,
        }, ensure_ascii=True, sort_keys=True), flush=True)

    def test_real_stream_running_completed_and_closed(self) -> None:
        agent = self._agent()
        initial = agent.get_status()
        self.assertEqual(initial.run_state, "idle")
        self.assertEqual(initial.last_event_seq, 0)
        streamed: list[str] = []

        def delta(_text: str) -> None:
            status = agent.get_status()
            streamed.append(status.run_state)
            self.assertEqual(status.run_state, "running")
            self.assertIsNotNone(status.current_turn)

        outcome, observed = self._run(
            agent, f"Reply with {_REPLY} only. Request marker: {_PROMPT_MARKER}",
            on_text_delta=delta,
        )
        response = self._assert_success(outcome)
        self.assertTrue(streamed, "No real text delta was received.")
        status = agent.get_status()
        self.assertEqual(status.run_state, "idle")
        self.assertIsNone(status.current_turn)
        self.assertEqual(status.last_turn.kind, "completed")
        self.assertEqual(status.task_counts.total, 0)
        self._assert_no_content(agent, response)
        self.assertEqual(self.proxy.request_count, 1)
        agent.close()
        self.assertEqual(agent.get_status().run_state, "stopped")
        self._evidence(agent, ["idle", *observed, "idle", "stopped"], real_text_delta_count=len(streamed))

    def test_real_stream_active_shutdown_remains_stopped(self) -> None:
        agent = self._agent()
        stopped_on_delta = Event()

        def stop(_text: str) -> None:
            if not stopped_on_delta.is_set():
                self.assertEqual(agent.get_status().run_state, "running")
                agent.cancel_for_shutdown()
                self.assertEqual(agent.get_status().run_state, "stopped")
                stopped_on_delta.set()

        outcome, observed = self._run(
            agent, "Write a numbered list of 80 short positive integers in words, one per line.",
            on_text_delta=stop,
        )
        self.assertTrue(stopped_on_delta.is_set(), "No real delta reached the stop callback.")
        self.assertIs(outcome, AgentSessionError, "Stopped request did not leave via its session fence.")
        self.assertEqual(agent.get_status().run_state, "stopped")
        self.assertEqual(self.proxy.request_count, 1, "Shutdown unexpectedly invoked the model again.")
        self.assertIsNone(agent.get_status().last_turn)
        self._assert_no_content(agent)
        agent.close()
        self.assertEqual(agent.get_status().run_state, "stopped")
        self._evidence(agent, ["idle", *observed, "stopped"], stopped_on_real_delta=True)

    def test_real_local_calculation_task_lifecycle(self) -> None:
        agent = self._agent(calculation=True)
        outcome, observed = self._run(
            agent, f"Use the specified code_interpreter calculation once: {_CODE}. Then report the result.",
        )
        self._assert_success(outcome)
        self.assertEqual(self.prepared_count, 1)
        status = agent.get_status()
        self.assertEqual(status.run_state, "idle")
        self.assertEqual(status.last_turn.kind, "completed")
        self.assertEqual(status.task_counts.total, 1)
        self.assertEqual(status.task_counts.completed, 1)
        self.assertEqual(status.task_counts.active, 0)
        events = agent.session_events
        prefixes: list[str] = []
        for index, event in enumerate(events, 1):
            if event.type in {"async/task/accepted", "async/task/started", "async/task/completed"}:
                projected = project_agent_status(status.session_id, events[:index])
                prefixes.append(projected.tasks[0].state)
            if event.type == "async/task/completed":
                actual = json.loads(event.data.result.result_json)
                self.assertTrue(actual["output"]["result"] == "45", "Real calculation returned the wrong result.")
        self.assertEqual(prefixes, ["accepted", "running", "completed"])
        self._assert_no_content(agent)
        agent.close()
        self._evidence(
            agent, ["idle", *observed, "idle", "stopped"],
            committed_task_prefix_states=prefixes,
            waiting_async_observed="waiting_async" in observed,
            real_calculation_verified=True,
        )


if __name__ == "__main__":
    unittest.main()
