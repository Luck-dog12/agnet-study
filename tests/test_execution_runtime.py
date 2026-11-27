"""Stage 5A internal execution runtime and authorization tests."""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from pydantic import BaseModel, SecretStr

from app import execution_runtime as runtime_module
from app.execution_backend import (
    AliyunSandboxExecutionBackend as _AliyunSandboxExecutionBackend,
    ExecutionBackend,
)
from tests.sandbox_cleanup_fakes import empty_recovery_provider


def AliyunSandboxExecutionBackend(*args, **kwargs):
    kwargs.setdefault("automatic_maintenance", False)
    kwargs.setdefault("recovery_provider", empty_recovery_provider())
    if "sandbox_factory" in kwargs and "persistence_sandbox_factory" not in kwargs:
        class PersistenceSandbox:
            sandbox_id = "runtime-persistence-sandbox"

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
from app.execution_runtime import (
    ExecutionRuntimeClosedError,
    ExecutionRuntimeError,
    ExecutionToolRuntime,
    LocalRegistryDelegationRequired,
    RemoteExecutionAuthorizationDenied,
    WORKSPACE_TOOL_NAMES,
)
from app.sandbox_config import SandboxSettings
from app.sandbox_transport import SandboxReadResult, SandboxWriteResult
from app.schemas import (
    BashShellArguments,
    CodeInterpreterArguments,
    EditorFileArguments,
    GrepArguments,
    ListFilesArguments,
    ReadFileArguments,
    WriteFileArguments,
)


def enabled_settings() -> SandboxSettings:
    return SandboxSettings(
        enabled=True,
        api_key=SecretStr("runtime-test-key"),
        api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        domain="cn-hangzhou.e2b.fc.aliyuncs.com",
        template="code-interpreter-v1",
        timeout_seconds=300,
        oss_bucket="agent-probe-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket_path="/projects/test/runs/runtime",
        oss_mount_dir="/mnt/oss",
        oss_role_arn="acs:ram::1234567890123456:role/runtime-test",
        oss_read_only=False,
    )


class _FakeSandbox:
    sandbox_id = "runtime-sandbox"

    def __init__(self) -> None:
        self.kill_calls = 0

    def kill(self) -> bool:
        self.kill_calls += 1
        return True


class _FakeRunHandle:
    def __init__(self) -> None:
        self.cancel_calls = 0
        self.cancel_error: Exception | None = None

    def wait(self):
        return None

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.cancel_error is not None:
            raise self.cancel_error


class _FakeRemoteGroup:
    instances: list["_FakeRemoteGroup"] = []

    def __init__(self, backend, *, authorization_consumer) -> None:
        self.backend = backend
        self.consume = authorization_consumer
        self.calls: list[tuple[str, BaseModel, object | None]] = []
        self.fail_tool: str | None = None
        self.fail_after_dispatch_tool: str | None = None
        self.system_error_after_dispatch_tool: str | None = None
        self.dispatch_started = threading.Event()
        self.handle_bound = threading.Event()
        self.release_dispatch = threading.Event()
        self.release_dispatch.set()
        self.release_after_bind = threading.Event()
        self.release_after_bind.set()
        self.handle = _FakeRunHandle()
        self.closed = False
        type(self).instances.append(self)

    def _call(
        self,
        name: str,
        arguments: BaseModel,
        authorization: object | None = None,
        kind: str | None = None,
        run_handle_consumer=None,
        dispatch_attempt_consumer=None,
    ) -> BaseModel:
        if kind is not None:
            self.consume(name, arguments, authorization, kind)
        self.backend.ensure_ready()
        self.calls.append((name, arguments, authorization))
        if self.fail_tool == name:
            raise OSError("remote failed")
        if run_handle_consumer is not None or dispatch_attempt_consumer is not None:
            if run_handle_consumer is None or dispatch_attempt_consumer is None:
                raise AssertionError("incomplete lifecycle consumers")
            dispatch_attempt_consumer()
            self.dispatch_started.set()
            self.release_dispatch.wait(2)
            if self.fail_after_dispatch_tool == name:
                raise OSError("provider disconnected before handle")
            if self.system_error_after_dispatch_tool == name:
                raise KeyboardInterrupt("provider interrupted before handle")
            run_handle_consumer(self.handle)
            self.handle_bound.set()
            self.release_after_bind.wait(2)
        return arguments

    def read_file(self, arguments):
        return self._call("read_file", arguments)

    def write_file(self, arguments, *, authorization=None):
        return self._call("write_file", arguments, authorization, "approval")

    def editor_file(self, arguments, *, authorization=None):
        return self._call("editor_file", arguments, authorization, "approval")

    def list_files(self, arguments):
        return self._call("list_files", arguments)

    def grep(self, arguments):
        return self._call("grep", arguments)

    def command(
        self,
        arguments,
        *,
        authorization=None,
        run_handle_consumer=None,
        dispatch_attempt_consumer=None,
    ):
        return self._call(
            "bash_shell",
            arguments,
            authorization,
            "approval",
            run_handle_consumer,
            dispatch_attempt_consumer,
        )

    def code_interpreter(
        self,
        arguments,
        *,
        authorization=None,
        run_handle_consumer=None,
        dispatch_attempt_consumer=None,
    ):
        return self._call(
            "code_interpreter",
            arguments,
            authorization,
            "policy",
            run_handle_consumer,
            dispatch_attempt_consumer,
        )

    def close(self) -> None:
        self.closed = True
        self.backend.close()


class _UnsupportedBackend(ExecutionBackend):
    def ensure_ready(self):
        raise AssertionError

    def close(self) -> None:
        return None


class _MinimalTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def read_file(self, path: str, *, offset: int, max_bytes: int, timeout_seconds: int):
        del offset, max_bytes, timeout_seconds
        self.calls.append(("read_file", path))
        return SandboxReadResult(b"ok", 2)

    def write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ):
        del overwrite, timeout_seconds
        self.calls.append(("write_file", path))
        return SandboxWriteResult(len(data))


class ExecutionToolRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeRemoteGroup.instances.clear()
        self.sandbox = _FakeSandbox()
        self.backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: self.sandbox,
            transport_factory=lambda candidate: object(),
        )
        self.group_patch = patch.object(
            runtime_module, "AliyunSandboxToolGroup", _FakeRemoteGroup
        )
        self.group_patch.start()

    def tearDown(self) -> None:
        self.backend.close()
        self.group_patch.stop()

    def remote_runtime(self) -> ExecutionToolRuntime:
        return ExecutionToolRuntime(self.backend)

    def test_default_local_route_is_complete_and_requires_registry_delegation(self):
        runtime = ExecutionToolRuntime()
        self.assertEqual(runtime.backend_name, "local")
        self.assertIsNone(runtime.session_id)
        self.assertEqual(runtime.workspace_tool_names, WORKSPACE_TOOL_NAMES)
        self.assertEqual(
            {runtime.route_for(name) for name in WORKSPACE_TOOL_NAMES},
            {"local-registry"},
        )
        with self.assertRaises(LocalRegistryDelegationRequired):
            runtime.execute("read_file", ReadFileArguments(path="README.md"))
        with self.assertRaises(LocalRegistryDelegationRequired):
            runtime.execute_authorized(
                "write_file",
                WriteFileArguments(path="new.txt", content="x", mode="create"),
                lambda: True,
            )
        self.assertFalse(_FakeRemoteGroup.instances)
        runtime.close()

    def test_remote_identity_is_read_only_and_begin_close_only_installs_fence(self):
        runtime = self.remote_runtime()
        self.assertEqual(runtime.backend_name, "aliyun-sandbox")
        self.assertEqual(runtime.session_id, "a" * 32)
        runtime.ensure_ready()

        runtime.begin_close()
        runtime.begin_close()

        self.assertTrue(runtime.closed)
        self.assertEqual(self.sandbox.kill_calls, 0)
        with self.assertRaises(ExecutionRuntimeClosedError):
            runtime.execute("read_file", ReadFileArguments(path="README.md"))

        runtime.close()
        runtime.close()
        self.assertEqual(self.sandbox.kill_calls, 1)

    def test_constructor_rejects_partial_or_unknown_backend_types(self):
        with self.assertRaises(TypeError):
            ExecutionToolRuntime(_UnsupportedBackend())
        with self.assertRaises(ExecutionRuntimeError):
            ExecutionToolRuntime().route_for("web_fetch")

    def test_remote_route_uses_one_group_and_workspace_for_all_seven_tools(self):
        runtime = self.remote_runtime()
        arguments = {
            "read_file": ReadFileArguments(path="README.md"),
            "write_file": WriteFileArguments(
                path="new.txt", content="x", mode="create"
            ),
            "editor_file": EditorFileArguments(
                path="README.md",
                old_text="old",
                new_text="new",
                expected_replacements=1,
            ),
            "list_files": ListFilesArguments(pattern="**/*"),
            "grep": GrepArguments(pattern="needle"),
            "bash_shell": BashShellArguments(command="compileall", paths=["app"]),
            "code_interpreter": CodeInterpreterArguments(code="1+1"),
        }
        for name in WORKSPACE_TOOL_NAMES:
            self.assertEqual(runtime.route_for(name), "aliyun-sandbox")
            if name in {"read_file", "list_files", "grep"}:
                runtime.execute(name, arguments[name])
            elif name in {"write_file", "editor_file"}:
                with self.assertRaises(RemoteExecutionAuthorizationDenied):
                    runtime.execute_authorized(name, arguments[name], lambda: True)
            else:
                runtime.execute_authorized(name, arguments[name], lambda: True)

        self.assertEqual(len(_FakeRemoteGroup.instances), 1)
        group = _FakeRemoteGroup.instances[0]
        self.assertEqual(
            {name for name, _args, _permit in group.calls},
            WORKSPACE_TOOL_NAMES - {"write_file", "editor_file"},
        )
        self.assertEqual(
            runtime.ensure_ready().workspace_root,
            "/workspace/agnet-study/" + "a" * 32,
        )
        runtime.close()
        runtime.close()
        self.assertTrue(group.closed)
        self.assertEqual(self.sandbox.kill_calls, 1)

    def test_remote_failure_never_falls_back_to_local(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.fail_tool = "read_file"
        with self.assertRaisesRegex(OSError, "remote failed"):
            runtime.execute("read_file", ReadFileArguments(path="README.md"))
        self.assertEqual([item[0] for item in group.calls], ["read_file"])
        runtime.close()

    def test_privileged_remote_tools_deny_without_exact_authorization(self):
        runtime = self.remote_runtime()
        arguments = WriteFileArguments(path="new.txt", content="x", mode="create")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime.execute("write_file", arguments)
        for callback in (
            lambda: False,
            lambda: "yes",
            lambda: (_ for _ in ()).throw(KeyboardInterrupt()),
        ):
            with self.subTest(callback=callback), self.assertRaises(
                RemoteExecutionAuthorizationDenied
            ):
                runtime.execute_authorized("write_file", arguments, callback)  # type: ignore[arg-type]
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime.execute_authorized(
                "bash_shell",
                BashShellArguments(command="compileall", paths=["app"]),
                True,  # type: ignore[arg-type]
            )
        self.assertFalse(_FakeRemoteGroup.instances[0].calls)
        runtime.close()

    def test_permit_is_not_json_and_rejects_replay_modified_tool_args_and_runtime(self):
        runtime = self.remote_runtime()
        other_sandbox = _FakeSandbox()
        other_backend = AliyunSandboxExecutionBackend(
            "b" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: other_sandbox,
            transport_factory=lambda candidate: object(),
        )
        other = ExecutionToolRuntime(other_backend)
        write = WriteFileArguments(path="new.txt", content="x", mode="create")
        modified = WriteFileArguments(path="new.txt", content="changed", mode="create")
        edit = EditorFileArguments(
            path="new.txt",
            old_text="x",
            new_text="y",
            expected_replacements=1,
        )
        permit = runtime._issue_authorization("write_file", write, "approval")
        with self.assertRaises(TypeError):
            json.dumps(permit)
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime._consume_authorization("write_file", modified, permit, "approval")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime._consume_authorization("editor_file", edit, permit, "approval")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            other._consume_authorization("write_file", write, permit, "approval")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime._consume_authorization("write_file", write, object(), "approval")

        runtime._consume_authorization("write_file", write, permit, "approval")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime._consume_authorization("write_file", write, permit, "approval")
        self.assertTrue(permit.consumed)
        runtime.close()
        other.close()
        self.assertEqual(other_sandbox.kill_calls, 0)

    def test_close_invalidates_permit_and_rejects_all_later_operations(self):
        runtime = self.remote_runtime()
        write = WriteFileArguments(path="new.txt", content="x", mode="create")
        permit = runtime._issue_authorization("write_file", write, "approval")
        runtime.close()
        with self.assertRaises(ExecutionRuntimeClosedError):
            runtime._consume_authorization("write_file", write, permit, "approval")
        with self.assertRaises(ExecutionRuntimeClosedError):
            runtime.execute("read_file", ReadFileArguments(path="README.md"))
        with self.assertRaises(ExecutionRuntimeClosedError):
            runtime.execute_authorized("write_file", write, lambda: True)
        self.assertEqual(self.sandbox.kill_calls, 0)

    def test_command_and_code_authorizations_have_distinct_kinds(self):
        runtime = self.remote_runtime()
        command = BashShellArguments(command="compileall", paths=["app"])
        code = CodeInterpreterArguments(code="1+1")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime._issue_authorization("bash_shell", command, "policy")
        with self.assertRaises(RemoteExecutionAuthorizationDenied):
            runtime._issue_authorization("code_interpreter", code, "approval")
        runtime.execute_authorized("bash_shell", command, lambda: True)
        runtime.execute_authorized("code_interpreter", code, lambda: True)
        runtime.close()

    def test_prepared_invocation_is_exact_one_shot_and_revocable_before_start(self):
        runtime = self.remote_runtime()
        code = CodeInterpreterArguments(code="1+1")
        prepared = runtime.prepare_async_authorized(
            "code_interpreter", code, lambda: True
        )

        prepared.cancel()

        with self.assertRaises(ExecutionRuntimeClosedError):
            prepared.run()
        self.assertFalse(runtime.closed)
        self.assertFalse(_FakeRemoteGroup.instances[0].calls)
        runtime.close()

    def test_concurrent_approvals_issue_distinct_one_shot_permits(self):
        runtime = self.remote_runtime()
        barrier = threading.Barrier(3)
        prepared: dict[str, object] = {}
        errors: list[BaseException] = []
        arguments = {
            "first": BashShellArguments(command="compileall", paths=["app"]),
            "second": BashShellArguments(command="unittest", paths=["tests"]),
        }

        def prepare(label: str) -> None:
            try:
                prepared[label] = runtime.prepare_async_authorized(
                    "bash_shell",
                    arguments[label],
                    lambda: barrier.wait(timeout=2) is not None,
                )
            except BaseException as error:
                errors.append(error)

        workers = [
            threading.Thread(target=prepare, args=(label,))
            for label in ("first", "second")
        ]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(2)

        self.assertFalse(errors)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        first = prepared["first"]
        second = prepared["second"]
        self.assertNotEqual(first._permit.nonce, second._permit.nonce)

        first.cancel()
        with self.assertRaises(ExecutionRuntimeClosedError):
            first.run()
        self.assertEqual(second.run(), arguments["second"])
        with self.assertRaises(ExecutionRuntimeError):
            second.run()
        self.assertEqual(
            [call[1] for call in _FakeRemoteGroup.instances[0].calls],
            [arguments["second"]],
        )
        runtime.close()

    def test_dispatch_failure_before_handle_fences_and_kills_sandbox(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.fail_after_dispatch_tool = "code_interpreter"
        prepared = runtime.prepare_async_authorized(
            "code_interpreter",
            CodeInterpreterArguments(code="1+1"),
            lambda: True,
        )

        with self.assertRaisesRegex(OSError, "provider disconnected"):
            prepared.run()

        self.assertTrue(runtime.closed)
        self.assertEqual(self.sandbox.kill_calls, 1)
        with self.assertRaises(ExecutionRuntimeClosedError):
            runtime.execute("read_file", ReadFileArguments(path="README.md"))

    def test_sync_authorized_code_uses_same_dispatch_fence(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.fail_after_dispatch_tool = "code_interpreter"

        with self.assertRaisesRegex(OSError, "provider disconnected"):
            runtime.execute_authorized(
                "code_interpreter",
                CodeInterpreterArguments(code="1+1"),
                lambda: True,
            )

        self.assertTrue(runtime.closed)
        self.assertEqual(self.sandbox.kill_calls, 1)

    def test_system_error_after_dispatch_before_handle_also_fences(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.system_error_after_dispatch_tool = "code_interpreter"

        with self.assertRaisesRegex(KeyboardInterrupt, "provider interrupted"):
            runtime.execute_authorized(
                "code_interpreter",
                CodeInterpreterArguments(code="1+1"),
                lambda: True,
            )

        self.assertTrue(runtime.closed)
        self.assertEqual(self.sandbox.kill_calls, 1)

    def test_cancel_during_dispatch_without_handle_fences_and_kills_sandbox(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.release_dispatch.clear()
        prepared = runtime.prepare_async_authorized(
            "code_interpreter",
            CodeInterpreterArguments(code="1+1"),
            lambda: True,
        )
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                prepared.run()
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(group.dispatch_started.wait(1))

        prepared.cancel()
        group.release_dispatch.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(runtime.closed)
        self.assertEqual(self.sandbox.kill_calls, 1)
        self.assertTrue(errors)

    def test_cancel_after_handle_bind_kills_handle_and_rejects_late_result(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.release_after_bind.clear()
        prepared = runtime.prepare_async_authorized(
            "code_interpreter",
            CodeInterpreterArguments(code="1+1"),
            lambda: True,
        )
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                prepared.run()
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(group.handle_bound.wait(1))

        prepared.cancel()
        group.release_after_bind.set()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(group.handle.cancel_calls, 1)
        self.assertEqual(self.sandbox.kill_calls, 0)
        self.assertTrue(any(isinstance(error, ExecutionRuntimeClosedError) for error in errors))
        runtime.close()

    def test_cancel_failure_fences_before_best_effort_manager_can_swallow_it(self):
        runtime = self.remote_runtime()
        group = _FakeRemoteGroup.instances[0]
        group.release_after_bind.clear()
        group.handle.cancel_error = OSError("kill failed")
        prepared = runtime.prepare_async_authorized(
            "code_interpreter",
            CodeInterpreterArguments(code="1+1"),
            lambda: True,
        )
        worker = threading.Thread(target=lambda: self._run_ignoring_error(prepared))
        worker.start()
        self.assertTrue(group.handle_bound.wait(1))

        prepared.cancel()
        group.release_after_bind.set()
        worker.join(2)

        self.assertTrue(runtime.closed)
        self.assertEqual(self.sandbox.kill_calls, 1)

    @staticmethod
    def _run_ignoring_error(prepared) -> None:
        try:
            prepared.run()
        except Exception:
            pass


class ExecutionToolRuntimeIntegrationTests(unittest.TestCase):
    def test_real_group_consumes_runtime_permit_without_exposing_boolean_bypass(self):
        sandbox = _FakeSandbox()
        transport = _MinimalTransport()
        backend = AliyunSandboxExecutionBackend(
            "c" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lambda candidate: transport,
        )
        runtime = ExecutionToolRuntime(backend)
        try:
            read = runtime.execute(
                "read_file", ReadFileArguments(path="README.md", max_bytes=10)
            )
            with self.assertRaises(RemoteExecutionAuthorizationDenied):
                runtime.execute_authorized(
                    "write_file",
                    WriteFileArguments(path="new.txt", content="x", mode="create"),
                    lambda: True,
                )
            self.assertEqual(read.content, "ok")
            self.assertEqual(
                transport.calls,
                [
                    ("read_file", "/workspace/agnet-study/" + "c" * 32 + "/README.md"),
                ],
            )
        finally:
            runtime.close()
        self.assertEqual(sandbox.kill_calls, 1)


if __name__ == "__main__":
    unittest.main()
