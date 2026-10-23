"""Strictly gated security-boundary E2E for Aliyun Sandbox.

This harness combines the three release hardenings that must hold in one real
Sandbox: disabled internet egress, runtime-user isolation from the writable OSS
mount, and fixed POSIX limits for structured command children.  It constructs
no model client and therefore cannot call DeepSeek.  Network probes use only
fixed numeric addresses, never resolve DNS, and never read or retain response
bytes.  A passing probe is supporting evidence for the fail-closed SDK create
setting, not a claim based on one external service being healthy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import ipaddress
import os
import posixpath
import re
import secrets
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Final
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app.aliyun_sandbox_probe import _create_sandbox
from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    _E2BSandboxRecoveryProvider,
    _scan_active,
    cleanup_session_sandboxes,
)
from app.execution_backend import AliyunSandboxExecutionBackend, ExecutionLease
from app.execution_runtime import ExecutionToolRuntime
from app.sandbox_config import SandboxSettings, load_sandbox_settings
from app.sandbox_transport import SandboxReadResult
from app.schemas import BashShellArguments, BashShellOutput, WriteFileArguments, WriteFileOutput
from app.workspace_checkpoint import create_workspace_checkpoint_initializer
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from app.workspace_versions import version_session_root
from tests.test_remote_mutation_runtime import _settings


_LIVE_GATE: Final[str] = "RUN_ALIYUN_SANDBOX_LIVE_SECURITY_BOUNDARIES_E2E"
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE_SCAN_TIMEOUT_SECONDS: Final[float] = 60.0
_V2_BODY: Final[bytes] = b"security-boundary-v2-ok\n"
_NETWORK_TARGETS: Final[tuple[tuple[str, int], ...]] = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 443),
    ("100.100.100.200", 80),
    ("169.254.169.254", 80),
)
_ALLOWED_STAGES: Final[frozenset[str]] = frozenset({
    "create",
    "persistence",
    "network",
    "resource_as",
    "resource_nproc",
    "resource_fsize",
    "v2_roundtrip",
    "close",
    "active_zero",
})


class _HarnessError(RuntimeError):
    """Fixed-text failure that cannot reflect provider or child output."""


@dataclass(frozen=True, slots=True)
class _SecurityBoundaryReport:
    persistence_unavailable: int
    network_denials: int
    resource_denials: int
    reuse_checks: int
    v2_roundtrips: int
    backend_closed: bool
    active_zero: bool

    def __post_init__(self) -> None:
        expected = (3, len(_NETWORK_TARGETS), 3, 3, 1)
        observed = (
            self.persistence_unavailable,
            self.network_denials,
            self.resource_denials,
            self.reuse_checks,
            self.v2_roundtrips,
        )
        if any(type(value) is not int for value in observed) or observed != expected:
            raise _HarnessError("security boundaries live 计数无效。")
        if (
            type(self.backend_closed) is not bool
            or type(self.active_zero) is not bool
            or not self.backend_closed
            or not self.active_zero
        ):
            raise _HarnessError("security boundaries live 终态无效。")


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _persistence_test_source(persistence_root: str) -> bytes:
    if (
        not isinstance(persistence_root, str)
        or not persistence_root.startswith("/mnt/")
        or posixpath.normpath(persistence_root) != persistence_root
        or "\\" in persistence_root
    ):
        raise _HarnessError("security boundaries live persistence 坐标无效。")
    source = f'''import errno
import os
import unittest


ROOT = {persistence_root!r}
UNAVAILABLE = {{errno.ENOENT, errno.EACCES, errno.EPERM}}


class PersistenceBoundaryProbe(unittest.TestCase):
    def _require_unavailable(self, action):
        try:
            action()
        except OSError as error:
            self.assertIn(error.errno, UNAVAILABLE)
        else:
            self.fail("persistence access unexpectedly succeeded")

    def test_mount_is_absent_and_list_read_create_are_unavailable(self):
        self.assertFalse(os.path.ismount(ROOT))
        self._require_unavailable(lambda: os.listdir(ROOT))
        self._require_unavailable(
            lambda: os.close(os.open(ROOT + "/.security-read-probe", os.O_RDONLY))
        )

        created = ROOT + "/.security-create-probe"
        descriptor = None
        try:
            descriptor = os.open(
                created,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except OSError as error:
            self.assertIn(error.errno, UNAVAILABLE)
        else:
            os.close(descriptor)
            descriptor = None
            try:
                os.unlink(created)
            finally:
                self.fail("persistence create unexpectedly succeeded")
        finally:
            if descriptor is not None:
                os.close(descriptor)
'''
    return source.encode("utf-8")


def _network_test_source() -> bytes:
    targets = repr(_NETWORK_TARGETS)
    source = f'''import socket
import unittest


TARGETS = {targets}


class NetworkBoundaryProbe(unittest.TestCase):
    def test_fixed_numeric_targets_are_unreachable(self):
        for address, port in TARGETS:
            with self.subTest(target=(address, port)):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.settimeout(0.75)
                    self.assertNotEqual(sock.connect_ex((address, port)), 0)
                finally:
                    sock.close()
'''
    return source.encode("utf-8")


def _as_test_source() -> bytes:
    return b'''import unittest


class AddressSpaceBoundaryProbe(unittest.TestCase):
    def test_allocation_above_fixed_limit_is_denied(self):
        try:
            bytearray(300 * 1024 * 1024)
        except MemoryError:
            return
        self.fail("address-space allocation unexpectedly succeeded")
'''


def _nproc_test_source() -> bytes:
    return b'''import errno
import subprocess
import sys
import unittest


class ProcessBoundaryProbe(unittest.TestCase):
    def test_process_count_above_fixed_limit_is_denied(self):
        children = []
        denied = False
        try:
            for _index in range(8):
                try:
                    child = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(5)"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as error:
                    denied = error.errno in {errno.EAGAIN, errno.ENOMEM}
                    break
                children.append(child)
        finally:
            for child in children:
                child.kill()
            for child in children:
                child.wait()
        self.assertTrue(denied)
'''


def _fsize_test_source() -> bytes:
    return b'''import errno
import os
import signal
import tempfile
import unittest


class FileSizeBoundaryProbe(unittest.TestCase):
    def test_file_above_fixed_limit_is_denied(self):
        path = os.path.join(tempfile.gettempdir(), ".security-fsize-" + str(os.getpid()))
        denied = False
        previous = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        try:
            with open(path, "xb", buffering=0) as stream:
                try:
                    for _index in range(17):
                        stream.write(b"x" * (1024 * 1024))
                except OSError as error:
                    denied = error.errno == errno.EFBIG
        finally:
            signal.signal(signal.SIGXFSZ, previous)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        self.assertTrue(denied)
'''


def _reuse_test_source() -> bytes:
    return b'''import unittest


class ReuseProbe(unittest.TestCase):
    def test_runtime_is_still_usable(self):
        self.assertEqual(6 * 7, 42)
'''


def _security_snapshot(persistence_root: str) -> WorkspaceSeedSnapshot:
    return WorkspaceSeedSnapshot((
        _entry("as_tests/__init__.py", b""),
        _entry("as_tests/test_as.py", _as_test_source()),
        _entry("fsize_tests/__init__.py", b""),
        _entry("fsize_tests/test_fsize.py", _fsize_test_source()),
        _entry("network_tests/__init__.py", b""),
        _entry("network_tests/test_network.py", _network_test_source()),
        _entry("nproc_tests/__init__.py", b""),
        _entry("nproc_tests/test_nproc.py", _nproc_test_source()),
        _entry("persistence_tests/__init__.py", b""),
        _entry("persistence_tests/test_persistence.py", _persistence_test_source(persistence_root)),
        _entry("reuse_tests/__init__.py", b""),
        _entry("reuse_tests/test_reuse.py", _reuse_test_source()),
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
        raise _HarnessError("security boundaries live active 计数失败。") from None
    return len(sandbox_ids)


def _run_unittest(runtime: ExecutionToolRuntime, package: str, failure: str) -> None:
    result = runtime.execute_authorized(
        "bash_shell",
        BashShellArguments(
            command="unittest",
            paths=[package],
            timeout_seconds=12,
        ),
        lambda: True,
    )
    if (
        not isinstance(result, BashShellOutput)
        or result.command != "unittest"
        or result.paths != [package]
        or result.exit_code != 0
        or result.truncated
    ):
        raise _HarnessError(failure)


def _require_reusable(
    runtime: ExecutionToolRuntime,
    same_runtime_check: Callable[[], None],
) -> None:
    _run_unittest(
        runtime,
        "reuse_tests",
        "security boundaries live 资源失败后 runtime 不可复用。",
    )
    same_runtime_check()


def _exercise_security_contract(
    runtime: ExecutionToolRuntime,
    *,
    same_runtime_check: Callable[[], None],
    stage_callback: Callable[[str], None] | None = None,
) -> tuple[int, int, int, int]:
    note_stage = stage_callback or (lambda _stage: None)
    note_stage("persistence")
    _run_unittest(
        runtime,
        "persistence_tests",
        "security boundaries live persistence 隔离失败。",
    )
    persistence_unavailable = 3

    note_stage("network")
    _run_unittest(
        runtime,
        "network_tests",
        "security boundaries live 网络出口隔离失败。",
    )
    network_denials = len(_NETWORK_TARGETS)

    reuse_checks = 0
    for stage, package, failure in (
        ("resource_as", "as_tests", "security boundaries live AS 上限失败。"),
        ("resource_nproc", "nproc_tests", "security boundaries live NPROC 上限失败。"),
        ("resource_fsize", "fsize_tests", "security boundaries live FSIZE 上限失败。"),
    ):
        note_stage(stage)
        _run_unittest(runtime, package, failure)
        _require_reusable(runtime, same_runtime_check)
        reuse_checks += 1
    return persistence_unavailable, network_denials, 3, reuse_checks


def _exercise_v2_roundtrip(
    runtime: ExecutionToolRuntime,
    backend: AliyunSandboxExecutionBackend,
    lease: ExecutionLease,
) -> int:
    if lease.persistence_root is None:
        raise _HarnessError("security boundaries live v2 persistence 缺失。")
    text = _V2_BODY.decode("ascii")
    prepared = runtime.prepare_remote_mutation(
        "write_file",
        WriteFileArguments(
            path="security_boundary_v2.txt",
            content=text,
            mode="create",
        ),
        operation_id=hashlib.sha256(b"security-boundary-v2-operation").hexdigest(),
        permission_mode="partial-control",
    )
    result = prepared.commit()
    digest = hashlib.sha256(_V2_BODY).hexdigest()
    if (
        not isinstance(result, WriteFileOutput)
        or result.operation != "created"
        or result.bytes_written != len(_V2_BODY)
        or result.sha256 != digest
    ):
        raise _HarnessError("security boundaries live v2 mutation 失败。")
    object_path = posixpath.join(
        version_session_root(lease.persistence_root, lease.session_id),
        "objects",
        digest,
    )
    observed = backend._with_transport(  # noqa: SLF001 - host-only live harness
        lambda transport: transport.workspace_version_read_file(
            object_path,
            offset=0,
            max_bytes=len(_V2_BODY),
            timeout_seconds=30,
        )
    )
    if (
        type(observed) is not SandboxReadResult
        or observed.data != _V2_BODY
        or observed.total_bytes != len(_V2_BODY)
        or not observed.data.islower()
    ):
        raise _HarnessError("security boundaries live v2 小写读回失败。")
    return 1


def _safe_stage(stage: object) -> str:
    return stage if isinstance(stage, str) and stage in _ALLOWED_STAGES else "create"


def _run_live_security_boundaries(
    settings: SandboxSettings,
    session_id: str,
    *,
    backend_factory: Callable[..., AliyunSandboxExecutionBackend] = AliyunSandboxExecutionBackend,
    runtime_factory: Callable[[AliyunSandboxExecutionBackend], ExecutionToolRuntime] = ExecutionToolRuntime,
    cleaner: Callable[[SandboxSettings, str], object] = cleanup_session_sandboxes,
    active_counter: Callable[[SandboxSettings, str], int] = _provider_active_count,
    exercise: Callable[..., tuple[int, int, int, int]] = _exercise_security_contract,
    v2_roundtrip: Callable[..., int] = _exercise_v2_roundtrip,
) -> _SecurityBoundaryReport:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise _HarnessError("security boundaries live session 身份无效。")
    if settings.allow_internet_access is not False:
        raise _HarnessError("security boundaries live 网络配置必须关闭。")

    backend: AliyunSandboxExecutionBackend | None = None
    runtime: ExecutionToolRuntime | None = None
    primary_error: BaseException | None = None
    stage = "create"
    counts: tuple[int, int, int, int] | None = None
    v2_count = 0
    close_failed = False
    cleanup_failed = False
    active_failed = False
    try:
        backend = backend_factory(
            session_id,
            settings,
            workspace_initializer=create_workspace_checkpoint_initializer(
                _security_snapshot(settings.oss_mount_dir)
            ),
        )
        runtime = runtime_factory(backend)
        lease = runtime.ensure_ready()
        sandbox_id = lease.sandbox_id
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise _HarnessError("security boundaries live 缺少安全 Sandbox 身份。")
        sandbox_digest = hashlib.sha256(sandbox_id.encode("utf-8")).digest()

        def same_runtime_check() -> None:
            current_id = runtime.ensure_ready().sandbox_id
            if (
                not isinstance(current_id, str)
                or not secrets.compare_digest(
                    hashlib.sha256(current_id.encode("utf-8")).digest(),
                    sandbox_digest,
                )
            ):
                raise _HarnessError("security boundaries live 创建了不同 Sandbox。")

        def _set_stage(value: str) -> None:
            nonlocal stage
            if value not in _ALLOWED_STAGES:
                raise _HarnessError("security boundaries live 阶段无效。")
            stage = value

        counts = exercise(
            runtime,
            same_runtime_check=same_runtime_check,
            stage_callback=_set_stage,
        )
        stage = "v2_roundtrip"
        v2_count = v2_roundtrip(runtime, backend, lease)
    except BaseException as error:
        if isinstance(error, _HarnessError) or not isinstance(error, Exception):
            primary_error = error
        else:
            primary_error = _HarnessError(
                "security boundaries live 安全失败 "
                f"stage={_safe_stage(stage)}。"
            )
    finally:
        stage = "close"
        if runtime is not None:
            try:
                runtime.close()
            except BaseException:
                close_failed = True
        elif backend is not None:
            try:
                backend.close()
            except BaseException:
                close_failed = True
        try:
            cleaner(settings, session_id)
        except BaseException:
            cleanup_failed = True
        stage = "active_zero"
        try:
            active_failed = active_counter(settings, session_id) != 0
        except BaseException:
            active_failed = True

    backend_closed = backend is not None and getattr(backend, "closed", None) is True
    if primary_error is not None:
        if close_failed or cleanup_failed or active_failed or not backend_closed:
            primary_error.add_note(
                "security boundaries live 的最终 active=0 或 backend close 未确认。"
            )
        raise primary_error
    if (
        counts is None
        or close_failed
        or cleanup_failed
        or active_failed
        or not backend_closed
    ):
        raise _HarnessError(
            "security boundaries live 的最终 active=0 或 backend close 未确认。"
        )
    return _SecurityBoundaryReport(*counts, v2_count, True, True)


def _execute_live_e2e(
    *,
    environ: Mapping[str, str] | None = None,
    settings_loader: Callable[[], SandboxSettings] = load_sandbox_settings,
    runner: Callable[[SandboxSettings, str], _SecurityBoundaryReport] = (
        _run_live_security_boundaries
    ),
) -> _SecurityBoundaryReport:
    selected = os.environ if environ is None else environ
    if selected.get(_LIVE_GATE) != "1":
        raise _HarnessError("security boundaries live gate 未启用。")
    try:
        settings = settings_loader()
        settings.require_enabled()
        settings.require_recovery_enabled()
        if settings.allow_internet_access is not False:
            raise ValueError
    except Exception:
        raise _HarnessError("security boundaries live 配置验证失败。") from None
    return runner(settings, uuid4().hex)


@unittest.skipUnless(
    os.environ.get(_LIVE_GATE) == "1",
    f"Set {_LIVE_GATE}=1 to run the real security-boundaries E2E",
)
class AliyunSandboxSecurityBoundariesLiveE2ETests(unittest.TestCase):
    def test_real_security_boundaries_are_enforced_and_cleaned(self) -> None:
        report = _execute_live_e2e()
        self.assertEqual(report, _SecurityBoundaryReport(3, 4, 3, 3, 1, True, True))
        print(
            "security_boundaries_live_ok "
            f"persistence_unavailable={report.persistence_unavailable} "
            f"network_denials={report.network_denials} "
            f"resource_denials={report.resource_denials} "
            f"reuse_checks={report.reuse_checks} "
            f"v2_roundtrips={report.v2_roundtrips} "
            f"backend_closed={int(report.backend_closed)} "
            f"active_zero={int(report.active_zero)}"
        )


class SecurityBoundariesHarnessOfflineTests(unittest.TestCase):
    def test_gate_precedes_settings_sdk_backend_cleanup_and_provider(self) -> None:
        loader = Mock(side_effect=AssertionError("settings"))
        runner = Mock(side_effect=AssertionError("runner"))
        with (
            patch(__name__ + ".AliyunSandboxExecutionBackend", side_effect=AssertionError("backend")) as backend,
            patch(__name__ + ".cleanup_session_sandboxes", side_effect=AssertionError("cleanup")) as cleanup,
            patch(__name__ + "._provider_active_count", side_effect=AssertionError("provider")) as provider,
            self.assertRaisesRegex(_HarnessError, "live gate"),
        ):
            _execute_live_e2e(environ={}, settings_loader=loader, runner=runner)
        for boundary in (loader, runner, backend, cleanup, provider):
            boundary.assert_not_called()

    def test_default_live_suite_is_skipped_and_zero_cloud(self) -> None:
        if os.environ.get(_LIVE_GATE) == "1":
            self.skipTest("The operator explicitly enabled the security gate.")
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            AliyunSandboxSecurityBoundariesLiveE2ETests
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

    def test_enabled_recovery_and_closed_egress_precede_runner(self) -> None:
        settings = Mock(spec=SandboxSettings)
        settings.allow_internet_access = False
        expected = _SecurityBoundaryReport(3, 4, 3, 3, 1, True, True)
        runner = Mock(return_value=expected)
        self.assertEqual(
            _execute_live_e2e(
                environ={_LIVE_GATE: "1"},
                settings_loader=lambda: settings,
                runner=runner,
            ),
            expected,
        )
        settings.require_enabled.assert_called_once_with()
        settings.require_recovery_enabled.assert_called_once_with()
        self.assertRegex(runner.call_args.args[1], _SESSION_ID_RE)

        for unsafe in (True, 0, None):
            bad = Mock(spec=SandboxSettings)
            bad.allow_internet_access = unsafe
            rejected_runner = Mock(side_effect=AssertionError("runner"))
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                _HarnessError,
                "配置验证失败",
            ):
                _execute_live_e2e(
                    environ={_LIVE_GATE: "1"},
                    settings_loader=lambda bad=bad: bad,
                    runner=rejected_runner,
                )
            rejected_runner.assert_not_called()

    def test_report_is_frozen_strict_and_exactly_bounded(self) -> None:
        report = _SecurityBoundaryReport(3, 4, 3, 3, 1, True, True)
        with self.assertRaises((AttributeError, TypeError)):
            report.network_denials = 5  # type: ignore[misc]
        invalid = (
            (True, 4, 3, 3, 1, True, True),
            (3, 5, 3, 3, 1, True, True),
            (3, 4, 3, 4, 1, True, True),
            (3, 4, 3, 3, 1, 1, True),
            (3, 4, 3, 3, 1, True, False),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(_HarnessError):
                _SecurityBoundaryReport(*values)  # type: ignore[arg-type]

    def test_snapshot_and_network_probes_are_bounded_numeric_and_response_free(self) -> None:
        snapshot = _security_snapshot("/mnt/oss")
        paths = tuple(entry.path for entry in snapshot.entries)
        self.assertEqual(len(paths), 12)
        self.assertEqual(paths, tuple(sorted(paths)))
        self.assertEqual(len(_NETWORK_TARGETS), 4)
        for address, port in _NETWORK_TARGETS:
            self.assertIsInstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
            self.assertIs(type(port), int)
            self.assertGreater(port, 0)
        network_source = _network_test_source().decode("utf-8")
        for forbidden in (
            "getaddrinfo",
            "gethostbyname",
            ".recv(",
            ".recvfrom(",
            "urlopen",
            "requests",
        ):
            self.assertNotIn(forbidden, network_source)
        self.assertIn("connect_ex", network_source)
        self.assertIn("100.100.100.200", network_source)
        self.assertIn("169.254.169.254", network_source)

        persistence_source = _persistence_test_source("/mnt/oss").decode("utf-8")
        self.assertIn("os.listdir(ROOT)", persistence_source)
        self.assertGreaterEqual(persistence_source.count("os.open("), 2)
        self.assertIn("os.O_CREAT | os.O_EXCL", persistence_source)
        self.assertIn("errno.EACCES", persistence_source)
        self.assertIn("errno.EPERM", persistence_source)
        self.assertIn("errno.ENOENT", persistence_source)
        self.assertIn("os.path.ismount(ROOT)", persistence_source)

        self.assertIn("300 * 1024 * 1024", _as_test_source().decode("utf-8"))
        self.assertIn("range(8)", _nproc_test_source().decode("utf-8"))
        self.assertIn("range(17)", _fsize_test_source().decode("utf-8"))

    def test_structured_contract_uses_three_fixed_limits_and_reuses_same_runtime(self) -> None:
        class FakeRuntime:
            def __init__(fake_self) -> None:
                fake_self.calls: list[str] = []

            def execute_authorized(fake_self, tool_name, arguments, authorize):
                self.assertEqual(tool_name, "bash_shell")
                self.assertIs(authorize(), True)
                self.assertIsInstance(arguments, BashShellArguments)
                self.assertEqual(arguments.command, "unittest")
                package = arguments.paths[0]
                fake_self.calls.append(package)
                return BashShellOutput(
                    command="unittest",
                    paths=[package],
                    exit_code=0,
                    stdout="",
                    stderr="fixed test summary",
                    truncated=False,
                )

        runtime = FakeRuntime()
        identity_checks: list[None] = []
        self.assertEqual(
            _exercise_security_contract(
                runtime,  # type: ignore[arg-type]
                same_runtime_check=lambda: identity_checks.append(None),
            ),
            (3, 4, 3, 3),
        )
        self.assertEqual(
            runtime.calls,
            [
                "persistence_tests",
                "network_tests",
                "as_tests",
                "reuse_tests",
                "nproc_tests",
                "reuse_tests",
                "fsize_tests",
                "reuse_tests",
            ],
        )
        self.assertEqual(len(identity_checks), 3)

    def test_v2_roundtrip_uses_canonical_object_and_lowercase_readback(self) -> None:
        session_id = "b" * 32
        persistence_root = "/mnt/oss"
        digest = hashlib.sha256(_V2_BODY).hexdigest()
        expected_path = posixpath.join(
            version_session_root(persistence_root, session_id),
            "objects",
            digest,
        )
        observed: list[object] = []

        class Prepared:
            def commit(fake_self):
                observed.append("commit")
                return WriteFileOutput(
                    path="security_boundary_v2.txt",
                    encoding="utf-8",
                    operation="created",
                    bytes_written=len(_V2_BODY),
                    sha256=digest,
                )

        class FakeRuntime:
            def prepare_remote_mutation(
                fake_self,
                tool_name,
                arguments,
                *,
                operation_id,
                permission_mode,
            ):
                observed.append((
                    tool_name,
                    arguments,
                    operation_id,
                    permission_mode,
                ))
                return Prepared()

        class FakeTransport:
            def workspace_version_read_file(
                fake_self,
                path,
                *,
                offset,
                max_bytes,
                timeout_seconds,
            ):
                observed.append((path, offset, max_bytes, timeout_seconds))
                return SandboxReadResult(_V2_BODY, len(_V2_BODY))

        class FakeBackend:
            def _with_transport(fake_self, operation):
                return operation(FakeTransport())

        lease = SimpleNamespace(
            persistence_root=persistence_root,
            session_id=session_id,
        )
        self.assertEqual(
            _exercise_v2_roundtrip(
                FakeRuntime(),  # type: ignore[arg-type]
                FakeBackend(),  # type: ignore[arg-type]
                lease,  # type: ignore[arg-type]
            ),
            1,
        )
        self.assertEqual(observed[0][0], "write_file")
        self.assertEqual(observed[0][1].content.encode("ascii"), _V2_BODY)
        self.assertEqual(observed[0][3], "partial-control")
        self.assertEqual(observed[1], "commit")
        self.assertEqual(observed[2], (expected_path, 0, len(_V2_BODY), 30))

    def test_product_sdk_create_receives_explicit_false_internet_flag(self) -> None:
        settings = _settings().model_copy(update={"allow_internet_access": False})
        observed: list[dict[str, object]] = []
        fake_e2b = ModuleType("e2b")

        class FakeSandbox:
            @staticmethod
            def create(**kwargs):
                observed.append(dict(kwargs))
                return SimpleNamespace(sandbox_id="offline")

        fake_e2b.Sandbox = FakeSandbox  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"e2b": fake_e2b}):
            _create_sandbox(settings, {"owner": "offline"})
        self.assertEqual(len(observed), 1)
        self.assertIn("allow_internet_access", observed[0])
        self.assertIs(observed[0]["allow_internet_access"], False)

    def test_failure_is_sanitized_and_finally_closes_cleans_exact_session_and_counts_zero(self) -> None:
        settings = _settings().model_copy(update={"allow_internet_access": False})
        session_id = "a" * 32
        events: list[tuple[str, object]] = []

        class FakeBackend:
            def __init__(fake_self, selected_session, selected_settings, *, workspace_initializer):
                self.assertEqual(selected_session, session_id)
                self.assertIs(selected_settings, settings)
                self.assertTrue(callable(workspace_initializer))
                fake_self.closed = False

            def close(fake_self):
                events.append(("close", session_id))
                fake_self.closed = True

        class FakeRuntime:
            def __init__(fake_self, backend):
                fake_self.backend = backend

            def ensure_ready(fake_self):
                return SimpleNamespace(sandbox_id="offline-sandbox")

            def close(fake_self):
                fake_self.backend.close()

        def fail_exercise(*_args, **_kwargs):
            _kwargs["stage_callback"]("persistence")
            raise OSError("provider-secret-id-url-path")

        def cleaner(selected_settings, selected_session):
            events.append(("clean", selected_session))
            self.assertIs(selected_settings, settings)

        def active(selected_settings, selected_session):
            events.append(("active", selected_session))
            self.assertIs(selected_settings, settings)
            return 0

        with self.assertRaisesRegex(
            _HarnessError,
            r"stage=persistence",
        ) as caught:
            _run_live_security_boundaries(
                settings,
                session_id,
                backend_factory=FakeBackend,  # type: ignore[arg-type]
                runtime_factory=FakeRuntime,  # type: ignore[arg-type]
                cleaner=cleaner,
                active_counter=active,
                exercise=fail_exercise,
                v2_roundtrip=Mock(side_effect=AssertionError("v2")),
            )
        rendered = str(caught.exception)
        self.assertNotIn("provider-secret", rendered)
        self.assertNotIn("offline-sandbox", rendered)
        self.assertEqual(
            events,
            [("close", session_id), ("clean", session_id), ("active", session_id)],
        )


if __name__ == "__main__":
    unittest.main()
