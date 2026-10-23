"""Strictly gated provider-kill exception recovery for Aliyun Sandbox.

This harness exercises the exception path before a direct provider kill can
acknowledge success.  It wraps only the current concrete Sandbox and calls its
real SDK ``kill`` against a per-call loopback endpoint that accepts no request,
using a fixed non-secret credential, disabled retries, and a small positive
timeout.  The resulting exception comes from the real SDK/transport stack; it
is not raised by the harness.  The product must then use its independently
constructed, exact-session cleaner with
the untouched real settings to perform the real kill and prove the
provider-active count is zero.  No model client is constructed, so DeepSeek
cannot be contacted.  This is a controlled real SDK/transport exception, not a
claim that an unforced production outage happened naturally.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import re
import secrets
import socket
import threading
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
    SandboxRecoveryPage,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.execution_backend import AliyunSandboxExecutionBackend
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from tests.test_remote_mutation_runtime import _settings


_LIVE_GATE: Final[str] = "RUN_ALIYUN_SANDBOX_LIVE_NATURAL_KILL_EXCEPTION_E2E"
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0
_PROVIDER_VISIBILITY_TIMEOUT_SECONDS: Final[float] = 30.0
_PROVIDER_VISIBILITY_POLL_SECONDS: Final[float] = 0.25
_CONTROLLED_KILL_TIMEOUT_SECONDS: Final[float] = 0.25
_NON_SECRET_LOOPBACK_API_KEY: Final[str] = "controlled-loopback-non-credential"
_ALLOWED_STAGES: Final[frozenset[str]] = frozenset({
    "create",
    "ready",
    "provider_visible",
    "direct_kill_exception",
    "independent_cleanup",
    "active_zero",
})


class _HarnessError(RuntimeError):
    """Fixed-text live failure that cannot reflect provider content."""


@dataclass(frozen=True, slots=True)
class _KillExceptionReport:
    direct_exceptions: int
    independent_kills: int
    active_zero: int
    backend_closed: bool

    def __post_init__(self) -> None:
        counts = (
            self.direct_exceptions,
            self.independent_kills,
            self.active_zero,
        )
        if any(type(value) is not int or value != 1 for value in counts):
            raise _HarnessError("provider kill exception live 计数无效。")
        if type(self.backend_closed) is not bool or not self.backend_closed:
            raise _HarnessError("provider kill exception live backend 终态无效。")


def _bounded_observer_count(value: object) -> int:
    if type(value) is int and 0 <= value <= 1:
        return value
    return 0


def _sanitized_failure_summary(
    stage: str,
    observer: _ControlledProviderKillExceptionObserver | None,
    backend: object,
) -> str:
    safe_stage = stage if stage in _ALLOWED_STAGES else "create"
    direct_calls = _bounded_observer_count(
        getattr(observer, "direct_call_count", 0)
    )
    sdk_exceptions = _bounded_observer_count(
        getattr(observer, "provider_exception_count", 0)
    )
    cleaner_kills = _bounded_observer_count(
        getattr(observer, "independent_kill_count", 0)
    )
    backend_closed = 1 if getattr(backend, "closed", None) is True else 0
    return (
        "provider kill exception live 安全失败 "
        f"stage={safe_stage} direct_calls={direct_calls} "
        f"sdk_exceptions={sdk_exceptions} cleaner_kills={cleaner_kills} "
        f"backend_closed={backend_closed}。"
    )


@contextmanager
def _nonresponsive_loopback_api_url() -> Iterator[str]:
    """Yield a private endpoint that makes the real SDK time out safely."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        if type(port) is not int or not 0 < port <= 65535:
            raise _HarnessError(
                "provider kill exception live loopback 边界无效。"
            )
        yield f"http://127.0.0.1:{port}"


class _ControlledProviderKillExceptionObserver:
    """Observe one real direct exception and one exact cleaner kill."""

    def __init__(
        self,
        concrete_sandbox: object,
        direct_kill: Callable[..., object],
        recovery_kill: Callable[..., object],
    ) -> None:
        sandbox_id = getattr(concrete_sandbox, "sandbox_id", None)
        if (
            not isinstance(sandbox_id, str)
            or not sandbox_id
            or len(sandbox_id) > 256
            or any(ord(character) < 32 or character.isspace() for character in sandbox_id)
            or not callable(direct_kill)
            or not callable(recovery_kill)
        ):
            raise _HarnessError("provider kill exception live 注入目标无效。")
        self._target_digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()
        self._direct_kill = direct_kill
        self._recovery_kill = recovery_kill
        self._lock = threading.Lock()
        self.direct_call_count = 0
        self.provider_exception_count = 0
        self.independent_kill_count = 0

    def _require_target(self, sandbox_id: object) -> None:
        if (
            not isinstance(sandbox_id, str)
            or not secrets.compare_digest(
                hashlib.sha256(sandbox_id.encode("utf-8")).digest(),
                self._target_digest,
            )
        ):
            raise _HarnessError(
                "provider kill exception live 拒绝非目标 Sandbox。"
            )

    def direct_kill(self, *args: object, **kwargs: object) -> object:
        if args or kwargs:
            raise _HarnessError(
                "provider kill exception live direct kill 调用无效。"
            )
        with self._lock:
            if self.direct_call_count != 0:
                raise _HarnessError(
                    "provider kill exception live direct kill 调用越界。"
                )
            self.direct_call_count = 1
        try:
            with _nonresponsive_loopback_api_url() as api_url:
                return self._direct_kill(
                    api_url=api_url,
                    api_key=_NON_SECRET_LOOPBACK_API_KEY,
                    retries=0,
                    request_timeout=_CONTROLLED_KILL_TIMEOUT_SECONDS,
                )
        except Exception:
            with self._lock:
                self.provider_exception_count += 1
            raise

    def recovery_kill(
        self,
        provider: object,
        sandbox: object,
        *,
        request_timeout: float,
    ) -> object:
        self._require_target(getattr(sandbox, "sandbox_id", None))
        with self._lock:
            if self.independent_kill_count != 0:
                raise _HarnessError(
                    "provider kill exception live cleaner kill 调用越界。"
                )
        result = self._recovery_kill(
            provider,
            sandbox,
            request_timeout=request_timeout,
        )
        if result is True:
            with self._lock:
                self.independent_kill_count = 1
        return result


@contextmanager
def _inject_exact_provider_kill_exception(
    concrete_sandbox: object,
) -> Iterator[_ControlledProviderKillExceptionObserver]:
    direct_kill = getattr(concrete_sandbox, "kill", None)
    recovery_kill = _E2BSandboxRecoveryProvider.kill
    observer = _ControlledProviderKillExceptionObserver(
        concrete_sandbox,
        direct_kill,
        recovery_kill,
    )

    def observed_recovery_kill(
        provider: object,
        sandbox: object,
        *,
        request_timeout: float,
    ) -> object:
        return observer.recovery_kill(
            provider,
            sandbox,
            request_timeout=request_timeout,
        )

    with (
        patch.object(concrete_sandbox, "kill", observer.direct_kill),
        patch.object(
            _E2BSandboxRecoveryProvider,
            "kill",
            observed_recovery_kill,
        ),
    ):
        yield observer


def _provider_active_count(settings: SandboxSettings, session_id: str) -> int:
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise _HarnessError("provider kill exception live session 身份无效。")
    metadata = {
        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
        SANDBOX_SESSION_METADATA_KEY: session_id,
    }
    try:
        active = _scan_active(
            _E2BSandboxRecoveryProvider(settings),
            expected_metadata=metadata,
            deadline=time.monotonic() + _ACTIVE_SCAN_TIMEOUT_SECONDS,
        )
    except Exception:
        raise _HarnessError(
            "provider kill exception live active 计数失败。"
        ) from None
    return len(active)


def _wait_for_exact_provider_visibility(
    settings: SandboxSettings,
    session_id: str,
    sandbox_id: str,
    *,
    provider_factory: Callable[[SandboxSettings], object] = (
        _E2BSandboxRecoveryProvider
    ),
    active_scanner: Callable[..., object] = _scan_active,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise _HarnessError(
            "provider kill exception live provider visibility 身份无效。"
        )
    if (
        not isinstance(sandbox_id, str)
        or not sandbox_id
        or len(sandbox_id) > 256
        or any(ord(character) < 32 or character.isspace() for character in sandbox_id)
    ):
        raise _HarnessError(
            "provider kill exception live provider visibility 目标无效。"
        )
    target_digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()
    metadata = {
        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
        SANDBOX_SESSION_METADATA_KEY: session_id,
    }
    deadline = monotonic() + _PROVIDER_VISIBILITY_TIMEOUT_SECONDS
    try:
        provider = provider_factory(settings)
        while True:
            if monotonic() >= deadline:
                raise _HarnessError(
                    "provider kill exception live provider visibility 未确认。"
                )
            active = active_scanner(
                provider,
                expected_metadata=metadata,
                deadline=deadline,
            )
            if not isinstance(active, tuple):
                raise _HarnessError(
                    "provider kill exception live provider visibility 未确认。"
                )
            if len(active) == 2:
                target_matches = sum(
                    isinstance(observed_id, str)
                    and secrets.compare_digest(
                        hashlib.sha256(observed_id.encode("utf-8")).digest(),
                        target_digest,
                    )
                    for observed_id in active
                )
                if target_matches != 1:
                    raise _HarnessError(
                        "provider kill exception live provider visibility 未确认。"
                    )
                return
            if len(active) > 2:
                raise _HarnessError(
                    "provider kill exception live provider visibility 未确认。"
                )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise _HarnessError(
                    "provider kill exception live provider visibility 未确认。"
                )
            sleeper(min(_PROVIDER_VISIBILITY_POLL_SECONDS, remaining))
    except _HarnessError:
        raise
    except Exception:
        raise _HarnessError(
            "provider kill exception live provider visibility 未确认。"
        ) from None


def _build_live_backend(
    settings: SandboxSettings,
    session_id: str,
) -> AliyunSandboxExecutionBackend:
    # No tool execution is needed for this lifecycle-only acceptance case.
    # A supplied transport factory keeps the test focused on real create/kill
    # control-plane behavior and avoids constructing a model or host runner.
    return AliyunSandboxExecutionBackend(
        session_id,
        settings,
        transport_factory=lambda _sandbox: object(),
    )


def _run_live_kill_exception(
    settings: SandboxSettings,
    session_id: str,
    *,
    backend_factory: Callable[
        [SandboxSettings, str], AliyunSandboxExecutionBackend
    ] = _build_live_backend,
    injection_context: Callable[[object], object] = (
        _inject_exact_provider_kill_exception
    ),
    visibility_observer: Callable[[SandboxSettings, str, str], None] = (
        _wait_for_exact_provider_visibility
    ),
    emergency_cleanup: Callable[[SandboxSettings, str], None] = (
        cleanup_session_sandboxes
    ),
    active_count_observer: Callable[[SandboxSettings, str], int] = (
        _provider_active_count
    ),
) -> _KillExceptionReport:
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise _HarnessError("provider kill exception live session 身份无效。")
    backend = backend_factory(settings, session_id)
    primary_error: BaseException | None = None
    close_failed = False
    cleanup_failed = False
    active_failed = False
    injector: _ControlledProviderKillExceptionObserver | None = None
    stage = "create"
    try:
        lease = backend.ensure_ready()
        sandbox_id = lease.sandbox_id
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise _HarnessError(
                "provider kill exception live 缺少安全 Sandbox 身份。"
            )
        stage = "ready"
        with backend._condition:
            concrete_sandbox = backend._execution_sandbox
        concrete_id = getattr(concrete_sandbox, "sandbox_id", None)
        if (
            concrete_sandbox is None
            or not isinstance(concrete_id, str)
            or not secrets.compare_digest(
                hashlib.sha256(concrete_id.encode("utf-8")).digest(),
                hashlib.sha256(sandbox_id.encode("utf-8")).digest(),
            )
        ):
            raise _HarnessError(
                "provider kill exception live concrete Sandbox 绑定无效。"
            )
        stage = "provider_visible"
        visibility_observer(settings, session_id, sandbox_id)
        context = injection_context(concrete_sandbox)
        with context as selected_injector:  # type: ignore[attr-defined]
            if not isinstance(
                selected_injector,
                _ControlledProviderKillExceptionObserver,
            ):
                raise _HarnessError(
                    "provider kill exception live 注入边界无效。"
                )
            injector = selected_injector
            stage = "direct_kill_exception"
            backend.close()
        stage = "independent_cleanup"
        if (
            injector.direct_call_count != 1
            or injector.provider_exception_count != 1
            or injector.independent_kill_count != 1
            or not backend.closed
        ):
            raise _HarnessError(
                "provider kill exception live 独立 cleaner 接管不完整。"
            )
    except BaseException as error:
        if isinstance(error, _HarnessError) or not isinstance(error, Exception):
            primary_error = error
        else:
            primary_error = _HarnessError(
                f"provider kill exception live 在固定阶段 {stage} 安全失败。"
            )
    finally:
        try:
            backend.close()
        except BaseException:
            close_failed = True
        try:
            emergency_cleanup(settings, session_id)
        except BaseException:
            cleanup_failed = True
        try:
            active_failed = active_count_observer(settings, session_id) != 0
        except BaseException:
            active_failed = True

    if primary_error is not None:
        if close_failed or cleanup_failed or active_failed:
            primary_error.add_note(
                "provider kill exception live 的最终 active=0 未确认。"
            )
        if not isinstance(primary_error, Exception):
            raise primary_error
        sanitized = _HarnessError(
            _sanitized_failure_summary(stage, injector, backend)
        )
        if close_failed or cleanup_failed or active_failed:
            sanitized.add_note(
                "provider kill exception live 的最终 active=0 未确认。"
            )
        raise sanitized from None
    if close_failed or cleanup_failed or active_failed or injector is None:
        raise _HarnessError(
            "provider kill exception live 的最终 active=0 未确认。"
        )
    stage = "active_zero"
    if stage not in _ALLOWED_STAGES:
        raise _HarnessError("provider kill exception live 阶段无效。")
    return _KillExceptionReport(1, 1, 1, True)


def _execute_live_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[[SandboxSettings, str], _KillExceptionReport] = (
        _run_live_kill_exception
    ),
) -> _KillExceptionReport:
    selected = os.environ if environ is None else environ
    if selected.get(_LIVE_GATE) != "1":
        raise _HarnessError("provider kill exception live gate 未启用。")
    try:
        settings = settings_loader()
        settings.require_enabled()
        settings.require_recovery_enabled()
    except Exception:
        raise _HarnessError(
            "provider kill exception live 配置验证失败。"
        ) from None
    return runner(settings, uuid4().hex)


@unittest.skipUnless(
    os.environ.get(_LIVE_GATE) == "1",
    f"Set {_LIVE_GATE}=1 to run the real provider kill exception E2E",
)
class AliyunSandboxNaturalKillExceptionLiveE2ETests(unittest.TestCase):
    def test_direct_provider_exception_uses_exact_cleaner_and_reaches_zero(self) -> None:
        self.assertEqual(
            _execute_live_e2e(),
            _KillExceptionReport(1, 1, 1, True),
        )


class NaturalKillExceptionHarnessOfflineTests(unittest.TestCase):
    def test_gate_precedes_settings_sdk_backend_cleanup_and_provider(self) -> None:
        loader = Mock(side_effect=AssertionError("settings"))
        runner = Mock(side_effect=AssertionError("runner"))
        with (
            patch(__name__ + ".AliyunSandboxExecutionBackend", side_effect=AssertionError("backend")) as backend,
            patch(__name__ + ".cleanup_session_sandboxes", side_effect=AssertionError("cleanup")) as cleanup,
            patch(__name__ + "._provider_active_count", side_effect=AssertionError("provider")) as provider,
        ):
            with self.assertRaisesRegex(_HarnessError, "live gate"):
                _execute_live_e2e(
                    environ={},
                    settings_loader=loader,
                    runner=runner,
                )
        for boundary in (loader, runner, backend, cleanup, provider):
            boundary.assert_not_called()

    def test_default_live_suite_is_skipped_and_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the kill exception gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxNaturalKillExceptionLiveE2ETests
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

    def test_three_gates_are_required_before_runner(self) -> None:
        settings = Mock(spec=SandboxSettings)
        runner = Mock(return_value=_KillExceptionReport(1, 1, 1, True))
        self.assertEqual(
            _execute_live_e2e(
                environ={_LIVE_GATE: "1"},
                settings_loader=lambda: settings,
                runner=runner,
            ),
            _KillExceptionReport(1, 1, 1, True),
        )
        settings.require_enabled.assert_called_once_with()
        settings.require_recovery_enabled.assert_called_once_with()
        session_id = runner.call_args.args[1]
        self.assertRegex(session_id, _SESSION_ID_RE)

    def test_controlled_observer_uses_real_loopback_transport_then_exact_cleaner(self) -> None:
        direct_options: list[dict[str, object]] = []
        recovery_calls: list[str] = []
        concrete = SimpleNamespace(sandbox_id="sandbox-target")

        def direct(**kwargs):
            direct_options.append(dict(kwargs))
            raise OSError("raw-sdk-provider-error")

        def recovery(provider, sandbox, *, request_timeout):
            del provider, request_timeout
            recovery_calls.append(sandbox.sandbox_id)
            return True

        observer = _ControlledProviderKillExceptionObserver(
            concrete,
            direct,
            recovery,
        )
        with self.assertRaisesRegex(OSError, "raw-sdk-provider-error"):
            observer.direct_kill()
        self.assertEqual(len(direct_options), 1)
        options = direct_options[0]
        self.assertEqual(
            set(options),
            {"api_url", "api_key", "retries", "request_timeout"},
        )
        self.assertRegex(
            options["api_url"],
            r"http://127\.0\.0\.1:[1-9][0-9]{0,4}\Z",
        )
        self.assertEqual(options["api_key"], _NON_SECRET_LOOPBACK_API_KEY)
        self.assertEqual(options["retries"], 0)
        self.assertEqual(
            options["request_timeout"],
            _CONTROLLED_KILL_TIMEOUT_SECONDS,
        )
        recovered = SimpleNamespace(sandbox_id="sandbox-target")
        self.assertIs(
            observer.recovery_kill(
                object(), recovered, request_timeout=3.0
            ),
            True,
        )
        self.assertEqual(recovery_calls, ["sandbox-target"])
        self.assertEqual(
            (
                observer.direct_call_count,
                observer.provider_exception_count,
                observer.independent_kill_count,
            ),
            (1, 1, 1),
        )

        other = _ControlledProviderKillExceptionObserver(
            concrete,
            direct,
            recovery,
        )
        with self.assertRaisesRegex(_HarnessError, "非目标"):
            other.recovery_kill(
                object(),
                SimpleNamespace(sandbox_id="sandbox-other"),
                request_timeout=3.0,
            )
        self.assertEqual(recovery_calls, ["sandbox-target"])

    def test_loopback_trigger_raises_from_real_sdk_transport_without_cloud(self) -> None:
        from e2b.sandbox_sync.sandbox_api import SandboxApi

        concrete = SimpleNamespace(sandbox_id="offline-trigger-check")

        def real_sdk_kill(**kwargs):
            return SandboxApi._cls_kill(
                concrete.sandbox_id,
                debug=False,
                **kwargs,
            )

        observer = _ControlledProviderKillExceptionObserver(
            concrete,
            real_sdk_kill,
            lambda *_args, **_kwargs: True,
        )
        with self.assertRaises(Exception) as caught:
            observer.direct_kill()
        self.assertNotIsInstance(caught.exception, _HarnessError)
        self.assertEqual(
            (observer.direct_call_count, observer.provider_exception_count),
            (1, 1),
        )

    def test_provider_visibility_retries_until_exact_split_pair_contains_target(self) -> None:
        settings = Mock(spec=SandboxSettings)
        session_id = "d" * 32
        sandbox_id = "sandbox-target"
        provider = object()
        scans: list[tuple[str, ...]] = [
            (),
            (sandbox_id,),
            (sandbox_id, "sandbox-persistence"),
        ]
        observed_metadata: list[dict[str, str]] = []
        observed_deadlines: list[float] = []
        sleeps: list[float] = []

        def scanner(selected_provider, *, expected_metadata, deadline):
            self.assertIs(selected_provider, provider)
            observed_metadata.append(dict(expected_metadata))
            observed_deadlines.append(deadline)
            return scans.pop(0)

        _wait_for_exact_provider_visibility(
            settings,
            session_id,
            sandbox_id,
            provider_factory=lambda selected_settings: (
                provider
                if selected_settings is settings
                else self.fail("wrong settings")
            ),
            active_scanner=scanner,
            monotonic=lambda: 100.0,
            sleeper=sleeps.append,
        )
        self.assertFalse(scans)
        self.assertEqual(
            observed_metadata,
            [{
                SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
                SANDBOX_SESSION_METADATA_KEY: session_id,
            }] * 3,
        )
        self.assertEqual(observed_deadlines, [130.0, 130.0, 130.0])
        self.assertEqual(
            sleeps,
            [_PROVIDER_VISIBILITY_POLL_SECONDS] * 2,
        )

    def test_provider_visibility_rejects_wrong_multiple_error_and_timeout_safely(self) -> None:
        settings = Mock(spec=SandboxSettings)
        session_id = "e" * 32
        sandbox_id = "sandbox-target"

        def raw_error(*_args, **_kwargs):
            raise OSError("provider-secret-id-and-url")

        cases = (
            (
                lambda *_args, **_kwargs: ("sandbox-other", "sandbox-another"),
                lambda: 0.0,
            ),
            (
                lambda *_args, **_kwargs: (
                    sandbox_id,
                    "sandbox-persistence",
                    "sandbox-extra",
                ),
                lambda: 0.0,
            ),
            (raw_error, lambda: 0.0),
            (
                lambda *_args, **_kwargs: (),
                Mock(side_effect=(0.0, 0.0, 31.0)),
            ),
        )
        for scanner, clock in cases:
            with self.subTest(scanner=scanner), self.assertRaisesRegex(
                _HarnessError,
                "provider visibility 未确认",
            ) as caught:
                _wait_for_exact_provider_visibility(
                    settings,
                    session_id,
                    sandbox_id,
                    provider_factory=lambda _settings: object(),
                    active_scanner=scanner,
                    monotonic=clock,
                    sleeper=lambda _seconds: None,
                )
            rendered = str(caught.exception)
            self.assertNotIn("sandbox-other", rendered)
            self.assertNotIn("provider-secret", rendered)

    def test_product_close_recovers_raw_exception_with_exact_session_provider(self) -> None:
        session_id = "a" * 32
        target = "offline-natural-kill"
        expected_metadata = {
            SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
            SANDBOX_SESSION_METADATA_KEY: session_id,
        }
        direct_calls = 0
        recovered_calls = 0
        list_calls = 0

        class DirectSandbox:
            sandbox_id = target

            def kill(self):
                nonlocal direct_calls
                direct_calls += 1
                raise OSError("provider-raw-secret")

        class RecoveredSandbox:
            sandbox_id = target

        class Provider:
            def list_page(
                _self,
                *,
                metadata,
                states,
                limit,
                next_token,
                request_timeout,
            ):
                nonlocal list_calls
                del states, limit, next_token, request_timeout
                self.assertEqual(metadata, expected_metadata)
                list_calls += 1
                if list_calls in {1, 2, 4}:
                    return SandboxRecoveryPage(items=())
                if list_calls == 3:
                    return SandboxRecoveryPage(items=(SimpleNamespace(
                        sandbox_id=target,
                        state="running",
                        metadata=expected_metadata,
                    ),))
                raise AssertionError("unexpected list")

            def connect(_self, sandbox_id, *, request_timeout):
                del request_timeout
                self.assertEqual(sandbox_id, target)
                return RecoveredSandbox()

            def kill(_self, sandbox, *, request_timeout):
                nonlocal recovered_calls
                del request_timeout
                self.assertIsInstance(sandbox, RecoveredSandbox)
                recovered_calls += 1
                return True

        settings = _settings()
        backend = AliyunSandboxExecutionBackend(
            session_id,
            settings,
            sandbox_factory=lambda _settings, _metadata: DirectSandbox(),
            persistence_sandbox_factory=lambda _settings, _metadata: SimpleNamespace(
                sandbox_id="natural-kill-persistence",
                kill=lambda: True,
            ),
            transport_factory=lambda _sandbox: object(),
            persistence_transport_factory=lambda _sandbox: object(),
            recovery_provider=Provider(),
        )
        self.assertEqual(backend.ensure_ready().sandbox_id, target)
        self.assertIsNone(backend.close())
        self.assertTrue(backend.closed)
        self.assertEqual((direct_calls, recovered_calls, list_calls), (1, 1, 4))

    def test_harness_failure_still_closes_cleans_and_checks_active_zero(self) -> None:
        settings = Mock(spec=SandboxSettings)
        backend = Mock()
        backend.ensure_ready.side_effect = RuntimeError("provider-secret")
        cleanup = Mock()
        active = Mock(return_value=0)
        with self.assertRaisesRegex(
            _HarnessError,
            r"stage=create direct_calls=0 sdk_exceptions=0 "
            r"cleaner_kills=0 backend_closed=0",
        ) as caught:
            _run_live_kill_exception(
                settings,
                "b" * 32,
                backend_factory=lambda _settings, _session: backend,
                emergency_cleanup=cleanup,
                active_count_observer=active,
            )
        self.assertNotIn("provider-secret", str(caught.exception))
        backend.close.assert_called_once_with()
        cleanup.assert_called_once_with(settings, "b" * 32)
        active.assert_called_once_with(settings, "b" * 32)

    def test_visibility_failure_precedes_injection_and_still_finally_cleans(self) -> None:
        settings = Mock(spec=SandboxSettings)
        concrete = SimpleNamespace(sandbox_id="sandbox-target")

        class Backend:
            def __init__(self):
                self._condition = threading.Condition()
                self._execution_sandbox = concrete
                self.closed = False
                self.close_calls = 0

            def ensure_ready(self):
                return SimpleNamespace(sandbox_id="sandbox-target")

            def close(self):
                self.close_calls += 1
                self.closed = True

        backend = Backend()
        injection = Mock(side_effect=AssertionError("injection"))
        cleanup = Mock()
        active = Mock(return_value=0)

        def visibility(_settings, _session_id, _sandbox_id):
            raise OSError("provider-secret-id-and-url")

        with self.assertRaisesRegex(
            _HarnessError,
            r"stage=provider_visible direct_calls=0 sdk_exceptions=0 "
            r"cleaner_kills=0 backend_closed=1",
        ) as caught:
            _run_live_kill_exception(
                settings,
                "f" * 32,
                backend_factory=lambda _settings, _session: backend,
                injection_context=injection,
                visibility_observer=visibility,
                emergency_cleanup=cleanup,
                active_count_observer=active,
            )
        self.assertNotIn("provider-secret", str(caught.exception))
        injection.assert_not_called()
        self.assertEqual(backend.close_calls, 1)
        cleanup.assert_called_once_with(settings, "f" * 32)
        active.assert_called_once_with(settings, "f" * 32)

    def test_incomplete_takeover_reports_only_bounded_sanitized_diagnostics(self) -> None:
        settings = Mock(spec=SandboxSettings)
        concrete = SimpleNamespace(sandbox_id="sandbox-target")

        class Backend:
            closed = False

            def ensure_ready(self):
                return SimpleNamespace(sandbox_id="sandbox-target")

            @property
            def _condition(self):
                return threading.Condition()

            _execution_sandbox = concrete

            def close(self):
                self.closed = True

        observer = _ControlledProviderKillExceptionObserver(
            concrete,
            lambda **_kwargs: True,
            lambda *_args, **_kwargs: True,
        )
        observer.direct_call_count = 1

        @contextmanager
        def injected(_sandbox):
            yield observer

        with self.assertRaisesRegex(
            _HarnessError,
            r"stage=independent_cleanup direct_calls=1 sdk_exceptions=0 "
            r"cleaner_kills=0 backend_closed=1",
        ) as caught:
            _run_live_kill_exception(
                settings,
                "c" * 32,
                backend_factory=lambda _settings, _session: Backend(),
                injection_context=injected,
                visibility_observer=lambda _settings, _session, _sandbox: None,
                emergency_cleanup=lambda _settings, _session: None,
                active_count_observer=lambda _settings, _session: 0,
            )
        rendered = str(caught.exception)
        self.assertNotIn("sandbox-target", rendered)
        self.assertNotIn("127.0.0.1", rendered)

    def test_report_rejects_bool_and_non_terminal_counts(self) -> None:
        invalid = (
            (True, 1, 1, True),
            (1, 0, 1, True),
            (1, 1, 2, True),
            (1, 1, 1, 1),
            (1, 1, 1, False),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(_HarnessError):
                _KillExceptionReport(*values)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
