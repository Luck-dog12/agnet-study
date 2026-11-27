"""Internal execution-backend lifecycle boundary.

This module deliberately describes execution environments only.  It does not
execute tools, expose a Sandbox SDK object, or alter the Agent/Registry path.
The control plane (model client, prompts, session events, and tool policy)
remains in the host process; a lease contains only safe workspace metadata.
"""

from __future__ import annotations

import posixpath
import re
import threading
import math
import time
from uuid import uuid4
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TypeVar

from app.aliyun_sandbox_probe import _create_sandbox, build_oss_metadata
from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    SANDBOX_GENERATION_METADATA_KEY,
    SandboxRecoveryProvider,
    cleanup_session_sandboxes,
)
from app.sandbox_config import SandboxProbeDisabledError, SandboxSettings
from app.sandbox_budget import SandboxBudget, SandboxBudgetError
from app.sandbox_transport import (
    SandboxPersistenceTransport,
    SandboxRuntimeIdentity,
    SandboxTransport,
    SplitSandboxTransport,
    create_e2b_persistence_transport,
    create_e2b_transport,
)


_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_SANDBOX_WORKSPACE_PREFIX: Final[str] = "/workspace/agnet-study"
ALIYUN_SANDBOX_OWNER_METADATA: Final[tuple[str, str]] = (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
)
ALIYUN_SANDBOX_SESSION_METADATA_KEY: Final[str] = SANDBOX_SESSION_METADATA_KEY
ALIYUN_SANDBOX_ROLE_METADATA_KEY: Final[str] = "agnet-study.role"
ALIYUN_SANDBOX_EXECUTION_ROLE: Final[str] = "execution"
ALIYUN_SANDBOX_PERSISTENCE_ROLE: Final[str] = "persistence"


class ExecutionBackendError(RuntimeError):
    """Base class for safe, local lifecycle errors."""


class ExecutionBackendClosedError(ExecutionBackendError):
    """The backend lease has been permanently closed."""


class ExecutionBackendCreateError(ExecutionBackendError):
    """A remote execution environment could not be created."""


class ExecutionBackendCloseError(ExecutionBackendError):
    """A remote execution environment could not be killed."""


@dataclass(frozen=True, slots=True)
class WorkspaceDescription:
    """Immutable workspace coordinates; never contains control-plane state."""

    backend_name: str
    session_id: str
    workspace_root: str
    persistence_root: str | None
    control_plane_isolated: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    """Immutable, safe description returned after a backend is ready."""

    workspace: WorkspaceDescription
    sandbox_id: str | None = None
    runtime_python_path: str | None = None
    runtime_identity: SandboxRuntimeIdentity | None = None

    @property
    def backend_name(self) -> str:
        return self.workspace.backend_name

    @property
    def session_id(self) -> str:
        return self.workspace.session_id

    @property
    def workspace_root(self) -> str:
        return self.workspace.workspace_root

    @property
    def persistence_root(self) -> str | None:
        return self.workspace.persistence_root

    @property
    def control_plane_isolated(self) -> bool:
        return self.workspace.control_plane_isolated


# Friendly aliases for callers that describe the value as a workspace lease.
WorkspaceLease = ExecutionLease


class ExecutionBackend(ABC):
    """Lifecycle-only backend contract; tools remain outside this boundary."""

    @abstractmethod
    def ensure_ready(self) -> ExecutionLease:
        """Lazily establish the backend and return safe workspace metadata."""

    @abstractmethod
    def close(self) -> None:
        """Close the backend; implementations must make this idempotent."""


class LocalExecutionBackend(ExecutionBackend):
    """Description of the existing fixed local project root.

    This class intentionally does not implement file access.  Existing
    ``tool.file_access`` and Registry policy checks remain the only file/tool
    execution path.  ``close`` is a true no-op because the host project root
    is owned by the process, not by this lifecycle adapter.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        root = _PROJECT_ROOT if project_root is None else project_root
        if not isinstance(root, Path):
            raise TypeError("project_root 必须是 Path。")
        self._workspace = WorkspaceDescription(
            backend_name="local",
            session_id="local",
            workspace_root=root.resolve().as_posix(),
            persistence_root=None,
            control_plane_isolated=True,
        )
        self._lease = ExecutionLease(workspace=self._workspace)

    def ensure_ready(self) -> ExecutionLease:
        return self._lease

    def close(self) -> None:
        return None

    def __repr__(self) -> str:
        return "LocalExecutionBackend(workspace_root={!r})".format(
            self._workspace.workspace_root
        )


SandboxFactory = Callable[[SandboxSettings, dict[str, str]], Any]
TransportFactory = Callable[[Any], SandboxTransport]
PersistenceTransportFactory = Callable[[Any], SandboxPersistenceTransport]
WorkspaceInitializer = Callable[[SandboxTransport, ExecutionLease], None]
_TransportResult = TypeVar("_TransportResult")


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id 必须是 32 位小写十六进制文本。")
    return session_id


def _validate_sandbox_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 or character.isspace() for character in value)
    ):
        raise ExecutionBackendCreateError("Sandbox 创建结果缺少安全 sandbox_id。")
    return value


class AliyunSandboxExecutionBackend(ExecutionBackend):
    """Lazy lifecycle adapter for an isolated execution/storage Sandbox pair.

    The E2B object is retained only in this private adapter and is never part
    of the lease or any public execution result.  Creation is terminally
    failed after one failed attempt; this prevents accidental split-brain
    retries and lets ``close`` safely finalize a failed adapter.
    """

    def __init__(
        self,
        session_id: str,
        settings: SandboxSettings,
        *,
        sandbox_factory: SandboxFactory | None = None,
        persistence_sandbox_factory: SandboxFactory | None = None,
        transport_factory: TransportFactory | None = None,
        persistence_transport_factory: PersistenceTransportFactory | None = None,
        workspace_initializer: WorkspaceInitializer | None = None,
        recovery_provider: SandboxRecoveryProvider | None = None,
        budget: SandboxBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
        maintenance_interval: float = 1.0,
        automatic_maintenance: bool = True,
        session_cleanup_complete: bool = False,
    ) -> None:
        self._session_id = _validate_session_id(session_id)
        if not isinstance(settings, SandboxSettings):
            raise TypeError("settings 必须是 SandboxSettings 实例。")
        self._settings = settings
        self._sandbox_factory = sandbox_factory or _create_sandbox
        self._persistence_sandbox_factory = (
            persistence_sandbox_factory or self._sandbox_factory
        )
        self._transport_factory = transport_factory
        self._persistence_transport_factory = persistence_transport_factory
        self._recovery_provider = recovery_provider
        if workspace_initializer is not None and not callable(workspace_initializer):
            raise TypeError("workspace_initializer 必须是 callable 或 None。")
        self._workspace_initializer = workspace_initializer
        self._condition = threading.Condition()
        self._state = "new"
        self._execution_sandbox: Any | None = None
        self._persistence_sandbox: Any | None = None
        self._transport: SandboxTransport | None = None
        self._lease: ExecutionLease | None = None
        self._create_error: BaseException | None = None
        self._active_operations = 0
        self._active_reservations = 0
        self._clock = clock
        self._budget = budget or SandboxBudget(
            settings.max_session_seconds, session_id=session_id, clock=clock
        )
        if maintenance_interval <= 0:
            raise ValueError("maintenance_interval 必须为正数。")
        self._maintenance_interval = maintenance_interval
        self._automatic_maintenance = automatic_maintenance
        self._maintenance_lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._deadline = 0.0
        self._lease_duration = float(settings.timeout_seconds)
        self._last_activity = clock()
        self._lifecycle_callback: Callable[[str], None] | None = None
        self._generation_id: str | None = None
        self._session_cleanup_complete = session_cleanup_complete

    def set_lifecycle_callback(self, callback: Callable[[str], None] | None) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("lifecycle callback 必须可调用。")
        with self._condition:
            self._lifecycle_callback = callback

    def _notify_lifecycle(self, message: str) -> None:
        with self._condition:
            callback = self._lifecycle_callback
        if callback is not None:
            try:
                callback(message)
            except Exception:
                pass  # Presentation must never prevent cleanup or fencing.

    def lifecycle_snapshot(self) -> dict[str, str | int | float | bool]:
        budget = self._budget.snapshot()
        with self._condition:
            return {
                "state": self._state,
                "used_seconds": budget["used_seconds"],
                "remaining_seconds": budget["remaining_seconds"],
                "max_session_seconds": self._settings.max_session_seconds,
                "idle_timeout_seconds": self._settings.idle_timeout_seconds,
                "timeout_seconds": self._settings.timeout_seconds,
                "active_operations": self._active_operations,
                "active_reservations": self._active_reservations,
            }

    def acquire_activity(self) -> Callable[[], None]:
        """Pin an approval, pending capability or entire remote invocation."""
        with self._condition:
            while self._state in {"creating", "recycling", "closing"}:
                self._condition.wait()
            if self._state in {"closed", "failed"}:
                raise ExecutionBackendClosedError("云沙箱已停止，不能接收新的操作。")
            self._active_reservations += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._condition:
                if released:
                    return
                released = True
                self._active_reservations -= 1
                self._last_activity = self._clock()
                self._condition.notify_all()

        return release

    def _start_monitor(self) -> None:
        with self._condition:
            if not self._automatic_maintenance or self._monitor_thread is not None:
                return
            thread = threading.Thread(
                target=self._monitor, name="sandbox-lifecycle", daemon=True
            )
            self._monitor_thread = thread
            thread.start()

    def _monitor(self) -> None:
        while not self._monitor_stop.wait(self._maintenance_interval):
            try:
                self.maintain_lifecycle()
            except Exception:
                self._terminate_lifecycle("云沙箱生命周期检查失败，已停止该会话的云操作。")

    @staticmethod
    def _kill_sandbox(sandbox: Any) -> object:
        # Production SDK calls are bounded; injected transports remain ordinary
        # objects, avoiding a requirement for SDK-specific signatures in tests.
        if type(sandbox).__module__.startswith("e2b."):
            return sandbox.kill(request_timeout=10)
        return sandbox.kill()

    def _creation_settings(self) -> SandboxSettings:
        remaining = math.floor(min(
            self._deadline - self._clock(),
            self._budget.reservation_remaining_seconds() - 10,
        ))
        if remaining < 1:
            raise ExecutionBackendCreateError("云沙箱剩余预算或初始化租期不足，未继续创建。")
        return self._settings.model_copy(update={"timeout_seconds": remaining})

    def _check_creation_deadline(self) -> None:
        if self._clock() >= self._deadline:
            raise ExecutionBackendCreateError("云沙箱初始化超过租期，未开放执行。")

    @property
    def session_id(self) -> str:
        """Return the immutable host-session identity without creating a Sandbox."""
        return self._session_id

    def ensure_ready(self) -> ExecutionLease:
        self.maintain_lifecycle()
        with self._condition:
            while self._state in {"creating", "closing", "recycling"}:
                self._condition.wait()
            if self._state == "closed":
                raise ExecutionBackendClosedError(
                    "Sandbox execution backend 已关闭，不能重新打开。"
                )
            if self._state == "failed":
                assert self._create_error is not None
                raise self._create_error
            if self._state == "ready":
                assert self._lease is not None
                return self._lease
            recreating = self._state == "idle"
            self._state = "creating"
            self._generation_id = uuid4().hex

        execution_candidate: Any | None = None
        persistence_candidate: Any | None = None
        lease: ExecutionLease | None = None
        transport: SandboxTransport | None = None
        create_error: BaseException | None = None
        create_outcome_unknown = False
        try:
            self._settings.require_enabled()
            if not self._session_cleanup_complete:
                cleanup_session_sandboxes(
                    self._settings,
                    self._session_id,
                    provider=self._recovery_provider,
                )
                self._session_cleanup_complete = True
            approved = self._budget.begin(self._settings.timeout_seconds + 10)
            self._deadline = self._clock() + approved - 10
            if approved < 40:
                raise ExecutionBackendCreateError("本会话云沙箱预算不足 30 秒租期及 10 秒请求预留，未创建资源。")
            owner_key, owner_value = ALIYUN_SANDBOX_OWNER_METADATA
            common_metadata = {
                owner_key: owner_value,
                ALIYUN_SANDBOX_SESSION_METADATA_KEY: self._session_id,
                SANDBOX_GENERATION_METADATA_KEY: self._generation_id,
            }
            execution_metadata = {
                **common_metadata,
                ALIYUN_SANDBOX_ROLE_METADATA_KEY: ALIYUN_SANDBOX_EXECUTION_ROLE,
            }
            create_settings = self._creation_settings()
            self._deadline = min(self._deadline, self._clock() + create_settings.timeout_seconds)
            create_outcome_unknown = True
            execution_candidate = self._sandbox_factory(create_settings, execution_metadata)
            create_outcome_unknown = False
            self._check_creation_deadline()
            execution_sandbox_id = _validate_sandbox_id(
                getattr(execution_candidate, "sandbox_id", None)
            )
            workspace_root = posixpath.join(
                _SANDBOX_WORKSPACE_PREFIX,
                self._session_id,
            )
            workspace = WorkspaceDescription(
                backend_name="aliyun-sandbox",
                session_id=self._session_id,
                workspace_root=workspace_root,
                persistence_root=self._settings.oss_mount_dir,
                control_plane_isolated=True,
            )
            execution_transport = (
                self._transport_factory(execution_candidate)
                if self._transport_factory is not None
                else create_e2b_transport(
                    execution_candidate,
                    workspace.workspace_root,
                    None,
                    self._settings.runtime_venv_dir,
                    self._settings.runtime_user,
                )
            )
            if execution_transport is None:
                raise ExecutionBackendCreateError(
                    "Aliyun execution transport 创建失败；未建立可用执行租约。"
                )
            runtime_identity = getattr(execution_transport, "runtime_identity", None)
            if self._transport_factory is None and not isinstance(
                runtime_identity, SandboxRuntimeIdentity
            ):
                raise ExecutionBackendCreateError(
                    "Aliyun Sandbox transport 未验证可信 Python runtime。"
                )

            persistence_metadata = build_oss_metadata(self._settings)
            persistence_metadata.update(common_metadata)
            persistence_metadata[
                ALIYUN_SANDBOX_ROLE_METADATA_KEY
            ] = ALIYUN_SANDBOX_PERSISTENCE_ROLE
            create_settings = self._creation_settings()
            self._deadline = min(self._deadline, self._clock() + create_settings.timeout_seconds)
            create_outcome_unknown = True
            persistence_candidate = self._persistence_sandbox_factory(create_settings, persistence_metadata)
            create_outcome_unknown = False
            self._check_creation_deadline()
            persistence_sandbox_id = _validate_sandbox_id(
                getattr(persistence_candidate, "sandbox_id", None)
            )
            if (
                persistence_candidate is execution_candidate
                or persistence_sandbox_id == execution_sandbox_id
            ):
                raise ExecutionBackendCreateError(
                    "Aliyun execution 与 persistence Sandbox 未被物理分离。"
                )
            if self._persistence_transport_factory is not None:
                persistence_transport = self._persistence_transport_factory(
                    persistence_candidate
                )
            elif self._transport_factory is not None:
                # Test/integration seam: callers that supply a generic typed
                # transport factory may use it for both role-scoped resources.
                persistence_transport = self._transport_factory(
                    persistence_candidate
                )
            else:
                persistence_transport = create_e2b_persistence_transport(
                    persistence_candidate,
                    self._settings.oss_mount_dir,
                )
            if persistence_transport is None:
                raise ExecutionBackendCreateError(
                    "Aliyun persistence transport 创建失败；未建立可用执行租约。"
                )
            transport = SplitSandboxTransport(
                execution_transport,
                persistence_transport,
            )
            lease = ExecutionLease(
                workspace=workspace,
                sandbox_id=execution_sandbox_id,
                runtime_python_path=self._settings.runtime_venv_dir + "/bin/python",
                runtime_identity=(
                    runtime_identity
                    if isinstance(runtime_identity, SandboxRuntimeIdentity)
                    else None
                ),
            )
            if self._workspace_initializer is not None:
                result = self._workspace_initializer(transport, lease)
                if result is not None:
                    raise ExecutionBackendCreateError(
                        "远端工作区 initializer 返回了无效结果。"
                    )
            self._check_creation_deadline()
        except SandboxProbeDisabledError as error:
            create_error = error
        except BaseException as error:
            if not isinstance(error, Exception):
                create_error = error
            else:
                create_error = (
                    error
                    if isinstance(error, ExecutionBackendCreateError)
                    else ExecutionBackendCreateError(
                        "Aliyun Sandbox 创建失败；未建立可用执行租约。"
                    )
                )
                if create_error is not error:
                    create_error.__cause__ = error

        if (
            create_error is not None
            or execution_candidate is None
            or persistence_candidate is None
            or lease is None
            or transport is None
        ):
            if create_error is None:
                create_error = ExecutionBackendCreateError(
                    "Aliyun Sandbox 创建失败；未建立可用执行租约。"
                )
            cleanup_system_error: BaseException | None = None
            candidates_list: list[Any] = []
            candidate_identities: set[int] = set()
            for candidate in (execution_candidate, persistence_candidate):
                if candidate is None or id(candidate) in candidate_identities:
                    continue
                candidate_identities.add(id(candidate))
                candidates_list.append(candidate)
            candidates = tuple(candidates_list)
            direct_cleanup_failed = False
            for candidate in candidates:
                try:
                    kill_result = self._kill_sandbox(candidate)
                    if kill_result is not True:
                        direct_cleanup_failed = True
                except BaseException as cleanup_error:
                    if not isinstance(cleanup_error, Exception):
                        cleanup_system_error = cleanup_error
                    else:
                        direct_cleanup_failed = True
            cleanup_confirmed = (
                not direct_cleanup_failed and cleanup_system_error is None
                and not create_outcome_unknown
            )
            if direct_cleanup_failed or cleanup_system_error is not None or create_outcome_unknown:
                try:
                    cleanup_session_sandboxes(
                        self._settings,
                        self._session_id,
                        provider=self._recovery_provider,
                        generation_id=self._generation_id,
                    )
                    # A lost create response can precede eventual visibility in
                    # provider listings. Even an empty scan cannot refund that
                    # request's worst-case reservation safely.
                    cleanup_confirmed = not create_outcome_unknown
                except BaseException as recovery_error:
                    if not isinstance(recovery_error, Exception):
                        if cleanup_system_error is None:
                            cleanup_system_error = recovery_error
                        else:
                            cleanup_system_error.add_note(
                                "独立会话清理也被系统级异常中断。"
                            )
                    else:
                        create_error.add_note(
                            "创建失败后的独立会话清理未能确认完成"
                            f"（{type(recovery_error).__name__}）。"
                        )
            try:
                self._budget.finish(confirmed=cleanup_confirmed)
            except SandboxBudgetError:
                create_error.add_note("预算记录未能完成结算，保守保留预留额度。")
            with self._condition:
                self._create_error = create_error
                self._state = "failed"
                self._condition.notify_all()
            if cleanup_system_error is not None:
                raise cleanup_system_error
            raise create_error

        with self._condition:
            self._execution_sandbox = execution_candidate
            self._persistence_sandbox = persistence_candidate
            self._transport = transport
            self._lease = lease
            self._state = "ready"
            self._lease_duration = max(1.0, self._deadline - self._clock())
            self._condition.notify_all()
            self._last_activity = self._clock()
        self._start_monitor()
        if recreating:
            self._notify_lifecycle("云沙箱已按需重建，工作区已从持久化记录恢复；没有重放旧命令。")
        return lease

    def close(self) -> None:
        self._monitor_stop.set()
        with self._maintenance_lock:
            self._close_resources()

    def _close_resources(self, *, idle: bool = False) -> None:
        with self._condition:
            while self._state in {"creating", "closing"}:
                self._condition.wait()
            if self._state == "closed":
                return None
            if self._state in {"new", "idle", "failed"}:
                self._state = "closed"
                self._condition.notify_all()
                return None
            assert self._state in {"ready", "recycling"}
            assert self._execution_sandbox is not None
            assert self._persistence_sandbox is not None
            sandboxes = (
                self._execution_sandbox,
                self._persistence_sandbox,
            )
            self._state = "recycling" if idle else "closing"

        direct_failures: list[BaseException] = []
        recovery_failure: BaseException | None = None
        try:
            # Stop execution first, then the host-only persistence endpoint.
            # A failure for either resource never prevents the other attempt.
            for sandbox in sandboxes:
                try:
                    result = self._kill_sandbox(sandbox)
                    if result is not True:
                        direct_failures.append(
                            RuntimeError("sandbox kill did not confirm success")
                        )
                except BaseException as error:
                    direct_failures.append(error)
            if direct_failures:
                try:
                    cleanup_session_sandboxes(
                        self._settings,
                        self._session_id,
                        provider=self._recovery_provider,
                        generation_id=self._generation_id,
                    )
                except BaseException as error:
                    recovery_failure = error

            system_failure = next(
                (
                    error
                    for error in (*direct_failures, recovery_failure)
                    if error is not None and not isinstance(error, Exception)
                ),
                None,
            )
            if system_failure is not None:
                if recovery_failure is not None:
                    system_failure.add_note("独立会话清理未能确认完成。")
                raise system_failure
            if direct_failures and recovery_failure is not None:
                raise ExecutionBackendCloseError(
                    "Aliyun Sandbox 资源直接销毁及独立清理均未能确认完成；"
                    "backend 已关闭。"
                ) from None
        finally:
            confirmed = not direct_failures or recovery_failure is None
            settlement_error = False
            try:
                self._budget.finish(confirmed=confirmed)
            except SandboxBudgetError:
                settlement_error = True
            with self._condition:
                self._execution_sandbox = None
                self._persistence_sandbox = None
                self._transport = None
                self._lease = None
                self._state = "idle" if idle and confirmed and not settlement_error else "closed"
                self._condition.notify_all()
            if idle and (not confirmed or settlement_error):
                raise ExecutionBackendCloseError("云沙箱回收或预算结算未确认，已停止自动重建。") from None

    def _terminate_lifecycle(self, message: str) -> None:
        with self._maintenance_lock:
            with self._condition:
                if self._state in {"closed", "failed"}:
                    return
            try:
                self._close_resources()
            except Exception:
                pass
            with self._condition:
                self._state = "failed"
                self._create_error = ExecutionBackendClosedError(message)
                self._condition.notify_all()
            self._monitor_stop.set()
        self._notify_lifecycle(message)

    def maintain_lifecycle(self) -> None:
        """Perform one bounded maintenance pass (also a deterministic test seam)."""
        with self._maintenance_lock:
            with self._condition:
                if self._state != "ready":
                    return
                now = self._clock()
                busy = self._active_reservations > 0 or self._active_operations > 0
                idle_remaining = self._settings.idle_timeout_seconds - (now - self._last_activity)
                expired = now >= self._deadline
                if not busy and idle_remaining <= 0:
                    self._state = "recycling"
                    recycle = True
                else:
                    recycle = False
                sandboxes = (self._execution_sandbox, self._persistence_sandbox)
            if recycle:
                try:
                    self._close_resources(idle=True)
                except Exception:
                    self._monitor_stop.set()
                    self._notify_lifecycle("云沙箱空闲回收未能确认，已停止自动重建。")
                    return
                self._notify_lifecycle("云沙箱已因空闲自动回收；下次新工具操作会按需恢复。")
                return
            try:
                budget_remaining = self._budget.remaining_seconds()
            except SandboxBudgetError:
                self._terminate_lifecycle("云沙箱预算记录不可用，已停止该会话的云操作。")
                return
            if budget_remaining <= 0:
                self._terminate_lifecycle("本会话云沙箱累计时长预算已耗尽，已终止云操作；未完成操作不会自动重试。")
                return
            if budget_remaining <= 10:
                self._terminate_lifecycle("本会话云沙箱剩余额度不足以安全续期，已停止沙箱；未完成操作不会自动重试。")
                return
            if expired:
                self._terminate_lifecycle("云沙箱租期已到期，已隔离本会话；未确认结果的操作不会自动重试。")
                return
            # Leave room for two sequential, 10-second bounded provider
            # requests even when the configured cloud lease is only 30s.
            margin = min(60.0, max(25.0, self._lease_duration / 3))
            if self._deadline - now > margin:
                return
            requested = self._settings.timeout_seconds if busy else min(
                self._settings.timeout_seconds, math.ceil(idle_remaining)
            )
            if min(requested, budget_remaining - 10) + now <= self._deadline + 1:
                return  # Already reserved through the latest affordable deadline.
            try:
                ttl = self._budget.extend(requested + 10)
                deadline = self._clock() + ttl - 10
                provider_deadlines: list[float] = []
                for sandbox in sandboxes:
                    remaining = math.floor(min(
                        deadline - self._clock(),
                        self._budget.reservation_remaining_seconds() - 10,
                    ))
                    if remaining < 1:
                        raise SandboxBudgetError("续期预算已用尽。")
                    provider_deadlines.append(self._clock() + remaining)
                    sandbox.set_timeout(timeout=remaining, request_timeout=min(10, remaining))
                if self._clock() >= deadline:
                    raise SandboxBudgetError("续期未在预算截止前完成。")
                with self._condition:
                    self._deadline = min(provider_deadlines)
                    self._lease_duration = max(1.0, self._deadline - now)
            except Exception:
                self._terminate_lifecycle("云沙箱续期失败，已停止云操作并尝试回收双实例；未完成操作不会自动重试。")

    def _with_transport(
        self,
        operation: Callable[[SandboxTransport], _TransportResult],
    ) -> _TransportResult:
        """Run one typed transport operation without blocking concurrent close.

        This is an internal seam for the remote tool group.  It never returns
        the raw Sandbox object.  ``close`` first moves the backend to
        ``closing`` and can then kill the Sandbox while this call is blocked;
        no later operation can enter once that transition has happened.
        """
        self.maintain_lifecycle()
        with self._condition:
            if self._state != "ready" or self._transport is None:
                raise ExecutionBackendClosedError(
                    "Sandbox execution backend 尚未就绪或已关闭。"
                )
            transport = self._transport
            self._active_operations += 1
        try:
            result = operation(transport)
            self.maintain_lifecycle()
            with self._condition:
                if self._state != "ready" or self._transport is not transport:
                    raise ExecutionBackendClosedError("云沙箱已停止，迟到的操作结果未被接受。")
            return result
        finally:
            with self._condition:
                self._active_operations -= 1
                self._last_activity = self._clock()
                self._condition.notify_all()

    def prepare_workspace_mutation(self, **kwargs: object):
        """Use the snapshot-bound v2 coordinator without exposing transport."""
        lease = self.ensure_ready()
        coordinator = self._workspace_initializer
        prepare = getattr(coordinator, "prepare", None)
        if not callable(prepare):
            raise ExecutionBackendCreateError(
                "远端工作区未绑定 durable mutation coordinator。"
            )
        return self._with_transport(
            lambda transport: prepare(transport, lease, **kwargs)
        )

    def commit_workspace_mutation(self, candidate: object, **kwargs: object):
        """Commit one exact prepared mutation through the v2 coordinator."""
        lease = self.ensure_ready()
        coordinator = self._workspace_initializer
        commit = getattr(coordinator, "commit", None)
        if not callable(commit):
            raise ExecutionBackendCreateError(
                "远端工作区未绑定 durable mutation coordinator。"
            )
        return self._with_transport(
            lambda transport: commit(transport, lease, candidate, **kwargs)
        )

    def prepare_workspace_command_mutation(self, **kwargs: object):
        """Build one isolated structured-command candidate."""
        lease = self.ensure_ready()
        coordinator = self._workspace_initializer
        prepare = getattr(coordinator, "prepare_command", None)
        if not callable(prepare):
            raise ExecutionBackendCreateError(
                "远端工作区未绑定 command mutation coordinator。"
            )
        return self._with_transport(lambda transport: prepare(transport, lease, **kwargs))

    def commit_workspace_command_mutation(self, candidate: object, **kwargs: object):
        """Commit one exact multi-file command candidate as one generation."""
        lease = self.ensure_ready()
        coordinator = self._workspace_initializer
        commit = getattr(coordinator, "commit_command", None)
        if not callable(commit):
            raise ExecutionBackendCreateError(
                "远端工作区未绑定 command mutation coordinator。"
            )
        return self._with_transport(
            lambda transport: commit(transport, lease, candidate, **kwargs)
        )

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._state in {"closed", "failed"}

    def __repr__(self) -> str:
        with self._condition:
            state = self._state
        return (
            "AliyunSandboxExecutionBackend(session_id={!r}, state={!r})"
        ).format(self._session_id, state)


__all__ = [
    "ALIYUN_SANDBOX_OWNER_METADATA",
    "ALIYUN_SANDBOX_ROLE_METADATA_KEY",
    "ALIYUN_SANDBOX_EXECUTION_ROLE",
    "ALIYUN_SANDBOX_PERSISTENCE_ROLE",
    "ALIYUN_SANDBOX_SESSION_METADATA_KEY",
    "AliyunSandboxExecutionBackend",
    "ExecutionBackend",
    "ExecutionBackendClosedError",
    "ExecutionBackendCloseError",
    "ExecutionBackendCreateError",
    "ExecutionBackendError",
    "ExecutionLease",
    "LocalExecutionBackend",
    "WorkspaceDescription",
    "WorkspaceInitializer",
    "WorkspaceLease",
]
