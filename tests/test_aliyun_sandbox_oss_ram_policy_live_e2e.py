"""Strictly gated live gate for the OSS RAM Role prefix boundary.

The positive Sandbox mounts the configured OSS prefix and round-trips one
random marker.  The negative Sandbox keeps the same Bucket and RAM Role but
requests a normalized adjacent sibling prefix.  A successful sibling list or
write is a policy failure.  Provider values, identifiers, and exception text
never cross the harness error/report boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
import posixpath
import re
import secrets
import time
from typing import Any, Final
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.aliyun_sandbox_probe import _create_sandbox, build_oss_metadata
from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from tests.test_remote_mutation_runtime import _settings


_LIVE_GATE: Final[str] = "RUN_ALIYUN_SANDBOX_LIVE_OSS_RAM_POLICY_E2E"
_OSS_METADATA_KEY: Final[str] = "fc.sandbox.storage.oss"
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0
_ALLOWED_STAGES: Final[frozenset[str]] = frozenset(
    {
        "positive_create",
        "positive_roundtrip",
        "negative_metadata",
        "negative_create",
        "negative_probe",
        "control_create",
        "control_roundtrip",
        "cleanup",
        "active_zero",
    }
)


class _HarnessError(RuntimeError):
    """Fixed-text local failure that cannot reflect provider data."""


@dataclass(frozen=True, slots=True)
class _OssRamPolicyReport:
    positive_roundtrips: int
    sibling_denials: int
    active_zero: bool

    def __post_init__(self) -> None:
        if (
            type(self.positive_roundtrips) is not int
            or self.positive_roundtrips != 1
            or type(self.sibling_denials) is not int
            or self.sibling_denials != 1
            or type(self.active_zero) is not bool
            or not self.active_zero
        ):
            raise _HarnessError("OSS RAM policy live 计数无效。")


def _validated_token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ValueError("token 必须是 32 位小写十六进制文本。")
    return value


def _path_contains(parent: str, candidate: str) -> bool:
    return candidate == parent or candidate.startswith(parent.rstrip("/") + "/")


def _sibling_bucket_path(configured_path: str, token: str) -> str:
    """Return one normalized sibling that is neither ancestor nor descendant."""
    safe_token = _validated_token(token)
    if (
        not isinstance(configured_path, str)
        or not configured_path.startswith("/")
        or configured_path.startswith("//")
        or configured_path == "/"
        or "\\" in configured_path
        or posixpath.normpath(configured_path) != configured_path
    ):
        raise ValueError("configured bucketPath 必须是规范化的绝对路径。")
    parent = posixpath.dirname(configured_path)
    sibling = posixpath.join(parent, f".agnet-study-ram-deny-{safe_token}")
    if not sibling.startswith("/"):
        sibling = "/" + sibling
    if (
        sibling == "/"
        or posixpath.normpath(sibling) != sibling
        or _path_contains(configured_path, sibling)
        or _path_contains(sibling, configured_path)
    ):
        raise ValueError("无法构造隔离的 sibling bucketPath。")
    return sibling


def _owned_metadata(
    metadata: Mapping[str, str],
    *,
    session_id: str,
) -> dict[str, str]:
    safe_session = _validated_token(session_id)
    owned = dict(metadata)
    owned[SANDBOX_OWNER_METADATA_KEY] = SANDBOX_OWNER_METADATA_VALUE
    owned[SANDBOX_SESSION_METADATA_KEY] = safe_session
    return owned


def _sibling_oss_metadata(
    settings: SandboxSettings,
    *,
    session_id: str,
    sibling_token: str,
) -> dict[str, str]:
    """Rewrite only bucketPath while preserving the configured Bucket/Role."""
    if settings.oss_bucket_path is None:
        raise ValueError("缺少 bucketPath。")
    metadata = build_oss_metadata(settings)
    try:
        storage = json.loads(metadata[_OSS_METADATA_KEY])
        mount_points = storage["mountPoints"]
        if not isinstance(mount_points, list) or len(mount_points) != 1:
            raise ValueError
        mount_point = mount_points[0]
        if not isinstance(mount_point, dict):
            raise ValueError
        if mount_point.get("bucketPath") != settings.oss_bucket_path:
            raise ValueError
        mount_point["bucketPath"] = _sibling_bucket_path(
            settings.oss_bucket_path,
            sibling_token,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise _HarnessError("OSS RAM policy live metadata 无效。") from None
    metadata[_OSS_METADATA_KEY] = json.dumps(
        storage,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _owned_metadata(metadata, session_id=session_id)


def _provider_active_count(settings: SandboxSettings, session_id: str) -> int:
    expected_metadata = {
        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
        SANDBOX_SESSION_METADATA_KEY: _validated_token(session_id),
    }
    try:
        sandbox_ids = _scan_active(
            _E2BSandboxRecoveryProvider(settings),
            expected_metadata=expected_metadata,
            deadline=time.monotonic() + _ACTIVE_SCAN_TIMEOUT_SECONDS,
        )
    except Exception:
        raise _HarnessError("OSS RAM policy live active 计数失败。") from None
    return len(sandbox_ids)


def _safe_stage(stage: object) -> str:
    return stage if isinstance(stage, str) and stage in _ALLOWED_STAGES else "positive_create"


def _valid_sandbox(sandbox: Any) -> Any:
    sandbox_id = getattr(sandbox, "sandbox_id", None)
    files = getattr(sandbox, "files", None)
    if not isinstance(sandbox_id, str) or not sandbox_id or files is None:
        raise _HarnessError("OSS RAM policy live Sandbox 结果无效。")
    return sandbox


def _remove_exact(sandbox: Any, marker_path: str) -> None:
    if sandbox.files.remove(marker_path) is False:
        raise _HarnessError("OSS RAM policy live marker 清理失败。")


def _sibling_escape_code(*, list_succeeded: bool, write_succeeded: bool) -> str:
    """Return only one fixed classification; never include provider values."""
    if type(list_succeeded) is not bool or type(write_succeeded) is not bool:
        raise TypeError("sibling probe 结果必须是 bool。")
    if list_succeeded and write_succeeded:
        return "list_and_write"
    if list_succeeded:
        return "list_only"
    if write_succeeded:
        return "write_only"
    raise ValueError("sibling probe 未发生权限越界。")


def _run_live_oss_ram_policy(
    settings: SandboxSettings,
    session_id: str,
    marker_token: str,
    sibling_token: str,
    control_token: str,
    *,
    sandbox_factory: Callable[[SandboxSettings, dict[str, str]], Any] = _create_sandbox,
    cleaner: Callable[[SandboxSettings, str], object] = cleanup_session_sandboxes,
    active_counter: Callable[[SandboxSettings, str], int] = _provider_active_count,
) -> _OssRamPolicyReport:
    """Exercise the allowed prefix and reject one adjacent sibling prefix."""
    safe_session = _validated_token(session_id)
    safe_marker = _validated_token(marker_token)
    _validated_token(sibling_token)
    safe_control = _validated_token(control_token)
    if not isinstance(settings, SandboxSettings):
        raise TypeError("settings 必须是 SandboxSettings 实例。")

    positive_marker = posixpath.join(
        settings.oss_mount_dir,
        f".agnet-study-ram-allow-{safe_marker}.txt",
    )
    negative_marker = posixpath.join(
        settings.oss_mount_dir,
        f".agnet-study-ram-deny-{safe_marker}.txt",
    )
    control_marker = posixpath.join(
        settings.oss_mount_dir,
        f".agnet-study-ram-control-{safe_control}.txt",
    )
    marker_body = f"agnet-study oss ram policy {safe_marker}\n"
    control_body = f"agnet-study oss ram control {safe_control}\n"

    sandboxes: list[Any] = []
    positive: Any | None = None
    negative: Any | None = None
    control: Any | None = None
    positive_write_attempted = False
    positive_removed = False
    negative_write_attempted = False
    negative_write_succeeded = False
    negative_removed = False
    control_write_attempted = False
    control_removed = False
    positive_roundtrips = 0
    sibling_denials = 0
    stage = "positive_create"
    primary_error: BaseException | None = None
    cleanup_failed = False
    active_failed = False

    try:
        positive_metadata = _owned_metadata(
            build_oss_metadata(settings),
            session_id=safe_session,
        )
        positive = sandbox_factory(settings, positive_metadata)
        sandboxes.append(positive)
        positive = _valid_sandbox(positive)

        stage = "positive_roundtrip"
        positive_write_attempted = True
        positive.files.write(positive_marker, marker_body)
        observed = positive.files.read(positive_marker)
        if not isinstance(observed, str) or not secrets.compare_digest(
            observed,
            marker_body,
        ):
            raise _HarnessError("OSS RAM policy live 正向回读不一致。")
        _remove_exact(positive, positive_marker)
        positive_removed = True
        positive_roundtrips = 1

        stage = "negative_metadata"
        negative_metadata = _sibling_oss_metadata(
            settings,
            session_id=safe_session,
            sibling_token=sibling_token,
        )

        stage = "negative_create"
        try:
            negative = sandbox_factory(settings, negative_metadata)
        except Exception:
            # A negative create failure is ambiguous by itself: it may be a
            # transient provider/control-plane outage rather than RAM denial.
            # Re-prove the configured prefix through a fresh Sandbox before
            # accepting the failure as a policy rejection.
            stage = "control_create"
            control = sandbox_factory(settings, positive_metadata)
            sandboxes.append(control)
            control = _valid_sandbox(control)
            stage = "control_roundtrip"
            control_write_attempted = True
            control.files.write(control_marker, control_body)
            control_observed = control.files.read(control_marker)
            if not isinstance(control_observed, str) or not secrets.compare_digest(
                control_observed,
                control_body,
            ):
                raise _HarnessError("OSS RAM policy live control 回读不一致。")
            _remove_exact(control, control_marker)
            control_removed = True
            sibling_denials = 1
        else:
            sandboxes.append(negative)
            negative = _valid_sandbox(negative)
            stage = "negative_probe"
            list_succeeded = False
            write_succeeded = False
            try:
                negative.files.list(settings.oss_mount_dir)
            except Exception:
                pass
            else:
                list_succeeded = True

            negative_write_attempted = True
            try:
                negative.files.write(negative_marker, marker_body)
            except Exception:
                pass
            else:
                write_succeeded = True
                negative_write_succeeded = True

            if list_succeeded or write_succeeded:
                escape_code = _sibling_escape_code(
                    list_succeeded=list_succeeded,
                    write_succeeded=write_succeeded,
                )
                raise _HarnessError(
                    "OSS RAM policy live sibling 权限越界 "
                    f"code={escape_code}。"
                )
            sibling_denials = 1
    except BaseException as error:
        if isinstance(error, _HarnessError) or not isinstance(error, Exception):
            primary_error = error
        else:
            primary_error = _HarnessError(
                "OSS RAM policy live 安全失败 "
                f"stage={_safe_stage(stage)}。"
            )
    finally:
        stage = "cleanup"
        if positive is not None and positive_write_attempted and not positive_removed:
            try:
                _remove_exact(positive, positive_marker)
                positive_removed = True
            except BaseException:
                cleanup_failed = True
        if negative is not None and negative_write_attempted:
            try:
                _remove_exact(negative, negative_marker)
                negative_removed = True
            except BaseException:
                if negative_write_succeeded:
                    cleanup_failed = True
        if control is not None and control_write_attempted and not control_removed:
            try:
                _remove_exact(control, control_marker)
                control_removed = True
            except BaseException:
                cleanup_failed = True

        seen: set[int] = set()
        for sandbox in reversed(sandboxes):
            identity = id(sandbox)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                sandbox.kill()
            except BaseException:
                # The exact-session cleaner below is the authoritative fallback.
                pass
        try:
            cleaner(settings, safe_session)
        except BaseException:
            cleanup_failed = True

        stage = "active_zero"
        try:
            active_failed = active_counter(settings, safe_session) != 0
        except BaseException:
            active_failed = True

    if negative_write_succeeded and not negative_removed:
        cleanup_failed = True
    if primary_error is not None:
        if cleanup_failed or active_failed:
            primary_error.add_note(
                "OSS RAM policy live 最终 marker 清理或 active=0 未确认。"
            )
        raise primary_error
    if (
        positive_roundtrips != 1
        or sibling_denials != 1
        or cleanup_failed
        or active_failed
    ):
        raise _HarnessError(
            "OSS RAM policy live 最终 marker 清理或 active=0 未确认。"
        )
    return _OssRamPolicyReport(1, 1, True)


def _execute_live_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[
        [SandboxSettings, str, str, str, str],
        _OssRamPolicyReport,
    ] = _run_live_oss_ram_policy,
) -> _OssRamPolicyReport:
    selected = os.environ if environ is None else environ
    if selected.get(_LIVE_GATE) != "1":
        raise _HarnessError("OSS RAM policy live gate 未启用。")
    try:
        settings = settings_loader()
        settings.require_enabled()
        settings.require_recovery_enabled()
    except Exception:
        raise _HarnessError("OSS RAM policy live 配置验证失败。") from None
    return runner(
        settings,
        uuid4().hex,
        uuid4().hex,
        uuid4().hex,
        uuid4().hex,
    )


@unittest.skipUnless(
    os.environ.get(_LIVE_GATE) == "1",
    f"Set {_LIVE_GATE}=1 to run the real OSS RAM policy E2E",
)
class AliyunSandboxOssRamPolicyLiveE2ETests(unittest.TestCase):
    def test_real_oss_ram_role_is_limited_to_the_configured_prefix(self) -> None:
        report = _execute_live_e2e()
        self.assertEqual(report, _OssRamPolicyReport(1, 1, True))
        print(
            "oss_ram_policy_live_ok "
            f"positive_roundtrips={report.positive_roundtrips} "
            f"sibling_denials={report.sibling_denials} "
            f"active_zero={int(report.active_zero)}"
        )


class _FakeFiles:
    def __init__(
        self,
        *,
        list_succeeds: bool = False,
        write_succeeds: bool = True,
    ) -> None:
        self.list_succeeds = list_succeeds
        self.write_succeeds = write_succeeds
        self.values: dict[str, str] = {}
        self.removed: list[str] = []

    def list(self, _path: str) -> list[object]:
        if not self.list_succeeds:
            raise PermissionError("provider-list-secret")
        return []

    def write(self, path: str, body: str) -> None:
        if not self.write_succeeds:
            raise PermissionError("provider-write-secret")
        self.values[path] = body

    def read(self, path: str) -> str:
        return self.values[path]

    def remove(self, path: str) -> None:
        self.removed.append(path)
        self.values.pop(path, None)


class _FakeSandbox:
    def __init__(
        self,
        sandbox_id: str,
        files: _FakeFiles,
        events: list[str],
        *,
        kill_raises: bool = False,
        kill_returns_false: bool = False,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.files = files
        self._events = events
        self._kill_raises = kill_raises
        self._kill_returns_false = kill_returns_false

    def kill(self) -> bool:
        self._events.append("kill")
        if self._kill_raises:
            raise OSError("provider-kill-secret")
        return not self._kill_returns_false


class OssRamPolicyHarnessOfflineTests(unittest.TestCase):
    def test_gate_precedes_settings_sdk_cleanup_and_provider(self) -> None:
        loader = Mock(side_effect=AssertionError("settings"))
        runner = Mock(side_effect=AssertionError("runner"))
        with (
            patch(__name__ + "._create_sandbox", side_effect=AssertionError("sdk")) as sdk,
            patch(
                __name__ + ".cleanup_session_sandboxes",
                side_effect=AssertionError("cleanup"),
            ) as cleanup,
            patch(
                __name__ + "._provider_active_count",
                side_effect=AssertionError("provider"),
            ) as provider,
            self.assertRaisesRegex(_HarnessError, "live gate"),
        ):
            _execute_live_e2e(environ={}, settings_loader=loader, runner=runner)
        for boundary in (loader, runner, sdk, cleanup, provider):
            boundary.assert_not_called()

    def test_default_live_suite_is_skipped_and_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the OSS RAM policy gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxOssRamPolicyLiveE2ETests
        )
        result = unittest.TestResult()
        with (
            patch(
                __name__ + ".load_sandbox_settings",
                side_effect=AssertionError("settings"),
            ) as loader,
            patch(
                __name__ + "._create_sandbox",
                side_effect=AssertionError("sdk"),
            ) as sdk,
        ):
            suite.run(result)
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.errors)
        self.assertFalse(result.failures)
        loader.assert_not_called()
        sdk.assert_not_called()

    def test_sibling_metadata_changes_only_normalized_bucket_path(self) -> None:
        settings = _settings()
        original = build_oss_metadata(settings)
        rewritten = _sibling_oss_metadata(
            settings,
            session_id="a" * 32,
            sibling_token="b" * 32,
        )
        self.assertEqual(
            rewritten["fc.sandbox.auth.role"],
            original["fc.sandbox.auth.role"],
        )
        original_storage = json.loads(original[_OSS_METADATA_KEY])
        rewritten_storage = json.loads(rewritten[_OSS_METADATA_KEY])
        original_point = original_storage["mountPoints"][0]
        rewritten_point = rewritten_storage["mountPoints"][0]
        sibling = rewritten_point["bucketPath"]
        self.assertEqual(
            {key: value for key, value in rewritten_point.items() if key != "bucketPath"},
            {key: value for key, value in original_point.items() if key != "bucketPath"},
        )
        self.assertTrue(sibling.startswith("/"))
        self.assertEqual(posixpath.normpath(sibling), sibling)
        self.assertEqual(posixpath.dirname(sibling), posixpath.dirname(settings.oss_bucket_path))
        self.assertFalse(_path_contains(settings.oss_bucket_path, sibling))
        self.assertFalse(_path_contains(sibling, settings.oss_bucket_path))
        self.assertEqual(
            rewritten[SANDBOX_OWNER_METADATA_KEY],
            SANDBOX_OWNER_METADATA_VALUE,
        )
        self.assertEqual(rewritten[SANDBOX_SESSION_METADATA_KEY], "a" * 32)

    def test_report_is_frozen_exact_and_contains_no_provider_fields(self) -> None:
        report = _OssRamPolicyReport(1, 1, True)
        self.assertEqual(
            set(report.__dataclass_fields__),
            {"positive_roundtrips", "sibling_denials", "active_zero"},
        )
        with self.assertRaises((AttributeError, TypeError)):
            report.sibling_denials = 0  # type: ignore[misc]
        for values in ((0, 1, True), (1, 0, True), (1, 1, False), (True, 1, True)):
            with self.subTest(values=values), self.assertRaises(_HarnessError):
                _OssRamPolicyReport(*values)  # type: ignore[arg-type]

    def _run_with_fakes(
        self,
        *,
        negative_list_succeeds: bool = False,
        negative_write_succeeds: bool = False,
        negative_create_denied: bool = False,
        control_create_fails: bool = False,
        kill_raises: bool = False,
        kill_returns_false: bool = False,
    ) -> tuple[_OssRamPolicyReport, list[str], _FakeFiles, _FakeFiles]:
        settings = _settings()
        events: list[str] = []
        positive_files = _FakeFiles()
        negative_files = _FakeFiles(
            list_succeeds=negative_list_succeeds,
            write_succeeds=negative_write_succeeds,
        )
        positive_creates = 0

        def factory(_settings_value: SandboxSettings, metadata: dict[str, str]):
            nonlocal positive_creates
            self.assertEqual(
                metadata[SANDBOX_OWNER_METADATA_KEY],
                SANDBOX_OWNER_METADATA_VALUE,
            )
            self.assertEqual(metadata[SANDBOX_SESSION_METADATA_KEY], "c" * 32)
            storage = json.loads(metadata[_OSS_METADATA_KEY])
            path = storage["mountPoints"][0]["bucketPath"]
            if path == settings.oss_bucket_path:
                positive_creates += 1
                events.append("positive_create")
                if control_create_fails and positive_creates > 1:
                    raise OSError("provider-control-secret")
                return _FakeSandbox(
                    "positive-secret-id",
                    positive_files,
                    events,
                    kill_raises=kill_raises,
                    kill_returns_false=kill_returns_false,
                )
            events.append("negative_create")
            if negative_create_denied:
                raise PermissionError("provider-create-secret")
            return _FakeSandbox(
                "negative-secret-id",
                negative_files,
                events,
                kill_raises=kill_raises,
                kill_returns_false=kill_returns_false,
            )

        def cleaner(selected: SandboxSettings, session_id: str) -> None:
            self.assertIs(selected, settings)
            self.assertEqual(session_id, "c" * 32)
            events.append("clean")

        def active(selected: SandboxSettings, session_id: str) -> int:
            self.assertIs(selected, settings)
            self.assertEqual(session_id, "c" * 32)
            events.append("active")
            return 0

        report = _run_live_oss_ram_policy(
            settings,
            "c" * 32,
            "d" * 32,
            "e" * 32,
            "f" * 32,
            sandbox_factory=factory,
            cleaner=cleaner,
            active_counter=active,
        )
        return report, events, positive_files, negative_files

    def test_negative_list_and_write_denials_pass_and_every_sandbox_is_killed(self) -> None:
        report, events, positive_files, negative_files = self._run_with_fakes()
        self.assertEqual(report, _OssRamPolicyReport(1, 1, True))
        self.assertEqual(events.count("kill"), 2)
        self.assertEqual(events[-2:], ["clean", "active"])
        self.assertEqual(len(positive_files.removed), 1)
        self.assertEqual(len(negative_files.removed), 1)
        self.assertRegex(positive_files.removed[0], r"ram-allow-[0-9a-f]{32}\.txt\Z")
        self.assertRegex(negative_files.removed[0], r"ram-deny-[0-9a-f]{32}\.txt\Z")

    def test_negative_mount_denial_requires_fresh_control_roundtrip(self) -> None:
        report, events, positive_files, negative_files = self._run_with_fakes(
            negative_create_denied=True
        )
        self.assertEqual(report, _OssRamPolicyReport(1, 1, True))
        self.assertEqual(events.count("positive_create"), 2)
        self.assertEqual(events.count("kill"), 2)
        self.assertEqual(len(positive_files.removed), 2)
        self.assertRegex(positive_files.removed[1], r"ram-control-[0-9a-f]{32}\.txt\Z")
        self.assertEqual(negative_files.removed, [])
        self.assertEqual(events[-2:], ["clean", "active"])

    def test_negative_create_failure_is_inconclusive_when_fresh_control_fails(self) -> None:
        with self.assertRaisesRegex(_HarnessError, r"stage=control_create") as caught:
            self._run_with_fakes(
                negative_create_denied=True,
                control_create_fails=True,
            )
        rendered = str(caught.exception)
        self.assertNotIn("provider", rendered.lower())
        self.assertNotIn("secret", rendered.lower())

    def test_direct_kill_failures_still_attempt_both_and_use_exact_session_cleaner(self) -> None:
        for options in ({"kill_raises": True}, {"kill_returns_false": True}):
            with self.subTest(options=options):
                report, events, _positive_files, _negative_files = self._run_with_fakes(
                    **options
                )
                self.assertEqual(report, _OssRamPolicyReport(1, 1, True))
                self.assertEqual(events.count("kill"), 2)
                self.assertEqual(events[-2:], ["clean", "active"])

    def test_unexpected_list_success_is_classified_without_provider_text(self) -> None:
        with self.assertRaisesRegex(
            _HarnessError,
            r"sibling 权限越界 code=list_only",
        ) as caught:
            self._run_with_fakes(negative_list_succeeds=True)
        rendered = str(caught.exception)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("bucket", rendered.lower())

    def test_unexpected_write_success_is_classified_and_removes_exact_marker(self) -> None:
        settings = _settings()
        events: list[str] = []
        positive_files = _FakeFiles()
        negative_files = _FakeFiles(write_succeeds=True)

        def factory(_settings_value: SandboxSettings, metadata: dict[str, str]):
            storage = json.loads(metadata[_OSS_METADATA_KEY])
            path = storage["mountPoints"][0]["bucketPath"]
            files = positive_files if path == settings.oss_bucket_path else negative_files
            return _FakeSandbox("provider-secret-id", files, events)

        with self.assertRaisesRegex(
            _HarnessError,
            r"sibling 权限越界 code=write_only",
        ):
            _run_live_oss_ram_policy(
                settings,
                "1" * 32,
                "2" * 32,
                "3" * 32,
                "4" * 32,
                sandbox_factory=factory,
                cleaner=lambda _settings_value, _session: events.append("clean"),
                active_counter=lambda _settings_value, _session: 0,
            )
        expected = posixpath.join(
            settings.oss_mount_dir,
            ".agnet-study-ram-deny-" + "2" * 32 + ".txt",
        )
        self.assertEqual(negative_files.removed, [expected])
        self.assertEqual(negative_files.values, {})
        self.assertEqual(events.count("kill"), 2)
        self.assertIn("clean", events)

    def test_unexpected_list_and_write_successes_have_combined_classification(self) -> None:
        with self.assertRaisesRegex(
            _HarnessError,
            r"sibling 权限越界 code=list_and_write",
        ) as caught:
            self._run_with_fakes(
                negative_list_succeeds=True,
                negative_write_succeeds=True,
            )
        rendered = str(caught.exception)
        self.assertNotIn("secret", rendered.lower())
        self.assertNotIn("bucket", rendered.lower())

    def test_positive_provider_failure_is_sanitized_and_cleanup_still_runs(self) -> None:
        settings = _settings()
        events: list[str] = []

        def fail_factory(_settings_value: SandboxSettings, _metadata: dict[str, str]):
            raise OSError("provider-role-bucket-path-sandbox-id-secret")

        with self.assertRaisesRegex(
            _HarnessError,
            r"stage=positive_create",
        ) as caught:
            _run_live_oss_ram_policy(
                settings,
                "4" * 32,
                "5" * 32,
                "6" * 32,
                "7" * 32,
                sandbox_factory=fail_factory,
                cleaner=lambda _settings_value, _session: events.append("clean"),
                active_counter=lambda _settings_value, _session: events.append("active") or 0,
            )
        rendered = str(caught.exception)
        for secret in ("provider", "role", "bucket", "path", "sandbox-id", "secret"):
            self.assertNotIn(secret, rendered.lower())
        self.assertEqual(events, ["clean", "active"])


if __name__ == "__main__":
    unittest.main()
