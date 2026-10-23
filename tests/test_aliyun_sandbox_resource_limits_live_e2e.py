"""Strictly gated real resource-boundary checks for Aliyun Sandbox.

The harness uses only the product execution runtime and synthetic bounded
payloads.  It never constructs a model client, so it cannot contact DeepSeek.
Process creation is intentionally not pressure-tested: the public Code
Interpreter AST denies imports and process APIs before provider dispatch;
``RLIMIT_NPROC`` establishment remains covered by the exact offline runner
tests instead of being misrepresented as a live process test.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import os
import re
import secrets
import time
from types import SimpleNamespace
from typing import Final
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.execution_backend import AliyunSandboxExecutionBackend
from app.execution_runtime import ExecutionToolRuntime
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from app.schemas import (
    BashShellArguments,
    BashShellOutput,
    CodeInterpreterArguments,
    CodeInterpreterOutput,
)
from app.workspace_checkpoint import create_workspace_checkpoint_initializer
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from tool.code_interpreter import validate_code_interpreter_policy
from tool.file_access import ToolAccessDenied


_LIVE_RESOURCE_LIMITS_GATE: Final[str] = (
    "RUN_ALIYUN_SANDBOX_LIVE_RESOURCE_LIMITS_E2E"
)
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0

# Every payload is finite and contains no filesystem, network, environment, or
# process capability.  300 MiB is intentionally just above the checked-in
# 256 MiB policy and therefore exercises RLIMIT_AS without unbounded growth.
_MEMORY_STRESS_CODE: Final[str] = (
    "payload = 'x' * (300 * 1024 * 1024)\nlen(payload)"
)
_CPU_STRESS_CODE: Final[str] = "for item in range(10 ** 100):\n    pass"
_OUTPUT_STRESS_CODE: Final[str] = "print('x' * 20000)"
_REUSE_CODE: Final[str] = "6 * 7"

_MEMORY_ERROR: Final[str] = "代码运行失败：MemoryError。"
_CPU_ERROR: Final[str] = "远端受限 Python 执行失败。"
_OUTPUT_ERROR: Final[str] = "代码输出超过安全上限。"
_WALL_TIMEOUT_ERROR: Final[str] = "命令执行超时。"
_OUTPUT_LIMIT_CHARACTERS: Final[int] = 16_384


class _HarnessError(RuntimeError):
    """Fixed-text failure that never contains provider or child output."""


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _resource_snapshot() -> WorkspaceSeedSnapshot:
    slow_test = (
        "import time\nimport unittest\n\n\n"
        "class BoundedWallClockProbe(unittest.TestCase):\n"
        "    def test_bounded_sleep(self):\n"
        "        time.sleep(4)\n"
    ).encode("utf-8")
    quick_test = (
        "import unittest\n\n\n"
        "class ReuseProbe(unittest.TestCase):\n"
        "    def test_reuse(self):\n"
        "        self.assertEqual(6 * 7, 42)\n"
    ).encode("utf-8")
    return WorkspaceSeedSnapshot((
        _entry("reuse_tests/__init__.py", b""),
        _entry("reuse_tests/test_reuse.py", quick_test),
        _entry("stress_timeout/__init__.py", b""),
        _entry("stress_timeout/test_wall_clock.py", slow_test),
    ))


def _provider_active_count(settings: SandboxSettings, session_id: str) -> int:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id 必须是 32 位小写十六进制文本。")
    expected_metadata = {
        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
        SANDBOX_SESSION_METADATA_KEY: session_id,
    }
    try:
        sandbox_ids = _scan_active(
            _E2BSandboxRecoveryProvider(settings),
            expected_metadata=expected_metadata,
            deadline=time.monotonic() + _ACTIVE_SCAN_TIMEOUT_SECONDS,
        )
    except Exception:
        raise _HarnessError("资源上限 live active 计数失败。") from None
    return len(sandbox_ids)


def _authorized(
    runtime: ExecutionToolRuntime,
    tool_name: str,
    arguments: CodeInterpreterArguments | BashShellArguments,
) -> CodeInterpreterOutput | BashShellOutput:
    result = runtime.execute_authorized(tool_name, arguments, lambda: True)
    if not isinstance(result, (CodeInterpreterOutput, BashShellOutput)):
        raise _HarnessError("资源上限 live 返回类型无效。")
    return result


def _require_reusable_code(runtime: ExecutionToolRuntime) -> None:
    output = _authorized(
        runtime,
        "code_interpreter",
        CodeInterpreterArguments(code=_REUSE_CODE, timeout_seconds=3),
    )
    if (
        not isinstance(output, CodeInterpreterOutput)
        or output.exit_code != 0
        or output.stdout != ""
        or output.result != "42"
        or output.stderr != ""
        or output.truncated
    ):
        raise _HarnessError("资源失败后 code runtime 不可复用。")


def _require_reusable_bash(runtime: ExecutionToolRuntime) -> None:
    output = _authorized(
        runtime,
        "bash_shell",
        BashShellArguments(
            command="unittest",
            paths=["reuse_tests"],
            timeout_seconds=10,
        ),
    )
    if (
        not isinstance(output, BashShellOutput)
        or output.command != "unittest"
        or output.paths != ["reuse_tests"]
        or output.exit_code != 0
        or output.truncated
    ):
        raise _HarnessError("资源失败后 command runtime 不可复用。")


def _exercise_resource_contract(
    runtime: ExecutionToolRuntime,
    *,
    same_runtime_check: Callable[[], None],
) -> None:
    memory = _authorized(
        runtime,
        "code_interpreter",
        CodeInterpreterArguments(code=_MEMORY_STRESS_CODE, timeout_seconds=5),
    )
    if (
        not isinstance(memory, CodeInterpreterOutput)
        or memory.exit_code != 1
        or memory.stdout != ""
        or memory.result != ""
        or memory.stderr != _MEMORY_ERROR
        or memory.truncated
    ):
        raise _HarnessError("真实内存上限契约不符合预期。")
    _require_reusable_code(runtime)
    same_runtime_check()

    cpu = _authorized(
        runtime,
        "code_interpreter",
        CodeInterpreterArguments(code=_CPU_STRESS_CODE, timeout_seconds=1),
    )
    if (
        not isinstance(cpu, CodeInterpreterOutput)
        or cpu.exit_code != 1
        or cpu.stdout != ""
        or cpu.result != ""
        or cpu.stderr != _CPU_ERROR
        or cpu.truncated
    ):
        raise _HarnessError("真实 CPU 上限契约不符合预期。")
    _require_reusable_code(runtime)
    same_runtime_check()

    wall = _authorized(
        runtime,
        "bash_shell",
        BashShellArguments(
            command="unittest",
            paths=["stress_timeout"],
            timeout_seconds=1,
        ),
    )
    if (
        not isinstance(wall, BashShellOutput)
        or wall.command != "unittest"
        or wall.paths != ["stress_timeout"]
        or wall.exit_code != 1
        or wall.stdout != ""
        or wall.stderr != _WALL_TIMEOUT_ERROR
        or wall.truncated
    ):
        raise _HarnessError("真实墙钟超时契约不符合预期。")
    _require_reusable_bash(runtime)
    same_runtime_check()

    output = _authorized(
        runtime,
        "code_interpreter",
        CodeInterpreterArguments(code=_OUTPUT_STRESS_CODE, timeout_seconds=3),
    )
    if (
        not isinstance(output, CodeInterpreterOutput)
        or output.exit_code != 1
        or len(output.stdout) != _OUTPUT_LIMIT_CHARACTERS
        or output.stdout != "x" * _OUTPUT_LIMIT_CHARACTERS
        or output.result != ""
        or output.stderr != _OUTPUT_ERROR
        or not output.truncated
    ):
        raise _HarnessError("真实输出上限契约不符合预期。")
    _require_reusable_code(runtime)
    same_runtime_check()


def _run_live_resource_limits(settings: SandboxSettings) -> None:
    session_id = uuid4().hex
    runtime = ExecutionToolRuntime(
        AliyunSandboxExecutionBackend(
            session_id,
            settings,
            workspace_initializer=create_workspace_checkpoint_initializer(
                _resource_snapshot()
            ),
        )
    )
    primary_error: BaseException | None = None
    close_failed = False
    cleanup_failed = False
    active_count_failed = False
    stage = "create"
    try:
        lease = runtime.ensure_ready()
        sandbox_id = lease.sandbox_id
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise _HarnessError("资源上限 live 缺少安全 Sandbox 身份。")
        sandbox_digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()

        def same_runtime_check() -> None:
            current_id = runtime.ensure_ready().sandbox_id
            if not isinstance(current_id, str) or not secrets.compare_digest(
                hashlib.sha256(current_id.encode("utf-8")).digest(),
                sandbox_digest,
            ):
                raise _HarnessError("资源失败后创建了不同 Sandbox。")

        stage = "exercise"
        _exercise_resource_contract(
            runtime,
            same_runtime_check=same_runtime_check,
        )
    except BaseException as error:
        if isinstance(error, _HarnessError) or not isinstance(error, Exception):
            primary_error = error
        else:
            primary_error = _HarnessError(
                f"资源上限 live 在固定阶段 {stage} 安全失败。"
            )
    finally:
        try:
            runtime.close()
        except BaseException:
            close_failed = True
        try:
            cleanup_session_sandboxes(settings, session_id)
        except BaseException:
            cleanup_failed = True
        try:
            active_count_failed = _provider_active_count(settings, session_id) != 0
        except BaseException:
            active_count_failed = True

    if primary_error is not None:
        if close_failed or cleanup_failed or active_count_failed:
            primary_error.add_note("资源上限 live 的最终 active=0 未确认。")
        raise primary_error
    if close_failed or cleanup_failed or active_count_failed:
        raise _HarnessError("资源上限 live 的最终 active=0 未确认。")


@unittest.skipUnless(
    os.environ.get(_LIVE_RESOURCE_LIMITS_GATE) == "1",
    f"Set {_LIVE_RESOURCE_LIMITS_GATE}=1 to run real resource limit E2E",
)
class AliyunSandboxResourceLimitsLiveE2ETests(unittest.TestCase):
    def test_real_bounded_resource_failures_are_reusable_and_cleaned(self) -> None:
        settings = load_sandbox_settings()
        settings.require_enabled()
        settings.require_recovery_enabled()
        _run_live_resource_limits(settings)


class ResourceLimitsHarnessOfflineTests(unittest.TestCase):
    def test_gate_is_zero_cloud_by_default(self) -> None:
        if os.environ.get(_LIVE_RESOURCE_LIMITS_GATE) == "1":
            self.skipTest("The operator explicitly enabled the resource gate.")
        load = Mock(side_effect=AssertionError("must not load settings"))
        backend = Mock(side_effect=AssertionError("must not create backend"))
        cleanup = Mock(side_effect=AssertionError("must not cleanup"))
        active_count = Mock(side_effect=AssertionError("must not list provider"))
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxResourceLimitsLiveE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(f"{__name__}.cleanup_session_sandboxes", cleanup),
            patch(f"{__name__}._provider_active_count", active_count),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        cleanup.assert_not_called()
        active_count.assert_not_called()

    def test_bounded_payloads_and_offline_contract_are_exact(self) -> None:
        self.assertNotIn("import", _MEMORY_STRESS_CODE)
        self.assertNotIn("while", _CPU_STRESS_CODE)
        self.assertNotIn("fork", repr((
            _MEMORY_STRESS_CODE,
            _CPU_STRESS_CODE,
            _OUTPUT_STRESS_CODE,
        )))
        self.assertEqual(_OUTPUT_STRESS_CODE, "print('x' * 20000)")
        snapshot = _resource_snapshot()
        self.assertEqual(
            tuple(item.path for item in snapshot.entries),
            (
                "reuse_tests/__init__.py",
                "reuse_tests/test_reuse.py",
                "stress_timeout/__init__.py",
                "stress_timeout/test_wall_clock.py",
            ),
        )

        class FakeRuntime:
            def __init__(self) -> None:
                self.calls: list[tuple[str, object]] = []

            def execute_authorized(
                fake_self,
                tool_name: str,
                arguments: object,
                authorize: Callable[[], bool],
            ) -> object:
                self.assertIs(authorize(), True)
                fake_self.calls.append((tool_name, arguments))
                if isinstance(arguments, CodeInterpreterArguments):
                    mapping = {
                        _MEMORY_STRESS_CODE: CodeInterpreterOutput(
                            exit_code=1,
                            stdout="",
                            result="",
                            stderr=_MEMORY_ERROR,
                            truncated=False,
                        ),
                        _CPU_STRESS_CODE: CodeInterpreterOutput(
                            exit_code=1,
                            stdout="",
                            result="",
                            stderr=_CPU_ERROR,
                            truncated=False,
                        ),
                        _OUTPUT_STRESS_CODE: CodeInterpreterOutput(
                            exit_code=1,
                            stdout="x" * _OUTPUT_LIMIT_CHARACTERS,
                            result="",
                            stderr=_OUTPUT_ERROR,
                            truncated=True,
                        ),
                        _REUSE_CODE: CodeInterpreterOutput(
                            exit_code=0,
                            stdout="",
                            result="42",
                            stderr="",
                            truncated=False,
                        ),
                    }
                    return mapping[arguments.code]
                assert isinstance(arguments, BashShellArguments)
                if arguments.paths == ["stress_timeout"]:
                    return BashShellOutput(
                        command="unittest",
                        paths=["stress_timeout"],
                        exit_code=1,
                        stdout="",
                        stderr=_WALL_TIMEOUT_ERROR,
                        truncated=False,
                    )
                return BashShellOutput(
                    command="unittest",
                    paths=["reuse_tests"],
                    exit_code=0,
                    stdout="",
                    stderr="Ran 1 test - OK",
                    truncated=False,
                )

        runtime = FakeRuntime()
        reuse_checks: list[None] = []
        _exercise_resource_contract(  # type: ignore[arg-type]
            runtime,
            same_runtime_check=lambda: reuse_checks.append(None),
        )

        self.assertEqual(len(runtime.calls), 8)
        self.assertEqual(len(reuse_checks), 4)
        self.assertEqual(
            [name for name, _arguments in runtime.calls],
            [
                "code_interpreter",
                "code_interpreter",
                "code_interpreter",
                "code_interpreter",
                "bash_shell",
                "bash_shell",
                "code_interpreter",
                "code_interpreter",
            ],
        )

    def test_process_creation_is_denied_before_live_provider_dispatch(self) -> None:
        attacks = (
            "import os",
            "import subprocess",
            "__import__('os')",
            "while True:\n    pass",
        )
        for code in attacks:
            with self.subTest(code=code), self.assertRaises(ToolAccessDenied):
                validate_code_interpreter_policy(
                    CodeInterpreterArguments(code=code, timeout_seconds=1)
                )


if __name__ == "__main__":
    unittest.main()
