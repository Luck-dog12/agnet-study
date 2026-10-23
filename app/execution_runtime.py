"""Session-scoped all-or-nothing routing for workspace execution tools.

The Registry and Agent use this internal seam to keep local execution unchanged
while binding remote workspace calls, one-shot capabilities, cancellation and
unknown-outcome fencing to exactly one Sandbox generation.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
from secrets import token_bytes
from threading import Lock, RLock
from typing import Final, Literal

from pydantic import BaseModel

from app.aliyun_sandbox_tools import AliyunSandboxToolGroup
from app.execution_backend import (
    AliyunSandboxExecutionBackend,
    ExecutionBackend,
    LocalExecutionBackend,
)
from app.schemas import (
    BashShellArguments,
    CodeInterpreterArguments,
    EditorFileArguments,
    GrepArguments,
    ListFilesArguments,
    ReadFileArguments,
    WriteFileArguments,
)
from app.sandbox_transport import SandboxRunHandle
from app.permissions import PermissionMode, validate_permission_mode
from app.workspace_versions import (
    PreparedWorkspaceCommandMutation,
    PreparedWorkspaceMutation,
    RemoteCommandMutationCandidate,
    RemoteMutationCandidate,
    WorkspaceVersionFencingRequired,
)


WORKSPACE_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "read_file",
        "write_file",
        "editor_file",
        "list_files",
        "grep",
        "bash_shell",
        "code_interpreter",
    }
)
_UNAUTHORIZED_TOOLS: Final[frozenset[str]] = frozenset(
    {"read_file", "list_files", "grep"}
)
_AUTHORIZATION_KINDS: Final[dict[str, Literal["approval", "policy"]]] = {
    "write_file": "approval",
    "editor_file": "approval",
    "bash_shell": "approval",
    "code_interpreter": "policy",
}
_ARGUMENT_MODELS: Final[dict[str, type[BaseModel]]] = {
    "read_file": ReadFileArguments,
    "write_file": WriteFileArguments,
    "editor_file": EditorFileArguments,
    "list_files": ListFilesArguments,
    "grep": GrepArguments,
    "bash_shell": BashShellArguments,
    "code_interpreter": CodeInterpreterArguments,
}


class ExecutionRuntimeError(RuntimeError):
    """Base error for internal execution-runtime routing."""


class ExecutionRuntimeClosedError(ExecutionRuntimeError):
    """The session-scoped runtime has crossed its terminal boundary."""


class LocalRegistryDelegationRequired(ExecutionRuntimeError):
    """The default local route must continue through the existing Registry."""


class RemoteExecutionAuthorizationDenied(ExecutionRuntimeError):
    """A remote privileged operation lacks an exact one-shot capability."""


class _RemoteExecutionPermit:
    __slots__ = (
        "runtime_identity",
        "generation",
        "tool_name",
        "arguments_digest",
        "authorization_kind",
        "nonce",
        "consumed",
    )

    def __init__(
        self,
        *,
        runtime_identity: object,
        generation: int,
        tool_name: str,
        arguments_digest: str,
        authorization_kind: Literal["approval", "policy"],
        nonce: bytes,
    ) -> None:
        self.runtime_identity = runtime_identity
        self.generation = generation
        self.tool_name = tool_name
        self.arguments_digest = arguments_digest
        self.authorization_kind = authorization_kind
        self.nonce = nonce
        self.consumed = False

    def __repr__(self) -> str:
        return "_RemoteExecutionPermit(private, consumed={!r})".format(
            self.consumed
        )


class _WorkspaceReservation:
    """One idempotently releasable claim on the remote workspace data plane."""

    __slots__ = ("_gate", "_lock", "_released")

    def __init__(self, gate: Lock) -> None:
        self._gate = gate
        self._lock = RLock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            self._gate.release()


class PreparedRemoteExecution:
    """Exact, one-shot remote invocation prepared by the trusted host thread."""

    __slots__ = (
        "_runtime",
        "_generation",
        "_group",
        "_tool_name",
        "_arguments",
        "_permit",
        "_lock",
        "_state",
        "_handle",
        "_dispatch_attempted",
    )

    def __init__(
        self,
        *,
        runtime: "ExecutionToolRuntime",
        generation: int,
        group: AliyunSandboxToolGroup,
        tool_name: str,
        arguments: BaseModel,
        permit: _RemoteExecutionPermit,
    ) -> None:
        self._runtime = runtime
        self._generation = generation
        self._group = group
        self._tool_name = tool_name
        self._arguments = arguments
        self._permit = permit
        self._lock = RLock()
        self._state = "prepared"
        self._handle: SandboxRunHandle | None = None
        self._dispatch_attempted = False

    def _mark_dispatch_attempted(self) -> None:
        """Cross the exact provider-dispatch boundary once.

        The transport calls this immediately before its background SDK call.
        A cancellation that wins before this point prevents dispatch; after
        this point, any missing handle is an unknown remote outcome and the
        whole runtime must be fenced.
        """
        with self._lock:
            if self._state == "cancelled":
                raise ExecutionRuntimeClosedError("远端执行已取消。")
            if self._state != "starting" or self._dispatch_attempted:
                raise ExecutionRuntimeError("远端执行派发状态无效。")
            self._dispatch_attempted = True

    def _bind_handle(self, handle: SandboxRunHandle) -> None:
        if not callable(getattr(handle, "wait", None)) or not callable(
            getattr(handle, "cancel", None)
        ):
            raise ExecutionRuntimeError("远端执行没有返回可取消句柄。")
        cancel_now = False
        with self._lock:
            if self._state == "cancelled":
                cancel_now = True
            elif self._state != "starting" or self._handle is not None:
                raise ExecutionRuntimeError("远端执行句柄绑定状态无效。")
            else:
                self._handle = handle
                self._state = "running"
        if cancel_now:
            try:
                handle.cancel()
            except BaseException:
                self._runtime._fence_remote_uncertainty()
            raise ExecutionRuntimeClosedError("远端执行已取消。")

    def run(self) -> BaseModel:
        with self._lock:
            if self._state == "cancelled":
                raise ExecutionRuntimeClosedError("远端执行已取消。")
            if self._state != "prepared":
                raise ExecutionRuntimeError("prepared remote invocation 只能执行一次。")
            self._state = "starting"
        reservation = self._runtime._reserve_workspace(self._generation)
        try:
            output = self._runtime._invoke_group(
                self._group,
                self._tool_name,
                self._arguments,
                authorization=self._permit,
                run_handle_consumer=self._bind_handle,
                dispatch_attempt_consumer=self._mark_dispatch_attempted,
            )
        except BaseException:
            with self._lock:
                outcome_unknown = self._dispatch_attempted
                cancelled = self._state == "cancelled"
                if not cancelled:
                    self._state = "failed"
            if outcome_unknown and not cancelled:
                self._runtime._fence_remote_uncertainty()
            raise
        finally:
            reservation.release()
        with self._lock:
            if self._state == "cancelled":
                raise ExecutionRuntimeClosedError("远端执行迟到结果已拒绝。")
            if self._state not in {"starting", "running"}:
                raise ExecutionRuntimeError("远端执行完成状态无效。")
            self._runtime._require_generation(self._generation)
            self._state = "completed"
            return output

    def cancel(self) -> None:
        handle: SandboxRunHandle | None
        dispatch_without_handle = False
        with self._lock:
            if self._state in {"completed", "cancelled"}:
                return
            if self._state == "failed":
                return
            self._state = "cancelled"
            handle = self._handle
            dispatch_without_handle = self._dispatch_attempted and handle is None
        self._runtime._revoke_authorization(self._permit)
        if dispatch_without_handle:
            # AsyncTaskManager intentionally swallows cancel-hook failures.
            # Fence and destroy synchronously while the dispatch outcome is
            # still unknown, before returning to that best-effort caller.
            self._runtime._fence_remote_uncertainty()
            return
        if handle is None:
            return
        try:
            handle.cancel()
        except BaseException:
            # AsyncTaskManager intentionally treats cancel hooks as best effort.
            # Fence and destroy here, before that exception can be swallowed.
            self._runtime._fence_remote_uncertainty()

    def __repr__(self) -> str:
        with self._lock:
            state = self._state
        return f"PreparedRemoteExecution(tool={self._tool_name!r}, state={state!r})"


class PreparedRemoteMutation:
    """In-memory candidate bound to one runtime generation and exact arguments."""

    __slots__ = (
        "_runtime", "_generation", "_tool_name", "_arguments", "_prepared",
        "_reservation", "_lock", "_state",
    )

    def __init__(
        self,
        *,
        runtime: "ExecutionToolRuntime",
        generation: int,
        tool_name: str,
        arguments: BaseModel,
        prepared: PreparedWorkspaceMutation,
        reservation: _WorkspaceReservation,
    ) -> None:
        self._runtime = runtime
        self._generation = generation
        self._tool_name = tool_name
        self._arguments = arguments
        self._prepared = prepared
        self._reservation = reservation
        self._lock = RLock()
        self._state = "prepared"

    @property
    def diff_preview(self) -> str:
        return self._prepared.diff_preview

    @property
    def path(self) -> str:
        return self._prepared.candidate.path

    @property
    def operation_id(self) -> str:
        return self._prepared.candidate.operation_id

    def commit(self) -> BaseModel:
        with self._lock:
            if self._state != "prepared":
                raise ExecutionRuntimeError("远端 mutation 候选只能提交一次。")
            self._state = "committing"
        try:
            output = self._runtime._commit_prepared_mutation(
                self._generation,
                self._tool_name,
                self._arguments,
                self._prepared.candidate,
            )
        except BaseException:
            with self._lock:
                self._state = "failed"
            raise
        finally:
            self._reservation.release()
        with self._lock:
            self._state = "committed"
        return output

    def discard(self) -> None:
        with self._lock:
            if self._state == "prepared":
                self._state = "discarded"
        self._reservation.release()

    def __repr__(self) -> str:
        with self._lock:
            state = self._state
        return f"PreparedRemoteMutation(tool={self._tool_name!r}, state={state!r})"


class PreparedRemoteCommandMutation:
    """Multi-file command candidate holding the workspace reservation through approval."""

    __slots__ = (
        "_runtime", "_generation", "_arguments", "_prepared", "_reservation",
        "_lock", "_state",
    )

    def __init__(
        self,
        *,
        runtime: "ExecutionToolRuntime",
        generation: int,
        arguments: BashShellArguments,
        prepared: PreparedWorkspaceCommandMutation,
        reservation: _WorkspaceReservation,
    ) -> None:
        self._runtime = runtime
        self._generation = generation
        self._arguments = arguments
        self._prepared = prepared
        self._reservation = reservation
        self._lock = RLock()
        self._state = "prepared"

    @property
    def diff_preview(self) -> str:
        return self._prepared.diff_preview

    @property
    def paths(self) -> tuple[str, ...]:
        return self._prepared.candidate.requested_paths

    @property
    def operation_id(self) -> str:
        return self._prepared.candidate.operation_id

    def commit(self) -> BaseModel:
        with self._lock:
            if self._state != "prepared":
                raise ExecutionRuntimeError("远端 command mutation 候选只能提交一次。")
            self._state = "committing"
        try:
            output = self._runtime._commit_prepared_command_mutation(
                self._generation,
                self._arguments,
                self._prepared.candidate,
            )
        except BaseException:
            with self._lock:
                self._state = "failed"
            raise
        finally:
            self._reservation.release()
        with self._lock:
            self._state = "committed"
        return output

    def discard(self) -> None:
        with self._lock:
            if self._state == "prepared":
                self._state = "discarded"
        self._reservation.release()

    def __repr__(self) -> str:
        with self._lock:
            state = self._state
        return f"PreparedRemoteCommandMutation(state={state!r})"


def _validated_arguments(tool_name: str, arguments: BaseModel) -> BaseModel:
    model = _ARGUMENT_MODELS.get(tool_name)
    if model is None or not isinstance(arguments, BaseModel):
        raise ExecutionRuntimeError("工作区工具名称或参数类型无效。")
    try:
        return model.model_validate(arguments.model_dump(mode="python"))
    except Exception:
        raise ExecutionRuntimeError("工作区工具参数无法安全验证。") from None


def _arguments_digest(tool_name: str, arguments: BaseModel) -> str:
    validated = _validated_arguments(tool_name, arguments)
    try:
        encoded = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ExecutionRuntimeError("工作区工具参数无法建立授权摘要。") from None
    return sha256(tool_name.encode("ascii") + b"\x00" + encoded).hexdigest()


class ExecutionToolRuntime:
    """Own one immutable local or Aliyun route for all workspace tools."""

    def __init__(self, backend: ExecutionBackend | None = None) -> None:
        selected = LocalExecutionBackend() if backend is None else backend
        if type(selected) not in {
            LocalExecutionBackend,
            AliyunSandboxExecutionBackend,
        }:
            raise TypeError(
                "backend 必须是 LocalExecutionBackend 或 AliyunSandboxExecutionBackend。"
            )
        self._backend = selected
        self._remote = isinstance(selected, AliyunSandboxExecutionBackend)
        self._session_id = selected.session_id if self._remote else None
        self._identity = object()
        self._generation = 0
        self._state = "open"
        self._cleanup_started = False
        self._lock = RLock()
        self._workspace_gate = Lock()
        self._outstanding: dict[bytes, _RemoteExecutionPermit] = {}
        self._remote_group = (
            AliyunSandboxToolGroup(
                selected,
                authorization_consumer=self._consume_authorization,
            )
            if self._remote
            else None
        )

    @property
    def backend_name(self) -> str:
        return "aliyun-sandbox" if self._remote else "local"

    @property
    def session_id(self) -> str | None:
        """Return the immutable remote session binding without making cloud calls."""
        return self._session_id

    @property
    def workspace_tool_names(self) -> frozenset[str]:
        return WORKSPACE_TOOL_NAMES

    def route_for(self, tool_name: str) -> Literal["local-registry", "aliyun-sandbox"]:
        if tool_name not in WORKSPACE_TOOL_NAMES:
            raise ExecutionRuntimeError("工具不属于工作区执行工具组。")
        return "aliyun-sandbox" if self._remote else "local-registry"

    def ensure_ready(self):
        with self._lock:
            self._require_open()
        return self._backend.ensure_ready()

    def execute(self, tool_name: str, arguments: BaseModel) -> BaseModel:
        """Execute an unprivileged remote tool or require local Registry delegation."""
        validated = _validated_arguments(tool_name, arguments)
        with self._lock:
            self._require_open()
            if not self._remote:
                raise LocalRegistryDelegationRequired(
                    "本地工具继续由现有 Registry 执行。"
                )
            if tool_name not in _UNAUTHORIZED_TOOLS:
                raise RemoteExecutionAuthorizationDenied(
                    "远端工具需要宿主控制面即时授权。"
                )
            group = self._remote_group
        assert group is not None
        reservation = self._reserve_workspace()
        try:
            return self._invoke_group(group, tool_name, validated)
        finally:
            reservation.release()

    def execute_authorized(
        self,
        tool_name: str,
        arguments: BaseModel,
        authorization_callback: Callable[[], bool],
    ) -> BaseModel:
        """Authorize, mint, and immediately consume one remote capability."""
        validated = _validated_arguments(tool_name, arguments)
        expected_kind = _AUTHORIZATION_KINDS.get(tool_name)
        if expected_kind is None:
            raise RemoteExecutionAuthorizationDenied(
                "该远端工具不接受授权执行入口。"
            )
        if not callable(authorization_callback):
            raise RemoteExecutionAuthorizationDenied("远端工具缺少宿主授权回调。")
        with self._lock:
            self._require_open()
            if not self._remote:
                raise LocalRegistryDelegationRequired(
                    "本地工具继续由现有 Registry 执行。"
                )
            group = self._remote_group
        if tool_name in {"write_file", "editor_file"}:
            raise RemoteExecutionAuthorizationDenied(
                "远端文件变更必须使用专用 mutation transaction。"
            )
        try:
            authorized = authorization_callback()
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise RemoteExecutionAuthorizationDenied("宿主授权过程失败。") from None
        if authorized is not True:
            raise RemoteExecutionAuthorizationDenied("宿主没有授权远端工具执行。")
        permit = self._issue_authorization(tool_name, validated, expected_kind)
        assert group is not None
        if tool_name in {"bash_shell", "code_interpreter"}:
            return PreparedRemoteExecution(
                runtime=self,
                generation=permit.generation,
                group=group,
                tool_name=tool_name,
                arguments=validated,
                permit=permit,
            ).run()
        reservation = self._reserve_workspace(permit.generation)
        try:
            return self._invoke_group(
                group,
                tool_name,
                validated,
                authorization=permit,
            )
        finally:
            reservation.release()

    def prepare_async_authorized(
        self,
        tool_name: str,
        arguments: BaseModel,
        authorization_callback: Callable[[], bool],
    ) -> PreparedRemoteExecution:
        """Complete policy/approval now and return an exact one-shot invocation."""
        validated = _validated_arguments(tool_name, arguments)
        expected_kind = _AUTHORIZATION_KINDS.get(tool_name)
        if tool_name not in {"bash_shell", "code_interpreter"} or expected_kind is None:
            raise RemoteExecutionAuthorizationDenied(
                "该远端工具不接受异步授权执行入口。"
            )
        if not callable(authorization_callback):
            raise RemoteExecutionAuthorizationDenied("远端工具缺少宿主授权回调。")
        with self._lock:
            self._require_open()
            if not self._remote:
                raise LocalRegistryDelegationRequired(
                    "本地工具继续由现有 Registry 执行。"
                )
            group = self._remote_group
        try:
            authorized = authorization_callback()
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise RemoteExecutionAuthorizationDenied("宿主授权过程失败。") from None
        if authorized is not True:
            raise RemoteExecutionAuthorizationDenied("宿主没有授权远端工具执行。")
        permit = self._issue_authorization(tool_name, validated, expected_kind)
        assert group is not None
        return PreparedRemoteExecution(
            runtime=self,
            generation=permit.generation,
            group=group,
            tool_name=tool_name,
            arguments=validated,
            permit=permit,
        )

    def prepare_remote_mutation(
        self,
        tool_name: str,
        arguments: BaseModel,
        *,
        operation_id: str,
        permission_mode: PermissionMode,
    ) -> PreparedRemoteMutation:
        """Build one remote candidate without changing active or durable state."""
        validated = _validated_arguments(tool_name, arguments)
        if tool_name not in {"write_file", "editor_file"}:
            raise RemoteExecutionAuthorizationDenied(
                "该工具不接受远端 mutation 候选入口。"
            )
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 64
            or any(character not in "0123456789abcdef" for character in operation_id)
        ):
            raise RemoteExecutionAuthorizationDenied("远端 operation_id 无效。")
        mode = validate_permission_mode(permission_mode)
        digest = _arguments_digest(tool_name, validated)
        with self._lock:
            self._require_open()
            if not self._remote:
                raise LocalRegistryDelegationRequired(
                    "本地工具继续由现有 Registry 执行。"
                )
            generation = self._generation
        reservation = self._reserve_workspace(generation)
        try:
            prepared = self._backend.prepare_workspace_mutation(  # type: ignore[attr-defined]
                tool_name=tool_name,
                arguments=validated,
                operation_id=operation_id,
                arguments_digest=digest,
                permission_mode=mode,
                runtime_generation=generation,
            )
        except BaseException:
            reservation.release()
            raise
        try:
            if type(prepared) is not PreparedWorkspaceMutation:
                raise ExecutionRuntimeError("远端 mutation candidate 返回类型无效。")
            candidate = prepared.candidate
            if (
                type(candidate) is not RemoteMutationCandidate
                or candidate.runtime_generation != generation
                or candidate.tool_name != tool_name
                or candidate.arguments_digest != digest
                or candidate.permission_mode != mode
                or candidate.operation_id != operation_id
            ):
                raise ExecutionRuntimeError("远端 mutation candidate 绑定无效。")
            self._require_generation(generation)
        except BaseException:
            reservation.release()
            raise
        return PreparedRemoteMutation(
            runtime=self,
            generation=generation,
            tool_name=tool_name,
            arguments=validated,
            prepared=prepared,
            reservation=reservation,
        )

    def prepare_remote_command_mutation(
        self,
        arguments: BashShellArguments,
        *,
        operation_id: str,
        permission_mode: PermissionMode,
    ) -> PreparedRemoteCommandMutation:
        validated = _validated_arguments("bash_shell", arguments)
        if not isinstance(validated, BashShellArguments) or validated.command != "format_json":
            raise RemoteExecutionAuthorizationDenied(
                "该命令不接受远端 command mutation 候选入口。"
            )
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 64
            or any(character not in "0123456789abcdef" for character in operation_id)
        ):
            raise RemoteExecutionAuthorizationDenied("远端 operation_id 无效。")
        mode = validate_permission_mode(permission_mode)
        digest = _arguments_digest("bash_shell", validated)
        with self._lock:
            self._require_open()
            if not self._remote:
                raise LocalRegistryDelegationRequired("本地工具继续由现有 Registry 执行。")
            generation = self._generation
        reservation = self._reserve_workspace(generation)
        try:
            prepared = self._backend.prepare_workspace_command_mutation(  # type: ignore[attr-defined]
                tool_name="bash_shell",
                arguments=validated,
                operation_id=operation_id,
                arguments_digest=digest,
                permission_mode=mode,
                runtime_generation=generation,
            )
        except WorkspaceVersionFencingRequired:
            reservation.release()
            self._fence_remote_uncertainty()
            raise ExecutionRuntimeClosedError(
                "远端 command candidate 修改了 active workspace，runtime 已隔离并销毁。"
            ) from None
        except BaseException:
            reservation.release()
            raise
        try:
            if type(prepared) is not PreparedWorkspaceCommandMutation:
                raise ExecutionRuntimeError("远端 command mutation candidate 返回类型无效。")
            candidate = prepared.candidate
            if (
                type(candidate) is not RemoteCommandMutationCandidate
                or candidate.runtime_generation != generation
                or candidate.tool_name != "bash_shell"
                or candidate.arguments_digest != digest
                or candidate.permission_mode != mode
                or candidate.operation_id != operation_id
            ):
                raise ExecutionRuntimeError("远端 command mutation candidate 绑定无效。")
            self._require_generation(generation)
        except BaseException:
            reservation.release()
            raise
        return PreparedRemoteCommandMutation(
            runtime=self,
            generation=generation,
            arguments=validated,
            prepared=prepared,
            reservation=reservation,
        )

    def _commit_prepared_command_mutation(
        self,
        generation: int,
        arguments: BashShellArguments,
        candidate: RemoteCommandMutationCandidate,
    ) -> BaseModel:
        self._require_generation(generation)
        permit = self._issue_authorization("bash_shell", arguments, "approval")
        self._consume_authorization("bash_shell", arguments, permit, "approval")
        try:
            output = self._backend.commit_workspace_command_mutation(  # type: ignore[attr-defined]
                candidate,
                runtime_generation=generation,
            )
        except WorkspaceVersionFencingRequired:
            self._fence_remote_uncertainty()
            raise ExecutionRuntimeClosedError(
                "远端 command mutation 结果不确定，runtime 已隔离并销毁。"
            ) from None
        self._require_generation(generation)
        if not isinstance(output, BaseModel):
            raise ExecutionRuntimeError("远端 command mutation 输出类型无效。")
        return output

    def _commit_prepared_mutation(
        self,
        generation: int,
        tool_name: str,
        arguments: BaseModel,
        candidate: RemoteMutationCandidate,
    ) -> BaseModel:
        self._require_generation(generation)
        permit = self._issue_authorization(tool_name, arguments, "approval")
        # Consume immediately at the host commit boundary.  The candidate is
        # independently bound to the same tool, argument digest and runtime
        # generation by the coordinator.
        self._consume_authorization(tool_name, arguments, permit, "approval")
        try:
            output = self._backend.commit_workspace_mutation(  # type: ignore[attr-defined]
                candidate,
                runtime_generation=generation,
            )
        except WorkspaceVersionFencingRequired:
            self._fence_remote_uncertainty()
            raise ExecutionRuntimeClosedError(
                "远端 mutation 结果不确定，runtime 已隔离并销毁。"
            ) from None
        self._require_generation(generation)
        if not isinstance(output, BaseModel):
            raise ExecutionRuntimeError("远端 mutation 输出类型无效。")
        return output

    def _issue_authorization(
        self,
        tool_name: str,
        arguments: BaseModel,
        authorization_kind: Literal["approval", "policy"],
    ) -> _RemoteExecutionPermit:
        """Private seam used by the immediate authorize-and-dispatch path."""
        expected_kind = _AUTHORIZATION_KINDS.get(tool_name)
        if expected_kind != authorization_kind:
            raise RemoteExecutionAuthorizationDenied("远端授权类型与工具不匹配。")
        digest = _arguments_digest(tool_name, arguments)
        with self._lock:
            self._require_open()
            if not self._remote:
                raise RemoteExecutionAuthorizationDenied("本地 runtime 不签发远端授权。")
            nonce = token_bytes(32)
            while nonce in self._outstanding:
                nonce = token_bytes(32)
            permit = _RemoteExecutionPermit(
                runtime_identity=self._identity,
                generation=self._generation,
                tool_name=tool_name,
                arguments_digest=digest,
                authorization_kind=authorization_kind,
                nonce=nonce,
            )
            self._outstanding[nonce] = permit
            return permit

    def _consume_authorization(
        self,
        tool_name: str,
        arguments: BaseModel,
        authorization: object | None,
        authorization_kind: str,
    ) -> None:
        try:
            digest = _arguments_digest(tool_name, arguments)
        except ExecutionRuntimeError:
            raise RemoteExecutionAuthorizationDenied("远端授权参数无效。") from None
        with self._lock:
            self._require_open()
            if (
                type(authorization) is not _RemoteExecutionPermit
                or authorization.runtime_identity is not self._identity
                or authorization.generation != self._generation
                or authorization.tool_name != tool_name
                or authorization.arguments_digest != digest
                or authorization.authorization_kind != authorization_kind
                or authorization.consumed
                or len(authorization.nonce) != 32
                or self._outstanding.get(authorization.nonce) is not authorization
            ):
                raise RemoteExecutionAuthorizationDenied(
                    "远端授权无效、绑定不匹配或已经消费。"
                )
            self._outstanding.pop(authorization.nonce)
            authorization.consumed = True

    def _revoke_authorization(self, authorization: _RemoteExecutionPermit) -> None:
        with self._lock:
            if type(authorization) is not _RemoteExecutionPermit:
                return
            if self._outstanding.get(authorization.nonce) is authorization:
                self._outstanding.pop(authorization.nonce, None)
                authorization.consumed = True

    def _require_generation(self, generation: int) -> None:
        with self._lock:
            self._require_open()
            if generation != self._generation:
                raise ExecutionRuntimeClosedError(
                    "远端执行 generation 已失效。"
                )

    def _reserve_workspace(self, generation: int | None = None) -> _WorkspaceReservation:
        """Serialize every remote workspace observer/mutator without blocking close."""
        with self._lock:
            self._require_open()
            expected = self._generation if generation is None else generation
            if expected != self._generation:
                raise ExecutionRuntimeClosedError("远端执行 generation 已失效。")
        self._workspace_gate.acquire()
        reservation = _WorkspaceReservation(self._workspace_gate)
        try:
            self._require_generation(expected)
        except BaseException:
            reservation.release()
            raise
        return reservation

    def _fence_remote_uncertainty(self) -> None:
        """Synchronously fence new work, then best-effort destroy the Sandbox."""
        self.begin_close()
        try:
            self._backend.close()
        finally:
            with self._lock:
                self._state = "closed"

    @staticmethod
    def _invoke_group(
        group: AliyunSandboxToolGroup,
        tool_name: str,
        arguments: BaseModel,
        *,
        authorization: object | None = None,
        run_handle_consumer: Callable[[SandboxRunHandle], None] | None = None,
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> BaseModel:
        methods = {
            "read_file": group.read_file,
            "write_file": group.write_file,
            "editor_file": group.editor_file,
            "list_files": group.list_files,
            "grep": group.grep,
            "bash_shell": group.command,
            "code_interpreter": group.code_interpreter,
        }
        method = methods[tool_name]
        if tool_name in _UNAUTHORIZED_TOOLS:
            return method(arguments)  # type: ignore[arg-type,return-value]
        if (
            tool_name in {"bash_shell", "code_interpreter"}
            and (
                run_handle_consumer is not None
                or dispatch_attempt_consumer is not None
            )
        ):
            if run_handle_consumer is None or dispatch_attempt_consumer is None:
                raise ExecutionRuntimeError("远端执行生命周期消费者不完整。")
            return method(  # type: ignore[arg-type,call-arg,return-value]
                arguments,
                authorization=authorization,
                run_handle_consumer=run_handle_consumer,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        return method(  # type: ignore[arg-type,call-arg,return-value]
            arguments,
            authorization=authorization,
        )

    def _require_open(self) -> None:
        if self._state != "open":
            raise ExecutionRuntimeClosedError("执行 runtime 已关闭。")

    def begin_close(self) -> None:
        """Install the close fence synchronously without performing remote cleanup."""
        with self._lock:
            if self._state != "open":
                return
            self._state = "closing"
            self._generation += 1
            self._outstanding.clear()

    def close(self) -> None:
        self.begin_close()
        with self._lock:
            if self._state == "closed" or self._cleanup_started:
                return
            self._cleanup_started = True
            group = self._remote_group
        try:
            if group is not None:
                group.close()
            else:
                self._backend.close()
        finally:
            with self._lock:
                self._state = "closed"

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state != "open"

    def __repr__(self) -> str:
        return "ExecutionToolRuntime(backend={!r}, closed={!r})".format(
            self.backend_name,
            self.closed,
        )


__all__ = [
    "ExecutionRuntimeClosedError",
    "ExecutionRuntimeError",
    "ExecutionToolRuntime",
    "LocalRegistryDelegationRequired",
    "PreparedRemoteExecution",
    "PreparedRemoteInvocation",
    "PreparedRemoteMutation",
    "RemoteExecutionAuthorizationDenied",
    "WORKSPACE_TOOL_NAMES",
]


# The design name used by callers and documentation.  Keep the original
# class name as a compatibility alias for the interrupted implementation.
PreparedRemoteInvocation = PreparedRemoteExecution
