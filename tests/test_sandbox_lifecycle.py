"""Offline lifecycle regressions: leases, approvals, recovery and cost bounds."""

from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from app.execution_backend import (
    AliyunSandboxExecutionBackend, ExecutionBackendClosedError,
    ExecutionBackendCreateError,
)
from app.execution_runtime import ExecutionToolRuntime, ExecutionRuntimeClosedError
from app.sandbox_budget import SandboxBudget
from app.aliyun_sandbox_session_cleanup import (
    SandboxRecoveryPage, SandboxSessionCleanupError, cleanup_session_sandboxes,
    SANDBOX_GENERATION_METADATA_KEY, SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE, SANDBOX_SESSION_METADATA_KEY,
)
from app.schemas import CodeInterpreterArguments, ReadFileArguments, WriteFileArguments
from app.workspace_seed import WorkspaceSeedSnapshot
from app.workspace_versions import WorkspaceVersionCoordinator
from tests.sandbox_cleanup_fakes import empty_recovery_provider
from tests.test_execution_backend import enabled_settings
from tests.test_execution_runtime import _FakeRemoteGroup
from tests.test_workspace_versions import _MemoryTransport, _seed_entry


class _Clock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class _Sandbox:
    def __init__(self, identity, ttl):
        self.sandbox_id = identity
        self.created_timeout = ttl
        self.kill_calls = 0
        self.renewals = []
        self.renew_error = False

    def kill(self):
        self.kill_calls += 1
        return True

    def set_timeout(self, *, timeout, request_timeout):
        self.renewals.append((timeout, request_timeout))
        if self.renew_error:
            raise RuntimeError("secret-provider-details")


class SandboxLifecycleTests(unittest.TestCase):
    session_id = "a" * 32
    workspace_root = "/workspace/agnet-study/" + session_id

    def fixture(self, *, initializer=None, automatic=False, **changes):
        clock = _Clock()
        settings = enabled_settings(**changes)
        sandboxes = []
        transports = []
        persistence = {}

        def factory(current_settings, _metadata):
            sandbox = _Sandbox(f"sandbox-{len(sandboxes)}", current_settings.timeout_seconds)
            sandboxes.append(sandbox)
            return sandbox

        def transport_factory(_sandbox):
            transport = _MemoryTransport(self.workspace_root, persistence)
            transports.append(transport)
            return transport

        backend = AliyunSandboxExecutionBackend(
            self.session_id, settings, sandbox_factory=factory,
            transport_factory=transport_factory,
            recovery_provider=empty_recovery_provider(),
            workspace_initializer=initializer, clock=clock,
            automatic_maintenance=automatic, maintenance_interval=0.01,
        )
        self.addCleanup(backend.close)
        return backend, clock, sandboxes, transports

    def test_creation_is_lazy_and_idle_pair_is_recreated_without_replay(self):
        backend, clock, sandboxes, _ = self.fixture()
        self.assertEqual(sandboxes, [])
        self.assertIsNone(backend._monitor_thread)
        first = backend.ensure_ready()
        clock.advance(120)
        backend.maintain_lifecycle()
        self.assertEqual(backend.lifecycle_snapshot()["state"], "idle")
        self.assertEqual([item.kill_calls for item in sandboxes], [1, 1])
        self.assertEqual(backend.lifecycle_snapshot()["used_seconds"], 120)
        clock.advance(500)
        second = backend.ensure_ready()
        self.assertNotEqual(first.sandbox_id, second.sandbox_id)
        self.assertEqual(len(sandboxes), 4)
        self.assertEqual(backend.lifecycle_snapshot()["used_seconds"], 120)

    def test_pending_mutation_is_busy_and_committed_oss_state_restores(self):
        coordinator = WorkspaceVersionCoordinator(
            WorkspaceSeedSnapshot((_seed_entry("a.py", b"old\n"),))
        )
        backend, clock, sandboxes, transports = self.fixture(initializer=coordinator)
        runtime = ExecutionToolRuntime(backend)
        pending = runtime.prepare_remote_mutation(
            "write_file", WriteFileArguments(path="a.py", content="new\n", mode="overwrite"),
            operation_id="b" * 64, permission_mode="partial-control",
        )
        clock.advance(240)
        backend.maintain_lifecycle()
        self.assertEqual(backend.lifecycle_snapshot()["state"], "ready")
        self.assertEqual([item.kill_calls for item in sandboxes], [0, 0])
        self.assertEqual([len(item.renewals) for item in sandboxes], [1, 1])
        pending.commit()
        clock.advance(120)
        backend.maintain_lifecycle()
        self.assertEqual(backend.lifecycle_snapshot()["state"], "idle")
        restored = runtime.execute("read_file", ReadFileArguments(path="a.py"))
        self.assertEqual(restored.content, "new\n")
        self.assertEqual(transports[2].active[self.workspace_root + "/a.py"], b"new\n")
        self.assertEqual(len(sandboxes), 4)

    def test_discarded_mutation_releases_busy_pin(self):
        coordinator = WorkspaceVersionCoordinator(
            WorkspaceSeedSnapshot((_seed_entry("a.py", b"old\n"),))
        )
        backend, clock, _, _ = self.fixture(initializer=coordinator)
        runtime = ExecutionToolRuntime(backend)
        pending = runtime.prepare_remote_mutation(
            "write_file", WriteFileArguments(path="a.py", content="new", mode="overwrite"),
            operation_id="b" * 64, permission_mode="partial-control",
        )
        pending.discard()
        pending.discard()
        self.assertEqual(backend.lifecycle_snapshot()["active_reservations"], 0)
        clock.advance(120)
        backend.maintain_lifecycle()
        self.assertEqual(backend.lifecycle_snapshot()["state"], "idle")

    def test_pending_async_capability_holds_pair_until_cancel(self):
        backend, clock, sandboxes, _ = self.fixture()
        backend.ensure_ready()
        with patch("app.execution_runtime.AliyunSandboxToolGroup", _FakeRemoteGroup):
            runtime = ExecutionToolRuntime(backend)
        pending = runtime.prepare_async_authorized(
            "code_interpreter", CodeInterpreterArguments(code="print(1)"), lambda: True
        )
        clock.advance(240)
        backend.maintain_lifecycle()
        self.assertEqual([item.kill_calls for item in sandboxes], [0, 0])
        pending.cancel()
        clock.advance(120)
        backend.maintain_lifecycle()
        self.assertEqual([item.kill_calls for item in sandboxes], [1, 1])
        with self.assertRaises(ExecutionRuntimeClosedError):
            pending.run()

    def test_approval_callback_is_pinned_before_capability_exists(self):
        backend, clock, sandboxes, _ = self.fixture()
        backend.ensure_ready()
        with patch("app.execution_runtime.AliyunSandboxToolGroup", _FakeRemoteGroup):
            runtime = ExecutionToolRuntime(backend)

        def approval():
            clock.advance(240)
            backend.maintain_lifecycle()
            self.assertEqual([item.kill_calls for item in sandboxes], [0, 0])
            return True

        prepared = runtime.prepare_async_authorized(
            "code_interpreter", CodeInterpreterArguments(code="print(1)"), approval
        )
        prepared.cancel()
        self.assertEqual(backend.lifecycle_snapshot()["active_reservations"], 0)

    def test_running_handle_remains_busy_until_wait_finishes(self):
        backend, clock, sandboxes, _ = self.fixture()
        with patch("app.execution_runtime.AliyunSandboxToolGroup", _FakeRemoteGroup):
            runtime = ExecutionToolRuntime(backend)
        group = runtime._remote_group
        group.release_after_bind.clear()
        prepared = runtime.prepare_async_authorized(
            "code_interpreter", CodeInterpreterArguments(code="print(1)"), lambda: True
        )
        with ThreadPoolExecutor() as executor:
            future = executor.submit(prepared.run)
            self.assertTrue(group.handle_bound.wait(1))
            clock.advance(240)
            backend.maintain_lifecycle()
            self.assertEqual([item.kill_calls for item in sandboxes], [0, 0])
            group.release_after_bind.set()
            future.result(timeout=2)
        self.assertEqual(backend.lifecycle_snapshot()["active_reservations"], 0)

    def test_partial_renew_failure_fences_pair_without_automatic_recreation(self):
        backend, clock, sandboxes, _ = self.fixture()
        messages = []
        backend.set_lifecycle_callback(messages.append)
        backend.ensure_ready()
        release = backend.acquire_activity()
        self.addCleanup(release)
        sandboxes[1].renew_error = True
        clock.advance(240)
        backend.maintain_lifecycle()
        self.assertEqual([item.kill_calls for item in sandboxes], [1, 1])
        self.assertEqual(backend.lifecycle_snapshot()["state"], "failed")
        self.assertIn("续期失败", messages[-1])
        self.assertNotIn("secret-provider-details", repr(messages))
        with self.assertRaises(ExecutionBackendClosedError):
            backend.ensure_ready()
        self.assertEqual(len(sandboxes), 2)

    def test_short_lease_renews_before_two_bounded_network_requests_expire_it(self):
        backend, clock, sandboxes, _ = self.fixture(timeout_seconds=30)
        backend.ensure_ready()
        release = backend.acquire_activity()
        self.addCleanup(release)
        for sandbox in sandboxes:
            original = sandbox.set_timeout

            def delayed(*, timeout, request_timeout, original=original):
                clock.advance(9)
                original(timeout=timeout, request_timeout=request_timeout)

            sandbox.set_timeout = delayed
        clock.advance(5)
        backend.maintain_lifecycle()
        self.assertEqual(clock(), 23)
        self.assertEqual([len(item.renewals) for item in sandboxes], [1, 1])
        self.assertEqual(backend.lifecycle_snapshot()["state"], "ready")
        self.assertEqual([item.kill_calls for item in sandboxes], [0, 0])

    def test_budget_limit_fences_blocked_operation_and_rejects_late_result(self):
        backend, clock, sandboxes, _ = self.fixture(max_session_seconds=60)
        backend.ensure_ready()
        self.assertEqual([item.created_timeout for item in sandboxes], [50, 50])
        started, finish = threading.Event(), threading.Event()

        def operation(_transport):
            started.set()
            finish.wait(2)
            return "late-success"

        with ThreadPoolExecutor() as executor:
            future = executor.submit(backend._with_transport, operation)
            self.assertTrue(started.wait(1))
            clock.advance(60)
            backend.maintain_lifecycle()
            finish.set()
            with self.assertRaises(ExecutionBackendClosedError):
                future.result(timeout=2)
        self.assertEqual([item.kill_calls for item in sandboxes], [1, 1])
        self.assertEqual(backend.lifecycle_snapshot()["state"], "failed")
        with self.assertRaises(ExecutionBackendClosedError):
            backend.acquire_activity()

    def test_delayed_initialization_never_returns_expired_lease(self):
        clock = _Clock()
        sandbox = _Sandbox("first", 300)

        def factory(_settings, _metadata):
            clock.advance(301)
            return sandbox

        backend = AliyunSandboxExecutionBackend(
            self.session_id, enabled_settings(), sandbox_factory=factory,
            transport_factory=lambda _: object(), clock=clock,
            recovery_provider=empty_recovery_provider(), automatic_maintenance=False,
        )
        self.addCleanup(backend.close)
        with self.assertRaises(ExecutionBackendCreateError):
            backend.ensure_ready()
        self.assertEqual(sandbox.kill_calls, 1)

    def test_unknown_create_without_confirmed_cleanup_keeps_full_reservation(self):
        clock = _Clock()
        budget = SandboxBudget(3600, session_id=self.session_id, clock=clock)

        def factory(_settings, _metadata):
            raise RuntimeError("created-but-response-lost secret-provider-details")

        backend = AliyunSandboxExecutionBackend(
            self.session_id, enabled_settings(), sandbox_factory=factory,
            transport_factory=lambda _: object(), clock=clock, budget=budget,
            automatic_maintenance=False,
        )
        self.addCleanup(backend.close)
        with patch("app.execution_backend.cleanup_session_sandboxes",
                   side_effect=[None, RuntimeError("cleanup unavailable")]) as cleanup:
            with self.assertRaises(ExecutionBackendCreateError) as raised:
                backend.ensure_ready()
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(budget.snapshot()["used_seconds"], 310)
        self.assertNotIn("secret-provider-details", str(raised.exception))

    def test_network_delay_is_inside_persisted_reservation(self):
        clock = _Clock()
        budget = SandboxBudget(60, session_id=self.session_id, clock=clock)
        deadlines = []

        def factory(settings, _metadata):
            clock.advance(9)
            deadlines.append(clock() + settings.timeout_seconds)
            return _Sandbox(str(len(deadlines)), settings.timeout_seconds)

        backend = AliyunSandboxExecutionBackend(
            self.session_id, enabled_settings(max_session_seconds=60), sandbox_factory=factory,
            transport_factory=lambda _: object(), clock=clock, budget=budget,
            recovery_provider=empty_recovery_provider(), automatic_maintenance=False,
        )
        self.addCleanup(backend.close)
        backend.ensure_ready()
        self.assertLessEqual(max(deadlines), clock() + budget.reservation_remaining_seconds())
        self.assertEqual(deadlines, [59, 59])

    def test_late_old_generation_cleanup_cannot_delete_new_pair(self):
        active = {}
        query_filters = []
        created = []
        clock = _Clock()

        class Provider:
            def list_page(self, **kwargs):
                query_filters.append(dict(kwargs["metadata"]))
                return SandboxRecoveryPage(tuple(
                    item for item in active.values()
                    if all(item.metadata.get(key) == value
                           for key, value in kwargs["metadata"].items())
                ))

            def connect(self, sandbox_id, *, request_timeout):
                return SimpleNamespace(sandbox_id=sandbox_id)

            def kill(self, sandbox, *, request_timeout):
                active.pop(sandbox.sandbox_id)
                return True

        def factory(settings, metadata):
            sandbox = _Sandbox(f"pair-{len(created)}", settings.timeout_seconds)
            created.append(sandbox)
            active[sandbox.sandbox_id] = SimpleNamespace(
                sandbox_id=sandbox.sandbox_id, metadata=dict(metadata), state="running"
            )
            # Simulate stale SDK objects that can no longer destroy by ID;
            # generation-scoped independent recovery must handle them.
            sandbox.kill = lambda: False
            return sandbox

        provider = Provider()
        old = AliyunSandboxExecutionBackend(
            self.session_id, enabled_settings(), sandbox_factory=factory,
            transport_factory=lambda _: object(), recovery_provider=provider,
            clock=clock, automatic_maintenance=False, session_cleanup_complete=True,
        )
        new = AliyunSandboxExecutionBackend(
            self.session_id, enabled_settings(), sandbox_factory=factory,
            transport_factory=lambda _: object(), recovery_provider=provider,
            clock=clock, automatic_maintenance=False, session_cleanup_complete=True,
        )
        self.addCleanup(old.close)
        self.addCleanup(new.close)
        old.ensure_ready()
        new.ensure_ready()
        old.close()
        self.assertEqual(set(active), {"pair-2", "pair-3"})
        self.assertTrue(all(SANDBOX_GENERATION_METADATA_KEY in query for query in query_filters))
        self.assertEqual(new.lifecycle_snapshot()["state"], "ready")

    def test_generation_cleanup_rechecks_provider_metadata_before_killing(self):
        class Provider:
            connected = False

            def list_page(self, **kwargs):
                return SandboxRecoveryPage((SimpleNamespace(
                    sandbox_id="wrong-generation", state="running", metadata={
                        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
                        SANDBOX_SESSION_METADATA_KEY: "a" * 32,
                        SANDBOX_GENERATION_METADATA_KEY: "b" * 32,
                    },
                ),))

            def connect(self, *args, **kwargs):
                self.connected = True
                raise AssertionError("must not connect to mismatched generation")

        provider = Provider()
        with self.assertRaises(SandboxSessionCleanupError):
            cleanup_session_sandboxes(enabled_settings(), self.session_id,
                                      provider=provider, generation_id="c" * 32)
        self.assertFalse(provider.connected)

    def test_unknown_create_keeps_reservation_even_when_scan_is_empty(self):
        clock = _Clock()
        budget = SandboxBudget(3600, session_id=self.session_id, clock=clock)
        backend = AliyunSandboxExecutionBackend(
            self.session_id, enabled_settings(), clock=clock, budget=budget,
            sandbox_factory=lambda *_: (_ for _ in ()).throw(RuntimeError("lost response")),
            recovery_provider=empty_recovery_provider(), automatic_maintenance=False,
        )
        self.addCleanup(backend.close)
        with self.assertRaises(ExecutionBackendCreateError):
            backend.ensure_ready()
        self.assertEqual(budget.snapshot()["used_seconds"], 310)

    def test_automatic_monitor_reclaims_idle_pair_and_stops_on_close(self):
        backend, clock, sandboxes, _ = self.fixture(automatic=True)
        reclaimed = threading.Event()
        backend.set_lifecycle_callback(lambda _: reclaimed.set())
        backend.ensure_ready()
        clock.advance(120)
        self.assertTrue(reclaimed.wait(1))
        self.assertEqual([item.kill_calls for item in sandboxes], [1, 1])
        backend.close()
        backend._monitor_thread.join(timeout=1)
        self.assertFalse(backend._monitor_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
