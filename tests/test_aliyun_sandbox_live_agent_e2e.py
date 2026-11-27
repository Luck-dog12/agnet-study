"""Strictly gated live CodingAgent E2E for Aliyun Sandbox.

The completion client is scripted and never contacts DeepSeek.  Committed
immutable v1/v2/v3 OSS data remains immutable.  Cleanup cases exercise only
the product's exact failed-transaction rollback or its separately gated,
session-scoped reference-scanning orphan collector; this module must not add
Agent-facing or arbitrary-path deletion powers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import sys
from threading import Event, Lock
import time
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from pydantic import SecretStr

from app.agent import CodingAgent
from app.agent_status import AgentStatus
from app.async_tasks import AsyncTaskSnapshot
from app.config import Settings
from app.execution_backend import (
    ALIYUN_SANDBOX_OWNER_METADATA,
    ALIYUN_SANDBOX_SESSION_METADATA_KEY,
    AliyunSandboxExecutionBackend,
    ExecutionBackendCloseError,
    ExecutionBackendCreateError,
)
from app.execution_runtime import ExecutionToolRuntime, PreparedRemoteExecution
from app.observability import Observability
from app.permissions import route_mutation_approval
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from app.sandbox_transport import (
    E2BSandboxTransport,
    SandboxReadResult,
    SandboxWriteResult,
    SplitSandboxTransport,
)
from app.schemas import (
    AsyncTaskAcceptedOutput,
    AsyncTaskStatusOutput,
    BashShellOutput,
    CancelAsyncTaskOutput,
    CodeInterpreterOutput,
    EditorFileOutput,
    ListFilesOutput,
    ReadFileOutput,
    ToolCall,
    ToolMessage,
    ToolResult,
    WriteFileOutput,
)
from tool.registry import prepare_async_tool_call
from app.workflow import SessionEventWorkflow, ToolConfirmationRequest
from app.workspace_checkpoint import create_workspace_checkpoint_initializer
from app.workspace_seed import (
    WorkspaceSeedEntry,
    WorkspaceSeedSnapshot,
    _path_is_hard_excluded,
    _validate_snapshot,
)
from app.workspace_versions import (
    WorkspaceCommandManifest,
    WorkspaceOrphanGcReport,
    parse_manifest,
    version_session_root,
)
from tests.sandbox_cleanup_fakes import empty_recovery_provider


_LIVE_GATE = "RUN_ALIYUN_SANDBOX_LIVE_AGENT_E2E"
_LIVE_CANCEL_GATE = "RUN_ALIYUN_SANDBOX_LIVE_CANCEL_E2E"
_LIVE_TIMEOUT_GATE = "RUN_ALIYUN_SANDBOX_LIVE_TIMEOUT_E2E"
_LIVE_WAIT_UNCERTAINTY_GATE = "RUN_ALIYUN_SANDBOX_LIVE_WAIT_UNCERTAINTY_E2E"
_LIVE_PREBIND_UNCERTAINTY_GATE = (
    "RUN_ALIYUN_SANDBOX_LIVE_PREBIND_UNCERTAINTY_E2E"
)
_LIVE_KILL_ACK_UNCERTAINTY_GATE = (
    "RUN_ALIYUN_SANDBOX_LIVE_KILL_ACK_UNCERTAINTY_E2E"
)
_LIVE_ORPHAN_CLEANUP_GATE = "RUN_ALIYUN_SANDBOX_LIVE_ORPHAN_CLEANUP_E2E"
_LIVE_MAINTENANCE_GATE = "RUN_ALIYUN_SANDBOX_LIVE_MAINTENANCE_E2E"
_PROVIDER_LIST_PAGE_LIMIT = 8
_PROVIDER_LIST_MAX_PAGES = 3
_PROVIDER_LIST_MAX_ITEMS = _PROVIDER_LIST_PAGE_LIMIT * _PROVIDER_LIST_MAX_PAGES
_PROVIDER_LIST_REQUEST_TIMEOUT_SECONDS = 5.0
_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_MARKER = "ALIYUN_LIVE_AGENT_E2E_MARKER"
_FULL_ACCESS_PATH = "AGENTS.md"
_FULL_ACCESS_INITIAL = "# Synthetic full-access persistence probe\nstate: before\n"
_FULL_ACCESS_FINAL = "# Synthetic full-access persistence probe\nstate: after\n"
_ROOT = Path(__file__).resolve().parents[1]
_ASYNC_TERMINAL_MARKER = re.compile(
    r"^\[事件 #[0-9]+ \| async/task/(?P<kind>completed|failed|cancelled) "
    r"\| task=(?P<task_id>[0-9a-f]{32})\]\n"
)
_RECORDED_FAILURE_TEXT = "5C6 harness 已记录脱敏前置或终态失败。"
_WAIT_UNCERTAINTY_INJECTION_SECRET = "provider-wait-injection-secret"
_PREBIND_UNCERTAINTY_INJECTION_SECRET = "provider-prebind-injection-secret"
_KILL_ACK_WAIT_INJECTION_SECRET = "provider-kill-ack-wait-injection-secret"
_KILL_ACK_INJECTION_SECRET = "provider-kill-success-ack-secret"
_ORPHAN_CLEANUP_INJECTION_SECRET = "provider-orphan-cleanup-injection-secret"
_ORPHAN_BASELINE = "VALUE = 'baseline'\n"
_ORPHAN_MUTATED = "VALUE = 'mutated'\n"
_MAINTENANCE_JSON_A_BEFORE = b'{"z":1,"a":2}\n'
_MAINTENANCE_JSON_A_AFTER = b'{\n  "a": 2,\n  "z": 1\n}\n'
_MAINTENANCE_JSON_B_BEFORE = b'{"nested":{"z":0,"a":1},"items":[3,1]}\n'
_MAINTENANCE_JSON_B_AFTER = (
    b'{\n  "items": [\n    3,\n    1\n  ],\n  "nested": {\n'
    b'    "a": 1,\n    "z": 0\n  }\n}\n'
)
_MAINTENANCE_ORPHAN_BODY = b"unreferenced live maintenance object\n"
# Public ToolResult contract produced by tool.registry's remote async runner.
# Keep this exact: the old "远程..." spelling made the offline fixture agree
# with itself while rejecting the real, correctly sanitized terminal.
_REMOTE_EXECUTION_ERROR = "远端工作区工具执行失败。"
_BACKEND_STATES = frozenset({"ready", "create_failed"})
_BACKEND_STAGES = frozenset({
    "lease_create",
    "trusted_runtime",
    "system_python_probe",
    "runtime_path_check",
    "venv_create",
    "runtime_freeze",
    "venv_python_probe",
    "runtime_inspect",
    "workspace_initializer_pending",
    "workspace_initializer",
    "initializer_active_list",
    "initializer_checkpoint_list",
    "initializer_checkpoint_read",
    "initializer_checkpoint_write",
    "initializer_active_write",
    "initializer_active_read",
    "initializer_version_list",
    "initializer_version_read",
    "ready",
    "mutation_prepare",
    "mutation_prepare_completed",
    "mutation_commit_v2",
    "mutation_commit_completed",
    "command_prepare",
    "command_prepare_completed",
    "command_commit_v3",
    "command_commit_completed",
})
_BACKEND_STAGE_HISTORY_LIMIT = 64
_INITIALIZER_STAGES = frozenset({
    "workspace_initializer",
    "initializer_active_list",
    "initializer_checkpoint_list",
    "initializer_checkpoint_read",
    "initializer_checkpoint_write",
    "initializer_active_write",
    "initializer_active_read",
    "initializer_version_list",
    "initializer_version_read",
})


class _ProviderActiveListVerificationError(AssertionError):
    """Fixed-category failure that never carries provider response text."""


def _provider_active_list_result(
    settings: SandboxSettings,
    session_id: str,
) -> tuple[str, int]:
    """Return only a fixed status and count; discard all provider objects."""
    import_failed = False
    try:
        from e2b import Sandbox, SandboxQuery, SandboxState
    except Exception:
        import_failed = True
    if import_failed:
        return "provider_active_list_import_failed", 0

    owner_key, owner_value = ALIYUN_SANDBOX_OWNER_METADATA
    request_failed = False
    bounds_exceeded = False
    active_count = 0
    try:
        query = SandboxQuery(
            metadata={
                owner_key: owner_value,
                ALIYUN_SANDBOX_SESSION_METADATA_KEY: session_id,
            },
            state=[SandboxState.RUNNING, SandboxState.PAUSED],
        )
        paginator = Sandbox.list(
            query=query,
            limit=_PROVIDER_LIST_PAGE_LIMIT,
            api_key=settings.api_key.get_secret_value(),
            api_url=settings.api_url,
            domain=settings.domain,
            request_timeout=_PROVIDER_LIST_REQUEST_TIMEOUT_SECONDS,
        )
        pages = 0
        while paginator.has_next:
            if pages >= _PROVIDER_LIST_MAX_PAGES:
                bounds_exceeded = True
                break
            items = paginator.next_items(
                request_timeout=_PROVIDER_LIST_REQUEST_TIMEOUT_SECONDS
            )
            pages += 1
            active_count += len(items)
            if active_count > _PROVIDER_LIST_MAX_ITEMS:
                bounds_exceeded = True
                break
    except Exception:
        request_failed = True

    if request_failed:
        return "provider_active_list_request_failed", 0
    if bounds_exceeded:
        return "provider_active_list_bounds_exceeded", 0
    return "ok", active_count


def _verify_provider_session_has_no_active_sandboxes(
    settings: SandboxSettings,
    session_id: str,
    target_sandbox_id: str,
) -> int:
    """Boundedly prove that one session has no provider-active Sandbox.

    This live-test-only boundary deliberately imports E2B lazily and returns
    only the zero active count.  It never collects or renders IDs returned for
    other sandboxes, and provider exceptions are collapsed to fixed classes.
    """
    if not isinstance(settings, SandboxSettings):
        raise _ProviderActiveListVerificationError(
            "provider_active_list_invalid_settings"
        )
    if (
        not isinstance(session_id, str)
        or _SESSION_ID_RE.fullmatch(session_id) is None
        or not isinstance(target_sandbox_id, str)
        or not target_sandbox_id
        or len(target_sandbox_id) > 256
        or any(
            ord(character) < 32 or character.isspace()
            for character in target_sandbox_id
        )
    ):
        raise _ProviderActiveListVerificationError(
            "provider_active_list_invalid_target"
        )
    if (
        not settings.enabled
        or settings.api_key is None
        or settings.api_url is None
        or settings.domain is None
    ):
        raise _ProviderActiveListVerificationError(
            "provider_active_list_invalid_settings"
        )

    # The provider query is session-scoped.  The exact created ID is captured
    # by each live harness only to prove that it had a concrete lifecycle
    # target; it must not be copied into list diagnostics or retained results.
    del target_sandbox_id
    status, active_count = _provider_active_list_result(settings, session_id)
    if status != "ok":
        raise _ProviderActiveListVerificationError(status)
    if active_count != 0:
        raise _ProviderActiveListVerificationError(
            "provider_active_sandbox_present"
        )
    return 0


@dataclass(frozen=True)
class _MaintenanceSplitBinding:
    """Private live-harness proof for both resources in one split backend."""

    transport: SplitSandboxTransport
    execution_sandbox: object
    persistence_sandbox: object
    execution_sandbox_id: str
    persistence_sandbox_id: str


def _is_bounded_sandbox_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 256
        and not any(
            ord(character) < 32 or character.isspace()
            for character in value
        )
    )


def _require_maintenance_split_binding(
    backend: object,
    lease_sandbox_id: str,
) -> _MaintenanceSplitBinding:
    """Capture both physical resources without rendering provider identities."""

    condition = getattr(backend, "_condition", None)
    if condition is None:
        raise AssertionError("maintenance_split_binding_missing")
    with condition:
        transport = getattr(backend, "_transport", None)
        execution_sandbox = getattr(backend, "_execution_sandbox", None)
        persistence_sandbox = getattr(backend, "_persistence_sandbox", None)
    execution_sandbox_id = getattr(execution_sandbox, "sandbox_id", None)
    persistence_sandbox_id = getattr(persistence_sandbox, "sandbox_id", None)
    if (
        not isinstance(transport, SplitSandboxTransport)
        or execution_sandbox is None
        or persistence_sandbox is None
        or execution_sandbox is persistence_sandbox
        or not _is_bounded_sandbox_id(execution_sandbox_id)
        or not _is_bounded_sandbox_id(persistence_sandbox_id)
        or execution_sandbox_id == persistence_sandbox_id
        or lease_sandbox_id != execution_sandbox_id
    ):
        raise AssertionError("maintenance_split_binding_invalid")
    return _MaintenanceSplitBinding(
        transport=transport,
        execution_sandbox=execution_sandbox,
        persistence_sandbox=persistence_sandbox,
        execution_sandbox_id=execution_sandbox_id,
        persistence_sandbox_id=persistence_sandbox_id,
    )


def _verify_maintenance_split_session_closed(
    settings: SandboxSettings,
    session_id: str,
    sandbox_ids: tuple[str, str],
) -> int:
    """Prove session-wide active=0 after binding two distinct role resources."""

    if (
        len(sandbox_ids) != 2
        or not all(_is_bounded_sandbox_id(value) for value in sandbox_ids)
        or sandbox_ids[0] == sandbox_ids[1]
    ):
        raise AssertionError("maintenance_split_close_binding_invalid")
    # The provider query itself is session-scoped, so one zero count covers
    # both the execution and persistence role resources captured above.
    return _verify_provider_session_has_no_active_sandboxes(
        settings,
        session_id,
        sandbox_ids[0],
    )


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), data=data
    )


def _synthetic_snapshot() -> WorkspaceSeedSnapshot:
    return WorkspaceSeedSnapshot((
        _entry("README.md", b"Synthetic Aliyun live CodingAgent E2E.\n"),
        _entry("tests/__init__.py", b""),
        _entry(
            "tests/test_live_probe.py",
            (
                "import unittest\n\nimport live_probe\n\n\n"
                "class LiveProbeTests(unittest.TestCase):\n"
                "    def test_answer(self):\n"
                "        self.assertEqual(live_probe.answer(), 42)\n"
            ).encode("utf-8"),
        ),
    ))


def _cancellation_snapshot() -> WorkspaceSeedSnapshot:
    return WorkspaceSeedSnapshot((
        _entry("cancel_tests/__init__.py", b""),
        _entry(
            "cancel_tests/test_slow_probe.py",
            (
                "import time\nimport unittest\n\n\n"
                "class SlowProbeTests(unittest.TestCase):\n"
                "    def test_wait_for_cancellation(self):\n"
                "        time.sleep(60)\n"
            ).encode("utf-8"),
        ),
        _entry("live_probe.py", b"def answer():\n    return 42\n"),
        _entry("tests/__init__.py", b""),
        _entry(
            "tests/test_live_probe.py",
            (
                "import unittest\n\nimport live_probe\n\n\n"
                "class LiveProbeTests(unittest.TestCase):\n"
                "    def test_answer(self):\n"
                "        self.assertEqual(live_probe.answer(), 42)\n"
            ).encode("utf-8"),
        ),
    ))


def _timeout_snapshot() -> WorkspaceSeedSnapshot:
    """Small canonical project with one bounded slow and one quick test tree."""

    return WorkspaceSeedSnapshot((
        _entry("tests/__init__.py", b""),
        _entry(
            "tests/test_quick_probe.py",
            (
                "import unittest\n\n\n"
                "class QuickProbeTests(unittest.TestCase):\n"
                "    def test_quick_probe(self):\n"
                "        self.assertEqual(6 * 7, 42)\n"
            ).encode("utf-8"),
        ),
        _entry("timeout_tests/__init__.py", b""),
        _entry(
            "timeout_tests/test_slow_probe.py",
            (
                "import time\nimport unittest\n\n\n"
                "class SlowProbeTests(unittest.TestCase):\n"
                "    def test_exceeds_remote_deadline(self):\n"
                "        time.sleep(60)\n"
            ).encode("utf-8"),
        ),
    ))


def _orphan_cleanup_snapshot() -> WorkspaceSeedSnapshot:
    """Small first-generation overwrite fixture with unique session storage."""

    return WorkspaceSeedSnapshot((
        _entry("orphan_probe.py", _ORPHAN_BASELINE.encode("utf-8")),
        _entry("orphan_tests/__init__.py", b""),
        _entry(
            "orphan_tests/test_orphan_probe.py",
            (
                "import unittest\n\nimport orphan_probe\n\n\n"
                "class OrphanProbeTests(unittest.TestCase):\n"
                "    def test_baseline(self):\n"
                "        self.assertEqual(orphan_probe.VALUE, 'baseline')\n"
            ).encode("utf-8"),
        ),
    ))


def _maintenance_snapshot() -> WorkspaceSeedSnapshot:
    """Two non-canonical JSON files for one atomic v3 command generation."""

    return WorkspaceSeedSnapshot((
        _entry(
            "README.md",
            b"Synthetic live v3 restore and reference-scanning GC probe.\n",
        ),
        _entry("config/a.json", _MAINTENANCE_JSON_A_BEFORE),
        _entry("config/b.json", _MAINTENANCE_JSON_B_BEFORE),
    ))


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
            role="assistant", content=content, tool_calls=tool_calls,
            reasoning_content=None,
        ),
    )])


def _text(content: str) -> list[SimpleNamespace]:
    return [_chunk(content=content)]


def _tool(
    call_id: str, name: str, arguments: Mapping[str, object]
) -> list[SimpleNamespace]:
    raw = json.dumps(
        dict(arguments), ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    )
    return [_chunk(calls=((call_id, name, raw),))]


@dataclass(frozen=True, slots=True)
class _HarnessFailure:
    call_id: str
    status: str
    result_status: str | None
    error_class: str
    recovery_status: str | None = None

    def as_assertion(
        self,
        backend: _BackendReadinessDiagnostic | None = None,
    ) -> AssertionError:
        recovery = (
            ""
            if self.recovery_status is None
            else f" recovery_status={self.recovery_status}"
        )
        backend_suffix = "" if backend is None else backend.as_suffix()
        return AssertionError(
            "Live async harness observed an unexpected terminal: "
            f"call_id={self.call_id} status={self.status} "
            f"result_status={self.result_status or 'none'} "
            f"error_class={self.error_class}{recovery}{backend_suffix}."
        )


@dataclass(frozen=True, slots=True)
class _BackendReadinessDiagnostic:
    """Fixed, secret-free classification of the public readiness boundary."""

    backend_state: str
    backend_stage: str
    exception_chain: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.backend_state not in _BACKEND_STATES:
            raise ValueError("backend_state classification is invalid")
        if self.backend_stage not in _BACKEND_STAGES:
            raise ValueError("backend_stage classification is invalid")
        if any(
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is None
            for name in self.exception_chain
        ):
            raise ValueError("exception class classification is invalid")

    def as_suffix(self) -> str:
        chain = ">".join(self.exception_chain) if self.exception_chain else "none"
        return (
            f" backend_state={self.backend_state}"
            f" backend_stage={self.backend_stage}"
            f" exception_chain={chain}"
        )


class _BackendStageRecorder:
    """Test-only stage markers; never retain exceptions, SDK values or paths."""

    def __init__(self) -> None:
        self.stage = "lease_create"
        self.history = [self.stage]

    def record(self, stage: str) -> None:
        if stage not in _BACKEND_STAGES:
            raise ValueError("backend stage classification is invalid")
        self.stage = stage
        self.history.append(stage)
        overflow = len(self.history) - _BACKEND_STAGE_HISTORY_LIMIT
        if overflow > 0:
            del self.history[:overflow]

    def wrap_python_probe(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        def wrapped(
            transport: object,
            executable: str,
            timeout_seconds: int,
            *,
            user: str,
        ) -> object:
            system_probe = executable in {"python3", "python"} and user == "root"
            self.record(
                "system_python_probe" if system_probe else "venv_python_probe"
            )
            result = original(
                transport,
                executable,
                timeout_seconds,
                user=user,
            )
            if system_probe:
                self.record("runtime_path_check")
            return result

        return wrapped

    def wrap_run_command(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        def wrapped(
            transport: object,
            command: str,
            *,
            cwd: str,
            timeout_seconds: int,
            user: str,
        ) -> object:
            if isinstance(command, str) and " -I -m venv " in command:
                self.record("venv_create")
            return original(
                transport,
                command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                user=user,
            )

        return wrapped

    def wrap_runtime_freeze(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        def wrapped(
            transport: object,
            system_python: str,
            timeout_seconds: int,
        ) -> object:
            self.record("runtime_freeze")
            return original(transport, system_python, timeout_seconds)

        return wrapped

    def wrap_runtime_inspect(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        def wrapped(
            transport: object,
            system_python: str,
            timeout_seconds: int,
        ) -> object:
            self.record("runtime_inspect")
            return original(transport, system_python, timeout_seconds)

        return wrapped

    def wrap_trusted_runtime(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        def wrapped(
            transport: object,
            timeout_seconds: int,
            *,
            discovered: tuple[str, tuple[int, int, int]],
        ) -> object:
            self.record("trusted_runtime")
            result = original(
                transport,
                timeout_seconds,
                discovered=discovered,
            )
            self.record("workspace_initializer_pending")
            return result

        return wrapped

    def wrap_initializer_transport(
        self,
        stage: str,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        if stage not in _INITIALIZER_STAGES or stage == "workspace_initializer":
            raise ValueError("initializer stage classification is invalid")

        def wrapped(
            transport: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            # These transport methods are also used after backend readiness.
            # Record them only while the initializer is active so later Agent
            # reads/writes cannot overwrite the backend creation diagnostic.
            if self.stage in _INITIALIZER_STAGES:
                self.record(stage)
            return original(transport, *args, **kwargs)

        return wrapped


class _DiagnosticWorkspaceCoordinator:
    """Transparent test wrapper for initializer/prepare/commit boundaries."""

    def __init__(self, coordinator: object, stages: _BackendStageRecorder) -> None:
        self._coordinator = coordinator
        self._stages = stages

    def __call__(self, *args: object, **kwargs: object) -> object:
        self._stages.record("workspace_initializer")
        result = self._coordinator(*args, **kwargs)  # type: ignore[operator]
        self._stages.record("ready")
        return result

    def prepare(self, *args: object, **kwargs: object) -> object:
        self._stages.record("mutation_prepare")
        prepare = getattr(self._coordinator, "prepare")
        result = prepare(*args, **kwargs)
        self._stages.record("mutation_prepare_completed")
        return result

    def commit(self, *args: object, **kwargs: object) -> object:
        self._stages.record("mutation_commit_v2")
        commit = getattr(self._coordinator, "commit")
        result = commit(*args, **kwargs)
        self._stages.record("mutation_commit_completed")
        return result

    def prepare_command(self, *args: object, **kwargs: object) -> object:
        self._stages.record("command_prepare")
        prepare = getattr(self._coordinator, "prepare_command")
        result = prepare(*args, **kwargs)
        self._stages.record("command_prepare_completed")
        return result

    def commit_command(self, *args: object, **kwargs: object) -> object:
        self._stages.record("command_commit_v3")
        commit = getattr(self._coordinator, "commit_command")
        result = commit(*args, **kwargs)
        self._stages.record("command_commit_completed")
        return result

    @property
    def last_orphan_gc_report(self) -> WorkspaceOrphanGcReport | None:
        report = getattr(self._coordinator, "last_orphan_gc_report", None)
        if report is not None and not isinstance(report, WorkspaceOrphanGcReport):
            raise AssertionError("maintenance_gc_report_type_invalid")
        return report


def _project_exception_chain(error: Exception) -> tuple[str, ...]:
    """Return only class names belonging to this project's modules."""

    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        error_type = type(current)
        if error_type.__module__.startswith(("app.", "tool.")):
            names.append(error_type.__name__)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return tuple(names)


def _diagnose_backend_readiness(
    runtime: ExecutionToolRuntime,
    stages: _BackendStageRecorder,
) -> _BackendReadinessDiagnostic:
    """Classify the cached public readiness result without raw backend access."""

    try:
        runtime.ensure_ready()
    except Exception as error:
        return _BackendReadinessDiagnostic(
            backend_state="create_failed",
            backend_stage=stages.stage,
            exception_chain=_project_exception_chain(error),
        )
    return _BackendReadinessDiagnostic(
        backend_state="ready",
        backend_stage=stages.stage,
    )


@dataclass(frozen=True, slots=True)
class _AsyncCallInspection:
    completed: bool
    failure: _HarnessFailure | None = None


def _async_terminal_failure(
    call_id: str,
    *,
    status: str,
    result_status: str | None,
    error_class: str,
    recovery_status: str | None = None,
) -> _HarnessFailure:
    """Build a diagnostic record without retaining provider/tool error text."""

    return _HarnessFailure(
        call_id=call_id,
        status=status,
        result_status=result_status,
        error_class=error_class,
        recovery_status=recovery_status,
    )


def _async_harness_failure(
    call_id: str,
    *,
    status: str,
    result_status: str | None,
    error_class: str,
    recovery_status: str | None = None,
) -> AssertionError:
    return _async_terminal_failure(
        call_id,
        status=status,
        result_status=result_status,
        error_class=error_class,
        recovery_status=recovery_status,
    ).as_assertion()


def _terminal_result(content: str) -> ToolResult | None:
    marker = "\nresult="
    if marker not in content:
        return None
    try:
        return ToolResult.model_validate_json(content.rsplit(marker, 1)[1])
    except Exception:
        return None


def _inspect_async_call(
    messages: Sequence[Mapping[str, object]], call_id: str
) -> _AsyncCallInspection:
    """Inspect projected state without raising inside the scripted client.

    ``completed=False`` without a failure means accepted and still active.
    Every invalid or adverse terminal is returned as a sanitized record so the
    callback can produce a legal response and the test can raise outside the
    ``CodingAgent._request_model`` exception-wrapping boundary.
    """

    matching_results = [
        message for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == call_id
    ]
    if len(matching_results) != 1:
        return _AsyncCallInspection(failure=_async_terminal_failure(
            call_id, status="projection_invalid", result_status=None,
            error_class="async_task_binding_invalid",
        ), completed=False)
    content = matching_results[0].get("content")
    try:
        placeholder = ToolResult.model_validate_json(content)
    except Exception:
        return _AsyncCallInspection(failure=_async_terminal_failure(
            call_id, status="projection_invalid", result_status=None,
            error_class="async_task_placeholder_invalid",
        ), completed=False)
    if placeholder.status != "accepted" or not isinstance(
        placeholder.output, AsyncTaskAcceptedOutput
    ):
        return _AsyncCallInspection(failure=_async_terminal_failure(
            call_id, status="not_accepted", result_status=placeholder.status,
            error_class="async_task_not_accepted",
        ), completed=False)

    task_id = placeholder.output.task_id
    terminals: list[tuple[str, str]] = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(
            message.get("content"), str
        ):
            continue
        terminal_content = str(message["content"])
        match = _ASYNC_TERMINAL_MARKER.match(terminal_content)
        if match is not None and match.group("task_id") == task_id:
            terminals.append((match.group("kind"), terminal_content))
    if not terminals:
        # CodingAgent independently verifies that this accepted task is still
        # active before waiting after the callback's provisional reply.
        return _AsyncCallInspection(completed=False)
    if len(terminals) != 1:
        return _AsyncCallInspection(failure=_async_terminal_failure(
            call_id, status="projection_invalid", result_status=None,
            error_class="async_task_terminal_duplicate",
        ), completed=False)

    kind, terminal_content = terminals[0]
    terminal_result = _terminal_result(terminal_content)
    result_status = None if terminal_result is None else terminal_result.status
    if kind == "completed":
        if terminal_result is None or terminal_result.status != "success":
            return _AsyncCallInspection(failure=_async_terminal_failure(
                call_id, status="completed", result_status=result_status,
                error_class="async_task_completed_result_invalid",
            ), completed=False)
        return _AsyncCallInspection(completed=True)
    if kind == "cancelled":
        return _AsyncCallInspection(failure=_async_terminal_failure(
            call_id, status="cancelled", result_status=result_status,
            error_class="async_task_cancelled",
        ), completed=False)

    recovery_match = re.search(
        r"\nrecovery_status=(recorded|not_started|outcome_unknown)(?:\n|$)",
        terminal_content,
    )
    recovery_status = (
        None if recovery_match is None else recovery_match.group(1)
    )
    status = (
        recovery_status
        if recovery_status in {"not_started", "outcome_unknown"}
        else "failed"
    )
    error_class = {
        "outcome_unknown": "async_task_outcome_unknown",
        "not_started": "async_task_not_started",
    }.get(status, "async_task_failed")
    return _AsyncCallInspection(failure=_async_terminal_failure(
        call_id,
        status=status,
        result_status=result_status,
        error_class=error_class,
        recovery_status=recovery_status,
    ), completed=False)


def _async_call_completed(
    messages: Sequence[Mapping[str, object]], call_id: str
) -> bool:
    """Assertion-oriented adapter used outside scripted client callbacks."""

    inspection = _inspect_async_call(messages, call_id)
    if inspection.failure is not None:
        raise inspection.failure.as_assertion()
    return inspection.completed


def _inspect_tool_precondition(
    messages: Sequence[Mapping[str, object]],
    call_id: str,
    expected_output: type[object],
) -> _HarnessFailure | None:
    """Require one exact successful tool result without retaining its error."""

    matching_results = [
        message for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == call_id
    ]
    if len(matching_results) != 1:
        return _async_terminal_failure(
            call_id,
            status="precondition_invalid",
            result_status=None,
            error_class="tool_result_binding_invalid",
        )
    try:
        result = ToolResult.model_validate_json(matching_results[0].get("content"))
    except Exception:
        return _async_terminal_failure(
            call_id,
            status="precondition_invalid",
            result_status=None,
            error_class="tool_result_payload_invalid",
        )
    if result.status != "success":
        return _async_terminal_failure(
            call_id,
            status="precondition_failed",
            result_status=result.status,
            error_class="tool_result_not_success",
        )
    if result.error is not None or not isinstance(result.output, expected_output):
        return _async_terminal_failure(
            call_id,
            status="precondition_failed",
            result_status=result.status,
            error_class="tool_result_output_invalid",
        )
    return None


def _successful_tool_output(
    messages: Sequence[Mapping[str, object]],
    call_id: str,
    expected_output: type[object],
) -> tuple[object | None, _HarnessFailure | None]:
    failure = _inspect_tool_precondition(messages, call_id, expected_output)
    if failure is not None:
        return None, failure
    matching = [
        message for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == call_id
    ]
    try:
        result = ToolResult.model_validate_json(matching[0].get("content"))
    except Exception:  # pragma: no cover - precondition already validated this
        return None, _async_terminal_failure(
            call_id,
            status="precondition_invalid",
            result_status=None,
            error_class="tool_result_payload_invalid",
        )
    return result.output, None


def _accepted_task_binding(
    messages: Sequence[Mapping[str, object]], call_id: str
) -> tuple[str | None, _HarnessFailure | None]:
    matching = [
        message for message in messages
        if message.get("role") == "tool"
        and message.get("tool_call_id") == call_id
    ]
    if len(matching) != 1:
        return None, _async_terminal_failure(
            call_id,
            status="projection_invalid",
            result_status=None,
            error_class="async_task_binding_invalid",
        )
    try:
        result = ToolResult.model_validate_json(matching[0].get("content"))
    except Exception:
        return None, _async_terminal_failure(
            call_id,
            status="projection_invalid",
            result_status=None,
            error_class="async_task_placeholder_invalid",
        )
    if result.status != "accepted" or not isinstance(
        result.output, AsyncTaskAcceptedOutput
    ):
        return None, _async_terminal_failure(
            call_id,
            status="not_accepted",
            result_status=result.status,
            error_class="async_task_not_accepted",
        )
    return result.output.task_id, None


def _completed_bash_output(
    messages: Sequence[Mapping[str, object]], call_id: str
) -> tuple[BashShellOutput | None, _HarnessFailure | None]:
    """Return a projected completed result without retaining adverse text."""

    inspection = _inspect_async_call(messages, call_id)
    if inspection.failure is not None:
        return None, inspection.failure
    if not inspection.completed:
        return None, None
    task_id, failure = _accepted_task_binding(messages, call_id)
    if failure is not None:
        return None, failure
    assert task_id is not None
    terminal_contents = [
        str(message["content"])
        for message in messages
        if message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and (
            (match := _ASYNC_TERMINAL_MARKER.match(str(message["content"])))
            is not None
        )
        and match.group("task_id") == task_id
    ]
    if len(terminal_contents) != 1:
        return None, _async_terminal_failure(
            call_id,
            status="projection_invalid",
            result_status=None,
            error_class="async_task_terminal_binding_invalid",
        )
    result = _terminal_result(terminal_contents[0])
    if (
        result is None
        or result.status != "success"
        or result.error is not None
        or not isinstance(result.output, BashShellOutput)
    ):
        return None, _async_terminal_failure(
            call_id,
            status="completed",
            result_status=None if result is None else result.status,
            error_class="async_task_bash_result_invalid",
        )
    return result.output, None


class _Completions:
    def __init__(self, driver: Callable[[Sequence[Mapping[str, object]]], list[SimpleNamespace]]) -> None:
        self.driver = driver
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> list[SimpleNamespace]:
        self.requests.append(dict(kwargs))
        messages = kwargs.get("messages")
        if not isinstance(messages, list):
            raise AssertionError("Scripted client expected a message list.")
        return self.driver(messages)


class _Client:
    """OpenAI-compatible minimum surface with no network implementation."""

    def __init__(self, driver: Callable[[Sequence[Mapping[str, object]]], list[SimpleNamespace]]) -> None:
        self.completions = _Completions(driver)
        self.chat = SimpleNamespace(completions=self.completions)


class _AsyncRecordingScript:
    def __init__(self) -> None:
        self.recorded_failure: _HarnessFailure | None = None

    def _observe_async(
        self,
        messages: Sequence[Mapping[str, object]],
        call_id: str,
        waiting_text: str,
    ) -> tuple[bool, list[SimpleNamespace] | None]:
        if self.recorded_failure is not None:
            return False, _text(_RECORDED_FAILURE_TEXT)
        inspection = _inspect_async_call(messages, call_id)
        if inspection.failure is not None:
            self.recorded_failure = inspection.failure
            return False, _text(_RECORDED_FAILURE_TEXT)
        if not inspection.completed:
            return False, _text(waiting_text)
        return True, None

    def _require_tool_result(
        self,
        messages: Sequence[Mapping[str, object]],
        call_id: str,
        expected_output: type[object],
    ) -> list[SimpleNamespace] | None:
        if self.recorded_failure is not None:
            return _text(_RECORDED_FAILURE_TEXT)
        failure = _inspect_tool_precondition(
            messages, call_id, expected_output
        )
        if failure is None:
            return None
        self.recorded_failure = failure
        return _text(_RECORDED_FAILURE_TEXT)

    def raise_recorded_failure(
        self,
        backend: _BackendReadinessDiagnostic | None = None,
    ) -> None:
        if self.recorded_failure is not None:
            raise self.recorded_failure.as_assertion(backend)


class _ScriptA(_AsyncRecordingScript):
    def __init__(self, checkpoint_path: str) -> None:
        super().__init__()
        self.stage = 0
        self.denials: tuple[tuple[str, str, dict[str, object]], ...] = (
            ("a_protected", "write_file", {
                "path": ".env", "content": "forbidden=true\n",
                "mode": "create", "encoding": "utf-8",
            }),
            ("a_escape", "write_file", {
                "path": "../outside.py", "content": "forbidden=True\n",
                "mode": "create", "encoding": "utf-8",
            }),
            ("a_checkpoint", "write_file", {
                "path": checkpoint_path, "content": "forbidden=True\n",
                "mode": "create", "encoding": "utf-8",
            }),
            ("a_pending_write", "write_file", {
                "path": "data/pending/live-e2e.txt", "content": "forbidden\n",
                "mode": "create", "encoding": "utf-8",
            }),
            ("a_pending_list", "list_pending_changes", {"max_results": 20}),
        )

    def __call__(self, messages: Sequence[Mapping[str, object]]) -> list[SimpleNamespace]:
        if self.stage == 0:
            self.stage = 1
            return _tool("a_write", "write_file", {
                "path": "live_probe.py",
                "content": f"{_MARKER} = True\n\ndef answer():\n    return 41\n",
                "mode": "create", "encoding": "utf-8",
            })
        if self.stage == 1:
            failure_response = self._require_tool_result(
                messages, "a_write", WriteFileOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage = 2
            return _tool("a_test_fail", "bash_shell", {
                "command": "unittest", "paths": ["tests"], "timeout_seconds": 30,
            })
        if self.stage == 2:
            completed, response = self._observe_async(
                messages, "a_test_fail", "等待第一次远端测试完成。"
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage = 3
            return _tool("a_edit", "editor_file", {
                "path": "live_probe.py", "operation": "replace_text",
                "old_text": "return 41", "new_text": "return 42",
                "expected_replacements": 1,
            })
        if self.stage == 3:
            failure_response = self._require_tool_result(
                messages, "a_edit", EditorFileOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage = 4
            return _tool("a_test_pass", "bash_shell", {
                "command": "unittest", "paths": ["tests"], "timeout_seconds": 30,
            })
        if self.stage == 4:
            completed, response = self._observe_async(
                messages, "a_test_pass", "等待修复后的远端测试完成。"
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage = 5
        denial_index = self.stage - 5
        if 0 <= denial_index < len(self.denials):
            call_id, name, arguments = self.denials[denial_index]
            self.stage += 1
            return _tool(call_id, name, arguments)
        if denial_index == len(self.denials):
            self.stage += 1
            return _text("Sandbox A 的创建、失败测试、修复和通过测试已完成。")
        full_access_start = 6 + len(self.denials)
        if self.stage == full_access_start:
            self.stage += 1
            return _tool("a_full_write", "write_file", {
                "path": _FULL_ACCESS_PATH,
                "content": _FULL_ACCESS_INITIAL,
                "mode": "create",
                "encoding": "utf-8",
            })
        if self.stage == full_access_start + 1:
            failure_response = self._require_tool_result(
                messages, "a_full_write", WriteFileOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage += 1
            return _tool("a_full_edit", "editor_file", {
                "path": _FULL_ACCESS_PATH,
                "operation": "replace_text",
                "old_text": "state: before",
                "new_text": "state: after",
                "expected_replacements": 1,
            })
        if self.stage == full_access_start + 2:
            failure_response = self._require_tool_result(
                messages, "a_full_edit", EditorFileOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage += 1
            return _tool("a_full_test", "bash_shell", {
                "command": "unittest", "paths": ["tests"], "timeout_seconds": 30,
            })
        if self.stage == full_access_start + 3:
            completed, response = self._observe_async(
                messages, "a_full_test", "等待 full-access 远程测试完成。"
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage += 1
            return _text("Sandbox A 的 full-access 持久化写入和测试已完成。")
        raise AssertionError("Unexpected Sandbox A model request.")


class _ScriptB(_AsyncRecordingScript):
    def __init__(self) -> None:
        super().__init__()
        self.stage = 0

    def __call__(self, messages: Sequence[Mapping[str, object]]) -> list[SimpleNamespace]:
        if self.stage == 0:
            self.stage = 1
            return _tool("b_list", "list_files", {"pattern": "**/*", "max_results": 20})
        if self.stage == 1:
            failure_response = self._require_tool_result(
                messages, "b_list", ListFilesOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage = 2
            return _tool("b_read", "read_file", {"path": "live_probe.py", "max_bytes": 4096})
        if self.stage == 2:
            failure_response = self._require_tool_result(
                messages, "b_read", ReadFileOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage = 3
            return _tool(
                "b_read_full",
                "read_file",
                {"path": _FULL_ACCESS_PATH, "max_bytes": 4096},
            )
        if self.stage == 3:
            failure_response = self._require_tool_result(
                messages, "b_read_full", ReadFileOutput
            )
            if failure_response is not None:
                return failure_response
            self.stage = 4
            return _tool("b_test_pass", "bash_shell", {
                "command": "unittest", "paths": ["tests"], "timeout_seconds": 30,
            })
        if self.stage == 4:
            completed, response = self._observe_async(
                messages, "b_test_pass", "等待 Sandbox B 的远端复验完成。"
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage = 5
            return _text("Sandbox B 已恢复修改并重新通过测试。")
        raise AssertionError("Unexpected Sandbox B model request.")


class _CancelScript(_AsyncRecordingScript):
    """Drive accepted -> running -> cancel -> reuse without exposing raw errors."""

    def __init__(self, remote_handle_bound: Event) -> None:
        super().__init__()
        self.remote_handle_bound = remote_handle_bound
        self.stage = 0
        self.task_id: str | None = None
        self.query_call_id: str | None = None
        self.query_count = 0

    def _record(
        self,
        call_id: str,
        *,
        status: str,
        result_status: str | None,
        error_class: str,
    ) -> list[SimpleNamespace]:
        self.recorded_failure = _async_terminal_failure(
            call_id,
            status=status,
            result_status=result_status,
            error_class=error_class,
        )
        return _text(_RECORDED_FAILURE_TEXT)

    def __call__(
        self, messages: Sequence[Mapping[str, object]]
    ) -> list[SimpleNamespace]:
        if self.recorded_failure is not None:
            return _text(_RECORDED_FAILURE_TEXT)
        if self.stage == 0:
            self.stage = 1
            return _tool("cancel_long", "bash_shell", {
                "command": "unittest",
                "paths": ["cancel_tests"],
                "timeout_seconds": 90,
            })
        if self.stage == 1:
            task_id, failure = _accepted_task_binding(messages, "cancel_long")
            if failure is not None:
                self.recorded_failure = failure
                return _text(_RECORDED_FAILURE_TEXT)
            assert task_id is not None
            self.task_id = task_id
            self.query_call_id = "cancel_poll_0"
            self.stage = 2
            return _tool(
                self.query_call_id, "get_async_task", {"task_id": task_id}
            )
        if self.stage == 2:
            assert self.task_id is not None and self.query_call_id is not None
            output, failure = _successful_tool_output(
                messages, self.query_call_id, AsyncTaskStatusOutput
            )
            if failure is not None:
                self.recorded_failure = failure
                return _text(_RECORDED_FAILURE_TEXT)
            assert isinstance(output, AsyncTaskStatusOutput)
            if output.task_id != self.task_id:
                return self._record(
                    self.query_call_id,
                    status="precondition_invalid",
                    result_status="success",
                    error_class="async_task_query_binding_invalid",
                )
            if output.state == "accepted":
                self.query_count += 1
                if self.query_count > 8:
                    return self._record(
                        self.query_call_id,
                        status="accepted",
                        result_status="success",
                        error_class="async_task_never_started",
                    )
                self.query_call_id = f"cancel_poll_{self.query_count}"
                return _tool(
                    self.query_call_id,
                    "get_async_task",
                    {"task_id": self.task_id},
                )
            if output.state != "running":
                return self._record(
                    self.query_call_id,
                    status=output.state,
                    result_status=output.result_status,
                    error_class="async_task_not_running_before_cancel",
                )
            if not self.remote_handle_bound.wait(10):
                return self._record(
                    self.query_call_id,
                    status="running",
                    result_status=output.result_status,
                    error_class="remote_run_handle_not_bound",
                )
            self.stage = 3
            return _tool("cancel_request", "cancel_async_task", {
                "task_id": self.task_id,
                "reason": "5C4 live cancellation probe",
            })
        if self.stage == 3:
            assert self.task_id is not None
            output, failure = _successful_tool_output(
                messages, "cancel_request", CancelAsyncTaskOutput
            )
            if failure is not None:
                self.recorded_failure = failure
                return _text(_RECORDED_FAILURE_TEXT)
            assert isinstance(output, CancelAsyncTaskOutput)
            if (
                output.task_id != self.task_id
                or output.state != "cancelled"
                or not output.cancellation_requested
            ):
                return self._record(
                    "cancel_request",
                    status=output.state,
                    result_status="success",
                    error_class="async_task_cancel_result_invalid",
                )
            self.stage = 4
            return _tool(
                "cancel_verify",
                "get_async_task",
                {"task_id": self.task_id},
            )
        if self.stage == 4:
            assert self.task_id is not None
            output, failure = _successful_tool_output(
                messages, "cancel_verify", AsyncTaskStatusOutput
            )
            if failure is not None:
                self.recorded_failure = failure
                return _text(_RECORDED_FAILURE_TEXT)
            assert isinstance(output, AsyncTaskStatusOutput)
            if (
                output.task_id != self.task_id
                or output.state != "cancelled"
                or output.result_status is not None
                or output.result_json is not None
            ):
                return self._record(
                    "cancel_verify",
                    status=output.state,
                    result_status=output.result_status,
                    error_class="async_task_cancel_state_invalid",
                )
            self.stage = 5
            return _tool("cancel_short", "bash_shell", {
                "command": "unittest",
                "paths": ["tests"],
                "timeout_seconds": 30,
            })
        if self.stage == 5:
            completed, response = self._observe_async(
                messages, "cancel_short", "等待取消后的远程复验完成。"
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage = 6
            return _text("运行中取消已生效，同一 Sandbox 仍可继续执行。")
        raise AssertionError("Unexpected cancellation model request.")


class _TimeoutScript(_AsyncRecordingScript):
    """Drive a structured runner timeout, then prove the runtime is reusable."""

    def __init__(self) -> None:
        super().__init__()
        self.stage = 0

    def _record(
        self,
        call_id: str,
        *,
        status: str,
        result_status: str | None,
        error_class: str,
    ) -> list[SimpleNamespace]:
        self.recorded_failure = _async_terminal_failure(
            call_id,
            status=status,
            result_status=result_status,
            error_class=error_class,
        )
        return _text(_RECORDED_FAILURE_TEXT)

    def __call__(
        self, messages: Sequence[Mapping[str, object]]
    ) -> list[SimpleNamespace]:
        if self.recorded_failure is not None:
            return _text(_RECORDED_FAILURE_TEXT)
        if self.stage == 0:
            self.stage = 1
            return _tool("timeout_long", "bash_shell", {
                "command": "unittest",
                "paths": ["timeout_tests"],
                "timeout_seconds": 2,
            })
        if self.stage == 1:
            output, failure = _completed_bash_output(
                messages, "timeout_long"
            )
            if failure is not None:
                self.recorded_failure = failure
                return _text(_RECORDED_FAILURE_TEXT)
            if output is None:
                return _text("等待远程超时结果。")
            if (
                output.command != "unittest"
                or output.paths != ["timeout_tests"]
                or output.exit_code != 1
                or output.stdout != ""
                or output.stderr != "命令执行超时。"
                or output.truncated
                or output.result != ""
            ):
                return self._record(
                    "timeout_long",
                    status="completed_timeout_contract_invalid",
                    result_status="success",
                    error_class="remote_timeout_result_invalid",
                )
            self.stage = 2
            return _tool("timeout_short", "bash_shell", {
                "command": "unittest",
                "paths": ["tests"],
                "timeout_seconds": 30,
            })
        if self.stage == 2:
            completed, response = self._observe_async(
                messages,
                "timeout_short",
                "等待超时后的远程复验完成。",
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage = 3
            return _text("远程超时结果已净化，同一 Sandbox 仍可继续执行。")
        raise AssertionError("Unexpected timeout model request.")


class _WaitUncertaintyInjector:
    """Inject exactly once only after dispatch and handle binding are proven."""

    def __init__(self) -> None:
        self.dispatch_attempted = Event()
        self.handle_bound = Event()
        self._lock = Lock()
        self.injection_count = 0

    def record_dispatch(self) -> None:
        self.dispatch_attempted.set()

    def record_bound_handle(self) -> None:
        if not self.dispatch_attempted.is_set():
            raise AssertionError("wait_uncertainty_bind_before_dispatch")
        self.handle_bound.set()

    def inject_wait_failure(self, _handle: object) -> object:
        if not self.dispatch_attempted.is_set() or not self.handle_bound.is_set():
            raise AssertionError("wait_uncertainty_injection_order_invalid")
        with self._lock:
            if self.injection_count != 0:
                raise AssertionError("wait_uncertainty_injection_repeated")
            self.injection_count = 1
        # This deliberately replaces _E2BRunHandle.wait and must never call
        # the real provider wait method: the result is intentionally unknown.
        raise ValueError(_WAIT_UNCERTAINTY_INJECTION_SECRET)


class _PrebindUncertaintyInjector:
    """Fail once after raw-handle wrapping, before Prepared binding."""

    def __init__(self) -> None:
        self.dispatch_attempted = Event()
        self.raw_handle_initialized = Event()
        self.bind_called = Event()
        self._lock = Lock()
        self.injection_count = 0

    def record_dispatch(self) -> None:
        self.dispatch_attempted.set()

    def record_bind_call(self) -> None:
        self.bind_called.set()

    def inject_after_handle_init(self) -> object:
        if not self.dispatch_attempted.is_set() or self.bind_called.is_set():
            raise AssertionError("prebind_uncertainty_injection_order_invalid")
        with self._lock:
            if self.injection_count != 0:
                raise AssertionError("prebind_uncertainty_injection_repeated")
            self.injection_count = 1
        self.raw_handle_initialized.set()
        raise ValueError(_PREBIND_UNCERTAINTY_INJECTION_SECRET)


def _prebind_injected_handle_initializer(
    original: Callable[..., None],
    injector: _PrebindUncertaintyInjector,
) -> Callable[..., None]:
    """Wrap a real handle first, then inject the fixed pre-bind failure."""

    def injected(
        handle: object,
        raw_handle: object,
        *,
        decode: Callable[[object], object],
        cleanup: Callable[[], None],
    ) -> None:
        original(handle, raw_handle, decode=decode, cleanup=cleanup)
        injector.inject_after_handle_init()

    return injected


class _KillAckUncertaintyInjector:
    """Lose only the acknowledgement after one proven successful kill."""

    def __init__(self) -> None:
        self.dispatch_attempted = Event()
        self.handle_bound = Event()
        self.original_kill_succeeded = Event()
        self.close_error_observed = Event()
        self._lock = Lock()
        self.wait_injection_count = 0
        self.original_kill_call_count = 0
        self.post_success_injection_count = 0

    def record_dispatch(self) -> None:
        self.dispatch_attempted.set()

    def record_bound_handle(self) -> None:
        if not self.dispatch_attempted.is_set():
            raise AssertionError("kill_ack_bind_before_dispatch")
        self.handle_bound.set()

    def inject_wait_failure(self, _handle: object) -> object:
        if not self.dispatch_attempted.is_set() or not self.handle_bound.is_set():
            raise AssertionError("kill_ack_wait_injection_order_invalid")
        with self._lock:
            if self.wait_injection_count != 0:
                raise AssertionError("kill_ack_wait_injection_repeated")
            self.wait_injection_count = 1
        raise ValueError(_KILL_ACK_WAIT_INJECTION_SECRET)

    def wrap_original_kill(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        """Call the concrete Sandbox kill first; inject only after ``True``."""

        if not callable(original):
            raise TypeError("original kill must be callable")

        def injected_kill(*args: object, **kwargs: object) -> object:
            with self._lock:
                if self.original_kill_call_count != 0:
                    raise AssertionError("kill_ack_original_kill_repeated")
                self.original_kill_call_count = 1
            result = original(*args, **kwargs)
            if result is True:
                self.original_kill_succeeded.set()
            if self.wait_injection_count != 1 or result is not True:
                # Preserve the real provider result.  False (or any other
                # value) is not a successful kill acknowledgement.  A cleanup
                # that runs before the wait fault is also allowed through, so
                # an unrelated harness failure can never block real cleanup.
                return result
            with self._lock:
                if self.post_success_injection_count != 0:
                    raise AssertionError("kill_ack_post_success_injection_repeated")
                self.post_success_injection_count = 1
            raise ValueError(_KILL_ACK_INJECTION_SECRET)

        return injected_kill

    def record_close_error(self, error: BaseException) -> None:
        """Prove the product converted the injected error to its fixed type."""

        if (
            type(error) is not ExecutionBackendCloseError
            or str(error) != "Aliyun Sandbox kill() 失败；backend 已关闭。"
            or type(error.__cause__) is not ValueError
            or str(error.__cause__) != _KILL_ACK_INJECTION_SECRET
            or not self.original_kill_succeeded.is_set()
            or self.post_success_injection_count != 1
        ):
            raise AssertionError("kill_ack_close_error_contract_invalid")
        if self.close_error_observed.is_set():
            raise AssertionError("kill_ack_close_error_repeated")
        self.close_error_observed.set()


class _WaitUncertaintyScript(_AsyncRecordingScript):
    """Accept exactly the sanitized failed terminal caused by unknown wait."""

    _CALL_ID = "wait_uncertain"
    _DIAGNOSTIC_PREFIX = "wait_uncertainty"
    _INJECTION_SECRETS = (_WAIT_UNCERTAINTY_INJECTION_SECRET,)
    _WAITING_TEXT = "等待远程 wait 终态确认。"
    _SUCCESS_TEXT = "远程 wait 结果无法确认，runtime 已完成隔离。"

    def __init__(self, runtime_closed: Callable[[], bool]) -> None:
        super().__init__()
        self._runtime_closed = runtime_closed
        self.stage = 0
        self.expected_failure_observed = False

    def _record(
        self,
        *,
        status: str,
        result_status: str | None,
        error_class: str,
        recovery_status: str | None = None,
    ) -> list[SimpleNamespace]:
        self.recorded_failure = _async_terminal_failure(
            self._CALL_ID,
            status=status,
            result_status=result_status,
            error_class=error_class,
            recovery_status=recovery_status,
        )
        return _text(_RECORDED_FAILURE_TEXT)

    @classmethod
    def _terminal_result_for_call(
        cls,
        messages: Sequence[Mapping[str, object]],
    ) -> ToolResult | None:
        matching_results = [
            message for message in messages
            if message.get("role") == "tool"
            and message.get("tool_call_id") == cls._CALL_ID
        ]
        if len(matching_results) != 1:
            return None
        try:
            placeholder = ToolResult.model_validate_json(
                matching_results[0].get("content")
            )
        except Exception:
            return None
        if not isinstance(placeholder.output, AsyncTaskAcceptedOutput):
            return None
        task_id = placeholder.output.task_id
        terminals = [
            str(message["content"])
            for message in messages
            if message.get("role") == "user"
            and isinstance(message.get("content"), str)
            and (
                match := _ASYNC_TERMINAL_MARKER.match(str(message["content"]))
            ) is not None
            and match.group("task_id") == task_id
        ]
        if len(terminals) != 1:
            return None
        return _terminal_result(terminals[0])

    def __call__(
        self, messages: Sequence[Mapping[str, object]]
    ) -> list[SimpleNamespace]:
        if self.recorded_failure is not None:
            return _text(_RECORDED_FAILURE_TEXT)
        if self.stage == 0:
            self.stage = 1
            return _tool(self._CALL_ID, "bash_shell", {
                "command": "unittest",
                "paths": ["tests"],
                "timeout_seconds": 30,
            })
        if self.stage == 1:
            inspection = _inspect_async_call(messages, self._CALL_ID)
            if not inspection.completed and inspection.failure is None:
                return _text(self._WAITING_TEXT)
            expected = inspection.failure
            if (
                inspection.completed
                or expected is None
                or expected.status != "failed"
                or expected.result_status != "error"
                or expected.recovery_status != "recorded"
            ):
                return self._record(
                    status=(
                        "completed"
                        if inspection.completed
                        else "terminal_contract_invalid"
                    ),
                    result_status=(
                        None if expected is None else expected.result_status
                    ),
                    error_class=f"{self._DIAGNOSTIC_PREFIX}_terminal_invalid",
                    recovery_status=(
                        None if expected is None else expected.recovery_status
                    ),
                )
            result = self._terminal_result_for_call(messages)
            if (
                result is None
                or result.status != "error"
                or result.error != _REMOTE_EXECUTION_ERROR
                or result.output != ""
            ):
                return self._record(
                    status="failed",
                    result_status=expected.result_status,
                    error_class=f"{self._DIAGNOSTIC_PREFIX}_result_invalid",
                    recovery_status=expected.recovery_status,
                )
            rendered = json.dumps(
                list(messages),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if any(secret in rendered for secret in self._INJECTION_SECRETS):
                return self._record(
                    status="failed",
                    result_status="error",
                    error_class=f"{self._DIAGNOSTIC_PREFIX}_secret_leaked",
                    recovery_status="recorded",
                )
            if not self._runtime_closed():
                return self._record(
                    status="failed",
                    result_status="error",
                    error_class="runtime_not_fenced_before_terminal_followup",
                    recovery_status="recorded",
                )
            self.expected_failure_observed = True
            self.stage = 2
            return _text(self._SUCCESS_TEXT)
        raise AssertionError("Unexpected remote-uncertainty model request.")


class _PrebindUncertaintyScript(_WaitUncertaintyScript):
    """Accept only a sanitized failure after a raw handle is lost pre-bind."""

    _CALL_ID = "prebind_uncertain"
    _DIAGNOSTIC_PREFIX = "prebind_uncertainty"
    _INJECTION_SECRETS = (_PREBIND_UNCERTAINTY_INJECTION_SECRET,)
    _WAITING_TEXT = "等待远程 pre-bind 终态确认。"
    _SUCCESS_TEXT = "远程句柄绑定前结果无法确认，runtime 已完成隔离。"


class _KillAckUncertaintyScript(_WaitUncertaintyScript):
    """Accept a sanitized failure after kill succeeded but ack was lost."""

    _CALL_ID = "kill_ack_uncertain"
    _DIAGNOSTIC_PREFIX = "kill_ack_uncertainty"
    _INJECTION_SECRETS = (
        _KILL_ACK_WAIT_INJECTION_SECRET,
        _KILL_ACK_INJECTION_SECRET,
    )
    _WAITING_TEXT = "等待远程 kill 成功确认故障终态。"
    _SUCCESS_TEXT = "远程 kill 已成功但确认丢失，runtime 已完成隔离。"


class _OrphanCleanupInjector:
    """Fail one exact manifest publish and observe only real object deletes."""

    def __init__(
        self,
        session_root: str,
        expected_objects: Mapping[str, tuple[int, str]],
    ) -> None:
        self.session_root = session_root
        self.expected_objects = dict(expected_objects)
        self._lock = Lock()
        self.injection_count = 0
        self.successful_cleanup_paths: list[str] = []

    def _is_target_manifest(self, path: object) -> bool:
        if not isinstance(path, str):
            return False
        prefix = self.session_root + "/generations/"
        if not path.startswith(prefix) or posixpath.normpath(path) != path:
            return False
        name = path[len(prefix) :]
        return re.fullmatch(r"[0-9]{16}-[0-9a-f]{64}\.json", name) is not None

    def wrap_version_write(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        if not callable(original):
            raise TypeError("original version write must be callable")

        def wrapped(
            transport: object,
            path: str,
            data: bytes,
            *,
            overwrite: bool,
            timeout_seconds: int,
        ) -> object:
            if self._is_target_manifest(path):
                if (
                    not isinstance(data, bytes)
                    or overwrite is not False
                    or type(timeout_seconds) is not int
                    or timeout_seconds <= 0
                ):
                    raise AssertionError("orphan_cleanup_manifest_call_invalid")
                with self._lock:
                    if self.injection_count != 0:
                        raise AssertionError(
                            "orphan_cleanup_manifest_injection_repeated"
                        )
                    self.injection_count = 1
                # The provider manifest writer is deliberately not called.
                raise ValueError(_ORPHAN_CLEANUP_INJECTION_SECRET)
            return original(
                transport,
                path,
                data,
                overwrite=overwrite,
                timeout_seconds=timeout_seconds,
            )

        return wrapped

    def wrap_object_cleanup(
        self,
        original: Callable[..., object],
    ) -> Callable[..., object]:
        if not callable(original):
            raise TypeError("original object cleanup must be callable")

        def wrapped(
            transport: object,
            path: str,
            *,
            expected_size: int,
            expected_sha256: str,
            timeout_seconds: int,
        ) -> object:
            expected = self.expected_objects.get(path)
            if expected != (expected_size, expected_sha256):
                raise AssertionError("orphan_cleanup_delete_binding_invalid")
            result = original(
                transport,
                path,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                timeout_seconds=timeout_seconds,
            )
            with self._lock:
                if path in self.successful_cleanup_paths:
                    raise AssertionError("orphan_cleanup_delete_repeated")
                self.successful_cleanup_paths.append(path)
            return result

        return wrapped


class _OrphanCleanupScript(_AsyncRecordingScript):
    """Accept one known mutation failure, then prove the runtime is reusable."""

    def __init__(self) -> None:
        super().__init__()
        self.stage = 0
        self.expected_failure_observed = False

    def _record(self, call_id: str, error_class: str) -> list[SimpleNamespace]:
        self.recorded_failure = _async_terminal_failure(
            call_id,
            status="failed",
            result_status="error",
            error_class=error_class,
            recovery_status="recorded",
        )
        return _text(_RECORDED_FAILURE_TEXT)

    @staticmethod
    def _result_for(
        messages: Sequence[Mapping[str, object]],
        call_id: str,
    ) -> ToolResult | None:
        matching = [
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("tool_call_id") == call_id
        ]
        if len(matching) != 1:
            return None
        try:
            return ToolResult.model_validate_json(matching[0].get("content"))
        except Exception:
            return None

    def __call__(
        self, messages: Sequence[Mapping[str, object]]
    ) -> list[SimpleNamespace]:
        if self.recorded_failure is not None:
            return _text(_RECORDED_FAILURE_TEXT)
        if self.stage == 0:
            self.stage = 1
            return _tool("orphan_write", "write_file", {
                "path": "orphan_probe.py",
                "content": _ORPHAN_MUTATED,
                "mode": "overwrite",
                "encoding": "utf-8",
            })
        if self.stage == 1:
            result = self._result_for(messages, "orphan_write")
            rendered = json.dumps(
                list(messages),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if (
                result is None
                or result.status != "error"
                or result.output != ""
                or result.error != _REMOTE_EXECUTION_ERROR
            ):
                return self._record(
                    "orphan_write", "orphan_cleanup_result_invalid"
                )
            if _ORPHAN_CLEANUP_INJECTION_SECRET in rendered:
                return self._record(
                    "orphan_write", "orphan_cleanup_secret_leaked"
                )
            self.expected_failure_observed = True
            self.stage = 2
            return _tool("orphan_read", "read_file", {
                "path": "orphan_probe.py",
                "offset": 0,
                "max_bytes": 4096,
            })
        if self.stage == 2:
            output, failure = _successful_tool_output(
                messages,
                "orphan_read",
                ReadFileOutput,
            )
            if failure is not None or not isinstance(output, ReadFileOutput):
                return self._record(
                    "orphan_read", "orphan_cleanup_read_result_invalid"
                )
            if (
                output.path != "orphan_probe.py"
                or output.content != _ORPHAN_BASELINE
                or output.offset != 0
                or output.truncated
            ):
                return self._record(
                    "orphan_read", "orphan_cleanup_baseline_not_restored"
                )
            self.stage = 3
            return _tool("orphan_code", "code_interpreter", {
                "code": "6 * 7",
                "timeout_seconds": 5,
            })
        if self.stage == 3:
            completed, response = self._observe_async(
                messages,
                "orphan_code",
                "等待事务清理后的远端执行复验。",
            )
            if not completed:
                return response or _text(_RECORDED_FAILURE_TEXT)
            self.stage = 4
            return _text("事务 orphan 已清理，baseline 与同一 Sandbox 均已复验。")
        raise AssertionError("Unexpected orphan-cleanup model request.")


class _MaintenanceScriptA(_AsyncRecordingScript):
    """Commit one v3 generation, then probe the sealed-path host gate."""

    def __init__(self) -> None:
        super().__init__()
        self.stage = 0

    def __call__(
        self,
        messages: Sequence[Mapping[str, object]],
    ) -> list[SimpleNamespace]:
        if self.stage == 0:
            self.stage = 1
            return _tool("maintenance_format", "bash_shell", {
                "command": "format_json",
                "paths": ["config/b.json", "config/a.json"],
                "timeout_seconds": 30,
            })
        if self.stage == 1:
            failure_response = self._require_tool_result(
                messages,
                "maintenance_format",
                BashShellOutput,
            )
            if failure_response is not None:
                return failure_response
            self.stage = 2
            return _tool(
                "maintenance_sealed",
                "read_file",
                {"path": ".env", "max_bytes": 1024},
            )
        if self.stage == 2:
            self.stage = 3
            return _text("Sandbox A 已发布 v3 generation 并验证安全门。")
        raise AssertionError("Unexpected maintenance Sandbox A model request.")


class _MaintenanceScriptB(_AsyncRecordingScript):
    """Read both restored v3 files after the host-maintenance GC."""

    def __init__(self) -> None:
        super().__init__()
        self.stage = 0

    def __call__(
        self,
        messages: Sequence[Mapping[str, object]],
    ) -> list[SimpleNamespace]:
        if self.stage == 0:
            self.stage = 1
            return _tool(
                "maintenance_read_a",
                "read_file",
                {"path": "config/a.json", "max_bytes": 4096},
            )
        if self.stage == 1:
            failure_response = self._require_tool_result(
                messages,
                "maintenance_read_a",
                ReadFileOutput,
            )
            if failure_response is not None:
                return failure_response
            self.stage = 2
            return _tool(
                "maintenance_read_b",
                "read_file",
                {"path": "config/b.json", "max_bytes": 4096},
            )
        if self.stage == 2:
            failure_response = self._require_tool_result(
                messages,
                "maintenance_read_b",
                ReadFileOutput,
            )
            if failure_response is not None:
                return failure_response
            self.stage = 3
            return _text("Sandbox B 已恢复 v3 generation 并完成 GC 复验。")
        raise AssertionError("Unexpected maintenance Sandbox B model request.")


class _DirectKillFalseInjector:
    """Replace only one concrete Sandbox direct kill with a strict ``False``."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.call_count = 0

    def __call__(self, *_args: object, **_kwargs: object) -> bool:
        with self._lock:
            if self.call_count != 0:
                raise AssertionError("maintenance_direct_kill_repeated")
            self.call_count = 1
        return False


class _FormatJsonStagingObserver:
    """Prove the trusted runner removed its random staging child."""

    def __init__(self, original: Callable[..., object]) -> None:
        if not callable(original):
            raise TypeError("format_json original must be callable")
        self._original = original
        self.call_count = 0

    def __call__(
        self,
        transport: E2BSandboxTransport,
        paths: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> object:
        if self.call_count != 0:
            raise AssertionError("maintenance_format_json_repeated")
        result = self._original(
            transport,
            paths,
            timeout_seconds=timeout_seconds,
        )
        transaction_parent = (
            transport._workspace_root + "/.agent-internal/transactions"
        )
        try:
            children = transport._sandbox.files.list(
                transaction_parent,
                depth=1,
                request_timeout=timeout_seconds,
            )
        except Exception:
            raise AssertionError("maintenance_staging_check_failed") from None
        if list(children):
            raise AssertionError("maintenance_staging_not_empty")
        self.call_count = 1
        return result


@contextmanager
def _managed_agent(
    settings: SandboxSettings,
    session_directory: Path,
    snapshot: WorkspaceSeedSnapshot,
    client: _Client,
    confirmation_callback: Callable[[ToolConfirmationRequest], bool],
    backend_stages: _BackendStageRecorder,
    *, recover: bool,
    collect_orphans: bool = False,
    after_close: Callable[[], None] | None = None,
) -> Iterator[tuple[CodingAgent, ExecutionToolRuntime]]:
    """Close Agent/runtime/Sandbox without replacing a primary failure."""
    session: SessionEventWorkflow | None = None
    runtime: ExecutionToolRuntime | None = None
    agent: CodingAgent | None = None
    primary_error: BaseException | None = None
    try:
        session = SessionEventWorkflow(
            storage_directory=session_directory, recover=recover,
            execution_backend_name="aliyun-sandbox",
        )
        coordinator = _DiagnosticWorkspaceCoordinator(
            create_workspace_checkpoint_initializer(
                snapshot,
                collect_orphans=collect_orphans,
            ),
            backend_stages,
        )
        runtime = ExecutionToolRuntime(AliyunSandboxExecutionBackend(
            session.session_id,
            settings,
            workspace_initializer=coordinator,
        ))
        agent = CodingAgent(
            client,
            Settings(api_key="scripted-no-network", model="scripted-live-e2e"),
            system_prompt="Use only the scripted tool calls supplied by this test.",
            max_tool_rounds=16,
            permission_mode="partial-control",
            confirmation_callback=confirmation_callback,
            session_workflow=session,
            observability=Mock(spec=Observability),
            execution_runtime=runtime,
        )
        trusted_runtime = E2BSandboxTransport._initialize_trusted_runtime
        python_probe = E2BSandboxTransport._probe_python
        run_command = E2BSandboxTransport._run_command
        runtime_freeze = E2BSandboxTransport._freeze_runtime
        runtime_inspect = E2BSandboxTransport._inspect_runtime
        active_list = E2BSandboxTransport.list_files
        checkpoint_list = E2BSandboxTransport.persistence_list_files
        checkpoint_read = E2BSandboxTransport.persistence_read_file
        checkpoint_write = E2BSandboxTransport.persistence_write_file
        active_write = E2BSandboxTransport.write_file
        active_read = E2BSandboxTransport.read_file
        version_list = E2BSandboxTransport.workspace_version_list_files
        version_read = E2BSandboxTransport.workspace_version_read_file
        with (
            patch.object(
                E2BSandboxTransport,
                "_initialize_trusted_runtime",
                backend_stages.wrap_trusted_runtime(trusted_runtime),
            ),
            patch.object(
                E2BSandboxTransport,
                "_probe_python",
                backend_stages.wrap_python_probe(python_probe),
            ),
            patch.object(
                E2BSandboxTransport,
                "_run_command",
                backend_stages.wrap_run_command(run_command),
            ),
            patch.object(
                E2BSandboxTransport,
                "_freeze_runtime",
                backend_stages.wrap_runtime_freeze(runtime_freeze),
            ),
            patch.object(
                E2BSandboxTransport,
                "_inspect_runtime",
                backend_stages.wrap_runtime_inspect(runtime_inspect),
            ),
            patch.object(
                E2BSandboxTransport,
                "list_files",
                backend_stages.wrap_initializer_transport(
                    "initializer_active_list", active_list
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "persistence_list_files",
                backend_stages.wrap_initializer_transport(
                    "initializer_checkpoint_list", checkpoint_list
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "persistence_read_file",
                backend_stages.wrap_initializer_transport(
                    "initializer_checkpoint_read", checkpoint_read
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "persistence_write_file",
                backend_stages.wrap_initializer_transport(
                    "initializer_checkpoint_write", checkpoint_write
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "write_file",
                backend_stages.wrap_initializer_transport(
                    "initializer_active_write", active_write
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "read_file",
                backend_stages.wrap_initializer_transport(
                    "initializer_active_read", active_read
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "workspace_version_list_files",
                backend_stages.wrap_initializer_transport(
                    "initializer_version_list", version_list
                ),
            ),
            patch.object(
                E2BSandboxTransport,
                "workspace_version_read_file",
                backend_stages.wrap_initializer_transport(
                    "initializer_version_read", version_read
                ),
            ),
        ):
            yield agent, runtime
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        cleanups: list[Callable[[], None]] = []
        if agent is not None:
            cleanups.append(agent.close)
        elif runtime is not None:
            cleanups.append(runtime.close)
        # The harness created the injected SessionEventWorkflow, so it closes
        # that caller-owned boundary explicitly even when Agent cleanup fails.
        if session is not None:
            cleanups.append(session.close)
        for cleanup in cleanups:
            try:
                cleanup()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        after_close_error: BaseException | None = None
        if after_close is not None:
            try:
                after_close()
            except BaseException as error:
                after_close_error = error
        earlier_error = primary_error if primary_error is not None else cleanup_error
        if after_close_error is not None:
            if earlier_error is None:
                raise after_close_error
            if isinstance(earlier_error, Exception):
                earlier_error.add_note(
                    "Live post-close verification also failed and was suppressed."
                )
        if cleanup_error is not None:
            if primary_error is None:
                raise cleanup_error
            if isinstance(primary_error, Exception):
                primary_error.add_note("Live Sandbox cleanup also failed and was suppressed.")


def _results(agent: CodingAgent) -> dict[str, ToolResult]:
    return {
        message.tool_call_id: message.result
        for message in agent.messages if isinstance(message, ToolMessage)
    }


def _task(agent: CodingAgent, call_id: str) -> AsyncTaskSnapshot:
    matches = [item for item in agent.list_async_tasks() if item.call_id == call_id]
    if len(matches) != 1:
        raise AssertionError(f"Expected one async task for {call_id}, found {len(matches)}.")
    return matches[0]


def _assert_completed_status(test: unittest.TestCase, agent: CodingAgent) -> AgentStatus:
    """Compare the public read-only view with real settled task state."""
    status = agent.get_status()
    test.assertEqual(status.run_state, "idle")
    test.assertIsNone(status.mode)
    test.assertIsNone(status.current_turn)
    test.assertIsNotNone(status.last_turn)
    assert status.last_turn is not None
    test.assertEqual(status.last_turn.kind, "completed")
    test.assertEqual(status.task_counts.active, 0)
    tasks = agent.list_async_tasks()
    test.assertEqual(status.task_counts.total, len(tasks))
    # A mismatch must not print provider/session/task coordinates.
    test.assertTrue(
        {item.task_id: item.state for item in status.tasks}
        == {item.task_id: item.status.value for item in tasks},
        "Public task states disagree with settled task-manager states.",
    )
    return status


def _bash_result(
    agent: CodingAgent, call_id: str, *, expect_zero: bool
) -> BashShellOutput:
    task = _task(agent, call_id)
    if (
        task.status.value != "completed"
        or task.result_status != "success"
        or task.result_json is None
    ):
        raise _async_harness_failure(
            call_id,
            status=task.status.value,
            result_status=task.result_status,
            error_class="async_task_snapshot_not_completed",
        )
    try:
        result = ToolResult.model_validate_json(task.result_json)
    except Exception:
        raise _async_harness_failure(
            call_id,
            status="completed",
            result_status=task.result_status,
            error_class="async_task_bash_result_payload_invalid",
        ) from None
    if (
        result.status != "success"
        or result.error is not None
        or not isinstance(result.output, BashShellOutput)
    ):
        raise _async_harness_failure(
            call_id,
            status="completed",
            result_status=result.status,
            error_class="async_task_bash_result_invalid",
        )
    output = result.output
    if (
        output.command != "unittest"
        or output.paths != ["tests"]
        or output.truncated
        or (expect_zero and output.exit_code != 0)
        or (not expect_zero and output.exit_code == 0)
    ):
        expectation = "zero" if expect_zero else "nonzero"
        raise _async_harness_failure(
            call_id,
            status=f"completed_unexpected_exit_{expectation}",
            result_status=result.status,
            error_class="async_task_bash_assertion_failed",
        )
    return output


def _timeout_bash_result(agent: CodingAgent, call_id: str) -> BashShellOutput:
    """Assert the known structured timeout contract, not a passing test run."""

    task = _task(agent, call_id)
    if (
        task.status.value != "completed"
        or task.result_status != "success"
        or task.result_json is None
    ):
        raise _async_harness_failure(
            call_id,
            status=task.status.value,
            result_status=task.result_status,
            error_class="remote_timeout_task_contract_invalid",
        )
    try:
        result = ToolResult.model_validate_json(task.result_json)
    except Exception:
        raise _async_harness_failure(
            call_id,
            status="completed",
            result_status=task.result_status,
            error_class="remote_timeout_result_payload_invalid",
        ) from None
    output = result.output
    if (
        result.status != "success"
        or result.error is not None
        or not isinstance(output, BashShellOutput)
        or output.command != "unittest"
        or output.paths != ["timeout_tests"]
        or output.exit_code != 1
        or output.stdout != ""
        or output.stderr != "命令执行超时。"
        or output.truncated
        or output.result != ""
    ):
        raise _async_harness_failure(
            call_id,
            status="completed_timeout_contract_invalid",
            result_status=result.status,
            error_class="remote_timeout_result_invalid",
        )
    return output


def _tree_fingerprint(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return (("missing",),)
    records: list[tuple[object, ...]] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current = Path(directory)
        for name in (*directory_names, *file_names):
            path = current / name
            stat_result = path.lstat()
            records.append((
                path.relative_to(root).as_posix(), stat_result.st_mode,
                stat_result.st_size, stat_result.st_mtime_ns,
            ))
            if len(records) > 2000:
                raise AssertionError("Host pending tree is unexpectedly large.")
    return tuple(records)


def _host_fingerprint() -> tuple[tuple[str, object], ...]:
    records: list[tuple[str, object]] = []
    for relative in (
        "AGENT_STATUS.md", "AGENTS.md", "app/agent.py", "tool/registry.py",
        "README.md", "live_probe.py", "tests/__init__.py",
        "tests/test_live_probe.py", "cancel_tests/__init__.py",
        "cancel_tests/test_slow_probe.py", "tests/test_quick_probe.py",
        "timeout_tests/__init__.py", "timeout_tests/test_slow_probe.py",
        "orphan_probe.py", "orphan_tests/__init__.py",
        "orphan_tests/test_orphan_probe.py",
    ):
        path = _ROOT / relative
        records.append((
            relative,
            None if not path.exists() else hashlib.sha256(path.read_bytes()).hexdigest(),
        ))
    records.append(("data/pending", _tree_fingerprint(_ROOT / "data/pending")))
    return tuple(records)


@unittest.skipUnless(
    os.environ.get(_LIVE_GATE) == "1",
    f"Set {_LIVE_GATE}=1 to run the real Aliyun CodingAgent E2E",
)
class AliyunSandboxLiveAgentE2ETests(unittest.TestCase):
    def _assert_success(self, result: ToolResult) -> None:
        self.assertEqual(result.status, "success")
        self.assertIsNone(result.error)

    def test_live_coding_loop_and_cross_sandbox_v2_restore(self) -> None:
        settings = load_sandbox_settings()
        # Independent gates: class-level live gate, base probe, recovery probe.
        settings.require_enabled()
        settings.require_recovery_enabled()
        snapshot = _synthetic_snapshot()
        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []
        approval_agent: CodingAgent | None = None
        approval_states: list[str] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            assert approval_agent is not None
            approval_states.append(approval_agent.get_status().run_state)
            approvals.append(request)
            return True

        checkpoint_path = posixpath.join(
            settings.oss_mount_dir, ".agnet-study/checkpoints/v2/objects", "0" * 64
        )
        sandbox_a_id: str | None = None
        sandbox_b_id: str | None = None
        session_id: str | None = None
        runtime_a: ExecutionToolRuntime | None = None
        runtime_b: ExecutionToolRuntime | None = None

        with tempfile.TemporaryDirectory(prefix="agnet-live-agent-e2e-") as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(side_effect=AssertionError("Remote tool reached local Registry."))
            local_shell = Mock(side_effect=AssertionError("Remote test reached host shell."))
            local_code = Mock(side_effect=AssertionError("Remote tool reached host code runner."))
            with (
                patch("tool.registry._execute", local_execute),
                patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                patch("tool.code_interpreter._run_child", local_code),
            ):
                script_a = _ScriptA(checkpoint_path)
                backend_stages_a = _BackendStageRecorder()
                with _managed_agent(
                    settings, session_directory, snapshot,
                    _Client(script_a), approve, backend_stages_a, recover=False,
                ) as (agent_a, runtime_a):
                    approval_agent = agent_a
                    initial_status = agent_a.get_status()
                    self.assertEqual(initial_status.run_state, "idle")
                    self.assertIsNone(initial_status.last_turn)
                    self.assertEqual(initial_status.task_counts.total, 0)
                    answer_a = agent_a.run("完成合成项目的远端修复闭环。")
                    readiness_a = _diagnose_backend_readiness(
                        runtime_a, backend_stages_a
                    )
                    script_a.raise_recorded_failure(readiness_a)
                    self.assertIn("已完成", answer_a)
                    session_id = runtime_a.session_id
                    sandbox_a_id = runtime_a.ensure_ready().sandbox_id
                    results_a = _results(agent_a)
                    self._assert_success(results_a["a_write"])
                    self.assertIsInstance(results_a["a_write"].output, WriteFileOutput)
                    self._assert_success(results_a["a_edit"])
                    self.assertIsInstance(results_a["a_edit"].output, EditorFileOutput)
                    _bash_result(agent_a, "a_test_fail", expect_zero=False)
                    _bash_result(agent_a, "a_test_pass", expect_zero=True)
                    _assert_completed_status(self, agent_a)
                    for call_id in (
                        "a_protected", "a_escape", "a_pending_write", "a_pending_list",
                    ):
                        self.assertEqual(results_a[call_id].status, "denied")
                        self.assertEqual(results_a[call_id].output, "")
                    # Absolute paths can be rejected by strict argument schema
                    # before the remote policy classifies them as denied.  Both
                    # statuses are fail-closed and neither carries output.
                    self.assertIn(results_a["a_checkpoint"].status, {"denied", "error"})
                    self.assertEqual(results_a["a_checkpoint"].output, "")
                    approvals_before_full_access = len(approvals)
                    self.assertEqual(
                        agent_a.set_permission_mode("full-access"), "full-access"
                    )
                    answer_full = agent_a.run(
                        "在同一 Sandbox A 中验证 full-access 持久化修改。"
                    )
                    script_a.raise_recorded_failure(readiness_a)
                    self.assertIn("full-access", answer_full)
                    self.assertEqual(len(approvals), approvals_before_full_access)
                    results_a = _results(agent_a)
                    self._assert_success(results_a["a_full_write"])
                    self.assertIsInstance(
                        results_a["a_full_write"].output, WriteFileOutput
                    )
                    self._assert_success(results_a["a_full_edit"])
                    self.assertIsInstance(
                        results_a["a_full_edit"].output, EditorFileOutput
                    )
                    _bash_result(agent_a, "a_full_test", expect_zero=True)
                    completed_a = _assert_completed_status(self, agent_a)
                assert runtime_a is not None
                self.assertTrue(runtime_a.closed)
                self.assertEqual(agent_a.get_status().run_state, "stopped")

                script_b = _ScriptB()
                backend_stages_b = _BackendStageRecorder()
                with _managed_agent(
                    settings, session_directory, snapshot,
                    _Client(script_b), approve, backend_stages_b, recover=True,
                ) as (agent_b, runtime_b):
                    approval_agent = agent_b
                    restored_status = agent_b.get_status()
                    self.assertEqual(restored_status.run_state, "idle")
                    self.assertTrue(
                        restored_status.last_turn == completed_a.last_turn,
                        "Restored status lost the last completed turn.",
                    )
                    self.assertEqual(restored_status.task_counts, completed_a.task_counts)
                    answer_b = agent_b.run("在 Sandbox B 中复验持久化修改。")
                    readiness_b = _diagnose_backend_readiness(
                        runtime_b, backend_stages_b
                    )
                    script_b.raise_recorded_failure(readiness_b)
                    self.assertIn("已恢复修改", answer_b)
                    if runtime_b.session_id != session_id:
                        raise _ProviderActiveListVerificationError(
                            "provider_active_list_session_mismatch"
                        )
                    sandbox_b_id = runtime_b.ensure_ready().sandbox_id
                    results_b = _results(agent_b)
                    self._assert_success(results_b["b_list"])
                    listing = results_b["b_list"].output
                    self.assertIsInstance(listing, ListFilesOutput)
                    assert isinstance(listing, ListFilesOutput)
                    self.assertEqual(
                        tuple(entry.path for entry in listing.entries),
                        (
                            "AGENTS.md", "README.md", "live_probe.py",
                            "tests/__init__.py", "tests/test_live_probe.py",
                        ),
                    )
                    self._assert_success(results_b["b_read"])
                    restored = results_b["b_read"].output
                    self.assertIsInstance(restored, ReadFileOutput)
                    assert isinstance(restored, ReadFileOutput)
                    self.assertIn(_MARKER, restored.content)
                    self.assertIn("return 42", restored.content)
                    self.assertNotIn("return 41", restored.content)
                    self._assert_success(results_b["b_read_full"])
                    restored_full = results_b["b_read_full"].output
                    self.assertIsInstance(restored_full, ReadFileOutput)
                    assert isinstance(restored_full, ReadFileOutput)
                    self.assertEqual(restored_full.path, _FULL_ACCESS_PATH)
                    self.assertEqual(restored_full.content, _FULL_ACCESS_FINAL)
                    _bash_result(agent_b, "b_test_pass", expect_zero=True)
                    _assert_completed_status(self, agent_b)
                assert runtime_b is not None
                self.assertTrue(runtime_b.closed)
                self.assertEqual(agent_b.get_status().run_state, "stopped")
                _verify_provider_session_has_no_active_sandboxes(
                    settings,
                    session_id or "",
                    sandbox_b_id or "",
                )
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertTrue(sandbox_a_id)
        self.assertTrue(sandbox_b_id)
        assert sandbox_a_id is not None and sandbox_b_id is not None
        self.assertNotEqual(
            hashlib.sha256(sandbox_a_id.encode()).digest(),
            hashlib.sha256(sandbox_b_id.encode()).digest(),
        )
        unittest_approvals = [
            request for request in approvals
            if request.tool_name == "bash_shell" and request.operation == "unittest"
        ]
        self.assertGreaterEqual(len(unittest_approvals), 3)
        self.assertEqual(approval_states, ["waiting_approval"] * len(approvals))


@unittest.skipUnless(
    os.environ.get(_LIVE_CANCEL_GATE) == "1",
    f"Set {_LIVE_CANCEL_GATE}=1 to run the real Aliyun cancellation E2E",
)
class AliyunSandboxLiveCancellationE2ETests(unittest.TestCase):
    def test_live_running_cancel_is_terminal_and_runtime_remains_usable(self) -> None:
        settings = load_sandbox_settings()
        # Three independent gates: dedicated test gate, base probe gate and
        # recovery probe gate.  The latter deliberately keeps every destructive
        # cloud lifecycle E2E behind the project's strongest existing opt-in.
        settings.require_enabled()
        settings.require_recovery_enabled()
        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []
        approval_agent: CodingAgent | None = None
        approval_states: list[str] = []
        bound_statuses: list[AgentStatus] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            assert approval_agent is not None
            approval_states.append(approval_agent.get_status().run_state)
            approvals.append(request)
            return True

        remote_handle_bound = Event()
        original_bind_handle = PreparedRemoteExecution._bind_handle

        def observed_bind_handle(
            prepared: PreparedRemoteExecution, handle: object
        ) -> None:
            original_bind_handle(prepared, handle)  # type: ignore[arg-type]
            # Observe the real running task before waking the scripted cancel.
            # No task locks, extra tools or synthetic lifecycle events are used.
            assert approval_agent is not None
            bound_statuses.append(approval_agent.get_status())
            remote_handle_bound.set()

        runtime: ExecutionToolRuntime | None = None
        session_id: str | None = None
        sandbox_id: str | None = None
        with tempfile.TemporaryDirectory(prefix="agnet-live-cancel-e2e-") as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote test reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote tool reached host code runner.")
            )
            with (
                patch("tool.registry._execute", local_execute),
                patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                patch("tool.code_interpreter._run_child", local_code),
                patch.object(
                    PreparedRemoteExecution,
                    "_bind_handle",
                    observed_bind_handle,
                ),
            ):
                script = _CancelScript(remote_handle_bound)
                backend_stages = _BackendStageRecorder()
                with _managed_agent(
                    settings,
                    session_directory,
                    _cancellation_snapshot(),
                    _Client(script),
                    approve,
                    backend_stages,
                    recover=False,
                ) as (agent, runtime):
                    approval_agent = agent
                    readiness = _diagnose_backend_readiness(
                        runtime, backend_stages
                    )
                    if readiness.backend_state != "ready":
                        raise _HarnessFailure(
                            call_id="cancel_long",
                            status="precondition_failed",
                            result_status=None,
                            error_class="backend_not_ready",
                        ).as_assertion(readiness)
                    session_id = runtime.session_id
                    sandbox_id = runtime.ensure_ready().sandbox_id
                    answer = agent.run(
                        "取消一个运行中的远程测试，然后在同一 Sandbox 中复验。"
                    )
                    script.raise_recorded_failure(readiness)
                    self.assertIn("取消已生效", answer)
                    self.assertTrue(remote_handle_bound.is_set())
                    self.assertTrue(bound_statuses)
                    running_status = bound_statuses[0]
                    self.assertEqual(running_status.run_state, "running")
                    self.assertIsNotNone(running_status.current_turn)
                    self.assertEqual(running_status.task_counts.running, 1)
                    self.assertEqual(running_status.task_counts.active, 1)

                    results = _results(agent)
                    self.assertEqual(results["cancel_long"].status, "accepted")
                    self.assertEqual(results["cancel_request"].status, "success")
                    cancel_output = results["cancel_request"].output
                    self.assertIsInstance(cancel_output, CancelAsyncTaskOutput)
                    assert isinstance(cancel_output, CancelAsyncTaskOutput)
                    self.assertEqual(cancel_output.state, "cancelled")
                    self.assertTrue(cancel_output.cancellation_requested)
                    self.assertEqual(results["cancel_verify"].status, "success")
                    verify_output = results["cancel_verify"].output
                    self.assertIsInstance(verify_output, AsyncTaskStatusOutput)
                    assert isinstance(verify_output, AsyncTaskStatusOutput)
                    self.assertEqual(verify_output.state, "cancelled")
                    self.assertIsNone(verify_output.result_status)
                    self.assertIsNone(verify_output.result_json)
                    _bash_result(agent, "cancel_short", expect_zero=True)

                    cancelled = _task(agent, "cancel_long")
                    deadline = time.monotonic() + 3
                    while (
                        cancelled.late_result_status is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                        cancelled = _task(agent, "cancel_long")
                    self.assertEqual(cancelled.status.value, "cancelled")
                    self.assertIsNone(cancelled.result_status)
                    self.assertIsNone(cancelled.result_json)
                    self.assertEqual(cancelled.late_result_status, "error")
                    self.assertIsNotNone(cancelled.late_result_json)
                    late = ToolResult.model_validate_json(
                        cancelled.late_result_json
                    )
                    self.assertEqual(late.status, "error")

                    task_events = [
                        event.type
                        for event in agent.session_events
                        if getattr(event.data, "task_id", None) == cancelled.task_id
                    ]
                    self.assertIn("async/task/started", task_events)
                    self.assertIn("async/task/cancel_requested", task_events)
                    self.assertIn("async/task/cancelled", task_events)
                    self.assertIn("async/task/late_result", task_events)
                    self.assertNotIn("async/task/completed", task_events)
                    settled_status = _assert_completed_status(self, agent)
                    self.assertEqual(settled_status.task_counts.cancelled, 1)
                    self.assertEqual(settled_status.task_counts.completed, 1)
                    self.assertTrue(
                        any(item.task_id == cancelled.task_id and item.state == "cancelled"
                            for item in settled_status.tasks),
                        "Late remote result changed the public cancelled state.",
                    )
                    self.assertFalse(runtime.closed)
                assert runtime is not None
                self.assertTrue(runtime.closed)
                self.assertEqual(agent.get_status().run_state, "stopped")
                self.assertEqual(agent.get_status().task_counts.cancelled, 1)
                _verify_provider_session_has_no_active_sandboxes(
                    settings,
                    session_id or "",
                    sandbox_id or "",
                )
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(
            [(request.tool_name, request.operation) for request in approvals],
            [("bash_shell", "unittest"), ("bash_shell", "unittest")],
        )
        self.assertEqual(approval_states, ["waiting_approval"] * len(approvals))


@unittest.skipUnless(
    os.environ.get(_LIVE_TIMEOUT_GATE) == "1",
    f"Set {_LIVE_TIMEOUT_GATE}=1 to run the real Aliyun timeout E2E",
)
class AliyunSandboxLiveTimeoutE2ETests(unittest.TestCase):
    def test_live_structured_timeout_is_sanitized_and_runtime_remains_usable(
        self,
    ) -> None:
        settings = load_sandbox_settings()
        # Three independent gates: this dedicated process gate, the base live
        # gate represented by require_enabled, and the destructive recovery
        # gate represented by require_recovery_enabled.
        settings.require_enabled()
        settings.require_recovery_enabled()
        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            approvals.append(request)
            return True

        runtime: ExecutionToolRuntime | None = None
        session_id: str | None = None
        sandbox_id_before: str | None = None
        with tempfile.TemporaryDirectory(prefix="agnet-live-timeout-e2e-") as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote test reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote tool reached host code runner.")
            )
            with (
                patch("tool.registry._execute", local_execute),
                patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                patch("tool.code_interpreter._run_child", local_code),
            ):
                script = _TimeoutScript()
                backend_stages = _BackendStageRecorder()
                with _managed_agent(
                    settings,
                    session_directory,
                    _timeout_snapshot(),
                    _Client(script),
                    approve,
                    backend_stages,
                    recover=False,
                ) as (agent, runtime):
                    readiness = _diagnose_backend_readiness(
                        runtime, backend_stages
                    )
                    if readiness.backend_state != "ready":
                        raise _HarnessFailure(
                            call_id="timeout_long",
                            status="precondition_failed",
                            result_status=None,
                            error_class="backend_not_ready",
                        ).as_assertion(readiness)
                    session_id = runtime.session_id
                    sandbox_id_before = runtime.ensure_ready().sandbox_id
                    answer = agent.run(
                        "验证远程结构化测试超时，然后在同一 Sandbox 中复验。"
                    )
                    script.raise_recorded_failure(readiness)
                    self.assertIn("超时结果已净化", answer)

                    results = _results(agent)
                    self.assertEqual(results["timeout_long"].status, "accepted")
                    self.assertEqual(results["timeout_short"].status, "accepted")
                    _timeout_bash_result(agent, "timeout_long")
                    _bash_result(agent, "timeout_short", expect_zero=True)

                    timeout_task = _task(agent, "timeout_long")
                    task_events = [
                        event.type
                        for event in agent.session_events
                        if getattr(event.data, "task_id", None)
                        == timeout_task.task_id
                    ]
                    self.assertIn("async/task/started", task_events)
                    self.assertIn("async/task/completed", task_events)
                    self.assertNotIn("async/task/failed", task_events)
                    self.assertEqual(timeout_task.status.value, "completed")
                    self.assertEqual(timeout_task.result_status, "success")
                    self.assertIsNone(timeout_task.error)
                    self.assertFalse(runtime.closed)
                    if runtime.ensure_ready().sandbox_id != sandbox_id_before:
                        raise _HarnessFailure(
                            call_id="timeout_short",
                            status="sandbox_identity_changed",
                            result_status="success",
                            error_class="runtime_reuse_invalid",
                        ).as_assertion(readiness)
                assert runtime is not None
                self.assertTrue(runtime.closed)
                _verify_provider_session_has_no_active_sandboxes(
                    settings,
                    session_id or "",
                    sandbox_id_before or "",
                )
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(
            [(request.tool_name, request.operation) for request in approvals],
            [("bash_shell", "unittest"), ("bash_shell", "unittest")],
        )


@unittest.skipUnless(
    os.environ.get(_LIVE_WAIT_UNCERTAINTY_GATE) == "1",
    f"Set {_LIVE_WAIT_UNCERTAINTY_GATE}=1 to run the real Aliyun wait uncertainty E2E",
)
class AliyunSandboxLiveWaitUncertaintyE2ETests(unittest.TestCase):
    def test_live_wait_uncertainty_fences_runtime_and_records_failure(self) -> None:
        settings = load_sandbox_settings()
        # Three independent gates: this process gate, base probe opt-in, and
        # the destructive lifecycle/recovery opt-in.
        settings.require_enabled()
        settings.require_recovery_enabled()

        # Private transport access is confined to this live fault-injection
        # test and occurs only after all three gates pass.
        from app.sandbox_transport import _E2BRunHandle

        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []
        injector = _WaitUncertaintyInjector()
        runtime_holder: list[ExecutionToolRuntime] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            approvals.append(request)
            return True

        def runtime_closed() -> bool:
            return len(runtime_holder) == 1 and runtime_holder[0].closed

        original_dispatch = PreparedRemoteExecution._mark_dispatch_attempted
        original_bind = PreparedRemoteExecution._bind_handle

        def observed_dispatch(prepared: PreparedRemoteExecution) -> None:
            original_dispatch(prepared)
            injector.record_dispatch()

        def observed_bind(
            prepared: PreparedRemoteExecution,
            handle: object,
        ) -> None:
            original_bind(prepared, handle)  # type: ignore[arg-type]
            injector.record_bound_handle()

        def injected_wait(handle: object) -> object:
            return injector.inject_wait_failure(handle)

        runtime: ExecutionToolRuntime | None = None
        session_id: str | None = None
        sandbox_id: str | None = None
        with tempfile.TemporaryDirectory(
            prefix="agnet-live-wait-uncertainty-e2e-"
        ) as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote test reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote tool reached host code runner.")
            )
            with (
                patch("tool.registry._execute", local_execute),
                patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                patch("tool.code_interpreter._run_child", local_code),
                patch.object(
                    PreparedRemoteExecution,
                    "_mark_dispatch_attempted",
                    observed_dispatch,
                ),
                patch.object(
                    PreparedRemoteExecution,
                    "_bind_handle",
                    observed_bind,
                ),
                patch.object(_E2BRunHandle, "wait", injected_wait),
            ):
                script = _WaitUncertaintyScript(runtime_closed)
                backend_stages = _BackendStageRecorder()
                with _managed_agent(
                    settings,
                    session_directory,
                    _timeout_snapshot(),
                    _Client(script),
                    approve,
                    backend_stages,
                    recover=False,
                ) as (agent, runtime):
                    runtime_holder.append(runtime)
                    readiness = _diagnose_backend_readiness(
                        runtime, backend_stages
                    )
                    if readiness.backend_state != "ready":
                        raise _HarnessFailure(
                            call_id="wait_uncertain",
                            status="precondition_failed",
                            result_status=None,
                            error_class="backend_not_ready",
                        ).as_assertion(readiness)
                    session_id = runtime.session_id
                    sandbox_id = runtime.ensure_ready().sandbox_id

                    answer = agent.run(
                        "注入一次远程 wait 结果不可确认故障并验证终态。"
                    )
                    script.raise_recorded_failure(readiness)
                    self.assertIn("runtime 已完成隔离", answer)
                    self.assertTrue(script.expected_failure_observed)
                    self.assertTrue(injector.dispatch_attempted.is_set())
                    self.assertTrue(injector.handle_bound.is_set())
                    self.assertEqual(injector.injection_count, 1)
                    self.assertTrue(runtime.closed)

                    task = _task(agent, "wait_uncertain")
                    self.assertEqual(task.status.value, "failed")
                    self.assertEqual(task.result_status, "error")
                    self.assertIsNotNone(task.result_json)
                    result = ToolResult.model_validate_json(task.result_json)
                    self.assertEqual(result.status, "error")
                    self.assertEqual(result.error, _REMOTE_EXECUTION_ERROR)
                    self.assertEqual(result.output, "")
                    task_events = [
                        event
                        for event in agent.session_events
                        if getattr(event.data, "task_id", None) == task.task_id
                    ]
                    self.assertEqual(
                        [event.type for event in task_events],
                        [
                            "async/task/accepted",
                            "async/task/started",
                            "async/task/progress",
                            "async/task/progress",
                            "async/task/failed",
                        ],
                    )
                    failed_event = task_events[-1]
                    self.assertEqual(
                        getattr(failed_event.data, "recovery_status", None),
                        "recorded",
                    )
                    rendered = repr((agent.messages, agent.session_events, task))
                    self.assertNotIn(
                        _WAIT_UNCERTAINTY_INJECTION_SECRET,
                        rendered,
                    )
                assert runtime is not None
                self.assertTrue(runtime.closed)
                _verify_provider_session_has_no_active_sandboxes(
                    settings,
                    session_id or "",
                    sandbox_id or "",
                )
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(
            [(request.tool_name, request.operation) for request in approvals],
            [("bash_shell", "unittest")],
        )


@unittest.skipUnless(
    os.environ.get(_LIVE_PREBIND_UNCERTAINTY_GATE) == "1",
    (
        f"Set {_LIVE_PREBIND_UNCERTAINTY_GATE}=1 to run the real Aliyun "
        "pre-bind uncertainty E2E"
    ),
)
class AliyunSandboxLivePrebindUncertaintyE2ETests(unittest.TestCase):
    def test_live_prebind_uncertainty_fences_runtime_and_records_failure(
        self,
    ) -> None:
        settings = load_sandbox_settings()
        settings.require_enabled()
        settings.require_recovery_enabled()

        # Private handle construction is patched only after all live gates pass.
        # The original initializer must complete before the fixed test failure.
        from app.sandbox_transport import _E2BRunHandle

        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []
        injector = _PrebindUncertaintyInjector()
        runtime_holder: list[ExecutionToolRuntime] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            approvals.append(request)
            return True

        def runtime_closed() -> bool:
            return len(runtime_holder) == 1 and runtime_holder[0].closed

        original_dispatch = PreparedRemoteExecution._mark_dispatch_attempted
        original_bind = PreparedRemoteExecution._bind_handle
        original_handle_init = _E2BRunHandle.__init__

        def observed_dispatch(prepared: PreparedRemoteExecution) -> None:
            original_dispatch(prepared)
            injector.record_dispatch()

        def observed_bind(
            prepared: PreparedRemoteExecution,
            handle: object,
        ) -> None:
            injector.record_bind_call()
            original_bind(prepared, handle)  # type: ignore[arg-type]

        runtime: ExecutionToolRuntime | None = None
        session_id: str | None = None
        sandbox_id: str | None = None
        with tempfile.TemporaryDirectory(
            prefix="agnet-live-prebind-uncertainty-e2e-"
        ) as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote test reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote tool reached host code runner.")
            )
            with (
                patch("tool.registry._execute", local_execute),
                patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                patch("tool.code_interpreter._run_child", local_code),
                patch.object(
                    PreparedRemoteExecution,
                    "_mark_dispatch_attempted",
                    observed_dispatch,
                ),
                patch.object(
                    PreparedRemoteExecution,
                    "_bind_handle",
                    observed_bind,
                ),
                patch.object(
                    _E2BRunHandle,
                    "__init__",
                    _prebind_injected_handle_initializer(
                        original_handle_init,
                        injector,
                    ),
                ),
            ):
                script = _PrebindUncertaintyScript(runtime_closed)
                backend_stages = _BackendStageRecorder()
                with _managed_agent(
                    settings,
                    session_directory,
                    _timeout_snapshot(),
                    _Client(script),
                    approve,
                    backend_stages,
                    recover=False,
                ) as (agent, runtime):
                    runtime_holder.append(runtime)
                    readiness = _diagnose_backend_readiness(
                        runtime, backend_stages
                    )
                    if readiness.backend_state != "ready":
                        raise _HarnessFailure(
                            call_id="prebind_uncertain",
                            status="precondition_failed",
                            result_status=None,
                            error_class="backend_not_ready",
                        ).as_assertion(readiness)
                    session_id = runtime.session_id
                    sandbox_id = runtime.ensure_ready().sandbox_id

                    answer = agent.run(
                        "注入一次远程句柄绑定前丢失故障并验证终态。"
                    )
                    script.raise_recorded_failure(readiness)
                    self.assertIn("runtime 已完成隔离", answer)
                    self.assertTrue(script.expected_failure_observed)
                    self.assertTrue(injector.dispatch_attempted.is_set())
                    self.assertTrue(injector.raw_handle_initialized.is_set())
                    self.assertFalse(injector.bind_called.is_set())
                    self.assertEqual(injector.injection_count, 1)
                    self.assertTrue(runtime.closed)

                    tasks = agent.list_async_tasks()
                    self.assertEqual(len(tasks), 1)
                    task = _task(agent, "prebind_uncertain")
                    self.assertEqual(task.status.value, "failed")
                    self.assertEqual(task.result_status, "error")
                    self.assertIsNotNone(task.result_json)
                    result = ToolResult.model_validate_json(task.result_json)
                    self.assertEqual(result.status, "error")
                    self.assertEqual(result.error, _REMOTE_EXECUTION_ERROR)
                    self.assertEqual(result.output, "")
                    task_events = [
                        event
                        for event in agent.session_events
                        if getattr(event.data, "task_id", None) == task.task_id
                    ]
                    event_types = [event.type for event in task_events]
                    self.assertEqual(event_types[:2], [
                        "async/task/accepted",
                        "async/task/started",
                    ])
                    self.assertEqual(event_types[-1], "async/task/failed")
                    self.assertNotIn("async/task/completed", event_types)
                    self.assertNotIn("async/task/cancelled", event_types)
                    self.assertEqual(
                        getattr(task_events[-1].data, "recovery_status", None),
                        "recorded",
                    )
                    rendered = repr((agent.messages, agent.session_events, task))
                    self.assertNotIn(
                        _PREBIND_UNCERTAINTY_INJECTION_SECRET,
                        rendered,
                    )
                assert runtime is not None
                self.assertTrue(runtime.closed)
                _verify_provider_session_has_no_active_sandboxes(
                    settings,
                    session_id or "",
                    sandbox_id or "",
                )
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(
            [(request.tool_name, request.operation) for request in approvals],
            [("bash_shell", "unittest")],
        )


@unittest.skipUnless(
    os.environ.get(_LIVE_KILL_ACK_UNCERTAINTY_GATE) == "1",
    (
        f"Set {_LIVE_KILL_ACK_UNCERTAINTY_GATE}=1 to run the real Aliyun "
        "kill acknowledgement uncertainty E2E"
    ),
)
class AliyunSandboxLiveKillAckUncertaintyE2ETests(unittest.TestCase):
    def test_live_successful_kill_ack_loss_is_sanitized_and_not_retried(
        self,
    ) -> None:
        settings = load_sandbox_settings()
        settings.require_enabled()
        settings.require_recovery_enabled()

        # Every private patch below is installed only after the independent
        # process/config/recovery gates pass.  The kill patch itself is bound
        # later to the one concrete Sandbox instance created by this test.
        from app.sandbox_transport import _E2BRunHandle

        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []
        injector = _KillAckUncertaintyInjector()
        runtime_holder: list[ExecutionToolRuntime] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            approvals.append(request)
            return True

        def runtime_closed() -> bool:
            if len(runtime_holder) != 1:
                return False
            current = runtime_holder[0]
            backend = current._backend
            return (
                current.closed
                and isinstance(backend, AliyunSandboxExecutionBackend)
                and backend.closed
            )

        original_dispatch = PreparedRemoteExecution._mark_dispatch_attempted
        original_bind = PreparedRemoteExecution._bind_handle
        original_run = PreparedRemoteExecution.run

        def observed_dispatch(prepared: PreparedRemoteExecution) -> None:
            original_dispatch(prepared)
            injector.record_dispatch()

        def observed_bind(
            prepared: PreparedRemoteExecution,
            handle: object,
        ) -> None:
            original_bind(prepared, handle)  # type: ignore[arg-type]
            injector.record_bound_handle()

        def injected_wait(handle: object) -> object:
            return injector.inject_wait_failure(handle)

        def observed_run(prepared: PreparedRemoteExecution) -> object:
            try:
                return original_run(prepared)
            except BaseException as error:
                injector.record_close_error(error)
                raise

        runtime: ExecutionToolRuntime | None = None
        backend: AliyunSandboxExecutionBackend | None = None
        session_id: str | None = None
        sandbox_id: str | None = None
        concrete_kill_patch: object | None = None
        concrete_kill_patch_started = False
        with tempfile.TemporaryDirectory(
            prefix="agnet-live-kill-ack-uncertainty-e2e-"
        ) as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote test reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote tool reached host code runner.")
            )
            try:
                with (
                    patch("tool.registry._execute", local_execute),
                    patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                    patch("tool.code_interpreter._run_child", local_code),
                    patch.object(
                        PreparedRemoteExecution,
                        "_mark_dispatch_attempted",
                        observed_dispatch,
                    ),
                    patch.object(
                        PreparedRemoteExecution,
                        "_bind_handle",
                        observed_bind,
                    ),
                    patch.object(
                        PreparedRemoteExecution,
                        "run",
                        observed_run,
                    ),
                    patch.object(_E2BRunHandle, "wait", injected_wait),
                ):
                    script = _KillAckUncertaintyScript(runtime_closed)
                    backend_stages = _BackendStageRecorder()
                    with _managed_agent(
                        settings,
                        session_directory,
                        _timeout_snapshot(),
                        _Client(script),
                        approve,
                        backend_stages,
                        recover=False,
                    ) as (agent, runtime):
                        runtime_holder.append(runtime)
                        readiness = _diagnose_backend_readiness(
                            runtime, backend_stages
                        )
                        if readiness.backend_state != "ready":
                            raise _HarnessFailure(
                                call_id="kill_ack_uncertain",
                                status="precondition_failed",
                                result_status=None,
                                error_class="backend_not_ready",
                            ).as_assertion(readiness)
                        session_id = runtime.session_id
                        sandbox_id = runtime.ensure_ready().sandbox_id

                        candidate_backend = runtime._backend
                        if not isinstance(
                            candidate_backend,
                            AliyunSandboxExecutionBackend,
                        ):
                            raise AssertionError("kill_ack_backend_type_invalid")
                        backend = candidate_backend
                        with backend._condition:
                            if backend._state != "ready":
                                raise AssertionError(
                                    "kill_ack_backend_not_ready_for_patch"
                                )
                            concrete_sandbox = backend._execution_sandbox
                        original_kill = getattr(
                            concrete_sandbox, "kill", None
                        )
                        if concrete_sandbox is None or not callable(original_kill):
                            raise AssertionError(
                                "kill_ack_concrete_sandbox_unavailable"
                            )
                        concrete_kill_patch = patch.object(
                            concrete_sandbox,
                            "kill",
                            injector.wrap_original_kill(original_kill),
                        )
                        concrete_kill_patch.start()  # type: ignore[attr-defined]
                        concrete_kill_patch_started = True

                        answer = agent.run(
                            "注入一次远程 kill 成功确认丢失故障并验证终态。"
                        )
                        script.raise_recorded_failure(readiness)
                        self.assertIn("runtime 已完成隔离", answer)
                        self.assertTrue(script.expected_failure_observed)
                        self.assertTrue(injector.dispatch_attempted.is_set())
                        self.assertTrue(injector.handle_bound.is_set())
                        self.assertEqual(injector.wait_injection_count, 1)
                        self.assertEqual(injector.original_kill_call_count, 1)
                        self.assertTrue(
                            injector.original_kill_succeeded.is_set()
                        )
                        self.assertEqual(
                            injector.post_success_injection_count, 1
                        )
                        self.assertTrue(injector.close_error_observed.is_set())
                        self.assertTrue(runtime_closed())

                        tasks = agent.list_async_tasks()
                        self.assertEqual(len(tasks), 1)
                        task = _task(agent, "kill_ack_uncertain")
                        self.assertEqual(task.status.value, "failed")
                        self.assertEqual(task.result_status, "error")
                        self.assertIsNotNone(task.result_json)
                        result = ToolResult.model_validate_json(task.result_json)
                        self.assertEqual(result.status, "error")
                        self.assertEqual(result.error, _REMOTE_EXECUTION_ERROR)
                        self.assertEqual(result.output, "")
                        task_events = [
                            event
                            for event in agent.session_events
                            if getattr(event.data, "task_id", None)
                            == task.task_id
                        ]
                        self.assertEqual(
                            [event.type for event in task_events],
                            [
                                "async/task/accepted",
                                "async/task/started",
                                "async/task/progress",
                                "async/task/progress",
                                "async/task/failed",
                            ],
                        )
                        self.assertEqual(
                            getattr(
                                task_events[-1].data,
                                "recovery_status",
                                None,
                            ),
                            "recorded",
                        )
                        rendered = repr(
                            (agent.messages, agent.session_events, task)
                        )
                        self.assertNotIn(
                            _KILL_ACK_WAIT_INJECTION_SECRET,
                            rendered,
                        )
                        self.assertNotIn(_KILL_ACK_INJECTION_SECRET, rendered)

                    assert runtime is not None
                    assert backend is not None
                    self.assertTrue(runtime_closed())
                    self.assertEqual(injector.original_kill_call_count, 1)
                    self.assertEqual(
                        injector.post_success_injection_count, 1
                    )
                    _verify_provider_session_has_no_active_sandboxes(
                        settings,
                        session_id or "",
                        sandbox_id or "",
                    )
            finally:
                if (
                    concrete_kill_patch is not None
                    and concrete_kill_patch_started
                ):
                    concrete_kill_patch.stop()  # type: ignore[attr-defined]

            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(
            [(request.tool_name, request.operation) for request in approvals],
            [("bash_shell", "unittest")],
        )


@unittest.skipUnless(
    os.environ.get(_LIVE_ORPHAN_CLEANUP_GATE) == "1",
    f"Set {_LIVE_ORPHAN_CLEANUP_GATE}=1 to run the real transaction cleanup E2E",
)
class AliyunSandboxLiveOrphanCleanupE2ETests(unittest.TestCase):
    def test_live_confirmed_manifest_failure_cleans_only_transaction_objects(
        self,
    ) -> None:
        settings = load_sandbox_settings()
        settings.require_enabled()
        settings.require_recovery_enabled()
        snapshot = _orphan_cleanup_snapshot()
        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []

        def approve(request: ToolConfirmationRequest) -> bool:
            approvals.append(request)
            return True

        sandbox_id: str | None = None
        session_id: str | None = None
        runtime: ExecutionToolRuntime | None = None

        def verify_after_managed_close() -> None:
            if (
                isinstance(session_id, str)
                and _SESSION_ID_RE.fullmatch(session_id) is not None
            ):
                _verify_provider_session_has_no_active_sandboxes(
                    settings,
                    session_id,
                    sandbox_id or "",
                )

        with tempfile.TemporaryDirectory(
            prefix="agnet-live-orphan-cleanup-e2e-"
        ) as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote test reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote code reached host runner.")
            )
            with (
                patch("tool.registry._execute", local_execute),
                patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                patch("tool.code_interpreter._run_child", local_code),
            ):
                script = _OrphanCleanupScript()
                backend_stages = _BackendStageRecorder()
                with _managed_agent(
                    settings,
                    session_directory,
                    snapshot,
                    _Client(script),
                    approve,
                    backend_stages,
                    recover=False,
                    after_close=verify_after_managed_close,
                ) as (agent, runtime):
                    readiness = _diagnose_backend_readiness(
                        runtime,
                        backend_stages,
                    )
                    if readiness.backend_state != "ready":
                        raise _HarnessFailure(
                            call_id="orphan_write",
                            status="precondition_failed",
                            result_status=None,
                            error_class="backend_not_ready",
                        ).as_assertion(readiness)
                    lease = runtime.ensure_ready()
                    candidate_session_id = runtime.session_id
                    if candidate_session_id is None:
                        raise AssertionError("orphan_cleanup_session_missing")
                    sandbox_id = lease.sandbox_id
                    session_id = candidate_session_id
                    session_root = version_session_root(
                        settings.oss_mount_dir,
                        session_id,
                    )
                    objects_root = session_root + "/objects/"
                    expected_objects: dict[str, tuple[int, str]] = {}
                    for entry in snapshot.entries:
                        body = (
                            _ORPHAN_MUTATED.encode("utf-8")
                            if entry.path == "orphan_probe.py"
                            else entry.data
                        )
                        digest = hashlib.sha256(body).hexdigest()
                        expected_objects[objects_root + digest] = (
                            len(body),
                            digest,
                        )
                    injector = _OrphanCleanupInjector(
                        session_root,
                        expected_objects,
                    )
                    original_version_write = (
                        E2BSandboxTransport.workspace_version_write_file
                    )
                    original_object_cleanup = (
                        E2BSandboxTransport.rollback_delete_workspace_version_object
                    )
                    with (
                        patch.object(
                            E2BSandboxTransport,
                            "workspace_version_write_file",
                            injector.wrap_version_write(original_version_write),
                        ),
                        patch.object(
                            E2BSandboxTransport,
                            "rollback_delete_workspace_version_object",
                            injector.wrap_object_cleanup(original_object_cleanup),
                        ),
                    ):
                        answer = agent.run(
                            "注入一次确定的 generation 发布失败并验证事务对象回收。"
                        )
                        script.raise_recorded_failure(readiness)
                        self.assertIn("事务 orphan 已清理", answer)
                        self.assertTrue(script.expected_failure_observed)
                        self.assertEqual(injector.injection_count, 1)
                        self.assertCountEqual(
                            injector.successful_cleanup_paths,
                            expected_objects,
                        )
                        self.assertEqual(
                            len(injector.successful_cleanup_paths),
                            len(expected_objects),
                        )
                        self.assertFalse(runtime.closed)
                        self.assertEqual(runtime.ensure_ready().sandbox_id, sandbox_id)
                        stage_history = tuple(backend_stages.history)
                        self.assertEqual(
                            backend_stages.stage,
                            "runtime_inspect",
                        )
                        self.assertLessEqual(
                            len(stage_history),
                            _BACKEND_STAGE_HISTORY_LIMIT,
                        )
                        self.assertIn("mutation_commit_v2", stage_history)
                        self.assertIn("runtime_inspect", stage_history)
                        self.assertLess(
                            max(
                                index
                                for index, stage in enumerate(stage_history)
                                if stage == "mutation_commit_v2"
                            ),
                            max(
                                index
                                for index, stage in enumerate(stage_history)
                                if stage == "runtime_inspect"
                            ),
                        )

                        results = _results(agent)
                        write_result = results["orphan_write"]
                        self.assertEqual(write_result.status, "error")
                        self.assertEqual(write_result.error, _REMOTE_EXECUTION_ERROR)
                        self.assertEqual(write_result.output, "")
                        read_result = results["orphan_read"]
                        self.assertEqual(read_result.status, "success")
                        self.assertIsInstance(read_result.output, ReadFileOutput)
                        assert isinstance(read_result.output, ReadFileOutput)
                        self.assertEqual(
                            read_result.output.content,
                            _ORPHAN_BASELINE,
                        )

                        write_events = [
                            event
                            for event in agent.session_events
                            if event.type == "tool/result"
                            and getattr(event.data, "call_id", None)
                            == "orphan_write"
                        ]
                        self.assertEqual(len(write_events), 1)
                        write_event = write_events[0].data
                        self.assertEqual(
                            getattr(write_event, "recovery_status", None),
                            "recorded",
                        )
                        self.assertEqual(
                            getattr(getattr(write_event, "result", None), "status", None),
                            "error",
                        )

                        code_task = _task(agent, "orphan_code")
                        self.assertEqual(code_task.status.value, "completed")
                        self.assertEqual(code_task.result_status, "success")
                        self.assertIsNotNone(code_task.result_json)
                        code_result = ToolResult.model_validate_json(
                            code_task.result_json
                        )
                        self.assertEqual(code_result.status, "success")
                        self.assertIsInstance(
                            code_result.output,
                            CodeInterpreterOutput,
                        )
                        assert isinstance(
                            code_result.output,
                            CodeInterpreterOutput,
                        )
                        self.assertEqual(code_result.output.exit_code, 0)
                        self.assertEqual(code_result.output.result, "42")
                        self.assertEqual(code_result.output.stderr, "")

                        candidate_backend = runtime._backend
                        if not isinstance(
                            candidate_backend,
                            AliyunSandboxExecutionBackend,
                        ):
                            raise AssertionError("orphan_cleanup_backend_type_invalid")
                        with candidate_backend._condition:
                            transport = candidate_backend._transport
                        if not isinstance(transport, SplitSandboxTransport):
                            raise AssertionError(
                                "orphan_cleanup_transport_type_invalid"
                            )
                        listing = transport.workspace_version_list_files(
                            session_root,
                            max_results=32,
                            max_depth=4,
                            timeout_seconds=30,
                        )
                        self.assertFalse(listing.truncated)
                        self.assertEqual(
                            [
                                entry.path
                                for entry in listing.entries
                                if not entry.is_directory
                            ],
                            [],
                        )
                        rendered = repr(
                            (agent.messages, agent.session_events, results)
                        )
                        self.assertNotIn(
                            _ORPHAN_CLEANUP_INJECTION_SECRET,
                            rendered,
                        )

                assert runtime is not None
                self.assertTrue(runtime.closed)
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(approvals, [])


@unittest.skipUnless(
    os.environ.get(_LIVE_MAINTENANCE_GATE) == "1",
    (
        f"Set {_LIVE_MAINTENANCE_GATE}=1 to run the real v3/cleaner/GC "
        "maintenance E2E"
    ),
)
class AliyunSandboxLiveMaintenanceE2ETests(unittest.TestCase):
    """One authorized A→B increment spanning all new maintenance paths."""

    def test_live_v3_direct_kill_fallback_restore_and_reference_gc(self) -> None:
        settings = load_sandbox_settings()
        settings.require_enabled()
        settings.require_recovery_enabled()
        snapshot = _maintenance_snapshot()
        host_before = _host_fingerprint()
        approvals: list[ToolConfirmationRequest] = []
        stage_observer = _FormatJsonStagingObserver(
            E2BSandboxTransport.format_json_candidates
        )
        direct_kill = _DirectKillFalseInjector()
        sealed_provider_hits: list[None] = []
        original_transport_read = E2BSandboxTransport.read_file

        def approve(request: ToolConfirmationRequest) -> bool:
            approvals.append(request)
            return True

        def observe_format_json(
            transport: E2BSandboxTransport,
            paths: Sequence[str],
            *,
            timeout_seconds: int,
        ) -> object:
            return stage_observer(
                transport,
                paths,
                timeout_seconds=timeout_seconds,
            )

        def observe_transport_read(
            transport: E2BSandboxTransport,
            path: str,
            *,
            offset: int,
            max_bytes: int,
            timeout_seconds: int,
        ) -> SandboxReadResult:
            if path == transport._workspace_root + "/.env":
                sealed_provider_hits.append(None)
                raise AssertionError("maintenance_sealed_path_reached_provider")
            return original_transport_read(
                transport,
                path,
                offset=offset,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )

        expected_bodies = {
            "README.md": (
                b"Synthetic live v3 restore and reference-scanning GC probe.\n"
            ),
            "config/a.json": _MAINTENANCE_JSON_A_AFTER,
            "config/b.json": _MAINTENANCE_JSON_B_AFTER,
        }
        expected_digests = {
            hashlib.sha256(body).hexdigest(): body
            for body in expected_bodies.values()
        }
        orphan_digest = hashlib.sha256(_MAINTENANCE_ORPHAN_BODY).hexdigest()

        session_id: str | None = None
        sandbox_a_ids: tuple[str, str] | None = None
        sandbox_b_ids: tuple[str, str] | None = None
        retained_objects: dict[str, bytes] = {}
        manifest_path: str | None = None
        manifest_raw: bytes | None = None
        inventory_before_orphan: tuple[str, ...] = ()
        active_zero_after_a: list[int] = []
        active_zero_after_b: list[int] = []

        with tempfile.TemporaryDirectory(
            prefix="agnet-live-maintenance-e2e-"
        ) as temporary:
            session_directory = Path(temporary) / "session"
            local_execute = Mock(
                side_effect=AssertionError("Remote tool reached local Registry.")
            )
            local_shell = Mock(
                side_effect=AssertionError("Remote command reached host shell.")
            )
            local_code = Mock(
                side_effect=AssertionError("Remote tool reached host code runner.")
            )

            def verify_a_closed() -> None:
                if session_id is None or sandbox_a_ids is None:
                    raise AssertionError("maintenance_a_close_binding_missing")
                active_zero_after_a.append(
                    _verify_maintenance_split_session_closed(
                        settings,
                        session_id,
                        sandbox_a_ids,
                    )
                )

            def verify_b_closed() -> None:
                if session_id is None or sandbox_b_ids is None:
                    raise AssertionError("maintenance_b_close_binding_missing")
                active_zero_after_b.append(
                    _verify_maintenance_split_session_closed(
                        settings,
                        session_id,
                        sandbox_b_ids,
                    )
                )

            concrete_kill_patch: object | None = None
            concrete_kill_patch_started = False
            try:
                with (
                    patch("tool.registry._execute", local_execute),
                    patch("tool.bash_shell._run_in_isolated_workspace", local_shell),
                    patch("tool.code_interpreter._run_child", local_code),
                    patch.object(
                        E2BSandboxTransport,
                        "format_json_candidates",
                        observe_format_json,
                    ),
                    patch.object(
                        E2BSandboxTransport,
                        "read_file",
                        observe_transport_read,
                    ),
                ):
                    script_a = _MaintenanceScriptA()
                    backend_stages_a = _BackendStageRecorder()
                    with _managed_agent(
                        settings,
                        session_directory,
                        snapshot,
                        _Client(script_a),
                        approve,
                        backend_stages_a,
                        recover=False,
                        after_close=verify_a_closed,
                    ) as (agent_a, runtime_a):
                        readiness_a = _diagnose_backend_readiness(
                            runtime_a,
                            backend_stages_a,
                        )
                        if readiness_a.backend_state != "ready":
                            raise _HarnessFailure(
                                call_id="maintenance_format",
                                status="precondition_failed",
                                result_status=None,
                                error_class="backend_not_ready",
                            ).as_assertion(readiness_a)
                        answer_a = agent_a.run(
                            "发布一个多文件 v3 generation，并验证安全门。"
                        )
                        script_a.raise_recorded_failure(readiness_a)
                        if "v3 generation" not in answer_a:
                            raise AssertionError("maintenance_a_answer_invalid")
                        results_a = _results(agent_a)
                        format_result = results_a.get("maintenance_format")
                        if (
                            format_result is None
                            or format_result.status != "success"
                            or not isinstance(
                                format_result.output,
                                BashShellOutput,
                            )
                            or format_result.output.command != "format_json"
                            or format_result.output.exit_code != 0
                            or format_result.output.paths
                            != ["config/b.json", "config/a.json"]
                        ):
                            raise AssertionError("maintenance_format_result_invalid")
                        sealed_result = results_a.get("maintenance_sealed")
                        if (
                            sealed_result is None
                            or sealed_result.status != "denied"
                            or sealed_result.output != ""
                        ):
                            raise AssertionError("maintenance_sealed_gate_invalid")

                        lease_a = runtime_a.ensure_ready()
                        session_id = runtime_a.session_id
                        if session_id is None:
                            raise AssertionError("maintenance_session_missing")
                        backend_a = runtime_a._backend
                        if not isinstance(
                            backend_a,
                            AliyunSandboxExecutionBackend,
                        ):
                            raise AssertionError("maintenance_backend_type_invalid")
                        binding_a = _require_maintenance_split_binding(
                            backend_a,
                            lease_a.sandbox_id or "",
                        )
                        transport_a = binding_a.transport
                        sandbox_a_ids = (
                            binding_a.execution_sandbox_id,
                            binding_a.persistence_sandbox_id,
                        )

                        session_root = version_session_root(
                            settings.oss_mount_dir,
                            session_id,
                        )
                        listing = transport_a.workspace_version_list_files(
                            session_root,
                            max_results=64,
                            max_depth=4,
                            timeout_seconds=30,
                        )
                        if listing.truncated:
                            raise AssertionError("maintenance_v3_inventory_truncated")
                        inventory_before_orphan = tuple(
                            item.path
                            for item in listing.entries
                            if not item.is_directory
                        )
                        manifest_paths = tuple(
                            path
                            for path in inventory_before_orphan
                            if path.startswith(session_root + "/generations/")
                        )
                        object_paths = tuple(
                            path
                            for path in inventory_before_orphan
                            if path.startswith(session_root + "/objects/")
                        )
                        if len(manifest_paths) != 1 or len(object_paths) != 3:
                            raise AssertionError("maintenance_v3_inventory_invalid")
                        manifest_path = manifest_paths[0]
                        observed_manifest = transport_a.workspace_version_read_file(
                            manifest_path,
                            offset=0,
                            max_bytes=4 * 1024 * 1024,
                            timeout_seconds=30,
                        )
                        manifest_raw = observed_manifest.data
                        manifest = parse_manifest(manifest_raw)
                        if (
                            not isinstance(manifest, WorkspaceCommandManifest)
                            or manifest.generation != 1
                            or manifest.tool_name != "bash_shell"
                            or manifest.operation != "format_json"
                            or len(manifest.changes) != 2
                            or {item.path for item in manifest.files}
                            != set(expected_bodies)
                        ):
                            raise AssertionError("maintenance_v3_manifest_invalid")
                        if {
                            item.sha256 for item in manifest.files
                        } != set(expected_digests):
                            raise AssertionError("maintenance_v3_references_invalid")
                        for path in object_paths:
                            digest = path.rsplit("/", 1)[-1]
                            expected = expected_digests.get(digest)
                            if expected is None:
                                raise AssertionError(
                                    "maintenance_unexpected_referenced_object"
                                )
                            observed = transport_a.workspace_version_probe_file(
                                path,
                                max_bytes=1024 * 1024,
                                timeout_seconds=30,
                            )
                            if (
                                type(observed) is not SandboxReadResult
                                or observed.data != expected
                                or observed.total_bytes != len(expected)
                            ):
                                raise AssertionError(
                                    "maintenance_referenced_object_invalid"
                                )
                            retained_objects[path] = expected

                        orphan_path = (
                            session_root + "/objects/" + orphan_digest
                        )
                        orphan_write = transport_a.workspace_version_write_file(
                            orphan_path,
                            _MAINTENANCE_ORPHAN_BODY,
                            overwrite=False,
                            timeout_seconds=30,
                        )
                        if (
                            type(orphan_write) is not SandboxWriteResult
                            or orphan_write.bytes_written
                            != len(_MAINTENANCE_ORPHAN_BODY)
                        ):
                            raise AssertionError("maintenance_orphan_write_invalid")
                        orphan_probe = transport_a.workspace_version_probe_file(
                            orphan_path,
                            max_bytes=len(_MAINTENANCE_ORPHAN_BODY),
                            timeout_seconds=30,
                        )
                        if (
                            type(orphan_probe) is not SandboxReadResult
                            or orphan_probe.data != _MAINTENANCE_ORPHAN_BODY
                        ):
                            raise AssertionError("maintenance_orphan_probe_invalid")

                        concrete_kill_patch = patch.object(
                            binding_a.execution_sandbox,
                            "kill",
                            direct_kill,
                        )
                        concrete_kill_patch.start()  # type: ignore[attr-defined]
                        concrete_kill_patch_started = True

                    if active_zero_after_a != [0]:
                        raise AssertionError("maintenance_a_active_zero_missing")
                    if direct_kill.call_count != 1:
                        raise AssertionError(
                            "maintenance_direct_kill_injection_missing"
                        )
                    concrete_kill_patch.stop()  # type: ignore[attr-defined]
                    concrete_kill_patch_started = False

                    # The host has already proved active=0 before enabling GC
                    # on the only Sandbox B initializer for this session.
                    script_b = _MaintenanceScriptB()
                    backend_stages_b = _BackendStageRecorder()
                    with _managed_agent(
                        settings,
                        session_directory,
                        snapshot,
                        _Client(script_b),
                        approve,
                        backend_stages_b,
                        recover=True,
                        collect_orphans=True,
                        after_close=verify_b_closed,
                    ) as (agent_b, runtime_b):
                        readiness_b = _diagnose_backend_readiness(
                            runtime_b,
                            backend_stages_b,
                        )
                        if readiness_b.backend_state != "ready":
                            raise _HarnessFailure(
                                call_id="maintenance_read_a",
                                status="precondition_failed",
                                result_status=None,
                                error_class="backend_not_ready",
                            ).as_assertion(readiness_b)
                        if runtime_b.session_id != session_id:
                            raise AssertionError("maintenance_session_mismatch")
                        lease_b = runtime_b.ensure_ready()
                        answer_b = agent_b.run(
                            "恢复 v3 generation，清理 crash orphan，并复验文件。"
                        )
                        script_b.raise_recorded_failure(readiness_b)
                        if "完成 GC 复验" not in answer_b:
                            raise AssertionError("maintenance_b_answer_invalid")
                        results_b = _results(agent_b)
                        for call_id, expected in (
                            ("maintenance_read_a", _MAINTENANCE_JSON_A_AFTER),
                            ("maintenance_read_b", _MAINTENANCE_JSON_B_AFTER),
                        ):
                            result = results_b.get(call_id)
                            if (
                                result is None
                                or result.status != "success"
                                or not isinstance(result.output, ReadFileOutput)
                                or result.output.content
                                != expected.decode("utf-8")
                            ):
                                raise AssertionError(
                                    "maintenance_restored_content_invalid"
                                )

                        backend_b = runtime_b._backend
                        if not isinstance(
                            backend_b,
                            AliyunSandboxExecutionBackend,
                        ):
                            raise AssertionError("maintenance_backend_type_invalid")
                        binding_b = _require_maintenance_split_binding(
                            backend_b,
                            lease_b.sandbox_id or "",
                        )
                        transport_b = binding_b.transport
                        sandbox_b_ids = (
                            binding_b.execution_sandbox_id,
                            binding_b.persistence_sandbox_id,
                        )
                        with backend_b._condition:
                            coordinator_b = backend_b._workspace_initializer
                        if not isinstance(
                            coordinator_b,
                            _DiagnosticWorkspaceCoordinator,
                        ):
                            raise AssertionError(
                                "maintenance_gc_coordinator_missing"
                            )
                        report = coordinator_b.last_orphan_gc_report
                        if (
                            type(report) is not WorkspaceOrphanGcReport
                            or report.generations != 1
                            or report.retained_object_count != 3
                            or report.orphan_object_count != 1
                            or report.deleted_object_count != 1
                            or report.orphan_bytes
                            != len(_MAINTENANCE_ORPHAN_BODY)
                            or report.deleted_bytes
                            != len(_MAINTENANCE_ORPHAN_BODY)
                            or report.baseline_digest != snapshot.digest
                        ):
                            raise AssertionError("maintenance_gc_report_invalid")
                        session_root_b = version_session_root(
                            settings.oss_mount_dir,
                            session_id or "",
                        )
                        orphan_after = transport_b.workspace_version_probe_file(
                            session_root_b + "/objects/" + orphan_digest,
                            max_bytes=len(_MAINTENANCE_ORPHAN_BODY),
                            timeout_seconds=30,
                        )
                        if orphan_after is not None:
                            raise AssertionError("maintenance_orphan_retained")
                        listing_after = transport_b.workspace_version_list_files(
                            session_root_b,
                            max_results=64,
                            max_depth=4,
                            timeout_seconds=30,
                        )
                        if listing_after.truncated:
                            raise AssertionError(
                                "maintenance_post_gc_inventory_truncated"
                            )
                        inventory_after = tuple(
                            item.path
                            for item in listing_after.entries
                            if not item.is_directory
                        )
                        if inventory_after != inventory_before_orphan:
                            raise AssertionError(
                                "maintenance_post_gc_inventory_mismatch"
                            )
                        if manifest_path is None or manifest_raw is None:
                            raise AssertionError("maintenance_manifest_binding_missing")
                        manifest_after = transport_b.workspace_version_probe_file(
                            manifest_path,
                            max_bytes=4 * 1024 * 1024,
                            timeout_seconds=30,
                        )
                        if (
                            type(manifest_after) is not SandboxReadResult
                            or manifest_after.data != manifest_raw
                        ):
                            raise AssertionError("maintenance_manifest_changed")
                        for path, expected in retained_objects.items():
                            observed = transport_b.workspace_version_probe_file(
                                path,
                                max_bytes=1024 * 1024,
                                timeout_seconds=30,
                            )
                            if (
                                type(observed) is not SandboxReadResult
                                or observed.data != expected
                            ):
                                raise AssertionError(
                                    "maintenance_referenced_object_changed"
                                )
            finally:
                if concrete_kill_patch_started and concrete_kill_patch is not None:
                    concrete_kill_patch.stop()  # type: ignore[attr-defined]

            if active_zero_after_b != [0]:
                raise AssertionError("maintenance_b_active_zero_missing")
            local_execute.assert_not_called()
            local_shell.assert_not_called()
            local_code.assert_not_called()

        self.assertEqual(_host_fingerprint(), host_before)
        self.assertEqual(stage_observer.call_count, 1)
        self.assertEqual(sealed_provider_hits, [])
        self.assertEqual(direct_kill.call_count, 1)
        self.assertIsNotNone(sandbox_a_ids)
        self.assertIsNotNone(sandbox_b_ids)
        assert sandbox_a_ids is not None and sandbox_b_ids is not None
        resource_digests = {
            hashlib.sha256(sandbox_id.encode()).digest()
            for sandbox_id in (*sandbox_a_ids, *sandbox_b_ids)
        }
        self.assertEqual(
            len(resource_digests),
            4,
        )
        self.assertEqual(len(approvals), 1)
        approval = approvals[0]
        self.assertEqual(approval.tool_name, "bash_shell")
        self.assertEqual(approval.operation, "format_json")
        self.assertEqual(approval.phase, "diff")
        self.assertEqual(
            approval.paths,
            ("config/b.json", "config/a.json"),
        )


class LiveAgentHarnessOfflineTests(unittest.TestCase):
    _TASK_A_FAIL = "1" * 32
    _TASK_A_PASS = "2" * 32
    _TASK_B_PASS = "3" * 32
    _TASK_A_FULL = "4" * 32
    _TASK_CANCEL_LONG = "5" * 32
    _TASK_CANCEL_SHORT = "6" * 32
    _TASK_TIMEOUT_LONG = "7" * 32
    _TASK_TIMEOUT_SHORT = "8" * 32
    _TASK_WAIT_UNCERTAIN = "9" * 32
    _TASK_PREBIND_UNCERTAIN = "a" * 32
    _TASK_KILL_ACK_UNCERTAIN = "b" * 32
    _TASK_ORPHAN_CODE = "c" * 32

    def _provider_list_settings(self) -> SandboxSettings:
        return SandboxSettings(
            enabled=True,
            api_key=SecretStr("fake-e2b-key"),
            api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
            domain="cn-hangzhou.e2b.fc.aliyuncs.com",
            template="code-interpreter-v1",
            timeout_seconds=300,
            oss_bucket="agent-probe-bucket",
            oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            oss_bucket_path="/projects/test/runs/provider-list",
            oss_mount_dir="/mnt/oss",
            oss_role_arn="acs:ram::1234567890123456:role/offline-role",
            oss_read_only=False,
        )

    def _fake_active_list_sdk(
        self,
        pages: Sequence[Sequence[object]],
        *,
        list_error: Exception | None = None,
    ) -> tuple[SimpleNamespace, dict[str, object]]:
        calls: dict[str, object] = {
            "query": None,
            "list": None,
            "next": [],
        }

        class FakeSandboxQuery:
            def __init__(
                query_self,
                *,
                metadata: dict[str, str],
                state: list[str],
            ) -> None:
                query_self.metadata = metadata
                query_self.state = state
                calls["query"] = query_self

        class FakeSandboxState:
            RUNNING = "running"
            PAUSED = "paused"

        class FakePaginator:
            def __init__(paginator_self) -> None:
                paginator_self.index = 0

            @property
            def has_next(paginator_self) -> bool:
                return paginator_self.index < len(pages)

            def next_items(
                paginator_self,
                **options: object,
            ) -> list[object]:
                next_calls = calls["next"]
                assert isinstance(next_calls, list)
                next_calls.append(options)
                page = list(pages[paginator_self.index])
                paginator_self.index += 1
                return page

        class FakeSandbox:
            @staticmethod
            def list(**options: object) -> FakePaginator:
                calls["list"] = options
                if list_error is not None:
                    raise list_error
                return FakePaginator()

        return (
            SimpleNamespace(
                Sandbox=FakeSandbox,
                SandboxQuery=FakeSandboxQuery,
                SandboxState=FakeSandboxState,
            ),
            calls,
        )

    def test_provider_active_list_filter_options_and_pagination_return_zero(
        self,
    ) -> None:
        sdk, calls = self._fake_active_list_sdk(((), ()))
        settings = self._provider_list_settings()
        session_id = "a" * 32

        with patch.dict(sys.modules, {"e2b": sdk}):
            active_count = _verify_provider_session_has_no_active_sandboxes(
                settings,
                session_id,
                "target-sandbox",
            )

        self.assertEqual(active_count, 0)
        query = calls["query"]
        self.assertIsNotNone(query)
        owner_key, owner_value = ALIYUN_SANDBOX_OWNER_METADATA
        self.assertEqual(
            query.metadata,
            {
                owner_key: owner_value,
                ALIYUN_SANDBOX_SESSION_METADATA_KEY: session_id,
            },
        )
        self.assertEqual(query.state, ["running", "paused"])
        list_options = calls["list"]
        self.assertIsInstance(list_options, dict)
        assert isinstance(list_options, dict)
        self.assertIs(list_options.pop("query"), query)
        self.assertEqual(
            list_options,
            {
                "limit": _PROVIDER_LIST_PAGE_LIMIT,
                "api_key": "fake-e2b-key",
                "api_url": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
                "domain": "cn-hangzhou.e2b.fc.aliyuncs.com",
                "request_timeout": _PROVIDER_LIST_REQUEST_TIMEOUT_SECONDS,
            },
        )
        self.assertEqual(
            calls["next"],
            [
                {"request_timeout": _PROVIDER_LIST_REQUEST_TIMEOUT_SECONDS},
                {"request_timeout": _PROVIDER_LIST_REQUEST_TIMEOUT_SECONDS},
            ],
        )

    def test_provider_active_list_rejects_active_target_without_ids(self) -> None:
        sdk, _calls = self._fake_active_list_sdk(
            ((SimpleNamespace(sandbox_id="provider-other-secret-id"),),)
        )

        with (
            patch.dict(sys.modules, {"e2b": sdk}),
            self.assertRaises(_ProviderActiveListVerificationError) as caught,
        ):
            _verify_provider_session_has_no_active_sandboxes(
                self._provider_list_settings(),
                "b" * 32,
                "target-secret-sandbox-id",
            )

        rendered = repr(caught.exception)
        self.assertEqual(str(caught.exception), "provider_active_sandbox_present")
        self.assertNotIn("provider-other", rendered)
        self.assertNotIn("target-secret", rendered)
        self.assertNotIn("fake-e2b-key", rendered)

    def test_provider_active_list_errors_are_fixed_and_sanitized(self) -> None:
        sdk, _calls = self._fake_active_list_sdk(
            (),
            list_error=RuntimeError(
                "provider-original fake-e2b-key target-secret-sandbox-id"
            ),
        )

        with (
            patch.dict(sys.modules, {"e2b": sdk}),
            self.assertRaises(_ProviderActiveListVerificationError) as caught,
        ):
            _verify_provider_session_has_no_active_sandboxes(
                self._provider_list_settings(),
                "c" * 32,
                "target-secret-sandbox-id",
            )

        self.assertEqual(
            str(caught.exception),
            "provider_active_list_request_failed",
        )
        rendered = repr(caught.exception)
        self.assertNotIn("provider-original", rendered)
        self.assertNotIn("target-secret", rendered)
        self.assertNotIn("fake-e2b-key", rendered)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_provider_active_list_enforces_page_bound(self) -> None:
        sdk, calls = self._fake_active_list_sdk(
            tuple(() for _ in range(_PROVIDER_LIST_MAX_PAGES + 1))
        )

        with (
            patch.dict(sys.modules, {"e2b": sdk}),
            self.assertRaises(_ProviderActiveListVerificationError) as caught,
        ):
            _verify_provider_session_has_no_active_sandboxes(
                self._provider_list_settings(),
                "d" * 32,
                "target-sandbox",
            )

        self.assertEqual(
            str(caught.exception),
            "provider_active_list_bounds_exceeded",
        )
        self.assertEqual(len(calls["next"]), _PROVIDER_LIST_MAX_PAGES)

    def test_provider_active_list_enforces_item_bound(self) -> None:
        sdk, _calls = self._fake_active_list_sdk(
            ((object(),) * (_PROVIDER_LIST_MAX_ITEMS + 1),)
        )

        with (
            patch.dict(sys.modules, {"e2b": sdk}),
            self.assertRaises(_ProviderActiveListVerificationError) as caught,
        ):
            _verify_provider_session_has_no_active_sandboxes(
                self._provider_list_settings(),
                "e" * 32,
                "target-sandbox",
            )

        self.assertEqual(
            str(caught.exception),
            "provider_active_list_bounds_exceeded",
        )

    def test_provider_active_list_disabled_settings_do_not_import_sdk(self) -> None:
        original_import = __import__
        imported_e2b = False

        def guarded_import(
            name: str,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal imported_e2b
            if name == "e2b" or name.startswith("e2b."):
                imported_e2b = True
                raise AssertionError("Disabled verifier imported E2B.")
            return original_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=guarded_import),
            self.assertRaises(_ProviderActiveListVerificationError) as caught,
        ):
            _verify_provider_session_has_no_active_sandboxes(
                SandboxSettings(),
                "f" * 32,
                "target-sandbox",
            )

        self.assertEqual(
            str(caught.exception),
            "provider_active_list_invalid_settings",
        )
        self.assertFalse(imported_e2b)

    def _call_id(self, response: list[SimpleNamespace]) -> str | None:
        calls = response[0].choices[0].delta.tool_calls
        return None if not calls else calls[0].id

    def _accepted(self, call_id: str, task_id: str) -> dict[str, object]:
        result = ToolResult(
            status="accepted",
            output=AsyncTaskAcceptedOutput(task_id=task_id, cancellable=True),
        )
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result.model_dump_json(),
        }

    def _success(self, call_id: str, output: object) -> dict[str, object]:
        result = ToolResult(status="success", output=output)
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": result.model_dump_json(),
        }

    def _write_output(self) -> WriteFileOutput:
        content = f"{_MARKER} = True\n\ndef answer():\n    return 41\n".encode()
        return WriteFileOutput(
            path="live_probe.py",
            encoding="utf-8",
            operation="created",
            bytes_written=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def _edit_output(self) -> EditorFileOutput:
        return EditorFileOutput(
            path="live_probe.py",
            encoding="utf-8",
            operation="replace_text",
            replacements=1,
            bytes_before=64,
            bytes_after=64,
            sha256_before="1" * 64,
            sha256_after="2" * 64,
        )

    def _full_write_output(self) -> WriteFileOutput:
        content = _FULL_ACCESS_INITIAL.encode("utf-8")
        return WriteFileOutput(
            path=_FULL_ACCESS_PATH,
            encoding="utf-8",
            operation="created",
            bytes_written=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def _full_edit_output(self) -> EditorFileOutput:
        before = _FULL_ACCESS_INITIAL.encode("utf-8")
        after = _FULL_ACCESS_FINAL.encode("utf-8")
        return EditorFileOutput(
            path=_FULL_ACCESS_PATH,
            encoding="utf-8",
            operation="replace_text",
            replacements=1,
            bytes_before=len(before),
            bytes_after=len(after),
            sha256_before=hashlib.sha256(before).hexdigest(),
            sha256_after=hashlib.sha256(after).hexdigest(),
        )

    def _list_output(self) -> ListFilesOutput:
        return ListFilesOutput(
            pattern="**/*", entries=[], count=0, truncated=False
        )

    def _read_output(self) -> ReadFileOutput:
        content = f"{_MARKER} = True\n\ndef answer():\n    return 42\n"
        size = len(content.encode())
        return ReadFileOutput(
            path="live_probe.py",
            encoding="utf-8",
            content=content,
            offset=0,
            bytes_read=size,
            total_bytes=size,
            truncated=False,
        )

    def _full_read_output(self) -> ReadFileOutput:
        size = len(_FULL_ACCESS_FINAL.encode("utf-8"))
        return ReadFileOutput(
            path=_FULL_ACCESS_PATH,
            encoding="utf-8",
            content=_FULL_ACCESS_FINAL,
            offset=0,
            bytes_read=size,
            total_bytes=size,
            truncated=False,
        )

    def _bash_output(self, exit_code: int) -> BashShellOutput:
        return BashShellOutput(
            command="unittest",
            paths=["tests"],
            exit_code=exit_code,
            stdout="offline synthetic output",
            stderr="",
            truncated=False,
        )

    def _timeout_output(self) -> BashShellOutput:
        return BashShellOutput(
            command="unittest",
            paths=["timeout_tests"],
            exit_code=1,
            stdout="",
            stderr="命令执行超时。",
            truncated=False,
            result="",
        )

    def _task_status_output(
        self, task_id: str, state: str
    ) -> AsyncTaskStatusOutput:
        return AsyncTaskStatusOutput(
            task_id=task_id,
            state=state,
            tool_name="bash_shell",
            cancellable=True,
        )

    def _terminal(
        self,
        task_id: str,
        kind: str,
        *,
        result: ToolResult | None = None,
        recovery_status: str = "recorded",
        secret: str = "provider-secret-must-not-escape",
    ) -> dict[str, object]:
        marker = f"[事件 #9 | async/task/{kind} | task={task_id}]"
        if kind == "completed":
            assert result is not None
            content = (
                f"{marker}\n后台工具任务已完成。\n"
                f"task_id={task_id}\nresult={result.model_dump_json()}"
            )
        elif kind == "failed":
            result_text = "" if result is None else f"\nresult={result.model_dump_json()}"
            content = (
                f"{marker}\n后台工具任务失败。\n"
                f"task_id={task_id}\nrecovery_status={recovery_status}\n"
                f"error={secret}{result_text}"
            )
        elif kind == "cancelled":
            content = (
                f"{marker}\n后台工具任务已取消。\n"
                f"task_id={task_id}\nreason={secret}"
            )
        else:
            raise AssertionError("Unsupported synthetic terminal kind.")
        return {"role": "user", "content": content}

    def test_wait_uncertainty_script_accepts_only_sanitized_failed_terminal(
        self,
    ) -> None:
        runtime_closed = False
        script = _WaitUncertaintyScript(lambda: runtime_closed)
        first = script([])
        self.assertEqual(self._call_id(first), "wait_uncertain")
        arguments = json.loads(
            first[0].choices[0].delta.tool_calls[0].function.arguments
        )
        self.assertEqual(
            arguments,
            {
                "command": "unittest",
                "paths": ["tests"],
                "timeout_seconds": 30,
            },
        )

        active = [self._accepted("wait_uncertain", self._TASK_WAIT_UNCERTAIN)]
        waiting = script(active)
        self.assertEqual(
            waiting[0].choices[0].delta.content,
            "等待远程 wait 终态确认。",
        )
        runtime_closed = True
        terminal = [
            *active,
            self._terminal(
                self._TASK_WAIT_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error=_REMOTE_EXECUTION_ERROR,
                ),
                recovery_status="recorded",
                secret=_REMOTE_EXECUTION_ERROR,
            ),
        ]
        final = script(terminal)

        self.assertEqual(
            final[0].choices[0].delta.content,
            "远程 wait 结果无法确认，runtime 已完成隔离。",
        )
        self.assertTrue(script.expected_failure_observed)
        self.assertEqual(script.stage, 2)
        self.assertIsNone(script.recorded_failure)
        self.assertIsNone(self._call_id(final))

    def test_wait_uncertainty_contract_matches_remote_async_runner(self) -> None:
        runtime = Mock(spec=ExecutionToolRuntime)
        runtime.route_for.return_value = "aliyun-sandbox"
        remote_execution = Mock()
        remote_execution.run.side_effect = ValueError(
            _WAIT_UNCERTAINTY_INJECTION_SECRET
        )
        remote_execution.cancel = Mock()

        def prepare_authorized(
            _tool_name: str,
            _arguments: object,
            authorize: Callable[[], bool],
        ) -> object:
            self.assertIs(authorize(), True)
            return remote_execution

        runtime.prepare_async_authorized.side_effect = prepare_authorized
        prepared = prepare_async_tool_call(
            ToolCall(
                id="wait_uncertain",
                name="bash_shell",
                arguments={
                    "command": "unittest",
                    "paths": ["tests"],
                    "timeout_seconds": 30,
                },
            ),
            confirmation_callback=lambda _request: True,
            execution_runtime=runtime,
        )

        outcome = prepared.runner(Mock())
        result = ToolResult.model_validate_json(outcome.result_json)

        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error, "远端工作区工具执行失败。")
        self.assertEqual(
            result.model_dump(mode="json"),
            {
                "status": "error",
                "output": "",
                "error": "远端工作区工具执行失败。",
            },
        )
        self.assertNotIn(
            _WAIT_UNCERTAINTY_INJECTION_SECRET,
            outcome.result_json,
        )
        runtime.execute.assert_not_called()

    def test_wait_uncertainty_script_rejects_other_terminal_safely(self) -> None:
        script = _WaitUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(script([])), "wait_uncertain")
        messages = [
            self._accepted("wait_uncertain", self._TASK_WAIT_UNCERTAIN),
            self._terminal(
                self._TASK_WAIT_UNCERTAIN,
                "completed",
                result=ToolResult(
                    status="success",
                    output=self._bash_output(exit_code=0),
                ),
            ),
        ]
        response = script(messages)
        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as caught:
            script.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("wait_uncertainty_terminal_invalid", diagnostic)
        self.assertNotIn("provider-secret-must-not-escape", diagnostic)

    def test_wait_uncertainty_script_rejects_near_match_error_contract(
        self,
    ) -> None:
        script = _WaitUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(script([])), "wait_uncertain")
        messages = [
            self._accepted("wait_uncertain", self._TASK_WAIT_UNCERTAIN),
            self._terminal(
                self._TASK_WAIT_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error="远程工作区工具执行失败。",
                ),
                recovery_status="recorded",
                secret="远程工作区工具执行失败。",
            ),
        ]
        response = script(messages)

        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as caught:
            script.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("wait_uncertainty_result_invalid", diagnostic)
        self.assertNotIn("远程工作区工具执行失败。", diagnostic)

    def test_wait_uncertainty_injection_requires_bound_handle_and_runs_once(
        self,
    ) -> None:
        injector = _WaitUncertaintyInjector()

        with self.assertRaises(AssertionError) as before_dispatch:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(before_dispatch.exception),
            "wait_uncertainty_injection_order_invalid",
        )
        injector.record_dispatch()
        with self.assertRaises(AssertionError) as before_bind:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(before_bind.exception),
            "wait_uncertainty_injection_order_invalid",
        )
        injector.record_bound_handle()
        with self.assertRaises(ValueError) as injected:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(injected.exception),
            _WAIT_UNCERTAINTY_INJECTION_SECRET,
        )
        self.assertEqual(injector.injection_count, 1)
        with self.assertRaises(AssertionError) as repeated:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(repeated.exception),
            "wait_uncertainty_injection_repeated",
        )

    def test_wait_uncertainty_diagnostic_does_not_leak_injection_secret(
        self,
    ) -> None:
        script = _WaitUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(script([])), "wait_uncertain")
        messages = [
            self._accepted("wait_uncertain", self._TASK_WAIT_UNCERTAIN),
            self._terminal(
                self._TASK_WAIT_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error=_REMOTE_EXECUTION_ERROR,
                ),
                recovery_status="recorded",
                secret=_WAIT_UNCERTAINTY_INJECTION_SECRET,
            ),
        ]
        response = script(messages)
        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as caught:
            script.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("wait_uncertainty_secret_leaked", diagnostic)
        self.assertNotIn(_WAIT_UNCERTAINTY_INJECTION_SECRET, diagnostic)

    def test_prebind_uncertainty_script_accepts_exact_sanitized_terminal(
        self,
    ) -> None:
        runtime_closed = False
        script = _PrebindUncertaintyScript(lambda: runtime_closed)
        first = script([])
        self.assertEqual(self._call_id(first), "prebind_uncertain")
        arguments = json.loads(
            first[0].choices[0].delta.tool_calls[0].function.arguments
        )
        self.assertEqual(arguments, {
            "command": "unittest",
            "paths": ["tests"],
            "timeout_seconds": 30,
        })

        active = [
            self._accepted(
                "prebind_uncertain", self._TASK_PREBIND_UNCERTAIN
            )
        ]
        waiting = script(active)
        self.assertEqual(
            waiting[0].choices[0].delta.content,
            "等待远程 pre-bind 终态确认。",
        )
        runtime_closed = True
        terminal = [
            *active,
            self._terminal(
                self._TASK_PREBIND_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error=_REMOTE_EXECUTION_ERROR,
                ),
                recovery_status="recorded",
                secret=_REMOTE_EXECUTION_ERROR,
            ),
        ]
        final = script(terminal)

        self.assertEqual(
            final[0].choices[0].delta.content,
            "远程句柄绑定前结果无法确认，runtime 已完成隔离。",
        )
        self.assertTrue(script.expected_failure_observed)
        self.assertEqual(script.stage, 2)
        self.assertIsNone(script.recorded_failure)

    def test_prebind_uncertainty_script_rejects_near_match_contract(
        self,
    ) -> None:
        script = _PrebindUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(script([])), "prebind_uncertain")
        messages = [
            self._accepted(
                "prebind_uncertain", self._TASK_PREBIND_UNCERTAIN
            ),
            self._terminal(
                self._TASK_PREBIND_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error="远程工作区工具执行失败。",
                ),
                recovery_status="recorded",
                secret="远程工作区工具执行失败。",
            ),
        ]
        response = script(messages)

        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as caught:
            script.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("prebind_uncertainty_result_invalid", diagnostic)
        self.assertNotIn("远程工作区工具执行失败。", diagnostic)

    def test_prebind_injection_requires_dispatch_runs_once_and_never_binds(
        self,
    ) -> None:
        injector = _PrebindUncertaintyInjector()
        with self.assertRaises(AssertionError) as before_dispatch:
            injector.inject_after_handle_init()
        self.assertEqual(
            str(before_dispatch.exception),
            "prebind_uncertainty_injection_order_invalid",
        )

        original_initialized = Event()

        def original_init(
            _handle: object,
            _raw_handle: object,
            *,
            decode: Callable[[object], object],
            cleanup: Callable[[], None],
        ) -> None:
            self.assertTrue(injector.dispatch_attempted.is_set())
            self.assertFalse(injector.raw_handle_initialized.is_set())
            self.assertTrue(callable(decode))
            self.assertTrue(callable(cleanup))
            original_initialized.set()

        injected_init = _prebind_injected_handle_initializer(
            original_init,
            injector,
        )
        injector.record_dispatch()
        with self.assertRaises(ValueError) as injected:
            injected_init(
                object(),
                object(),
                decode=lambda result: result,
                cleanup=lambda: None,
            )
        self.assertEqual(
            str(injected.exception),
            _PREBIND_UNCERTAINTY_INJECTION_SECRET,
        )
        self.assertTrue(injector.dispatch_attempted.is_set())
        self.assertTrue(original_initialized.is_set())
        self.assertTrue(injector.raw_handle_initialized.is_set())
        self.assertFalse(injector.bind_called.is_set())
        self.assertEqual(injector.injection_count, 1)
        with self.assertRaises(AssertionError) as repeated:
            injector.inject_after_handle_init()
        self.assertEqual(
            str(repeated.exception),
            "prebind_uncertainty_injection_repeated",
        )

        invalid = _PrebindUncertaintyInjector()
        invalid.record_dispatch()
        invalid.record_bind_call()
        with self.assertRaises(AssertionError) as after_bind:
            invalid.inject_after_handle_init()
        self.assertEqual(
            str(after_bind.exception),
            "prebind_uncertainty_injection_order_invalid",
        )
        self.assertEqual(invalid.injection_count, 0)

    def test_prebind_uncertainty_diagnostic_does_not_leak_secret(self) -> None:
        script = _PrebindUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(script([])), "prebind_uncertain")
        messages = [
            self._accepted(
                "prebind_uncertain", self._TASK_PREBIND_UNCERTAIN
            ),
            self._terminal(
                self._TASK_PREBIND_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error=_REMOTE_EXECUTION_ERROR,
                ),
                recovery_status="recorded",
                secret=_PREBIND_UNCERTAINTY_INJECTION_SECRET,
            ),
        ]
        response = script(messages)
        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as caught:
            script.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("prebind_uncertainty_secret_leaked", diagnostic)
        self.assertNotIn(_PREBIND_UNCERTAINTY_INJECTION_SECRET, diagnostic)

    def _prime_kill_ack_injector(
        self,
        injector: _KillAckUncertaintyInjector,
    ) -> None:
        injector.record_dispatch()
        injector.record_bound_handle()
        with self.assertRaises(ValueError) as wait_failure:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(wait_failure.exception),
            _KILL_ACK_WAIT_INJECTION_SECRET,
        )

    def test_kill_ack_script_accepts_exact_sanitized_terminal(self) -> None:
        runtime_closed = False
        script = _KillAckUncertaintyScript(lambda: runtime_closed)
        first = script([])
        self.assertEqual(self._call_id(first), "kill_ack_uncertain")
        arguments = json.loads(
            first[0].choices[0].delta.tool_calls[0].function.arguments
        )
        self.assertEqual(arguments, {
            "command": "unittest",
            "paths": ["tests"],
            "timeout_seconds": 30,
        })

        active = [
            self._accepted(
                "kill_ack_uncertain", self._TASK_KILL_ACK_UNCERTAIN
            )
        ]
        waiting = script(active)
        self.assertEqual(
            waiting[0].choices[0].delta.content,
            "等待远程 kill 成功确认故障终态。",
        )
        runtime_closed = True
        terminal = [
            *active,
            self._terminal(
                self._TASK_KILL_ACK_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error=_REMOTE_EXECUTION_ERROR,
                ),
                recovery_status="recorded",
                secret=_REMOTE_EXECUTION_ERROR,
            ),
        ]
        final = script(terminal)

        self.assertEqual(
            final[0].choices[0].delta.content,
            "远程 kill 已成功但确认丢失，runtime 已完成隔离。",
        )
        self.assertTrue(script.expected_failure_observed)
        self.assertEqual(script.stage, 2)
        self.assertIsNone(script.recorded_failure)

    def test_kill_ack_script_rejects_near_match_and_hides_both_secrets(
        self,
    ) -> None:
        near_match = _KillAckUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(near_match([])), "kill_ack_uncertain")
        response = near_match([
            self._accepted(
                "kill_ack_uncertain", self._TASK_KILL_ACK_UNCERTAIN
            ),
            self._terminal(
                self._TASK_KILL_ACK_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error="远程工作区工具执行失败。",
                ),
                recovery_status="recorded",
                secret="远程工作区工具执行失败。",
            ),
        ])
        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as near_failure:
            near_match.raise_recorded_failure()
        self.assertIn(
            "kill_ack_uncertainty_result_invalid",
            str(near_failure.exception),
        )

        leaked = _KillAckUncertaintyScript(lambda: True)
        self.assertEqual(self._call_id(leaked([])), "kill_ack_uncertain")
        leaked_response = leaked([
            self._accepted(
                "kill_ack_uncertain", self._TASK_KILL_ACK_UNCERTAIN
            ),
            self._terminal(
                self._TASK_KILL_ACK_UNCERTAIN,
                "failed",
                result=ToolResult(
                    status="error",
                    error=_REMOTE_EXECUTION_ERROR,
                ),
                recovery_status="recorded",
                secret=(
                    _KILL_ACK_WAIT_INJECTION_SECRET
                    + ":"
                    + _KILL_ACK_INJECTION_SECRET
                ),
            ),
        ])
        self.assertEqual(
            leaked_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as leaked_failure:
            leaked.raise_recorded_failure()
        diagnostic = str(leaked_failure.exception)
        self.assertIn("kill_ack_uncertainty_secret_leaked", diagnostic)
        self.assertNotIn(_KILL_ACK_WAIT_INJECTION_SECRET, diagnostic)
        self.assertNotIn(_KILL_ACK_INJECTION_SECRET, diagnostic)

    def test_kill_ack_injection_calls_real_kill_before_failure_once(self) -> None:
        injector = _KillAckUncertaintyInjector()
        with self.assertRaises(AssertionError) as before_dispatch:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(before_dispatch.exception),
            "kill_ack_wait_injection_order_invalid",
        )
        injector.record_dispatch()
        with self.assertRaises(AssertionError) as before_bind:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(before_bind.exception),
            "kill_ack_wait_injection_order_invalid",
        )
        injector.record_bound_handle()
        with self.assertRaises(ValueError) as wait_failure:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(wait_failure.exception),
            _KILL_ACK_WAIT_INJECTION_SECRET,
        )
        with self.assertRaises(AssertionError) as repeated_wait:
            injector.inject_wait_failure(object())
        self.assertEqual(
            str(repeated_wait.exception),
            "kill_ack_wait_injection_repeated",
        )

        order: list[str] = []

        def original_kill() -> bool:
            order.append("original_kill_success")
            return True

        sandbox = SimpleNamespace(
            sandbox_id="offline-kill-ack-sandbox",
            kill=original_kill,
        )
        backend = AliyunSandboxExecutionBackend(
            "c" * 32,
            self._provider_list_settings(),
            sandbox_factory=lambda _settings, _metadata: sandbox,
            persistence_sandbox_factory=lambda _settings, _metadata: SimpleNamespace(
                sandbox_id="offline-kill-ack-persistence",
                kill=lambda: True,
            ),
            transport_factory=lambda _sandbox: object(),
            persistence_transport_factory=lambda _sandbox: object(),
            recovery_provider=empty_recovery_provider(),
        )
        backend.ensure_ready()
        sandbox.kill = injector.wrap_original_kill(sandbox.kill)

        self.assertIsNone(backend.close())
        order.append("backend_close_returned")

        self.assertEqual(
            order,
            ["original_kill_success", "backend_close_returned"],
        )
        self.assertEqual(injector.wait_injection_count, 1)
        self.assertEqual(injector.original_kill_call_count, 1)
        self.assertTrue(injector.original_kill_succeeded.is_set())
        self.assertEqual(injector.post_success_injection_count, 1)
        self.assertFalse(injector.close_error_observed.is_set())
        self.assertTrue(backend.closed)

        # The managed cleanup path sees an already-closed backend and cannot
        # issue a second provider kill.
        backend.close()
        self.assertEqual(injector.original_kill_call_count, 1)

    def test_kill_ack_injection_does_not_relabel_false_or_exception(self) -> None:
        early_cleanup_injector = _KillAckUncertaintyInjector()
        early_cleanup_calls = 0

        def early_cleanup() -> bool:
            nonlocal early_cleanup_calls
            early_cleanup_calls += 1
            return True

        early_wrapper = early_cleanup_injector.wrap_original_kill(
            early_cleanup
        )
        self.assertIs(early_wrapper(), True)
        self.assertEqual(early_cleanup_calls, 1)
        self.assertEqual(early_cleanup_injector.wait_injection_count, 0)
        self.assertTrue(
            early_cleanup_injector.original_kill_succeeded.is_set()
        )
        self.assertEqual(
            early_cleanup_injector.post_success_injection_count,
            0,
        )

        false_injector = _KillAckUncertaintyInjector()
        self._prime_kill_ack_injector(false_injector)
        false_wrapper = false_injector.wrap_original_kill(lambda: False)
        self.assertIs(false_wrapper(), False)
        self.assertEqual(false_injector.original_kill_call_count, 1)
        self.assertFalse(false_injector.original_kill_succeeded.is_set())
        self.assertEqual(false_injector.post_success_injection_count, 0)

        exception_injector = _KillAckUncertaintyInjector()
        self._prime_kill_ack_injector(exception_injector)

        def failed_original() -> object:
            raise RuntimeError("original-provider-kill-failure")

        exception_wrapper = exception_injector.wrap_original_kill(
            failed_original
        )
        with self.assertRaises(RuntimeError) as original_failure:
            exception_wrapper()
        self.assertEqual(
            str(original_failure.exception),
            "original-provider-kill-failure",
        )
        self.assertEqual(exception_injector.original_kill_call_count, 1)
        self.assertFalse(exception_injector.original_kill_succeeded.is_set())
        self.assertEqual(exception_injector.post_success_injection_count, 0)

    def test_kill_ack_close_error_is_sanitized_by_remote_runner(self) -> None:
        runtime = Mock(spec=ExecutionToolRuntime)
        runtime.route_for.return_value = "aliyun-sandbox"
        remote_execution = Mock()
        close_error = ExecutionBackendCloseError(
            "Aliyun Sandbox kill() 失败；backend 已关闭。"
        )
        injected_error = ValueError(_KILL_ACK_INJECTION_SECRET)
        injected_error.__context__ = ValueError(
            _KILL_ACK_WAIT_INJECTION_SECRET
        )
        close_error.__cause__ = injected_error
        remote_execution.run.side_effect = close_error
        remote_execution.cancel = Mock()

        def prepare_authorized(
            _tool_name: str,
            _arguments: object,
            authorize: Callable[[], bool],
        ) -> object:
            self.assertIs(authorize(), True)
            return remote_execution

        runtime.prepare_async_authorized.side_effect = prepare_authorized
        prepared = prepare_async_tool_call(
            ToolCall(
                id="kill_ack_uncertain",
                name="bash_shell",
                arguments={
                    "command": "unittest",
                    "paths": ["tests"],
                    "timeout_seconds": 30,
                },
            ),
            confirmation_callback=lambda _request: True,
            execution_runtime=runtime,
        )

        outcome = prepared.runner(Mock())
        result = ToolResult.model_validate_json(outcome.result_json)
        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.error, _REMOTE_EXECUTION_ERROR)
        self.assertEqual(
            result.model_dump(mode="json"),
            {
                "status": "error",
                "output": "",
                "error": _REMOTE_EXECUTION_ERROR,
            },
        )
        self.assertNotIn(
            _KILL_ACK_WAIT_INJECTION_SECRET,
            outcome.result_json,
        )
        self.assertNotIn(_KILL_ACK_INJECTION_SECRET, outcome.result_json)
        runtime.execute.assert_not_called()

    def test_full_access_probe_path_crosses_only_the_approval_boundary(self) -> None:
        partial = route_mutation_approval(
            "partial-control", (_FULL_ACCESS_PATH,), project_root=_ROOT
        )
        full = route_mutation_approval(
            "full-access", (_FULL_ACCESS_PATH,), project_root=_ROOT
        )
        self.assertTrue(partial.requires_approval)
        self.assertEqual(partial.reason, "sensitive path")
        self.assertFalse(full.requires_approval)
        self.assertEqual(full.reason, "full-access")
        self.assertFalse(_path_is_hard_excluded(_FULL_ACCESS_PATH))

    def test_cancellation_script_requires_running_and_reuses_runtime(self) -> None:
        remote_handle_bound = Event()
        remote_handle_bound.set()
        script = _CancelScript(remote_handle_bound)
        self.assertEqual(self._call_id(script([])), "cancel_long")

        long_accepted = [
            self._accepted("cancel_long", self._TASK_CANCEL_LONG)
        ]
        self.assertEqual(
            self._call_id(script(long_accepted)), "cancel_poll_0"
        )
        accepted_query = [
            *long_accepted,
            self._success(
                "cancel_poll_0",
                self._task_status_output(
                    self._TASK_CANCEL_LONG, "accepted"
                ),
            ),
        ]
        self.assertEqual(
            self._call_id(script(accepted_query)), "cancel_poll_1"
        )
        running_query = [
            *accepted_query,
            self._success(
                "cancel_poll_1",
                self._task_status_output(
                    self._TASK_CANCEL_LONG, "running"
                ),
            ),
        ]
        self.assertEqual(
            self._call_id(script(running_query)), "cancel_request"
        )
        cancelled_result = [
            *running_query,
            self._success(
                "cancel_request",
                CancelAsyncTaskOutput(
                    task_id=self._TASK_CANCEL_LONG,
                    state="cancelled",
                    cancellation_requested=True,
                ),
            ),
        ]
        self.assertEqual(
            self._call_id(script(cancelled_result)), "cancel_verify"
        )
        verified_cancel = [
            *cancelled_result,
            self._success(
                "cancel_verify",
                self._task_status_output(
                    self._TASK_CANCEL_LONG, "cancelled"
                ),
            ),
        ]
        self.assertEqual(
            self._call_id(script(verified_cancel)), "cancel_short"
        )
        short_active = [
            *verified_cancel,
            self._accepted("cancel_short", self._TASK_CANCEL_SHORT),
        ]
        self.assertIsNone(self._call_id(script(short_active)))
        short_done = [
            *short_active,
            self._terminal(
                self._TASK_CANCEL_SHORT,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=0)
                ),
            ),
        ]
        final = script(short_done)
        self.assertIsNone(self._call_id(final))
        self.assertIn("取消已生效", final[0].choices[0].delta.content)
        self.assertIsNone(script.recorded_failure)

    def test_cancellation_snapshot_is_canonical_and_fully_validated(self) -> None:
        snapshot = _cancellation_snapshot()
        _validate_snapshot(snapshot)
        expected_paths = (
            "cancel_tests/__init__.py",
            "cancel_tests/test_slow_probe.py",
            "live_probe.py",
            "tests/__init__.py",
            "tests/test_live_probe.py",
        )
        self.assertEqual(
            tuple(entry.path for entry in snapshot.entries), expected_paths
        )
        self.assertEqual(expected_paths, tuple(sorted(expected_paths)))
        self.assertEqual(
            snapshot.total_bytes,
            sum(entry.size for entry in snapshot.entries),
        )
        self.assertRegex(snapshot.digest, r"^[0-9a-f]{64}$")
        for entry in snapshot.entries:
            self.assertEqual(entry.size, len(entry.data))
            self.assertEqual(
                entry.sha256, hashlib.sha256(entry.data).hexdigest()
            )

    def test_timeout_script_requires_exact_timeout_then_reuses_runtime(self) -> None:
        script = _TimeoutScript()
        self.assertEqual(self._call_id(script([])), "timeout_long")

        long_active = [
            self._accepted("timeout_long", self._TASK_TIMEOUT_LONG)
        ]
        waiting = script(long_active)
        self.assertIsNone(self._call_id(waiting))
        self.assertEqual(
            waiting[0].choices[0].delta.content,
            "等待远程超时结果。",
        )

        long_done = [
            *long_active,
            self._terminal(
                self._TASK_TIMEOUT_LONG,
                "completed",
                result=ToolResult(
                    status="success", output=self._timeout_output()
                ),
            ),
        ]
        self.assertEqual(self._call_id(script(long_done)), "timeout_short")

        short_active = [
            *long_done,
            self._accepted("timeout_short", self._TASK_TIMEOUT_SHORT),
        ]
        waiting = script(short_active)
        self.assertIsNone(self._call_id(waiting))
        self.assertEqual(
            waiting[0].choices[0].delta.content,
            "等待超时后的远程复验完成。",
        )
        short_done = [
            *short_active,
            self._terminal(
                self._TASK_TIMEOUT_SHORT,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=0)
                ),
            ),
        ]
        final = script(short_done)
        self.assertIsNone(self._call_id(final))
        self.assertIn("超时结果已净化", final[0].choices[0].delta.content)
        self.assertIsNone(script.recorded_failure)

    def test_timeout_script_rejects_wrong_or_adverse_terminal_safely(self) -> None:
        wrong_script = _TimeoutScript()
        self.assertEqual(self._call_id(wrong_script([])), "timeout_long")
        wrong_output = self._timeout_output().model_copy(
            update={"stderr": "provider-secret-must-not-escape"}
        )
        wrong_response = wrong_script([
            self._accepted("timeout_long", self._TASK_TIMEOUT_LONG),
            self._terminal(
                self._TASK_TIMEOUT_LONG,
                "completed",
                result=ToolResult(status="success", output=wrong_output),
            ),
        ])
        self.assertEqual(
            wrong_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as wrong_caught:
            wrong_script.raise_recorded_failure()
        wrong_diagnostic = str(wrong_caught.exception)
        self.assertIn("remote_timeout_result_invalid", wrong_diagnostic)
        self.assertNotIn("provider-secret-must-not-escape", wrong_diagnostic)

        failed_script = _TimeoutScript()
        self.assertEqual(self._call_id(failed_script([])), "timeout_long")
        failed_response = failed_script([
            self._accepted("timeout_long", self._TASK_TIMEOUT_LONG),
            self._terminal(
                self._TASK_TIMEOUT_LONG,
                "failed",
                result=ToolResult(
                    status="error",
                    error="provider-secret-must-not-escape",
                ),
            ),
        ])
        self.assertEqual(
            failed_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as failed_caught:
            failed_script.raise_recorded_failure()
        failed_diagnostic = str(failed_caught.exception)
        self.assertIn("async_task_failed", failed_diagnostic)
        self.assertNotIn("provider-secret-must-not-escape", failed_diagnostic)

    def test_timeout_snapshot_is_canonical_and_fully_validated(self) -> None:
        snapshot = _timeout_snapshot()
        _validate_snapshot(snapshot)
        expected_paths = (
            "tests/__init__.py",
            "tests/test_quick_probe.py",
            "timeout_tests/__init__.py",
            "timeout_tests/test_slow_probe.py",
        )
        self.assertEqual(
            tuple(entry.path for entry in snapshot.entries), expected_paths
        )
        self.assertEqual(expected_paths, tuple(sorted(expected_paths)))
        self.assertEqual(
            snapshot.total_bytes,
            sum(entry.size for entry in snapshot.entries),
        )
        self.assertRegex(snapshot.digest, r"^[0-9a-f]{64}$")
        for entry in snapshot.entries:
            self.assertEqual(entry.size, len(entry.data))
            self.assertEqual(
                entry.sha256, hashlib.sha256(entry.data).hexdigest()
            )

    def test_scripts_wait_only_for_accepted_tasks_and_advance_on_completed(self) -> None:
        script_a = _ScriptA("/mnt/oss/.agnet-study/checkpoints/v2/objects/" + "0" * 64)
        self.assertEqual(self._call_id(script_a([])), "a_write")
        a_write_done = [self._success("a_write", self._write_output())]
        self.assertEqual(self._call_id(script_a(a_write_done)), "a_test_fail")
        a_fail_active = [
            *a_write_done,
            self._accepted("a_test_fail", self._TASK_A_FAIL),
        ]
        self.assertIsNone(self._call_id(script_a(a_fail_active)))
        a_fail_done = [
            *a_fail_active,
            self._terminal(
                self._TASK_A_FAIL,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=1)
                ),
            ),
        ]
        self.assertEqual(self._call_id(script_a(a_fail_done)), "a_edit")
        a_edit_done = [
            *a_fail_done,
            self._success("a_edit", self._edit_output()),
        ]
        self.assertEqual(self._call_id(script_a(a_edit_done)), "a_test_pass")
        a_pass_active = [
            *a_edit_done,
            self._accepted("a_test_pass", self._TASK_A_PASS),
        ]
        self.assertIsNone(self._call_id(script_a(a_pass_active)))
        a_pass_done = [
            *a_pass_active,
            self._terminal(
                self._TASK_A_PASS,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=0)
                ),
            ),
        ]
        self.assertEqual(self._call_id(script_a(a_pass_done)), "a_protected")
        for expected_call_id in (
            "a_escape", "a_checkpoint", "a_pending_write", "a_pending_list",
        ):
            self.assertEqual(
                self._call_id(script_a(a_pass_done)), expected_call_id
            )
        partial_final = script_a(a_pass_done)
        self.assertIsNone(self._call_id(partial_final))
        self.assertIn("已完成", partial_final[0].choices[0].delta.content)

        self.assertEqual(self._call_id(script_a(a_pass_done)), "a_full_write")
        a_full_write_done = [
            *a_pass_done,
            self._success("a_full_write", self._full_write_output()),
        ]
        self.assertEqual(
            self._call_id(script_a(a_full_write_done)), "a_full_edit"
        )
        a_full_edit_done = [
            *a_full_write_done,
            self._success("a_full_edit", self._full_edit_output()),
        ]
        self.assertEqual(
            self._call_id(script_a(a_full_edit_done)), "a_full_test"
        )
        a_full_active = [
            *a_full_edit_done,
            self._accepted("a_full_test", self._TASK_A_FULL),
        ]
        self.assertIsNone(self._call_id(script_a(a_full_active)))
        a_full_done = [
            *a_full_active,
            self._terminal(
                self._TASK_A_FULL,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=0)
                ),
            ),
        ]
        full_final = script_a(a_full_done)
        self.assertIsNone(self._call_id(full_final))
        self.assertIn("full-access", full_final[0].choices[0].delta.content)

        script_b = _ScriptB()
        self.assertEqual(self._call_id(script_b(a_full_done)), "b_list")
        b_list_done = [
            *a_full_done,
            self._success("b_list", self._list_output()),
        ]
        self.assertEqual(self._call_id(script_b(b_list_done)), "b_read")
        b_read_done = [
            *b_list_done,
            self._success("b_read", self._read_output()),
        ]
        self.assertEqual(self._call_id(script_b(b_read_done)), "b_read_full")
        b_full_read_done = [
            *b_read_done,
            self._success("b_read_full", self._full_read_output()),
        ]
        self.assertEqual(
            self._call_id(script_b(b_full_read_done)), "b_test_pass"
        )
        b_active = [
            *b_full_read_done,
            self._accepted("b_test_pass", self._TASK_B_PASS),
        ]
        self.assertIsNone(self._call_id(script_b(b_active)))
        b_done = [
            *b_active,
            self._terminal(
                self._TASK_B_PASS,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=0)
                ),
            ),
        ]
        self.assertIsNone(self._call_id(script_b(b_done)))
        self.assertEqual(script_b.stage, 5)

    def test_failed_cancelled_unknown_and_not_accepted_are_diagnostic(self) -> None:
        cases = (
            (
                [
                    self._accepted("call_failed", self._TASK_A_FAIL),
                    self._terminal(
                        self._TASK_A_FAIL,
                        "failed",
                        result=ToolResult(status="error", error="synthetic failure"),
                    ),
                ],
                "call_failed",
                "status=failed",
                "result_status=error",
                "error_class=async_task_failed",
            ),
            (
                [
                    self._accepted("call_cancelled", self._TASK_A_PASS),
                    self._terminal(self._TASK_A_PASS, "cancelled"),
                ],
                "call_cancelled",
                "status=cancelled",
                "result_status=none",
                "error_class=async_task_cancelled",
            ),
            (
                [
                    self._accepted("call_unknown", self._TASK_B_PASS),
                    self._terminal(
                        self._TASK_B_PASS,
                        "failed",
                        recovery_status="outcome_unknown",
                    ),
                ],
                "call_unknown",
                "status=outcome_unknown",
                "result_status=none",
                "error_class=async_task_outcome_unknown",
            ),
            (
                [{
                    "role": "tool",
                    "tool_call_id": "call_denied",
                    "content": ToolResult(
                        status="denied", error="provider-secret-must-not-escape"
                    ).model_dump_json(),
                }],
                "call_denied",
                "status=not_accepted",
                "result_status=denied",
                "error_class=async_task_not_accepted",
            ),
        )
        for messages, call_id, status, result_status, error_class in cases:
            with self.subTest(call_id=call_id):
                with self.assertRaises(AssertionError) as caught:
                    _async_call_completed(messages, call_id)
                diagnostic = str(caught.exception)
                self.assertIn(f"call_id={call_id}", diagnostic)
                self.assertIn(status, diagnostic)
                self.assertIn(result_status, diagnostic)
                self.assertIn(error_class, diagnostic)
                self.assertNotIn("provider-secret-must-not-escape", diagnostic)

    def test_script_records_failure_then_test_layer_raises_after_agent_run(self) -> None:
        script_a = _ScriptA(
            "/mnt/oss/.agnet-study/checkpoints/v2/objects/" + "0" * 64
        )
        self.assertEqual(self._call_id(script_a([])), "a_write")
        self.assertEqual(
            self._call_id(script_a([
                self._success("a_write", self._write_output())
            ])),
            "a_test_fail",
        )
        failed_messages = [
            self._accepted("a_test_fail", self._TASK_A_FAIL),
            self._terminal(
                self._TASK_A_FAIL,
                "failed",
                result=ToolResult(
                    status="error", error="provider-secret-must-not-escape"
                ),
            ),
        ]
        response = script_a(failed_messages)
        self.assertEqual(response[0].choices[0].delta.content, _RECORDED_FAILURE_TEXT)
        self.assertIsNotNone(script_a.recorded_failure)
        with self.assertRaises(AssertionError) as caught:
            script_a.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("call_id=a_test_fail", diagnostic)
        self.assertIn("status=failed", diagnostic)
        self.assertIn("result_status=error", diagnostic)
        self.assertIn("error_class=async_task_failed", diagnostic)
        self.assertNotIn("provider-secret-must-not-escape", diagnostic)

        class _DeniedScript(_AsyncRecordingScript):
            def __init__(self) -> None:
                super().__init__()
                self.stage = 0

            def __call__(
                self, messages: Sequence[Mapping[str, object]]
            ) -> list[SimpleNamespace]:
                if self.stage == 0:
                    self.stage = 1
                    return _tool("denied_async", "bash_shell", {
                        "command": "unittest",
                        "paths": ["tests"],
                        "timeout_seconds": 30,
                    })
                completed, terminal_response = self._observe_async(
                    messages, "denied_async", "unexpected wait"
                )
                if completed:
                    return _text("unexpected completion")
                return terminal_response or _text(_RECORDED_FAILURE_TEXT)

        denied_script = _DeniedScript()
        workflow = SessionEventWorkflow.in_memory()
        agent = CodingAgent(
            _Client(denied_script),
            Settings(api_key="scripted-no-network", model="scripted-offline"),
            system_prompt="Follow the scripted offline test calls.",
            max_tool_rounds=2,
            permission_mode="partial-control",
            confirmation_callback=lambda _request: False,
            session_workflow=workflow,
            observability=Mock(spec=Observability),
        )
        try:
            answer = agent.run("Exercise a denied asynchronous preparation.")
            self.assertEqual(answer, _RECORDED_FAILURE_TEXT)
            with self.assertRaises(AssertionError) as denied_caught:
                denied_script.raise_recorded_failure()
            denied_diagnostic = str(denied_caught.exception)
            self.assertIn("call_id=denied_async", denied_diagnostic)
            self.assertIn("status=not_accepted", denied_diagnostic)
            self.assertIn("result_status=denied", denied_diagnostic)
            self.assertIn("error_class=async_task_not_accepted", denied_diagnostic)
        finally:
            agent.close()
            workflow.close()

    def test_scripts_stop_before_downstream_when_sync_precondition_fails(self) -> None:
        denied = ToolResult(
            status="denied", error="provider-secret-must-not-escape"
        ).model_dump_json()

        write_script = _ScriptA(
            "/mnt/oss/.agnet-study/checkpoints/v2/objects/" + "0" * 64
        )
        self.assertEqual(self._call_id(write_script([])), "a_write")
        write_response = write_script([{
            "role": "tool", "tool_call_id": "a_write", "content": denied,
        }])
        self.assertEqual(
            write_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as write_caught:
            write_script.raise_recorded_failure()
        write_diagnostic = str(write_caught.exception)
        self.assertIn("call_id=a_write", write_diagnostic)
        self.assertIn("status=precondition_failed", write_diagnostic)
        self.assertIn("result_status=denied", write_diagnostic)
        self.assertIn("error_class=tool_result_not_success", write_diagnostic)
        self.assertNotIn("provider-secret-must-not-escape", write_diagnostic)

        edit_script = _ScriptA(
            "/mnt/oss/.agnet-study/checkpoints/v2/objects/" + "0" * 64
        )
        self.assertEqual(self._call_id(edit_script([])), "a_write")
        a_write_done = [self._success("a_write", self._write_output())]
        self.assertEqual(self._call_id(edit_script(a_write_done)), "a_test_fail")
        a_test_done = [
            *a_write_done,
            self._accepted("a_test_fail", self._TASK_A_FAIL),
            self._terminal(
                self._TASK_A_FAIL,
                "completed",
                result=ToolResult(
                    status="success", output=self._bash_output(exit_code=1)
                ),
            ),
        ]
        self.assertEqual(self._call_id(edit_script(a_test_done)), "a_edit")
        edit_response = edit_script([
            *a_test_done,
            {"role": "tool", "tool_call_id": "a_edit", "content": denied},
        ])
        self.assertEqual(
            edit_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaisesRegex(AssertionError, "call_id=a_edit"):
            edit_script.raise_recorded_failure()

        list_script = _ScriptB()
        self.assertEqual(self._call_id(list_script([])), "b_list")
        list_response = list_script([{
            "role": "tool", "tool_call_id": "b_list", "content": denied,
        }])
        self.assertEqual(
            list_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaisesRegex(AssertionError, "call_id=b_list"):
            list_script.raise_recorded_failure()

        read_script = _ScriptB()
        self.assertEqual(self._call_id(read_script([])), "b_list")
        b_list_done = [self._success("b_list", self._list_output())]
        self.assertEqual(self._call_id(read_script(b_list_done)), "b_read")
        read_response = read_script([
            *b_list_done,
            {"role": "tool", "tool_call_id": "b_read", "content": denied},
        ])
        self.assertEqual(
            read_response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaisesRegex(AssertionError, "call_id=b_read"):
            read_script.raise_recorded_failure()

    def test_bash_result_strictly_distinguishes_nonzero_and_zero(self) -> None:
        def fake_agent(call_id: str, exit_code: int) -> Mock:
            result = ToolResult(
                status="success", output=self._bash_output(exit_code=exit_code)
            )
            task = SimpleNamespace(
                call_id=call_id,
                status=SimpleNamespace(value="completed"),
                result_status="success",
                result_json=result.model_dump_json(),
            )
            agent = Mock(spec=CodingAgent)
            agent.list_async_tasks.return_value = (task,)
            return agent

        self.assertEqual(
            _bash_result(fake_agent("expected_failure", 1), "expected_failure", expect_zero=False).exit_code,
            1,
        )
        self.assertEqual(
            _bash_result(fake_agent("expected_success", 0), "expected_success", expect_zero=True).exit_code,
            0,
        )
        with self.assertRaisesRegex(AssertionError, "async_task_bash_assertion_failed"):
            _bash_result(fake_agent("wrong_failure", 0), "wrong_failure", expect_zero=False)
        with self.assertRaisesRegex(AssertionError, "async_task_bash_assertion_failed"):
            _bash_result(fake_agent("wrong_success", 1), "wrong_success", expect_zero=True)

    def test_timeout_result_helper_is_exact_and_secret_free(self) -> None:
        def fake_agent(result_json: str) -> Mock:
            task = SimpleNamespace(
                call_id="timeout_long",
                status=SimpleNamespace(value="completed"),
                result_status="success",
                result_json=result_json,
            )
            agent = Mock(spec=CodingAgent)
            agent.list_async_tasks.return_value = (task,)
            return agent

        valid = ToolResult(
            status="success", output=self._timeout_output()
        ).model_dump_json()
        self.assertEqual(
            _timeout_bash_result(fake_agent(valid), "timeout_long").stderr,
            "命令执行超时。",
        )

        wrong = ToolResult(
            status="success",
            output=self._timeout_output().model_copy(
                update={"stdout": "provider-secret-must-not-escape"}
            ),
        ).model_dump_json()
        with self.assertRaises(AssertionError) as wrong_caught:
            _timeout_bash_result(fake_agent(wrong), "timeout_long")
        self.assertIn(
            "remote_timeout_result_invalid", str(wrong_caught.exception)
        )
        self.assertNotIn(
            "provider-secret-must-not-escape", str(wrong_caught.exception)
        )

        with self.assertRaises(AssertionError) as malformed_caught:
            _timeout_bash_result(
                fake_agent("provider-secret-must-not-escape"),
                "timeout_long",
            )
        self.assertIn(
            "remote_timeout_result_payload_invalid",
            str(malformed_caught.exception),
        )
        self.assertNotIn(
            "provider-secret-must-not-escape", str(malformed_caught.exception)
        )

    def test_backend_readiness_diagnostic_is_cached_and_secret_free(self) -> None:
        settings = SandboxSettings(
            enabled=True,
            api_key=SecretStr("offline-secret-key"),
            api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
            domain="cn-hangzhou.e2b.fc.aliyuncs.com",
            template="code-interpreter-v1",
            timeout_seconds=300,
            oss_bucket="agent-probe-bucket",
            oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            oss_bucket_path="/projects/test/runs/live-diagnostic",
            oss_mount_dir="/mnt/oss",
            oss_role_arn="acs:ram::1234567890123456:role/offline-role",
            oss_read_only=False,
        )
        creation_calls = 0

        class ProviderFailure(RuntimeError):
            pass

        def fail_creation(_settings: object, _metadata: object) -> object:
            nonlocal creation_calls
            creation_calls += 1
            raise ProviderFailure("provider-secret-must-not-escape")

        runtime = ExecutionToolRuntime(AliyunSandboxExecutionBackend(
            "d" * 32,
            settings,
            sandbox_factory=fail_creation,
            recovery_provider=empty_recovery_provider(),
        ))
        stages = _BackendStageRecorder()
        try:
            with self.assertRaises(ExecutionBackendCreateError):
                runtime.ensure_ready()
            diagnostic = _diagnose_backend_readiness(runtime, stages)
            self.assertEqual(creation_calls, 1)
            self.assertEqual(diagnostic.backend_state, "create_failed")
            self.assertEqual(diagnostic.backend_stage, "lease_create")
            self.assertEqual(
                diagnostic.exception_chain,
                ("ExecutionBackendCreateError",),
            )
            assertion = _HarnessFailure(
                call_id="a_write",
                status="precondition_failed",
                result_status="error",
                error_class="tool_result_not_success",
            ).as_assertion(diagnostic)
            rendered = str(assertion)
            self.assertIn("backend_state=create_failed", rendered)
            self.assertIn("backend_stage=lease_create", rendered)
            self.assertIn(
                "exception_chain=ExecutionBackendCreateError", rendered
            )
            self.assertNotIn("provider-secret-must-not-escape", rendered)
            self.assertNotIn("ProviderFailure", rendered)
        finally:
            runtime.close()

    def test_trusted_runtime_stage_wrappers_are_fixed_and_secret_free(self) -> None:
        stages = _BackendStageRecorder()

        def failed_probe(
            _transport: object,
            _executable: str,
            _timeout_seconds: int,
            *,
            user: str,
        ) -> object:
            del user
            raise ValueError("provider-secret-must-not-escape")

        wrapped_failed_probe = stages.wrap_python_probe(failed_probe)
        for candidate in ("python3", "python"):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                wrapped_failed_probe(object(), candidate, 30, user="root")
            self.assertEqual(stages.stage, "system_python_probe")

        safe_error = ExecutionBackendCreateError("fixed safe boundary")
        safe_error.__cause__ = ValueError("provider-secret-must-not-escape")
        diagnostic = _BackendReadinessDiagnostic(
            backend_state="create_failed",
            backend_stage=stages.stage,
            exception_chain=_project_exception_chain(safe_error),
        )
        rendered = str(_HarnessFailure(
            call_id="a_write",
            status="precondition_failed",
            result_status="error",
            error_class="tool_result_not_success",
        ).as_assertion(diagnostic))
        self.assertIn("backend_stage=system_python_probe", rendered)
        self.assertIn(
            "exception_chain=ExecutionBackendCreateError", rendered
        )
        self.assertNotIn("ValueError", rendered)
        self.assertNotIn("provider-secret-must-not-escape", rendered)
        self.assertNotIn("python3", rendered)

        def successful_probe(
            _transport: object,
            _executable: str,
            _timeout_seconds: int,
            *,
            user: str,
        ) -> object:
            del user
            return ("/fixed/python", (3, 11, 0))

        wrapped_probe = stages.wrap_python_probe(successful_probe)
        wrapped_probe(object(), "python3", 30, user="root")
        self.assertEqual(stages.stage, "runtime_path_check")
        wrapped_probe(
            object(), "/fixed/runtime/bin/python", 30, user="agentuser"
        )
        self.assertEqual(stages.stage, "venv_python_probe")

        def successful_command(
            _transport: object,
            _command: str,
            *,
            cwd: str,
            timeout_seconds: int,
            user: str,
        ) -> object:
            del cwd, timeout_seconds, user
            return object()

        wrapped_command = stages.wrap_run_command(successful_command)
        wrapped_command(
            object(),
            "python -I -m venv /fixed/runtime",
            cwd="/",
            timeout_seconds=30,
            user="root",
        )
        self.assertEqual(stages.stage, "venv_create")

        def successful_stage(
            _transport: object,
            _system_python: str,
            _timeout_seconds: int,
        ) -> object:
            return object()

        stages.wrap_runtime_freeze(successful_stage)(
            object(), "/fixed/python", 30
        )
        self.assertEqual(stages.stage, "runtime_freeze")
        stages.wrap_runtime_inspect(successful_stage)(
            object(), "/fixed/python", 30
        )
        self.assertEqual(stages.stage, "runtime_inspect")

        discovered = ("/fixed/python", (3, 11, 0))
        trusted_calls: list[tuple[int, object]] = []

        def successful_trusted_runtime(
            _transport: object,
            timeout_seconds: int,
            *,
            discovered: tuple[str, tuple[int, int, int]],
        ) -> object:
            trusted_calls.append((timeout_seconds, discovered))
            return object()

        stages.wrap_trusted_runtime(successful_trusted_runtime)(
            object(),
            30,
            discovered=discovered,
        )
        self.assertEqual(trusted_calls, [(30, discovered)])
        self.assertEqual(stages.stage, "workspace_initializer_pending")
        self.assertTrue(set(stages.history).issubset(_BACKEND_STAGES))
        self.assertNotIn("/fixed/python", repr(stages.history))

    def test_backend_stage_history_is_bounded_valid_and_keeps_latest(self) -> None:
        stages = _BackendStageRecorder()
        expected = ["lease_create"]
        repeated_stages = ("mutation_commit_v2", "runtime_inspect", "ready")
        for index in range(_BACKEND_STAGE_HISTORY_LIMIT + 7):
            stage = repeated_stages[index % len(repeated_stages)]
            stages.record(stage)
            expected.append(stage)

        self.assertEqual(
            stages.history,
            expected[-_BACKEND_STAGE_HISTORY_LIMIT:],
        )
        self.assertEqual(len(stages.history), _BACKEND_STAGE_HISTORY_LIMIT)
        self.assertEqual(stages.stage, expected[-1])
        self.assertEqual(stages.history[-1], stages.stage)
        self.assertTrue(set(stages.history).issubset(_BACKEND_STAGES))

        prior_stage = stages.stage
        prior_history = tuple(stages.history)
        with self.assertRaisesRegex(
            ValueError,
            "backend stage classification is invalid",
        ):
            stages.record("provider-secret-path-must-not-escape")
        self.assertEqual(stages.stage, prior_stage)
        self.assertEqual(tuple(stages.history), prior_history)
        self.assertNotIn("provider-secret", repr(stages.history))

    def test_initializer_transport_stages_are_fixed_scoped_and_secret_free(self) -> None:
        stages = _BackendStageRecorder()
        calls = 0

        def successful_transport(
            _transport: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            nonlocal calls
            del args, kwargs
            calls += 1
            return "sentinel"

        ordered_stages = (
            "initializer_active_list",
            "initializer_checkpoint_list",
            "initializer_checkpoint_read",
            "initializer_checkpoint_write",
            "initializer_active_write",
            "initializer_active_read",
            "initializer_version_list",
            "initializer_version_read",
        )
        stages.record("workspace_initializer")
        for expected in ordered_stages:
            wrapped = stages.wrap_initializer_transport(
                expected, successful_transport
            )
            self.assertEqual(
                wrapped(
                    object(),
                    "provider-secret-path-must-not-escape",
                    provider_token="provider-secret-value-must-not-escape",
                ),
                "sentinel",
            )
            self.assertEqual(stages.stage, expected)
        self.assertEqual(calls, len(ordered_stages))
        self.assertTrue(set(stages.history).issubset(_BACKEND_STAGES))
        self.assertNotIn("provider-secret", repr(stages.history))

        stages.record("ready")
        stages.wrap_initializer_transport(
            "initializer_active_read", successful_transport
        )(object(), "post-ready-secret-must-not-escape")
        self.assertEqual(stages.stage, "ready")
        self.assertNotIn("post-ready-secret", repr(stages.history))

        def failed_transport(
            _transport: object,
            *args: object,
            **kwargs: object,
        ) -> object:
            del args, kwargs
            raise ValueError("provider-secret-must-not-escape")

        stages.record("workspace_initializer")
        wrapped_failure = stages.wrap_initializer_transport(
            "initializer_checkpoint_list", failed_transport
        )
        with self.assertRaises(ValueError):
            wrapped_failure(object(), "provider-secret-path-must-not-escape")
        self.assertEqual(stages.stage, "initializer_checkpoint_list")
        diagnostic = _BackendReadinessDiagnostic(
            backend_state="create_failed",
            backend_stage=stages.stage,
            exception_chain=("ExecutionBackendCreateError",),
        )
        rendered = diagnostic.as_suffix()
        self.assertNotIn("provider-secret", rendered)
        self.assertNotIn("ValueError", rendered)

        with self.assertRaises(ValueError):
            stages.wrap_initializer_transport(
                "ready", successful_transport
            )

    def test_orphan_cleanup_injector_is_prewrite_once_and_exactly_scoped(
        self,
    ) -> None:
        session_root = version_session_root("/mnt/oss", "d" * 32)
        digest = "e" * 64
        object_path = session_root + "/objects/" + digest
        target = session_root + "/generations/" + "0" * 16 + "-" + digest + ".json"
        injector = _OrphanCleanupInjector(
            session_root,
            {object_path: (4, digest)},
        )
        forwarded: list[str] = []

        def original(
            _transport: object,
            path: str,
            _data: bytes,
            *,
            overwrite: bool,
            timeout_seconds: int,
        ) -> str:
            self.assertFalse(overwrite)
            self.assertGreater(timeout_seconds, 0)
            forwarded.append(path)
            return "provider-write-result"

        wrapped = injector.wrap_version_write(original)
        with self.assertRaises(ValueError) as injected:
            wrapped(
                object(),
                target,
                b"manifest",
                overwrite=False,
                timeout_seconds=30,
            )
        self.assertEqual(
            str(injected.exception),
            _ORPHAN_CLEANUP_INJECTION_SECRET,
        )
        self.assertEqual(injector.injection_count, 1)
        self.assertEqual(forwarded, [])
        with self.assertRaisesRegex(
            AssertionError,
            "orphan_cleanup_manifest_injection_repeated",
        ):
            wrapped(
                object(),
                target,
                b"manifest",
                overwrite=False,
                timeout_seconds=30,
            )
        self.assertEqual(forwarded, [])

        near_paths = (
            object_path,
            session_root + "/generations/" + "0" * 15 + "-" + digest + ".json",
            session_root + "/generations/" + "0" * 16 + "-" + digest.upper() + ".json",
            target + ".bak",
            version_session_root("/mnt/oss", "f" * 32)
            + "/generations/"
            + "0" * 16
            + "-"
            + digest
            + ".json",
        )
        for path in near_paths:
            with self.subTest(path=path):
                self.assertEqual(
                    wrapped(
                        object(),
                        path,
                        b"payload",
                        overwrite=False,
                        timeout_seconds=30,
                    ),
                    "provider-write-result",
                )
        self.assertEqual(forwarded, list(near_paths))
        self.assertEqual(injector.injection_count, 1)

    def test_orphan_cleanup_injector_records_only_successful_real_delete(
        self,
    ) -> None:
        session_root = version_session_root("/mnt/oss", "1" * 32)
        digest = "2" * 64
        path = session_root + "/objects/" + digest
        injector = _OrphanCleanupInjector(
            session_root,
            {path: (7, digest)},
        )
        original_calls = 0

        def failed_original(*_args: object, **_kwargs: object) -> object:
            nonlocal original_calls
            original_calls += 1
            raise ValueError("provider-delete-secret")

        with self.assertRaisesRegex(ValueError, "provider-delete-secret"):
            injector.wrap_object_cleanup(failed_original)(
                object(),
                path,
                expected_size=7,
                expected_sha256=digest,
                timeout_seconds=30,
            )
        self.assertEqual(original_calls, 1)
        self.assertEqual(injector.successful_cleanup_paths, [])

        sentinel = object()

        def successful_original(*_args: object, **_kwargs: object) -> object:
            nonlocal original_calls
            original_calls += 1
            return sentinel

        result = injector.wrap_object_cleanup(successful_original)(
            object(),
            path,
            expected_size=7,
            expected_sha256=digest,
            timeout_seconds=30,
        )
        self.assertIs(result, sentinel)
        self.assertEqual(original_calls, 2)
        self.assertEqual(injector.successful_cleanup_paths, [path])

    def test_orphan_cleanup_rejects_invalid_delete_binding_before_original(
        self,
    ) -> None:
        session_root = version_session_root("/mnt/oss", "3" * 32)
        digest = "4" * 64
        path = session_root + "/objects/" + digest
        original = Mock(
            side_effect=AssertionError(
                "invalid cleanup binding reached original delete"
            )
        )
        injector = _OrphanCleanupInjector(
            session_root,
            {path: (9, digest)},
        )
        wrapped = injector.wrap_object_cleanup(original)
        invalid = (
            (path + ".bak", 9, digest),
            (path, 8, digest),
            (path, 9, "5" * 64),
        )
        for candidate_path, expected_size, expected_sha256 in invalid:
            with (
                self.subTest(
                    path=candidate_path,
                    size=expected_size,
                    digest=expected_sha256,
                ),
                self.assertRaisesRegex(
                    AssertionError,
                    "orphan_cleanup_delete_binding_invalid",
                ),
            ):
                wrapped(
                    object(),
                    candidate_path,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    timeout_seconds=30,
                )
        original.assert_not_called()
        self.assertEqual(injector.successful_cleanup_paths, [])

    def test_managed_agent_runs_post_close_check_without_masking_primary(
        self,
    ) -> None:
        order: list[str] = []
        session = Mock()
        session.session_id = "6" * 32
        session.close.side_effect = lambda: order.append("session_close")
        runtime = Mock(spec=ExecutionToolRuntime)
        agent = Mock(spec=CodingAgent)
        agent.close.side_effect = lambda: order.append("agent_close")

        def post_close() -> None:
            order.append("post_close")
            raise ValueError("provider-post-close-secret")

        with (
            patch(f"{__name__}.SessionEventWorkflow", return_value=session),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", return_value=object()),
            patch(f"{__name__}.ExecutionToolRuntime", return_value=runtime),
            patch(f"{__name__}.CodingAgent", return_value=agent),
            self.assertRaisesRegex(RuntimeError, "primary-harness-failure") as caught,
        ):
            with _managed_agent(
                self._provider_list_settings(),
                Path("offline-session-never-opened"),
                _orphan_cleanup_snapshot(),
                _Client(lambda _messages: _text("unused")),
                lambda _request: True,
                _BackendStageRecorder(),
                recover=False,
                after_close=post_close,
            ):
                order.append("body")
                raise RuntimeError("primary-harness-failure")

        self.assertEqual(
            order,
            ["body", "agent_close", "session_close", "post_close"],
        )
        rendered_notes = repr(getattr(caught.exception, "__notes__", ()))
        self.assertIn("post-close verification", rendered_notes)
        self.assertNotIn("provider-post-close-secret", rendered_notes)

    def test_orphan_cleanup_script_requires_fixed_sanitized_contract(
        self,
    ) -> None:
        script = _OrphanCleanupScript()
        first = script([])
        self.assertEqual(self._call_id(first), "orphan_write")
        write_error = {
            "role": "tool",
            "tool_call_id": "orphan_write",
            "content": ToolResult(
                status="error",
                error=_REMOTE_EXECUTION_ERROR,
            ).model_dump_json(),
        }
        read_call = script([write_error])
        self.assertEqual(self._call_id(read_call), "orphan_read")
        baseline_size = len(_ORPHAN_BASELINE.encode("utf-8"))
        read_success = {
            "role": "tool",
            "tool_call_id": "orphan_read",
            "content": ToolResult(
                status="success",
                output=ReadFileOutput(
                    path="orphan_probe.py",
                    encoding="utf-8",
                    content=_ORPHAN_BASELINE,
                    offset=0,
                    bytes_read=baseline_size,
                    total_bytes=baseline_size,
                    truncated=False,
                ),
            ).model_dump_json(),
        }
        code_call = script([write_error, read_success])
        self.assertEqual(self._call_id(code_call), "orphan_code")
        active = [
            write_error,
            read_success,
            self._accepted("orphan_code", self._TASK_ORPHAN_CODE),
        ]
        waiting = script(active)
        self.assertEqual(
            waiting[0].choices[0].delta.content,
            "等待事务清理后的远端执行复验。",
        )
        completed = self._terminal(
            self._TASK_ORPHAN_CODE,
            "completed",
            result=ToolResult(
                status="success",
                output=CodeInterpreterOutput(
                    exit_code=0,
                    stdout="",
                    result="42",
                    stderr="",
                    truncated=False,
                ),
            ),
        )
        final = script([*active, completed])
        self.assertIn("事务 orphan 已清理", final[0].choices[0].delta.content)
        self.assertTrue(script.expected_failure_observed)
        self.assertIsNone(script.recorded_failure)

        leaked = _OrphanCleanupScript()
        self.assertEqual(self._call_id(leaked([])), "orphan_write")
        response = leaked([
            write_error,
            {"role": "user", "content": _ORPHAN_CLEANUP_INJECTION_SECRET},
        ])
        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError) as caught:
            leaked.raise_recorded_failure()
        diagnostic = str(caught.exception)
        self.assertIn("orphan_cleanup_secret_leaked", diagnostic)
        self.assertNotIn(_ORPHAN_CLEANUP_INJECTION_SECRET, diagnostic)

    def test_orphan_cleanup_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_ORPHAN_CLEANUP_GATE) == "1":
            self.skipTest("The operator explicitly enabled the orphan cleanup gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped orphan cleanup test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped orphan cleanup test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped orphan cleanup test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveOrphanCleanupE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_maintenance_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_MAINTENANCE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the maintenance gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped maintenance test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped maintenance test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped maintenance test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveMaintenanceE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_maintenance_scripts_require_exact_v3_and_restore_contracts(
        self,
    ) -> None:
        script_a = _MaintenanceScriptA()
        first = script_a([])
        self.assertEqual(self._call_id(first), "maintenance_format")
        format_success = {
            "role": "tool",
            "tool_call_id": "maintenance_format",
            "content": ToolResult(
                status="success",
                output=BashShellOutput(
                    command="format_json",
                    paths=["config/b.json", "config/a.json"],
                    exit_code=0,
                    stdout="已规范格式化 2 个 JSON 文件。",
                    stderr="",
                    truncated=False,
                ),
            ).model_dump_json(),
        }
        sealed_call = script_a([format_success])
        self.assertEqual(self._call_id(sealed_call), "maintenance_sealed")
        sealed_denied = {
            "role": "tool",
            "tool_call_id": "maintenance_sealed",
            "content": ToolResult(
                status="denied",
                output="",
                error="安全域禁止访问。",
            ).model_dump_json(),
        }
        final_a = script_a([format_success, sealed_denied])
        self.assertIn(
            "v3 generation",
            final_a[0].choices[0].delta.content,
        )
        self.assertIsNone(script_a.recorded_failure)

        script_b = _MaintenanceScriptB()
        read_a_call = script_b([])
        self.assertEqual(self._call_id(read_a_call), "maintenance_read_a")

        def read_message(call_id: str, path: str, body: bytes) -> dict[str, str]:
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "content": ToolResult(
                    status="success",
                    output=ReadFileOutput(
                        path=path,
                        encoding="utf-8",
                        content=body.decode("utf-8"),
                        offset=0,
                        bytes_read=len(body),
                        total_bytes=len(body),
                        truncated=False,
                    ),
                ).model_dump_json(),
            }

        read_a = read_message(
            "maintenance_read_a",
            "config/a.json",
            _MAINTENANCE_JSON_A_AFTER,
        )
        read_b_call = script_b([read_a])
        self.assertEqual(self._call_id(read_b_call), "maintenance_read_b")
        read_b = read_message(
            "maintenance_read_b",
            "config/b.json",
            _MAINTENANCE_JSON_B_AFTER,
        )
        final_b = script_b([read_a, read_b])
        self.assertIn("GC 复验", final_b[0].choices[0].delta.content)
        self.assertIsNone(script_b.recorded_failure)

        failed = _MaintenanceScriptA()
        self.assertEqual(self._call_id(failed([])), "maintenance_format")
        response = failed([{
            "role": "tool",
            "tool_call_id": "maintenance_format",
            "content": ToolResult(
                status="error",
                error=_REMOTE_EXECUTION_ERROR,
            ).model_dump_json(),
        }])
        self.assertEqual(
            response[0].choices[0].delta.content,
            _RECORDED_FAILURE_TEXT,
        )
        with self.assertRaises(AssertionError):
            failed.raise_recorded_failure()

    def test_maintenance_snapshot_and_command_coordinator_proxy_are_exact(
        self,
    ) -> None:
        snapshot = _maintenance_snapshot()
        _validate_snapshot(snapshot)
        self.assertEqual(
            tuple(item.path for item in snapshot.entries),
            ("README.md", "config/a.json", "config/b.json"),
        )

        report = WorkspaceOrphanGcReport(
            generations=1,
            retained_object_count=3,
            orphan_object_count=1,
            deleted_object_count=1,
            orphan_bytes=7,
            deleted_bytes=7,
            baseline_digest="a" * 64,
        )

        class Coordinator:
            last_orphan_gc_report = report

            def prepare_command(self, *args: object, **kwargs: object) -> str:
                del args, kwargs
                return "prepared"

            def commit_command(self, *args: object, **kwargs: object) -> str:
                del args, kwargs
                return "committed"

        stages = _BackendStageRecorder()
        wrapper = _DiagnosticWorkspaceCoordinator(Coordinator(), stages)
        self.assertEqual(wrapper.prepare_command(object()), "prepared")
        self.assertEqual(wrapper.commit_command(object()), "committed")
        self.assertIs(wrapper.last_orphan_gc_report, report)
        self.assertEqual(
            tuple(stages.history),
            (
                "lease_create",
                "command_prepare",
                "command_prepare_completed",
                "command_commit_v3",
                "command_commit_completed",
            ),
        )

    def test_maintenance_injectors_are_once_only_and_staging_is_empty(
        self,
    ) -> None:
        direct = _DirectKillFalseInjector()
        self.assertIs(direct(), False)
        self.assertEqual(direct.call_count, 1)
        with self.assertRaisesRegex(
            AssertionError,
            "maintenance_direct_kill_repeated",
        ):
            direct()

        sentinel = object()
        calls: list[tuple[tuple[str, ...], int]] = []

        def original(
            _transport: object,
            paths: Sequence[str],
            *,
            timeout_seconds: int,
        ) -> object:
            calls.append((tuple(paths), timeout_seconds))
            return sentinel

        files = SimpleNamespace(list=lambda *_args, **_kwargs: [])
        transport = SimpleNamespace(
            _workspace_root="/workspace/agnet-study/" + "a" * 32,
            _sandbox=SimpleNamespace(files=files),
        )
        observer = _FormatJsonStagingObserver(original)
        self.assertIs(
            observer(
                transport,  # type: ignore[arg-type]
                ("/workspace/agnet-study/" + "a" * 32 + "/a.json",),
                timeout_seconds=9,
            ),
            sentinel,
        )
        self.assertEqual(observer.call_count, 1)
        self.assertEqual(len(calls), 1)

        nonempty = _FormatJsonStagingObserver(original)
        transport._sandbox.files = SimpleNamespace(  # type: ignore[attr-defined]
            list=lambda *_args, **_kwargs: [object()]
        )
        with self.assertRaisesRegex(
            AssertionError,
            "maintenance_staging_not_empty",
        ):
            nonempty(
                transport,  # type: ignore[arg-type]
                ("/workspace/agnet-study/" + "a" * 32 + "/a.json",),
                timeout_seconds=9,
            )

    def test_maintenance_split_binding_and_close_are_session_scoped(self) -> None:
        execution = SimpleNamespace(sandbox_id="execution-fixed-id")
        persistence = SimpleNamespace(sandbox_id="persistence-fixed-id")
        split = SplitSandboxTransport(object(), object())  # type: ignore[arg-type]
        backend = SimpleNamespace(
            _condition=Lock(),
            _transport=split,
            _execution_sandbox=execution,
            _persistence_sandbox=persistence,
        )
        binding = _require_maintenance_split_binding(
            backend,
            "execution-fixed-id",
        )
        self.assertIs(binding.transport, split)
        self.assertIs(binding.execution_sandbox, execution)
        self.assertIs(binding.persistence_sandbox, persistence)

        verify = Mock(return_value=0)
        settings = self._provider_list_settings()
        with patch(
            f"{__name__}._verify_provider_session_has_no_active_sandboxes",
            verify,
        ):
            self.assertEqual(
                _verify_maintenance_split_session_closed(
                    settings,
                    "a" * 32,
                    (
                        binding.execution_sandbox_id,
                        binding.persistence_sandbox_id,
                    ),
                ),
                0,
            )
        verify.assert_called_once_with(settings, "a" * 32, "execution-fixed-id")

        persistence.sandbox_id = "provider-secret-id"
        with self.assertRaisesRegex(
            AssertionError,
            r"^maintenance_split_binding_invalid$",
        ) as caught:
            _require_maintenance_split_binding(backend, "provider-secret-id")
        self.assertNotIn("provider-secret-id", str(caught.exception))

    def test_managed_agent_forwards_explicit_orphan_gc_only(self) -> None:
        session = Mock()
        session.session_id = "7" * 32
        runtime = Mock(spec=ExecutionToolRuntime)
        agent = Mock(spec=CodingAgent)
        initializer = object()
        factory = Mock(return_value=initializer)
        backend = Mock()
        snapshot = _maintenance_snapshot()
        with (
            patch(f"{__name__}.SessionEventWorkflow", return_value=session),
            patch(
                f"{__name__}.create_workspace_checkpoint_initializer",
                factory,
            ),
            patch(
                f"{__name__}.AliyunSandboxExecutionBackend",
                return_value=backend,
            ),
            patch(f"{__name__}.ExecutionToolRuntime", return_value=runtime),
            patch(f"{__name__}.CodingAgent", return_value=agent),
        ):
            with _managed_agent(
                self._provider_list_settings(),
                Path("offline-maintenance-session-never-opened"),
                snapshot,
                _Client(lambda _messages: _text("unused")),
                lambda _request: True,
                _BackendStageRecorder(),
                recover=True,
                collect_orphans=True,
            ):
                pass
        factory.assert_called_once()
        self.assertIs(factory.call_args.args[0], snapshot)
        self.assertEqual(factory.call_args.kwargs, {"collect_orphans": True})
        agent.close.assert_called_once_with()
        session.close.assert_called_once_with()

    def test_orphan_cleanup_path_uses_ordinary_partial_control_contract(
        self,
    ) -> None:
        ordinary = route_mutation_approval(
            "partial-control",
            ("orphan_probe.py",),
            project_root=_ROOT,
        )
        sensitive = route_mutation_approval(
            "partial-control",
            ("AGENTS.md",),
            project_root=_ROOT,
        )
        unknown = route_mutation_approval(
            "partial-control",
            (),
            project_root=_ROOT,
            unknown_target=True,
        )

        self.assertFalse(ordinary.requires_approval)
        self.assertEqual(ordinary.reason, "ordinary project path")
        self.assertTrue(sensitive.requires_approval)
        self.assertEqual(sensitive.reason, "sensitive path")
        self.assertTrue(unknown.requires_approval)
        self.assertEqual(unknown.reason, "unknown mutation target")

    def test_default_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the live gate.")
        load = Mock(side_effect=AssertionError("Skipped live test loaded cloud settings."))
        backend = Mock(side_effect=AssertionError("Skipped live test created a backend."))
        active_list = Mock(
            side_effect=AssertionError("Skipped live test listed provider sandboxes.")
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveAgentE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_cancel_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_CANCEL_GATE) == "1":
            self.skipTest("The operator explicitly enabled the cancellation gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped cancellation test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped cancellation test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped cancellation test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveCancellationE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_timeout_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_TIMEOUT_GATE) == "1":
            self.skipTest("The operator explicitly enabled the timeout gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped timeout test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped timeout test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped timeout test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveTimeoutE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_wait_uncertainty_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_WAIT_UNCERTAINTY_GATE) == "1":
            self.skipTest("The operator explicitly enabled the wait uncertainty gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped wait uncertainty test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped wait uncertainty test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped wait uncertainty test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveWaitUncertaintyE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_prebind_uncertainty_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_PREBIND_UNCERTAINTY_GATE) == "1":
            self.skipTest("The operator explicitly enabled the pre-bind gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped pre-bind uncertainty test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped pre-bind uncertainty test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped pre-bind uncertainty test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLivePrebindUncertaintyE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()

    def test_kill_ack_uncertainty_gate_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_KILL_ACK_UNCERTAINTY_GATE) == "1":
            self.skipTest("The operator explicitly enabled the kill ack gate.")
        load = Mock(
            side_effect=AssertionError(
                "Skipped kill ack uncertainty test loaded cloud settings."
            )
        )
        backend = Mock(
            side_effect=AssertionError(
                "Skipped kill ack uncertainty test created a backend."
            )
        )
        active_list = Mock(
            side_effect=AssertionError(
                "Skipped kill ack test listed provider sandboxes."
            )
        )
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxLiveKillAckUncertaintyE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(f"{__name__}.load_sandbox_settings", load),
            patch(f"{__name__}.AliyunSandboxExecutionBackend", backend),
            patch(
                f"{__name__}._verify_provider_session_has_no_active_sandboxes",
                active_list,
            ),
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        load.assert_not_called()
        backend.assert_not_called()
        active_list.assert_not_called()


if __name__ == "__main__":
    unittest.main()
