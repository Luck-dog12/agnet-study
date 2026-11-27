"""Stage 5B3a Agent/runtime binding and lifecycle tests (offline only)."""

from __future__ import annotations

import hashlib
import json
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import SecretStr

from app.agent import AgentProtocolError, AgentSessionError, CodingAgent
from app.config import Settings
from app.execution_backend import (
    AliyunSandboxExecutionBackend as _AliyunSandboxExecutionBackend,
)
from tests.sandbox_cleanup_fakes import empty_recovery_provider


def AliyunSandboxExecutionBackend(*args, **kwargs):
    kwargs.setdefault("automatic_maintenance", False)
    kwargs.setdefault("recovery_provider", empty_recovery_provider())
    if "sandbox_factory" in kwargs and "persistence_sandbox_factory" not in kwargs:
        class PersistenceSandbox:
            sandbox_id = "agent-runtime-persistence-sandbox"

            def kill(self):
                return True

        kwargs["persistence_sandbox_factory"] = (
            lambda _settings, _metadata: PersistenceSandbox()
        )
    if "transport_factory" in kwargs and "persistence_transport_factory" not in kwargs:
        original = kwargs["transport_factory"]
        cached = {}

        def execution_transport(candidate):
            cached["value"] = original(candidate)
            return cached["value"]

        kwargs["transport_factory"] = execution_transport
        kwargs["persistence_transport_factory"] = lambda _candidate: cached["value"]
    return _AliyunSandboxExecutionBackend(*args, **kwargs)
from app.execution_runtime import ExecutionRuntimeClosedError, ExecutionToolRuntime
from app.sandbox_config import SandboxSettings
from app.sandbox_transport import (
    SandboxCommandResult,
    SandboxFileEntry,
    SandboxGrepMatch,
    SandboxGrepResult,
    SandboxListResult,
    SandboxPersistenceEntry,
    SandboxPersistenceListResult,
    SandboxReadResult,
    SandboxWriteResult,
)
from app.schemas import ReadFileArguments, TurnStartEventData
from app.workspace_seed import (
    WorkspaceSeedEntry,
    WorkspaceSeedSnapshot,
    create_workspace_seed_initializer,
)
from app.workflow import SessionEventError, SessionEventWorkflow


def _chunk(*, content=None, calls=None):
    tool_calls = [
        SimpleNamespace(
            index=index,
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for index, (call_id, name, arguments) in enumerate(calls or ())
    ]
    delta = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(index=0, delta=delta)])


def _reply(*, content=None, calls=None):
    return [_chunk(content=content, calls=calls)]


class _FakeCompletions:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses=()) -> None:
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _settings() -> Settings:
    return Settings(api_key="fake-key", model="fake-model")


def _sandbox_settings() -> SandboxSettings:
    return SandboxSettings(
        enabled=True,
        api_key=SecretStr("agent-runtime-test-key"),
        api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        domain="cn-hangzhou.e2b.fc.aliyuncs.com",
        template="code-interpreter-v1",
        timeout_seconds=300,
        oss_bucket="agent-probe-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket_path="/projects/test/runs/agent-runtime",
        oss_mount_dir="/mnt/oss",
        oss_role_arn="acs:ram::1234567890123456:role/agent-runtime-test",
        oss_read_only=False,
    )


class _FakeSandbox:
    sandbox_id = "agent-runtime-sandbox"

    def __init__(self, *, kill_error: bool = False, block_kill: bool = False) -> None:
        self.kill_calls = 0
        self.kill_error = kill_error
        self.block_kill = block_kill
        self.kill_started = Event()
        self.kill_release = Event()
        self.kill_finished = Event()
        self.on_kill = None

    def kill(self) -> bool:
        self.kill_calls += 1
        if self.on_kill is not None:
            self.on_kill()
        self.kill_started.set()
        if self.block_kill:
            self.kill_release.wait(2)
        try:
            if self.kill_error:
                raise OSError("provider secret detail")
            return True
        finally:
            self.kill_finished.set()


class _ReadOnlyTransport:
    def __init__(self, session_id: str) -> None:
        self.root = f"/workspace/agnet-study/{session_id}"
        self.calls: list[str] = []

    class _Handle:
        def __init__(self, result: SandboxCommandResult) -> None:
            self._result = result
            self.cancel_calls = 0

        def wait(self) -> SandboxCommandResult:
            return self._result

        def cancel(self) -> None:
            self.cancel_calls += 1

    def start_run(
        self,
        command,
        *,
        paths,
        code,
        cwd,
        timeout_seconds,
        max_output_bytes,
        max_output_characters=None,
        max_memory_mb=None,
        max_processes=None,
        dispatch_attempt_consumer=None,
    ):
        del (
            paths,
            cwd,
            timeout_seconds,
            max_output_bytes,
            max_output_characters,
            max_memory_mb,
            max_processes,
        )
        if dispatch_attempt_consumer is None:
            raise AssertionError("missing dispatch boundary")
        dispatch_attempt_consumer()
        self.calls.append("code_interpreter" if command == "python" else command)
        return self._Handle(
            SandboxCommandResult(
                exit_code=0,
                stdout="",
                stderr="",
                result="2" if code is not None else "",
                truncated=False,
            )
        )

    def read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del max_bytes, timeout_seconds
        self.calls.append("read_file")
        data = b"needle"
        return SandboxReadResult(data[offset:], len(data))

    def list_files(
        self,
        pattern,
        *,
        workspace_root,
        max_results,
        timeout_seconds,
        path_predicate,
    ):
        del max_results, timeout_seconds
        self.calls.append("list_files")
        assert pattern == "README.md"
        assert workspace_root == self.root
        assert path_predicate("README.md", False) is True
        return SandboxListResult(
            (SandboxFileEntry(f"{self.root}/README.md", 6),),
            False,
        )

    def grep(
        self,
        pattern,
        *,
        workspace_root,
        file_pattern,
        case_sensitive,
        max_results,
        timeout_seconds,
        path_predicate,
    ):
        del max_results, timeout_seconds
        self.calls.append("grep")
        assert (pattern, file_pattern, case_sensitive) == (
            "needle",
            "README.md",
            True,
        )
        assert workspace_root == self.root
        assert path_predicate("README.md", False) is True
        return SandboxGrepResult(
            (SandboxGrepMatch(f"{self.root}/README.md", 1, 1, "needle"),),
            1,
            False,
        )


class _SeedTransport:
    def __init__(self, session_id: str) -> None:
        self.root = f"/workspace/agnet-study/{session_id}"
        self.files: dict[str, bytes] = {}
        self.persistence: dict[str, bytes] = {}
        self.persistence_dirs: set[str] = set()

    def write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        if overwrite or path in self.files:
            raise FileExistsError
        self.files[path] = data
        return SandboxWriteResult(len(data))

    def read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del timeout_seconds
        data = self.files[path]
        return SandboxReadResult(data[offset : offset + max_bytes], len(data))

    def list_files(
        self,
        pattern,
        *,
        workspace_root,
        max_results,
        timeout_seconds,
        path_predicate,
    ):
        del pattern, timeout_seconds
        assert workspace_root == self.root
        entries = tuple(
            SandboxFileEntry(path, len(data))
            for path, data in sorted(self.files.items())
            if path_predicate(path[len(self.root) + 1 :], False)
        )
        return SandboxListResult(entries[:max_results], len(entries) > max_results)

    def persistence_write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        if overwrite or path in self.persistence:
            raise FileExistsError
        self.persistence[path] = data
        checkpoint_root = path.split("/files/", 1)[0]
        if path.endswith("/manifest.json"):
            checkpoint_root = path.rsplit("/", 1)[0]
        parent = path.rsplit("/", 1)[0]
        while parent.startswith(checkpoint_root + "/"):
            self.persistence_dirs.add(parent)
            parent = parent.rsplit("/", 1)[0]
        return SandboxWriteResult(len(data))

    def persistence_read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del timeout_seconds
        data = self.persistence[path]
        return SandboxReadResult(data[offset : offset + max_bytes], len(data))

    def persistence_list_files(
        self,
        checkpoint_root,
        *,
        max_results,
        max_depth,
        timeout_seconds,
    ):
        del max_depth, timeout_seconds
        entries = [
            SandboxPersistenceEntry(path, True, 0)
            for path in self.persistence_dirs
            if path.startswith(checkpoint_root + "/")
        ]
        entries.extend(
            SandboxPersistenceEntry(path, False, len(data))
            for path, data in self.persistence.items()
            if path.startswith(checkpoint_root + "/")
        )
        entries.sort(key=lambda item: item.path)
        return SandboxPersistenceListResult(
            tuple(entries[:max_results]),
            len(entries) > max_results,
        )

    def workspace_version_list_files(
        self,
        session_root,
        *,
        max_results,
        max_depth,
        timeout_seconds,
    ):
        del session_root, max_results, max_depth, timeout_seconds
        return SandboxPersistenceListResult((), False)


def _remote_runtime(
    session_id: str,
    *,
    sandbox: _FakeSandbox | None = None,
    transport: object | None = None,
    factory_calls: list[str] | None = None,
) -> tuple[ExecutionToolRuntime, _FakeSandbox]:
    selected_sandbox = sandbox if sandbox is not None else _FakeSandbox()

    def factory(_settings, _metadata):
        if factory_calls is not None:
            factory_calls.append("create")
        return selected_sandbox

    backend = AliyunSandboxExecutionBackend(
        session_id,
        _sandbox_settings(),
        sandbox_factory=factory,
        transport_factory=lambda _candidate: (
            transport if transport is not None else object()
        ),
    )
    return ExecutionToolRuntime(backend), selected_sandbox


class AgentExecutionRuntimeTests(unittest.TestCase):
    def test_remote_mutation_runtime_close_is_recorded_as_outcome_unknown(self):
        session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        runtime, sandbox = _remote_runtime(session.session_id)
        client = _FakeClient(
            [
                _reply(
                    calls=[
                        (
                            "call_unknown_write",
                            "write_file",
                            '{"path":"README.md","content":"new","mode":"overwrite"}',
                        )
                    ]
                )
            ]
        )
        agent = CodingAgent(
            client,
            _settings(),
            system_prompt="test",
            session_workflow=session,
            execution_runtime=runtime,
        )
        try:
            with patch(
                "app.agent.call_tool",
                side_effect=ExecutionRuntimeClosedError("unknown remote result"),
            ), self.assertRaises(AgentProtocolError):
                agent.run("write remotely")
            pending = agent.pending_recovery()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].call.id, "call_unknown_write")
            result_events = [
                event for event in agent.session_events if event.type == "tool/result"
            ]
            self.assertEqual(result_events[-1].data.recovery_status, "outcome_unknown")
        finally:
            agent.close()
        self.assertEqual(sandbox.kill_calls, 0)

    def test_fake_agent_reads_host_seed_after_first_remote_tool_call(self):
        session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        session_id = session.session_id
        body = b"seed body"
        snapshot = WorkspaceSeedSnapshot(
            (
                WorkspaceSeedEntry(
                    path="README.md",
                    size=len(body),
                    sha256=hashlib.sha256(body).hexdigest(),
                    data=body,
                ),
            )
        )
        transport = _SeedTransport(session_id)
        sandbox = _FakeSandbox()
        factory_calls: list[str] = []

        def factory(_settings, _metadata):
            factory_calls.append("create")
            return sandbox

        backend = AliyunSandboxExecutionBackend(
            session_id,
            _sandbox_settings(),
            sandbox_factory=factory,
            transport_factory=lambda _candidate: transport,
            workspace_initializer=create_workspace_seed_initializer(snapshot),
        )
        runtime = ExecutionToolRuntime(backend)
        client = _FakeClient(
            [
                _reply(calls=[("call_read", "read_file", '{"path":"README.md"}')]),
                _reply(content="done"),
            ]
        )
        agent = CodingAgent(
            client,
            _settings(),
            system_prompt="test system",
            session_workflow=session,
            execution_runtime=runtime,
        )
        try:
            self.assertEqual(factory_calls, [])
            self.assertEqual(agent.run("read seed"), "done")
            self.assertEqual(factory_calls, ["create"])
            self.assertEqual(transport.files[transport.root + "/README.md"], body)
            tool_messages = [
                item
                for item in client.completions.requests[-1]["messages"]
                if item.get("role") == "tool"
            ]
            self.assertEqual(len(tool_messages), 1)
            self.assertIn("seed body", tool_messages[0]["content"])
        finally:
            agent.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_none_and_local_runtime_preserve_local_messages_schema_and_results(self):
        responses = [
            _reply(calls=[("call_read", "read_file", '{"path":"AGENTS.md"}')]),
            _reply(content="done"),
        ]
        plain_client = _FakeClient(responses)
        local_client = _FakeClient(responses)
        plain_session = SessionEventWorkflow.in_memory()
        local_session = SessionEventWorkflow.in_memory()
        local_runtime = ExecutionToolRuntime()
        plain = CodingAgent(
            plain_client,
            _settings(),
            system_prompt="test system",
            session_workflow=plain_session,
        )
        local = CodingAgent(
            local_client,
            _settings(),
            system_prompt="test system",
            session_workflow=local_session,
            execution_runtime=local_runtime,
        )
        try:
            self.assertEqual(plain.run("read"), "done")
            self.assertEqual(local.run("read"), "done")
            self.assertEqual(plain_client.completions.requests, local_client.completions.requests)
            self.assertEqual(
                [item.model_dump(mode="json") for item in plain.messages],
                [item.model_dump(mode="json") for item in local.messages],
            )
        finally:
            plain.close()
            local.close()
        self.assertTrue(local_runtime.closed)

    def test_strict_binding_rejects_missing_mismatch_remote_and_closed_without_cloud(self):
        local_session = SessionEventWorkflow.in_memory()
        factory_calls: list[str] = []
        remote_runtime, _sandbox = _remote_runtime(
            "a" * 32,
            factory_calls=factory_calls,
        )
        with self.assertRaises(AgentSessionError):
            CodingAgent(
                _FakeClient(),
                _settings(),
                system_prompt="test",
                session_workflow=local_session,
                execution_runtime=remote_runtime,
            )
        self.assertFalse(remote_runtime.closed)
        self.assertEqual(factory_calls, [])
        local_session.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
        local_session.close()
        remote_runtime.close()

        remote_session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        with self.assertRaises(AgentSessionError):
            CodingAgent(
                _FakeClient(),
                _settings(),
                system_prompt="test",
                session_workflow=remote_session,
            )
        remote_session.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
        remote_session.close()

        mismatch_session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        mismatch_runtime, _sandbox = _remote_runtime(
            "b" * 32,
            factory_calls=factory_calls,
        )
        with self.assertRaises(AgentSessionError):
            CodingAgent(
                _FakeClient(),
                _settings(),
                system_prompt="test",
                session_workflow=mismatch_session,
                execution_runtime=mismatch_runtime,
            )
        self.assertFalse(mismatch_runtime.closed)
        self.assertEqual(factory_calls, [])
        mismatch_session.close()
        mismatch_runtime.close()

        closed_session = SessionEventWorkflow.in_memory()
        closed_runtime = ExecutionToolRuntime()
        closed_runtime.close()
        with self.assertRaises(AgentSessionError):
            CodingAgent(
                _FakeClient(),
                _settings(),
                system_prompt="test",
                session_workflow=closed_session,
                execution_runtime=closed_runtime,
            )
        closed_session.close()

    def test_matching_remote_read_list_and_grep_route_agent_registry_runtime(self):
        session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        transport = _ReadOnlyTransport(session.session_id)
        runtime, sandbox = _remote_runtime(session.session_id, transport=transport)
        client = _FakeClient(
            [
                _reply(
                    calls=[
                        ("call_read", "read_file", '{"path":"README.md"}'),
                        ("call_list", "list_files", '{"pattern":"README.md"}'),
                        (
                            "call_grep",
                            "grep",
                            '{"pattern":"needle","file_pattern":"README.md"}',
                        ),
                    ]
                ),
                _reply(content="remote done"),
            ]
        )
        agent = CodingAgent(
            client,
            _settings(),
            system_prompt="test",
            session_workflow=session,
            execution_runtime=runtime,
        )
        self.assertEqual(agent.run("inspect"), "remote done")
        self.assertEqual(transport.calls, ["read_file", "list_files", "grep"])
        tool_results = [
            json.loads(message["content"])
            for message in client.completions.requests[1]["messages"]
            if message["role"] == "tool"
        ]
        self.assertEqual([item["status"] for item in tool_results], ["success"] * 3)
        agent.close()
        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)

    def test_remote_async_code_and_bash_execute_without_local_fallback(self):
        session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        factory_calls: list[str] = []
        transport = _ReadOnlyTransport(session.session_id)
        runtime, sandbox = _remote_runtime(
            session.session_id,
            transport=transport,
            factory_calls=factory_calls,
        )
        client = _FakeClient(
            [
                _reply(
                    calls=[
                        (
                            "call_code",
                            "code_interpreter",
                            '{"code":"1 + 1","timeout_seconds":5}',
                        ),
                        (
                            "call_bash",
                            "bash_shell",
                            '{"command":"compileall","paths":["app"]}',
                        ),
                    ]
                ),
                _reply(content="remote accepted"),
                _reply(content="remote completed"),
            ]
        )
        agent = CodingAgent(
            client,
            _settings(),
            system_prompt="test",
            session_workflow=session,
            execution_runtime=runtime,
        )
        with patch("tool.registry._execute", side_effect=AssertionError("local")), patch(
            "tool.code_interpreter._run_child",
            side_effect=AssertionError("child"),
        ):
            self.assertEqual(agent.run("do privileged work"), "remote completed")
        tool_results = [
            json.loads(message["content"])
            for message in client.completions.requests[1]["messages"]
            if message["role"] == "tool"
        ]
        self.assertEqual([item["status"] for item in tool_results], ["accepted", "accepted"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            snapshots = agent.list_async_tasks()
            if len(snapshots) == 2 and all(item.completed for item in snapshots):
                break
            time.sleep(0.01)
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(
            all(item.result_status == "success" for item in snapshots),
            repr(snapshots),
        )
        self.assertEqual(
            sorted(transport.calls),
            ["code_interpreter", "compileall"],
        )
        self.assertEqual(factory_calls, ["create"])
        agent.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_normal_close_recovers_kill_error_and_remains_idempotent(self):
        session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        sandbox = _FakeSandbox(kill_error=True)
        runtime, _sandbox = _remote_runtime(session.session_id, sandbox=sandbox)
        runtime.ensure_ready()
        agent = CodingAgent(
            _FakeClient(),
            _settings(),
            system_prompt="test",
            session_workflow=session,
            execution_runtime=runtime,
        )
        agent.close()
        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)
        with self.assertRaises(SessionEventError):
            session.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
        agent.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_emergency_close_fences_before_writer_boundary_and_kills_in_daemon(self):
        session = SessionEventWorkflow.in_memory(
            execution_backend_name="aliyun-sandbox"
        )
        sandbox = _FakeSandbox(block_kill=True)
        runtime, _sandbox = _remote_runtime(session.session_id, sandbox=sandbox)
        runtime.ensure_ready()
        agent = CodingAgent(
            _FakeClient(),
            _settings(),
            system_prompt="test",
            session_workflow=session,
            execution_runtime=runtime,
        )
        order: list[str] = []
        original_begin_close = runtime.begin_close
        original_dispatcher_close = agent._dispatcher.emergency_close
        original_session_close = session.emergency_close

        def begin_close():
            order.append("fence")
            original_begin_close()

        def dispatcher_close():
            self.assertTrue(runtime.closed)
            order.append("dispatcher")
            original_dispatcher_close()

        def session_close():
            self.assertTrue(runtime.closed)
            order.append("session")
            original_session_close()

        sandbox.on_kill = lambda: order.append("kill")
        with patch.object(runtime, "begin_close", side_effect=begin_close), patch.object(
            agent._dispatcher,
            "emergency_close",
            side_effect=dispatcher_close,
        ), patch.object(session, "emergency_close", side_effect=session_close):
            started = time.monotonic()
            agent.close(wait=False)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertEqual(order[:3], ["fence", "dispatcher", "session"])
            self.assertTrue(sandbox.kill_started.wait(1))
            self.assertGreater(order.index("kill"), order.index("session"))
            with self.assertRaises(ExecutionRuntimeClosedError):
                runtime.execute("read_file", ReadFileArguments(path="README.md"))
            agent.close(wait=False)
            sandbox.kill_release.set()
            self.assertTrue(sandbox.kill_finished.wait(1))
        self.assertEqual(sandbox.kill_calls, 1)

    def test_constructor_failure_cleans_owned_runtime_tasks_dispatcher_and_session(self):
        for owns_session in (False, True):
            with self.subTest(owns_session=owns_session):
                session = SessionEventWorkflow.in_memory(
                    execution_backend_name="aliyun-sandbox"
                )
                factory_calls: list[str] = []
                runtime, _sandbox = _remote_runtime(
                    session.session_id,
                    factory_calls=factory_calls,
                )
                tasks = Mock()
                dispatcher = Mock()
                patches = [
                    patch("app.agent.EventDispatcher", return_value=dispatcher),
                    patch("app.agent.AsyncTaskManager.from_events", return_value=tasks),
                    patch("app.agent.SkillRuntime", side_effect=RuntimeError("boom")),
                ]
                if owns_session:
                    patches.append(
                        patch("app.agent.SessionEventWorkflow", return_value=session)
                    )
                started = []
                try:
                    for item in patches:
                        item.start()
                        started.append(item)
                    kwargs = {} if owns_session else {"session_workflow": session}
                    with self.assertRaises(AgentSessionError):
                        CodingAgent(
                            _FakeClient(),
                            _settings(),
                            system_prompt="test",
                            execution_runtime=runtime,
                            **kwargs,
                        )
                finally:
                    for item in reversed(started):
                        item.stop()
                self.assertTrue(runtime.closed)
                self.assertEqual(factory_calls, [])
                tasks.shutdown.assert_called_once_with(wait=True)
                dispatcher.close.assert_called_once_with()
                if owns_session:
                    with self.assertRaises(SessionEventError):
                        session.append(
                            "turn/start", TurnStartEventData(turn_id=uuid4().hex)
                        )
                else:
                    session.append(
                        "turn/start", TurnStartEventData(turn_id=uuid4().hex)
                    )
                    session.close()


if __name__ == "__main__":
    unittest.main()
