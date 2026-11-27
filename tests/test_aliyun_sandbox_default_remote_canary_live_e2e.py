"""Strictly gated default-remote canary for one new Aliyun session.

The live path drives the real CLI backend selection, persistent workflow,
CodingAgent, Registry and execution runtime.  Its OpenAI-compatible client is
fully scripted and has no network implementation, so DeepSeek cannot be
contacted.  Provider diagnostics are reduced to fixed stages and integer
counts; Sandbox IDs and OSS paths are never retained in reports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Final
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app import main as main_module
from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.config import Settings, load_default_execution_backend
from app.execution_backend import AliyunSandboxExecutionBackend
from app.execution_runtime import ExecutionToolRuntime
from app.observability import Observability
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from app.sandbox_transport import E2BSandboxTransport
from app.schemas import ReadFileOutput, ToolResult, WriteFileArguments, WriteFileOutput
from app.workflow import SessionEventWorkflow
from app.workspace_checkpoint import create_workspace_checkpoint_initializer
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from tests.sandbox_cleanup_fakes import empty_recovery_provider
from tests.model_service_helpers import ready_model_service_patch
from tests.test_remote_mutation_runtime import _Sandbox, _settings
from tests.test_workspace_versions import _MemoryTransport
from tool import registry


_LIVE_GATE: Final[str] = "RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_E2E"
_LIVE_BATCH_GATE: Final[str] = (
    "RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_BATCH_E2E"
)
_DEFAULT_BACKEND_ENV: Final[str] = "AGENT_DEFAULT_EXECUTION_BACKEND"
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0
_INPUT_TIMEOUT_SECONDS: Final[float] = 30.0
_MAX_VERSION_WRITES: Final[int] = 4
_BATCH_ATTEMPTS: Final[int] = 3
_MAX_ATTEMPT_MILLISECONDS: Final[int] = 3_600_000
_MAX_BATCH_MILLISECONDS: Final[int] = (
    _BATCH_ATTEMPTS * _MAX_ATTEMPT_MILLISECONDS
)
_CANARY_PATH: Final[str] = "canary.txt"
_CANARY_BEFORE: Final[bytes] = b"state: before\n"
_CANARY_AFTER: Final[str] = "state: after\n"
_ALLOWED_STAGES: Final[frozenset[str]] = frozenset({
    "default_selected",
    "sandbox_validated",
    "remote_bound",
    "runtime_created",
    "mutation_committed",
    "read_verified",
    "remote_closed",
    "local_rollback_bound",
    "active_zero",
})


class _HarnessError(RuntimeError):
    """Fixed-text canary failure without provider-derived content."""


@dataclass(frozen=True, slots=True)
class _VersionDelta:
    objects: int
    generations: int

    def __post_init__(self) -> None:
        values = (self.objects, self.generations)
        if any(
            type(value) is not int or not 0 <= value <= _MAX_VERSION_WRITES
            for value in values
        ):
            raise _HarnessError("default remote canary v2 计数越界。")


@dataclass(frozen=True, slots=True)
class _BatchReport:
    """Bounded aggregate containing only booleans-free integers."""

    attempts: int
    successes: int
    failures: int
    active_zero: int
    objects: int
    generations: int
    attempt_milliseconds: tuple[int, ...]
    total_milliseconds: int
    min_milliseconds: int
    max_milliseconds: int

    def __post_init__(self) -> None:
        counts = (
            self.attempts,
            self.successes,
            self.failures,
            self.active_zero,
            self.objects,
            self.generations,
        )
        if any(type(value) is not int for value in counts):
            raise _HarnessError("default remote canary batch 计数类型无效。")
        if (
            not 0 <= self.attempts <= _BATCH_ATTEMPTS
            or not 0 <= self.successes <= self.attempts
            or not 0 <= self.failures <= 1
            or self.successes + self.failures != self.attempts
            or self.active_zero != self.successes
            or self.objects != self.successes
            or self.generations != self.successes
        ):
            raise _HarnessError("default remote canary batch 计数越界。")
        if type(self.attempt_milliseconds) is not tuple or len(
            self.attempt_milliseconds
        ) != self.attempts:
            raise _HarnessError("default remote canary batch 耗时数组无效。")
        if any(
            type(value) is not int
            or not 0 <= value <= _MAX_ATTEMPT_MILLISECONDS
            for value in self.attempt_milliseconds
        ):
            raise _HarnessError("default remote canary batch 单次耗时无效。")
        duration_fields = (
            self.total_milliseconds,
            self.min_milliseconds,
            self.max_milliseconds,
        )
        if any(type(value) is not int for value in duration_fields):
            raise _HarnessError("default remote canary batch 耗时类型无效。")
        expected_total = sum(self.attempt_milliseconds)
        expected_min = min(self.attempt_milliseconds, default=0)
        expected_max = max(self.attempt_milliseconds, default=0)
        if (
            self.total_milliseconds != expected_total
            or self.min_milliseconds != expected_min
            or self.max_milliseconds != expected_max
            or not 0 <= self.total_milliseconds <= _MAX_BATCH_MILLISECONDS
        ):
            raise _HarnessError("default remote canary batch 耗时汇总无效。")

    def summary(self) -> str:
        per_attempt = ",".join(str(value) for value in self.attempt_milliseconds)
        return (
            "DEFAULT_REMOTE_CANARY_BATCH"
            f" attempts={self.attempts}"
            f" successes={self.successes}"
            f" failures={self.failures}"
            f" active_zero={self.active_zero}"
            f" objects={self.objects}"
            f" generations={self.generations}"
            f" total_ms={self.total_milliseconds}"
            f" min_ms={self.min_milliseconds}"
            f" max_ms={self.max_milliseconds}"
            f" per_attempt_ms={per_attempt}"
        )


class _BatchHarnessError(_HarnessError):
    def __init__(self, attempt: int, report: _BatchReport) -> None:
        if type(attempt) is not int or not 1 <= attempt <= _BATCH_ATTEMPTS:
            raise _HarnessError("default remote canary batch 失败序号无效。")
        self.attempt = attempt
        self.report = report
        super().__init__(
            "default remote canary batch failed"
            f" attempt={attempt}"
            f" attempts={report.attempts}"
            f" successes={report.successes}"
            f" failures={report.failures}"
            f" active_zero={report.active_zero}"
            f" objects={report.objects}"
            f" generations={report.generations}"
            f" total_ms={report.total_milliseconds}"
        )


class _VersionWriteRecorder:
    """Keep only bounded category counts, never an OSS coordinate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects = 0
        self._generations = 0

    def record_success(self, path: object) -> None:
        if not isinstance(path, str):
            raise _HarnessError("default remote canary v2 路由类型无效。")
        is_object = "/objects/" in path
        is_generation = "/generations/" in path
        if is_object == is_generation:
            raise _HarnessError("default remote canary v2 路由分类无效。")
        with self._lock:
            if is_object:
                self._objects += 1
            else:
                self._generations += 1
            _VersionDelta(self._objects, self._generations)

    def snapshot(self) -> _VersionDelta:
        with self._lock:
            return _VersionDelta(self._objects, self._generations)


class _StageRecorder:
    def __init__(self) -> None:
        self._stages: list[str] = []

    def record(self, stage: str) -> None:
        if stage not in _ALLOWED_STAGES or len(self._stages) >= 16:
            raise _HarnessError("default remote canary 阶段记录无效。")
        self._stages.append(stage)

    @property
    def stages(self) -> tuple[str, ...]:
        return tuple(self._stages)


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _canary_snapshot() -> WorkspaceSeedSnapshot:
    # One baseline file makes the first overwrite publish exactly one new v2
    # content object and one generation manifest.
    return WorkspaceSeedSnapshot((_entry(_CANARY_PATH, _CANARY_BEFORE),))


def _chunk(
    *, content: str | None = None, calls: Sequence[tuple[str, str, str]] = ()
) -> SimpleNamespace:
    tool_calls = [
        SimpleNamespace(
            index=index,
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )
        for index, (call_id, name, arguments) in enumerate(calls)
    ]
    return SimpleNamespace(choices=[SimpleNamespace(
        index=0,
        delta=SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning_content=None,
        ),
    )])


def _tool(call_id: str, name: str, arguments: Mapping[str, object]) -> list[SimpleNamespace]:
    raw = json.dumps(
        dict(arguments),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return [_chunk(calls=((call_id, name, raw),))]


def _matching_result(
    messages: Sequence[Mapping[str, object]],
    call_id: str,
) -> ToolResult | None:
    matching = [
        message
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") == call_id
    ]
    if len(matching) != 1:
        return None
    try:
        return ToolResult.model_validate_json(matching[0].get("content"))
    except Exception:
        return None


class _CanaryScript:
    def __init__(self, final_response_ready: threading.Event) -> None:
        self._ready = final_response_ready
        self.stage = 0
        self.failed = False

    def __call__(self, messages: Sequence[Mapping[str, object]]) -> list[SimpleNamespace]:
        if self.stage == 0:
            self.stage = 1
            return _tool(
                "canary-write",
                "write_file",
                {
                    "path": _CANARY_PATH,
                    "content": _CANARY_AFTER,
                    "mode": "overwrite",
                    "encoding": "utf-8",
                },
            )
        if self.stage == 1:
            written = _matching_result(messages, "canary-write")
            if (
                written is None
                or written.status != "success"
                or written.error is not None
                or not isinstance(written.output, WriteFileOutput)
            ):
                self.failed = True
                self.stage = 3
                self._ready.set()
                return [_chunk(content="Canary stopped at a fixed write check.")]
            self.stage = 2
            return _tool(
                "canary-read",
                "read_file",
                {"path": _CANARY_PATH, "max_bytes": 1024},
            )
        if self.stage == 2:
            observed = _matching_result(messages, "canary-read")
            if (
                observed is None
                or observed.status != "success"
                or observed.error is not None
                or not isinstance(observed.output, ReadFileOutput)
                or observed.output.path != _CANARY_PATH
                or observed.output.content != _CANARY_AFTER
                or observed.output.truncated
            ):
                self.failed = True
            self.stage = 3
            self._ready.set()
            return [_chunk(content="Default remote canary completed.")]
        self.failed = True
        self._ready.set()
        return [_chunk(content="Canary stopped at a fixed request bound.")]


class _Completions:
    def __init__(self, script: _CanaryScript) -> None:
        self._script = script
        self.requests = 0

    def create(self, **kwargs: object) -> list[SimpleNamespace]:
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            raise _HarnessError("default remote canary 模型请求协议无效。")
        self.requests += 1
        if self.requests > 3:
            raise _HarnessError("default remote canary 模型请求越界。")
        return self._script(messages)


class _ScriptedClient:
    """Minimum OpenAI-compatible surface with no network code."""

    def __init__(self, script: _CanaryScript) -> None:
        self.completions = _Completions(script)
        self.chat = SimpleNamespace(completions=self.completions)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _CanaryInput:
    """One prompt, then EOF only after the scripted final response exists."""

    def __init__(self, final_response_ready: threading.Event) -> None:
        self._ready = final_response_ready
        self._lock = threading.Lock()
        self._calls = 0

    def __call__(self, _prompt: str, _complete_commands: bool) -> str:
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            return "run the fixed remote canary"
        if not self._ready.wait(_INPUT_TIMEOUT_SECONDS):
            raise EOFError
        raise EOFError


class _ExitInput:
    def __call__(self, _prompt: str, _complete_commands: bool) -> str:
        return "exit"


class _QuietMaintenance:
    def start(self) -> None:
        return None

    def close(self) -> None:
        return None


def _provider_active_count(settings: SandboxSettings, session_id: str) -> int:
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise _HarnessError("default remote canary session 身份无效。")
    expected = {
        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
        SANDBOX_SESSION_METADATA_KEY: session_id,
    }
    try:
        active = _scan_active(
            _E2BSandboxRecoveryProvider(settings),
            expected_metadata=expected,
            deadline=time.monotonic() + _ACTIVE_SCAN_TIMEOUT_SECONDS,
        )
    except Exception:
        raise _HarnessError("default remote canary active 计数失败。") from None
    return len(active)


def _read_bound_session_id(directory: Path) -> str | None:
    path = directory / "execution.json"
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        raise _HarnessError("default remote canary 持久身份无效。") from None
    if (
        type(value) is not dict
        or set(value) != {"backend_name", "session_id", "version"}
        or value.get("backend_name") != "aliyun-sandbox"
        or value.get("version") != 1
        or not isinstance(value.get("session_id"), str)
        or _SESSION_ID_RE.fullmatch(value["session_id"]) is None
    ):
        raise _HarnessError("default remote canary 持久身份无效。")
    return value["session_id"]


def _live_runtime_factory(
    recorder: _VersionWriteRecorder,
    observed: dict[str, object],
) -> Callable[..., ExecutionToolRuntime]:
    del recorder  # Writes are observed at the concrete E2B transport boundary.

    def create(
        execution_backend: object,
        session_id: object,
        settings: object,
        *,
        collect_orphans: object = False,
        session_directory: Path | None = None,
        session_cleanup_complete: bool = False,
    ) -> ExecutionToolRuntime:
        if (
            execution_backend != "aliyun-sandbox"
            or not isinstance(session_id, str)
            or _SESSION_ID_RE.fullmatch(session_id) is None
            or not isinstance(settings, SandboxSettings)
            or collect_orphans is not True
            or not isinstance(session_directory, Path)
            or session_cleanup_complete is not True
        ):
            raise _HarnessError("default remote canary runtime 绑定无效。")
        from app.sandbox_budget import SandboxBudget

        runtime = ExecutionToolRuntime(AliyunSandboxExecutionBackend(
            session_id,
            settings,
            session_cleanup_complete=session_cleanup_complete,
            budget=SandboxBudget(
                settings.max_session_seconds,
                path=session_directory / "sandbox-budget.json",
                session_id=session_id,
            ),
            workspace_initializer=create_workspace_checkpoint_initializer(
                _canary_snapshot(), collect_orphans=True
            ),
        ))
        observed["runtime"] = runtime
        observed["session_id"] = session_id
        return runtime

    return create


def _run_default_remote_canary(
    settings: SandboxSettings,
    *,
    runtime_factory_builder: Callable[
        [_VersionWriteRecorder, dict[str, object]], Callable[..., ExecutionToolRuntime]
    ] = _live_runtime_factory,
    version_write_context: Callable[[_VersionWriteRecorder], object] | None = None,
    startup_cleanup: Callable[[SandboxSettings, str], None] = cleanup_session_sandboxes,
    emergency_cleanup: Callable[[SandboxSettings, str], None] = cleanup_session_sandboxes,
    active_count_observer: Callable[[SandboxSettings, str], int] = _provider_active_count,
) -> _VersionDelta:
    recorder = _VersionWriteRecorder()
    stages = _StageRecorder()
    observed: dict[str, object] = {}
    session_id: str | None = None
    primary_error: BaseException | None = None
    cleanup_failed = False
    active_failed = False
    stage = "cli"

    original_version_write = E2BSandboxTransport.workspace_version_write_file

    def observed_version_write(
        transport: E2BSandboxTransport,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ):
        result = original_version_write(
            transport,
            path,
            data,
            overwrite=overwrite,
            timeout_seconds=timeout_seconds,
        )
        recorder.record_success(path)
        return result

    default_version_context = lambda _recorder: patch.object(
        E2BSandboxTransport,
        "workspace_version_write_file",
        observed_version_write,
    )
    context_factory = version_write_context or default_version_context

    with tempfile.TemporaryDirectory(prefix="agnet-default-remote-canary-") as temporary:
        sessions = Path(temporary) / "sessions"
        remote_name = "remote-canary"
        local_name = "local-rollback"
        remote_directory = sessions / remote_name
        final_response_ready = threading.Event()
        script = _CanaryScript(final_response_ready)
        client = _ScriptedClient(script)
        canary_input = _CanaryInput(final_response_ready)
        runtime_factory = runtime_factory_builder(recorder, observed)
        approval_calls = 0

        def unexpected_approval(_broker: object, _request: object) -> bool:
            nonlocal approval_calls
            approval_calls += 1
            raise _HarnessError("default remote canary 意外进入审批。")

        local_write = Mock(side_effect=AssertionError("host write fallback"))
        local_read = Mock(side_effect=AssertionError("host read fallback"))
        write_registration = replace(registry._REGISTRY["write_file"], handler=local_write)
        read_registration = replace(registry._REGISTRY["read_file"], handler=local_read)
        observer = Mock(spec=Observability)

        try:
            with (
                patch.dict(os.environ, {_DEFAULT_BACKEND_ENV: "aliyun-sandbox"}),
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(
                        api_key="scripted-no-network",
                        model="scripted-default-remote-canary",
                    ),
                ),
                patch.object(main_module, "load_sandbox_settings", return_value=settings),
                patch.object(
                    main_module,
                    "cleanup_recovered_remote_session",
                    side_effect=startup_cleanup,
                ),
                patch.object(main_module, "create_execution_runtime", side_effect=runtime_factory),
                patch.object(main_module, "create_client", return_value=client),
                ready_model_service_patch(),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "MaintenanceService", return_value=_QuietMaintenance()),
                patch.object(main_module._InputBroker, "confirm", unexpected_approval),
                patch("app.terminal_input.create_terminal_reader", return_value=canary_input),
                patch.dict(
                    registry._REGISTRY,
                    {"write_file": write_registration, "read_file": read_registration},
                ),
                context_factory(recorder),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                stages.record("default_selected")
                stages.record("sandbox_validated")
                code = main_module.main(["--new-session", remote_name, "--plain"])
            if code != 0 or script.failed or script.stage != 3:
                raise _HarnessError("default remote canary 远程 CLI 失败。")
            if client.completions.requests != 3 or client.close_calls != 1:
                raise _HarnessError("default remote canary scripted client 边界无效。")
            if approval_calls != 0:
                raise _HarnessError("default remote canary 普通修改错误要求审批。")
            local_write.assert_not_called()
            local_read.assert_not_called()
            stages.record("runtime_created")

            runtime = observed.get("runtime")
            session_id_value = observed.get("session_id")
            if (
                not isinstance(runtime, ExecutionToolRuntime)
                or not runtime.closed
                or not isinstance(session_id_value, str)
                or _SESSION_ID_RE.fullmatch(session_id_value) is None
            ):
                raise _HarnessError("default remote canary runtime 终态无效。")
            session_id = session_id_value
            bound_id = _read_bound_session_id(remote_directory)
            if bound_id != session_id:
                raise _HarnessError("default remote canary remote 绑定不匹配。")
            with SessionEventWorkflow(
                storage_directory=remote_directory,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as restored:
                if restored.execution_backend_name != "aliyun-sandbox":
                    raise _HarnessError("default remote canary remote 绑定无效。")
            remote_binding = (remote_directory / "execution.json").read_bytes()
            stages.record("remote_bound")
            delta = recorder.snapshot()
            if delta != _VersionDelta(objects=1, generations=1):
                raise _HarnessError("default remote canary v2 增量不符合预期。")
            stages.record("mutation_committed")
            stages.record("read_verified")
            stages.record("remote_closed")

            forbidden_default = Mock(side_effect=AssertionError("explicit local loaded default"))
            forbidden_sandbox = Mock(side_effect=AssertionError("explicit local loaded sandbox"))
            forbidden_runtime = Mock(side_effect=AssertionError("explicit local created runtime"))
            forbidden_client = Mock(side_effect=AssertionError("explicit local created client"))
            with (
                patch.dict(os.environ, {_DEFAULT_BACKEND_ENV: "aliyun-sandbox"}),
                patch.object(main_module, "_SESSIONS_ROOT", sessions),
                patch.object(
                    main_module,
                    "load_settings",
                    return_value=Settings(api_key="scripted-no-network"),
                ),
                patch.object(main_module, "load_default_execution_backend", forbidden_default),
                patch.object(main_module, "load_sandbox_settings", forbidden_sandbox),
                patch.object(main_module, "create_execution_runtime", forbidden_runtime),
                patch.object(main_module, "create_client", forbidden_client),
                ready_model_service_patch(),
                patch("app.terminal_input.create_terminal_reader", return_value=_ExitInput()),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                local_code = main_module.main([
                    "--new-session",
                    local_name,
                    "--execution-backend",
                    "local",
                    "--plain",
                ])
            if local_code != 0:
                raise _HarnessError("default remote canary 显式 local 回滚失败。")
            for forbidden in (
                forbidden_default,
                forbidden_sandbox,
                forbidden_runtime,
                forbidden_client,
            ):
                forbidden.assert_not_called()
            with SessionEventWorkflow(
                storage_directory=sessions / local_name,
                execution_backend_name="local",
                recover=False,
            ) as restored_local:
                if restored_local.execution_backend_name != "local":
                    raise _HarnessError("default remote canary local 绑定无效。")
            if (remote_directory / "execution.json").read_bytes() != remote_binding:
                raise _HarnessError("default remote canary 回滚改写了 remote 绑定。")
            stages.record("local_rollback_bound")
        except BaseException as error:
            if isinstance(error, _HarnessError) or not isinstance(error, Exception):
                primary_error = error
            else:
                primary_error = _HarnessError(
                    f"default remote canary 在固定阶段 {stage} 安全失败。"
                )
        finally:
            runtime = observed.get("runtime")
            if isinstance(runtime, ExecutionToolRuntime):
                try:
                    runtime.close()
                except BaseException:
                    cleanup_failed = True
            if session_id is None:
                try:
                    session_id = _read_bound_session_id(remote_directory)
                except BaseException:
                    cleanup_failed = True
            if session_id is not None:
                try:
                    emergency_cleanup(settings, session_id)
                except BaseException:
                    cleanup_failed = True
                try:
                    active_failed = active_count_observer(settings, session_id) != 0
                except BaseException:
                    active_failed = True

        if primary_error is None and not cleanup_failed and not active_failed:
            stages.record("active_zero")
        if primary_error is not None:
            if cleanup_failed or active_failed:
                primary_error.add_note("default remote canary 的最终 active=0 未确认。")
            raise primary_error
        if cleanup_failed or active_failed or session_id is None:
            raise _HarnessError("default remote canary 的最终 active=0 未确认。")
        expected_stages = (
            "default_selected",
            "sandbox_validated",
            "runtime_created",
            "remote_bound",
            "mutation_committed",
            "read_verified",
            "remote_closed",
            "local_rollback_bound",
            "active_zero",
        )
        if stages.stages != expected_stages:
            raise _HarnessError("default remote canary 成功阶段不完整。")
        return recorder.snapshot()


def _execute_live_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[[SandboxSettings], _VersionDelta] = _run_default_remote_canary,
) -> _VersionDelta:
    selected = os.environ if environ is None else environ
    if selected.get(_LIVE_GATE) != "1":
        raise _HarnessError("default remote canary live gate 未启用。")
    try:
        settings = settings_loader()
        settings.require_enabled()
        settings.require_recovery_enabled()
    except Exception:
        raise _HarnessError("default remote canary 配置验证失败。") from None
    return runner(settings)


def _elapsed_milliseconds(
    started_ns: object,
    finished_ns: object,
) -> int:
    if (
        type(started_ns) is not int
        or type(finished_ns) is not int
        or started_ns < 0
        or finished_ns < started_ns
    ):
        raise _HarnessError("default remote canary batch 时钟无效。")
    elapsed = (finished_ns - started_ns) // 1_000_000
    if elapsed > _MAX_ATTEMPT_MILLISECONDS:
        raise _HarnessError("default remote canary batch 单次耗时越界。")
    return elapsed


def _make_batch_report(
    *,
    attempts: int,
    successes: int,
    failures: int,
    attempt_milliseconds: Sequence[int],
) -> _BatchReport:
    durations = tuple(attempt_milliseconds)
    return _BatchReport(
        attempts=attempts,
        successes=successes,
        failures=failures,
        active_zero=successes,
        objects=successes,
        generations=successes,
        attempt_milliseconds=durations,
        total_milliseconds=sum(durations),
        min_milliseconds=min(durations, default=0),
        max_milliseconds=max(durations, default=0),
    )


def _run_default_remote_canary_batch(
    settings: SandboxSettings,
    *,
    attempt_runner: Callable[[SandboxSettings], _VersionDelta] = (
        _run_default_remote_canary
    ),
    clock_ns: Callable[[], int] = time.monotonic_ns,
    summary_sink: Callable[[str], None] = print,
) -> _BatchReport:
    """Run the fixed three-attempt rollout, stopping at the first failure."""

    durations: list[int] = []
    successes = 0
    for attempt in range(1, _BATCH_ATTEMPTS + 1):
        started = clock_ns()
        try:
            delta = attempt_runner(settings)
            if delta != _VersionDelta(objects=1, generations=1):
                raise _HarnessError(
                    "default remote canary batch 单次增量无效。"
                )
        except Exception:
            durations.append(_elapsed_milliseconds(started, clock_ns()))
            report = _make_batch_report(
                attempts=attempt,
                successes=successes,
                failures=1,
                attempt_milliseconds=durations,
            )
            raise _BatchHarnessError(attempt, report) from None
        durations.append(_elapsed_milliseconds(started, clock_ns()))
        successes += 1

    report = _make_batch_report(
        attempts=_BATCH_ATTEMPTS,
        successes=successes,
        failures=0,
        attempt_milliseconds=durations,
    )
    summary_sink(report.summary())
    return report


def _execute_live_batch_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[[SandboxSettings], _BatchReport] = (
        _run_default_remote_canary_batch
    ),
) -> _BatchReport:
    selected = os.environ if environ is None else environ
    if selected.get(_LIVE_BATCH_GATE) != "1":
        raise _HarnessError("default remote canary batch live gate 未启用。")
    try:
        settings = settings_loader()
        settings.require_enabled()
        settings.require_recovery_enabled()
    except Exception:
        raise _HarnessError(
            "default remote canary batch 配置验证失败。"
        ) from None
    return runner(settings)


@unittest.skipUnless(
    os.environ.get(_LIVE_GATE) == "1",
    f"Set {_LIVE_GATE}=1 to run the real default-remote canary",
)
class AliyunSandboxDefaultRemoteCanaryLiveE2ETests(unittest.TestCase):
    def test_new_default_remote_session_mutates_reads_rolls_back_and_cleans(self) -> None:
        self.assertEqual(
            _execute_live_e2e(),
            _VersionDelta(objects=1, generations=1),
        )


@unittest.skipUnless(
    os.environ.get(_LIVE_BATCH_GATE) == "1",
    f"Set {_LIVE_BATCH_GATE}=1 to run the real three-session canary batch",
)
class AliyunSandboxDefaultRemoteCanaryBatchLiveE2ETests(unittest.TestCase):
    def test_three_new_default_remote_sessions_are_bounded_and_cleaned(self) -> None:
        report = _execute_live_batch_e2e()
        self.assertEqual(
            (
                report.attempts,
                report.successes,
                report.failures,
                report.active_zero,
                report.objects,
                report.generations,
            ),
            (3, 3, 0, 3, 3, 3),
        )


class DefaultRemoteCanaryHarnessOfflineTests(unittest.TestCase):
    def test_gate_precedes_settings_backend_cleanup_and_provider(self) -> None:
        with (
            patch(__name__ + ".load_sandbox_settings", side_effect=AssertionError("settings")) as loader,
            patch(__name__ + ".AliyunSandboxExecutionBackend", side_effect=AssertionError("backend")) as backend,
            patch(__name__ + ".cleanup_session_sandboxes", side_effect=AssertionError("cleanup")) as cleanup,
            patch(__name__ + "._provider_active_count", side_effect=AssertionError("provider")) as provider,
        ):
            with self.assertRaisesRegex(_HarnessError, "live gate"):
                _execute_live_e2e(environ={})
        for item in (loader, backend, cleanup, provider):
            item.assert_not_called()

    def test_default_suite_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the canary gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxDefaultRemoteCanaryLiveE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(__name__ + ".load_sandbox_settings", side_effect=AssertionError("settings")) as loader,
            patch(__name__ + ".AliyunSandboxExecutionBackend", side_effect=AssertionError("backend")) as backend,
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        loader.assert_not_called()
        backend.assert_not_called()

    def test_batch_gate_precedes_settings_runner_and_provider(self) -> None:
        with (
            patch(__name__ + ".load_sandbox_settings", side_effect=AssertionError("settings")) as loader,
            patch(__name__ + "._run_default_remote_canary_batch", side_effect=AssertionError("runner")) as runner,
            patch(__name__ + "._provider_active_count", side_effect=AssertionError("provider")) as provider,
        ):
            with self.assertRaisesRegex(_HarnessError, "batch live gate"):
                _execute_live_batch_e2e(environ={})
        loader.assert_not_called()
        runner.assert_not_called()
        provider.assert_not_called()

    def test_default_batch_suite_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_BATCH_GATE) == "1":
            self.skipTest("The operator explicitly enabled the batch gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxDefaultRemoteCanaryBatchLiveE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(__name__ + ".load_sandbox_settings", side_effect=AssertionError("settings")) as loader,
            patch(__name__ + ".AliyunSandboxExecutionBackend", side_effect=AssertionError("backend")) as backend,
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        loader.assert_not_called()
        backend.assert_not_called()

    def test_config_failure_is_fixed_and_never_runs_canary(self) -> None:
        runner = Mock(side_effect=AssertionError("runner"))
        with self.assertRaisesRegex(_HarnessError, "配置验证失败") as caught:
            _execute_live_e2e(
                environ={_LIVE_GATE: "1"},
                settings_loader=Mock(side_effect=RuntimeError("provider-secret")),
                runner=runner,
            )
        self.assertNotIn("provider-secret", str(caught.exception))
        runner.assert_not_called()

    def test_full_cli_canary_contract_runs_on_offline_product_runtime(self) -> None:
        sandbox = _Sandbox()
        transports: list[_MemoryTransport] = []
        startup_cleanup = Mock()
        emergency_cleanup = Mock()
        active = Mock(return_value=0)

        def builder(recorder, observed):
            class CountingTransport(_MemoryTransport):
                def workspace_version_write_file(
                    self, path, data, *, overwrite, timeout_seconds
                ):
                    result = super().workspace_version_write_file(
                        path,
                        data,
                        overwrite=overwrite,
                        timeout_seconds=timeout_seconds,
                    )
                    recorder.record_success(path)
                    return result

            def create(
                execution_backend,
                session_id,
                settings,
                *,
                collect_orphans=False,
                session_directory=None,
                session_cleanup_complete=False,
            ):
                if (
                    execution_backend != "aliyun-sandbox"
                    or collect_orphans is not True
                    or settings is not selected_settings
                    or not isinstance(session_directory, Path)
                    or session_cleanup_complete is not True
                ):
                    raise _HarnessError("offline canary runtime 绑定无效。")
                from app.sandbox_budget import SandboxBudget

                transport = CountingTransport(
                    "/workspace/agnet-study/" + session_id
                )
                transports.append(transport)
                runtime = ExecutionToolRuntime(AliyunSandboxExecutionBackend(
                    session_id,
                    settings,
                    session_cleanup_complete=session_cleanup_complete,
                    budget=SandboxBudget(
                        settings.max_session_seconds,
                        path=session_directory / "sandbox-budget.json",
                        session_id=session_id,
                    ),
                    sandbox_factory=lambda _settings, _metadata: sandbox,
                    persistence_sandbox_factory=lambda _settings, _metadata: SimpleNamespace(
                        sandbox_id="offline-canary-persistence",
                        kill=lambda: True,
                    ),
                    transport_factory=lambda _candidate: transport,
                    persistence_transport_factory=lambda _candidate: transport,
                    workspace_initializer=create_workspace_checkpoint_initializer(
                        _canary_snapshot(), collect_orphans=True
                    ),
                    recovery_provider=empty_recovery_provider(),
                ))
                observed["runtime"] = runtime
                observed["session_id"] = session_id
                return runtime

            return create

        selected_settings = _settings()
        self.assertEqual(
            _run_default_remote_canary(
                selected_settings,
                runtime_factory_builder=builder,
                version_write_context=lambda _recorder: nullcontext(),
                startup_cleanup=startup_cleanup,
                emergency_cleanup=emergency_cleanup,
                active_count_observer=active,
            ),
            _VersionDelta(1, 1),
        )
        self.assertEqual(len(transports), 1)
        self.assertEqual(
            transports[0].active[
                transports[0].workspace_root + "/" + _CANARY_PATH
            ],
            _CANARY_AFTER.encode(),
        )
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertEqual(startup_cleanup.call_count, 1)
        emergency_cleanup.assert_called_once()
        active.assert_called_once()

    def test_failure_after_identity_always_closes_cleans_and_checks_zero(self) -> None:
        settings = Mock(spec=SandboxSettings)
        cleanup = Mock()
        active = Mock(return_value=0)

        def failed_builder(_recorder, observed):
            runtime = Mock(spec=ExecutionToolRuntime)
            runtime.closed = False
            observed["runtime"] = runtime

            def fail(_backend, session_id, _settings, *, collect_orphans=False, session_directory=None, session_cleanup_complete=False):
                del collect_orphans, session_directory, session_cleanup_complete
                observed["session_id"] = session_id
                raise RuntimeError("runtime-secret")

            return fail

        with self.assertRaisesRegex(_HarnessError, "远程 CLI 失败") as caught:
            _run_default_remote_canary(
                settings,
                runtime_factory_builder=failed_builder,
                version_write_context=lambda _recorder: nullcontext(),
                startup_cleanup=Mock(),
                emergency_cleanup=cleanup,
                active_count_observer=active,
            )
        self.assertNotIn("runtime-secret", str(caught.exception))
        cleanup.assert_called_once()
        active.assert_called_once()

    def test_default_parser_is_strict_and_model_fields_cannot_select_backend(self) -> None:
        with patch.dict(os.environ, {_DEFAULT_BACKEND_ENV: "True"}):
            with self.assertRaisesRegex(ValueError, "local 或 aliyun-sandbox"):
                load_default_execution_backend()
        with self.assertRaises(ValidationError):
            WriteFileArguments.model_validate({
                "path": _CANARY_PATH,
                "content": _CANARY_AFTER,
                "mode": "overwrite",
                "execution_backend": "aliyun-sandbox",
            })

    def test_version_delta_is_bounded_typed_and_discards_coordinates(self) -> None:
        for value in (True, -1, _MAX_VERSION_WRITES + 1):
            with self.subTest(value=value), self.assertRaises(_HarnessError):
                _VersionDelta(value, 0)  # type: ignore[arg-type]
        recorder = _VersionWriteRecorder()
        recorder.record_success("/opaque/objects/" + "a" * 64)
        recorder.record_success("/opaque/generations/0001.json")
        self.assertEqual(recorder.snapshot(), _VersionDelta(1, 1))
        self.assertNotIn("opaque", repr(recorder.snapshot()))
        with self.assertRaises(_HarnessError):
            recorder.record_success("/opaque/objects/x/generations/y")

    def test_batch_runs_exactly_three_attempts_and_aggregates(self) -> None:
        settings = Mock(spec=SandboxSettings)
        calls: list[int] = []
        clock_values = iter((0, 1_000_000, 2_000_000, 4_000_000, 5_000_000, 8_000_000))
        summary = Mock()

        def attempt(_settings):
            self.assertIs(_settings, settings)
            calls.append(len(calls) + 1)
            return _VersionDelta(1, 1)

        report = _run_default_remote_canary_batch(
            settings,
            attempt_runner=attempt,
            clock_ns=lambda: next(clock_values),
            summary_sink=summary,
        )
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(
            report,
            _BatchReport(
                attempts=3,
                successes=3,
                failures=0,
                active_zero=3,
                objects=3,
                generations=3,
                attempt_milliseconds=(1, 2, 3),
                total_milliseconds=6,
                min_milliseconds=1,
                max_milliseconds=3,
            ),
        )
        summary.assert_called_once_with(
            "DEFAULT_REMOTE_CANARY_BATCH attempts=3 successes=3 failures=0 "
            "active_zero=3 objects=3 generations=3 total_ms=6 min_ms=1 "
            "max_ms=3 per_attempt_ms=1,2,3"
        )

    def test_batch_stops_after_second_failure_and_preserves_attempt_cleanup(self) -> None:
        settings = Mock(spec=SandboxSettings)
        calls: list[int] = []
        cleanup_attempts: list[int] = []
        clock_values = iter((0, 2_000_000, 3_000_000, 8_000_000))
        summary = Mock()

        def attempt(_settings):
            number = len(calls) + 1
            calls.append(number)
            try:
                if number == 2:
                    raise RuntimeError("provider-secret")
                return _VersionDelta(1, 1)
            finally:
                # The batch invokes the complete single-attempt boundary; its
                # finally must finish before fail-fast propagation.
                cleanup_attempts.append(number)

        with self.assertRaises(_BatchHarnessError) as caught:
            _run_default_remote_canary_batch(
                settings,
                attempt_runner=attempt,
                clock_ns=lambda: next(clock_values),
                summary_sink=summary,
            )
        self.assertEqual(calls, [1, 2])
        self.assertEqual(cleanup_attempts, [1, 2])
        self.assertEqual(caught.exception.attempt, 2)
        self.assertEqual(
            caught.exception.report,
            _BatchReport(
                attempts=2,
                successes=1,
                failures=1,
                active_zero=1,
                objects=1,
                generations=1,
                attempt_milliseconds=(2, 5),
                total_milliseconds=7,
                min_milliseconds=2,
                max_milliseconds=5,
            ),
        )
        self.assertNotIn("provider-secret", str(caught.exception))
        summary.assert_not_called()

    def test_batch_report_rejects_bool_bounds_and_inconsistent_totals(self) -> None:
        values: dict[str, object] = {
            "attempts": 3,
            "successes": 3,
            "failures": 0,
            "active_zero": 3,
            "objects": 3,
            "generations": 3,
            "attempt_milliseconds": (1, 2, 3),
            "total_milliseconds": 6,
            "min_milliseconds": 1,
            "max_milliseconds": 3,
        }
        invalid = (
            ("attempts", True),
            ("objects", 4),
            ("active_zero", 2),
            ("attempt_milliseconds", (1, True, 3)),
            ("total_milliseconds", 7),
            ("max_milliseconds", _MAX_ATTEMPT_MILLISECONDS + 1),
        )
        for field, value in invalid:
            arguments = {**values, field: value}
            with self.subTest(field=field), self.assertRaises(_HarnessError):
                _BatchReport(**arguments)  # type: ignore[arg-type]

    def test_external_environment_cannot_expand_fixed_batch(self) -> None:
        settings = Mock(spec=SandboxSettings)
        calls = 0
        clock_value = 0

        def attempt(_settings):
            nonlocal calls
            calls += 1
            return _VersionDelta(1, 1)

        def clock() -> int:
            nonlocal clock_value
            current = clock_value
            clock_value += 1_000_000
            return current

        with patch.dict(
            os.environ,
            {"RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_BATCH_ATTEMPTS": "99"},
        ):
            report = _run_default_remote_canary_batch(
                settings,
                attempt_runner=attempt,
                clock_ns=clock,
                summary_sink=lambda _line: None,
            )
        self.assertEqual(calls, _BATCH_ATTEMPTS)
        self.assertEqual(report.attempts, _BATCH_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
