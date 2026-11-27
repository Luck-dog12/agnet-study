"""Stage 5B2a Registry routing tests for session execution runtimes."""

from __future__ import annotations

from dataclasses import replace
import importlib
import json
import unittest
from unittest.mock import Mock, patch

from pydantic import SecretStr

from app.aliyun_sandbox_tools import (
    RemoteSandboxPolicyDenied,
    RemoteSandboxToolError,
)
from app.execution_backend import (
    AliyunSandboxExecutionBackend as _AliyunSandboxExecutionBackend,
)
from app.execution_runtime import (
    ExecutionRuntimeClosedError,
    ExecutionToolRuntime,
    RemoteExecutionAuthorizationDenied,
)
from app.observability import Observability
from app.sandbox_config import SandboxSettings
from app.sandbox_transport import SandboxCommandResult
from app.schemas import (
    ApplyPendingChangeOutput,
    CodeInterpreterOutput,
    GrepOutput,
    ListFilesOutput,
    ListPendingChangesOutput,
    ReadFileOutput,
    ToolCall,
    ToolMessage,
    WebSearchOutput,
)
from app.workflow import FileApprovalWorkflow
from tool.policy import PolicyLoadError, load_tool_policy
from tool.registry import AsyncToolPreparationError, call_tool, prepare_async_tool_call
from tool import registry
from tests.sandbox_cleanup_fakes import empty_recovery_provider


def AliyunSandboxExecutionBackend(*args, **kwargs):
    kwargs.setdefault("automatic_maintenance", False)
    kwargs.setdefault("recovery_provider", empty_recovery_provider())
    if "sandbox_factory" in kwargs and "persistence_sandbox_factory" not in kwargs:
        class PersistenceSandbox:
            sandbox_id = "registry-persistence-sandbox"

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


REMOTE_ERROR = "远端工作区工具执行失败。"
REMOTE_DENIED = "当前阶段不允许远端特权工作区工具执行。"
REMOTE_CODE_DENIED = "远端 Python 代码未通过宿主安全策略或授权。"
REMOTE_PATH_DENIED = "远端工作区路径权限检查未通过。"
REMOTE_PENDING_DENIED = "远端会话不能访问宿主本地文件候选。"

code_interpreter_module = importlib.import_module("tool.code_interpreter")


def enabled_settings() -> SandboxSettings:
    return SandboxSettings(
        enabled=True,
        api_key=SecretStr("registry-runtime-test-key"),
        api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        domain="cn-hangzhou.e2b.fc.aliyuncs.com",
        template="code-interpreter-v1",
        timeout_seconds=300,
        oss_bucket="agent-probe-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket_path="/projects/test/runs/registry-code",
        oss_mount_dir="/mnt/oss",
        oss_role_arn="acs:ram::1234567890123456:role/registry-code-test",
        oss_read_only=False,
    )


class _CodeSandbox:
    sandbox_id = "registry-code-sandbox"

    def __init__(self) -> None:
        self.kill_calls = 0

    def kill(self) -> bool:
        self.kill_calls += 1
        return True


class _CodeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.result: object = SandboxCommandResult(
            exit_code=0,
            stdout="",
            stderr="",
            result="2",
            truncated=False,
        )

    def run(
        self,
        command: str,
        *,
        paths,
        code: str | None,
        cwd: str,
        timeout_seconds: int,
        max_output_bytes: int,
        max_output_characters: int | None = None,
        max_memory_mb: int | None = None,
        max_processes: int | None = None,
    ):
        self.calls.append(
            (
                "run",
                (
                    command,
                    paths,
                    code,
                    cwd,
                    timeout_seconds,
                    max_output_bytes,
                    max_output_characters,
                    max_memory_mb,
                    max_processes,
                ),
            )
        )
        return self.result

    class _Handle:
        def __init__(self, owner: "_CodeTransport") -> None:
            self._owner = owner
            self.cancel_calls = 0

        def wait(self):
            return self._owner.result

        def cancel(self) -> None:
            self.cancel_calls += 1

    def start_run(
        self,
        command: str,
        *,
        paths,
        code: str | None,
        cwd: str,
        timeout_seconds: int,
        max_output_bytes: int,
        max_output_characters: int | None = None,
        max_memory_mb: int | None = None,
        max_processes: int | None = None,
        dispatch_attempt_consumer=None,
    ):
        if dispatch_attempt_consumer is None:
            raise AssertionError("missing dispatch boundary")
        dispatch_attempt_consumer()
        self.calls.append(
            (
                "start_run",
                (
                    command,
                    paths,
                    code,
                    cwd,
                    timeout_seconds,
                    max_output_bytes,
                    max_output_characters,
                    max_memory_mb,
                    max_processes,
                ),
            )
        )
        return self._Handle(self)


def result_from(raw: str) -> dict[str, object]:
    return json.loads(ToolMessage.model_validate_json(raw).model_dump_json())["result"]


def remote_runtime() -> Mock:
    runtime = Mock(spec=ExecutionToolRuntime)
    runtime.backend_name = "aliyun-sandbox"
    runtime.route_for.return_value = "aliyun-sandbox"
    return runtime


def real_remote_code_runtime() -> tuple[
    ExecutionToolRuntime, _CodeSandbox, _CodeTransport
]:
    sandbox = _CodeSandbox()
    transport = _CodeTransport()
    backend = AliyunSandboxExecutionBackend(
        "c" * 32,
        enabled_settings(),
        sandbox_factory=lambda _settings, _metadata: sandbox,
        transport_factory=lambda _candidate: transport,
    )
    return ExecutionToolRuntime(backend), sandbox, transport


class RegistryExecutionRuntimeTests(unittest.TestCase):
    def test_three_read_only_tools_route_through_the_same_runtime_and_validate_output(self) -> None:
        runtime = remote_runtime()
        outputs = {
            "read_file": ReadFileOutput(
                path="a.txt",
                encoding="utf-8",
                content="ok",
                offset=0,
                bytes_read=2,
                total_bytes=2,
                truncated=False,
            ),
            "list_files": ListFilesOutput(
                pattern="**/*", entries=[], count=0, truncated=False
            ),
            "grep": GrepOutput(
                pattern="needle",
                file_pattern="**/*",
                case_sensitive=True,
                matches=[],
                count=0,
                files_searched=0,
                truncated=False,
            ),
        }
        runtime.execute.side_effect = lambda name, _arguments: outputs[name]
        arguments = {
            "read_file": {"path": "a.txt", "max_bytes": 10},
            "list_files": {"pattern": "**/*"},
            "grep": {"pattern": "needle"},
        }
        replacements = {}
        validators = {}
        handlers = {}
        for name in outputs:
            registration = registry._REGISTRY[name]
            validators[name] = Mock(wraps=registration.output_validator)
            handlers[name] = Mock(side_effect=AssertionError("local handler called"))
            replacements[name] = replace(
                registration,
                handler=handlers[name],
                output_validator=validators[name],
            )

        with patch.dict(registry._REGISTRY, replacements):
            for index, name in enumerate(outputs):
                raw = call_tool(
                    ToolCall(id=f"remote_{index}", name=name, arguments=arguments[name]),
                    execution_runtime=runtime,
                )
                self.assertEqual(result_from(raw)["status"], "success")

        self.assertEqual(
            [call.args[0] for call in runtime.route_for.call_args_list],
            ["read_file", "list_files", "grep"],
        )
        self.assertEqual(
            [call.args[0] for call in runtime.execute.call_args_list],
            ["read_file", "list_files", "grep"],
        )
        for name in outputs:
            handlers[name].assert_not_called()
            validators[name].assert_called_once()

    def test_remote_failure_closed_and_bad_output_are_fixed_errors_without_fallback(self) -> None:
        cases = (
            OSError("SECRET provider failure"),
            ExecutionRuntimeClosedError("SECRET closed detail"),
            RemoteSandboxToolError("SECRET protected path from malicious transport"),
            {"path": "different.txt", "content": "SECRET"},
        )
        for index, outcome in enumerate(cases):
            with self.subTest(index=index):
                runtime = remote_runtime()
                if isinstance(outcome, Exception):
                    runtime.execute.side_effect = outcome
                else:
                    runtime.execute.return_value = outcome
                handler = Mock(side_effect=AssertionError("local fallback"))
                registration = replace(registry._REGISTRY["read_file"], handler=handler)
                with patch.dict(registry._REGISTRY, {"read_file": registration}):
                    result = result_from(
                        call_tool(
                            ToolCall(
                                id=f"failure_{index}",
                                name="read_file",
                                arguments={"path": "a.txt"},
                            ),
                            execution_runtime=runtime,
                        )
                    )
                self.assertEqual(result, {"status": "error", "output": "", "error": REMOTE_ERROR})
                self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
                handler.assert_not_called()

    def test_remote_read_only_policy_denial_is_fixed_path_error_without_fallback(self) -> None:
        calls = (
            ("read_file", {"path": "a.txt"}),
            ("list_files", {"pattern": "**/*"}),
            ("grep", {"pattern": "needle", "file_pattern": "**/*"}),
        )
        for index, (name, arguments) in enumerate(calls):
            with self.subTest(tool=name):
                runtime = remote_runtime()
                runtime.execute.side_effect = RemoteSandboxPolicyDenied(
                    "SECRET provider returned .envrc"
                )
                handler = Mock(side_effect=AssertionError("local fallback"))
                registration = replace(registry._REGISTRY[name], handler=handler)
                with patch.dict(registry._REGISTRY, {name: registration}):
                    result = result_from(
                        call_tool(
                            ToolCall(
                                id=f"read_only_policy_denied_{index}",
                                name=name,
                                arguments=arguments,
                            ),
                            permission_mode="full-access",
                            execution_runtime=runtime,
                        )
                    )
                self.assertEqual(
                    result,
                    {
                        "status": "denied",
                        "output": "",
                        "error": REMOTE_PATH_DENIED,
                    },
                )
                self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
                self.assertNotEqual(result["error"], REMOTE_CODE_DENIED)
                handler.assert_not_called()

    def test_remote_mutations_require_workflow_and_non_format_bash_remains_sync_denied(self) -> None:
        runtime = remote_runtime()
        calls = {
            "write_file": {"path": "new.txt", "content": "x", "mode": "create"},
            "editor_file": {
                "path": "a.txt",
                "old_text": "old",
                "new_text": "new",
                "expected_replacements": 1,
            },
            "bash_shell": {"command": "compileall", "paths": ["app"]},
        }
        handlers = {}
        replacements = {}
        for name in calls:
            handlers[name] = Mock(side_effect=AssertionError("local privileged handler called"))
            replacements[name] = replace(registry._REGISTRY[name], handler=handlers[name])

        with (
            patch.dict(registry._REGISTRY, replacements),
            patch.object(registry, "_prepare_existing_mutation") as existing,
            patch.object(registry, "_prepare_new_mutation") as new,
        ):
            for index, (name, arguments) in enumerate(calls.items()):
                result = result_from(
                    call_tool(
                        ToolCall(id=f"privileged_{index}", name=name, arguments=arguments),
                        execution_runtime=runtime,
                    )
                )
                self.assertEqual(result["status"], "denied")
                if name == "bash_shell":
                    self.assertEqual(result["error"], REMOTE_DENIED)
            existing.assert_not_called()
            new.assert_not_called()
        runtime.execute.assert_not_called()
        runtime.execute_authorized.assert_not_called()
        for handler in handlers.values():
            handler.assert_not_called()

    def test_remote_format_json_registry_gates_precede_candidate_generation(self) -> None:
        runtime = remote_runtime()
        handler = Mock(side_effect=AssertionError("local fallback"))
        registration = replace(registry._REGISTRY["bash_shell"], handler=handler)
        invalid_paths = (
            ["../escape.json"],
            ["notes.txt"],
            [".env.json"],
            [".agent/config.json"],
            ["same.json", "same.json"],
        )
        with patch.dict(registry._REGISTRY, {"bash_shell": registration}):
            for index, paths in enumerate(invalid_paths):
                with self.subTest(paths=paths):
                    result = result_from(call_tool(
                        ToolCall(
                            id=f"invalid_format_{index}",
                            name="bash_shell",
                            arguments={"command": "format_json", "paths": paths},
                        ),
                        permission_mode="full-access",
                        execution_runtime=runtime,
                        workflow=FileApprovalWorkflow(),
                        confirmation_callback=lambda _request: True,
                    ))
                    self.assertEqual(result["status"], "denied")

            policy = load_tool_policy()
            restricted = policy.model_copy(update={
                "file_access": policy.file_access.model_copy(update={
                    "allowed_path_patterns": ["src/**"],
                    "protected_relative_paths": [
                        *policy.file_access.protected_relative_paths,
                        "src/private",
                    ],
                }),
                "shell_commands": {
                    **policy.shell_commands,
                    "format_json": policy.shell_commands["format_json"].model_copy(
                        update={"max_paths": 1}
                    ),
                },
            })
            with patch.object(registry, "load_tool_policy", return_value=restricted):
                for index, paths in enumerate((
                    ["outside.json"],
                    ["src/private/value.json"],
                    ["src/a.json", "src/b.json"],
                )):
                    with self.subTest(policy_paths=paths):
                        result = result_from(call_tool(
                            ToolCall(
                                id=f"policy_format_{index}",
                                name="bash_shell",
                                arguments={"command": "format_json", "paths": paths},
                            ),
                            permission_mode="partial-control",
                            execution_runtime=runtime,
                            workflow=FileApprovalWorkflow(),
                            confirmation_callback=lambda _request: True,
                        ))
                        self.assertEqual(result["status"], "denied")

            for index, kwargs in enumerate((
                {"workflow": FileApprovalWorkflow()},
                {"confirmation_callback": lambda _request: True},
            )):
                with self.subTest(missing_approval_seam=index):
                    result = result_from(call_tool(
                        ToolCall(
                            id=f"missing_format_approval_{index}",
                            name="bash_shell",
                            arguments={
                                "command": "format_json",
                                "paths": ["ordinary.json"],
                            },
                        ),
                        permission_mode="full-access",
                        execution_runtime=runtime,
                        **kwargs,
                    ))
                    self.assertEqual(result["status"], "denied")

        runtime.prepare_remote_command_mutation.assert_not_called()
        runtime.execute.assert_not_called()
        handler.assert_not_called()

    def test_format_json_is_never_classified_as_async(self) -> None:
        metadata = registry.get_tool_execution_metadata(
            "bash_shell",
            {"command": "format_json", "paths": ["a.json"]},
        )
        self.assertFalse(metadata.run_async)
        self.assertFalse(metadata.cancellable)

    def test_remote_code_uses_real_authorized_runtime_and_double_policy_check(self) -> None:
        runtime, sandbox, transport = real_remote_code_runtime()
        policy = load_tool_policy()
        low_output_policy = policy.model_copy(
            update={
                "code_interpreter": policy.code_interpreter.model_copy(
                    update={
                        "max_output_characters": 1024,
                        "max_memory_mb": 64,
                        "max_processes": 1,
                    }
                )
            }
        )
        handler = Mock(side_effect=AssertionError("local code interpreter called"))
        validator = Mock(
            wraps=registry._REGISTRY["code_interpreter"].output_validator
        )
        registration = replace(
            registry._REGISTRY["code_interpreter"],
            handler=handler,
            output_validator=validator,
        )
        try:
            with (
                patch.dict(registry._REGISTRY, {"code_interpreter": registration}),
                patch.object(
                    code_interpreter_module,
                    "load_tool_policy",
                    side_effect=[policy, low_output_policy],
                ) as policy_loader,
                patch.object(
                    runtime,
                    "execute",
                    side_effect=AssertionError("unauthorized execute used"),
                ) as unprivileged,
                patch.object(
                    runtime,
                    "execute_authorized",
                    wraps=runtime.execute_authorized,
                ) as authorized,
            ):
                result = result_from(
                    call_tool(
                        ToolCall(
                            id="remote_code_success",
                            name="code_interpreter",
                            arguments={"code": "1 + 1", "timeout_seconds": 3},
                        ),
                        execution_runtime=runtime,
                    )
                )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["output"]["result"], "2")  # type: ignore[index]
            authorized.assert_called_once()
            self.assertEqual(authorized.call_args.args[0], "code_interpreter")
            self.assertTrue(callable(authorized.call_args.args[2]))
            unprivileged.assert_not_called()
            handler.assert_not_called()
            validator.assert_called_once()
            self.assertEqual(policy_loader.call_count, 2)
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.calls[0][1][0], "python")  # type: ignore[index]
            self.assertEqual(transport.calls[0][1][6], 1024)  # type: ignore[index]
            self.assertEqual(
                transport.calls[0][1][7],
                low_output_policy.code_interpreter.max_memory_mb,
            )  # type: ignore[index]
            self.assertEqual(
                transport.calls[0][1][8],
                low_output_policy.code_interpreter.max_processes,
            )  # type: ignore[index]
        finally:
            runtime.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_remote_code_policy_and_ast_rejections_never_reach_transport(self) -> None:
        policy = load_tool_policy()
        disabled = policy.model_copy(
            update={
                "code_interpreter": policy.code_interpreter.model_copy(
                    update={"enabled": False}
                )
            }
        )
        short_timeout = policy.model_copy(
            update={
                "code_interpreter": policy.code_interpreter.model_copy(
                    update={"max_timeout_seconds": 1}
                )
            }
        )
        small_ast = policy.model_copy(
            update={
                "code_interpreter": policy.code_interpreter.model_copy(
                    update={"max_ast_nodes": 32}
                )
            }
        )
        cases = (
            ("missing", "1 + 1", 1, PolicyLoadError("SECRET missing")),
            ("disabled", "1 + 1", 1, disabled),
            ("timeout", "1 + 1", 5, short_timeout),
            ("import", "import os", 1, policy),
            ("nodes", "value = " + "+".join(["1"] * 80), 1, small_ast),
        )
        handler = Mock(side_effect=AssertionError("local code interpreter called"))
        registration = replace(registry._REGISTRY["code_interpreter"], handler=handler)
        for index, (name, code, timeout, policy_or_error) in enumerate(cases):
            runtime, sandbox, transport = real_remote_code_runtime()
            policy_patch = (
                patch.object(
                    code_interpreter_module,
                    "load_tool_policy",
                    side_effect=policy_or_error,
                )
                if isinstance(policy_or_error, Exception)
                else patch.object(
                    code_interpreter_module,
                    "load_tool_policy",
                    return_value=policy_or_error,
                )
            )
            try:
                with patch.dict(
                    registry._REGISTRY, {"code_interpreter": registration}
                ), policy_patch:
                    result = result_from(
                        call_tool(
                            ToolCall(
                                id=f"remote_code_policy_{index}",
                                name="code_interpreter",
                                arguments={
                                    "code": code,
                                    "timeout_seconds": timeout,
                                },
                            ),
                            execution_runtime=runtime,
                        )
                    )
                self.assertEqual(
                    result,
                    {
                        "status": "denied",
                        "output": "",
                        "error": REMOTE_CODE_DENIED,
                    },
                    name,
                )
                self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
                self.assertEqual(transport.calls, [])
            finally:
                runtime.close()
            self.assertEqual(sandbox.kill_calls, 0)
        handler.assert_not_called()

    def test_remote_code_group_rechecks_policy_after_permit_is_issued(self) -> None:
        policy = load_tool_policy()
        disabled = policy.model_copy(
            update={
                "code_interpreter": policy.code_interpreter.model_copy(
                    update={"enabled": False}
                )
            }
        )
        runtime, sandbox, transport = real_remote_code_runtime()
        handler = Mock(side_effect=AssertionError("local code interpreter called"))
        registration = replace(registry._REGISTRY["code_interpreter"], handler=handler)
        try:
            with (
                patch.dict(registry._REGISTRY, {"code_interpreter": registration}),
                patch.object(
                    code_interpreter_module,
                    "load_tool_policy",
                    side_effect=[policy, disabled],
                ) as policy_loader,
            ):
                result = result_from(
                    call_tool(
                        ToolCall(
                            id="remote_code_policy_changed",
                            name="code_interpreter",
                            arguments={"code": "1 + 1"},
                        ),
                        execution_runtime=runtime,
                    )
                )
            self.assertEqual(
                result,
                {
                    "status": "denied",
                    "output": "",
                    "error": REMOTE_CODE_DENIED,
                },
            )
            self.assertEqual(policy_loader.call_count, 2)
            self.assertEqual(transport.calls, [])
            handler.assert_not_called()
        finally:
            runtime.close()
        self.assertEqual(sandbox.kill_calls, 0)

    def test_remote_code_failure_classes_are_fixed_and_never_fall_back(self) -> None:
        valid_output = CodeInterpreterOutput(
            exit_code=0,
            stdout="",
            result="2",
            stderr="",
            truncated=False,
        )

        def after_policy(outcome):
            def execute(_name, _arguments, callback):
                self.assertIs(callback(), True)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            return execute

        cases = (
            (
                "authorization",
                RemoteExecutionAuthorizationDenied("SECRET unauthorized"),
                "denied",
                REMOTE_CODE_DENIED,
                False,
            ),
            (
                "policy_second_check",
                RemoteSandboxPolicyDenied("SECRET changed"),
                "denied",
                REMOTE_CODE_DENIED,
                True,
            ),
            (
                "closed",
                ExecutionRuntimeClosedError("SECRET closed"),
                "error",
                REMOTE_ERROR,
                False,
            ),
            ("transport", OSError("SECRET provider"), "error", REMOTE_ERROR, True),
            ("bad_output", {"result": "SECRET"}, "error", REMOTE_ERROR, True),
        )
        for index, (name, outcome, status, error, runs_policy) in enumerate(cases):
            runtime = remote_runtime()
            runtime.execute_authorized.side_effect = (
                after_policy(outcome) if runs_policy else outcome
            )
            handler = Mock(side_effect=AssertionError("local fallback"))
            registration = replace(
                registry._REGISTRY["code_interpreter"], handler=handler
            )
            with patch.dict(
                registry._REGISTRY, {"code_interpreter": registration}
            ):
                result = result_from(
                    call_tool(
                        ToolCall(
                            id=f"remote_code_failure_{index}",
                            name="code_interpreter",
                            arguments={"code": "1 + 1"},
                        ),
                        execution_runtime=runtime,
                    )
                )
            self.assertEqual(
                result,
                {"status": status, "output": "", "error": error},
                name,
            )
            self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))
            handler.assert_not_called()
            runtime.execute.assert_not_called()

        runtime = remote_runtime()
        runtime.execute_authorized.side_effect = after_policy(valid_output)
        handler = Mock(side_effect=AssertionError("local fallback"))
        validator = Mock(side_effect=ValueError("SECRET validator"))
        registration = replace(
            registry._REGISTRY["code_interpreter"],
            handler=handler,
            output_validator=validator,
        )
        with patch.dict(registry._REGISTRY, {"code_interpreter": registration}):
            result = result_from(
                call_tool(
                    ToolCall(
                        id="remote_code_validator_failure",
                        name="code_interpreter",
                        arguments={"code": "1 + 1"},
                    ),
                    execution_runtime=runtime,
                )
            )
        self.assertEqual(
            result,
            {"status": "error", "output": "", "error": REMOTE_ERROR},
        )
        handler.assert_not_called()

    def test_none_and_local_runtime_preserve_exact_local_result(self) -> None:
        output = ReadFileOutput(
            path="a.txt",
            encoding="utf-8",
            content="local",
            offset=0,
            bytes_read=5,
            total_bytes=5,
            truncated=False,
        )
        handler = Mock(return_value=output)
        registration = replace(registry._REGISTRY["read_file"], handler=handler)
        call = ToolCall(id="local_1", name="read_file", arguments={"path": "a.txt"})
        local_runtime = Mock(spec=ExecutionToolRuntime)
        local_runtime.route_for.return_value = "local-registry"
        with patch.dict(registry._REGISTRY, {"read_file": registration}):
            baseline = call_tool(call)
            explicit_none = call_tool(call, execution_runtime=None)
            routed_local = call_tool(call, execution_runtime=local_runtime)
        self.assertEqual(baseline, explicit_none)
        self.assertEqual(baseline, routed_local)
        self.assertEqual(handler.call_count, 3)
        local_runtime.execute.assert_not_called()

    def test_non_workspace_tool_ignores_remote_runtime(self) -> None:
        runtime = remote_runtime()
        output = WebSearchOutput(results=[], more_results=False)
        handler = Mock(return_value=output)
        registration = replace(registry._REGISTRY["web_search"], handler=handler)
        with patch.dict(registry._REGISTRY, {"web_search": registration}):
            result = result_from(
                call_tool(
                    ToolCall(
                        id="search_1",
                        name="web_search",
                        arguments={"query": "runtime isolation"},
                    ),
                    execution_runtime=runtime,
                )
            )
        self.assertEqual(result["status"], "success")
        handler.assert_called_once()
        runtime.route_for.assert_not_called()
        runtime.execute.assert_not_called()

    def test_remote_pending_tools_are_denied_without_touching_host_storage(self) -> None:
        runtime = remote_runtime()
        pending_id = "12345678123442348234123456789abc"
        calls = {
            "list_pending_changes": {},
            "apply_pending_change": {"pending_file_id": pending_id},
        }
        handlers: dict[str, Mock] = {}
        replacements = {}
        for name in calls:
            handlers[name] = Mock(side_effect=AssertionError("host pending accessed"))
            replacements[name] = replace(registry._REGISTRY[name], handler=handlers[name])

        with patch.dict(registry._REGISTRY, replacements):
            for index, (name, arguments) in enumerate(calls.items()):
                result = result_from(
                    call_tool(
                        ToolCall(
                            id=f"remote_pending_{index}",
                            name=name,
                            arguments=arguments,
                        ),
                        execution_runtime=runtime,
                    )
                )
                self.assertEqual(
                    result,
                    {
                        "status": "denied",
                        "output": "",
                        "error": REMOTE_PENDING_DENIED,
                    },
                )

        for handler in handlers.values():
            handler.assert_not_called()
        runtime.route_for.assert_not_called()
        runtime.execute.assert_not_called()

    def test_none_and_local_runtime_preserve_pending_tool_behavior(self) -> None:
        pending_id = "12345678123442348234123456789abc"
        outputs = {
            "list_pending_changes": ListPendingChangesOutput(
                entries=[], count=0, truncated=False
            ),
            "apply_pending_change": ApplyPendingChangeOutput(
                pending_file_id=pending_id,
                path="src/generated.py",
                source_tool="write_file",
                operation="created",
                encoding="utf-8",
                bytes_written=0,
                sha256=("e3b0c44298fc1c149afbf4c8996fb924"
                        "27ae41e4649b934ca495991b7852b855"),
            ),
        }
        calls = {
            "list_pending_changes": {},
            "apply_pending_change": {"pending_file_id": pending_id},
        }
        local_runtime = Mock(spec=ExecutionToolRuntime)
        local_runtime.backend_name = "local"
        local_runtime.route_for.return_value = "local-registry"
        handlers: dict[str, Mock] = {}
        replacements = {}
        for name, output in outputs.items():
            handlers[name] = Mock(return_value=output)
            replacements[name] = replace(registry._REGISTRY[name], handler=handlers[name])

        with patch.dict(registry._REGISTRY, replacements):
            for index, (name, arguments) in enumerate(calls.items()):
                call = ToolCall(id=f"local_pending_{index}", name=name, arguments=arguments)
                baseline = call_tool(call)
                explicit_none = call_tool(call, execution_runtime=None)
                routed_local = call_tool(call, execution_runtime=local_runtime)
                self.assertEqual(baseline, explicit_none)
                self.assertEqual(baseline, routed_local)

        for handler in handlers.values():
            self.assertEqual(handler.call_count, 3)
        local_runtime.route_for.assert_not_called()
        local_runtime.execute.assert_not_called()

    def test_remote_read_file_uses_lexical_posix_and_host_policy_only(self) -> None:
        runtime = remote_runtime()
        runtime.execute.side_effect = lambda _name, arguments: ReadFileOutput(
            path=arguments.path,
            encoding="utf-8",
            content="remote",
            offset=arguments.offset,
            bytes_read=6,
            total_bytes=6,
            truncated=False,
        )
        handler = Mock(side_effect=AssertionError("local read handler called"))
        registration = replace(registry._REGISTRY["read_file"], handler=handler)
        invalid_paths = (
            "/etc/passwd",
            "../outside.txt",
            "nested/../outside.txt",
            "nested\\file.txt",
            "nested//file.txt",
            "C:/outside.txt",
            "name\x00.txt",
            "name\x7f.txt",
            "a" * 4097,
        )

        with (
            patch.dict(registry._REGISTRY, {"read_file": registration}),
            patch.object(registry.Path, "resolve", side_effect=AssertionError("host resolved")),
        ):
            for index, path in enumerate(invalid_paths):
                with self.subTest(path=path):
                    result = result_from(
                        call_tool(
                            ToolCall(
                                id=f"invalid_remote_path_{index}",
                                name="read_file",
                                arguments={"path": path},
                            ),
                            permission_mode="full-access",
                            execution_runtime=runtime,
                        )
                    )
                    self.assertEqual(
                        result,
                        {
                            "status": "denied",
                            "output": "",
                            "error": REMOTE_PATH_DENIED,
                        },
                    )

            for mode in ("partial-control", "full-access"):
                for index, path in enumerate(
                    (".env", ".envrc", "tls/server.key", "credentials", "token")
                ):
                    with self.subTest(mode=mode, sealed_path=path):
                        protected = result_from(
                            call_tool(
                                ToolCall(
                                    id=f"{mode}_{index}",
                                    name="read_file",
                                    arguments={"path": path},
                                ),
                                permission_mode=mode,
                                execution_runtime=runtime,
                            )
                        )
                        self.assertEqual(protected["status"], "denied")

        runtime.execute.assert_not_called()
        handler.assert_not_called()

    def test_remote_full_access_sealed_scan_and_mutation_never_dispatch_or_fallback(self) -> None:
        runtime = remote_runtime()
        handlers: dict[str, Mock] = {}
        replacements = {}
        for name in ("list_files", "grep", "write_file", "editor_file"):
            handlers[name] = Mock(side_effect=AssertionError("local fallback"))
            replacements[name] = replace(
                registry._REGISTRY[name], handler=handlers[name]
            )
        calls = (
            ToolCall(
                id="sealed_list", name="list_files", arguments={"pattern": ".env*"}
            ),
            ToolCall(
                id="sealed_grep",
                name="grep",
                arguments={"pattern": "needle", "file_pattern": "**/*.key"},
            ),
            ToolCall(
                id="sealed_write",
                name="write_file",
                arguments={"path": "credentials", "content": "x", "mode": "create"},
            ),
            ToolCall(
                id="sealed_edit",
                name="editor_file",
                arguments={
                    "path": "token",
                    "operation": "replace_text",
                    "old_text": "x",
                    "new_text": "y",
                    "expected_replacements": 1,
                },
            ),
        )
        with patch.dict(registry._REGISTRY, replacements):
            for call in calls:
                with self.subTest(tool=call.name):
                    result = result_from(
                        call_tool(
                            call,
                            permission_mode="full-access",
                            execution_runtime=runtime,
                            workflow=FileApprovalWorkflow(),
                            confirmation_callback=lambda _request: True,
                        )
                    )
                    self.assertEqual(result["status"], "denied")
        runtime.execute.assert_not_called()
        runtime.prepare_remote_mutation.assert_not_called()
        for handler in handlers.values():
            handler.assert_not_called()

    def test_remote_full_access_async_bash_rejects_sealed_path_before_dispatch(self) -> None:
        runtime = remote_runtime()

        def authorize(_name, _arguments, callback):
            callback()
            raise AssertionError("provider preparation reached")

        runtime.prepare_async_authorized.side_effect = authorize
        with self.assertRaises(AsyncToolPreparationError) as raised:
            prepare_async_tool_call(
                ToolCall(
                    id="sealed_async_bash",
                    name="bash_shell",
                    arguments={"command": "unittest", "paths": ["credentials"]},
                ),
                permission_mode="full-access",
                execution_runtime=runtime,
                confirmation_callback=lambda _request: True,
            )
        self.assertEqual(raised.exception.result.status, "denied")
        runtime.execute.assert_not_called()

    def test_remote_scan_rejects_sealed_provider_output_without_local_fallback(self) -> None:
        runtime = remote_runtime()
        outputs = (
            (
                "list_files",
                {"pattern": "**/*"},
                ListFilesOutput(
                    pattern="**/*",
                    entries=[{"path": ".envrc", "size": 1}],
                    count=1,
                    truncated=False,
                ),
            ),
            (
                "grep",
                {"pattern": "secret", "file_pattern": "**/*"},
                GrepOutput(
                    pattern="secret",
                    file_pattern="**/*",
                    case_sensitive=True,
                    matches=[
                        {
                            "path": "private/server.key",
                            "line_number": 1,
                            "column": 1,
                            "text": "secret",
                        }
                    ],
                    count=1,
                    files_searched=1,
                    truncated=False,
                ),
            ),
        )
        handlers = {}
        for index, (name, arguments, output) in enumerate(outputs):
            handler = Mock(side_effect=AssertionError("local fallback"))
            handlers[name] = handler
            registration = replace(registry._REGISTRY[name], handler=handler)
            runtime.execute.return_value = output
            with patch.dict(registry._REGISTRY, {name: registration}):
                result = result_from(
                    call_tool(
                        ToolCall(
                            id=f"sealed_provider_output_{index}",
                            name=name,
                            arguments=arguments,
                        ),
                        permission_mode="full-access",
                        execution_runtime=runtime,
                    )
                )
            self.assertEqual(result["status"], "error")
        for handler in handlers.values():
            handler.assert_not_called()

    def test_remote_partial_read_requires_allowed_policy_and_fails_closed(self) -> None:
        runtime = remote_runtime()
        handler = Mock(side_effect=AssertionError("local read handler called"))
        registration = replace(registry._REGISTRY["read_file"], handler=handler)
        default_policy = load_tool_policy()
        narrow_policy = default_policy.model_copy(
            update={
                "file_access": default_policy.file_access.model_copy(
                    update={"allowed_path_patterns": ["src/**"]}
                )
            }
        )

        with (
            patch.dict(registry._REGISTRY, {"read_file": registration}),
            patch.object(registry, "load_tool_policy", return_value=narrow_policy),
        ):
            denied = result_from(
                call_tool(
                    ToolCall(
                        id="not_allowed",
                        name="read_file",
                        arguments={"path": "README.md"},
                    ),
                    execution_runtime=runtime,
                )
            )
        with (
            patch.dict(registry._REGISTRY, {"read_file": registration}),
            patch.object(
                registry, "load_tool_policy", side_effect=PolicyLoadError("broken")
            ),
        ):
            unavailable = result_from(
                call_tool(
                    ToolCall(
                        id="policy_unavailable",
                        name="read_file",
                        arguments={"path": "src/main.py"},
                    ),
                    execution_runtime=runtime,
                )
            )

        self.assertEqual(denied["status"], "denied")
        self.assertEqual(unavailable["status"], "denied")
        runtime.execute.assert_not_called()
        handler.assert_not_called()

    def test_remote_async_workspace_calls_prepare_exact_capabilities_in_foreground(self) -> None:
        runtime = remote_runtime()
        prepared_remote = [Mock(), Mock()]
        for prepared in prepared_remote:
            prepared.run = Mock()
            prepared.cancel = Mock()

        def prepare_authorized(name, arguments, callback):
            self.assertIn(name, {"bash_shell", "code_interpreter"})
            self.assertIs(callback(), True)
            return prepared_remote.pop(0)

        runtime.prepare_async_authorized.side_effect = prepare_authorized
        calls = (
            ToolCall(
                id="async_bash",
                name="bash_shell",
                arguments={"command": "unittest", "paths": ["tests"]},
            ),
            ToolCall(
                id="async_code",
                name="code_interpreter",
                arguments={"code": "1 + 1"},
            ),
        )
        with patch.object(registry, "prepare_bash_shell_async") as prepare_bash:
            prepared_calls = [
                prepare_async_tool_call(
                    call,
                    execution_runtime=runtime,
                    confirmation_callback=lambda _request: True,
                )
                for call in calls
            ]
            prepare_bash.assert_not_called()
        self.assertEqual(runtime.prepare_async_authorized.call_count, 2)
        self.assertEqual(
            [item.call.name for item in prepared_calls],
            ["bash_shell", "code_interpreter"],
        )
        for item in prepared_calls:
            item.cancel_hook()
        runtime.execute.assert_not_called()

    def test_none_and_local_runtime_preserve_async_preparation(self) -> None:
        call = ToolCall(
            id="async_local",
            name="code_interpreter",
            arguments={"code": "1 + 1"},
        )
        local_runtime = Mock(spec=ExecutionToolRuntime)
        local_runtime.route_for.return_value = "local-registry"
        baseline = prepare_async_tool_call(call)
        explicit_none = prepare_async_tool_call(call, execution_runtime=None)
        routed_local = prepare_async_tool_call(call, execution_runtime=local_runtime)
        self.assertEqual(baseline.arguments_json, explicit_none.arguments_json)
        self.assertEqual(baseline.arguments_json, routed_local.arguments_json)
        self.assertEqual(baseline.call, routed_local.call)
        local_runtime.execute.assert_not_called()
        baseline.cancel_hook()
        explicit_none.cancel_hook()
        routed_local.cancel_hook()

    def test_observability_finishes_remote_success_and_failure(self) -> None:
        observer = Mock(spec=Observability)
        success_invocation = object()
        failure_invocation = object()
        observer.start_tool.side_effect = [success_invocation, failure_invocation]
        runtime = remote_runtime()
        runtime.execute.side_effect = [
            ReadFileOutput(
                path="a.txt",
                encoding="utf-8",
                content="ok",
                offset=0,
                bytes_read=2,
                total_bytes=2,
                truncated=False,
            ),
            OSError("SECRET remote error"),
        ]
        first = result_from(
            call_tool(
                ToolCall(id="observe_1", name="read_file", arguments={"path": "a.txt"}),
                execution_runtime=runtime,
                observability=observer,
            )
        )
        second = result_from(
            call_tool(
                ToolCall(id="observe_2", name="read_file", arguments={"path": "a.txt"}),
                execution_runtime=runtime,
                observability=observer,
            )
        )
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "error")
        self.assertEqual(observer.finish_tool.call_count, 2)
        self.assertIs(observer.finish_tool.call_args_list[0].args[0], success_invocation)
        self.assertEqual(observer.finish_tool.call_args_list[0].args[1].status, "success")
        self.assertIs(observer.finish_tool.call_args_list[1].args[0], failure_invocation)
        self.assertEqual(observer.finish_tool.call_args_list[1].args[1].status, "error")

    def test_observability_finishes_remote_code_success_denied_and_error(self) -> None:
        observer = Mock(spec=Observability)
        invocations = [object(), object(), object()]
        observer.start_tool.side_effect = invocations
        runtime = remote_runtime()
        outcomes = [
            CodeInterpreterOutput(
                exit_code=0,
                stdout="",
                result="2",
                stderr="",
                truncated=False,
            ),
            RemoteExecutionAuthorizationDenied("SECRET unauthorized"),
            OSError("SECRET provider"),
        ]

        def execute_authorized(_name, _arguments, callback):
            outcome = outcomes.pop(0)
            if not isinstance(outcome, RemoteExecutionAuthorizationDenied):
                self.assertIs(callback(), True)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        runtime.execute_authorized.side_effect = execute_authorized
        handler = Mock(side_effect=AssertionError("local code interpreter called"))
        registration = replace(
            registry._REGISTRY["code_interpreter"], handler=handler
        )
        results = []
        with patch.dict(
            registry._REGISTRY, {"code_interpreter": registration}
        ):
            for index in range(3):
                results.append(
                    result_from(
                        call_tool(
                            ToolCall(
                                id=f"observe_code_{index}",
                                name="code_interpreter",
                                arguments={"code": "1 + 1"},
                            ),
                            execution_runtime=runtime,
                            observability=observer,
                        )
                    )
                )

        self.assertEqual(
            [(item["status"], item["error"]) for item in results],
            [
                ("success", None),
                ("denied", REMOTE_CODE_DENIED),
                ("error", REMOTE_ERROR),
            ],
        )
        self.assertEqual(observer.finish_tool.call_count, 3)
        self.assertEqual(
            [call.args[1].status for call in observer.finish_tool.call_args_list],
            ["success", "denied", "error"],
        )
        self.assertEqual(
            [call.args[0] for call in observer.finish_tool.call_args_list],
            invocations,
        )
        handler.assert_not_called()
        runtime.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
