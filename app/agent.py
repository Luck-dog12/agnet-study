"""模型与工具之间的协调层。

``CodingAgent`` 保存经过 Pydantic 校验的内部消息，并在这里集中完成内部
``Message`` 与 OpenAI Chat Completions 消息格式之间的转换。工具模块无需知道
模型 SDK 的存在，模型客户端也不会直接接触 Python 工具函数。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from typing import Any
from threading import Event, Lock, RLock
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.permissions import (
    DEFAULT_PERMISSION_MODE,
    PermissionMode,
    validate_permission_mode,
)
from app.observability import Observability
from app.plan_mode import (
    PlanClarificationCallback,
    PlanClarificationQuestion,
    PlanModelResponse,
    build_clarification_answer,
    build_persisted_plan_request,
    build_plan_protocol_correction,
    build_plan_request_prompt,
    parse_plan_model_response,
    render_plan_document_chunks,
)
from app.async_tasks import (
    AsyncTaskManager,
    AsyncTaskRequest,
    AsyncTaskSnapshot,
    AsyncTaskStatus,
)
from app.daemon_executor import start_daemon_cleanup
from app.event_queue import EventDispatcher, EventQueueError
from app.execution_runtime import ExecutionToolRuntime
from app.schemas import (
    AsyncTaskAcceptedEventData,
    AsyncTaskAcceptedOutput,
    AsyncTaskCancelledEventData,
    AsyncTaskCompletedEventData,
    AsyncTaskFailedEventData,
    AssistantAttemptEventData,
    AssistantMessage,
    AssistantMessageEventData,
    AssistantStreamSnapshot,
    Message,
    RecoveryResolutionEventData,
    ResolveRecoveryArguments,
    SessionEvent,
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
from app.recovery import RecoveryToolRuntime
from app.skills import SkillRuntime, build_skill_catalog_message
from app.workflow import (
    ConfirmationCallback,
    FileApprovalWorkflow,
    SessionEventError,
    SessionEventWorkflow,
    assistant_message_snapshot,
    canonical_json_snapshot,
    tool_call_snapshot,
    tool_result_snapshot,
    tool_retry_safety,
    runtime_async_events,
)
from tool.registry import (
    TOOLSCHEMAS,
    AsyncToolPreparationError,
    call_tool,
    is_async_tool_call,
    prepare_async_tool_call,
)


StreamCallback = Callable[[str], None]
_MAX_STREAM_TEXT_CHARACTERS = 1024 * 1024
_MAX_STREAM_TOOL_ARGUMENT_CHARACTERS = 1024 * 1024
_MAX_STREAM_TOOL_CALLS = 64
_MAX_STREAM_CHUNKS = 16384
_MAX_STREAM_SNAPSHOT_BYTES = 6 * 1024 * 1024
_SYSTEM_PROMPT_FILE = Path(__file__).resolve().parent.parent / "code.md"
_MAX_SYSTEM_PROMPT_BYTES = 256 * 1024
_ASYNC_WAIT_POLL_SECONDS = 0.1
_MAX_PLAN_PROTOCOL_RETRIES = 2


class AgentError(RuntimeError):
    """Agent 对外可安全展示的基础异常。"""


class AgentProtocolError(AgentError):
    """模型或工具返回了不符合内部消息协议的数据。"""


class AgentLoopLimitError(AgentError):
    """模型连续调用工具，超过了本次任务允许的轮次。"""


class AgentRequestError(AgentError):
    """模型请求未完成；异常文本不会包含客户端内部信息。"""


class AgentPlanCancelledError(AgentError):
    """用户取消了尚未完成且尚未持久化的规划。"""


_REQUEST_ERROR_MESSAGES = {
    "timeout": "模型请求失败：请求超时，可检查网络或调大 DEEPSEEK_TIMEOUT_SECONDS。",
    "connection": "模型请求失败：无法连接模型服务。",
    "rate_limit": "模型请求失败：服务限流，请稍后重试。",
    "auth": "模型请求失败：认证或权限配置无效。",
    "server": "模型请求失败：模型服务端暂时异常。",
    "context": "模型请求失败：上下文超过模型限制，当前版本尚未自动摘要长会话。",
    "bad_request": "模型请求失败：模型服务拒绝了请求格式。",
    "unknown": "模型请求失败：未知的客户端或服务异常。",
}


def _classify_request_error(error: BaseException) -> str:
    """只使用类名和 HTTP 状态码，不读取或回显异常正文。"""
    name = type(error).__name__.casefold()
    status = getattr(error, "status_code", None)
    if "timeout" in name:
        return "timeout"
    if "connection" in name:
        return "connection"
    if "ratelimit" in name or status == 429:
        return "rate_limit"
    if any(token in name for token in ("authentication", "permission", "unauthorized")) or status in {401, 403}:
        return "auth"
    if "context" in name or status == 413:
        return "context"
    if type(status) is int and 500 <= status <= 599:
        return "server"
    if "badrequest" in name or status in {400, 404, 405, 409, 422}:
        return "bad_request"
    return "unknown"


class AgentInterruptedError(AgentError):
    """当前模型 generation 被紧急宿主事件作废。"""


class SystemPromptLoadError(AgentError):
    """持久化系统提示词缺失、无效或文件身份不安全。"""


class AgentSessionError(AgentError):
    """完整会话事件无法可靠保存或恢复。"""


def _prompt_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
    )


def load_system_prompt() -> str:
    """从固定项目根安全读取 UTF-8 ``code.md``，不依赖当前工作目录。"""
    try:
        before = os.lstat(_SYSTEM_PROMPT_FILE)
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or attributes & 0x400
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_SYSTEM_PROMPT_BYTES
        ):
            raise ValueError("系统提示词文件状态不安全")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(_SYSTEM_PROMPT_FILE, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                _prompt_identity(opened) != _prompt_identity(before)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
            ):
                raise ValueError("打开的系统提示词文件身份不一致")
            chunks: list[bytes] = []
            remaining = opened.st_size + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after_read = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        after_path = os.lstat(_SYSTEM_PROMPT_FILE)
        if (
            _prompt_identity(after_read) != _prompt_identity(opened)
            or _prompt_identity(after_path) != _prompt_identity(before)
            or getattr(after_path, "st_file_attributes", 0) & 0x400
            or len(raw) != opened.st_size
        ):
            raise ValueError("系统提示词文件在读取期间发生变化")
        prompt = raw.decode("utf-8-sig")
        if not prompt.strip() or "\x00" in prompt:
            raise ValueError("系统提示词不能为空或包含 NUL")
        return prompt
    except (OSError, UnicodeError, ValueError):
        raise SystemPromptLoadError("系统提示词配置不可用。") from None


def _field(value: object, name: str, default: Any = None) -> Any:
    """同时读取 OpenAI SDK 对象和测试用字典。"""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _parse_arguments(raw_arguments: object) -> dict[str, Any]:
    """把 SDK 的 function.arguments 严格解析为 JSON object。"""
    if not isinstance(raw_arguments, str):
        raise AgentProtocolError("模型工具参数不是 JSON 字符串。")

    def reject_nonstandard_number(value: str) -> None:
        raise ValueError(value)

    try:
        parsed = json.loads(raw_arguments, parse_constant=reject_nonstandard_number)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentProtocolError("模型工具参数不是有效 JSON。") from None
    if not isinstance(parsed, dict):
        raise AgentProtocolError("模型工具参数必须是 JSON object。")
    return parsed


def _parse_assistant_message(raw_message: object) -> AssistantMessage:
    """将一次 Chat Completions 回复转换为内部 AssistantMessage。"""
    raw_calls = _field(raw_message, "tool_calls", None) or []
    if not isinstance(raw_calls, (list, tuple)):
        raise AgentProtocolError("模型返回的 tool_calls 格式无效。")

    calls: list[ToolCall] = []
    try:
        for raw_call in raw_calls:
            if _field(raw_call, "type", "function") != "function":
                raise AgentProtocolError("模型返回了不支持的工具调用类型。")
            function = _field(raw_call, "function")
            if function is None:
                raise AgentProtocolError("模型工具调用缺少 function。")
            calls.append(
                ToolCall(
                    id=_field(raw_call, "id"),
                    name=_field(function, "name"),
                    arguments=_parse_arguments(_field(function, "arguments")),
                )
            )
        return AssistantMessage(
            content=_field(raw_message, "content"),
            tool_calls=calls,
            reasoning_content=_field(raw_message, "reasoning_content"),
        )
    except ValidationError:
        raise AgentProtocolError("模型回复不符合内部消息结构。") from None


def _append_stream_fragment(
    parts: list[str], fragment: object, *, field_name: str, maximum: int
) -> None:
    if fragment is None:
        return
    if not isinstance(fragment, str):
        raise AgentProtocolError(f"模型流式 {field_name} 格式无效。")
    if sum(len(part) for part in parts) + len(fragment) > maximum:
        raise AgentProtocolError(f"模型流式 {field_name} 超过大小限制。")
    parts.append(fragment)


def _raw_stream_value(value: object) -> object:
    """仅为离线 Fake chunk 补充 JSON 形状；真实 SDK chunk 使用 model_dump。"""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="python")
    if isinstance(value, SimpleNamespace):
        return {key: _raw_stream_value(item) for key, item in vars(value).items()}
    if isinstance(value, Mapping):
        return {key: _raw_stream_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_raw_stream_value(item) for item in value]
    return value


@dataclass(frozen=True)
class _CollectedStream:
    message: dict[str, object]
    snapshots: tuple[AssistantStreamSnapshot, ...]


def _collect_streamed_message(
    stream: object, on_text_delta: StreamCallback | None,
    snapshots: list[AssistantStreamSnapshot] | None = None,
) -> _CollectedStream:
    """严格汇总 Chat Completions chunk，包括分片的 function tool calls。"""
    contents: list[str] = []
    reasoning: list[str] = []
    calls: dict[int, dict[str, object]] = {}
    snapshots = snapshots if snapshots is not None else []
    snapshot_bytes = 0
    saw_choice = False
    try:
        iterator = iter(stream)
    except TypeError:
        raise AgentProtocolError("模型没有返回可迭代的流式响应。") from None

    for chunk in iterator:
        try:
            raw_snapshot = canonical_json_snapshot(_raw_stream_value(chunk))
            snapshot_bytes += len(raw_snapshot.encode("utf-8"))
            if len(snapshots) >= _MAX_STREAM_CHUNKS or snapshot_bytes > _MAX_STREAM_SNAPSHOT_BYTES:
                raise AgentProtocolError("模型流式快照超过保存上限。")
            snapshots.append(
                AssistantStreamSnapshot(
                    index=len(snapshots),
                    received_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    chunk_json=raw_snapshot,
                )
            )
        except (SessionEventError, ValidationError, TypeError, ValueError):
            raise AgentProtocolError("模型流式数据块无法无损保存。") from None
        choices = _field(chunk, "choices")
        if not isinstance(choices, (list, tuple)):
            raise AgentProtocolError("模型流式响应的 choices 格式无效。")
        if not choices:
            # 某些兼容服务会在最后发送只有 usage 的 chunk。
            continue
        if len(choices) != 1:
            raise AgentProtocolError("模型流式响应必须只包含一个选项。")
        choice = choices[0]
        choice_index = _field(choice, "index", 0)
        if type(choice_index) is not int or choice_index != 0:
            raise AgentProtocolError("模型流式响应的选项索引无效。")
        delta = _field(choice, "delta")
        if delta is None:
            raise AgentProtocolError("模型流式响应缺少 delta。")
        role = _field(delta, "role")
        if role not in (None, "assistant"):
            raise AgentProtocolError("模型流式响应的角色无效。")
        saw_choice = True

        content = _field(delta, "content")
        _append_stream_fragment(
            contents, content, field_name="content", maximum=_MAX_STREAM_TEXT_CHARACTERS
        )
        if isinstance(content, str) and content and on_text_delta is not None:
            on_text_delta(content)
        _append_stream_fragment(
            reasoning,
            _field(delta, "reasoning_content"),
            field_name="reasoning_content",
            maximum=_MAX_STREAM_TEXT_CHARACTERS,
        )

        raw_calls = _field(delta, "tool_calls")
        if raw_calls is None:
            continue
        if not isinstance(raw_calls, (list, tuple)):
            raise AgentProtocolError("模型流式 tool_calls 格式无效。")
        for raw_call in raw_calls:
            index = _field(raw_call, "index")
            if type(index) is not int or not 0 <= index < _MAX_STREAM_TOOL_CALLS:
                raise AgentProtocolError("模型流式工具调用索引无效。")
            item = calls.setdefault(
                index,
                {"id": [], "name": [], "arguments": [], "type": "function"},
            )
            call_type = _field(raw_call, "type")
            if call_type not in (None, "function"):
                raise AgentProtocolError("模型返回了不支持的流式工具调用类型。")
            _append_stream_fragment(
                item["id"],  # type: ignore[arg-type]
                _field(raw_call, "id"),
                field_name="tool call id",
                maximum=512,
            )
            function = _field(raw_call, "function")
            if function is not None:
                _append_stream_fragment(
                    item["name"],  # type: ignore[arg-type]
                    _field(function, "name"),
                    field_name="tool name",
                    maximum=64,
                )
                _append_stream_fragment(
                    item["arguments"],  # type: ignore[arg-type]
                    _field(function, "arguments"),
                    field_name="tool arguments",
                    maximum=_MAX_STREAM_TOOL_ARGUMENT_CHARACTERS,
                )

    if not saw_choice:
        raise AgentProtocolError("模型流式响应没有可用选项。")
    if calls and sorted(calls) != list(range(len(calls))):
        raise AgentProtocolError("模型流式工具调用索引不连续。")
    tool_calls = []
    for index in sorted(calls):
        item = calls[index]
        tool_calls.append(
            {
                "id": "".join(item["id"]),  # type: ignore[arg-type]
                "type": "function",
                "function": {
                    "name": "".join(item["name"]),  # type: ignore[arg-type]
                    "arguments": "".join(item["arguments"]),  # type: ignore[arg-type]
                },
            }
        )
    return _CollectedStream(
        message={
            "content": "".join(contents) if contents else None,
            "reasoning_content": "".join(reasoning) if reasoning else None,
            "tool_calls": tool_calls,
        },
        snapshots=tuple(snapshots),
    )


def message_to_sdk(message: Message) -> dict[str, Any]:
    """把一条内部 Message 转成 Chat Completions 的 messages 元素。

    内部工具参数保存为字典；SDK 要求 ``function.arguments`` 是 JSON 字符串。
    工具返回消息只发送 ``ToolResult`` JSON，并用 ``tool_call_id`` 保持关联。
    """
    if isinstance(message, (SystemMessage, UserMessage)):
        return {"role": message.role, "content": message.content}
    if isinstance(message, AssistantMessage):
        converted: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            converted["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.reasoning_content is not None:
            converted["reasoning_content"] = message.reasoning_content
        return converted
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.result.model_dump_json(),
        }
    raise TypeError("不支持的内部消息类型。")


def _tool_protocol_error(call_id: str, error: str) -> ToolMessage:
    """为无法执行或无法信任的调用生成可回传、可继续对话的结果。"""
    return ToolMessage(
        tool_call_id=call_id,
        result=ToolResult(status="error", error=error),
    )


class CodingAgent:
    """消息只从 SessionEvent 投影，模型或工具副作用前先建立持久化检查点。"""

    def __init__(
        self,
        client: object,
        settings: Settings,
        *,
        system_prompt: str | None = None,
        max_tool_rounds: int = 8,
        max_plan_clarifications: int = 5,
        permission_mode: PermissionMode = DEFAULT_PERMISSION_MODE,
        confirmation_callback: ConfirmationCallback | None = None,
        plan_clarification_callback: PlanClarificationCallback | None = None,
        workflow: FileApprovalWorkflow | None = None,
        session_workflow: SessionEventWorkflow | None = None,
        session_storage_directory: Path | None = None,
        observability: Observability | None = None,
        execution_runtime: ExecutionToolRuntime | None = None,
    ) -> None:
        if isinstance(max_tool_rounds, bool) or not isinstance(max_tool_rounds, int):
            raise TypeError("max_tool_rounds 必须是整数。")
        if max_tool_rounds < 0:
            raise ValueError("max_tool_rounds 不能小于 0。")
        if (
            type(max_plan_clarifications) is not int
            or not 1 <= max_plan_clarifications <= 10
        ):
            raise ValueError("max_plan_clarifications 必须是 1..10 的整数。")
        if plan_clarification_callback is not None and not callable(
            plan_clarification_callback
        ):
            raise TypeError("plan_clarification_callback 必须可调用。")
        permission_mode = validate_permission_mode(permission_mode)
        self._client = client
        self._settings = settings
        self._max_tool_rounds = max_tool_rounds
        self._max_plan_clarifications = max_plan_clarifications
        self._permission_lock = Lock()
        self._permission_mode = permission_mode
        self._confirmation_callback = confirmation_callback
        self._plan_clarification_callback = plan_clarification_callback
        self._workflow = workflow if workflow is not None else FileApprovalWorkflow()
        self._failed = False
        self._run_lock = Lock()
        self._generation_lock = RLock()
        self._generation = 0
        self._generation_cancel: Event | None = None
        self._response_stream: object | None = None
        self._system_prompt = load_system_prompt() if system_prompt is None else system_prompt
        SystemMessage(content=self._system_prompt)
        if session_workflow is not None and session_storage_directory is not None:
            raise ValueError("会话工作流和会话目录不能同时指定。")
        owns_session = session_workflow is None
        self._session = (
            session_workflow
            if session_workflow is not None
            else SessionEventWorkflow(storage_directory=session_storage_directory)
        )
        self._observability = observability if observability is not None else Observability()
        runtime_owned = False
        try:
            if execution_runtime is not None and type(
                execution_runtime
            ) is not ExecutionToolRuntime:
                raise TypeError("execution_runtime 必须是 ExecutionToolRuntime。")
            session_backend = self._session.execution_backend_name
            if session_backend == "local":
                if execution_runtime is not None and (
                    execution_runtime.closed
                    or execution_runtime.backend_name != "local"
                ):
                    raise ValueError("本地会话只能绑定未关闭的本地 execution runtime。")
            elif (
                execution_runtime is None
                or execution_runtime.closed
                or execution_runtime.backend_name != "aliyun-sandbox"
                or execution_runtime.session_id != self._session.session_id
            ):
                raise ValueError(
                    "远端会话必须绑定同一 session_id 的未关闭远端 execution runtime。"
                )
            self._execution_runtime = execution_runtime
            runtime_owned = execution_runtime is not None
            self._dispatcher = EventDispatcher(
                self._session,
                cancel_generation=lambda: self._cancel_active_generation(wait=False),
            )
            self._tasks = AsyncTaskManager.from_events(
                runtime_async_events(self._session.events),
                event_sink=self._dispatcher.accept_task_event,
            )
            self._recovery_runtime = RecoveryToolRuntime(
                session_id=self._session.session_id,
                pending_provider=self._session.unresolved_external_calls,
                resolution_sink=self._append_agent_recovery_resolution,
            )
            self._skill_runtime = SkillRuntime(session_id=self._session.session_id)
            self._skill_catalog_message = build_skill_catalog_message(
                self._skill_runtime.catalog
            )
            self._messages = list(self._session.project_messages(self._system_prompt))
        except Exception:
            tasks = getattr(self, "_tasks", None)
            if tasks is not None:
                try:
                    tasks.shutdown(wait=True)
                except Exception:
                    pass
            if runtime_owned:
                try:
                    execution_runtime.close()
                except Exception:
                    pass
            dispatcher = getattr(self, "_dispatcher", None)
            if dispatcher is not None:
                try:
                    dispatcher.close()
                except Exception:
                    pass
            if owns_session:
                self._session.close()
            raise AgentSessionError("会话上下文无法安全恢复。") from None

    @property
    def messages(self) -> tuple[Message, ...]:
        """返回事件投影的只读快照。"""
        return tuple(message.model_copy(deep=True) for message in self._messages)

    @property
    def session_events(self) -> tuple[SessionEvent, ...]:
        return self._session.events

    @property
    def recovery_report(self):
        return self._session.recovery_report

    @property
    def max_tool_rounds(self) -> int:
        return self._max_tool_rounds

    @property
    def max_plan_clarifications(self) -> int:
        return self._max_plan_clarifications

    @property
    def permission_mode(self) -> PermissionMode:
        with self._permission_lock:
            return self._permission_mode

    def set_permission_mode(self, permission_mode: PermissionMode) -> PermissionMode:
        """Change the trusted-host mode only while no agent turn is active.

        The CLI calls this from its idle command loop. Acquiring ``_run_lock``
        non-blockingly makes the same invariant explicit for other embedders:
        a mode change never alters an already-started model/tool turn.
        """
        validated = validate_permission_mode(permission_mode)
        if not self._run_lock.acquire(blocking=False):
            raise AgentError("当前请求正在执行，不能切换权限模式。")
        try:
            if self._failed:
                raise AgentSessionError("会话已停止，不能切换权限模式。")
            with self._permission_lock:
                self._permission_mode = validated
            return validated
        finally:
            self._run_lock.release()

    def _refresh_messages(self) -> None:
        try:
            self._messages = list(self._session.project_messages(
                self._system_prompt,
                allow_incomplete_tool_tail=True,
            ))
        except SessionEventError:
            self._failed = True
            raise AgentSessionError("会话上下文无法安全重建。") from None

    def _commit(self, event_type: str, data: BaseModel) -> SessionEvent:
        if self._failed:
            raise AgentSessionError("Agent 已停止，拒绝提交迟到事件。")
        try:
            event = self._dispatcher.submit_barrier(event_type, data)
            self._refresh_messages()
            return event
        except (SessionEventError, EventQueueError):
            self._failed = True
            raise AgentSessionError("会话事件无法可靠保存，当前工作流已停止。") from None

    def _commit_batch(
        self, pending: tuple[tuple[str, BaseModel], ...]
    ) -> tuple[SessionEvent, ...]:
        if self._failed:
            raise AgentSessionError("Agent 已停止，拒绝提交迟到事件。")
        try:
            events = self._dispatcher.submit_barrier_batch(pending)
            self._refresh_messages()
            return events
        except (SessionEventError, EventQueueError):
            self._failed = True
            raise AgentSessionError("会话事件批次无法可靠保存，当前工作流已停止。") from None

    def _commit_tool_result(
        self,
        *,
        turn_id: str,
        step: int,
        call_id: str,
        result: ToolResult,
        recovery_status: str = "recorded",
    ) -> None:
        self._commit(
            "tool/result",
            ToolResultEventData(
                turn_id=turn_id,
                step=step,
                call_id=call_id,
                result=tool_result_snapshot(result),
                recovery_status=recovery_status,
            ),
        )

    def _finish_boundaries(self, turn_id: str, step: int, kind: str) -> None:
        self._commit(
            "step/end", StepEndEventData(turn_id=turn_id, step=step, kind=kind)
        )
        self._commit("turn/end", TurnEndEventData(turn_id=turn_id, kind=kind))

    def to_sdk_messages(self) -> list[dict[str, Any]]:
        return [message_to_sdk(message) for message in self._messages]

    def _request_context_messages(self) -> list[dict[str, Any]]:
        """Build an SDK-only view; the Skill catalog is never persisted/projected."""
        messages = self.to_sdk_messages()
        if self._skill_catalog_message is not None:
            insertion = 1 if messages and messages[0].get("role") == "system" else 0
            messages.insert(
                insertion,
                {"role": "user", "content": self._skill_catalog_message},
            )
        return messages

    def compact_history(
        self, *, summary: str, replace_start_seq: int, replace_end_seq: int
    ) -> None:
        """显式追加摘要并替换投影；自动阈值尚未配置。"""
        if self._failed:
            raise AgentSessionError("会话已停止，请重新启动。")
        try:
            self._dispatcher.flush()
            self._session.append_compaction_summary(
                summary=summary,
                replace_start_seq=replace_start_seq,
                replace_end_seq=replace_end_seq,
            )
            self._refresh_messages()
        except (SessionEventError, EventQueueError):
            self._failed = True
            raise AgentSessionError("会话摘要无法可靠保存。") from None

    def ask(self, prompt: str, *, on_text_delta: StreamCallback | None = None) -> str:
        if self._failed:
            raise AgentSessionError("会话已停止，请重新启动以核对已提交的事件。")
        if not self._run_lock.acquire(blocking=False):
            raise AgentError("已有一轮会话正在执行。")
        try:
            next_prompt = prompt
            while True:
                try:
                    answer = self._ask(next_prompt, on_text_delta=on_text_delta)
                except AgentInterruptedError:
                    self._dispatcher.flush()
                    self._refresh_messages()
                    next_prompt = "请根据刚刚追加的紧急宿主事件重新评估局势并继续。"
                    continue
                except KeyboardInterrupt:
                    self.interrupt("用户通过 Ctrl+C 中断当前模型响应。")
                    next_prompt = "请根据刚刚追加的紧急宿主事件重新评估局势并继续。"
                    continue
                return answer
        except AgentSessionError:
            self._failed = True
            raise
        except BaseException:
            try:
                self._session.recover_incomplete()
                self._refresh_messages()
            except SessionEventError:
                self._failed = True
                raise AgentSessionError("中断状态无法可靠保存，请重新启动恢复会话。") from None
            raise
        finally:
            self._run_lock.release()

    def _ask(self, prompt: str, *, on_text_delta: StreamCallback | None = None) -> str:
        """完成一个持久化轮；日志故障时不再请求模型或启动下一工具。"""
        user_message = UserMessage(content=prompt)
        turn_id = uuid4().hex
        self._commit("turn/start", TurnStartEventData(turn_id=turn_id))
        self._commit(
            "user/message",
            UserMessageEventData(
                turn_id=turn_id,
                message=SessionUserMessageSnapshot(content=user_message.content),
            ),
        )
        completed_tool_rounds = 0
        step = 0

        while True:
            self._commit("step/start", StepStartEventData(turn_id=turn_id, step=step))
            request_context_seq = self._session.events[-1].seq
            snapshots: list[AssistantStreamSnapshot] = []
            try:
                collected = self._request_model(on_text_delta, snapshots)
                assistant = _parse_assistant_message(collected.message)
            except (AgentProtocolError, AgentRequestError):
                self._commit("assistant/attempt", AssistantAttemptEventData(
                    turn_id=turn_id, step=step, stream=tuple(snapshots), kind="error"
                ))
                self._finish_boundaries(turn_id, step, "error")
                raise
            except AgentInterruptedError:
                self._commit("assistant/attempt", AssistantAttemptEventData(
                    turn_id=turn_id, step=step, stream=tuple(snapshots), kind="interrupted"
                ))
                self._finish_boundaries(turn_id, step, "interrupted")
                raise
            except KeyboardInterrupt:
                self._commit("assistant/attempt", AssistantAttemptEventData(
                    turn_id=turn_id, step=step, stream=tuple(snapshots), kind="interrupted"
                ))
                self._finish_boundaries(turn_id, step, "interrupted")
                raise

            self._commit(
                "assistant/message",
                AssistantMessageEventData(
                    turn_id=turn_id,
                    step=step,
                    message=assistant_message_snapshot(assistant),
                    stream=collected.snapshots,
                ),
            )

            if not assistant.tool_calls:
                if not isinstance(assistant.content, str) or not assistant.content.strip():
                    self._finish_boundaries(turn_id, step, "error")
                    raise AgentProtocolError("模型最终回复缺少有效文本。")
                active_tasks, unseen_terminal = self._turn_async_followup_state(
                    turn_id, after_seq=request_context_seq
                )
                if not active_tasks and not unseen_terminal:
                    self._finish_boundaries(turn_id, step, "completed")
                    return assistant.content
                waiting_boundary = self._commit(
                    "step/end",
                    StepEndEventData(
                        turn_id=turn_id, step=step, kind="awaiting_async"
                    ),
                )
                if active_tasks:
                    self._wait_for_turn_async_tasks(
                        turn_id, urgent_after_seq=waiting_boundary.seq
                    )
                if on_text_delta is not None:
                    on_text_delta("\n")
                step += 1
                continue

            if completed_tool_rounds >= self._max_tool_rounds:
                for call in assistant.tool_calls:
                    result = _tool_protocol_error(
                        call.id, "已达到工具调用轮次上限，本次调用未执行。"
                    ).result
                    self._commit_tool_result(
                        turn_id=turn_id,
                        step=step,
                        call_id=call.id,
                        result=result,
                        recovery_status="not_started",
                    )
                self._finish_boundaries(turn_id, step, "error")
                raise AgentLoopLimitError("模型工具调用超过最大轮次。")

            if assistant.content and on_text_delta is not None:
                on_text_delta("\n")

            for index, tool_call in enumerate(assistant.tool_calls):
                safety = tool_retry_safety(tool_call)
                if self._session.requires_external_resolution(tool_call):
                    tool_message = _tool_protocol_error(
                        tool_call.id,
                        "尚有修改型工具结果未知；Agent 完成 inspect_recovery 和 resolve_recovery 前暂停其他非只读操作。",
                    )
                    self._commit_tool_result(
                        turn_id=turn_id,
                        step=step,
                        call_id=tool_call.id,
                        result=tool_message.result,
                        recovery_status="not_started",
                    )
                    continue
                self._commit(
                    "tool/call",
                    ToolCallEventData(
                        turn_id=turn_id, step=step,
                        call=tool_call_snapshot(tool_call), retry_safety=safety,
                    ),
                )
                if self._failed:
                    raise AgentSessionError("Agent 已停止，拒绝准备迟到工具调用。")
                if is_async_tool_call(tool_call):
                    try:
                        runtime_arguments = (
                            {}
                            if self._execution_runtime is None
                            else {"execution_runtime": self._execution_runtime}
                        )
                        prepared = prepare_async_tool_call(
                            tool_call,
                            confirmation_callback=self._confirmation_callback,
                            workflow=self._workflow,
                            observability=self._observability,
                            permission_mode=self.permission_mode,
                            **runtime_arguments,
                        )
                    except AsyncToolPreparationError as exc:
                        self._commit_tool_result(
                            turn_id=turn_id,
                            step=step,
                            call_id=tool_call.id,
                            result=exc.result,
                        )
                        continue
                    if self._failed:
                        try:
                            prepared.cancel_hook()
                        except Exception:
                            pass
                        raise AgentSessionError("Agent 已停止，拒绝接受迟到后台任务。")
                    request = AsyncTaskRequest(
                        session_id=self._session.session_id,
                        turn_id=turn_id,
                        step=step,
                        call_id=tool_call.id,
                        tool_name=tool_call.name,
                        request_json=canonical_json_snapshot(tool_call.arguments),
                        retry_safety=safety,
                        resource_kind=prepared.metadata.resource_kind,
                        cancellable=prepared.metadata.cancellable,
                    )
                    try:
                        accepted = self._tasks.accept(
                            request,
                            prepared.runner,
                            cancel_hook=prepared.cancel_hook,
                        )
                    except Exception:
                        try:
                            prepared.cancel_hook()
                        except Exception:
                            pass
                        self._failed = True
                        raise AgentSessionError(
                            "后台任务接受事件无法可靠保存，任务没有启动。"
                        ) from None
                    placeholder = ToolResult(
                        status="accepted",
                        output=AsyncTaskAcceptedOutput(
                            task_id=accepted.task_id,
                            cancellable=accepted.cancellable,
                        ),
                    )
                    try:
                        self._commit_tool_result(
                            turn_id=turn_id,
                            step=step,
                            call_id=tool_call.id,
                            result=placeholder,
                        )
                    except BaseException:
                        # The accepted event exists, but execution has not
                        # started. Mark it logically terminal when possible,
                        # and always revoke the exact remote capability before
                        # preserving the original persistence failure. The
                        # task manager may itself fail while emitting the
                        # cancellation event, so the direct hook is required.
                        try:
                            self._tasks.cancel(
                                self._session.session_id,
                                accepted.task_id,
                                reason="accepted 占位结果保存失败，任务未启动。",
                            )
                        except BaseException:
                            try:
                                prepared.cancel_hook()
                            except BaseException:
                                pass
                        raise
                    try:
                        self._tasks.start(self._session.session_id, accepted.task_id)
                    except Exception:
                        try:
                            prepared.cancel_hook()
                        except Exception:
                            pass
                        self._commit(
                            "async/task/failed",
                            AsyncTaskFailedEventData(
                                task_id=accepted.task_id,
                                error="后台执行器未能启动任务。",
                                recovery_status="not_started",
                            ),
                        )
                    continue
                try:
                    if self._failed:
                        raise AgentSessionError("Agent 已停止，拒绝执行迟到工具调用。")
                    runtime_arguments = (
                        {}
                        if self._execution_runtime is None
                        else {"execution_runtime": self._execution_runtime}
                    )
                    raw_result = call_tool(
                        tool_call,
                        confirmation_callback=self._confirmation_callback,
                        workflow=self._workflow,
                        observability=self._observability,
                        async_task_manager=self._tasks,
                        session_id=self._session.session_id,
                        recovery_runtime=self._recovery_runtime,
                        skill_runtime=self._skill_runtime,
                        skill_step=step,
                        permission_mode=self.permission_mode,
                        **runtime_arguments,
                    )
                    tool_message = ToolMessage.model_validate_json(raw_result)
                    if tool_message.tool_call_id != tool_call.id:
                        raise ValueError("tool_call_id 不匹配")
                except Exception:
                    unknown = _tool_protocol_error(
                        tool_call.id,
                        "工具返回异常，无法确认上次执行结果。",
                    )
                    self._commit_tool_result(
                        turn_id=turn_id,
                        step=step,
                        call_id=tool_call.id,
                        result=unknown.result,
                        recovery_status="outcome_unknown",
                    )
                    for remaining in assistant.tool_calls[index + 1 :]:
                        not_started = _tool_protocol_error(
                            remaining.id, "同轮前序工具返回异常，本次调用未执行。"
                        )
                        self._commit_tool_result(
                            turn_id=turn_id,
                            step=step,
                            call_id=remaining.id,
                            result=not_started.result,
                            recovery_status="not_started",
                        )
                    self._finish_boundaries(turn_id, step, "error")
                    raise AgentProtocolError("工具返回不符合内部消息结构。") from None
                self._commit_tool_result(
                    turn_id=turn_id,
                    step=step,
                    call_id=tool_call.id,
                    result=tool_message.result,
                )
            self._commit(
                "step/end", StepEndEventData(turn_id=turn_id, step=step, kind="completed")
            )
            completed_tool_rounds += 1
            step += 1

    def _turn_async_followup_state(
        self, turn_id: str, *, after_seq: int
    ) -> tuple[set[str], bool]:
        """Return active task ids and whether a terminal event missed this model input."""
        accepted: set[str] = set()
        terminal: dict[str, int] = {}
        for event in self._session.events:
            data = event.data
            if isinstance(data, AsyncTaskAcceptedEventData) and data.turn_id == turn_id:
                accepted.add(data.task_id)
            elif isinstance(
                data,
                (
                    AsyncTaskCompletedEventData,
                    AsyncTaskFailedEventData,
                    AsyncTaskCancelledEventData,
                ),
            ):
                terminal[data.task_id] = event.seq
        active = accepted.difference(terminal)
        unseen_terminal = any(
            task_id in accepted and seq > after_seq
            for task_id, seq in terminal.items()
        )
        return active, unseen_terminal

    def _wait_for_turn_async_tasks(
        self, turn_id: str, *, urgent_after_seq: int
    ) -> None:
        """Wait for task terminal state, or yield promptly to a new urgent host event."""
        while True:
            self._dispatcher.flush()
            self._refresh_messages()
            if any(
                event.seq > urgent_after_seq and event.type == "host/urgent"
                for event in self._session.events
            ):
                return
            durable_active, _ = self._turn_async_followup_state(turn_id, after_seq=0)
            if not durable_active:
                return
            snapshots = self._tasks.list(self._session.session_id)
            active = tuple(
                item
                for item in snapshots
                if item.task_id in durable_active
                and item.status in {AsyncTaskStatus.ACCEPTED, AsyncTaskStatus.RUNNING}
            )
            if not active:
                self._dispatcher.flush()
                self._refresh_messages()
                durable_active, _ = self._turn_async_followup_state(
                    turn_id, after_seq=0
                )
                if durable_active:
                    raise AgentSessionError("后台任务缺少可恢复的执行状态。")
                return
            previous = tuple((item.task_id, item.status) for item in snapshots)
            self._tasks.wait_for_change(
                self._session.session_id,
                previous,
                timeout=_ASYNC_WAIT_POLL_SECONDS,
            )

    def run(self, prompt: str, *, on_text_delta: StreamCallback | None = None) -> str:
        return self.ask(prompt, on_text_delta=on_text_delta)

    def plan(self, prompt: str, *, on_text_delta: StreamCallback | None = None) -> str:
        """先在内存中完成模型规划/澄清，成功后一次提交完整计划轮。"""
        if self._failed:
            raise AgentSessionError("会话已停止，请重新启动以核对已提交的事件。")
        if not self._run_lock.acquire(blocking=False):
            raise AgentError("已有一轮会话正在执行。")
        try:
            request_prompt = build_plan_request_prompt(prompt)
            planning_messages = self._request_context_messages()
            planning_messages.append({"role": "user", "content": request_prompt})
            answers: list[tuple[PlanClarificationQuestion, int]] = []
            seen_question_ids: set[str] = set()
            seen_question_texts: set[str] = set()
            final_collected: _CollectedStream | None = None
            final_response: PlanModelResponse | None = None
            protocol_retries = 0

            while True:
                snapshots: list[AssistantStreamSnapshot] = []
                try:
                    collected = self._request_model(
                        None,
                        snapshots,
                        allow_tools=False,
                        messages=planning_messages,
                        response_format={"type": "json_object"},
                    )
                    assistant = _parse_assistant_message(collected.message)
                    if assistant.tool_calls:
                        raise AgentProtocolError("计划模式拒绝模型返回的工具调用。")
                    if not isinstance(assistant.content, str):
                        raise AgentProtocolError("计划模式模型响应缺少 JSON 文本。")
                    try:
                        response = parse_plan_model_response(assistant.content)
                    except ValueError:
                        if protocol_retries >= _MAX_PLAN_PROTOCOL_RETRIES:
                            raise AgentProtocolError(
                                "计划模式模型连续返回无效结构，已停止纠正。"
                            ) from None
                        protocol_retries += 1
                        planning_messages.append(
                            {"role": "assistant", "content": assistant.content}
                        )
                        planning_messages.append(
                            {"role": "user", "content": build_plan_protocol_correction()}
                        )
                        continue
                except AgentInterruptedError:
                    raise AgentPlanCancelledError("规划已取消，未写入会话。") from None
                except KeyboardInterrupt:
                    self._cancel_active_generation()
                    raise AgentPlanCancelledError("规划已取消，未写入会话。") from None

                if response.kind == "final":
                    final_collected = collected
                    final_response = response
                    break

                question = response.clarification
                assert question is not None
                normalized_question = question.question.casefold()
                if (
                    question.id in seen_question_ids
                    or normalized_question in seen_question_texts
                ):
                    raise AgentProtocolError("计划模式重复返回同一澄清问题。")
                if len(answers) >= self._max_plan_clarifications:
                    raise AgentLoopLimitError("计划澄清超过最大轮次，未写入会话。")
                if self._plan_clarification_callback is None:
                    raise AgentProtocolError("计划模式需要宿主提供澄清选择回调。")
                seen_question_ids.add(question.id)
                seen_question_texts.add(normalized_question)
                try:
                    selected_index = self._plan_clarification_callback(question)
                except (EOFError, KeyboardInterrupt):
                    selected_index = None
                if selected_index is None:
                    raise AgentPlanCancelledError("规划已取消，未写入会话。")
                if type(selected_index) is not int or not 0 <= selected_index < 3:
                    raise AgentProtocolError("宿主返回了无效的 Plan 澄清选择。")
                answers.append((question, selected_index))
                planning_messages.append(
                    {"role": "assistant", "content": assistant.content}
                )
                planning_messages.append(
                    {
                        "role": "user",
                        "content": build_clarification_answer(question, selected_index),
                    }
                )

            assert final_collected is not None and final_response is not None
            plan_document = final_response.plan
            assert plan_document is not None
            rendered_chunks = render_plan_document_chunks(plan_document)
            rendered = "".join(rendered_chunks)
            persisted_prompt = build_persisted_plan_request(prompt, answers)
            user_message = UserMessage(content=persisted_prompt)
            assistant_message = AssistantMessage(content=rendered)
            turn_id = uuid4().hex
            self._commit_batch((
                ("turn/start", TurnStartEventData(turn_id=turn_id)),
                (
                    "user/message",
                    UserMessageEventData(
                        turn_id=turn_id,
                        message=SessionUserMessageSnapshot(content=user_message.content),
                    ),
                ),
                ("step/start", StepStartEventData(turn_id=turn_id, step=0)),
                (
                    "assistant/message",
                    AssistantMessageEventData(
                        turn_id=turn_id,
                        step=0,
                        message=assistant_message_snapshot(assistant_message),
                        stream=final_collected.snapshots,
                    ),
                ),
                ("step/end", StepEndEventData(turn_id=turn_id, step=0, kind="completed")),
                ("turn/end", TurnEndEventData(turn_id=turn_id, kind="completed")),
            ))
            if on_text_delta is not None:
                for chunk in rendered_chunks:
                    on_text_delta(chunk)
            return rendered
        except AgentSessionError:
            self._failed = True
            raise
        finally:
            self._run_lock.release()

    def _request_model(
        self, on_text_delta: StreamCallback | None = None,
        snapshots: list[AssistantStreamSnapshot] | None = None,
        *,
        allow_tools: bool = True,
        messages: list[dict[str, Any]] | None = None,
        response_format: dict[str, str] | None = None,
    ) -> _CollectedStream:
        if self._failed:
            raise AgentSessionError("Agent 已停止，拒绝启动新的模型请求。")
        response_stream = None
        with self._generation_lock:
            self._generation += 1
            generation = self._generation
            cancelled = Event()
            self._generation_cancel = cancelled
        try:
            request = {
                "model": self._settings.model,
                "messages": (
                    self._request_context_messages()
                    if messages is None
                    else deepcopy(messages)
                ),
                "stream": True,
            }
            if response_format is not None:
                request["response_format"] = response_format
            if allow_tools:
                request.update(tools=TOOLSCHEMAS, tool_choice="auto")
            response_stream = self._client.chat.completions.create(**request)
            with self._generation_lock:
                if self._failed or generation != self._generation or cancelled.is_set():
                    raise AgentInterruptedError("模型响应已被紧急事件取消。")
                self._response_stream = response_stream
            try:
                response_iterator = iter(response_stream)
            except TypeError:
                raise AgentProtocolError("流式响应不可迭代。") from None

            def guarded_delta(delta: str) -> None:
                if cancelled.is_set():
                    raise AgentInterruptedError("模型响应已被紧急事件取消。")
                if on_text_delta is not None:
                    on_text_delta(delta)

            def guarded_stream():
                for chunk in response_iterator:
                    if cancelled.is_set():
                        raise AgentInterruptedError("模型响应已被紧急事件取消。")
                    yield chunk

            collected = _collect_streamed_message(
                guarded_stream(), guarded_delta, snapshots
            )
            if cancelled.is_set():
                raise AgentInterruptedError("模型响应已被紧急事件取消。")
            return collected
        except AgentInterruptedError:
            raise
        except AgentProtocolError:
            raise
        except Exception as error:
            category = _classify_request_error(error)
            raise AgentRequestError(_REQUEST_ERROR_MESSAGES[category]) from None
        finally:
            with self._generation_lock:
                if generation == self._generation:
                    self._response_stream = None
                    self._generation_cancel = None
            close = getattr(response_stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def cancel_current_plan(self) -> None:
        """只取消当前 generation，不追加宿主事件或执行任何工具。"""
        self._cancel_active_generation(wait=False)

    def cancel_for_shutdown(self) -> None:
        """不追加会话事件；只阻止新工作并关闭当前流。"""
        self._failed = True
        self._cancel_active_generation(wait=False)

    def _cancel_active_generation(self, *, wait: bool = True) -> None:
        """Thread-safe generation fence used by urgent event delivery."""
        with self._generation_lock:
            cancelled = self._generation_cancel
            response_stream = self._response_stream
            if cancelled is not None:
                cancelled.set()
        close = getattr(response_stream, "close", None)
        if callable(close):
            if wait:
                try:
                    close()
                except Exception:
                    pass
            else:
                start_daemon_cleanup(close, name="agent-stream-close")

    def interrupt(self, message: str = "用户请求立即中断当前模型响应。") -> SessionEvent:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("中断说明不能为空。")
        try:
            event = self._dispatcher.submit_urgent(
                "host/urgent",
                UrgentHostEventData(
                    source="user",
                    message=message,
                    details_json="{}",
                ),
            )
            self._refresh_messages()
            return event
        except EventQueueError:
            self._failed = True
            raise AgentSessionError("紧急事件无法可靠保存，会话已停止。") from None

    def list_async_tasks(self) -> tuple[AsyncTaskSnapshot, ...]:
        return self._tasks.list(self._session.session_id)

    def get_async_task(self, task_id: str) -> AsyncTaskSnapshot:
        return self._tasks.query(self._session.session_id, task_id)

    def cancel_async_task(
        self, task_id: str, *, reason: str = "用户请求取消后台任务。"
    ) -> AsyncTaskSnapshot:
        return self._tasks.cancel(self._session.session_id, task_id, reason=reason)

    def pending_recovery(self) -> tuple[ToolCallEventData, ...]:
        return self._session.unresolved_external_calls()

    def _append_agent_recovery_resolution(
        self, arguments: ResolveRecoveryArguments, evidence: str
    ) -> None:
        """由会话绑定恢复工具调用；不提供绕过 inspect 的公开快捷入口。"""
        self._commit(
            "recovery/resolution",
            RecoveryResolutionEventData(
                turn_id=arguments.turn_id,
                step=arguments.step,
                call_id=arguments.call_id,
                resolution=(
                    "confirmed_applied"
                    if arguments.resolution == "applied"
                    else "confirmed_not_applied"
                ),
                evidence=evidence,
            ),
        )

    def close(self, *, wait: bool = True) -> None:
        self._failed = True
        runtime = self._execution_runtime
        fence_error: BaseException | None = None
        if runtime is not None:
            try:
                runtime.begin_close()
            except BaseException as error:
                fence_error = error
        self._cancel_active_generation(wait=wait)
        if wait:
            if runtime is not None:
                first_error = fence_error
                for cleanup in (
                    lambda: self._tasks.shutdown(wait=True),
                    runtime.close,
                    self._dispatcher.close,
                    self._session.close,
                ):
                    try:
                        cleanup()
                    except BaseException as error:
                        if first_error is None:
                            first_error = error
                if first_error is not None:
                    raise first_error
                return
            try:
                self._tasks.shutdown(wait=True)
            finally:
                try:
                    self._dispatcher.close()
                finally:
                    self._session.close()
            return

        # Emergency shutdown must release the durable writer boundary before
        # touching the task manager.  A task worker can hold its state lock
        # while a started/progress/terminal event sink is blocked; calling
        # shutdown(wait=False) synchronously in that state would still hang.
        try:
            self._dispatcher.emergency_close()
        finally:
            emergency_close = getattr(self._session, "emergency_close", None)
            if callable(emergency_close):
                emergency_close()
            else:
                self._session.close()
        if runtime is not None:
            start_daemon_cleanup(
                runtime.close,
                name="agent-execution-runtime-close",
            )
        start_daemon_cleanup(
            lambda: self._tasks.shutdown(wait=False),
            name="agent-async-tasks-close",
        )
