"""离线验证执行后端生命周期骨架。"""

from __future__ import annotations

import builtins
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import SecretStr

from app.execution_backend import (
    ALIYUN_SANDBOX_OWNER_METADATA,
    ALIYUN_SANDBOX_EXECUTION_ROLE,
    ALIYUN_SANDBOX_PERSISTENCE_ROLE,
    ALIYUN_SANDBOX_ROLE_METADATA_KEY,
    ALIYUN_SANDBOX_SESSION_METADATA_KEY,
    AliyunSandboxExecutionBackend as _AliyunSandboxExecutionBackend,
    ExecutionBackendClosedError,
    ExecutionBackendCloseError,
    ExecutionBackendCreateError,
    LocalExecutionBackend,
)
from app.sandbox_transport import (
    SandboxRuntimeIdentity,
    create_e2b_persistence_transport,
)
from app.sandbox_config import SandboxProbeDisabledError, SandboxSettings
from tests.sandbox_cleanup_fakes import (
    empty_recovery_provider,
)
from app.aliyun_sandbox_session_cleanup import (
    SandboxRecoveryPage, SANDBOX_GENERATION_METADATA_KEY,
)


def AliyunSandboxExecutionBackend(*args, **kwargs):
    kwargs.setdefault("automatic_maintenance", False)
    kwargs.setdefault("recovery_provider", empty_recovery_provider())
    if (
        "sandbox_factory" in kwargs
        and "persistence_sandbox_factory" not in kwargs
    ):
        kwargs["persistence_sandbox_factory"] = (
            lambda _settings, _metadata: FakeSandbox("persistence-sandbox")
        )
    if (
        "transport_factory" in kwargs
        and "persistence_transport_factory" not in kwargs
    ):
        cached: dict[str, object] = {}
        original = kwargs["transport_factory"]

        def execution_transport(candidate):
            value = original(candidate)
            cached["value"] = value
            return value

        def persistence_transport(_candidate):
            return cached["value"]

        kwargs["transport_factory"] = execution_transport
        kwargs["persistence_transport_factory"] = persistence_transport
    return _AliyunSandboxExecutionBackend(*args, **kwargs)


def enabled_settings(**changes: object) -> SandboxSettings:
    values: dict[str, object] = {
        "enabled": True,
        "api_key": SecretStr("fake-e2b-key"),
        "api_url": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        "domain": "cn-hangzhou.e2b.fc.aliyuncs.com",
        "template": "code-interpreter-v1",
        "timeout_seconds": 300,
        "oss_bucket": "agent-probe-bucket",
        "oss_endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "oss_bucket_path": "/projects/test/runs/backend",
        "oss_mount_dir": "/mnt/oss",
        "oss_role_arn": "acs:ram::1234567890123456:role/secret-role",
        "oss_read_only": False,
    }
    values.update(changes)
    return SandboxSettings(**values)


class FakeSandbox:
    def __init__(self, sandbox_id: str = "sandbox-1") -> None:
        self.sandbox_id = sandbox_id
        self.kill_calls = 0
        self.kill_error: BaseException | None = None
        self.kill_result: object = True

    def kill(self) -> object:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        return self.kill_result


def lifecycle_transport(candidate: object) -> object:
    del candidate
    return object()


class ExecutionBackendTests(unittest.TestCase):
    @staticmethod
    def _recovery_item(sandbox_id: str, *, state: str = "running") -> object:
        return SimpleNamespace(
            sandbox_id=sandbox_id,
            state=state,
            metadata={
                ALIYUN_SANDBOX_OWNER_METADATA[0]: ALIYUN_SANDBOX_OWNER_METADATA[1],
                ALIYUN_SANDBOX_SESSION_METADATA_KEY: "a" * 32,
            },
        )

    def test_session_recovery_kills_stale_running_and_paused_before_create(self):
        events: list[str] = []
        pages = iter((
            SandboxRecoveryPage(items=(
                self._recovery_item("stale-running"),
                self._recovery_item("stale-paused", state="paused"),
            )),
            SandboxRecoveryPage(items=()),
        ))
        stale_handles = {
            "stale-running": FakeSandbox("stale-running"),
            "stale-paused": FakeSandbox("stale-paused"),
        }

        class Provider:
            def list_page(_self, **kwargs):
                del kwargs
                events.append("list")
                return next(pages)

            def connect(_self, sandbox_id, *, request_timeout):
                del request_timeout
                events.append(f"connect:{sandbox_id}")
                handle = stale_handles[sandbox_id]
                original_kill = handle.kill

                def recorded_kill():
                    events.append(f"kill:{sandbox_id}")
                    return original_kill()

                handle.kill = recorded_kill  # type: ignore[method-assign]
                return handle

            def kill(_self, sandbox, *, request_timeout):
                del request_timeout
                return sandbox.kill()

        candidate = FakeSandbox("candidate")

        def factory(_settings, _metadata):
            events.append("create")
            return candidate

        backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            transport_factory=lifecycle_transport,
            recovery_provider=Provider(),
        )
        self.assertEqual(backend.ensure_ready().sandbox_id, "candidate")
        self.assertEqual(
            events,
            [
                "list",
                "connect:stale-running",
                "kill:stale-running",
                "connect:stale-paused",
                "kill:stale-paused",
                "list",
                "create",
            ],
        )
        backend.close()

    def test_recovery_failure_prevents_candidate_creation(self):
        creation_calls = 0

        class Provider:
            def list_page(self, **kwargs):
                del kwargs
                raise OSError("provider-secret.invalid")

        def factory(_settings, _metadata):
            nonlocal creation_calls
            creation_calls += 1
            return FakeSandbox()

        backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            transport_factory=lifecycle_transport,
            recovery_provider=Provider(),
        )
        with self.assertRaises(ExecutionBackendCreateError) as caught:
            backend.ensure_ready()
        self.assertEqual(creation_calls, 0)
        self.assertNotIn("provider-secret.invalid", str(caught.exception))
        backend.close()

    def test_failed_candidate_direct_kill_uses_independent_recovery(self):
        candidate = FakeSandbox("candidate-after-create-failure")
        candidate.kill_result = False
        pages = iter((
            SandboxRecoveryPage(items=()),
            SandboxRecoveryPage(items=()),
            SandboxRecoveryPage(items=(
                self._recovery_item("candidate-after-create-failure"),
            )),
            SandboxRecoveryPage(items=()),
        ))
        recovered = FakeSandbox("candidate-after-create-failure")

        class Provider:
            def list_page(self, **kwargs):
                page = next(pages)
                for item in page.items:
                    item.metadata.update(kwargs["metadata"])
                return page

            def connect(self, sandbox_id, *, request_timeout):
                del request_timeout
                self_outer.assertEqual(
                    sandbox_id, "candidate-after-create-failure"
                )
                return recovered

            def kill(self, sandbox, *, request_timeout):
                del request_timeout
                return sandbox.kill()

        self_outer = self
        backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=lambda _settings, _metadata: candidate,
            transport_factory=lambda _candidate: (_ for _ in ()).throw(
                OSError("transport-secret.invalid")
            ),
            recovery_provider=Provider(),
        )
        with self.assertRaises(ExecutionBackendCreateError) as caught:
            backend.ensure_ready()
        self.assertNotIn("transport-secret.invalid", str(caught.exception))
        self.assertEqual(candidate.kill_calls, 1)
        self.assertEqual(recovered.kill_calls, 1)
        backend.close()

    def test_default_transport_binds_workspace_and_persistence_roots(self):
        sandbox = FakeSandbox()
        transport = Mock()
        transport.runtime_identity = SandboxRuntimeIdentity(
            python_path="/opt/agnet-study/runtime/venv/bin/python",
            system_python_path="/usr/bin/python3",
            version=(3, 11, 9),
            resolved_python_path="/usr/bin/python3.11",
            executable_sha256="b" * 64,
            metadata_digest="c" * 64,
            runtime_user="user",
            digest="a" * 64,
        )
        with patch(
            "app.execution_backend.create_e2b_transport",
            return_value=transport,
        ) as transport_factory:
            backend = AliyunSandboxExecutionBackend(
                "f" * 32,
                enabled_settings(),
                sandbox_factory=lambda _settings, _metadata: sandbox,
                persistence_transport_factory=lambda _candidate: transport,
            )
            lease = backend.ensure_ready()
        transport_factory.assert_called_once_with(
            sandbox,
            "/workspace/agnet-study/" + "f" * 32,
            None,
            "/opt/agnet-study/runtime/venv",
            "user",
        )
        self.assertEqual(lease.persistence_root, "/mnt/oss")
        self.assertEqual(
            lease.runtime_python_path,
            "/opt/agnet-study/runtime/venv/bin/python",
        )
        backend.close()

    def test_workspace_initializer_requires_callable_and_runs_before_ready_once(self):
        with self.assertRaises(TypeError):
            AliyunSandboxExecutionBackend(
                "0" * 32,
                enabled_settings(),
                workspace_initializer=object(),  # type: ignore[arg-type]
            )

        order: list[str] = []
        sandbox = FakeSandbox()
        transport = object()
        entered = threading.Event()
        release = threading.Event()

        def factory(settings, metadata):
            del settings, metadata
            order.append("sandbox")
            return sandbox

        def transport_factory(candidate):
            self.assertIs(candidate, sandbox)
            order.append("transport")
            return transport

        def initializer(exact_transport, lease):
            self.assertIs(exact_transport._execution, transport)
            self.assertIs(exact_transport._persistence, transport)
            self.assertEqual(lease.workspace_root, "/workspace/agnet-study/" + "0" * 32)
            self.assertEqual(lease.persistence_root, "/mnt/oss")
            self.assertEqual(lease.sandbox_id, "sandbox-1")
            order.append("initializer")
            entered.set()
            self.assertTrue(release.wait(5))

        backend = AliyunSandboxExecutionBackend(
            "0" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            transport_factory=transport_factory,
            workspace_initializer=initializer,
        )
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(backend.ensure_ready) for _ in range(6)]
            self.assertTrue(entered.wait(5))
            self.assertFalse(any(future.done() for future in futures))
            release.set()
            leases = [future.result(5) for future in futures]
        self.assertEqual(order, ["sandbox", "transport", "initializer"])
        self.assertTrue(all(lease is leases[0] for lease in leases))
        backend.close()

    def test_workspace_initializer_failure_kills_candidate_and_preserves_system_exit(self):
        for failure in (OSError("SECRET seed upload"), KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(failure=type(failure).__name__):
                sandbox = FakeSandbox()
                create_calls = 0

                def factory(settings, metadata):
                    nonlocal create_calls
                    del settings, metadata
                    create_calls += 1
                    return sandbox

                def fail(_transport, _lease, error=failure):
                    raise error

                backend = AliyunSandboxExecutionBackend(
                    "1" * 32,
                    enabled_settings(),
                    sandbox_factory=factory,
                    transport_factory=lifecycle_transport,
                    workspace_initializer=fail,
                )
                expected = (
                    ExecutionBackendCreateError
                    if isinstance(failure, Exception)
                    else type(failure)
                )
                with self.assertRaises(expected) as caught:
                    backend.ensure_ready()
                self.assertEqual(sandbox.kill_calls, 1)
                self.assertNotIn("SECRET", str(caught.exception))
                with self.assertRaises(expected):
                    backend.ensure_ready()
                self.assertEqual(create_calls, 1)
                self.assertEqual(sandbox.kill_calls, 1)
                backend.close()
                self.assertEqual(sandbox.kill_calls, 1)

    def test_local_backend_describes_fixed_root_and_close_is_noop(self):
        with TemporaryDirectory() as temporary:
            backend = LocalExecutionBackend(Path(temporary))
            first = backend.ensure_ready()
            backend.close()
            second = backend.ensure_ready()

        self.assertEqual(first, second)
        self.assertEqual(first.backend_name, "local")
        self.assertEqual(first.workspace_root, Path(temporary).resolve().as_posix())
        self.assertIsNone(first.persistence_root)
        self.assertTrue(first.control_plane_isolated)

    def test_remote_constructor_is_lazy_and_does_not_import_e2b(self):
        imports: list[str] = []
        original_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            imports.append(name)
            if name == "e2b" or name.startswith("e2b."):
                raise AssertionError("constructor imported e2b before ensure_ready")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=guarded_import):
            backend = AliyunSandboxExecutionBackend(
                "a" * 32,
                enabled_settings(),
            )

        self.assertNotIn("e2b", imports)
        backend.close()

    def test_remote_creates_once_and_returns_only_safe_workspace_lease(self):
        created: list[FakeSandbox] = []

        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> FakeSandbox:
            self.assertTrue(settings.enabled)
            sandbox = FakeSandbox(f"sandbox-{len(created) + 1}")
            created.append(sandbox)
            return sandbox

        backend = AliyunSandboxExecutionBackend(
            "b" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            persistence_sandbox_factory=factory,
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
        )
        lease = backend.ensure_ready()
        self.assertIs(lease, backend.ensure_ready())
        self.assertEqual(len(created), 2)
        self.assertNotIn("fc.sandbox.storage.oss", repr(created[0]))
        self.assertEqual(lease.workspace_root, "/workspace/agnet-study/" + "b" * 32)
        self.assertEqual(lease.persistence_root, "/mnt/oss")
        self.assertEqual(lease.sandbox_id, "sandbox-1")
        self.assertTrue(lease.control_plane_isolated)
        self.assertNotIn("fake-e2b-key", repr(backend))
        self.assertNotIn("secret-role", repr(backend))
        self.assertNotIn("fake-e2b-key", repr(lease))
        self.assertNotIn("secret-role", repr(lease))
        backend.close()
        self.assertEqual(created[0].kill_calls, 1)
        self.assertEqual(created[1].kill_calls, 1)

    def test_remote_creation_metadata_is_exact_and_session_scoped(self):
        settings = enabled_settings()
        captured: list[dict[str, str]] = []

        def factory(
            _settings: SandboxSettings,
            metadata: dict[str, str],
        ) -> FakeSandbox:
            captured.append(dict(metadata))
            return FakeSandbox(f"sandbox-{len(captured)}")

        first_session = "1" * 32
        second_session = "2" * 32
        first = AliyunSandboxExecutionBackend(
            first_session,
            settings,
            sandbox_factory=factory,
            persistence_sandbox_factory=factory,
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
        )
        second = AliyunSandboxExecutionBackend(
            second_session,
            settings,
            sandbox_factory=factory,
            persistence_sandbox_factory=factory,
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
        )

        first_lease = first.ensure_ready()
        second_lease = second.ensure_ready()

        owner_key, owner_value = ALIYUN_SANDBOX_OWNER_METADATA
        expected_persistence = {
            "fc.sandbox.storage.oss": (
                '{"mountPoints":[{"bucketName":"agent-probe-bucket",'
                '"bucketPath":"/projects/test/runs/backend",'
                '"endpoint":"https://oss-cn-hangzhou.aliyuncs.com",'
                '"mountDir":"/mnt/oss","readOnly":false}]}'
            ),
            "fc.sandbox.auth.role": (
                "acs:ram::1234567890123456:role/secret-role"
            ),
            owner_key: owner_value,
        }
        self.assertEqual(
            [{key: value for key, value in item.items()
              if key != SANDBOX_GENERATION_METADATA_KEY} for item in captured],
            [
                {
                    owner_key: owner_value,
                    ALIYUN_SANDBOX_SESSION_METADATA_KEY: first_session,
                    ALIYUN_SANDBOX_ROLE_METADATA_KEY: ALIYUN_SANDBOX_EXECUTION_ROLE,
                },
                {
                    **expected_persistence,
                    ALIYUN_SANDBOX_SESSION_METADATA_KEY: first_session,
                    ALIYUN_SANDBOX_ROLE_METADATA_KEY: ALIYUN_SANDBOX_PERSISTENCE_ROLE,
                },
                {
                    owner_key: owner_value,
                    ALIYUN_SANDBOX_SESSION_METADATA_KEY: second_session,
                    ALIYUN_SANDBOX_ROLE_METADATA_KEY: ALIYUN_SANDBOX_EXECUTION_ROLE,
                },
                {
                    **expected_persistence,
                    ALIYUN_SANDBOX_SESSION_METADATA_KEY: second_session,
                    ALIYUN_SANDBOX_ROLE_METADATA_KEY: ALIYUN_SANDBOX_PERSISTENCE_ROLE,
                },
            ],
        )
        self.assertRegex(captured[0][SANDBOX_GENERATION_METADATA_KEY], r"^[a-f0-9]{32}$")
        self.assertEqual(captured[0][SANDBOX_GENERATION_METADATA_KEY], captured[1][SANDBOX_GENERATION_METADATA_KEY])
        self.assertEqual(captured[2][SANDBOX_GENERATION_METADATA_KEY], captured[3][SANDBOX_GENERATION_METADATA_KEY])
        self.assertNotEqual(captured[0][SANDBOX_GENERATION_METADATA_KEY], captured[2][SANDBOX_GENERATION_METADATA_KEY])
        self.assertNotEqual(
            captured[0][ALIYUN_SANDBOX_SESSION_METADATA_KEY],
            captured[2][ALIYUN_SANDBOX_SESSION_METADATA_KEY],
        )
        for rendered in (
            repr(first),
            repr(second),
            repr(first_lease),
            repr(second_lease),
        ):
            self.assertNotIn("fake-e2b-key", rendered)
            self.assertNotIn("secret-role", rendered)
            self.assertNotIn("fc.sandbox", rendered)

        first.close()
        second.close()

    def test_concurrent_ensure_ready_shares_one_creation(self):
        started = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()
        sandbox = FakeSandbox()

        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> FakeSandbox:
            nonlocal calls
            del settings, metadata
            with calls_lock:
                calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return sandbox

        backend = AliyunSandboxExecutionBackend(
            "c" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            transport_factory=lifecycle_transport,
        )
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(backend.ensure_ready) for _ in range(8)]
            self.assertTrue(started.wait(timeout=5))
            release.set()
            leases = [future.result(timeout=5) for future in futures]

        self.assertEqual(calls, 1)
        self.assertTrue(all(lease is leases[0] for lease in leases))
        backend.close()

    def test_close_racing_creation_waits_for_candidate_then_kills_it_once(self):
        started = threading.Event()
        release = threading.Event()
        sandbox = FakeSandbox()

        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> FakeSandbox:
            del settings, metadata
            started.set()
            self.assertTrue(release.wait(timeout=5))
            return sandbox

        backend = AliyunSandboxExecutionBackend(
            "4" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            transport_factory=lifecycle_transport,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            create_future = pool.submit(backend.ensure_ready)
            self.assertTrue(started.wait(timeout=5))
            close_future = pool.submit(backend.close)
            self.assertFalse(close_future.done())
            release.set()
            self.assertEqual(create_future.result(timeout=5).sandbox_id, "sandbox-1")
            self.assertIsNone(close_future.result(timeout=5))

        self.assertEqual(sandbox.kill_calls, 1)
        with self.assertRaises(ExecutionBackendClosedError):
            backend.ensure_ready()

    def test_invalid_or_missing_sandbox_id_kills_returned_candidate_once(self):
        for invalid_id in ("", None):
            with self.subTest(invalid_id=invalid_id):
                sandbox = FakeSandbox()
                sandbox.sandbox_id = invalid_id  # type: ignore[assignment]
                backend = AliyunSandboxExecutionBackend(
                    "9" * 32,
                    enabled_settings(),
                    sandbox_factory=lambda settings, metadata: sandbox,
                )
                with self.assertRaises(ExecutionBackendCreateError):
                    backend.ensure_ready()
                self.assertEqual(sandbox.kill_calls, 1)
                backend.close()
                self.assertEqual(sandbox.kill_calls, 1)

    def test_candidate_kill_failure_keeps_create_error_safe(self):
        sandbox = FakeSandbox()
        sandbox.sandbox_id = ""
        sandbox.kill_error = OSError("role-secret.invalid")
        backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
        )
        with self.assertRaises(ExecutionBackendCreateError) as caught:
            backend.ensure_ready()
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertNotIn("role-secret.invalid", str(caught.exception))
        self.assertFalse(getattr(caught.exception, "__notes__", ()))
        backend.close()

    def test_transport_factory_failure_kills_created_candidate_once(self):
        sandbox = FakeSandbox()
        backend = AliyunSandboxExecutionBackend(
            "7" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lambda candidate: (_ for _ in ()).throw(
                OSError("transport-secret.invalid")
            ),
        )
        with self.assertRaises(ExecutionBackendCreateError) as caught:
            backend.ensure_ready()
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertNotIn("transport-secret.invalid", str(caught.exception))
        backend.close()

    def test_none_transport_factory_result_kills_created_candidate_once(self):
        sandbox = FakeSandbox()
        backend = AliyunSandboxExecutionBackend(
            "8" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lambda candidate: None,  # type: ignore[return-value]
        )
        with self.assertRaises(ExecutionBackendCreateError):
            backend.ensure_ready()
        self.assertEqual(sandbox.kill_calls, 1)
        backend.close()

    def test_default_transport_rejects_missing_sdk_surface_and_kills_candidate(self):
        sandbox = FakeSandbox()
        backend = AliyunSandboxExecutionBackend(
            "6" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
        )
        with self.assertRaises(ExecutionBackendCreateError):
            backend.ensure_ready()
        self.assertEqual(sandbox.kill_calls, 1)
        backend.close()

    def test_keyboard_interrupt_is_not_wrapped_and_backend_can_close(self):
        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> object:
            del settings, metadata
            raise KeyboardInterrupt()

        backend = AliyunSandboxExecutionBackend(
            "b" * 32,
            enabled_settings(),
            sandbox_factory=factory,
            transport_factory=lifecycle_transport,
        )
        with self.assertRaises(KeyboardInterrupt):
            backend.ensure_ready()
        backend.close()

        sandbox = FakeSandbox()
        sandbox.kill_error = KeyboardInterrupt()
        backend = AliyunSandboxExecutionBackend(
            "c" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
        )
        backend.ensure_ready()
        with self.assertRaises(KeyboardInterrupt):
            backend.close()
        backend.close()
        with self.assertRaises(ExecutionBackendClosedError):
            backend.ensure_ready()

    def test_close_is_idempotent_and_cannot_reopen_remote_backend(self):
        sandbox = FakeSandbox()
        backend = AliyunSandboxExecutionBackend(
            "d" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
        )
        backend.ensure_ready()
        backend.close()
        backend.close()
        self.assertEqual(sandbox.kill_calls, 1)
        self.assertTrue(backend.closed)
        with self.assertRaises(ExecutionBackendClosedError):
            backend.ensure_ready()

    def test_close_kills_while_transport_operation_is_blocked_and_rejects_new_work(self):
        sandbox = FakeSandbox()
        entered = threading.Event()
        release = threading.Event()
        transport = object()
        backend = AliyunSandboxExecutionBackend(
            "0" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lambda candidate: transport,
        )
        backend.ensure_ready()

        def blocked_operation(candidate: object) -> str:
            self.assertIs(candidate._execution, transport)
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return "finished-after-kill"

        with ThreadPoolExecutor(max_workers=2) as pool:
            operation_future = pool.submit(backend._with_transport, blocked_operation)
            self.assertTrue(entered.wait(timeout=5))
            close_future = pool.submit(backend.close)
            self.assertIsNone(close_future.result(timeout=1))
            self.assertEqual(sandbox.kill_calls, 1)
            with self.assertRaises(ExecutionBackendClosedError):
                backend._with_transport(lambda candidate: None)
            release.set()
            with self.assertRaises(ExecutionBackendClosedError):
                operation_future.result(timeout=5)

        self.assertTrue(backend.closed)
        backend.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_transport_operation_system_exception_still_releases_active_registration(self):
        for exception in (KeyboardInterrupt(), SystemExit(9)):
            with self.subTest(exception=type(exception).__name__):
                sandbox = FakeSandbox()
                backend = AliyunSandboxExecutionBackend(
                    "3" * 32,
                    enabled_settings(),
                    sandbox_factory=lambda settings, metadata: sandbox,
                    transport_factory=lifecycle_transport,
                )
                backend.ensure_ready()

                with self.assertRaises(type(exception)):
                    backend._with_transport(
                        lambda transport: (_ for _ in ()).throw(exception)
                    )
                backend.close()
                self.assertEqual(sandbox.kill_calls, 1)

    def test_blocked_transport_exception_after_kill_is_not_rewritten(self):
        sandbox = FakeSandbox()
        entered = threading.Event()
        release = threading.Event()
        backend = AliyunSandboxExecutionBackend(
            "5" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
        )
        backend.ensure_ready()

        def fail_after_kill(transport: object) -> None:
            del transport
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            raise OSError("transport stopped after kill")

        with ThreadPoolExecutor(max_workers=2) as pool:
            operation_future = pool.submit(backend._with_transport, fail_after_kill)
            self.assertTrue(entered.wait(timeout=5))
            backend.close()
            release.set()
            with self.assertRaisesRegex(OSError, "transport stopped after kill"):
                operation_future.result(timeout=5)

        self.assertEqual(sandbox.kill_calls, 1)

    def test_concurrent_close_calls_share_one_kill(self):
        sandbox = FakeSandbox()
        kill_started = threading.Event()
        kill_release = threading.Event()

        def blocking_kill() -> bool:
            sandbox.kill_calls += 1
            kill_started.set()
            self.assertTrue(kill_release.wait(timeout=5))
            return True

        sandbox.kill = blocking_kill  # type: ignore[method-assign]
        backend = AliyunSandboxExecutionBackend(
            "6" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
        )
        backend.ensure_ready()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(backend.close)
            self.assertTrue(kill_started.wait(timeout=5))
            second = pool.submit(backend.close)
            self.assertFalse(second.done())
            kill_release.set()
            self.assertIsNone(first.result(timeout=5))
            self.assertIsNone(second.result(timeout=5))

        self.assertEqual(sandbox.kill_calls, 1)

    def test_concurrent_close_calls_share_one_independent_cleanup(self):
        sandbox = FakeSandbox("candidate-close-recovery")
        direct_started = threading.Event()
        direct_release = threading.Event()

        def failed_direct_kill() -> bool:
            sandbox.kill_calls += 1
            direct_started.set()
            self.assertTrue(direct_release.wait(timeout=5))
            return False

        sandbox.kill = failed_direct_kill  # type: ignore[method-assign]
        recovered = FakeSandbox("candidate-close-recovery")
        list_calls = 0

        class Provider:
            def list_page(_self, **kwargs):
                nonlocal list_calls
                list_calls += 1
                if list_calls in {1, 2, 4}:
                    return SandboxRecoveryPage(items=())
                if list_calls == 3:
                    item = self._recovery_item("candidate-close-recovery")
                    item.metadata.update(kwargs["metadata"])
                    return SandboxRecoveryPage(items=(
                        item,
                    ))
                raise AssertionError("unexpected list call")

            def connect(_self, sandbox_id, *, request_timeout):
                del request_timeout
                self.assertEqual(sandbox_id, "candidate-close-recovery")
                return recovered

            def kill(_self, sandbox, *, request_timeout):
                del request_timeout
                return sandbox.kill()

        backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=lambda _settings, _metadata: sandbox,
            transport_factory=lifecycle_transport,
            recovery_provider=Provider(),
        )
        backend.ensure_ready()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(backend.close)
            self.assertTrue(direct_started.wait(timeout=5))
            second = pool.submit(backend.close)
            self.assertFalse(second.done())
            direct_release.set()
            self.assertIsNone(first.result(timeout=5))
            self.assertIsNone(second.result(timeout=5))

        self.assertEqual(sandbox.kill_calls, 1)
        self.assertEqual(recovered.kill_calls, 1)
        self.assertEqual(list_calls, 4)
        self.assertTrue(backend.closed)

    def test_create_failure_can_be_closed_without_retry_or_secret_leak(self):
        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> object:
            del settings, metadata
            raise OSError("https://user:password@secret.invalid/request")

        backend = AliyunSandboxExecutionBackend(
            "e" * 32,
            enabled_settings(),
            sandbox_factory=factory,
        )
        with self.assertRaises(ExecutionBackendCreateError) as caught:
            backend.ensure_ready()
        self.assertNotIn("secret.invalid", str(caught.exception))
        backend.close()
        backend.close()
        with self.assertRaises(ExecutionBackendClosedError):
            backend.ensure_ready()

    def test_disabled_create_fails_before_factory_and_close_is_safe(self):
        called = False

        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> object:
            nonlocal called
            del settings, metadata
            called = True
            return FakeSandbox()

        backend = AliyunSandboxExecutionBackend(
            "f" * 32,
            SandboxSettings(),
            sandbox_factory=factory,
        )
        with self.assertRaises(SandboxProbeDisabledError):
            backend.ensure_ready()
        self.assertFalse(called)
        backend.close()

    def test_kill_exception_is_recovered_by_independent_cleanup(self):
        sandbox = FakeSandbox()
        sandbox.kill_error = OSError("authorization secret.invalid")
        backend = AliyunSandboxExecutionBackend(
            "1" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
        )
        backend.ensure_ready()
        self.assertIsNone(backend.close())
        backend.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_kill_false_is_recovered_by_independent_cleanup(self):
        sandbox = FakeSandbox()
        sandbox.kill_result = False
        backend = AliyunSandboxExecutionBackend(
            "2" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
        )
        backend.ensure_ready()
        self.assertIsNone(backend.close())
        backend.close()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_direct_and_independent_cleanup_failure_is_fixed_and_idempotent(self):
        class FailAfterInitialProof:
            def __init__(self) -> None:
                self.list_calls = 0

            def list_page(self, **kwargs):
                del kwargs
                self.list_calls += 1
                if self.list_calls > 2:
                    raise OSError("provider-sensitive-error")
                return SandboxRecoveryPage(items=())

            def connect(self, sandbox_id, *, request_timeout):
                del sandbox_id, request_timeout
                raise AssertionError("no connection expected")

        sandbox = FakeSandbox()
        sandbox.kill_error = OSError("direct-secret.invalid")
        recovery_provider = FailAfterInitialProof()
        backend = AliyunSandboxExecutionBackend(
            "2" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: sandbox,
            transport_factory=lifecycle_transport,
            recovery_provider=recovery_provider,
        )
        backend.ensure_ready()
        with self.assertRaises(ExecutionBackendCloseError) as caught:
            backend.close()
        self.assertNotIn("provider-sensitive-error", str(caught.exception))
        self.assertNotIn("direct-secret.invalid", str(caught.exception))
        self.assertEqual(sandbox.kill_calls, 1)
        backend.close()

    def test_split_backend_routes_execution_and_persistence_method_families(self):
        calls: list[tuple[str, str]] = []

        class RoutedTransport:
            def __init__(self, role: str) -> None:
                self.role = role

            def __getattr__(self, name: str):
                def invoke(*_args, **_kwargs):
                    calls.append((self.role, name))
                    return name

                return invoke

        candidates = [FakeSandbox("execution-1"), FakeSandbox("persistence-1")]
        transports = {
            "execution-1": RoutedTransport("execution"),
            "persistence-1": RoutedTransport("persistence"),
        }

        def factory(_settings, _metadata):
            return candidates.pop(0)

        backend = _AliyunSandboxExecutionBackend(
            "4" * 32,
            enabled_settings(),
            automatic_maintenance=False,
            sandbox_factory=factory,
            persistence_sandbox_factory=factory,
            transport_factory=lambda candidate: transports[candidate.sandbox_id],
            persistence_transport_factory=(
                lambda candidate: transports[candidate.sandbox_id]
            ),
            recovery_provider=empty_recovery_provider(),
        )
        backend.ensure_ready()
        self.assertEqual(
            backend._with_transport(lambda item: item.read_file("x")),
            "read_file",
        )
        self.assertEqual(
            backend._with_transport(
                lambda item: item.rollback_delete_file("x")
            ),
            "rollback_delete_file",
        )
        self.assertEqual(
            backend._with_transport(
                lambda item: item.persistence_read_file("x")
            ),
            "persistence_read_file",
        )
        self.assertEqual(
            backend._with_transport(
                lambda item: item.rollback_delete_workspace_version_object("x")
            ),
            "rollback_delete_workspace_version_object",
        )
        self.assertEqual(
            calls,
            [
                ("execution", "read_file"),
                ("execution", "rollback_delete_file"),
                ("persistence", "persistence_read_file"),
                ("persistence", "rollback_delete_workspace_version_object"),
            ],
        )
        backend.close()

    def test_persistence_creation_or_initialization_failure_cleans_every_candidate(self):
        execution = FakeSandbox("execution-failure-case")

        def fail_persistence_factory(_settings, metadata):
            self.assertEqual(
                metadata[ALIYUN_SANDBOX_ROLE_METADATA_KEY],
                ALIYUN_SANDBOX_PERSISTENCE_ROLE,
            )
            raise OSError("provider-secret.invalid")

        backend = _AliyunSandboxExecutionBackend(
            "5" * 32,
            enabled_settings(),
            automatic_maintenance=False,
            sandbox_factory=lambda _settings, _metadata: execution,
            persistence_sandbox_factory=fail_persistence_factory,
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
            recovery_provider=empty_recovery_provider(),
        )
        with self.assertRaises(ExecutionBackendCreateError):
            backend.ensure_ready()
        self.assertEqual(execution.kill_calls, 1)

        execution = FakeSandbox("execution-init-case")
        persistence = FakeSandbox("persistence-init-case")
        candidates = iter((execution, persistence))
        backend = _AliyunSandboxExecutionBackend(
            "6" * 32,
            enabled_settings(),
            automatic_maintenance=False,
            sandbox_factory=lambda _settings, _metadata: next(candidates),
            persistence_sandbox_factory=lambda _settings, _metadata: next(candidates),
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lambda _candidate: (_ for _ in ()).throw(
                OSError("persistence-init-secret.invalid")
            ),
            recovery_provider=empty_recovery_provider(),
        )
        with self.assertRaises(ExecutionBackendCreateError) as caught:
            backend.ensure_ready()
        self.assertNotIn("secret.invalid", str(caught.exception))
        self.assertEqual(execution.kill_calls, 1)
        self.assertEqual(persistence.kill_calls, 1)

    def test_same_candidate_from_two_role_factories_is_killed_only_once(self):
        candidate = FakeSandbox("same-candidate")
        backend = _AliyunSandboxExecutionBackend(
            "7" * 32,
            enabled_settings(),
            automatic_maintenance=False,
            sandbox_factory=lambda _settings, _metadata: candidate,
            persistence_sandbox_factory=lambda _settings, _metadata: candidate,
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
            recovery_provider=empty_recovery_provider(),
        )
        with self.assertRaisesRegex(
            ExecutionBackendCreateError, "未被物理分离"
        ):
            backend.ensure_ready()
        self.assertEqual(candidate.kill_calls, 1)

    def test_close_attempts_execution_then_persistence_even_after_failure(self):
        events: list[str] = []
        execution = FakeSandbox("execution-close")
        persistence = FakeSandbox("persistence-close")

        def execution_kill():
            execution.kill_calls += 1
            events.append("execution")
            raise OSError("execution-secret.invalid")

        def persistence_kill():
            persistence.kill_calls += 1
            events.append("persistence")
            return True

        execution.kill = execution_kill  # type: ignore[method-assign]
        persistence.kill = persistence_kill  # type: ignore[method-assign]
        candidates = iter((execution, persistence))
        backend = _AliyunSandboxExecutionBackend(
            "8" * 32,
            enabled_settings(),
            automatic_maintenance=False,
            sandbox_factory=lambda _settings, _metadata: next(candidates),
            persistence_sandbox_factory=lambda _settings, _metadata: next(candidates),
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
            recovery_provider=empty_recovery_provider(),
        )
        backend.ensure_ready()
        self.assertIsNone(backend.close())
        self.assertEqual(events, ["execution", "persistence"])
        self.assertEqual(execution.kill_calls, 1)
        self.assertEqual(persistence.kill_calls, 1)

    def test_close_two_failures_and_cleaner_failure_is_sanitized_fail_closed(self):
        execution = FakeSandbox("execution-close-fail")
        persistence = FakeSandbox("persistence-close-fail")
        execution.kill_error = OSError("execution-close-secret.invalid")
        persistence.kill_error = OSError("persistence-close-secret.invalid")

        class Provider:
            def __init__(self) -> None:
                self.list_calls = 0

            def list_page(self, **kwargs):
                del kwargs
                self.list_calls += 1
                if self.list_calls <= 2:
                    return SandboxRecoveryPage(items=())
                raise OSError("cleaner-close-secret.invalid")

            def connect(self, sandbox_id, *, request_timeout):
                del sandbox_id, request_timeout
                raise AssertionError("no connection expected")

        provider = Provider()
        candidates = iter((execution, persistence))
        backend = _AliyunSandboxExecutionBackend(
            "9" * 32,
            enabled_settings(),
            automatic_maintenance=False,
            sandbox_factory=lambda _settings, _metadata: next(candidates),
            persistence_sandbox_factory=lambda _settings, _metadata: next(candidates),
            transport_factory=lifecycle_transport,
            persistence_transport_factory=lifecycle_transport,
            recovery_provider=provider,
        )
        backend.ensure_ready()
        with self.assertRaises(ExecutionBackendCloseError) as caught:
            backend.close()
        rendered = str(caught.exception)
        self.assertNotIn("secret.invalid", rendered)
        self.assertEqual(execution.kill_calls, 1)
        self.assertEqual(persistence.kill_calls, 1)
        self.assertEqual(provider.list_calls, 3)
        self.assertTrue(backend.closed)

    def test_persistence_transport_is_files_only_and_validates_mount_as_root(self):
        calls: list[tuple[str, str, object]] = []

        class Files:
            def get_info(_self, path, *, user, request_timeout):
                calls.append((path, user, request_timeout))
                return SimpleNamespace(
                    type="directory",
                    symlink_target=None,
                )

        transport = create_e2b_persistence_transport(
            SimpleNamespace(files=Files()),
            "/mnt/oss",
        )
        self.assertFalse(hasattr(transport, "run"))
        self.assertFalse(hasattr(transport, "read_file"))
        self.assertEqual(calls, [("/mnt/oss", "root", 30)])


if __name__ == "__main__":
    unittest.main()
