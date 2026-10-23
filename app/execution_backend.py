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
    SandboxRecoveryProvider,
    cleanup_session_sandboxes,
)
from app.sandbox_config import SandboxProbeDisabledError, SandboxSettings
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

    @property
    def session_id(self) -> str:
        """Return the immutable host-session identity without creating a Sandbox."""
        return self._session_id

    def ensure_ready(self) -> ExecutionLease:
        with self._condition:
            while self._state in {"creating", "closing"}:
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
            self._state = "creating"

        execution_candidate: Any | None = None
        persistence_candidate: Any | None = None
        lease: ExecutionLease | None = None
        transport: SandboxTransport | None = None
        create_error: BaseException | None = None
        try:
            self._settings.require_enabled()
            cleanup_session_sandboxes(
                self._settings,
                self._session_id,
                provider=self._recovery_provider,
            )
            owner_key, owner_value = ALIYUN_SANDBOX_OWNER_METADATA
            common_metadata = {
                owner_key: owner_value,
                ALIYUN_SANDBOX_SESSION_METADATA_KEY: self._session_id,
            }
            execution_metadata = {
                **common_metadata,
                ALIYUN_SANDBOX_ROLE_METADATA_KEY: ALIYUN_SANDBOX_EXECUTION_ROLE,
            }
            execution_candidate = self._sandbox_factory(
                self._settings, execution_metadata
            )
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
            persistence_candidate = self._persistence_sandbox_factory(
                self._settings, persistence_metadata
            )
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
                    kill_result = candidate.kill()
                    if kill_result is not True:
                        direct_cleanup_failed = True
                except BaseException as cleanup_error:
                    if not isinstance(cleanup_error, Exception):
                        cleanup_system_error = cleanup_error
                    else:
                        direct_cleanup_failed = True
            if direct_cleanup_failed or cleanup_system_error is not None:
                try:
                    cleanup_session_sandboxes(
                        self._settings,
                        self._session_id,
                        provider=self._recovery_provider,
                    )
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
            self._condition.notify_all()
            return lease

    def close(self) -> None:
        with self._condition:
            while self._state in {"creating", "closing"}:
                self._condition.wait()
            if self._state == "closed":
                return None
            if self._state in {"new", "failed"}:
                self._state = "closed"
                self._condition.notify_all()
                return None
            assert self._state == "ready"
            assert self._execution_sandbox is not None
            assert self._persistence_sandbox is not None
            sandboxes = (
                self._execution_sandbox,
                self._persistence_sandbox,
            )
            self._state = "closing"

        direct_failures: list[BaseException] = []
        recovery_failure: BaseException | None = None
        try:
            # Stop execution first, then the host-only persistence endpoint.
            # A failure for either resource never prevents the other attempt.
            for sandbox in sandboxes:
                try:
                    result = sandbox.kill()
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
            with self._condition:
                self._execution_sandbox = None
                self._persistence_sandbox = None
                self._transport = None
                self._state = "closed"
                self._condition.notify_all()

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
        with self._condition:
            if self._state != "ready" or self._transport is None:
                raise ExecutionBackendClosedError(
                    "Sandbox execution backend 尚未就绪或已关闭。"
                )
            transport = self._transport
            self._active_operations += 1
        try:
            return operation(transport)
        finally:
            with self._condition:
                self._active_operations -= 1
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
            return self._state == "closed"

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
