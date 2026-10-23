"""Strictly gated live takeover of a stale Sandbox across OS processes.

No completion client is constructed, so this harness cannot contact DeepSeek.
The worker protocol contains only fixed tokens; it never returns provider
output, credentials, Sandbox identifiers, or OSS paths to the test process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Final
import unittest
import uuid
from unittest.mock import Mock, patch

from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from tests._aliyun_sandbox_cross_process_worker import (
    CLEANUP_OK_TOKEN,
    CLEANUP_STAGE,
    CREATE_OK_TOKEN,
    CREATE_STAGE,
    FAILURE_TOKEN,
    LIVE_GATE,
    SESSION_ENV,
    STAGE_ENV,
)


_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_WORKER: Final[Path] = _ROOT / "tests" / "_aliyun_sandbox_cross_process_worker.py"
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_WORKER_TIMEOUT_SECONDS: Final[float] = 180.0
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0
_ACTIVE_VISIBILITY_TIMEOUT_SECONDS: Final[float] = 45.0
_ACTIVE_VISIBILITY_POLL_SECONDS: Final[float] = 1.0


class _HarnessError(RuntimeError):
    """Fixed-text harness failure that contains no child/provider output."""


@dataclass(frozen=True, slots=True)
class _WorkerResult:
    pid: int
    stage: str


def _provider_active_count(settings: SandboxSettings, session_id: str) -> int:
    """Return only the count for one exact session; never expose provider IDs."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id 必须是 32 位小写十六进制文本。")
    settings.require_enabled()
    settings.require_recovery_enabled()
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
        raise _HarnessError("跨进程 live active 计数失败。") from None
    return len(sandbox_ids)


def _wait_for_exact_stale(
    settings: SandboxSettings,
    session_id: str,
    *,
    active_count_observer: Callable[[SandboxSettings, str], int] = (
        _provider_active_count
    ),
    timeout_seconds: float = _ACTIVE_VISIBILITY_TIMEOUT_SECONDS,
    poll_seconds: float = _ACTIVE_VISIBILITY_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait only for provider indexing to converge from zero to exactly one."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 60
    ):
        raise ValueError("timeout_seconds 必须在 (0, 60]。")
    if (
        not isinstance(poll_seconds, (int, float))
        or isinstance(poll_seconds, bool)
        or not 0 < poll_seconds <= 5
    ):
        raise ValueError("poll_seconds 必须在 (0, 5]。")

    deadline = monotonic() + float(timeout_seconds)
    while True:
        count = active_count_observer(settings, session_id)
        if type(count) is not int or count < 0:
            raise _HarnessError("进程 A 退出后的 active 计数无效。")
        if count == 1:
            return
        if count > 1:
            raise _HarnessError(
                "进程 A 退出后的 stale active 数量超过安全上限。"
            )
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _HarnessError(
                "进程 A 退出后的 stale active 在时限内不可见。"
            )
        sleeper(min(float(poll_seconds), remaining))


def _run_worker(
    stage: str,
    session_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> _WorkerResult:
    selected_env = dict(os.environ if environ is None else environ)
    if selected_env.get(LIVE_GATE) != "1":
        raise _HarnessError("跨进程 live gate 未启用。")
    if stage not in {CREATE_STAGE, CLEANUP_STAGE}:
        raise ValueError("stage 必须是固定 worker 操作。")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id 必须是 32 位小写十六进制文本。")

    child_env = selected_env.copy()
    child_env[STAGE_ENV] = stage
    child_env[SESSION_ENV] = session_id
    expected_token = CREATE_OK_TOKEN if stage == CREATE_STAGE else CLEANUP_OK_TOKEN
    process = popen_factory(
        [sys.executable, str(_WORKER)],
        cwd=str(_ROOT),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    try:
        stdout, stderr = process.communicate(timeout=_WORKER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise _HarnessError("跨进程 live worker 超时。") from None
    except BaseException:
        try:
            process.kill()
            process.communicate()
        except BaseException:
            pass
        raise

    if process.returncode != 0:
        # Never include captured output: even an unexpected interpreter or SDK
        # failure must remain behind this fixed safe boundary.
        raise _HarnessError("跨进程 live worker 执行失败。")
    if stdout != expected_token + "\n" or stderr != "":
        raise _HarnessError("跨进程 live worker 返回了非固定协议。")
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or pid <= 0:
        raise _HarnessError("跨进程 live worker 缺少进程身份。")
    return _WorkerResult(pid=pid, stage=stage)


def _run_takeover(
    settings: SandboxSettings,
    session_id: str,
    *,
    worker_runner: Callable[[str, str], _WorkerResult] = _run_worker,
    stale_waiter: Callable[[SandboxSettings, str], None] | None = None,
    active_count_observer: Callable[[SandboxSettings, str], int] = (
        _provider_active_count
    ),
    emergency_cleanup: Callable[[SandboxSettings, str], None] = (
        cleanup_session_sandboxes
    ),
) -> tuple[_WorkerResult, _WorkerResult]:
    primary_error: BaseException | None = None
    result: tuple[_WorkerResult, _WorkerResult] | None = None
    cleanup_error = False
    try:
        creator = worker_runner(CREATE_STAGE, session_id)
        if stale_waiter is None:
            _wait_for_exact_stale(
                settings,
                session_id,
                active_count_observer=active_count_observer,
            )
        else:
            stale_waiter(settings, session_id)
        cleaner = worker_runner(CLEANUP_STAGE, session_id)
        after_cleaner = active_count_observer(settings, session_id)
        if type(after_cleaner) is not int or after_cleaner != 0:
            raise _HarnessError(
                "进程 B 返回后的 active=0 未得到外部确认。"
            )
        if creator.pid == cleaner.pid:
            raise _HarnessError("创建与接管必须由不同 OS 进程完成。")
        result = (creator, cleaner)
    except BaseException as error:
        primary_error = error
    finally:
        # Run even when process A times out or exits ambiguously.  The target is
        # still only this newly generated session and the product cleaner must
        # itself prove the final active set is empty.
        try:
            emergency_cleanup(settings, session_id)
        except BaseException:
            cleanup_error = True

    if primary_error is not None:
        if cleanup_error:
            primary_error.add_note("跨进程 live 的 emergency cleanup 未确认完成。")
        raise primary_error
    if cleanup_error:
        raise _HarnessError("跨进程 live 的 emergency cleanup 未确认完成。")
    if result is None:
        raise _HarnessError("跨进程 live 未产生结果。")
    return result


def _execute_live_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] | None = None,
    takeover_runner: Callable[
        [SandboxSettings, str], tuple[_WorkerResult, _WorkerResult]
    ]
    | None = None,
) -> tuple[_WorkerResult, _WorkerResult]:
    """Gate the entire live suite before settings or cleanup dependencies run."""
    selected_env = os.environ if environ is None else environ
    if selected_env.get(LIVE_GATE) != "1":
        raise _HarnessError("跨进程 live gate 未启用。")
    loader = load_sandbox_settings if settings_loader is None else settings_loader
    runner = _run_takeover if takeover_runner is None else takeover_runner
    settings = loader()
    settings.require_enabled()
    settings.require_recovery_enabled()
    return runner(settings, uuid.uuid4().hex)


@unittest.skipUnless(
    os.environ.get(LIVE_GATE) == "1",
    f"Set {LIVE_GATE}=1 to run the real cross-process stale takeover E2E",
)
class AliyunSandboxCrossProcessStaleLiveE2ETests(unittest.TestCase):
    def test_stale_sandbox_is_taken_over_by_a_new_os_process(self) -> None:
        creator, cleaner = _execute_live_e2e()

        self.assertEqual(creator.stage, CREATE_STAGE)
        self.assertEqual(cleaner.stage, CLEANUP_STAGE)
        self.assertNotEqual(creator.pid, cleaner.pid)


class CrossProcessStaleHarnessOfflineTests(unittest.TestCase):
    def test_worker_gate_is_closed_before_any_child_process(self) -> None:
        popen = Mock(side_effect=AssertionError("must not start a worker"))

        with self.assertRaisesRegex(_HarnessError, "live gate"):
            _run_worker(
                CREATE_STAGE,
                "a" * 32,
                environ={},
                popen_factory=popen,
            )

        popen.assert_not_called()

    def test_default_suite_gate_precedes_settings_cleanup_and_popen(self) -> None:
        with (
            patch(
                __name__ + ".load_sandbox_settings",
                side_effect=AssertionError("must not load settings"),
            ) as settings_loader,
            patch(
                __name__ + ".cleanup_session_sandboxes",
                side_effect=AssertionError("must not cleanup"),
            ) as cleanup,
            patch(
                __name__ + ".subprocess.Popen",
                side_effect=AssertionError("must not start a worker"),
            ) as popen,
        ):
            with self.assertRaisesRegex(_HarnessError, "live gate"):
                _execute_live_e2e(environ={})

        settings_loader.assert_not_called()
        cleanup.assert_not_called()
        popen.assert_not_called()

    def test_worker_without_gate_uses_only_fixed_failure_token(self) -> None:
        child_env = os.environ.copy()
        for name in (
            LIVE_GATE,
            "ALIYUN_SANDBOX_PROBE_ENABLED",
            "ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED",
        ):
            child_env.pop(name, None)
        completed = subprocess.run(
            [sys.executable, str(_WORKER)],
            cwd=str(_ROOT),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, FAILURE_TOKEN + "\n")
        self.assertEqual(completed.stderr, "")

    def test_orchestrator_orders_processes_and_always_emergency_cleans(self) -> None:
        settings = Mock(spec=SandboxSettings)
        calls: list[tuple[str, str]] = []

        def run(stage: str, session_id: str) -> _WorkerResult:
            calls.append(("worker:" + stage, session_id))
            return _WorkerResult(
                pid=101 if stage == CREATE_STAGE else 202,
                stage=stage,
            )

        counts = iter((1, 0))

        def observe(_settings: SandboxSettings, session_id: str) -> int:
            calls.append(("observe", session_id))
            return next(counts)

        def cleanup(_settings: SandboxSettings, session_id: str) -> None:
            calls.append(("emergency", session_id))

        result = _run_takeover(
            settings,
            "b" * 32,
            worker_runner=run,
            active_count_observer=observe,
            emergency_cleanup=cleanup,
        )

        self.assertEqual(
            calls,
            [
                ("worker:" + CREATE_STAGE, "b" * 32),
                ("observe", "b" * 32),
                ("worker:" + CLEANUP_STAGE, "b" * 32),
                ("observe", "b" * 32),
                ("emergency", "b" * 32),
            ],
        )
        self.assertEqual([item.pid for item in result], [101, 202])

    def test_orchestrator_emergency_cleans_after_ambiguous_create(self) -> None:
        settings = Mock(spec=SandboxSettings)
        cleanup = Mock()

        def fail(_stage: str, _session_id: str) -> _WorkerResult:
            raise _HarnessError("跨进程 live worker 执行失败。")

        with self.assertRaisesRegex(_HarnessError, "worker 执行失败"):
            _run_takeover(
                settings,
                "c" * 32,
                worker_runner=fail,
                active_count_observer=Mock(
                    side_effect=AssertionError("must not observe")
                ),
                emergency_cleanup=cleanup,
            )

        cleanup.assert_called_once_with(settings, "c" * 32)

    def test_external_count_one_is_required_before_process_b(self) -> None:
        settings = Mock(spec=SandboxSettings)
        worker = Mock(
            return_value=_WorkerResult(pid=404, stage=CREATE_STAGE)
        )
        cleanup = Mock()
        waiter = Mock(
            side_effect=_HarnessError(
                "进程 A 退出后的 stale active 在时限内不可见。"
            )
        )

        with self.assertRaisesRegex(_HarnessError, "stale active"):
            _run_takeover(
                settings,
                "e" * 32,
                worker_runner=worker,
                stale_waiter=waiter,
                active_count_observer=Mock(
                    side_effect=AssertionError("process B must not run")
                ),
                emergency_cleanup=cleanup,
            )

        worker.assert_called_once_with(CREATE_STAGE, "e" * 32)
        waiter.assert_called_once_with(settings, "e" * 32)
        cleanup.assert_called_once_with(settings, "e" * 32)

    def test_stale_waiter_allows_bounded_zero_to_one_convergence(self) -> None:
        settings = Mock(spec=SandboxSettings)
        counts = iter((0, 0, 1))
        now = [10.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        _wait_for_exact_stale(
            settings,
            "f" * 32,
            active_count_observer=lambda _settings, _session: next(counts),
            timeout_seconds=5,
            poll_seconds=1,
            monotonic=lambda: now[0],
            sleeper=sleep,
        )

        self.assertEqual(sleeps, [1.0, 1.0])

    def test_stale_waiter_times_out_on_persistent_zero(self) -> None:
        settings = Mock(spec=SandboxSettings)
        now = [20.0]
        observations: list[int] = []

        def observe(_settings: SandboxSettings, _session_id: str) -> int:
            observations.append(0)
            return 0

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with self.assertRaisesRegex(_HarnessError, "时限内不可见"):
            _wait_for_exact_stale(
                settings,
                "1" * 32,
                active_count_observer=observe,
                timeout_seconds=2,
                poll_seconds=1,
                monotonic=lambda: now[0],
                sleeper=sleep,
            )

        self.assertEqual(len(observations), 3)

    def test_stale_waiter_fails_closed_on_multiple_resources(self) -> None:
        settings = Mock(spec=SandboxSettings)
        sleeper = Mock(side_effect=AssertionError("must not retry split state"))

        with self.assertRaisesRegex(_HarnessError, "超过安全上限"):
            _wait_for_exact_stale(
                settings,
                "2" * 32,
                active_count_observer=Mock(return_value=2),
                timeout_seconds=5,
                poll_seconds=1,
                monotonic=Mock(return_value=0.0),
                sleeper=sleeper,
            )

        sleeper.assert_not_called()

    def test_non_fixed_child_output_is_never_reflected_in_error(self) -> None:
        secret = "provider-secret-and-sandbox-id"

        class FakeProcess:
            pid = 303
            returncode = 1

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                del timeout
                return secret, secret

            def kill(self) -> None:
                raise AssertionError("completed process must not be killed")

        with self.assertRaises(_HarnessError) as captured:
            _run_worker(
                CLEANUP_STAGE,
                "d" * 32,
                environ={LIVE_GATE: "1"},
                popen_factory=lambda *_args, **_kwargs: FakeProcess(),  # type: ignore[arg-type]
            )

        self.assertNotIn(secret, str(captured.exception))


if __name__ == "__main__":
    unittest.main()
