"""Strictly gated real approval-concurrency checks for Aliyun Sandbox.

The harness uses the product Registry/runtime with deterministic host-side
barriers and synthetic workspace files.  It never constructs a model client,
so it cannot contact DeepSeek.  Diagnostics are fixed text and never contain
credentials, Sandbox identifiers, provider output, or OSS paths.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
import hashlib
import json
import os
import re
import secrets
import threading
import time
from types import SimpleNamespace
from typing import Final
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from pydantic import ValidationError

from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.execution_backend import AliyunSandboxExecutionBackend
from app.execution_runtime import (
    ExecutionRuntimeClosedError,
    ExecutionToolRuntime,
    RemoteExecutionAuthorizationDenied,
)
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from app.sandbox_transport import SandboxCommandResult
from app.schemas import (
    BashShellArguments,
    BashShellOutput,
    ReadFileArguments,
    ToolCall,
    ToolMessage,
    WriteFileArguments,
)
from app.workflow import FileApprovalWorkflow, ToolConfirmationRequest
from app.workspace_checkpoint import create_workspace_checkpoint_initializer
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from app.workspace_versions import WorkspaceVersionCoordinator
from tests.sandbox_cleanup_fakes import empty_recovery_provider
from tests.test_remote_mutation_runtime import _Sandbox, _settings
from tests.test_workspace_versions import _MemoryTransport
from tool import registry
from tool.registry import call_tool


_LIVE_APPROVAL_CONCURRENCY_GATE: Final[str] = (
    "RUN_ALIYUN_SANDBOX_LIVE_APPROVAL_CONCURRENCY_E2E"
)
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_SAFE_PENDING_RE: Final[re.Pattern[str]] = re.compile(r"remote-memory/[0-9a-f]{64}\Z")
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0
_THREAD_TIMEOUT_SECONDS: Final[float] = 30.0


class _HarnessError(RuntimeError):
    """Fixed-text failure that cannot reflect provider or thread output."""


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _approval_snapshot() -> WorkspaceSeedSnapshot:
    return WorkspaceSeedSnapshot((
        _entry("AGENTS.md", b"state: initial\n"),
        _entry("probe.py", b"VALUE = 42\n"),
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
        raise _HarnessError("审批并发 live active 计数失败。") from None
    return len(sandbox_ids)


def _result(raw: str) -> dict[str, object]:
    try:
        result = json.loads(ToolMessage.model_validate_json(raw).model_dump_json())[
            "result"
        ]
    except Exception:
        raise _HarnessError("审批并发 live 工具结果协议无效。") from None
    if not isinstance(result, dict):
        raise _HarnessError("审批并发 live 工具结果类型无效。")
    return result


def _join(worker: threading.Thread) -> None:
    worker.join(_THREAD_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise _HarnessError("审批并发 live 工作线程未在时限内结束。")


def _join_all(workers: tuple[threading.Thread, ...]) -> None:
    stalled = False
    for worker in workers:
        worker.join(_THREAD_TIMEOUT_SECONDS)
        stalled = stalled or worker.is_alive()
    if stalled:
        raise _HarnessError("审批并发 live 工作线程未在时限内结束。")


def _exercise_approval_concurrency(runtime: ExecutionToolRuntime) -> None:
    first_workflow = FileApprovalWorkflow()
    second_workflow = FileApprovalWorkflow()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    collision_callback_entered = threading.Event()
    collision_done = threading.Event()
    command_authorized = threading.Event()
    command_finished = threading.Event()
    all_reservations_attempted = threading.Event()
    reserve_lock = threading.Lock()
    reserve_attempts = 0
    requests: dict[str, ToolConfirmationRequest] = {}
    results: dict[str, dict[str, object]] = {}
    command_results: list[BashShellOutput] = []
    errors: list[BaseException] = []

    original_reserve = runtime._reserve_workspace

    def observed_reserve(generation: int | None = None):
        nonlocal reserve_attempts
        with reserve_lock:
            reserve_attempts += 1
            if reserve_attempts == 3:
                all_reservations_attempted.set()
        return original_reserve(generation)

    def approve_first(request: ToolConfirmationRequest) -> bool:
        requests["first"] = request
        first_entered.set()
        if not release_first.wait(_THREAD_TIMEOUT_SECONDS):
            raise RuntimeError("bounded approval wait expired")
        return True

    def reject_second(request: ToolConfirmationRequest) -> bool:
        requests["second"] = request
        second_entered.set()
        return False

    def unexpected_collision(_request: ToolConfirmationRequest) -> bool:
        collision_callback_entered.set()
        return True

    def invoke_write(
        label: str,
        call: ToolCall,
        workflow: FileApprovalWorkflow,
        callback: Callable[[ToolConfirmationRequest], bool],
    ) -> None:
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
            if label == "collision":
                collision_done.set()

    first_call = ToolCall(
        id="approval-live-first",
        name="write_file",
        arguments={
            "path": "AGENTS.md",
            "content": "state: approved\n",
            "mode": "overwrite",
        },
    )
    second_call = ToolCall(
        id="approval-live-second",
        name="write_file",
        arguments={
            "path": "AGENTS.md",
            "content": "state: rejected\n",
            "mode": "overwrite",
        },
    )
    collision_call = ToolCall(
        id="approval-live-collision",
        name="write_file",
        arguments={
            "path": "AGENTS.md",
            "content": "state: collision\n",
            "mode": "overwrite",
        },
    )

    def invoke_command() -> None:
        try:
            output = runtime.execute_authorized(
                "bash_shell",
                BashShellArguments(
                    command="compileall",
                    paths=["probe.py"],
                    timeout_seconds=10,
                ),
                lambda: command_authorized.set() or True,
            )
            if isinstance(output, BashShellOutput):
                command_results.append(output)
            else:
                errors.append(_HarnessError("审批并发 live 命令返回类型无效。"))
        except BaseException as error:
            errors.append(error)
        finally:
            command_finished.set()

    local_handler = Mock(side_effect=AssertionError("host fallback"))
    registration = replace(registry._REGISTRY["write_file"], handler=local_handler)
    first = threading.Thread(
        target=invoke_write,
        args=("first", first_call, first_workflow, approve_first),
        daemon=True,
    )
    collision = threading.Thread(
        target=invoke_write,
        args=(
            "collision",
            collision_call,
            first_workflow,
            unexpected_collision,
        ),
        daemon=True,
    )
    second = threading.Thread(
        target=invoke_write,
        args=("second", second_call, second_workflow, reject_second),
        daemon=True,
    )
    command = threading.Thread(target=invoke_command, daemon=True)

    with (
        patch.dict(registry._REGISTRY, {"write_file": registration}),
        patch.object(runtime, "_reserve_workspace", side_effect=observed_reserve),
    ):
        first.start()
        try:
            if not first_entered.wait(_THREAD_TIMEOUT_SECONDS):
                raise _HarnessError("首个审批未在时限内到达。")

            collision.start()
            if not collision_done.wait(_THREAD_TIMEOUT_SECONDS):
                raise _HarnessError("共享审批冲突未在时限内拒绝。")
            if collision_callback_entered.is_set():
                raise _HarnessError("共享审批冲突错误触发了第二回调。")
            if results.get("collision", {}).get("status") != "denied":
                raise _HarnessError("共享审批冲突未固定拒绝。")

            second.start()
            command.start()
            if not all_reservations_attempted.wait(_THREAD_TIMEOUT_SECONDS):
                raise _HarnessError("竞争操作未到达 workspace gate。")
            if not command_authorized.is_set():
                raise _HarnessError("竞争命令未完成可信回调。")
            if second_entered.is_set() or command_finished.is_set():
                raise _HarnessError("审批期间 workspace gate 未阻止竞争操作。")
        finally:
            release_first.set()
            _join_all(tuple(
                worker
                for worker in (first, collision, second, command)
                if worker.ident is not None
            ))

    if errors:
        raise _HarnessError("审批并发 live 工作线程安全失败。")
    if results.get("first", {}).get("status") != "success":
        raise _HarnessError("首个审批未精确提交。")
    if results.get("second", {}).get("status") != "denied":
        raise _HarnessError("第二个审批未精确拒绝。")
    if len(command_results) != 1:
        raise _HarnessError("竞争命令没有精确完成一次。")
    command_output = command_results[0]
    if (
        command_output.command != "compileall"
        or command_output.paths != ["probe.py"]
        or command_output.exit_code != 0
        or command_output.truncated
    ):
        raise _HarnessError("竞争命令结果不符合安全契约。")
    if not second_entered.is_set():
        raise _HarnessError("第二个审批未在 gate 释放后到达。")
    first_request = requests.get("first")
    second_request = requests.get("second")
    if (
        first_request is None
        or second_request is None
        or first_request.request_id == second_request.request_id
        or first_request.pending_file == second_request.pending_file
        or first_request.phase != "diff"
        or second_request.phase != "diff"
        or not isinstance(first_request.pending_file, str)
        or not isinstance(second_request.pending_file, str)
        or not _SAFE_PENDING_RE.fullmatch(first_request.pending_file)
        or not _SAFE_PENDING_RE.fullmatch(second_request.pending_file)
    ):
        raise _HarnessError("并发审批绑定不唯一或不完整。")
    observed = runtime.execute(
        "read_file",
        ReadFileArguments(path="AGENTS.md", max_bytes=1024),
    )
    if observed.content != "state: approved\n":
        raise _HarnessError("拒绝的并发 mutation 改变了工作区。")
    local_handler.assert_not_called()

    exceptional = _result(call_tool(
        ToolCall(
            id="approval-live-exception",
            name="write_file",
            arguments={
                "path": "AGENTS.md",
                "content": "state: exception\n",
                "mode": "overwrite",
            },
        ),
        execution_runtime=runtime,
        workflow=FileApprovalWorkflow(),
        confirmation_callback=lambda _request: (_ for _ in ()).throw(
            RuntimeError("bounded confirmation failure")
        ),
        permission_mode="partial-control",
    ))
    if exceptional.get("status") != "denied":
        raise _HarnessError("审批异常未固定拒绝。")
    if runtime.execute(
        "read_file", ReadFileArguments(path="AGENTS.md", max_bytes=1024)
    ).content != "state: approved\n":
        raise _HarnessError("审批异常后 workspace gate 未安全释放。")

    cancelled = runtime.prepare_async_authorized(
        "bash_shell",
        BashShellArguments(command="compileall", paths=["probe.py"]),
        lambda: True,
    )
    cancelled.cancel()
    try:
        cancelled.run()
    except ExecutionRuntimeClosedError:
        pass
    else:
        raise _HarnessError("已取消 permit 仍可执行。")
    if runtime.closed:
        raise _HarnessError("派发前取消错误关闭了 runtime。")

    close_entered = threading.Event()
    release_close = threading.Event()
    close_errors: list[BaseException] = []

    def wait_for_close(_request: ToolConfirmationRequest) -> bool:
        close_entered.set()
        if not release_close.wait(_THREAD_TIMEOUT_SECONDS):
            raise RuntimeError("bounded close approval wait expired")
        return True

    def invoke_close_race() -> None:
        try:
            call_tool(
                ToolCall(
                    id="approval-live-close",
                    name="write_file",
                    arguments={
                        "path": "AGENTS.md",
                        "content": "state: close-race\n",
                        "mode": "overwrite",
                    },
                ),
                execution_runtime=runtime,
                workflow=FileApprovalWorkflow(),
                confirmation_callback=wait_for_close,
                permission_mode="partial-control",
            )
        except BaseException as error:
            close_errors.append(error)

    close_worker = threading.Thread(target=invoke_close_race, daemon=True)
    close_worker.start()
    try:
        if not close_entered.wait(_THREAD_TIMEOUT_SECONDS):
            raise _HarnessError("close 竞态审批未在时限内到达。")
        runtime.close()
    finally:
        release_close.set()
        _join(close_worker)
    if not runtime.closed or not any(
        isinstance(error, ExecutionRuntimeClosedError) for error in close_errors
    ):
        raise _HarnessError("审批期间 close 未建立 runtime fence。")
    if not runtime._workspace_gate.acquire(blocking=False):
        raise _HarnessError("审批期间 close 后 workspace gate 未释放。")
    runtime._workspace_gate.release()


def _create_runtime(settings: SandboxSettings, session_id: str) -> ExecutionToolRuntime:
    return ExecutionToolRuntime(
        AliyunSandboxExecutionBackend(
            session_id,
            settings,
            workspace_initializer=create_workspace_checkpoint_initializer(
                _approval_snapshot()
            ),
        )
    )


def _run_live_approval_concurrency(
    settings: SandboxSettings,
    session_id: str,
    *,
    runtime_factory: Callable[[SandboxSettings, str], ExecutionToolRuntime] = (
        _create_runtime
    ),
    exercise: Callable[[ExecutionToolRuntime], None] = (
        _exercise_approval_concurrency
    ),
    emergency_cleanup: Callable[[SandboxSettings, str], None] = (
        cleanup_session_sandboxes
    ),
    active_count_observer: Callable[[SandboxSettings, str], int] = (
        _provider_active_count
    ),
) -> None:
    runtime = runtime_factory(settings, session_id)
    primary_error: BaseException | None = None
    close_failed = False
    cleanup_failed = False
    active_count_failed = False
    stage = "create"
    try:
        lease = runtime.ensure_ready()
        sandbox_id = lease.sandbox_id
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise _HarnessError("审批并发 live 缺少安全 Sandbox 身份。")
        sandbox_digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()
        stage = "exercise"
        exercise(runtime)
        if not runtime.closed:
            current_id = runtime.ensure_ready().sandbox_id
            if not isinstance(current_id, str) or not secrets.compare_digest(
                hashlib.sha256(current_id.encode("utf-8")).digest(),
                sandbox_digest,
            ):
                raise _HarnessError("审批并发期间创建了不同 Sandbox。")
    except BaseException as error:
        if isinstance(error, _HarnessError) or not isinstance(error, Exception):
            primary_error = error
        else:
            primary_error = _HarnessError(
                f"审批并发 live 在固定阶段 {stage} 安全失败。"
            )
    finally:
        try:
            runtime.close()
        except BaseException:
            close_failed = True
        try:
            emergency_cleanup(settings, session_id)
        except BaseException:
            cleanup_failed = True
        try:
            active_count_failed = active_count_observer(settings, session_id) != 0
        except BaseException:
            active_count_failed = True

    if primary_error is not None:
        if close_failed or cleanup_failed or active_count_failed:
            primary_error.add_note("审批并发 live 的最终 active=0 未确认。")
        raise primary_error
    if close_failed or cleanup_failed or active_count_failed:
        raise _HarnessError("审批并发 live 的最终 active=0 未确认。")


def _execute_live_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[[SandboxSettings, str], None] = _run_live_approval_concurrency,
) -> None:
    selected_env = os.environ if environ is None else environ
    if selected_env.get(_LIVE_APPROVAL_CONCURRENCY_GATE) != "1":
        raise _HarnessError("审批并发 live gate 未启用。")
    settings = settings_loader()
    settings.require_enabled()
    settings.require_recovery_enabled()
    runner(settings, uuid4().hex)


@unittest.skipUnless(
    os.environ.get(_LIVE_APPROVAL_CONCURRENCY_GATE) == "1",
    f"Set {_LIVE_APPROVAL_CONCURRENCY_GATE}=1 to run real approval concurrency E2E",
)
class AliyunSandboxApprovalConcurrencyLiveE2ETests(unittest.TestCase):
    def test_real_concurrent_approvals_are_exact_serialized_and_cleaned(self) -> None:
        _execute_live_e2e()


class ApprovalConcurrencyHarnessOfflineTests(unittest.TestCase):
    def test_gate_precedes_settings_backend_cleanup_and_provider_scan(self) -> None:
        with (
            patch(
                __name__ + ".load_sandbox_settings",
                side_effect=AssertionError("must not load settings"),
            ) as settings_loader,
            patch(
                __name__ + ".AliyunSandboxExecutionBackend",
                side_effect=AssertionError("must not create backend"),
            ) as backend,
            patch(
                __name__ + ".cleanup_session_sandboxes",
                side_effect=AssertionError("must not cleanup"),
            ) as cleanup,
            patch(
                __name__ + "._provider_active_count",
                side_effect=AssertionError("must not list provider"),
            ) as active_count,
        ):
            with self.assertRaisesRegex(_HarnessError, "live gate"):
                _execute_live_e2e(environ={})

        settings_loader.assert_not_called()
        backend.assert_not_called()
        cleanup.assert_not_called()
        active_count.assert_not_called()

    def test_default_live_suite_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_APPROVAL_CONCURRENCY_GATE) == "1":
            self.skipTest("The operator explicitly enabled the approval gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxApprovalConcurrencyLiveE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(
                __name__ + ".load_sandbox_settings",
                side_effect=AssertionError("must not load settings"),
            ) as settings_loader,
            patch(
                __name__ + ".AliyunSandboxExecutionBackend",
                side_effect=AssertionError("must not create backend"),
            ) as backend,
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        settings_loader.assert_not_called()
        backend.assert_not_called()

    def test_failure_always_closes_cleans_and_confirms_active_zero(self) -> None:
        settings = Mock(spec=SandboxSettings)
        runtime = Mock()
        runtime.ensure_ready.return_value = SimpleNamespace(sandbox_id="opaque")
        cleanup = Mock()
        active_count = Mock(return_value=0)

        with self.assertRaisesRegex(_HarnessError, "fixed failure"):
            _run_live_approval_concurrency(
                settings,
                "a" * 32,
                runtime_factory=lambda _settings, _session: runtime,
                exercise=Mock(side_effect=_HarnessError("fixed failure")),
                emergency_cleanup=cleanup,
                active_count_observer=active_count,
            )

        runtime.close.assert_called_once_with()
        cleanup.assert_called_once_with(settings, "a" * 32)
        active_count.assert_called_once_with(settings, "a" * 32)

    def test_live_scenario_contract_runs_against_offline_product_runtime(self) -> None:
        session_id = "b" * 32
        workspace_root = "/workspace/agnet-study/" + session_id
        sandbox = _Sandbox()
        transport = _MemoryTransport(workspace_root)

        class Handle:
            def wait(self):
                return SandboxCommandResult(0, "", "")

            def cancel(self):
                return None

        def start_run(*_args, dispatch_attempt_consumer, **_kwargs):
            dispatch_attempt_consumer()
            return Handle()

        transport.start_run = start_run  # type: ignore[attr-defined]
        runtime = ExecutionToolRuntime(
            AliyunSandboxExecutionBackend(
                session_id,
                _settings(),
                sandbox_factory=lambda _settings, _metadata: sandbox,
                persistence_sandbox_factory=lambda _settings, _metadata: SimpleNamespace(
                    sandbox_id="offline-approval-persistence",
                    kill=lambda: True,
                ),
                transport_factory=lambda _candidate: transport,
                persistence_transport_factory=lambda _candidate: transport,
                workspace_initializer=WorkspaceVersionCoordinator(
                    _approval_snapshot()
                ),
                recovery_provider=empty_recovery_provider(),
            )
        )

        _exercise_approval_concurrency(runtime)

        self.assertTrue(runtime.closed)
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertEqual(
            transport.active[workspace_root + "/AGENTS.md"],
            b"state: approved\n",
        )

    def test_model_controlled_boolean_is_not_an_approval_capability(self) -> None:
        for model, payload in (
            (
                WriteFileArguments,
                {
                    "path": "AGENTS.md",
                    "content": "x",
                    "mode": "overwrite",
                    "approval_granted": True,
                },
            ),
            (
                BashShellArguments,
                {
                    "command": "compileall",
                    "paths": ["probe.py"],
                    "approval_granted": True,
                },
            ),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

        runtime = ExecutionToolRuntime()
        try:
            with self.assertRaises(RemoteExecutionAuthorizationDenied):
                runtime.execute_authorized(
                    "bash_shell",
                    BashShellArguments(command="compileall", paths=["probe.py"]),
                    True,  # type: ignore[arg-type]
                )
        finally:
            runtime.close()

    def test_snapshot_and_pending_protocol_are_fixed_and_synthetic(self) -> None:
        snapshot = _approval_snapshot()
        self.assertEqual(
            tuple(item.path for item in snapshot.entries),
            ("AGENTS.md", "probe.py"),
        )
        self.assertTrue(_SAFE_PENDING_RE.fullmatch("remote-memory/" + "a" * 64))
        self.assertFalse(_SAFE_PENDING_RE.fullmatch("data/pending/" + "a" * 64))


if __name__ == "__main__":
    unittest.main()
