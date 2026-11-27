"""主机文件变更的一次或两阶段审批状态机。

审批只在同步调用栈内有效，并绑定精确工具、操作和规范化项目路径。diff 阶段
额外绑定候选 UUID 与 diff 哈希；无回调、拒绝、异常或调用结束都会复位。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Any, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.async_tasks import AsyncTaskEvent, has_started_without_terminal_task
from app.daemon_executor import start_daemon_cleanup
from app.schemas import (
    AsyncTaskAcceptedOutput,
    AsyncTaskAcceptedEventData,
    AsyncTaskCancelRequestedEventData,
    AsyncTaskCancelledEventData,
    AsyncTaskCompletedEventData,
    AsyncTaskFailedEventData,
    AsyncTaskLateResultEventData,
    AsyncTaskProgressEventData,
    AsyncTaskStartedEventData,
    AssistantAttemptEventData,
    AssistantMessage,
    AssistantMessageEventData,
    AssistantStreamSnapshot,
    CompactionSummaryEventData,
    ControlHostEventData,
    FileApprovalState,
    FileApprovalStatus,
    Message,
    NormalHostEventData,
    RecoveredToolState,
    RecoveryResolutionEventData,
    ResolveRecoveryArguments,
    SessionAssistantMessageSnapshot,
    SessionEvent,
    SessionEventStructuredView,
    SessionRecoveryReport,
    SessionToolCallSnapshot,
    SessionToolResultSnapshot,
    SessionUserMessageSnapshot,
    StepEndEventData,
    StepStartEventData,
    SystemMessage,
    ToolCall,
    ToolCallEventData,
    ToolMessage,
    ToolResult,
    ToolResultEventData,
    TurnEndEventData,
    TurnStartEventData,
    UserMessage,
    UserMessageEventData,
    UrgentHostEventData,
)


T = TypeVar("T")


def runtime_async_events(events: Iterable[SessionEvent]) -> tuple[AsyncTaskEvent, ...]:
    """Purely convert durable session events to the shared task projection."""
    converted: list[AsyncTaskEvent] = []
    for event in events:
        if not isinstance(event, SessionEvent):
            raise TypeError("异步任务投影只接受 SessionEvent。")
        data = event.data
        common = {
            "kind": event.type,
            "task_id": getattr(data, "task_id", ""),
            "session_id": event.session_id,
            "occurred_at": event.time,
        }
        if isinstance(data, AsyncTaskAcceptedEventData):
            converted.append(AsyncTaskEvent(
                **common,
                turn_id=data.turn_id,
                step=data.step,
                call_id=data.call_id,
                tool_name=data.tool_name,
                request_json=data.request_json,
                retry_safety=data.retry_safety,
                resource_kind=data.resource_kind,
                cancellable=data.cancellable,
            ))
        elif isinstance(data, AsyncTaskStartedEventData):
            converted.append(AsyncTaskEvent(**common))
        elif isinstance(data, AsyncTaskProgressEventData):
            converted.append(AsyncTaskEvent(
                **common,
                phase=data.phase,
                completed=data.completed,
                total=data.total,
                progress=data.progress,
                message=data.message,
            ))
        elif isinstance(data, AsyncTaskCompletedEventData):
            converted.append(AsyncTaskEvent(
                **common,
                result_status=data.result.status,
                result_json=data.result.result_json,
            ))
        elif isinstance(data, AsyncTaskFailedEventData):
            converted.append(AsyncTaskEvent(
                **common,
                error=data.error,
                result_status=None if data.result is None else data.result.status,
                result_json=None if data.result is None else data.result.result_json,
                recovery_status=data.recovery_status,
            ))
        elif isinstance(
            data,
            (AsyncTaskCancelRequestedEventData, AsyncTaskCancelledEventData),
        ):
            converted.append(AsyncTaskEvent(**common, reason=data.reason))
        elif isinstance(data, AsyncTaskLateResultEventData):
            converted.append(AsyncTaskEvent(
                **common,
                late_status=data.status,
                result_status=None if data.result is None else data.result.status,
                result_json=None if data.result is None else data.result.result_json,
            ))
    return tuple(converted)


def has_started_without_terminal_async_task(
    events: Iterable[SessionEvent],
) -> bool:
    """Project durable task history and detect dispatched work with no terminal."""
    return has_started_without_terminal_task(runtime_async_events(events))


class WorkflowApprovalDenied(RuntimeError):
    """审批未建立或已失效；调用方必须拒绝文件变更。"""


@dataclass(frozen=True)
class ToolConfirmationRequest:
    """提供给宿主 UI 的审批内容，不包含文件正文或绝对路径。"""

    tool_name: str
    operation: str
    paths: tuple[str, ...]
    description: str
    request_id: str
    phase: str = "operation"
    diff_preview: str | None = None
    pending_file: str | None = None

    @property
    def command(self) -> str:
        """兼容原 bash_shell 审批 UI 的字段名。"""
        return self.operation


ConfirmationCallback = Callable[[ToolConfirmationRequest], bool]


class FileApprovalWorkflow:
    """保存每个文件的当前状态，并消费一次性、精确绑定的批准。"""

    def __init__(self) -> None:
        self._states: dict[str, FileApprovalState] = {}
        self._active_request_id: str | None = None
        self._lock = RLock()

    def state_for(self, path: str) -> FileApprovalState:
        """返回深拷贝；首次查询的文件默认是未批准。"""
        with self._lock:
            state = self._states.setdefault(path, FileApprovalState(path=path))
            return state.model_copy(deep=True)

    def run_approved(
        self,
        *,
        tool_name: str,
        operation: str,
        paths: Iterable[str],
        description: str,
        confirmation_callback: ConfirmationCallback | None,
        action: Callable[[], T],
        verify_before_execute: Callable[[], None] | None = None,
    ) -> T:
        """完成 pending→approved→执行→unapproved；任何异常都撤销状态。"""
        normalized_paths = tuple(paths)
        if not normalized_paths or len(set(normalized_paths)) != len(normalized_paths):
            raise WorkflowApprovalDenied("审批路径无效。")
        if confirmation_callback is None:
            raise WorkflowApprovalDenied("文件变更需要用户确认。")

        request_id = uuid4().hex
        request = ToolConfirmationRequest(
            tool_name=tool_name,
            operation=operation,
            paths=normalized_paths,
            description=description,
            request_id=request_id,
        )
        with self._lock:
            if self._active_request_id is not None:
                raise WorkflowApprovalDenied("已有文件审批正在进行。")
            self._active_request_id = request_id
            for path in normalized_paths:
                self._states[path] = FileApprovalState(
                    path=path,
                    status=FileApprovalStatus.PENDING,
                    request_id=request_id,
                    tool_name=tool_name,
                    operation=operation,
                )

        try:
            try:
                approved = confirmation_callback(request)
            except Exception:
                raise WorkflowApprovalDenied("用户确认过程失败。") from None
            if approved is not True:
                raise WorkflowApprovalDenied("用户未批准文件变更。")

            with self._lock:
                self._require_exact_binding(request)
                for path in normalized_paths:
                    self._states[path] = FileApprovalState(
                        path=path,
                        status=FileApprovalStatus.APPROVED,
                        request_id=request_id,
                        tool_name=tool_name,
                        operation=operation,
                    )
                self._require_exact_binding(request, approved=True)

            if verify_before_execute is not None:
                verify_before_execute()
            with self._lock:
                self._require_exact_binding(request, approved=True)
            return action()
        finally:
            with self._lock:
                for path in normalized_paths:
                    self._states[path] = FileApprovalState(path=path)
                if self._active_request_id == request_id:
                    self._active_request_id = None

    def run_diff_approved(
        self,
        *,
        tool_name: str,
        operation: str,
        paths: Iterable[str],
        description: str,
        confirmation_callback: ConfirmationCallback | None,
        prepare: Callable[[str], tuple[T, str]],
        commit: Callable[[T], T],
        verify_before_prepare: Callable[[], None] | None = None,
        verify_before_commit: Callable[[], None] | None = None,
        discard_after_commit: Callable[[T], None] | None = None,
        diff_only_confirmation: bool = False,
        pending_file_id_override: str | None = None,
        pending_file_label: str | None = None,
    ) -> T:
        """两阶段批准：先批准生成候选，再批准 diff，最后才提交正式文件。

        ``prepare`` 必须只生成受保护的候选文件和有界 diff，不能修改正式目标。
        第二次拒绝、确认异常或提交失败都保留候选文件，便于用户后续检查。
        """
        normalized_paths = tuple(paths)
        if not normalized_paths or len(set(normalized_paths)) != len(normalized_paths):
            raise WorkflowApprovalDenied("审批路径无效。")
        if confirmation_callback is None:
            raise WorkflowApprovalDenied("文件变更需要用户确认。")

        request_id = uuid4().hex
        first_request = ToolConfirmationRequest(
            tool_name=tool_name,
            operation=operation,
            paths=normalized_paths,
            description=description,
            request_id=request_id,
        )
        with self._lock:
            if self._active_request_id is not None:
                raise WorkflowApprovalDenied("已有文件审批正在进行。")
            self._active_request_id = request_id
            self._set_states(first_request, FileApprovalStatus.PENDING)

        staged: T | None = None
        try:
            if (
                not diff_only_confirmation
                and self._confirm(confirmation_callback, first_request) is not True
            ):
                raise WorkflowApprovalDenied("用户未批准生成文件候选。")
            with self._lock:
                self._require_exact_binding(first_request)
                self._set_states(first_request, FileApprovalStatus.APPROVED)
                self._require_exact_binding(first_request, approved=True)

            if verify_before_prepare is not None:
                verify_before_prepare()
            with self._lock:
                self._require_exact_binding(first_request, approved=True)

            pending_file_id = (
                uuid4().hex
                if pending_file_id_override is None
                else pending_file_id_override
            )
            if (
                not isinstance(pending_file_id, str)
                or len(pending_file_id) != 32
                or any(character not in "0123456789abcdef" for character in pending_file_id)
            ):
                raise WorkflowApprovalDenied("候选标识无效。")
            staged, diff_preview = prepare(pending_file_id)
            if not isinstance(diff_preview, str) or not diff_preview:
                raise WorkflowApprovalDenied("无法建立可审批的 diff。")
            diff_sha256 = sha256(diff_preview.encode("utf-8")).hexdigest()
            diff_request = ToolConfirmationRequest(
                tool_name=tool_name,
                operation=operation,
                paths=normalized_paths,
                description="审阅文件 diff 并提交正式文件",
                request_id=request_id,
                phase="diff",
                diff_preview=diff_preview,
                pending_file=(
                    f"data/pending/{pending_file_id}.bin"
                    if pending_file_label is None
                    else pending_file_label
                ),
            )
            with self._lock:
                self._require_exact_binding(first_request, approved=True)
                self._set_states(
                    diff_request,
                    FileApprovalStatus.DIFF_PENDING,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )

            if self._confirm(confirmation_callback, diff_request) is not True:
                raise WorkflowApprovalDenied("用户未批准 diff，正式文件保持不变。")
            with self._lock:
                self._require_exact_binding(
                    diff_request,
                    expected_status=FileApprovalStatus.DIFF_PENDING,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
                self._set_states(
                    diff_request,
                    FileApprovalStatus.DIFF_APPROVED,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
                self._require_exact_binding(
                    diff_request,
                    expected_status=FileApprovalStatus.DIFF_APPROVED,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )

            if verify_before_commit is not None:
                verify_before_commit()
            with self._lock:
                self._require_exact_binding(
                    diff_request,
                    expected_status=FileApprovalStatus.DIFF_APPROVED,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
            result = commit(staged)
            if discard_after_commit is not None:
                try:
                    discard_after_commit(staged)
                except Exception:
                    # 正式文件已完成且验证成功；候选清理失败时保留候选比误删更安全。
                    pass
            return result
        finally:
            with self._lock:
                for path in normalized_paths:
                    self._states[path] = FileApprovalState(path=path)
                if self._active_request_id == request_id:
                    self._active_request_id = None

    def run_recovery_diff_approved(
        self,
        *,
        tool_name: str,
        operation: str,
        paths: Iterable[str],
        pending_file_id: str,
        diff_preview: str,
        confirmation_callback: ConfirmationCallback | None,
        verify_before_commit: Callable[[], None],
        action: Callable[[], T],
        discard_after_commit: Callable[[], None] | None = None,
    ) -> T:
        """对已有候选执行一次 diff 审批；不生成或改写候选。"""
        normalized_paths = tuple(paths)
        if not normalized_paths or len(set(normalized_paths)) != len(normalized_paths):
            raise WorkflowApprovalDenied("审批路径无效。")
        if confirmation_callback is None:
            raise WorkflowApprovalDenied("恢复候选需要用户确认。")
        if not isinstance(diff_preview, str) or not diff_preview:
            raise WorkflowApprovalDenied("无法建立可审批的 diff。")

        request_id = uuid4().hex
        diff_sha256 = sha256(diff_preview.encode("utf-8")).hexdigest()
        request = ToolConfirmationRequest(
            tool_name=tool_name,
            operation=operation,
            paths=normalized_paths,
            description="审阅以前保留的文件 diff 并提交正式文件",
            request_id=request_id,
            phase="recovery_diff",
            diff_preview=diff_preview,
            pending_file=f"data/pending/{pending_file_id}.bin",
        )
        with self._lock:
            if self._active_request_id is not None:
                raise WorkflowApprovalDenied("已有文件审批正在进行。")
            self._active_request_id = request_id
            self._set_states(
                request,
                FileApprovalStatus.RECOVERY_DIFF_PENDING,
                pending_file_id=pending_file_id,
                diff_sha256=diff_sha256,
            )
        try:
            if self._confirm(confirmation_callback, request) is not True:
                raise WorkflowApprovalDenied("用户未批准已有候选，正式文件保持不变。")
            with self._lock:
                self._require_exact_binding(
                    request,
                    expected_status=FileApprovalStatus.RECOVERY_DIFF_PENDING,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
                self._set_states(
                    request,
                    FileApprovalStatus.RECOVERY_DIFF_APPROVED,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
                self._require_exact_binding(
                    request,
                    expected_status=FileApprovalStatus.RECOVERY_DIFF_APPROVED,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
            verify_before_commit()
            with self._lock:
                self._require_exact_binding(
                    request,
                    expected_status=FileApprovalStatus.RECOVERY_DIFF_APPROVED,
                    pending_file_id=pending_file_id,
                    diff_sha256=diff_sha256,
                )
            result = action()
            if discard_after_commit is not None:
                try:
                    discard_after_commit()
                except Exception:
                    pass
            return result
        finally:
            with self._lock:
                for path in normalized_paths:
                    self._states[path] = FileApprovalState(path=path)
                if self._active_request_id == request_id:
                    self._active_request_id = None

    @staticmethod
    def _confirm(
        callback: ConfirmationCallback,
        request: ToolConfirmationRequest,
    ) -> bool:
        try:
            return callback(request)
        except Exception:
            raise WorkflowApprovalDenied("用户确认过程失败。") from None

    def _set_states(
        self,
        request: ToolConfirmationRequest,
        status: FileApprovalStatus,
        *,
        pending_file_id: str | None = None,
        diff_sha256: str | None = None,
    ) -> None:
        for path in request.paths:
            self._states[path] = FileApprovalState(
                path=path,
                status=status,
                request_id=request.request_id,
                tool_name=request.tool_name,
                operation=request.operation,
                pending_file_id=pending_file_id,
                diff_sha256=diff_sha256,
            )

    def _require_exact_binding(
        self,
        request: ToolConfirmationRequest,
        *,
        approved: bool = False,
        expected_status: FileApprovalStatus | None = None,
        pending_file_id: str | None = None,
        diff_sha256: str | None = None,
    ) -> None:
        status = expected_status or (
            FileApprovalStatus.APPROVED if approved else FileApprovalStatus.PENDING
        )
        if self._active_request_id != request.request_id:
            raise WorkflowApprovalDenied("审批请求已经失效。")
        for path in request.paths:
            state = self._states.get(path)
            if state is None or (
                state.status,
                state.request_id,
                state.tool_name,
                state.operation,
                state.pending_file_id,
                state.diff_sha256,
            ) != (
                status,
                request.request_id,
                request.tool_name,
                request.operation,
                pending_file_id,
                diff_sha256,
            ):
                raise WorkflowApprovalDenied("审批绑定已经变化。")


class SessionEventError(RuntimeError):
    """会话事实源无法被安全读取或可靠提交。"""


class SessionStoreBusyError(SessionEventError):
    """选定会话的单写者租约已由另一个进程持有。"""


class _SessionBackendMismatch(SessionEventError):
    """The caller requested a backend different from the durable binding."""


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CURRENT_SESSION_DIRECTORY = _PROJECT_ROOT / "data" / "sessions" / "current"
_EVENT_FILE_PATTERN = re.compile(r"^([0-9]{16})\.json$")
_TEMP_FILE_PATTERN = re.compile(r"^\.[0-9]{16}\.[0-9a-f]{32}\.tmp$")
_SANDBOX_BUDGET_FILES = frozenset({"sandbox-budget.json", "sandbox-budget.json.lock"})
_SANDBOX_BUDGET_TEMP_PATTERN = re.compile(r"sandbox-budget\.json\.[0-9a-f]{32}\.tmp\Z")
_EXECUTION_BINDING_FILE = "execution.json"
_EXECUTION_BINDING_MAX_BYTES = 1024
_MAX_EVENT_BYTES = 16 * 1024 * 1024
_MAX_SESSION_EVENTS = 1_000_000
_EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    "turn/start": TurnStartEventData,
    "turn/end": TurnEndEventData,
    "step/start": StepStartEventData,
    "step/end": StepEndEventData,
    "user/message": UserMessageEventData,
    "assistant/message": AssistantMessageEventData,
    "assistant/attempt": AssistantAttemptEventData,
    "tool/call": ToolCallEventData,
    "tool/result": ToolResultEventData,
    "recovery/resolution": RecoveryResolutionEventData,
    "async/task/accepted": AsyncTaskAcceptedEventData,
    "async/task/started": AsyncTaskStartedEventData,
    "async/task/progress": AsyncTaskProgressEventData,
    "async/task/completed": AsyncTaskCompletedEventData,
    "async/task/failed": AsyncTaskFailedEventData,
    "async/task/cancel_requested": AsyncTaskCancelRequestedEventData,
    "async/task/cancelled": AsyncTaskCancelledEventData,
    "async/task/late_result": AsyncTaskLateResultEventData,
    "host/normal": NormalHostEventData,
    "host/urgent": UrgentHostEventData,
    "host/control": ControlHostEventData,
    "compaction/summary": CompactionSummaryEventData,
}
_DIRECT_RETRY_TOOLS = frozenset(
    {
        "read_file", "list_files", "grep", "list_pending_changes",
        "code_interpreter", "web_fetch", "web_search", "get_async_task",
        "inspect_recovery", "resolve_recovery", "load_skill", "read_skill_resource",
    }
)
_DIRECT_RETRY_SHELL_COMMANDS = frozenset({"compileall", "validate_json"})


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _json_native(value: object) -> object:
    """转换 SDK/Pydantic JSON 数据，并拒绝会在 JSON 中悄悄改变语义的值。"""
    if isinstance(value, BaseModel):
        if type(value).__module__ != "app.schemas":
            return _json_native(value.model_dump(mode="python"))
        # 只把本项目冻结 schema 明确声明的序列字段转换为 JSON array。
        # 不使用 mode=json，避免 datetime/bytes/Decimal 等被静默改写。
        converted = {}
        for name in type(value).model_fields:
            item = getattr(value, name)
            if isinstance(item, tuple) and value.model_config.get("frozen") and name in {
                "stream", "tool_calls", "repaired", "interrupted_turns"
            }:
                converted[name] = [_json_native(child) for child in item]
            else:
                converted[name] = _json_native(item)
        return converted
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("JSON 数字必须有限")
        return value
    if type(value) is list:
        return [_json_native(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object 的键必须是字符串")
        return {key: _json_native(item) for key, item in value.items()}
    raise TypeError("值不能无损表示为 JSON")


def canonical_json_snapshot(value: object) -> str:
    """生成并复核规范 JSON；不可无损往返的数据直接拒绝。"""
    try:
        native = _json_native(value)
        encoded = json.dumps(
            native,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded, parse_constant=_reject_json_constant)
        verified = json.dumps(
            decoded,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        encoded_size = len(encoded.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise SessionEventError("事件包含无法可靠保存的数据。") from None
    if verified != encoded or decoded != native or encoded_size > _MAX_EVENT_BYTES:
        raise SessionEventError("事件 JSON 快照无法无损往返或超过大小限制。")
    return encoded


def tool_retry_safety(call: ToolCall | SessionToolCallSnapshot) -> Literal[
    "safe", "inspect_external"
]:
    """只有明确白名单中的纯读取/验证操作能在恢复后重新发起。"""
    if call.name in _DIRECT_RETRY_TOOLS:
        return "safe"
    if call.name == "bash_shell":
        raw = call.arguments if isinstance(call, ToolCall) else json.loads(call.arguments_json)
        if isinstance(raw, dict) and raw.get("command") in _DIRECT_RETRY_SHELL_COMMANDS:
            return "safe"
    return "inspect_external"


def tool_call_snapshot(call: ToolCall) -> SessionToolCallSnapshot:
    return SessionToolCallSnapshot(
        id=call.id,
        name=call.name,
        arguments_json=canonical_json_snapshot(call.arguments),
    )


def assistant_message_snapshot(message: AssistantMessage) -> SessionAssistantMessageSnapshot:
    return SessionAssistantMessageSnapshot(
        content=message.content,
        tool_calls=tuple(tool_call_snapshot(call) for call in message.tool_calls),
        reasoning_content=message.reasoning_content,
    )


def tool_result_snapshot(result: ToolResult) -> SessionToolResultSnapshot:
    return SessionToolResultSnapshot(
        status=result.status,
        result_json=canonical_json_snapshot(result),
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode, left.st_nlink) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
    )


def _safe_regular_file(path: Path, *, missing_ok: bool) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse_point(path)
        or info.st_nlink != 1
    ):
        raise OSError("会话文件状态不安全")
    return info


def _read_exact_file(path: Path, expected: os.stat_result, *, maximum: int) -> bytes:
    if expected.st_size > maximum:
        raise OSError("会话文件超过大小限制")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        current = _safe_regular_file(path, missing_ok=False)
        if current is None or not _same_identity(expected, opened) or not _same_identity(opened, current):
            raise OSError("会话文件身份发生变化")
        chunks: list[bytes] = []
        remaining = expected.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = _safe_regular_file(path, missing_ok=False)
        if (
            len(raw) != expected.st_size or not _same_identity(opened, after)
            or current is None or not _same_identity(after, current)
            or (expected.st_size, expected.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            != (expected.st_size, expected.st_mtime_ns, expected.st_ctime_ns)
        ):
            raise OSError("会话文件读取期间发生变化")
        return raw
    finally:
        os.close(descriptor)


def _validate_session_sequence(events: Iterable[SessionEvent]) -> None:
    """验证轮/步骤/工具关联；允许最后一轮因崩溃保持打开。"""
    sequence = tuple(events)
    current_turn: str | None = None
    open_step: tuple[str, int] | None = None
    next_step = 0
    user_seen = False
    assistant_for_step = False
    assistant_calls: dict[tuple[str, int, str], SessionToolCallSnapshot] = {}
    top_calls: set[tuple[str, int, str]] = set()
    results: dict[tuple[str, int, str], str] = {}
    result_statuses: dict[tuple[str, int, str], str] = {}
    resolved: set[tuple[str, int, str]] = set()
    seen_turns: set[str] = set()
    summaries: list[tuple[int, int]] = []
    async_tasks: dict[str, AsyncTaskAcceptedEventData] = {}
    async_accepted_seq: dict[str, int] = {}
    async_calls: set[tuple[str, int, str]] = set()
    async_started: set[str] = set()
    async_cancel_requested: set[str] = set()
    async_terminal: dict[str, str] = {}
    async_terminal_seq: dict[str, int] = {}
    async_late_results: set[str] = set()
    async_progress: dict[str, tuple[str, int, int, float]] = {}
    async_unknown_calls: set[tuple[str, int, str]] = set()
    attempt_for_step = False
    final_answer = False
    async_followup_allowed = False
    for expected_seq, event in enumerate(sequence, 1):
        if event.seq != expected_seq:
            raise ValueError("会话事件序号不连续")
        data = event.data
        if isinstance(data, TurnStartEventData):
            if current_turn is not None or open_step is not None or data.turn_id in seen_turns:
                raise ValueError("轮边界发生嵌套")
            seen_turns.add(data.turn_id)
            current_turn = data.turn_id
            next_step = 0
            user_seen = False
            final_answer = False
        elif isinstance(data, UserMessageEventData):
            if current_turn != data.turn_id or user_seen or open_step is not None:
                raise ValueError("用户消息不在有效轮起点")
            user_seen = True
        elif isinstance(data, StepStartEventData):
            if (
                current_turn != data.turn_id
                or not user_seen
                or open_step is not None
                or data.step != next_step
                or (final_answer and not async_followup_allowed)
            ):
                raise ValueError("步骤起点状态无效")
            open_step = (data.turn_id, data.step)
            assistant_for_step = False
            attempt_for_step = False
            async_followup_allowed = False
        elif isinstance(data, AssistantMessageEventData):
            if open_step != (data.turn_id, data.step) or assistant_for_step or attempt_for_step:
                raise ValueError("模型消息不在有效步骤中")
            assistant_for_step = True
            final_answer = not data.message.tool_calls
            for call in data.message.tool_calls:
                key = (data.turn_id, data.step, call.id)
                if key in assistant_calls:
                    raise ValueError("同一步骤工具调用 id 重复")
                assistant_calls[key] = call
        elif isinstance(data, AssistantAttemptEventData):
            if open_step != (data.turn_id, data.step) or assistant_for_step or attempt_for_step:
                raise ValueError("失败尝试不在有效步骤中")
            attempt_for_step = True
        elif isinstance(data, ToolCallEventData):
            key = (data.turn_id, data.step, data.call.id)
            expected = assistant_calls.get(key)
            if (
                open_step != (data.turn_id, data.step)
                or expected != data.call
                or key in top_calls
                or key in results
            ):
                raise ValueError("顶层工具调用与模型请求不匹配")
            if data.retry_safety != tool_retry_safety(data.call):
                raise ValueError("工具恢复风险分类不正确")
            top_calls.add(key)
        elif isinstance(data, ToolResultEventData):
            key = (data.turn_id, data.step, data.call_id)
            expected = assistant_calls.get(key)
            if (
                open_step != (data.turn_id, data.step)
                or expected is None
                or key in results
            ):
                raise ValueError("工具结果与模型请求不匹配")
            has_top_call = key in top_calls
            if data.recovery_status == "not_started" and has_top_call:
                raise ValueError("未启动工具不能已有顶层调用")
            if data.recovery_status != "not_started" and not has_top_call:
                raise ValueError("工具结果缺少顶层调用")
            if data.recovery_status in {"confirmed_applied", "confirmed_not_applied"}:
                raise ValueError("外部检查结论必须使用追加恢复事件")
            is_async = key in async_calls
            if (data.result.status == "accepted") != is_async:
                raise ValueError("异步工具调用必须且只能写入 accepted 占位结果")
            results[key] = data.recovery_status
            result_statuses[key] = data.result.status
        elif isinstance(data, AsyncTaskAcceptedEventData):
            key = (data.turn_id, data.step, data.call_id)
            expected = assistant_calls.get(key)
            if (
                data.task_id in async_tasks
                or key in async_calls
                or key not in top_calls
                or expected is None
                or expected.name != data.tool_name
                or expected.arguments_json != data.request_json
                or data.retry_safety != tool_retry_safety(expected)
            ):
                raise ValueError("后台任务接受事件与工具调用不匹配")
            async_tasks[data.task_id] = data
            async_accepted_seq[data.task_id] = event.seq
            async_calls.add(key)
        elif isinstance(data, AsyncTaskStartedEventData):
            accepted = async_tasks.get(data.task_id)
            key = None if accepted is None else (
                accepted.turn_id, accepted.step, accepted.call_id
            )
            if (
                accepted is None
                or data.task_id in async_started
                or data.task_id in async_terminal
                or key not in results
                or result_statuses.get(key) != "accepted"
                or data.task_id in async_cancel_requested
            ):
                raise ValueError("后台任务开始事件状态无效")
            async_started.add(data.task_id)
        elif isinstance(data, AsyncTaskProgressEventData):
            if data.task_id not in async_started or data.task_id in async_terminal:
                raise ValueError("后台任务进度事件状态无效")
            previous = async_progress.get(data.task_id)
            if previous is not None:
                previous_phase, previous_completed, previous_total, previous_progress = previous
                if data.progress < previous_progress:
                    raise ValueError("后台任务总进度不能倒退")
                if data.phase == previous_phase and (
                    data.total != previous_total or data.completed < previous_completed
                ):
                    raise ValueError("后台任务同阶段进度必须单调且总量固定")
            async_progress[data.task_id] = (
                data.phase, data.completed, data.total, data.progress
            )
        elif isinstance(data, AsyncTaskCancelRequestedEventData):
            if (
                data.task_id not in async_tasks
                or data.task_id in async_terminal
                or data.task_id in async_cancel_requested
            ):
                raise ValueError("后台任务取消请求状态无效")
            async_cancel_requested.add(data.task_id)
        elif isinstance(data, AsyncTaskCompletedEventData):
            if (
                data.task_id not in async_started
                or data.task_id in async_terminal
                or data.task_id in async_cancel_requested
            ):
                raise ValueError("后台任务完成事件状态无效")
            async_terminal[data.task_id] = "completed"
            async_terminal_seq[data.task_id] = event.seq
        elif isinstance(data, AsyncTaskFailedEventData):
            expected_recovery = (
                "not_started" if data.task_id not in async_started else "outcome_unknown"
            )
            if (
                data.task_id not in async_tasks
                or data.task_id in async_terminal
                or data.task_id in async_cancel_requested
                or (
                    data.recovery_status == "recorded"
                    and data.task_id not in async_started
                )
                or (
                    data.recovery_status != "recorded"
                    and data.recovery_status != expected_recovery
                )
            ):
                raise ValueError("后台任务失败事件状态无效")
            async_terminal[data.task_id] = "failed"
            async_terminal_seq[data.task_id] = event.seq
            accepted = async_tasks[data.task_id]
            if (
                data.recovery_status == "outcome_unknown"
                and accepted.retry_safety == "inspect_external"
            ):
                async_unknown_calls.add(
                    (accepted.turn_id, accepted.step, accepted.call_id)
                )
        elif isinstance(data, AsyncTaskCancelledEventData):
            if (
                data.task_id not in async_tasks
                or data.task_id not in async_cancel_requested
                or data.task_id in async_terminal
            ):
                raise ValueError("后台任务取消事件状态无效")
            async_terminal[data.task_id] = "cancelled"
            async_terminal_seq[data.task_id] = event.seq
        elif isinstance(data, AsyncTaskLateResultEventData):
            if (
                async_terminal.get(data.task_id) != "cancelled"
                or data.task_id in async_late_results
            ):
                raise ValueError("迟到结果只能追加到已取消任务且只能记录一次")
            async_late_results.add(data.task_id)
        elif isinstance(data, (NormalHostEventData, UrgentHostEventData, ControlHostEventData)):
            pass
        elif isinstance(data, StepEndEventData):
            if open_step != (data.turn_id, data.step):
                raise ValueError("步骤结束边界无效")
            step_call_ids = {key for key in assistant_calls if key[:2] == open_step}
            if assistant_for_step and not step_call_ids.issubset(results):
                raise ValueError("步骤结束前仍有未闭合工具调用")
            if data.kind == "completed" and not assistant_for_step:
                raise ValueError("完成步骤缺少完整回答")
            if data.kind == "awaiting_async":
                has_turn_async_task = any(
                    accepted.turn_id == data.turn_id
                    for accepted in async_tasks.values()
                )
                if not assistant_for_step or not final_answer or not has_turn_async_task:
                    raise ValueError("等待后台结果的步骤状态无效")
                async_followup_allowed = True
            open_step = None
            next_step += 1
        elif isinstance(data, TurnEndEventData):
            if current_turn != data.turn_id or open_step is not None:
                raise ValueError("轮结束边界无效")
            if data.kind == "completed" and (
                not user_seen or not final_answer or async_followup_allowed
            ):
                raise ValueError("完成轮缺少用户消息或最终回答")
            current_turn = None
        elif isinstance(data, RecoveryResolutionEventData):
            key = (data.turn_id, data.step, data.call_id)
            if key in resolved:
                raise ValueError("外部状态结论位置无效")
            if current_turn is not None:
                if open_step is None:
                    raise ValueError("轮内恢复结论必须位于打开的模型步骤")
                resolvers: list[tuple[str, int, str]] = []
                for resolver_key, resolver in assistant_calls.items():
                    if (
                        resolver_key[:2] != open_step
                        or resolver.name != "resolve_recovery"
                        or resolver_key not in top_calls
                        or resolver_key in results
                    ):
                        continue
                    try:
                        arguments = ResolveRecoveryArguments.model_validate_json(
                            resolver.arguments_json
                        )
                    except ValidationError:
                        continue
                    if (
                        arguments.turn_id,
                        arguments.step,
                        arguments.call_id,
                    ) == key:
                        resolvers.append(resolver_key)
                if len(resolvers) != 1:
                    raise ValueError("轮内恢复结论缺少唯一且已落盘的 resolve_recovery 调用")
            expected = assistant_calls.get(key)
            if (
                expected is None
                or (
                    results.get(key) != "outcome_unknown"
                    and key not in async_unknown_calls
                )
                or tool_retry_safety(expected) != "inspect_external"
            ):
                raise ValueError("外部状态结论没有对应的未知修改")
            resolved.add(key)
        elif isinstance(data, CompactionSummaryEventData):
            if current_turn is not None or not (
                1 <= data.replace_start_seq <= data.replace_end_seq < event.seq
            ):
                raise ValueError("摘要事件位置或区间无效")
            selected = sequence[data.replace_start_seq - 1 : data.replace_end_seq]
            if not selected or selected[0].type != "turn/start" or selected[-1].type != "turn/end":
                raise ValueError("摘要没有替换完整轮")
            start, end = data.replace_start_seq, data.replace_end_seq
            for task_id, accepted_seq in async_accepted_seq.items():
                terminal_seq = async_terminal_seq.get(task_id)
                intersects = start <= accepted_seq <= end or (
                    terminal_seq is not None and start <= terminal_seq <= end
                )
                if intersects and not (
                    terminal_seq is not None
                    and start <= accepted_seq <= end
                    and start <= terminal_seq <= end
                ):
                    raise ValueError("摘要不能截断后台任务生命周期")
            for old_start, old_end in summaries:
                if start <= old_end and old_start <= end and not (start <= old_start and end >= old_end):
                    raise ValueError("新摘要必须完整覆盖重叠的旧摘要区间")
            summaries = [(a, b) for a, b in summaries if not (start <= a and b <= end)]
            summaries.append((start, end))
    # current_turn/open_step 是合法崩溃尾部，由恢复流程只追加闭合。


class SessionEventWorkflow:
    """DeepSeek Harness 风格的 append-only SessionEvent 工作流。

    持久模式为每个事件写一个原子 JSON 分段，避免 JSONL 崩溃半行。任一损坏、
    序号缺口、链接/硬链接或锁异常都会 fail closed，不会越过坏尾继续写入。
    """

    def __init__(
        self,
        *,
        storage_directory: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        memory_only: bool = False,
        recover: bool = True,
        execution_backend_name: Literal["local", "aliyun-sandbox"] | None = None,
        new_session_backend_name: Literal["local", "aliyun-sandbox"] = "local",
        require_existing_identity: bool = False,
    ) -> None:
        requested_backend = execution_backend_name
        if requested_backend is not None and (
            type(requested_backend) is not str
            or requested_backend not in {"local", "aliyun-sandbox"}
        ):
            raise SessionEventError("执行后端名称无效。")
        if (
            type(new_session_backend_name) is not str
            or new_session_backend_name not in {"local", "aliyun-sandbox"}
        ):
            raise SessionEventError("新会话执行后端名称无效。")
        if type(require_existing_identity) is not bool:
            raise SessionEventError("会话身份要求无效。")
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._thread_lock = RLock()
        self._directory = (
            (Path(os.path.abspath(storage_directory)) if storage_directory is not None else _CURRENT_SESSION_DIRECTORY)
            if not memory_only
            else None
        )
        self._events: list[SessionEvent] = []
        self._lock_descriptor: int | None = None
        self._directory_identities: list[tuple[Path, os.stat_result]] = []
        self._directory_handles: list[int] = []
        self._closed = False
        self._failed = False
        self._session_id = uuid4().hex
        if requested_backend is not None:
            initial_backend = requested_backend
        elif self._directory is None:
            # The configurable migration switch applies only to persistent
            # sessions.  Existing in-memory callers retain their local default.
            initial_backend = "local"
        else:
            initial_backend = new_session_backend_name
        self._execution_backend_name: Literal[
            "local", "aliyun-sandbox"
        ] = initial_backend
        self._recovery_report = SessionRecoveryReport()
        if self._directory is not None:
            try:
                self._prepare_directory()
                self._acquire_writer_lease()
                self._initialize_persistent_identity(
                    requested_backend,
                    new_session_backend_name,
                    require_existing_identity=require_existing_identity,
                )
                self._load_events()
                if recover:
                    self._recovery_report = self._recover_incomplete_tail()
            except SessionStoreBusyError:
                self.close()
                raise
            except _SessionBackendMismatch as exc:
                self.close()
                raise SessionEventError(str(exc)) from None
            except Exception:
                self.close()
                raise SessionEventError("会话存储不可用，已拒绝启动。") from None

    @classmethod
    def in_memory(
        cls,
        *,
        clock: Callable[[], datetime] | None = None,
        execution_backend_name: Literal["local", "aliyun-sandbox"] | None = None,
    ) -> "SessionEventWorkflow":
        return cls(
            clock=clock,
            memory_only=True,
            execution_backend_name=execution_backend_name,
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def execution_backend_name(self) -> Literal["local", "aliyun-sandbox"]:
        return self._execution_backend_name

    @property
    def events(self) -> tuple[SessionEvent, ...]:
        return tuple(self._events)

    def _revoked_model_turns(self) -> frozenset[str]:
        """A failed model continuation revokes its interrupted unfinished ancestors.

        Generic turn errors also represent tool limits/local failures. Those
        keep their task context so a fresh user request can explicitly retry.
        """
        unfinished: set[str] = set()
        revoked: set[str] = set()
        model_failures: set[str] = set()
        for event in self._events:
            data = event.data
            if isinstance(data, TurnStartEventData):
                unfinished.add(data.turn_id)
            elif isinstance(data, AssistantAttemptEventData) and data.kind == "error":
                model_failures.add(data.turn_id)
            elif isinstance(data, TurnEndEventData):
                if data.kind == "error" and data.turn_id in model_failures:
                    revoked.update(unfinished)
                    unfinished.clear()
                elif data.kind != "interrupted":
                    unfinished.clear()
        return frozenset(revoked)

    @property
    def structured_events(self) -> tuple[SessionEventStructuredView, ...]:
        """统一只读视图；不会改变或重新提交底层 v1 事件。"""
        return tuple(event.structured_view for event in self._events)

    @property
    def recovery_report(self) -> SessionRecoveryReport:
        return self._recovery_report

    def _prepare_directory(self) -> None:
        assert self._directory is not None
        chain = [*reversed(self._directory.parents), self._directory]
        for directory in chain:
            try:
                info = os.lstat(directory)
            except FileNotFoundError:
                directory.mkdir()
                info = os.lstat(directory)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _is_reparse_point(directory)
            ):
                raise OSError("会话目录状态不安全")
            self._directory_identities.append((directory, info))
            if os.name == "nt":
                # 无 FILE_SHARE_DELETE：在持有会话期间固定路径组件，阻止替换目录。
                import ctypes
                from ctypes import wintypes

                create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
                create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
                create.restype = wintypes.HANDLE
                handle = create(str(directory), 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise OSError("无法固定会话目录")
                self._directory_handles.append(handle)
        self._verify_storage(check_lock=False)

    def _verify_storage(self, *, check_lock: bool = True) -> None:
        for path, identity in self._directory_identities:
            current = os.lstat(path)
            if (identity.st_dev, identity.st_ino, identity.st_mode) != (
                current.st_dev, current.st_ino, current.st_mode
            ) or _is_reparse_point(path):
                raise OSError("会话目录在运行期间发生变化")
        if check_lock and self._directory is not None:
            if self._lock_descriptor is None:
                raise OSError("会话写锁未持有")
            current = _safe_regular_file(self._directory / "writer.lock", missing_ok=False)
            if current is None or not _same_identity(current, os.fstat(self._lock_descriptor)):
                raise OSError("会话写锁身份发生变化")

    @staticmethod
    def _publish_no_replace(source: Path, target: Path) -> None:
        """在支持的平台上原子发布且不覆盖；Windows 同步移动元数据。"""
        import ctypes

        if os.name == "nt":
            from ctypes import wintypes

            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
            move.restype = wintypes.BOOL
            if not move(str(source), str(target), 0x8):  # WRITE_THROUGH，无 REPLACE_EXISTING
                raise OSError("无法原子发布会话事件")
        elif os.name == "posix":
            libc = ctypes.CDLL(None, use_errno=True)
            rename = getattr(libc, "renameat2", None)
            if rename is None:
                raise OSError("当前平台不支持原子无覆盖发布")
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
                raise OSError("无法原子发布会话事件")
        else:
            raise OSError("当前平台不支持可靠会话存储")

    def _acquire_writer_lease(self) -> None:
        assert self._directory is not None
        lock_path = self._directory / "writer.lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            current = _safe_regular_file(lock_path, missing_ok=False)
            if current is None or not _same_identity(opened, current):
                raise OSError("会话锁文件身份不安全")
            os.lseek(descriptor, 0, os.SEEK_SET)
        except Exception:
            os.close(descriptor)
            raise

        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                os.close(descriptor)
                contention_errors = {
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EBUSY,
                    errno.EDEADLK,
                }
                if exc.errno in contention_errors:
                    raise SessionStoreBusyError("会话已由其它进程占用。") from None
                raise
        elif os.name == "posix":
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError):
                os.close(descriptor)
                raise SessionStoreBusyError("会话已由其它进程占用。") from None
            except OSError:
                os.close(descriptor)
                raise
        else:
            os.close(descriptor)
            raise OSError("当前平台缺少可靠文件锁")

        try:
            if opened.st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            raise
        self._lock_descriptor = descriptor

    def _atomic_publish(self, target: Path, payload: bytes, *, seq: int = 0) -> None:
        assert self._directory is not None
        self._verify_storage()
        if len(payload) > _MAX_EVENT_BYTES:
            raise OSError("会话事件超过大小限制")
        if _safe_regular_file(target, missing_ok=True) is not None:
            raise OSError("会话分段已经存在")
        temporary = self._directory / f".{seq:016d}.{uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            opened = os.fstat(descriptor)
            current = _safe_regular_file(temporary, missing_ok=False)
            if current is None or not _same_identity(opened, current):
                raise OSError("会话临时文件身份不安全")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("会话事件写入失败")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            if _safe_regular_file(target, missing_ok=True) is not None:
                raise OSError("会话分段在提交期间出现")
            self._verify_storage()
            current = _safe_regular_file(temporary, missing_ok=False)
            if current is None or not _same_identity(opened, current):
                raise OSError("会话临时文件被替换")
            self._publish_no_replace(temporary, target)
            self._verify_storage()
            info = _safe_regular_file(target, missing_ok=False)
            if info is None or _read_exact_file(target, info, maximum=_MAX_EVENT_BYTES) != payload:
                raise OSError("会话事件提交后验证失败")
            if os.name == "posix":
                directory_descriptor = os.open(self._directory, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            try:
                if _safe_regular_file(temporary, missing_ok=True) is not None:
                    temporary.unlink()
            except OSError:
                pass

    def _load_or_create_header(self) -> None:
        assert self._directory is not None
        header = self._directory / "session.json"
        info = _safe_regular_file(header, missing_ok=True)
        if info is None:
            if any(_EVENT_FILE_PATTERN.fullmatch(item.name) for item in self._directory.iterdir()):
                raise OSError("已有事件缺少会话头")
            payload = canonical_json_snapshot(
                {"session_id": self._session_id, "version": 1}
            ).encode("utf-8")
            self._atomic_publish(header, payload)
            return
        raw = _read_exact_file(header, info, maximum=1024)
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise OSError("会话头损坏") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"session_id", "version"}
            or type(value.get("version")) is not int or value.get("version") != 1
            or not isinstance(value.get("session_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["session_id"])
            or canonical_json_snapshot(value).encode("utf-8") != raw
        ):
            raise OSError("会话头无效")
        self._session_id = value["session_id"]

    def _execution_binding_payload(
        self, backend_name: Literal["local", "aliyun-sandbox"]
    ) -> bytes:
        return canonical_json_snapshot(
            {
                "backend_name": backend_name,
                "session_id": self._session_id,
                "version": 1,
            }
        ).encode("utf-8")

    def _read_execution_binding(
        self, *, require_session_match: bool
    ) -> tuple[str, Literal["local", "aliyun-sandbox"]]:
        assert self._directory is not None
        path = self._directory / _EXECUTION_BINDING_FILE
        info = _safe_regular_file(path, missing_ok=False)
        assert info is not None
        raw = _read_exact_file(path, info, maximum=_EXECUTION_BINDING_MAX_BYTES)
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError, json.JSONDecodeError):
            raise OSError("执行后端绑定损坏") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"backend_name", "session_id", "version"}
            or type(value.get("version")) is not int
            or value.get("version") != 1
            or type(value.get("session_id")) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", value["session_id"])
            or type(value.get("backend_name")) is not str
            or value["backend_name"] not in {"local", "aliyun-sandbox"}
            or canonical_json_snapshot(value).encode("utf-8") != raw
        ):
            raise OSError("执行后端绑定无效")
        if require_session_match and value["session_id"] != self._session_id:
            raise OSError("执行后端绑定与会话身份不匹配")
        return value["session_id"], value["backend_name"]

    def _create_execution_binding(
        self, backend_name: Literal["local", "aliyun-sandbox"]
    ) -> None:
        assert self._directory is not None
        self._atomic_publish(
            self._directory / _EXECUTION_BINDING_FILE,
            self._execution_binding_payload(backend_name),
        )

    def _require_requested_backend(
        self,
        bound_backend: Literal["local", "aliyun-sandbox"],
        requested_backend: Literal["local", "aliyun-sandbox"] | None,
    ) -> None:
        if requested_backend is not None and bound_backend != requested_backend:
            raise _SessionBackendMismatch("请求的执行后端与会话绑定不匹配。")
        self._execution_backend_name = bound_backend

    def _initialize_persistent_identity(
        self,
        requested_backend: Literal["local", "aliyun-sandbox"] | None,
        new_session_backend: Literal["local", "aliyun-sandbox"],
        *,
        require_existing_identity: bool,
    ) -> None:
        """Load or durably create the session/backend identity before event replay.

        ``None`` adopts an existing validated binding.  Only when neither identity
        file exists does ``new_session_backend`` select the durable initial value;
        neither ``None`` nor that fallback can override an existing identity.
        A new store publishes ``execution.json`` first.  If a process stops before
        publishing ``session.json``, the next opener can safely reuse the sidecar's
        session id instead of mistaking the newly-created header for a legacy store.
        """
        assert self._directory is not None
        header_path = self._directory / "session.json"
        binding_path = self._directory / _EXECUTION_BINDING_FILE
        entries = tuple(self._directory.iterdir())
        for item in entries:
            if item.name in {"session.json", _EXECUTION_BINDING_FILE, "writer.lock"}:
                continue
            if item.name in _SANDBOX_BUDGET_FILES or _SANDBOX_BUDGET_TEMP_PATTERN.fullmatch(item.name):
                _safe_regular_file(item, missing_ok=False)
                continue
            if _EVENT_FILE_PATTERN.fullmatch(item.name) or _TEMP_FILE_PATTERN.fullmatch(
                item.name
            ):
                continue
            raise OSError("会话目录包含未知对象")
        header_exists = _safe_regular_file(header_path, missing_ok=True) is not None
        binding_exists = _safe_regular_file(binding_path, missing_ok=True) is not None
        event_exists = any(
            _EVENT_FILE_PATTERN.fullmatch(item.name)
            for item in entries
        )

        if (
            require_existing_identity
            and not header_exists
            and not binding_exists
        ):
            raise OSError("会话持久身份不存在")

        if header_exists:
            self._load_or_create_header()
            if binding_exists:
                _session_id, bound_backend = self._read_execution_binding(
                    require_session_match=True
                )
            else:
                # A pre-sidecar store is permanently local, even when it has no
                # events.  Persist that migration before comparing the request.
                bound_backend = "local"
                self._create_execution_binding(bound_backend)
            self._require_requested_backend(bound_backend, requested_backend)
            return

        if event_exists:
            # Preserve the established invariant: committed events without their
            # session header are not repairable and must never receive a binding.
            self._load_or_create_header()
            raise AssertionError("unreachable")

        if binding_exists:
            session_id, bound_backend = self._read_execution_binding(
                require_session_match=False
            )
            self._session_id = session_id
            self._require_requested_backend(bound_backend, requested_backend)
            expected_backend = bound_backend
        else:
            expected_backend = (
                new_session_backend
                if requested_backend is None
                else requested_backend
            )
            self._create_execution_binding(expected_backend)
            self._execution_backend_name = expected_backend
        self._load_or_create_header()
        # Re-read both identities after publishing/loading the header.  This
        # closes the window where an uncooperative process could create a
        # different valid header between the initial absence check and publish.
        _session_id, bound_backend = self._read_execution_binding(
            require_session_match=True
        )
        self._require_requested_backend(bound_backend, expected_backend)

    def _load_events(self) -> None:
        assert self._directory is not None
        numbered: list[tuple[int, Path]] = []
        with os.scandir(self._directory) as entries:
            for entry in entries:
                match = _EVENT_FILE_PATTERN.fullmatch(entry.name)
                if match is not None:
                    numbered.append((int(match.group(1)), self._directory / entry.name))
                    continue
                if entry.name in {"session.json", _EXECUTION_BINDING_FILE, "writer.lock"}:
                    continue
                if entry.name in _SANDBOX_BUDGET_FILES or _SANDBOX_BUDGET_TEMP_PATTERN.fullmatch(entry.name):
                    _safe_regular_file(self._directory / entry.name, missing_ok=False)
                    continue
                if _TEMP_FILE_PATTERN.fullmatch(entry.name):
                    _safe_regular_file(self._directory / entry.name, missing_ok=False)
                    continue
                raise OSError("会话目录包含未知对象")
        numbered.sort()
        if len(numbered) > _MAX_SESSION_EVENTS:
            raise OSError("会话事件数量超过限制")
        if [seq for seq, _ in numbered] != list(range(1, len(numbered) + 1)):
            raise OSError("会话事件序号不连续")
        loaded: list[SessionEvent] = []
        for seq, path in numbered:
            info = _safe_regular_file(path, missing_ok=False)
            if info is None:
                raise OSError("会话事件消失")
            raw = _read_exact_file(path, info, maximum=_MAX_EVENT_BYTES)
            try:
                event = SessionEvent.model_validate_json(raw)
            except (UnicodeError, ValidationError, ValueError):
                raise OSError("会话事件损坏") from None
            if event.seq != seq or event.session_id != self._session_id:
                raise OSError("会话事件身份或序号不匹配")
            if canonical_json_snapshot(event).encode("utf-8") != raw:
                raise OSError("会话事件不是规范无损 JSON")
            loaded.append(event)
        try:
            _validate_session_sequence(loaded)
        except ValueError:
            raise OSError("会话事件状态机无效") from None
        self._events = loaded

    def _now(self) -> str:
        now = self._clock()
        if not isinstance(now, datetime):
            raise SessionEventError("会话时钟无效。")
        if now.tzinfo is None:
            raise SessionEventError("会话时钟必须包含时区。")
        return now.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def append_batch(
        self,
        pending: Iterable[tuple[str, BaseModel | dict[str, object]]],
    ) -> tuple[SessionEvent, ...]:
        """整批预检并模拟状态机，再在同一锁内连续可靠发布。

        发布中途失败时，已经确认发布的连续前缀保留在内存和磁盘中；当前实例
        随即 poison。若失败发生在发布成功之后但验证返回之前，只有重新打开并
        回放磁盘才能判断最后一个分段是否已经提交。
        """
        with self._thread_lock:
            if self._closed or self._failed:
                raise SessionEventError("会话工作流已经关闭。")
            try:
                items = tuple(pending)
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise SessionEventError("待追加事件批次无法读取。") from None
            if not items:
                return ()
            if len(self._events) + len(items) > _MAX_SESSION_EVENTS:
                raise SessionEventError("会话事件数量超过限制。")

            validated_items: list[tuple[str, BaseModel]] = []
            try:
                for item in items:
                    if type(item) is not tuple or len(item) != 2:
                        raise TypeError("事件批次元素必须是二元 tuple")
                    event_type, data = item
                    if type(event_type) is not str:
                        raise TypeError("事件类型必须是字符串")
                    model_type = _EVENT_DATA_MODELS.get(event_type)
                    if model_type is None:
                        raise ValueError("事件类型不受支持")
                    data_json = canonical_json_snapshot(data)
                    validated = model_type.model_validate_json(data_json)
                    if canonical_json_snapshot(validated) != data_json:
                        raise ValueError("事件往返不一致")
                    validated_items.append((event_type, validated))
            except (ValidationError, ValueError, TypeError, SessionEventError):
                raise SessionEventError("待追加事件批次无法无损验证。") from None

            prepared: list[tuple[SessionEvent, bytes]] = []
            try:
                next_seq = len(self._events) + 1
                for offset, (event_type, validated) in enumerate(validated_items):
                    event = SessionEvent(
                        session_id=self._session_id,
                        seq=next_seq + offset,
                        time=self._now(),
                        type=event_type,
                        data=validated,
                    )
                    prepared.append(
                        (event, canonical_json_snapshot(event).encode("utf-8"))
                    )
                # 整批状态转换必须在任何文件发布之前成功。
                _validate_session_sequence(
                    (*self._events, *(event for event, _payload in prepared))
                )
            except (ValidationError, ValueError, TypeError, SessionEventError):
                raise SessionEventError("待追加事件批次不符合会话状态机。") from None

            committed: list[SessionEvent] = []
            for event, payload in prepared:
                try:
                    if self._directory is not None:
                        target = self._directory / f"{event.seq:016d}.json"
                        self._atomic_publish(target, payload, seq=event.seq)
                except BaseException as exc:
                    # 磁盘可能已经发布当前事件；必须重新打开回放才能判定。
                    self._failed = True
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    try:
                        raise SessionEventError("会话事件批次无法可靠提交。") from exc
                    finally:
                        # 不把底层路径、磁盘或敏感内容暴露给调用者。
                        exc = None
                self._events.append(event)
                committed.append(event)
            return tuple(committed)

    def append(self, event_type: str, data: BaseModel | dict[str, object]) -> SessionEvent:
        """兼容单条 API；与批量追加共享同一套 fail-closed 实现。"""
        return self.append_batch(((event_type, data),))[0]

    def append_compaction_summary(
        self, *, summary: str, replace_start_seq: int, replace_end_seq: int
    ) -> SessionEvent:
        """只允许用摘要替换一个或多个完整结束的轮；不删除旧事件。"""
        if not (1 <= replace_start_seq <= replace_end_seq <= len(self._events)):
            raise SessionEventError("摘要替换区间超出当前事件流。")
        selected = self._events[replace_start_seq - 1 : replace_end_seq]
        if selected[0].type != "turn/start" or selected[-1].type != "turn/end":
            raise SessionEventError("摘要只能替换完整轮边界。")
        depth = 0
        for event in selected:
            if event.type == "turn/start":
                depth += 1
            elif event.type == "turn/end":
                depth -= 1
            if depth < 0:
                raise SessionEventError("摘要轮边界无效。")
        if depth != 0:
            raise SessionEventError("摘要不能替换未结束的轮。")
        selected_seqs = {event.seq for event in selected}
        accepted_seq: dict[str, int] = {}
        terminal_seq: dict[str, int] = {}
        for event in self._events:
            data = event.data
            if isinstance(data, AsyncTaskAcceptedEventData):
                accepted_seq[data.task_id] = event.seq
            elif isinstance(
                data,
                (AsyncTaskCompletedEventData, AsyncTaskFailedEventData, AsyncTaskCancelledEventData),
            ):
                terminal_seq[data.task_id] = event.seq
        for task_id, start_seq in accepted_seq.items():
            end_seq = terminal_seq.get(task_id)
            touches = start_seq in selected_seqs or (
                end_seq is not None and end_seq in selected_seqs
            )
            if touches and (end_seq is None or start_seq not in selected_seqs or end_seq not in selected_seqs):
                raise SessionEventError("摘要不能截断后台任务生命周期。")
        return self.append(
            "compaction/summary",
            CompactionSummaryEventData(
                summary=summary,
                replace_start_seq=replace_start_seq,
                replace_end_seq=replace_end_seq,
            ),
        )

    @staticmethod
    def _tool_call_from_snapshot(snapshot: SessionToolCallSnapshot) -> ToolCall:
        return ToolCall(
            id=snapshot.id,
            name=snapshot.name,
            arguments=json.loads(snapshot.arguments_json, parse_constant=_reject_json_constant),
        )

    def project_messages(
        self,
        system_prompt: str,
        *,
        allow_incomplete_tool_tail: bool = False,
    ) -> tuple[Message, ...]:
        """从不可变事件重建 SDK 上下文；最新摘要在投影层遮蔽旧事件。

        Chat Completions 要求含 ``tool_calls`` 的 assistant 后紧跟该组的
        全部 tool messages。宿主紧急事件与后台任务终态可在持久化时
        合法地线性化到多个工具结果之间；它们在投影层暂存，并在
        工具组闭合后按原顺序释放。持久化事件本身不会被改写。
        """
        shadowed: set[int] = set()
        replacements: dict[int, CompactionSummaryEventData] = {}
        revoked_turns = self._revoked_model_turns()
        pending_recovery_keys = {
            (item.turn_id, item.step, item.call.id)
            for item in self.unresolved_external_calls()
        }
        async_recovery_bindings = {
            event.data.task_id: (
                event.data.turn_id, event.data.step, event.data.call_id
            )
            for event in self._events
            if isinstance(event.data, AsyncTaskAcceptedEventData)
        }
        projected_recovery_keys: set[tuple[str, int, str]] = set()
        for event in self._events:
            if event.type == "compaction/summary":
                data = event.data
                assert isinstance(data, CompactionSummaryEventData)
                if data.replace_end_seq >= event.seq:
                    raise SessionEventError("摘要不能替换自身或未来事件。")
                # Free-form summaries may repeat a revoked request. Keep the
                # original audited tool facts instead of re-authorizing it.
                if any(
                    getattr(item.data, "turn_id", None) in revoked_turns
                    for item in self._events[data.replace_start_seq - 1:data.replace_end_seq]
                ):
                    continue
                for previous_start in tuple(replacements):
                    if data.replace_start_seq <= previous_start <= data.replace_end_seq:
                        replacements.pop(previous_start)
                shadowed.update(range(data.replace_start_seq, data.replace_end_seq + 1))
                replacements[data.replace_start_seq] = data
        messages: list[Message] = [SystemMessage(content=system_prompt)]
        pending_tool_key: tuple[str, int] | None = None
        pending_tool_ids: frozenset[str] = frozenset()
        seen_tool_ids: set[str] = set()
        deferred_messages: list[Message] = []
        pending_message_index: int | None = None

        def append_delayable(message: Message) -> None:
            if pending_tool_key is None:
                messages.append(message)
            else:
                deferred_messages.append(message)

        def close_tool_group_if_complete() -> None:
            nonlocal pending_tool_key, pending_tool_ids, seen_tool_ids
            nonlocal pending_message_index
            if pending_tool_key is None or seen_tool_ids != pending_tool_ids:
                return
            messages.extend(deferred_messages)
            deferred_messages.clear()
            pending_tool_key = None
            pending_tool_ids = frozenset()
            seen_tool_ids = set()
            pending_message_index = None

        try:
            for event in self._events:
                replacement = replacements.get(event.seq)
                if replacement is not None:
                    if pending_tool_key is not None:
                        raise SessionEventError(
                            "会话摘要不能跨越未闭合的工具消息组。"
                        )
                    messages.append(UserMessage(content=f"此前会话摘要：\n{replacement.summary}"))
                if event.seq in shadowed:
                    continue
                data = event.data
                if isinstance(data, UserMessageEventData):
                    if pending_tool_key is not None:
                        raise SessionEventError(
                            "新用户消息前的工具消息组尚未闭合。"
                        )
                    if data.turn_id in revoked_turns:
                        messages.append(SystemMessage(content=(
                            f"历史轮 {data.turn_id} 因故障已撤销执行授权；原始用户指令已隔离。"
                            "该轮保留的工具调用和结果仅是已发生的事实，不得继续或重放旧任务。"
                            "已产生的副作用未撤销，未知结果仍必须按现有恢复规则核验。"
                        )))
                    else:
                        messages.append(UserMessage(content=data.message.content))
                elif isinstance(data, AssistantMessageEventData):
                    if pending_tool_key is not None:
                        raise SessionEventError(
                            "新模型消息前的工具消息组尚未闭合。"
                        )
                    revoked = data.turn_id in revoked_turns
                    if revoked and not data.message.tool_calls:
                        continue
                    assistant = AssistantMessage(
                        content=None if revoked else data.message.content,
                        tool_calls=[
                            self._tool_call_from_snapshot(call)
                            for call in data.message.tool_calls
                        ],
                        reasoning_content=None if revoked else data.message.reasoning_content,
                    )
                    messages.append(assistant)
                    call_ids = tuple(call.id for call in assistant.tool_calls)
                    if len(call_ids) != len(set(call_ids)):
                        raise SessionEventError("模型工具调用 ID 重复。")
                    if call_ids:
                        pending_tool_key = (data.turn_id, data.step)
                        pending_tool_ids = frozenset(call_ids)
                        seen_tool_ids = set()
                        pending_message_index = len(messages) - 1
                elif isinstance(data, ToolResultEventData):
                    if pending_tool_key is None:
                        raise SessionEventError("工具结果没有对应的模型工具调用。")
                    if (data.turn_id, data.step) != pending_tool_key:
                        raise SessionEventError("工具结果跨越了模型步骤边界。")
                    if (
                        data.call_id not in pending_tool_ids
                        or data.call_id in seen_tool_ids
                    ):
                        raise SessionEventError("工具结果 ID 缺失、重复或不匹配。")
                    result = ToolResult.model_validate_json(data.result.result_json)
                    messages.append(ToolMessage(tool_call_id=data.call_id, result=result))
                    seen_tool_ids.add(data.call_id)
                    recovery_key = (data.turn_id, data.step, data.call_id)
                    if recovery_key in pending_recovery_keys:
                        projected_recovery_keys.add(recovery_key)
                        append_delayable(UserMessage(content=(
                            "待 Agent 自主核验的未知工具结果："
                            f"turn_id={data.turn_id} step={data.step} call_id={data.call_id}。"
                            "请先调用 inspect_recovery，不要重放旧调用。"
                        )))
                    close_tool_group_if_complete()
                elif isinstance(data, AsyncTaskCompletedEventData):
                    result = ToolResult.model_validate_json(data.result.result_json)
                    append_delayable(UserMessage(content=(
                        f"{event.display_marker}\n后台工具任务已完成。\n"
                        f"task_id={data.task_id}\nresult={result.model_dump_json()}"
                    )))
                elif isinstance(data, AsyncTaskFailedEventData):
                    result_text = (
                        ""
                        if data.result is None
                        else f"\nresult={ToolResult.model_validate_json(data.result.result_json).model_dump_json()}"
                    )
                    append_delayable(UserMessage(content=(
                        f"{event.display_marker}\n后台工具任务失败。\n"
                        f"task_id={data.task_id}\nrecovery_status={data.recovery_status}"
                        f"\nerror={data.error}{result_text}"
                    )))
                    recovery_key = async_recovery_bindings.get(data.task_id)
                    if recovery_key in pending_recovery_keys:
                        assert recovery_key is not None
                        projected_recovery_keys.add(recovery_key)
                        append_delayable(UserMessage(content=(
                            "待 Agent 自主核验的未知后台工具结果："
                            f"turn_id={recovery_key[0]} step={recovery_key[1]} "
                            f"call_id={recovery_key[2]}。"
                            "请先调用 inspect_recovery，不要重放旧调用。"
                        )))
                elif isinstance(data, AsyncTaskCancelledEventData):
                    append_delayable(UserMessage(content=(
                        f"{event.display_marker}\n后台工具任务已取消。\n"
                        f"task_id={data.task_id}\nreason={data.reason}"
                    )))
                elif isinstance(data, (NormalHostEventData, UrgentHostEventData)):
                    append_delayable(UserMessage(content=(
                        f"{event.display_marker}\n宿主事件：{data.message}\n"
                        f"source={data.source}\ndetails={data.details_json}"
                    )))
                elif isinstance(data, ControlHostEventData):
                    from app.model_guard import MODEL_GUARD_SOURCE

                    if data.source == MODEL_GUARD_SOURCE:
                        # Local latch controls are audit data, never user input.
                        continue
                    append_delayable(UserMessage(content=(
                        f"{event.display_marker}\n宿主控制事件：{data.message}\n"
                        f"source={data.source}\ncommand={data.command}\n"
                        f"details={data.details_json}"
                    )))
                elif isinstance(data, RecoveryResolutionEventData):
                    append_delayable(UserMessage(content=(
                        f"Agent 外部状态核验：turn={data.turn_id} step={data.step} "
                        f"call_id={data.call_id}；结论={data.resolution}；证据：{data.evidence}"
                    )))
                elif pending_tool_key is not None and event.type in {
                    "turn/start", "step/start", "assistant/attempt",
                    "step/end", "turn/end", "compaction/summary",
                }:
                    raise SessionEventError("工具消息组跨越了不可安全重排的边界。")
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
            raise SessionEventError("会话事件无法重建有效模型上下文。") from None
        if pending_tool_key is not None:
            if allow_incomplete_tool_tail and pending_message_index is not None:
                # During a live turn the append-only workflow publishes the
                # assistant and each result through separate durability
                # barriers. Keep the last fully valid SDK prefix in memory;
                # the next refresh recomputes from all events and publishes
                # the whole group atomically once its final result exists.
                del messages[pending_message_index:]
                return tuple(messages)
            raise SessionEventError("会话结束时仍有未闭合的工具消息组。")
        # 即使显式摘要已遮蔽原工具轮，未解决目标的精确身份也必须留在模型上下文中。
        for turn_id, step, call_id in sorted(
            pending_recovery_keys - projected_recovery_keys
        ):
            messages.append(UserMessage(content=(
                "待 Agent 自主核验的未知工具结果："
                f"turn_id={turn_id} step={step} call_id={call_id}。"
                "请先调用 inspect_recovery，不要重放旧调用。"
            )))
        return tuple(messages)

    def active_async_task_ids(self) -> tuple[str, ...]:
        """返回事件流中已接受但尚无终态的后台任务。"""
        active: dict[str, None] = {}
        for event in self._events:
            data = event.data
            if isinstance(data, AsyncTaskAcceptedEventData):
                active[data.task_id] = None
            elif isinstance(
                data,
                (AsyncTaskCompletedEventData, AsyncTaskFailedEventData, AsyncTaskCancelledEventData),
            ):
                active.pop(data.task_id, None)
        return tuple(active)

    def unresolved_external_calls(self) -> tuple[ToolCallEventData, ...]:
        """返回必须由 Agent 检查外部状态并追加结论的原调用。"""
        calls: dict[tuple[str, int, str], ToolCallEventData] = {}
        unknown: set[tuple[str, int, str]] = set()
        resolved: set[tuple[str, int, str]] = set()
        async_calls: dict[str, tuple[str, int, str, str]] = {}
        for event in self._events:
            data = event.data
            if isinstance(data, ToolCallEventData):
                calls[(data.turn_id, data.step, data.call.id)] = data
            elif isinstance(data, ToolResultEventData):
                key = (data.turn_id, data.step, data.call_id)
                if data.recovery_status == "outcome_unknown" and key in calls and calls[key].retry_safety == "inspect_external":
                    unknown.add(key)
            elif isinstance(data, AsyncTaskAcceptedEventData):
                async_calls[data.task_id] = (
                    data.turn_id, data.step, data.call_id, data.retry_safety
                )
            elif isinstance(data, AsyncTaskFailedEventData):
                binding = async_calls.get(data.task_id)
                if (
                    binding is not None
                    and binding[3] == "inspect_external"
                    and data.recovery_status == "outcome_unknown"
                ):
                    unknown.add(binding[:3])
            elif isinstance(data, RecoveryResolutionEventData):
                resolved.add((data.turn_id, data.step, data.call_id))
        return tuple(value for key, value in calls.items() if key in unknown - resolved)

    def requires_external_resolution(self, call: ToolCall) -> bool:
        """存在未知修改结果时，所有非只读调用都须等待外部核验。"""
        return tool_retry_safety(call) != "safe" and bool(self.unresolved_external_calls())

    def resolve_tool_outcome(
        self, call_id: str, *, applied: bool, evidence: str,
        turn_id: str | None = None, step: int | None = None,
    ) -> SessionEvent:
        """完成只读外部检查后追加结论；高层 Agent 工具还会强制 token 和二次检查。"""
        if type(applied) is not bool or not isinstance(evidence, str) or not evidence.strip():
            raise SessionEventError("恢复结论必须是明确布尔值并包含外部核验证据。")
        pending = [item for item in self.unresolved_external_calls() if item.call.id == call_id
                   and (turn_id is None or item.turn_id == turn_id)
                   and (step is None or item.step == step)]
        if len(pending) != 1:
            raise SessionEventError("该工具调用没有待确认的外部状态。")
        item = pending[0]
        return self.append(
            "recovery/resolution",
            RecoveryResolutionEventData(
                turn_id=item.turn_id,
                step=item.step,
                call_id=call_id,
                resolution="confirmed_applied" if applied else "confirmed_not_applied",
                evidence=evidence,
            ),
        )

    def _recover_incomplete_tail(self) -> SessionRecoveryReport:
        """仅追加恢复事实和边界；绝不执行或自动重做任何工具。"""
        top_calls: dict[tuple[str, int, str], ToolCallEventData] = {}
        assistant_calls: list[tuple[AssistantMessageEventData, SessionToolCallSnapshot]] = []
        results: set[tuple[str, int, str]] = set()
        open_turns: dict[str, TurnStartEventData] = {}
        open_steps: dict[tuple[str, int], StepStartEventData] = {}
        async_accepted: dict[str, AsyncTaskAcceptedEventData] = {}
        async_started: set[str] = set()
        async_terminal: set[str] = set()
        async_cancel_requested: dict[str, str] = {}
        assistant_attempts: list[AssistantAttemptEventData] = []
        for event in self._events:
            data = event.data
            if isinstance(data, TurnStartEventData):
                open_turns[data.turn_id] = data
            elif isinstance(data, TurnEndEventData):
                open_turns.pop(data.turn_id, None)
            elif isinstance(data, StepStartEventData):
                open_steps[(data.turn_id, data.step)] = data
            elif isinstance(data, StepEndEventData):
                open_steps.pop((data.turn_id, data.step), None)
            elif isinstance(data, AssistantMessageEventData):
                assistant_calls.extend((data, call) for call in data.message.tool_calls)
            elif isinstance(data, AssistantAttemptEventData):
                assistant_attempts.append(data)
            elif isinstance(data, ToolCallEventData):
                top_calls[(data.turn_id, data.step, data.call.id)] = data
            elif isinstance(data, ToolResultEventData):
                results.add((data.turn_id, data.step, data.call_id))
            elif isinstance(data, AsyncTaskAcceptedEventData):
                async_accepted[data.task_id] = data
            elif isinstance(data, AsyncTaskStartedEventData):
                async_started.add(data.task_id)
            elif isinstance(data, AsyncTaskCancelRequestedEventData):
                async_cancel_requested[data.task_id] = data.reason
            elif isinstance(
                data,
                (AsyncTaskCompletedEventData, AsyncTaskFailedEventData, AsyncTaskCancelledEventData),
            ):
                async_terminal.add(data.task_id)

        repaired: list[RecoveredToolState] = []
        for assistant_data, call in assistant_calls:
            key = (assistant_data.turn_id, assistant_data.step, call.id)
            if key in results:
                continue
            accepted = next(
                (
                    item
                    for item in async_accepted.values()
                    if (item.turn_id, item.step, item.call_id) == key
                ),
                None,
            )
            if accepted is not None:
                placeholder = ToolResult(
                    status="accepted",
                    output=AsyncTaskAcceptedOutput(
                        task_id=accepted.task_id,
                        cancellable=accepted.cancellable,
                    ),
                )
                self.append(
                    "tool/result",
                    ToolResultEventData(
                        turn_id=assistant_data.turn_id,
                        step=assistant_data.step,
                        call_id=call.id,
                        result=tool_result_snapshot(placeholder),
                    ),
                )
                results.add(key)
                continue
            durable_call = top_calls.get(key)
            status: Literal["not_started", "outcome_unknown"] = (
                "outcome_unknown" if durable_call is not None else "not_started"
            )
            safety = (
                durable_call.retry_safety
                if durable_call is not None
                else tool_retry_safety(call)
            )
            error = (
                (
                    "上次工具执行结果未知；不能重试旧调用。"
                    "Agent 必须先调用 inspect_recovery，再调用 resolve_recovery；"
                    f"turn_id={assistant_data.turn_id} step={assistant_data.step} call_id={call.id}。"
                )
                if status == "outcome_unknown" and safety == "inspect_external"
                else (
                    "上次工具执行结果未知；该只读操作可以由模型重新发起。"
                    if status == "outcome_unknown"
                    else "上次工具调用尚未开始；可以由模型重新发起。"
                )
            )
            result = ToolResult(status="error", error=error)
            self.append(
                "tool/result",
                ToolResultEventData(
                    turn_id=assistant_data.turn_id,
                    step=assistant_data.step,
                    call_id=call.id,
                    result=tool_result_snapshot(result),
                    recovery_status=status,
                ),
            )
            results.add(key)
            repaired.append(
                RecoveredToolState(
                    turn_id=assistant_data.turn_id,
                    step=assistant_data.step,
                    call_id=call.id,
                    tool_name=call.name,
                    retry_safety=safety,
                    status=status,
                )
            )

        for task_id in async_accepted:
            if task_id in async_terminal:
                continue
            if task_id in async_cancel_requested:
                self.append(
                    "async/task/cancelled",
                    AsyncTaskCancelledEventData(
                        task_id=task_id,
                        reason=async_cancel_requested[task_id],
                    ),
                )
                continue
            recovery_status: Literal["not_started", "outcome_unknown"] = (
                "outcome_unknown" if task_id in async_started else "not_started"
            )
            error = (
                (
                    "进程中断前后台任务已经启动，但没有可靠终态；"
                    "恢复后不会自动重启，结果未知。"
                    "若该调用是修改型，Agent 必须按精确标识自主恢复；"
                    f"turn_id={async_accepted[task_id].turn_id} "
                    f"step={async_accepted[task_id].step} "
                    f"call_id={async_accepted[task_id].call_id}。"
                )
                if recovery_status == "outcome_unknown"
                else "进程中断前后台任务尚未启动；恢复后不会自动启动。"
            )
            self.append(
                "async/task/failed",
                AsyncTaskFailedEventData(
                    task_id=task_id,
                    error=error,
                    recovery_status=recovery_status,
                ),
            )

        interrupted_turn_ids = tuple(open_turns)
        for turn_id, step in sorted(open_steps):
            self.append(
                "step/end",
                StepEndEventData(turn_id=turn_id, step=step, kind="interrupted"),
            )
        for turn_id in interrupted_turn_ids:
            self.append(
                "turn/end", TurnEndEventData(turn_id=turn_id, kind="interrupted")
            )
        # assistant/attempt 保存的是不可信的截断流，不能作为完整 assistant 消息
        # 重放。恢复时另行追加一条无原文的宿主事实，既让后续模型知道发生过
        # 中断，又让该事实跨下一次启动仍然存在。
        interrupted = set(interrupted_turn_ids)
        for attempt in assistant_attempts:
            if attempt.turn_id not in interrupted:
                continue
            self.append(
                "host/normal",
                NormalHostEventData(
                    source="recovery",
                    message=(
                        f"模型生成曾以 {attempt.kind} 结束；turn={attempt.turn_id} "
                        f"step={attempt.step}。部分流仅保留用于审计，未作为回答重放。"
                    ),
                    details_json=canonical_json_snapshot(
                        {
                            "attempt_kind": attempt.kind,
                            "partial_stream_replayed": False,
                            "step": attempt.step,
                            "stream_chunk_count": len(attempt.stream),
                            "turn_id": attempt.turn_id,
                        }
                    ),
                ),
            )
        return SessionRecoveryReport(
            repaired=tuple(repaired), interrupted_turns=interrupted_turn_ids
        )

    def recover_incomplete(self) -> SessionRecoveryReport:
        """可捕获中断后也使用同一追加式恢复；失败存储必须重新打开。"""
        with self._thread_lock:
            if self._failed or self._closed:
                raise SessionEventError("会话必须重新打开才能恢复。")
            self._recovery_report = self._recover_incomplete_tail()
            return self._recovery_report

    def _close(self, *, emergency: bool) -> None:
        with self._thread_lock:
            if self._closed:
                return
            descriptor = self._lock_descriptor
            self._lock_descriptor = None
            if descriptor is not None:
                try:
                    if not emergency and os.name == "nt":
                        import msvcrt

                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    elif not emergency and os.name == "posix":
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)
            if self._directory_handles:
                import ctypes
                from ctypes import wintypes

                close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
                close_handle.argtypes = [wintypes.HANDLE]
                close_handle.restype = wintypes.BOOL
                for handle in reversed(self._directory_handles):
                    close_handle(handle)
                self._directory_handles.clear()
            self._closed = True

    def close(self) -> None:
        self._close(emergency=False)

    def emergency_close(self) -> None:
        """Release the writer lease without a potentially blocking unlock call.

        Closing the descriptor releases the OS lock on both supported
        platforms.  This path is used only after the Agent has installed its
        shutdown fence, so any late append fails the missing-lease check.
        """
        if not self._thread_lock.acquire(timeout=0.1):
            start_daemon_cleanup(
                lambda: self._close(emergency=True),
                name="agent-session-close",
            )
            return
        try:
            self._close(emergency=True)
        finally:
            self._thread_lock.release()

    def __enter__(self) -> "SessionEventWorkflow":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
