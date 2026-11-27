"""Registry/runtime integration for durable remote write and edit."""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock
from unittest.mock import patch

from pydantic import SecretStr

from app.execution_backend import (
    AliyunSandboxExecutionBackend as _AliyunSandboxExecutionBackend,
)
from app.execution_runtime import ExecutionRuntimeClosedError, ExecutionToolRuntime
from app.sandbox_config import SandboxSettings
from app.sandbox_transport import SandboxCommandResult
from app.schemas import (
    BashShellArguments,
    FileApprovalStatus,
    ReadFileArguments,
    ToolCall,
    ToolMessage,
    WriteFileArguments,
)
from app.workflow import FileApprovalWorkflow
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from app.workspace_versions import WorkspaceVersionCoordinator, parse_manifest
from tests.test_workspace_versions import _MemoryTransport
from tool import registry
from tool.registry import call_tool
from tests.sandbox_cleanup_fakes import empty_recovery_provider


def AliyunSandboxExecutionBackend(*args, **kwargs):
    kwargs.setdefault("automatic_maintenance", False)
    kwargs.setdefault("recovery_provider", empty_recovery_provider())
    if "sandbox_factory" in kwargs and "persistence_sandbox_factory" not in kwargs:
        class PersistenceSandbox:
            sandbox_id = "mutation-persistence-sandbox"

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


class _Sandbox:
    sandbox_id = "remote-mutation-test"

    def __init__(self) -> None:
        self.kill_calls = 0

    def kill(self):
        self.kill_calls += 1
        return True


def _settings() -> SandboxSettings:
    return SandboxSettings(
        enabled=True,
        api_key=SecretStr("remote-mutation-test-key"),
        api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        domain="cn-hangzhou.e2b.fc.aliyuncs.com",
        template="code-interpreter-v1",
        timeout_seconds=300,
        oss_bucket="bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket_path="/project/test",
        oss_mount_dir="/mnt/oss",
        oss_role_arn="acs:ram::1234567890123456:role/test",
        oss_read_only=False,
    )


def _result(raw: str) -> dict[str, object]:
    return json.loads(ToolMessage.model_validate_json(raw).model_dump_json())["result"]


class RemoteMutationRuntimeTests(unittest.TestCase):
    session_id = "9" * 32
    workspace_root = "/workspace/agnet-study/" + session_id

    def _runtime(self, body: bytes = b"old\n"):
        snapshot = WorkspaceSeedSnapshot(
            (
                WorkspaceSeedEntry(
                    "AGENTS.md", len(body), hashlib.sha256(body).hexdigest(), body
                ),
            )
        )
        sandbox = _Sandbox()
        transport = _MemoryTransport(self.workspace_root)
        backend = AliyunSandboxExecutionBackend(
            self.session_id,
            _settings(),
            sandbox_factory=lambda _settings, _metadata: sandbox,
            transport_factory=lambda _candidate: transport,
            workspace_initializer=WorkspaceVersionCoordinator(snapshot),
        )
        return ExecutionToolRuntime(backend), sandbox, transport

    def _format_runtime(self):
        bodies = {
            "a.json": b'{"z":1,"a":2}\n',
            "b.json": b'{"nested":{"b":2,"a":1}}\n',
        }
        snapshot = WorkspaceSeedSnapshot(tuple(
            WorkspaceSeedEntry(
                path,
                len(body),
                hashlib.sha256(body).hexdigest(),
                body,
            )
            for path, body in sorted(bodies.items())
        ))
        sandbox = _Sandbox()
        transport = _MemoryTransport(self.workspace_root)
        backend = AliyunSandboxExecutionBackend(
            self.session_id,
            _settings(),
            sandbox_factory=lambda _settings, _metadata: sandbox,
            transport_factory=lambda _candidate: transport,
            workspace_initializer=WorkspaceVersionCoordinator(snapshot),
        )
        return ExecutionToolRuntime(backend), sandbox, transport, bodies

    def test_remote_format_json_requires_one_diff_approval_and_one_generation(self):
        runtime, sandbox, transport, bodies = self._format_runtime()
        workflow = FileApprovalWorkflow()
        requests = []
        local_handler = Mock(side_effect=AssertionError("local fallback"))
        registration = replace(
            registry._REGISTRY["bash_shell"], handler=local_handler
        )

        def approve(request):
            requests.append(request)
            self.assertEqual(request.phase, "diff")
            self.assertEqual(
                transport.active[self.workspace_root + "/a.json"],
                bodies["a.json"],
            )
            self.assertEqual(
                transport.active[self.workspace_root + "/b.json"],
                bodies["b.json"],
            )
            self.assertFalse(any(
                "/generations/" in path for path in transport.persistence
            ))
            return True

        with patch.dict(registry._REGISTRY, {"bash_shell": registration}):
            result = _result(call_tool(
                ToolCall(
                    id="format-json-transaction",
                    name="bash_shell",
                    arguments={
                        "command": "format_json",
                        "paths": ["b.json", "a.json"],
                        "timeout_seconds": 9,
                    },
                ),
                execution_runtime=runtime,
                workflow=workflow,
                confirmation_callback=approve,
                permission_mode="full-access",
            ))

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"], {
            "command": "format_json",
            "paths": ["b.json", "a.json"],
            "exit_code": 0,
            "stdout": "已规范格式化 2 个 JSON 文件。",
            "stderr": "",
            "truncated": False,
            "result": "",
        })
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].paths, ("b.json", "a.json"))
        self.assertTrue(requests[0].pending_file.startswith("remote-memory/"))
        manifests = [
            parse_manifest(body)
            for path, body in transport.persistence.items()
            if "/generations/" in path
        ]
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0].tool_name, "bash_shell")
        self.assertEqual(len(manifests[0].changes), 2)
        local_handler.assert_not_called()
        runtime.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_remote_format_json_rejection_keeps_active_and_releases_workspace_gate(self):
        runtime, _sandbox, transport, bodies = self._format_runtime()
        result = _result(call_tool(
            ToolCall(
                id="format-json-rejected",
                name="bash_shell",
                arguments={
                    "command": "format_json",
                    "paths": ["a.json", "b.json"],
                },
            ),
            execution_runtime=runtime,
            workflow=FileApprovalWorkflow(),
            confirmation_callback=lambda _request: False,
            permission_mode="full-access",
        ))
        self.assertEqual(result["status"], "denied")
        self.assertEqual(
            transport.active[self.workspace_root + "/a.json"], bodies["a.json"]
        )
        self.assertEqual(
            transport.active[self.workspace_root + "/b.json"], bodies["b.json"]
        )
        self.assertFalse(any(
            "/generations/" in path for path in transport.persistence
        ))
        observed = runtime.execute(
            "read_file", ReadFileArguments(path="a.json")
        )
        self.assertEqual(observed.content, bodies["a.json"].decode("utf-8"))
        runtime.close()

    def test_remote_format_json_candidate_holds_workspace_gate_until_discard(self):
        runtime, _sandbox, _transport, _bodies = self._format_runtime()
        prepared = runtime.prepare_remote_command_mutation(
            BashShellArguments(command="format_json", paths=["a.json"]),
            operation_id="f" * 64,
            permission_mode="full-access",
        )
        started = threading.Event()
        finished = threading.Event()
        observed: list[object] = []

        def read_remote() -> None:
            started.set()
            try:
                observed.append(runtime.execute(
                    "read_file", ReadFileArguments(path="a.json")
                ))
            finally:
                finished.set()

        thread = threading.Thread(target=read_remote, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(finished.wait(0.05))
        prepared.discard()
        self.assertTrue(finished.wait(1))
        thread.join(1)
        self.assertEqual(observed[0].path, "a.json")
        runtime.close()

    def test_remote_format_json_manifest_uncertainty_fences_without_fallback(self):
        runtime, sandbox, transport, _bodies = self._format_runtime()
        runtime.ensure_ready()
        transport.fail_manifest = True
        transport.uncertain_manifest = True
        local_handler = Mock(side_effect=AssertionError("local fallback"))
        registration = replace(
            registry._REGISTRY["bash_shell"], handler=local_handler
        )
        with patch.dict(registry._REGISTRY, {"bash_shell": registration}):
            with self.assertRaises(ExecutionRuntimeClosedError):
                call_tool(
                    ToolCall(
                        id="format-json-uncertain",
                        name="bash_shell",
                        arguments={
                            "command": "format_json",
                            "paths": ["a.json", "b.json"],
                        },
                    ),
                    execution_runtime=runtime,
                    workflow=FileApprovalWorkflow(),
                    confirmation_callback=lambda _request: True,
                    permission_mode="full-access",
                )
        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)
        local_handler.assert_not_called()

    def test_remote_write_uses_exact_diff_in_memory_and_publishes_stable_operation(self):
        runtime, sandbox, transport = self._runtime()
        workflow = FileApprovalWorkflow()
        requests = []

        def approve(request):
            requests.append(request)
            return True

        call = ToolCall(
            id="stable-call-id",
            name="write_file",
            arguments={"path": "AGENTS.md", "content": "new\n", "mode": "overwrite"},
        )
        with (
            patch.object(registry, "prepare_write_file", side_effect=AssertionError("host pending used")),
            patch.object(registry, "prepare_editor_file", side_effect=AssertionError("host pending used")),
        ):
            result = _result(
                call_tool(
                    call,
                    execution_runtime=runtime,
                    workflow=workflow,
                    confirmation_callback=approve,
                    permission_mode="partial-control",
                )
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(transport.active[self.workspace_root + "/AGENTS.md"], b"new\n")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].phase, "diff")
        self.assertTrue(requests[0].pending_file.startswith("remote-memory/"))
        self.assertNotIn("data/pending", requests[0].pending_file)
        manifest_raw = next(
            body for path, body in transport.persistence.items() if "/generations/" in path
        )
        manifest = parse_manifest(manifest_raw)
        expected_operation = hashlib.sha256(
            json.dumps(
                {
                    "arguments": call.arguments,
                    "call_id": call.id,
                    "session_id": self.session_id,
                    "tool_name": call.name,
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(manifest.operation_id, expected_operation)
        runtime.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_remote_editor_commits_and_manifest_uncertainty_fences_without_fallback(self):
        runtime, sandbox, transport = self._runtime()
        workflow = FileApprovalWorkflow()
        transport.fail_manifest = True
        transport.uncertain_manifest = True
        local_handler = Mock(side_effect=AssertionError("local fallback"))
        registration = replace(
            registry._REGISTRY["editor_file"], handler=local_handler
        )
        with patch.dict(registry._REGISTRY, {"editor_file": registration}):
            with self.assertRaises(ExecutionRuntimeClosedError):
                call_tool(
                    ToolCall(
                        id="uncertain-edit",
                        name="editor_file",
                        arguments={
                            "path": "AGENTS.md",
                            "old_text": "old",
                            "new_text": "new",
                            "expected_replacements": 1,
                        },
                    ),
                    execution_runtime=runtime,
                    workflow=workflow,
                    confirmation_callback=lambda _request: True,
                    permission_mode="partial-control",
                )
        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)
        local_handler.assert_not_called()

    def test_active_write_uncertainty_fences_runtime_and_propagates(self):
        runtime, sandbox, transport = self._runtime()
        runtime.ensure_ready()
        transport.raise_after_active_write = True
        with self.assertRaises(ExecutionRuntimeClosedError):
            call_tool(
                ToolCall(
                    id="uncertain-active-write",
                    name="write_file",
                    arguments={
                        "path": "AGENTS.md",
                        "content": "new\n",
                        "mode": "overwrite",
                    },
                ),
                execution_runtime=runtime,
                workflow=FileApprovalWorkflow(),
                confirmation_callback=lambda _request: True,
                permission_mode="partial-control",
            )
        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)

    def test_mutation_reservation_blocks_read_until_discard(self):
        runtime, _sandbox, _transport = self._runtime()
        prepared = runtime.prepare_remote_mutation(
            "write_file",
            WriteFileArguments(path="AGENTS.md", content="new\n", mode="overwrite"),
            operation_id="a" * 64,
            permission_mode="partial-control",
        )
        started = threading.Event()
        finished = threading.Event()
        result: list[object] = []

        def read_remote() -> None:
            started.set()
            try:
                result.append(
                    runtime.execute("read_file", ReadFileArguments(path="AGENTS.md"))
                )
            finally:
                finished.set()

        thread = threading.Thread(target=read_remote, daemon=True)
        thread.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(finished.wait(0.05))
        prepared.discard()
        self.assertTrue(finished.wait(1))
        thread.join(1)
        self.assertEqual(result[0].content, "old\n")
        runtime.close()

    def test_close_does_not_wait_for_mutation_reservation(self):
        runtime, sandbox, _transport = self._runtime()
        prepared = runtime.prepare_remote_mutation(
            "write_file",
            WriteFileArguments(path="AGENTS.md", content="new\n", mode="overwrite"),
            operation_id="b" * 64,
            permission_mode="partial-control",
        )
        closed = threading.Event()

        def close_runtime() -> None:
            runtime.close()
            closed.set()

        thread = threading.Thread(target=close_runtime, daemon=True)
        thread.start()
        self.assertTrue(closed.wait(1))
        thread.join(1)
        self.assertEqual(sandbox.kill_calls, 1)
        prepared.discard()

    def test_remote_mutation_rejects_escape_and_hard_exclusion_without_local_handler(self):
        runtime, _sandbox, transport = self._runtime()
        local_handler = Mock(side_effect=AssertionError("local fallback"))
        registration = replace(registry._REGISTRY["write_file"], handler=local_handler)
        with patch.dict(registry._REGISTRY, {"write_file": registration}):
            for path in ("../escape.py", ".agent/tool_policy.json"):
                with self.subTest(path=path):
                    result = _result(
                        call_tool(
                            ToolCall(
                                id="denied-" + hashlib.sha256(path.encode()).hexdigest()[:8],
                                name="write_file",
                                arguments={"path": path, "content": "x", "mode": "create"},
                            ),
                            execution_runtime=runtime,
                            workflow=FileApprovalWorkflow(),
                            confirmation_callback=lambda _request: True,
                            permission_mode="full-access",
                        )
                    )
                    self.assertEqual(result["status"], "denied")
        local_handler.assert_not_called()
        self.assertFalse(any("/generations/" in path for path in transport.persistence))
        runtime.close()

    def test_diff_rejection_discards_reservation_and_approval_race_preserves_observed_file(self):
        runtime, _sandbox, transport = self._runtime()
        rejected = _result(
            call_tool(
                ToolCall(
                    id="reject-protected",
                    name="write_file",
                    arguments={
                        "path": "AGENTS.md",
                        "content": "rejected\n",
                        "mode": "overwrite",
                    },
                ),
                execution_runtime=runtime,
                workflow=FileApprovalWorkflow(),
                confirmation_callback=lambda _request: False,
                permission_mode="partial-control",
            )
        )
        self.assertEqual(rejected["status"], "denied")
        self.assertEqual(
            runtime.execute("read_file", ReadFileArguments(path="AGENTS.md")).content,
            "old\n",
        )

        active_path = self.workspace_root + "/AGENTS.md"

        def race(_request):
            transport.active[active_path] = b"outside race\n"
            return True

        raced = _result(
            call_tool(
                ToolCall(
                    id="approval-race",
                    name="write_file",
                    arguments={
                        "path": "AGENTS.md",
                        "content": "approved\n",
                        "mode": "overwrite",
                    },
                ),
                execution_runtime=runtime,
                workflow=FileApprovalWorkflow(),
                confirmation_callback=race,
                permission_mode="partial-control",
            )
        )
        self.assertEqual(raced["status"], "error")
        self.assertEqual(transport.active[active_path], b"outside race\n")
        self.assertFalse(any("/generations/" in path for path in transport.persistence))
        runtime.close()

    def test_concurrent_approvals_keep_exact_bindings_and_hold_workspace_gate(self):
        runtime, _sandbox, transport = self._runtime()
        first_workflow = FileApprovalWorkflow()
        second_workflow = FileApprovalWorkflow()
        first_entered = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        all_reservations_attempted = threading.Event()
        command_dispatched = threading.Event()
        reserve_lock = threading.Lock()
        reserve_attempts = 0
        requests: dict[str, object] = {}
        results: dict[str, object] = {}
        errors: list[BaseException] = []

        original_reserve = runtime._reserve_workspace

        def observed_reserve(generation=None):
            nonlocal reserve_attempts
            with reserve_lock:
                reserve_attempts += 1
                if reserve_attempts == 3:
                    all_reservations_attempted.set()
            return original_reserve(generation)

        class _Handle:
            def wait(self):
                return SandboxCommandResult(0, "ok", "")

            def cancel(self):
                return None

        def start_run(*_args, dispatch_attempt_consumer, **_kwargs):
            command_dispatched.set()
            dispatch_attempt_consumer()
            return _Handle()

        transport.start_run = start_run  # type: ignore[attr-defined]

        def invoke_write(label, call, workflow, callback):
            try:
                results[label] = _result(call_tool(
                    call,
                    execution_runtime=runtime,
                    workflow=workflow,
                    confirmation_callback=callback,
                    permission_mode="partial-control",
                ))
            except BaseException as error:
                errors.append(error)

        def approve_first(request):
            requests["first"] = request
            first_entered.set()
            if not release_first.wait(2):
                raise RuntimeError("bounded approval wait expired")
            return True

        def reject_second(request):
            requests["second"] = request
            second_entered.set()
            return False

        first_call = ToolCall(
            id="concurrent-first",
            name="write_file",
            arguments={"path": "AGENTS.md", "content": "first\n", "mode": "overwrite"},
        )
        second_call = ToolCall(
            id="concurrent-second",
            name="write_file",
            arguments={"path": "AGENTS.md", "content": "second\n", "mode": "overwrite"},
        )
        command_result: list[object] = []

        def invoke_command():
            try:
                command_result.append(runtime.execute_authorized(
                    "bash_shell",
                    BashShellArguments(command="compileall", paths=["AGENTS.md"]),
                    lambda: True,
                ))
            except BaseException as error:
                errors.append(error)

        with patch.object(runtime, "_reserve_workspace", side_effect=observed_reserve):
            first = threading.Thread(
                target=invoke_write,
                args=("first", first_call, first_workflow, approve_first),
            )
            first.start()
            second = threading.Thread(
                target=invoke_write,
                args=("second", second_call, second_workflow, reject_second),
            )
            command = threading.Thread(target=invoke_command)
            try:
                self.assertTrue(first_entered.wait(1))
                second.start()
                command.start()
                self.assertTrue(all_reservations_attempted.wait(1))
                self.assertFalse(second_entered.is_set())
                self.assertFalse(command_dispatched.is_set())
            finally:
                release_first.set()
                for worker in (first, second, command):
                    if worker.ident is not None:
                        worker.join(2)

        self.assertTrue(all(not worker.is_alive() for worker in (first, second, command)))
        self.assertFalse(errors)
        self.assertEqual(results["first"]["status"], "success")
        self.assertEqual(results["second"]["status"], "denied")
        self.assertEqual(len(command_result), 1)
        self.assertTrue(second_entered.is_set())
        self.assertTrue(command_dispatched.is_set())
        first_request = requests["first"]
        second_request = requests["second"]
        self.assertNotEqual(first_request.request_id, second_request.request_id)
        self.assertNotEqual(first_request.pending_file, second_request.pending_file)
        self.assertEqual(
            transport.active[self.workspace_root + "/AGENTS.md"],
            b"first\n",
        )
        runtime.close()

    def test_shared_workflow_rejects_second_concurrent_request_without_resetting_first(self):
        runtime, _sandbox, transport = self._runtime()
        workflow = FileApprovalWorkflow()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_callback_entered = threading.Event()
        second_done = threading.Event()
        first_requests: list[object] = []
        results: dict[str, object] = {}
        errors: list[BaseException] = []

        def approve_first(request):
            first_requests.append(request)
            first_entered.set()
            if not release_first.wait(2):
                raise RuntimeError("bounded approval wait expired")
            return True

        def unexpected_second(_request):
            second_callback_entered.set()
            return True

        def invoke(label, call, callback):
            try:
                results[label] = _result(call_tool(
                    call,
                    execution_runtime=runtime,
                    workflow=workflow,
                    confirmation_callback=callback,
                    permission_mode="partial-control",
                ))
            except BaseException as error:
                errors.append(error)
            finally:
                if label == "second":
                    second_done.set()

        first = threading.Thread(
            target=invoke,
            args=(
                "first",
                ToolCall(
                    id="shared-first",
                    name="write_file",
                    arguments={
                        "path": "AGENTS.md",
                        "content": "first\n",
                        "mode": "overwrite",
                    },
                ),
                approve_first,
            ),
        )
        second = threading.Thread(
            target=invoke,
            args=(
                "second",
                ToolCall(
                    id="shared-second",
                    name="write_file",
                    arguments={
                        "path": "AGENTS.md",
                        "content": "second\n",
                        "mode": "overwrite",
                    },
                ),
                unexpected_second,
            ),
        )
        first.start()
        try:
            self.assertTrue(first_entered.wait(1))
            second.start()
            self.assertTrue(second_done.wait(1))
            self.assertFalse(second_callback_entered.is_set())
            self.assertEqual(results["second"]["status"], "denied")
            state = workflow.state_for("AGENTS.md")
            self.assertEqual(state.status, FileApprovalStatus.DIFF_PENDING)
            self.assertEqual(state.request_id, first_requests[0].request_id)
        finally:
            release_first.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results["first"]["status"], "success")
        self.assertEqual(
            transport.active[self.workspace_root + "/AGENTS.md"],
            b"first\n",
        )
        runtime.close()

    def test_approval_exception_releases_remote_mutation_reservation(self):
        runtime, _sandbox, _transport = self._runtime()

        def fail_confirmation(_request):
            raise RuntimeError("untrusted confirmation detail")

        result = _result(call_tool(
            ToolCall(
                id="confirmation-exception",
                name="write_file",
                arguments={
                    "path": "AGENTS.md",
                    "content": "not-written\n",
                    "mode": "overwrite",
                },
            ),
            execution_runtime=runtime,
            workflow=FileApprovalWorkflow(),
            confirmation_callback=fail_confirmation,
            permission_mode="partial-control",
        ))

        self.assertEqual(result["status"], "denied")
        observed = runtime.execute(
            "read_file", ReadFileArguments(path="AGENTS.md")
        )
        self.assertEqual(observed.content, "old\n")
        runtime.close()

    def test_close_during_diff_approval_fences_runtime_and_releases_gate(self):
        runtime, sandbox, transport = self._runtime()
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def approve(_request):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("bounded approval wait expired")
            return True

        def invoke():
            try:
                call_tool(
                    ToolCall(
                        id="close-during-approval",
                        name="write_file",
                        arguments={
                            "path": "AGENTS.md",
                            "content": "not-committed\n",
                            "mode": "overwrite",
                        },
                    ),
                    execution_runtime=runtime,
                    workflow=FileApprovalWorkflow(),
                    confirmation_callback=approve,
                    permission_mode="partial-control",
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            runtime.close()
        finally:
            release.set()
            worker.join(2)
            runtime.close()

        self.assertFalse(worker.is_alive())
        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertTrue(any(isinstance(error, ExecutionRuntimeClosedError) for error in errors))
        self.assertEqual(
            transport.active[self.workspace_root + "/AGENTS.md"],
            b"old\n",
        )
        self.assertTrue(runtime._workspace_gate.acquire(blocking=False))
        runtime._workspace_gate.release()


if __name__ == "__main__":
    unittest.main()
