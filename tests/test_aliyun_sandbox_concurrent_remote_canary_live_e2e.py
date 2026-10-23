"""Strictly gated three-session concurrent Aliyun remote canary.

The worker path reuses the complete single-session default-remote canary.  Each
live attempt runs in its own spawned Python process because that canary drives
the real CLI and temporarily patches process-global state.  A multiprocessing
barrier releases exactly three workers together.  Results crossing the process
boundary contain only bounded integers; provider text and resource identities
are never returned to the parent process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import multiprocessing
import os
import queue
import threading
import time
from typing import Final, Protocol
import unittest
from unittest.mock import Mock, patch

from app.aliyun_sandbox_session_cleanup import cleanup_session_sandboxes
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from tests.test_aliyun_sandbox_default_remote_canary_live_e2e import (
    _SESSION_ID_RE,
    _VersionDelta,
    _live_runtime_factory,
    _provider_active_count,
    _run_default_remote_canary,
)


_LIVE_GATE: Final[str] = (
    "RUN_ALIYUN_SANDBOX_LIVE_CONCURRENT_REMOTE_CANARY_E2E"
)
_CONCURRENCY: Final[int] = 3
_BARRIER_TIMEOUT_SECONDS: Final[float] = 120.0
_RESULT_QUEUE_TIMEOUT_SECONDS: Final[float] = 1.0
_IDENTITY_QUEUE_TIMEOUT_SECONDS: Final[float] = 1.0
_PROCESS_WALL_TIMEOUT_SECONDS: Final[float] = 3_900.0
_PROCESS_STOP_GRACE_SECONDS: Final[float] = 10.0
_MAX_ATTEMPT_MILLISECONDS: Final[int] = 3_600_000
_MAX_TOTAL_MILLISECONDS: Final[int] = 3_900_000


class _ConcurrentHarnessError(RuntimeError):
    """Fixed-text failure which never includes provider-derived content."""


class _StartBarrier(Protocol):
    def wait(self, timeout: float | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class _AttemptOutcome:
    attempt: int
    success: int
    active_zero: int
    object_delta: int
    generation_delta: int
    milliseconds: int

    def __post_init__(self) -> None:
        values = (
            self.attempt,
            self.success,
            self.active_zero,
            self.object_delta,
            self.generation_delta,
            self.milliseconds,
        )
        if any(type(value) is not int for value in values):
            raise _ConcurrentHarnessError(
                "concurrent remote canary attempt 类型无效。"
            )
        if (
            not 1 <= self.attempt <= _CONCURRENCY
            or self.success not in {0, 1}
            or self.active_zero not in {0, 1}
            or self.object_delta not in {0, 1}
            or self.generation_delta not in {0, 1}
            or not 0 <= self.milliseconds <= _MAX_ATTEMPT_MILLISECONDS
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary attempt 边界无效。"
            )
        if self.success == 1 and (
            self.active_zero != 1
            or self.object_delta != 1
            or self.generation_delta != 1
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 成功 attempt 不完整。"
            )
        if self.success == 0 and (
            self.object_delta != 0 or self.generation_delta != 0
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 失败 attempt 增量无效。"
            )

    def to_wire(self) -> tuple[int, int, int, int, int, int]:
        return (
            self.attempt,
            self.success,
            self.active_zero,
            self.object_delta,
            self.generation_delta,
            self.milliseconds,
        )

    @classmethod
    def from_wire(cls, value: object) -> "_AttemptOutcome":
        if (
            type(value) is not tuple
            or len(value) != 6
            or any(type(item) is not int for item in value)
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 子进程结果无效。"
            )
        return cls(*value)


@dataclass(frozen=True, slots=True)
class _ConcurrentReport:
    """Bounded, identity-free aggregate for the fixed concurrent gate."""

    concurrency: int
    attempts: int
    successes: int
    failures: int
    active_zero: int
    object_delta: int
    generation_delta: int
    delta_complete: int
    attempt_milliseconds: tuple[int, ...]
    total_milliseconds: int
    min_milliseconds: int
    max_milliseconds: int

    def __post_init__(self) -> None:
        counts = (
            self.concurrency,
            self.attempts,
            self.successes,
            self.failures,
            self.active_zero,
            self.object_delta,
            self.generation_delta,
            self.delta_complete,
        )
        if any(type(value) is not int for value in counts):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 聚合计数类型无效。"
            )
        if (
            self.concurrency != _CONCURRENCY
            or self.attempts != _CONCURRENCY
            or not 0 <= self.successes <= _CONCURRENCY
            or not 0 <= self.failures <= _CONCURRENCY
            or self.successes + self.failures != _CONCURRENCY
            or not self.successes <= self.active_zero <= _CONCURRENCY
            or self.object_delta != self.successes
            or self.generation_delta != self.successes
            or self.delta_complete != int(self.failures == 0)
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 聚合计数边界无效。"
            )
        if type(self.attempt_milliseconds) is not tuple or len(
            self.attempt_milliseconds
        ) != _CONCURRENCY:
            raise _ConcurrentHarnessError(
                "concurrent remote canary 单次耗时数组无效。"
            )
        if any(
            type(value) is not int
            or not 0 <= value <= _MAX_ATTEMPT_MILLISECONDS
            for value in self.attempt_milliseconds
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 单次耗时边界无效。"
            )
        durations = (
            self.total_milliseconds,
            self.min_milliseconds,
            self.max_milliseconds,
        )
        if any(type(value) is not int for value in durations):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 聚合耗时类型无效。"
            )
        expected_min = min(self.attempt_milliseconds)
        expected_max = max(self.attempt_milliseconds)
        if (
            self.min_milliseconds != expected_min
            or self.max_milliseconds != expected_max
            or not expected_max <= self.total_milliseconds <= _MAX_TOTAL_MILLISECONDS
        ):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 聚合耗时边界无效。"
            )
        if self.failures == 0 and self.active_zero != _CONCURRENCY:
            raise _ConcurrentHarnessError(
                "concurrent remote canary 成功清理计数无效。"
            )

    def summary(self) -> str:
        return (
            "CONCURRENT_REMOTE_CANARY"
            f" concurrency={self.concurrency}"
            f" attempts={self.attempts}"
            f" successes={self.successes}"
            f" failures={self.failures}"
            f" active_zero={self.active_zero}"
            f" object_delta={self.object_delta}"
            f" generation_delta={self.generation_delta}"
            f" delta_complete={self.delta_complete}"
            f" total_ms={self.total_milliseconds}"
            f" min_ms={self.min_milliseconds}"
            f" max_ms={self.max_milliseconds}"
            " capacity_limit_inferred=0"
        )


class _ConcurrentRunFailed(_ConcurrentHarnessError):
    def __init__(self, report: _ConcurrentReport) -> None:
        self.report = report
        super().__init__(report.summary() + " result=failed")


def _elapsed_milliseconds(started_ns: object, finished_ns: object) -> int:
    if (
        type(started_ns) is not int
        or type(finished_ns) is not int
        or started_ns < 0
        or finished_ns < started_ns
    ):
        raise _ConcurrentHarnessError(
            "concurrent remote canary 时钟无效。"
        )
    elapsed = (finished_ns - started_ns) // 1_000_000
    if elapsed > _MAX_TOTAL_MILLISECONDS:
        raise _ConcurrentHarnessError(
            "concurrent remote canary 耗时越界。"
        )
    return elapsed


def _run_attempt_after_barrier(
    attempt: int,
    settings: SandboxSettings,
    barrier: _StartBarrier,
    *,
    attempt_runner: Callable[..., _VersionDelta] = _run_default_remote_canary,
    active_count_observer: Callable[[SandboxSettings, str], int] = (
        _provider_active_count
    ),
    clock_ns: Callable[[], int] = time.monotonic_ns,
) -> _AttemptOutcome:
    """Release with the other two workers, then run one complete canary."""

    active_confirmed = 0
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)
    except BaseException:
        return _AttemptOutcome(attempt, 0, 0, 0, 0, 0)

    started = clock_ns()

    def observe_active_zero(
        selected_settings: SandboxSettings,
        session_id: str,
    ) -> int:
        nonlocal active_confirmed
        count = active_count_observer(selected_settings, session_id)
        if type(count) is int and count == 0:
            active_confirmed = 1
        return count

    success = 0
    object_delta = 0
    generation_delta = 0
    try:
        delta = attempt_runner(
            settings,
            active_count_observer=observe_active_zero,
        )
        if delta != _VersionDelta(objects=1, generations=1):
            raise _ConcurrentHarnessError(
                "concurrent remote canary 单次增量无效。"
            )
        success = 1
        object_delta = 1
        generation_delta = 1
    except BaseException:
        # Never retain or return the provider/SDK/CLI exception.  In
        # particular, quota and rate-limit text is not evidence of a known
        # account maximum.
        success = 0
        object_delta = 0
        generation_delta = 0

    try:
        elapsed = _elapsed_milliseconds(started, clock_ns())
    except BaseException:
        return _AttemptOutcome(attempt, 0, active_confirmed, 0, 0, 0)
    if success == 1 and active_confirmed != 1:
        return _AttemptOutcome(attempt, 0, 0, 0, 0, elapsed)
    return _AttemptOutcome(
        attempt,
        success,
        active_confirmed,
        object_delta,
        generation_delta,
        elapsed,
    )


def _live_process_entry(
    attempt: int,
    settings: SandboxSettings,
    barrier: object,
    result_queue: object,
    identity_queue: object,
) -> None:
    """Spawn-safe child entry which emits one integer-only tuple."""

    def reporting_runtime_factory(recorder, observed):
        create_runtime = _live_runtime_factory(recorder, observed)

        def create(
            execution_backend,
            session_id,
            selected_settings,
            *,
            collect_orphans=False,
        ):
            if (
                not isinstance(session_id, str)
                or _SESSION_ID_RE.fullmatch(session_id) is None
            ):
                raise _ConcurrentHarnessError(
                    "concurrent remote canary 子进程身份无效。"
                )
            # This internal control coordinate is sent before any backend can
            # create cloud resources.  It is used only for exact parent-side
            # emergency cleanup and never enters a report or exception.
            identity_queue.put((attempt, session_id), timeout=10)  # type: ignore[attr-defined]
            identity_queue.close()  # type: ignore[attr-defined]
            identity_queue.join_thread()  # type: ignore[attr-defined]
            return create_runtime(
                execution_backend,
                session_id,
                selected_settings,
                collect_orphans=collect_orphans,
            )

        return create

    def run_with_identity(selected_settings, *, active_count_observer):
        return _run_default_remote_canary(
            selected_settings,
            runtime_factory_builder=reporting_runtime_factory,
            active_count_observer=active_count_observer,
        )

    outcome = _run_attempt_after_barrier(
        attempt,
        settings,
        barrier,  # type: ignore[arg-type]
        attempt_runner=run_with_identity,
    )
    try:
        result_queue.put(outcome.to_wire())  # type: ignore[attr-defined]
    except BaseException:
        # The parent treats a missing result as a fixed failure.  Do not print
        # the queue exception because it may contain process internals.
        return None


def _offline_spawn_entry(
    attempt: int,
    settings: SandboxSettings,
    barrier: object,
    result_queue: object,
    identity_queue: object,
) -> None:
    """Picklable zero-cloud worker used only to test spawn/barrier plumbing."""

    del settings, identity_queue
    try:
        barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
        result_queue.put(  # type: ignore[attr-defined]
            _AttemptOutcome(attempt, 1, 1, 1, 1, attempt).to_wire()
        )
    except BaseException:
        return None


def _offline_missing_result_entry(
    attempt: int,
    settings: SandboxSettings,
    barrier: object,
    result_queue: object,
    identity_queue: object,
) -> None:
    """Exit normally after the barrier without producing a result."""

    del attempt, settings, result_queue, identity_queue
    barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)  # type: ignore[attr-defined]


def _offline_nonzero_exit_entry(
    attempt: int,
    settings: SandboxSettings,
    barrier: object,
    result_queue: object,
    identity_queue: object,
) -> None:
    """Exit non-zero after all three workers reached the barrier."""

    del attempt, settings, result_queue, identity_queue
    barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
    raise SystemExit(7)


def _offline_hanging_entry(
    attempt: int,
    settings: SandboxSettings,
    barrier: object,
    result_queue: object,
    identity_queue: object,
) -> None:
    """Publish a synthetic identity, then hang until the parent stops us."""

    del settings, result_queue
    barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
    identity_queue.put(  # type: ignore[attr-defined]
        (attempt, format(attempt, "x") * 32), timeout=10
    )
    identity_queue.close()  # type: ignore[attr-defined]
    identity_queue.join_thread()  # type: ignore[attr-defined]
    threading.Event().wait(60.0)


def _offline_unclean_failure_entry(
    attempt: int,
    settings: SandboxSettings,
    barrier: object,
    result_queue: object,
    identity_queue: object,
) -> None:
    """Return a cleaned-unconfirmed failure for parent cleaner retry tests."""

    del settings
    barrier.wait(timeout=_BARRIER_TIMEOUT_SECONDS)  # type: ignore[attr-defined]
    identity_queue.put(  # type: ignore[attr-defined]
        (attempt, format(attempt, "x") * 32), timeout=10
    )
    identity_queue.close()  # type: ignore[attr-defined]
    identity_queue.join_thread()  # type: ignore[attr-defined]
    result_queue.put(  # type: ignore[attr-defined]
        _AttemptOutcome(attempt, 0, 0, 0, 0, attempt).to_wire()
    )


def _run_spawned_attempts(
    settings: SandboxSettings,
    *,
    process_entry: Callable[..., None] = _live_process_entry,
    process_starter: Callable[[object, int], None] | None = None,
    process_wall_timeout_seconds: float = _PROCESS_WALL_TIMEOUT_SECONDS,
    emergency_cleanup: Callable[
        [SandboxSettings, str], None
    ] = cleanup_session_sandboxes,
    active_count_observer: Callable[[SandboxSettings, str], int] = (
        _provider_active_count
    ),
) -> tuple[_AttemptOutcome, ...]:
    """Start exactly three isolated workers and collect sanitized outcomes."""

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(_CONCURRENCY)
    result_queue = context.Queue(maxsize=_CONCURRENCY)
    identity_queue = context.Queue(maxsize=_CONCURRENCY)
    processes = [
        context.Process(
            target=process_entry,
            args=(attempt, settings, barrier, result_queue, identity_queue),
            name=f"remote-canary-{attempt}",
        )
        for attempt in range(1, _CONCURRENCY + 1)
    ]
    starter = process_starter or (lambda process, _attempt: process.start())
    started: list[tuple[int, object]] = []
    try:
        for attempt, process in enumerate(processes, start=1):
            starter(process, attempt)
            started.append((attempt, process))
    except BaseException:
        try:
            barrier.abort()
        except BaseException:
            pass
    deadline = time.monotonic() + process_wall_timeout_seconds
    timed_out: set[int] = set()
    for attempt, process in started:
        process.join(max(0.0, deadline - time.monotonic()))
        if process.is_alive():
            timed_out.add(attempt)
    for attempt, process in started:
        if attempt not in timed_out:
            continue
        try:
            process.terminate()
            process.join(_PROCESS_STOP_GRACE_SECONDS)
            if process.is_alive():
                process.kill()
                process.join(_PROCESS_STOP_GRACE_SECONDS)
        except BaseException:
            pass

    outcomes: dict[int, _AttemptOutcome] = {}
    for _ in range(len(started)):
        try:
            raw = result_queue.get(timeout=_RESULT_QUEUE_TIMEOUT_SECONDS)
            outcome = _AttemptOutcome.from_wire(raw)
        except queue.Empty:
            break
        except _ConcurrentHarnessError:
            continue
        if outcome.attempt not in outcomes:
            outcomes[outcome.attempt] = outcome

    identities: dict[int, str] = {}
    for _ in range(len(started)):
        try:
            raw_identity = identity_queue.get(
                timeout=_IDENTITY_QUEUE_TIMEOUT_SECONDS
            )
        except queue.Empty:
            break
        if (
            type(raw_identity) is tuple
            and len(raw_identity) == 2
            and type(raw_identity[0]) is int
            and isinstance(raw_identity[1], str)
            and 1 <= raw_identity[0] <= _CONCURRENCY
            and _SESSION_ID_RE.fullmatch(raw_identity[1]) is not None
        ):
            identities.setdefault(raw_identity[0], raw_identity[1])

    started_by_attempt = dict(started)
    for attempt in range(1, _CONCURRENCY + 1):
        process = started_by_attempt.get(attempt)
        abnormal = (
            process is None
            or attempt in timed_out
            or process.exitcode != 0
            or attempt not in outcomes
            or outcomes[attempt].active_zero != 1
        )
        if not abnormal:
            continue
        active_zero = 0
        session_id = identities.get(attempt)
        if session_id is not None and (
            process is None or not process.is_alive()
        ):
            try:
                emergency_cleanup(settings, session_id)
                if active_count_observer(settings, session_id) == 0:
                    active_zero = 1
            except BaseException:
                active_zero = 0
        outcomes[attempt] = _AttemptOutcome(
            attempt,
            0,
            active_zero,
            0,
            0,
            outcomes.get(
                attempt,
                _AttemptOutcome(attempt, 0, 0, 0, 0, 0),
            ).milliseconds,
        )
    try:
        result_queue.close()
        result_queue.join_thread()
        identity_queue.close()
        identity_queue.join_thread()
    except BaseException:
        pass
    return tuple(
        outcomes.get(
            attempt,
            _AttemptOutcome(attempt, 0, 0, 0, 0, 0),
        )
        for attempt in range(1, _CONCURRENCY + 1)
    )


def _make_report(
    outcomes: Sequence[_AttemptOutcome],
    *,
    total_milliseconds: int,
) -> _ConcurrentReport:
    if (
        len(outcomes) != _CONCURRENCY
        or tuple(outcome.attempt for outcome in outcomes) != (1, 2, 3)
    ):
        raise _ConcurrentHarnessError(
            "concurrent remote canary attempt 集合无效。"
        )
    durations = tuple(outcome.milliseconds for outcome in outcomes)
    successes = sum(outcome.success for outcome in outcomes)
    return _ConcurrentReport(
        concurrency=_CONCURRENCY,
        attempts=_CONCURRENCY,
        successes=successes,
        failures=_CONCURRENCY - successes,
        active_zero=sum(outcome.active_zero for outcome in outcomes),
        object_delta=sum(outcome.object_delta for outcome in outcomes),
        generation_delta=sum(outcome.generation_delta for outcome in outcomes),
        delta_complete=int(successes == _CONCURRENCY),
        attempt_milliseconds=durations,
        total_milliseconds=total_milliseconds,
        min_milliseconds=min(durations),
        max_milliseconds=max(durations),
    )


def _run_concurrent_remote_canary(
    settings: SandboxSettings,
    *,
    attempt_group_runner: Callable[
        [SandboxSettings], tuple[_AttemptOutcome, ...]
    ] = _run_spawned_attempts,
    clock_ns: Callable[[], int] = time.monotonic_ns,
    summary_sink: Callable[[str], None] = print,
) -> _ConcurrentReport:
    """Run one fixed three-session wave; failures never cause replacement work."""

    started = clock_ns()
    try:
        outcomes = attempt_group_runner(settings)
    except BaseException:
        outcomes = tuple(
            _AttemptOutcome(attempt, 0, 0, 0, 0, 0)
            for attempt in range(1, _CONCURRENCY + 1)
        )
    try:
        total = _elapsed_milliseconds(started, clock_ns())
        report = _make_report(outcomes, total_milliseconds=total)
    except BaseException:
        report = _make_report(
            tuple(
                _AttemptOutcome(attempt, 0, 0, 0, 0, 0)
                for attempt in range(1, _CONCURRENCY + 1)
            ),
            total_milliseconds=0,
        )
    if report.failures:
        raise _ConcurrentRunFailed(report)
    summary_sink(report.summary())
    return report


def _execute_live_concurrent_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[[SandboxSettings], _ConcurrentReport] = (
        _run_concurrent_remote_canary
    ),
) -> _ConcurrentReport:
    selected = os.environ if environ is None else environ
    if selected.get(_LIVE_GATE) != "1":
        raise _ConcurrentHarnessError(
            "concurrent remote canary live gate 未启用。"
        )
    try:
        settings = settings_loader()
        settings.require_enabled()
        settings.require_recovery_enabled()
    except BaseException:
        raise _ConcurrentHarnessError(
            "concurrent remote canary 配置验证失败。"
        ) from None
    return runner(settings)


@unittest.skipUnless(
    os.environ.get(_LIVE_GATE) == "1",
    f"Set {_LIVE_GATE}=1 to run the real fixed three-session concurrent canary",
)
class AliyunSandboxConcurrentRemoteCanaryLiveE2ETests(unittest.TestCase):
    def test_three_sessions_overlap_commit_rollback_and_clean(self) -> None:
        report = _execute_live_concurrent_e2e()
        self.assertEqual(
            (
                report.concurrency,
                report.attempts,
                report.successes,
                report.failures,
                report.active_zero,
                report.object_delta,
                report.generation_delta,
            ),
            (3, 3, 3, 0, 3, 3, 3),
        )


class ConcurrentRemoteCanaryHarnessOfflineTests(unittest.TestCase):
    def _thread_group(
        self,
        settings: SandboxSettings,
        runner: Callable[..., _VersionDelta],
        active_observer: Callable[[SandboxSettings, str], int],
    ) -> tuple[_AttemptOutcome, ...]:
        barrier = threading.Barrier(_CONCURRENCY)
        with ThreadPoolExecutor(max_workers=_CONCURRENCY) as executor:
            futures = [
                executor.submit(
                    _run_attempt_after_barrier,
                    attempt,
                    settings,
                    barrier,
                    attempt_runner=runner,
                    active_count_observer=active_observer,
                )
                for attempt in range(1, _CONCURRENCY + 1)
            ]
        return tuple(future.result() for future in futures)

    def test_gate_precedes_settings_runner_and_provider(self) -> None:
        loader = Mock(side_effect=AssertionError("settings"))
        runner = Mock(side_effect=AssertionError("runner"))
        with self.assertRaisesRegex(_ConcurrentHarnessError, "live gate"):
            _execute_live_concurrent_e2e(
                environ={}, settings_loader=loader, runner=runner
            )
        loader.assert_not_called()
        runner.assert_not_called()

    def test_default_suite_is_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the concurrent gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxConcurrentRemoteCanaryLiveE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(
                __name__ + ".load_sandbox_settings",
                side_effect=AssertionError("settings"),
            ) as loader,
            patch(
                __name__ + "._run_spawned_attempts",
                side_effect=AssertionError("cloud"),
            ) as cloud,
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        loader.assert_not_called()
        cloud.assert_not_called()

    def test_config_failure_is_sanitized_and_stops_before_runner(self) -> None:
        runner = Mock(side_effect=AssertionError("runner"))
        with self.assertRaisesRegex(_ConcurrentHarnessError, "配置验证失败") as caught:
            _execute_live_concurrent_e2e(
                environ={_LIVE_GATE: "1"},
                settings_loader=Mock(side_effect=RuntimeError("secret-provider")),
                runner=runner,
            )
        self.assertNotIn("secret-provider", str(caught.exception))
        runner.assert_not_called()

    def test_barrier_forces_exactly_three_attempts_to_overlap(self) -> None:
        settings = Mock(spec=SandboxSettings)
        lock = threading.Lock()
        all_running = threading.Event()
        active = 0
        maximum = 0
        calls = 0

        def runner(_settings, *, active_count_observer):
            nonlocal active, maximum, calls
            self.assertIs(_settings, settings)
            with lock:
                calls += 1
                active += 1
                maximum = max(maximum, active)
                if active == _CONCURRENCY:
                    all_running.set()
            try:
                if not all_running.wait(5):
                    raise RuntimeError("not-overlapped")
                active_count_observer(_settings, "a" * 32)
                return _VersionDelta(1, 1)
            finally:
                with lock:
                    active -= 1

        outcomes = self._thread_group(settings, runner, lambda _s, _i: 0)
        self.assertEqual(calls, _CONCURRENCY)
        self.assertEqual(maximum, _CONCURRENCY)
        self.assertTrue(all(outcome.success == 1 for outcome in outcomes))

    def test_spawn_group_uses_three_picklable_zero_cloud_workers(self) -> None:
        outcomes = _run_spawned_attempts(
            SandboxSettings(),
            process_entry=_offline_spawn_entry,
        )
        self.assertEqual(
            outcomes,
            (
                _AttemptOutcome(1, 1, 1, 1, 1, 1),
                _AttemptOutcome(2, 1, 1, 1, 1, 2),
                _AttemptOutcome(3, 1, 1, 1, 1, 3),
            ),
        )

    def test_spawn_start_failure_aborts_wave_and_starts_no_replacement(self) -> None:
        starts: list[int] = []

        def starter(process, attempt):
            starts.append(attempt)
            if attempt == 2:
                raise RuntimeError("start-secret")
            process.start()

        outcomes = _run_spawned_attempts(
            SandboxSettings(),
            process_entry=_offline_spawn_entry,
            process_starter=starter,
        )
        self.assertEqual(starts, [1, 2])
        self.assertEqual(
            outcomes,
            tuple(
                _AttemptOutcome(attempt, 0, 0, 0, 0, 0)
                for attempt in range(1, 4)
            ),
        )

    def test_spawn_missing_results_fail_closed(self) -> None:
        outcomes = _run_spawned_attempts(
            SandboxSettings(),
            process_entry=_offline_missing_result_entry,
        )
        self.assertEqual(
            outcomes,
            tuple(
                _AttemptOutcome(attempt, 0, 0, 0, 0, 0)
                for attempt in range(1, 4)
            ),
        )

    def test_spawn_nonzero_exits_fail_closed(self) -> None:
        outcomes = _run_spawned_attempts(
            SandboxSettings(),
            process_entry=_offline_nonzero_exit_entry,
        )
        self.assertEqual(
            outcomes,
            tuple(
                _AttemptOutcome(attempt, 0, 0, 0, 0, 0)
                for attempt in range(1, 4)
            ),
        )

    def test_spawn_timeout_stops_workers_then_cleans_exact_identities(self) -> None:
        cleanup = Mock()
        active = Mock(return_value=0)
        outcomes = _run_spawned_attempts(
            SandboxSettings(),
            process_entry=_offline_hanging_entry,
            process_wall_timeout_seconds=2.0,
            emergency_cleanup=cleanup,
            active_count_observer=active,
        )
        self.assertEqual(
            outcomes,
            tuple(
                _AttemptOutcome(attempt, 0, 1, 0, 0, 0)
                for attempt in range(1, 4)
            ),
        )
        self.assertEqual(cleanup.call_count, 3)
        self.assertEqual(active.call_count, 3)
        cleaned_ids = {call.args[1] for call in cleanup.call_args_list}
        self.assertEqual(
            cleaned_ids,
            {"1" * 32, "2" * 32, "3" * 32},
        )

    def test_unclean_failure_retries_parent_cleaner_and_confirms_active_zero(self) -> None:
        cleanup = Mock()
        active = Mock(return_value=0)
        outcomes = _run_spawned_attempts(
            SandboxSettings(),
            process_entry=_offline_unclean_failure_entry,
            emergency_cleanup=cleanup,
            active_count_observer=active,
        )
        self.assertEqual(
            outcomes,
            tuple(
                _AttemptOutcome(attempt, 0, 1, 0, 0, attempt)
                for attempt in range(1, 4)
            ),
        )
        self.assertEqual(cleanup.call_count, 3)
        self.assertEqual(active.call_count, 3)

    def test_fixed_concurrency_cannot_be_expanded_by_environment(self) -> None:
        settings = Mock(spec=SandboxSettings)
        calls = 0
        lock = threading.Lock()

        def runner(_settings, *, active_count_observer):
            nonlocal calls
            with lock:
                calls += 1
            active_count_observer(_settings, "b" * 32)
            return _VersionDelta(1, 1)

        with patch.dict(
            os.environ,
            {"RUN_ALIYUN_SANDBOX_LIVE_CONCURRENT_REMOTE_CANARY_CONCURRENCY": "99"},
        ):
            outcomes = self._thread_group(settings, runner, lambda _s, _i: 0)
        self.assertEqual(calls, 3)
        self.assertEqual(tuple(item.attempt for item in outcomes), (1, 2, 3))

    def test_success_aggregate_is_exact_bounded_and_identity_free(self) -> None:
        outcomes = (
            _AttemptOutcome(1, 1, 1, 1, 1, 7),
            _AttemptOutcome(2, 1, 1, 1, 1, 5),
            _AttemptOutcome(3, 1, 1, 1, 1, 9),
        )
        report = _make_report(outcomes, total_milliseconds=12)
        self.assertEqual(
            report,
            _ConcurrentReport(
                concurrency=3,
                attempts=3,
                successes=3,
                failures=0,
                active_zero=3,
                object_delta=3,
                generation_delta=3,
                delta_complete=1,
                attempt_milliseconds=(7, 5, 9),
                total_milliseconds=12,
                min_milliseconds=5,
                max_milliseconds=9,
            ),
        )
        summary = report.summary()
        self.assertEqual(
            summary,
            "CONCURRENT_REMOTE_CANARY concurrency=3 attempts=3 successes=3 "
            "failures=0 active_zero=3 object_delta=3 generation_delta=3 "
            "delta_complete=1 total_ms=12 min_ms=5 max_ms=9 "
            "capacity_limit_inferred=0",
        )
        for forbidden in (
            "session",
            "path",
            "sandbox",
            "provider",
            "digest",
            "/mnt/",
        ):
            self.assertNotIn(forbidden, summary.lower())

    def test_failure_waits_for_all_cleanups_adds_no_replacement_and_is_sanitized(self) -> None:
        settings = Mock(spec=SandboxSettings)
        lock = threading.Lock()
        assigned = 0
        cleanup: list[int] = []

        def runner(_settings, *, active_count_observer):
            nonlocal assigned
            with lock:
                assigned += 1
                number = assigned
            try:
                if number == 2:
                    raise RuntimeError("quota-secret-provider-body")
                return _VersionDelta(1, 1)
            finally:
                active_count_observer(_settings, "c" * 32)
                with lock:
                    cleanup.append(number)

        outcomes = self._thread_group(settings, runner, lambda _s, _i: 0)
        self.assertEqual(assigned, 3)
        self.assertCountEqual(cleanup, [1, 2, 3])
        with self.assertRaises(_ConcurrentRunFailed) as caught:
            _run_concurrent_remote_canary(
                settings,
                attempt_group_runner=lambda _settings: outcomes,
                clock_ns=iter((0, 20_000_000)).__next__,
                summary_sink=Mock(),
            )
        self.assertEqual(caught.exception.report.attempts, 3)
        self.assertEqual(caught.exception.report.failures, 1)
        self.assertEqual(caught.exception.report.active_zero, 3)
        self.assertNotIn("quota-secret", str(caught.exception))
        self.assertIn("capacity_limit_inferred=0", str(caught.exception))

    def test_report_rejects_bool_bounds_and_inconsistent_deltas(self) -> None:
        valid: dict[str, object] = {
            "concurrency": 3,
            "attempts": 3,
            "successes": 3,
            "failures": 0,
            "active_zero": 3,
            "object_delta": 3,
            "generation_delta": 3,
            "delta_complete": 1,
            "attempt_milliseconds": (1, 2, 3),
            "total_milliseconds": 4,
            "min_milliseconds": 1,
            "max_milliseconds": 3,
        }
        invalid = (
            ("concurrency", 4),
            ("attempts", True),
            ("successes", 2),
            ("active_zero", 2),
            ("object_delta", 2),
            ("delta_complete", 0),
            ("attempt_milliseconds", (1, True, 3)),
            ("total_milliseconds", 2),
        )
        for field, value in invalid:
            with self.subTest(field=field), self.assertRaises(_ConcurrentHarnessError):
                _ConcurrentReport(**{**valid, field: value})  # type: ignore[arg-type]

    def test_wire_payload_rejects_text_and_never_carries_coordinates(self) -> None:
        outcome = _AttemptOutcome(1, 1, 1, 1, 1, 10)
        self.assertEqual(_AttemptOutcome.from_wire(outcome.to_wire()), outcome)
        for invalid in (
            (1, 1, 1, 1, 1, "provider text"),
            (1, 1, 1, 1, 1, 10, "/secret/path"),
            {"attempt": 1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                _ConcurrentHarnessError
            ):
                _AttemptOutcome.from_wire(invalid)


if __name__ == "__main__":
    unittest.main()
