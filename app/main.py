"""Coding Agent 的交互式命令行入口。"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
import re
import stat
import sys
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.daemon_executor import DaemonExecutor, start_daemon_cleanup

if TYPE_CHECKING:
    from app.agent import CodingAgent
    from app.workflow import ToolConfirmationRequest


_INPUT_EOF = object()
_INPUT_INTERRUPT = object()
_INPUT_STOP = object()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SESSIONS_ROOT = _PROJECT_ROOT / "data" / "sessions"
_SESSION_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", "clock$",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class _EmergencyExit(Exception):
    """宿主已选择有界退出，不再等待模型 worker。"""


def create_client(settings):
    """延迟导入 OpenAI SDK；保留可 patch 的 CLI 适配点。"""
    from app.client import create_client as factory

    return factory(settings)


def load_settings():
    from app.config import load_settings as loader

    return loader()


def load_default_execution_backend():
    """延迟读取仅影响未绑定新会话的默认后端。"""
    from app.config import load_default_execution_backend as loader

    return loader()


def load_sandbox_settings():
    """延迟加载远端执行配置；local/list/help 路径不会导入该模块。"""
    from app.sandbox_config import load_sandbox_settings as loader

    return loader()


def capture_workspace_seed():
    """Lazy host snapshot seam; local/help/list/direct-exit paths never import it."""
    from app.workspace_seed import capture_workspace_seed as capture

    return capture()


def create_workspace_seed_initializer(snapshot, *, collect_orphans=False):
    """Lazy initializer factory kept patchable for offline CLI tests."""
    from app.workspace_seed import create_workspace_seed_initializer as factory

    return factory(snapshot, collect_orphans=collect_orphans)


def create_execution_runtime(
    execution_backend,
    session_id,
    sandbox_settings,
    *,
    collect_orphans=False,
):
    """首次模型请求捕获 host seed 并组装 runtime；不建立云资源。"""
    if execution_backend != "aliyun-sandbox":
        raise ValueError("仅 aliyun-sandbox 需要远端 execution runtime。")
    if type(collect_orphans) is not bool:
        raise TypeError("collect_orphans 必须是 bool。")
    from app.execution_backend import AliyunSandboxExecutionBackend
    from app.execution_runtime import ExecutionToolRuntime

    snapshot = capture_workspace_seed()
    if collect_orphans:
        initializer = create_workspace_seed_initializer(
            snapshot,
            collect_orphans=True,
        )
    else:
        initializer = create_workspace_seed_initializer(snapshot)
    backend = AliyunSandboxExecutionBackend(
        session_id,
        sandbox_settings,
        workspace_initializer=initializer,
    )
    try:
        return ExecutionToolRuntime(backend)
    except BaseException:
        try:
            backend.close()
        except BaseException:
            pass
        raise


def cleanup_recovered_remote_session(sandbox_settings, session_id):
    """Lazy, patchable startup seam for stale remote-session cleanup."""
    from app.aliyun_sandbox_session_cleanup import cleanup_session_sandboxes

    return cleanup_session_sandboxes(sandbox_settings, session_id)


def CodingAgent(*args: object, **kwargs: object):
    from app.agent import CodingAgent as implementation

    return implementation(*args, **kwargs)


def Observability():
    from app.observability import Observability as implementation

    return implementation()


def MaintenanceService(*args: object, **kwargs: object):
    from app.maintenance import MaintenanceService as implementation

    return implementation(*args, **kwargs)


def inspect_external_state(*args: object, **kwargs: object):
    """延迟导入恢复检查器，同时保留测试与嵌入方可 patch 的入口。"""
    from app.recovery import inspect_external_state as implementation

    return implementation(*args, **kwargs)


class _SessionCliError(ValueError):
    """A short, user-safe session-selection failure."""


@dataclass(frozen=True, slots=True)
class _SessionChoice:
    name: str
    directory: Path


@dataclass(frozen=True, slots=True)
class _SessionRequest:
    create_new: bool
    requested_name: str | None = None
    choice: _SessionChoice | None = None


def _validate_session_name(name: object, *, allow_current: bool) -> str:
    if not isinstance(name, str) or _SESSION_NAME_PATTERN.fullmatch(name) is None:
        raise _SessionCliError(
            "会话名只能包含 1-64 个 ASCII 字母、数字、点、下划线或连字符，"
            "且必须以字母或数字开头。"
        )
    folded = name.casefold()
    device_stem = folded.split(".", 1)[0]
    if (
        name.endswith(".")
        or device_stem in _WINDOWS_RESERVED_NAMES
        or (folded == "current" and not allow_current)
    ):
        raise _SessionCliError(f"会话名“{name}”为保留名，请换一个名称。")
    return name


def _is_reparse_or_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _safe_existing_session_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode) and not _is_reparse_or_link(path)
    except OSError:
        return False


def _verify_or_create_directory_chain(path: Path, *, create: bool) -> bool:
    """Walk a fixed absolute chain without following a link/reparse component."""
    absolute = path.absolute()
    for component in (*reversed(absolute.parents), absolute):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                component.mkdir()
                info = component.lstat()
            except OSError:
                raise _SessionCliError("会话目录链无法安全创建。") from None
        except OSError:
            raise _SessionCliError("会话目录链无法安全读取。") from None
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(attributes & reparse_flag)
        ):
            raise _SessionCliError("会话目录链包含不安全组件。")
    return True


def _has_safe_session_header(directory: Path) -> bool:
    return _has_safe_session_identity_file(directory / "session.json")


def _has_safe_session_identity_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not _is_reparse_or_link(path)
        and info.st_nlink == 1
    )


def _has_safe_session_identity(directory: Path) -> bool:
    """Allow the workflow to validate either complete or crash-window identity."""

    return _has_safe_session_header(directory) or _has_safe_session_identity_file(
        directory / "execution.json"
    )


def _has_session_identity_entry(directory: Path) -> bool:
    """Conservatively detect identity presence before the workflow takes its lock."""

    for name in ("session.json", "execution.json"):
        try:
            (directory / name).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            # An unreadable identity path is not a new session.  Let the
            # workflow reject it under its hardened storage boundary.
            return True
        return True
    return False


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent",
        description="启动 Coding Agent，并选择独立的持久会话。",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--new-session",
        nargs="?",
        const="",
        metavar="NAME",
        help="创建新会话；省略 NAME 时自动生成安全唯一名称。",
    )
    group.add_argument("--resume", metavar="NAME", help="恢复一个已存在的会话。")
    group.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出可恢复的会话，不加载 API 配置。",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="禁用 ANSI 颜色和终端样式，使用纯文本输出。",
    )
    parser.add_argument(
        "--permission-mode",
        choices=("partial-control", "full-access"),
        default="partial-control",
        help="可信宿主的初始工具权限模式（默认 partial-control）。",
    )
    parser.add_argument(
        "--execution-backend",
        choices=("local", "aliyun-sandbox"),
        default=None,
        help=(
            "会话执行后端（显式值优先；新会话省略时采用 "
            "AGENT_DEFAULT_EXECUTION_BACKEND，缺省 local；恢复会话省略时"
            "只采用其持久绑定）。"
        ),
    )
    return parser


def _list_sessions(root: Path | None = None) -> tuple[str, ...]:
    root = _SESSIONS_ROOT if root is None else root
    if not _verify_or_create_directory_chain(root, create=False):
        return ()
    try:
        entries = tuple(root.iterdir())
    except FileNotFoundError:
        return ()
    except OSError:
        raise _SessionCliError("会话列表无法安全读取。") from None
    names = []
    for entry in entries:
        try:
            _validate_session_name(entry.name, allow_current=True)
        except _SessionCliError:
            continue
        if _safe_existing_session_directory(entry) and _has_safe_session_identity(entry):
            names.append(entry.name)
    return tuple(sorted(names, key=str.casefold))


def _create_session_directory(name: str | None, root: Path | None = None) -> _SessionChoice:
    root = _SESSIONS_ROOT if root is None else root
    _verify_or_create_directory_chain(root, create=True)

    if name is not None:
        safe_name = _validate_session_name(name, allow_current=False)
        directory = root / safe_name
        try:
            directory.mkdir()
        except FileExistsError:
            raise _SessionCliError(
                f"会话“{safe_name}”已存在；请用 --resume {safe_name} 恢复，或换一个新名称。"
            ) from None
        except OSError:
            raise _SessionCliError(f"会话“{safe_name}”无法安全创建。") from None
        return _SessionChoice(safe_name, directory)

    for _ in range(32):
        generated = (
            f"session-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-"
            f"{uuid4().hex[:10]}"
        )
        directory = root / generated
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        except OSError:
            raise _SessionCliError("新会话无法安全创建。") from None
        return _SessionChoice(generated, directory)
    raise _SessionCliError("无法生成唯一会话名，请重试。")


def _prepare_session_request(arguments: argparse.Namespace) -> _SessionRequest:
    if arguments.new_session is not None:
        requested = arguments.new_session or None
        if requested is not None:
            _validate_session_name(requested, allow_current=False)
        return _SessionRequest(create_new=True, requested_name=requested)
    if arguments.resume is not None:
        name = _validate_session_name(arguments.resume, allow_current=True)
        if not _verify_or_create_directory_chain(_SESSIONS_ROOT, create=False):
            raise _SessionCliError(f"会话“{name}”不存在，请先用 --list-sessions 查看。")
        directory = _SESSIONS_ROOT / name
        if not directory.exists():
            raise _SessionCliError(f"会话“{name}”不存在，请先用 --list-sessions 查看。")
        if not _safe_existing_session_directory(directory):
            raise _SessionCliError(f"会话“{name}”的存储目录不安全。")
        if not _has_safe_session_identity(directory):
            raise _SessionCliError(f"会话“{name}”缺少安全的会话身份，无法恢复。")
        return _SessionRequest(
            create_new=False,
            choice=_SessionChoice(name, directory),
        )
    return _SessionRequest(
        create_new=False,
        choice=_SessionChoice("current", _SESSIONS_ROOT / "current"),
    )


@dataclass(slots=True)
class _ApprovalRequest:
    request: ToolConfirmationRequest
    ready: Event = field(default_factory=Event)
    approved: bool = False


@dataclass(slots=True)
class _PlanChoiceRequest:
    question: Any
    ready: Event = field(default_factory=Event)
    selected_index: int | None = None


@dataclass(frozen=True, slots=True)
class _LineRequest:
    prompt: str = ""
    complete_commands: bool = False


class _InputBroker:
    """Single owner for stdin; routes lines and approval requests without races."""

    def __init__(self, *, plain: bool = False, reader: Any = None) -> None:
        self._requests: Queue[object] = Queue()
        self._lines: Queue[object] = Queue()
        self._approvals: Queue[_ApprovalRequest] = Queue()
        self._plan_choices: Queue[_PlanChoiceRequest] = Queue()
        self._lock = Lock()
        self._outstanding = False
        self._stopped = False
        self._plain = plain
        self._reader = reader
        self._thread = Thread(target=self._read_loop, name="agent-cli-input", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request_line(self, prompt: str = "", *, complete_commands: bool = False) -> None:
        with self._lock:
            if self._stopped or self._outstanding:
                return
            self._outstanding = True
            self._requests.put(_LineRequest(prompt, complete_commands))

    def request_or_adopt_approval_line(self) -> bool:
        """为工具审批取得唯一输入行；返回值表示是否接管了现有读取。

        普通 act 模型运行期间通常已有一条带补全的主命令读取。工具审批
        到达时不能再启动第二个 stdin reader，因此显式把那条读取的下一行
        归属当前审批；若尚无读取，则排入一条不带补全的简单读取。
        """
        with self._lock:
            if self._stopped:
                return False
            if self._outstanding:
                return True
            self._outstanding = True
            self._requests.put(_LineRequest())
            return False

    def get_line(self, timeout: float | None = None) -> object:
        value = self._lines.get(timeout=timeout)
        with self._lock:
            self._outstanding = False
        return value

    def poll_line(self, timeout: float = 0.05) -> object | None:
        try:
            return self.get_line(timeout)
        except Empty:
            return None

    def confirm(self, request: ToolConfirmationRequest) -> bool:
        approval = _ApprovalRequest(request=request)
        with self._lock:
            if self._stopped:
                return False
            self._approvals.put(approval)
        approval.ready.wait()
        return approval.approved

    def poll_approval(self) -> _ApprovalRequest | None:
        try:
            return self._approvals.get_nowait()
        except Empty:
            return None

    def choose_plan_option(self, question: Any) -> int | None:
        request = _PlanChoiceRequest(question=question)
        with self._lock:
            if self._stopped:
                return None
            self._plan_choices.put(request)
        request.ready.wait()
        return request.selected_index

    def poll_plan_choice(self) -> _PlanChoiceRequest | None:
        try:
            return self._plan_choices.get_nowait()
        except Empty:
            return None

    @staticmethod
    def resolve_plan_choice(
        request: _PlanChoiceRequest, selected_index: int | None
    ) -> None:
        request.selected_index = selected_index
        request.ready.set()

    @staticmethod
    def resolve_approval(approval: _ApprovalRequest, answer: object) -> None:
        approval.approved = (
            isinstance(answer, str) and answer.strip().casefold() == "yes"
        )
        approval.ready.set()

    def close(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._requests.put(_INPUT_STOP)
        while True:
            approval = self.poll_approval()
            if approval is None:
                break
            self.resolve_approval(approval, "no")
        while True:
            plan_choice = self.poll_plan_choice()
            if plan_choice is None:
                break
            self.resolve_plan_choice(plan_choice, None)

    def _read_loop(self) -> None:
        reader = self._reader
        if reader is None:
            from app.terminal_input import create_terminal_reader

            reader = create_terminal_reader(plain=self._plain)
        while True:
            request = self._requests.get()
            if request is _INPUT_STOP:
                return
            assert isinstance(request, _LineRequest)
            try:
                value: object = reader(request.prompt, request.complete_commands)
            except EOFError:
                value = _INPUT_EOF
            except KeyboardInterrupt:
                value = _INPUT_INTERRUPT
            except Exception:
                # 输入后端失败时安全结束读取，不能让 broker 线程静默死亡并使主线程永久等待。
                value = _INPUT_EOF
            self._lines.put(value)


def _recovery_command(
    agent: CodingAgent, prompt: str, *, read_line: Any = None
) -> bool:
    """CLI 只读诊断快捷命令；恢复结论由 Agent 工具链追加。"""
    command = prompt.strip().split()
    if not command or command[0] not in {"/recovery", "/resolve"}:
        return False
    if command[0] == "/resolve":
        print("/resolve 人工确认流程已停用；Agent 会先只读检查，再自主追加恢复结论。")
        return True
    pending = agent.pending_recovery()
    if not pending:
        print("没有待核验的修改型工具调用。")
    for index, item in enumerate(pending, 1):
        checked = inspect_external_state(item)
        print(f"{index}. {item.call.name}；轮={item.turn_id}；步骤={item.step}；调用={item.call.id}")
        print(f"当前只读观察：{checked.state}；{checked.evidence}")
    if pending:
        print("Agent 会通过 inspect_recovery 和 resolve_recovery 自主处理；旧调用不会重放。")
    return True


def _show_confirmation(request: ToolConfirmationRequest) -> str:
    """显示不含正文/diff 的审批摘要，并返回审批输入提示。"""
    paths = ", ".join(request.paths)
    if request.phase in {"diff", "recovery_diff"}:
        heading = (
            "以前保留的文件修改待审批："
            if request.phase == "recovery_diff"
            else "文件修改待审批："
        )
        print(f"\n{heading}")
        print(f"目标路径：{paths}")
        print("diff 预览不在终端显示；请在宿主界面审阅本次工具调用的变更。")
        if request.pending_file:
            print(f"完整候选已保存到：{request.pending_file}")
        return "输入 yes 批准写入正式文件；输入 no 或其他内容拒绝并保留候选："
    action = (
        "生成 diff 候选"
        if request.tool_name in {"editor_file", "write_file"}
        else "执行本次操作"
    )
    return (
        f"\n需要确认：{request.description}；命令={request.command}；路径={paths}。"
        f"输入 yes 批准{action}，本次其他输入均拒绝："
    )


def _show_plan_choice(request: _PlanChoiceRequest) -> str:
    question = request.question
    print(f"\nPlan 需要确认：{question.question}")
    for index, option in enumerate(question.options, 1):
        recommendation = " [推荐]" if option.recommended else ""
        print(f"  {index}. {option.label}{recommendation} — {option.description}")
    return "请选择 1、2 或 3："


def _confirm_tool_operation(request: ToolConfirmationRequest) -> bool:
    """同步嵌入方兼容入口；只有明确输入 yes 才批准。"""
    try:
        answer = input(_show_confirmation(request))
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().casefold() == "yes"


def _async_command(
    agent: CodingAgent, prompt: str, *, reinvoke_after_interrupt: bool = True
) -> bool:
    """Handle host-only async task inspection/cancellation commands."""
    from app.agent import AgentError, AgentSessionError
    from app.async_tasks import AsyncTaskError

    stripped = prompt.strip()
    if stripped == "/tasks":
        tasks = agent.list_async_tasks()
        if not tasks:
            print("当前会话没有后台任务。")
        for item in tasks:
            progress = "" if item.progress is None else f"；进度={item.progress:.0%}"
            print(
                f"{item.task_id}；{item.tool_name}；状态={item.status.value}{progress}"
            )
        return True
    if stripped.startswith("/task "):
        task_id = stripped[6:].strip()
        try:
            item = agent.get_async_task(task_id)
        except (ValueError, AgentError, AsyncTaskError):
            print("任务 ID 无效，或任务不属于当前会话。")
            return True
        print(f"任务={item.task_id}；工具={item.tool_name}；状态={item.status.value}")
        if item.message:
            print(f"进度说明：{item.message}")
        if item.error:
            print(f"错误：{item.error}")
        if item.result_json:
            print(f"结果：{item.result_json}")
        return True
    if stripped.startswith("/cancel "):
        task_id = stripped[8:].strip()
        try:
            item = agent.cancel_async_task(task_id)
        except (ValueError, AgentError, AsyncTaskError):
            print("任务 ID 无效，或任务不属于当前会话。")
            return True
        print(f"任务 {item.task_id} 当前状态：{item.status.value}")
        return True
    if stripped.startswith("/interrupt"):
        reason = stripped[10:].strip() or "用户通过 /interrupt 请求重新评估。"
        agent.interrupt(reason)
        print("紧急事件已追加；正在运行的模型 generation 已作废。")
        if reinvoke_after_interrupt:
            result = agent.run("请根据刚刚追加的紧急宿主事件重新评估局势并继续。")
            print(f"Agent> {result}")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    """启动交互循环；导入本模块不会读取配置或发送网络请求。"""
    arguments = _build_argument_parser().parse_args(argv)
    if arguments.list_sessions:
        try:
            sessions = _list_sessions()
        except _SessionCliError as exc:
            print(f"启动失败：{exc}", file=sys.stderr)
            return 2
        if sessions:
            print("可恢复会话：")
            for name in sessions:
                print(name)
        else:
            print("没有可恢复会话。")
        return 0
    try:
        session_request = _prepare_session_request(arguments)
    except _SessionCliError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    # 会话存储是显示提示符前唯一必须加载的运行组件。模型协调、异步任务、
    # 可观测性与 OpenAI SDK 均推迟到用户真正发出模型请求时再加载。
    from app.workflow import (
        SessionEventError,
        SessionEventWorkflow,
        SessionStoreBusyError,
        has_started_without_terminal_async_task,
    )

    class _NeverRaisedError(Exception):
        pass

    agent_error_type: type[BaseException] = _NeverRaisedError
    agent_session_error_type: type[BaseException] = _NeverRaisedError
    agent_plan_cancelled_type: type[BaseException] = _NeverRaisedError

    client = None
    agent: Any = None
    selected_session = session_request.choice
    observability = None
    maintenance = None
    session_workflow = None
    executor: DaemonExecutor | None = None
    broker = _InputBroker(plain=arguments.plain)
    settings = None
    sandbox_settings = None
    resolved_execution_backend = None
    new_session_backend = "local"
    require_existing_identity = False
    emergency_exit = False
    host_permission_mode = arguments.permission_mode
    remote_session_cleaned = False
    try:
        try:
            settings = load_settings()
            if arguments.resume is not None:
                # --resume must never fall through to a configured new-session
                # default if its identity disappears before the lock is taken.
                require_existing_identity = True
            elif not session_request.create_new:
                assert selected_session is not None
                require_existing_identity = _has_session_identity_entry(
                    selected_session.directory
                )

            if (
                not require_existing_identity
                and arguments.execution_backend is None
            ):
                new_session_backend = load_default_execution_backend()
            backend_if_new = arguments.execution_backend or new_session_backend
            requested_execution_backend = (
                arguments.execution_backend
                if require_existing_identity
                else backend_if_new
            )
            if (
                not require_existing_identity
                and backend_if_new == "aliyun-sandbox"
            ):
                sandbox_settings = load_sandbox_settings()
                sandbox_settings.require_enabled()
            if session_request.create_new:
                selected_session = _create_session_directory(
                    session_request.requested_name
                )
            assert selected_session is not None
            session_workflow = SessionEventWorkflow(
                storage_directory=selected_session.directory,
                execution_backend_name=requested_execution_backend,
                new_session_backend_name=new_session_backend,
                require_existing_identity=require_existing_identity,
                recover=False,
            )
            resolved_execution_backend = session_workflow.execution_backend_name
            if resolved_execution_backend not in {"local", "aliyun-sandbox"}:
                raise SessionEventError("会话执行后端绑定无效。")
            if (
                resolved_execution_backend == "aliyun-sandbox"
                and sandbox_settings is None
            ):
                sandbox_settings = load_sandbox_settings()
                sandbox_settings.require_enabled()
            needs_remote_task_cleanup = has_started_without_terminal_async_task(
                session_workflow.events
            )
            if (
                resolved_execution_backend == "aliyun-sandbox"
                and needs_remote_task_cleanup
            ):
                assert sandbox_settings is not None
                cleanup_recovered_remote_session(
                    sandbox_settings,
                    session_workflow.session_id,
                )
                remote_session_cleaned = True
            session_workflow.recover_incomplete()
        except _SessionCliError as exc:
            print(f"启动失败：{exc}", file=sys.stderr)
            return 2
        except SessionStoreBusyError:
            assert selected_session is not None
            print(
                f"启动失败：会话“{selected_session.name}”正在被其它 Agent 占用。"
                "可在另一终端使用 agent --new-session [NAME] 打开独立会话。",
                file=sys.stderr,
            )
            return 2
        except SessionEventError:
            assert selected_session is not None
            print(
                f"启动失败：会话“{selected_session.name}”已损坏或无法安全恢复。",
                file=sys.stderr,
            )
            return 2
        except (ValueError, OSError):
            print("启动失败：请检查项目配置和环境变量。", file=sys.stderr)
            return 2
        except Exception:
            print("启动失败：无法验证轻量配置。", file=sys.stderr)
            return 2

        from app.console_ui import ConsoleUI

        ui = ConsoleUI(plain=arguments.plain)
        ui.banner(selected_session.name)
        pending_count = len(session_workflow.unresolved_external_calls())
        if pending_count:
            ui.warning(f"有 {pending_count} 个修改型调用结果待核验；Agent 将先自主检查并确认，期间其他修改工具仍受阻。输入 /recovery 可只读查看。")
        broker.start()
        executor = DaemonExecutor(max_workers=1, thread_name_prefix="agent-cli-run")
        deferred: list[str] = []
        exit_requested = False
        def read_host_line(prompt_text: str, *, complete_commands: bool = False) -> str | None:
            broker.request_line(prompt_text, complete_commands=complete_commands)
            value = broker.get_line()
            if value is _INPUT_EOF:
                return None
            if value is _INPUT_INTERRUPT:
                raise KeyboardInterrupt
            assert isinstance(value, str)
            return value

        def ensure_runtime() -> Any:
            nonlocal client, agent, observability, maintenance
            nonlocal agent_error_type, agent_session_error_type, agent_plan_cancelled_type
            nonlocal remote_session_cleaned
            if agent is not None:
                return agent
            ui.status("正在初始化模型运行时…")
            assert settings is not None and session_workflow is not None
            created_client = None
            created_observer = None
            created_execution_runtime = None
            try:
                from app.agent import AgentError, AgentPlanCancelledError, AgentSessionError

                agent_error_type = AgentError
                agent_session_error_type = AgentSessionError
                agent_plan_cancelled_type = AgentPlanCancelledError
                if resolved_execution_backend == "aliyun-sandbox":
                    assert sandbox_settings is not None
                    created_execution_runtime = create_execution_runtime(
                        resolved_execution_backend,
                        session_workflow.session_id,
                        sandbox_settings,
                        collect_orphans=True,
                    )
                    # Runtime construction captures and preflights the host
                    # snapshot but remains cloud-lazy.  Only after those
                    # deterministic checks pass do we prove provider active=0;
                    # the runtime is not handed to the Agent before this gate.
                    if not remote_session_cleaned:
                        cleanup_recovered_remote_session(
                            sandbox_settings,
                            session_workflow.session_id,
                        )
                        remote_session_cleaned = True
                elif resolved_execution_backend != "local":
                    raise RuntimeError("会话执行后端绑定无效。")
                created_client = create_client(settings)
                created_observer = Observability()
                agent_arguments = {
                    "confirmation_callback": broker.confirm,
                    "plan_clarification_callback": broker.choose_plan_option,
                    "session_workflow": session_workflow,
                    "observability": created_observer,
                    "permission_mode": host_permission_mode,
                }
                if created_execution_runtime is not None:
                    agent_arguments["execution_runtime"] = created_execution_runtime
                created_agent = CodingAgent(
                    created_client,
                    settings,
                    **agent_arguments,
                )
            except Exception:
                close_runtime = getattr(created_execution_runtime, "close", None)
                if callable(close_runtime):
                    try:
                        close_runtime()
                    except Exception:
                        pass
                close = getattr(created_client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                if created_observer is not None:
                    try:
                        created_observer.flush_pending()
                    except Exception:
                        pass
                raise
            client = created_client
            observability = created_observer
            agent = created_agent
            try:
                maintenance = MaintenanceService(observability)
                maintenance.start()
            except Exception:
                if maintenance is not None:
                    try:
                        maintenance.close()
                    except Exception:
                        pass
                maintenance = None
            return agent

        while True:
            try:
                if deferred:
                    prompt = deferred.pop(0)
                else:
                    broker.request_line(ui.prompt_text(), complete_commands=True)
                    incoming = broker.get_line()
                    if incoming is _INPUT_EOF:
                        print()
                        break
                    if incoming is _INPUT_INTERRUPT:
                        print("\n已退出。")
                        break
                    assert isinstance(incoming, str)
                    prompt = incoming
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if prompt.strip().lower() in {"exit", "quit"}:
                break
            if not prompt.strip():
                continue

            stripped = prompt.strip()
            if stripped in {"/mode plan", "/mode act"}:
                ui.warning(
                    "该模式切换命令已移除；普通输入始终执行 act，"
                    "需要交互式规划时请使用 /plan [需求]。"
                )
                continue
            if stripped == "/status":
                assert settings is not None and selected_session is not None
                rounds = agent.max_tool_rounds if agent is not None else 8
                plan_rounds = agent.max_plan_clarifications if agent is not None else 5
                ui.status(
                    f"workflow=act; plan_entry=/plan; session={selected_session.name}; model={settings.model}; "
                    f"timeout={settings.timeout_seconds:g}s; retries={settings.max_retries}; "
                    f"max_tool_rounds={rounds} (单用户轮的含工具 assistant step 上限); "
                    f"max_plan_clarifications={plan_rounds}"
                )
                continue
            if stripped == "/permissions" or stripped.startswith("/permissions "):
                permission_parts = stripped.split()
                if len(permission_parts) == 1:
                    risk = (
                        "高风险：文件修改不审批，可访问项目外路径。"
                        if host_permission_mode == "full-access"
                        else "默认：仅敏感文件修改需要审批。"
                    )
                    ui.status(
                        f"permission_mode={host_permission_mode}; {risk} "
                        "用法：/permissions partial-control|full-access"
                    )
                    continue
                if (
                    len(permission_parts) != 2
                    or permission_parts[1] not in {"partial-control", "full-access"}
                ):
                    ui.warning("用法：/permissions partial-control|full-access")
                    continue
                requested_permission_mode = permission_parts[1]
                if requested_permission_mode == host_permission_mode:
                    ui.status(f"permission_mode={host_permission_mode}; 未变更。")
                    continue
                if requested_permission_mode == "full-access":
                    try:
                        answer = read_host_line(
                            "警告：full-access 允许不经审批修改任意普通文件并访问项目外路径。"
                            "明确输入 yes 继续："
                        )
                    except KeyboardInterrupt:
                        answer = None
                    if answer is None or answer.strip().casefold() != "yes":
                        ui.warning("权限提升已取消；当前仍为 partial-control。")
                        continue
                try:
                    if agent is not None:
                        agent.set_permission_mode(requested_permission_mode)
                except (agent_error_type, agent_session_error_type):
                    ui.warning("当前请求正在执行或会话已停止，权限模式未变更。")
                    continue
                host_permission_mode = requested_permission_mode
                if host_permission_mode == "full-access":
                    ui.warning(
                        "已切换到 full-access；仅影响之后新启动的工具调用，"
                        "重启后仍以 --permission-mode 为准。"
                    )
                else:
                    ui.status(
                        "已立即降级到 partial-control；仅敏感文件修改需要审批。"
                    )
                continue

            plan_request = stripped == "/plan" or stripped.startswith("/plan ")
            model_prompt = prompt
            if plan_request:
                initial = stripped[5:].strip() if stripped.startswith("/plan") else prompt
                if not initial:
                    try:
                        initial = read_host_line("请输入需要规划的目标或需求：") or ""
                    except KeyboardInterrupt:
                        initial = ""
                if not initial.strip():
                    ui.warning("规划已取消；未调用模型，未写入会话。")
                    continue
                model_prompt = initial
                ui.status("模型正在生成计划；如关键信息不足，将显示三选一澄清问题。")

            try:
                active_agent = ensure_runtime()
            except SessionStoreBusyError:
                assert selected_session is not None
                ui.error(
                    f"会话“{selected_session.name}”正在被其它 Agent 占用。"
                    "可用 agent --new-session [NAME] 打开独立会话。"
                )
                return 2
            except (agent_session_error_type, SessionEventError):
                ui.error("会话已损坏或无法安全恢复。")
                return 2
            except Exception:
                ui.error("无法初始化模型运行时，请检查 SDK 与配置。")
                return 2

            try:
                if not plan_request and _async_command(active_agent, prompt):
                    continue
                if not plan_request and _recovery_command(
                    active_agent, prompt,
                    read_line=lambda question: read_host_line(question) or "",
                ):
                    continue
            except agent_session_error_type as exc:
                print(f"会话已停止：{exc}", file=sys.stderr)
                return 3
            except (EOFError, KeyboardInterrupt):
                print("\n核验已取消。")
                continue

            streamed = False

            def write_delta(delta: str) -> None:
                nonlocal streamed
                if not streamed:
                    ui.start_assistant()
                    streamed = True
                ui.text(delta, end="")

            runner = active_agent.plan if plan_request else active_agent.run
            # Consecutive Ctrl+C escalation belongs to this generation only.
            # A harmless interrupt in an older turn must not make a later,
            # unrelated interrupt terminate the whole CLI.
            interrupt_requests = 0
            future: Future[str] = executor.submit(
                runner, model_prompt, on_text_delta=write_delta
            )

            # Track an interaction after it has been removed from the broker
            # queue.  A real main-thread SIGINT can otherwise strand the model
            # worker on its ready Event forever.
            active_plan_choice: _PlanChoiceRequest | None = None
            active_approval: _ApprovalRequest | None = None
            background_input_exhausted = False

            def begin_emergency_exit() -> None:
                """Install shutdown flags even if another SIGINT lands in cleanup."""
                nonlocal exit_requested, emergency_exit
                exit_requested = True
                emergency_exit = True
                try:
                    active_agent.cancel_for_shutdown()
                except BaseException:
                    pass

            def wait_after_main_interrupt() -> tuple[str, object]:
                """Keep polling input/future after SIGINT; a second interrupt exits."""
                nonlocal interrupt_requests, exit_requested, emergency_exit
                nonlocal active_plan_choice, active_approval, background_input_exhausted
                while True:
                    try:
                        if active_plan_choice is not None:
                            broker.resolve_plan_choice(active_plan_choice, None)
                            active_plan_choice = None
                        if active_approval is not None:
                            broker.resolve_approval(active_approval, "no")
                            active_approval = None

                        # Any interaction created by the model after the first
                        # interrupt is rejected and woken.  The interrupted
                        # generation may explain/recover, but must not block on
                        # a fresh approval that this reduced loop cannot show.
                        queued_plan_choice = broker.poll_plan_choice()
                        if queued_plan_choice is not None:
                            broker.resolve_plan_choice(queued_plan_choice, None)
                        queued_approval = broker.poll_approval()
                        if queued_approval is not None:
                            broker.resolve_approval(queued_approval, "no")

                        if plan_request:
                            # Plan 不打开 act 主命令读取；Ctrl+C 由主线程直接
                            # 取消 generation，随后只等待 worker 安全退出。
                            return "result", future.result(timeout=0.05)
                        if not background_input_exhausted:
                            # 这是一个完整的主命令读取，不是只能服务当前
                            # generation 的临时 read。若 future 先结束，外层 idle
                            # 循环会接管同一读取，因此它必须始终保留主提示符和补全。
                            broker.request_line(
                                ui.prompt_text(), complete_commands=True
                            )
                            incoming = broker.poll_line(timeout=0.0)
                            if incoming is _INPUT_EOF:
                                background_input_exhausted = True
                            elif incoming is _INPUT_INTERRUPT:
                                interrupt_requests += 1
                                if interrupt_requests >= 2:
                                    begin_emergency_exit()
                                    return "emergency", None
                            elif isinstance(incoming, str):
                                if incoming.strip().casefold() in {"exit", "quit"}:
                                    begin_emergency_exit()
                                    return "emergency", None
                                if incoming.strip():
                                    deferred.append(incoming)
                        return "result", future.result(timeout=0.05)
                    except FutureTimeout:
                        continue
                    except KeyboardInterrupt:
                        interrupt_requests += 1
                        if interrupt_requests < 2:
                            continue
                        begin_emergency_exit()
                        return "emergency", None
                    except BaseException as exc:
                        return "error", exc

            # 普通 act 模型运行期间的输入属于抢先读取。某些终端宿主会在当前
            # 输入批次暂时耗尽时返回 EOF；它不能等价于用户在空闲提示符主动
            # 关闭会话。Plan 不使用该抢先读取，只在澄清时创建专属选择读取。
            try:
                try:
                    result = future.result(timeout=0.05)
                except FutureTimeout:
                    while not future.done():
                        plan_choice = broker.poll_plan_choice()
                        if plan_choice is not None:
                            active_plan_choice = plan_choice
                            prompt_text = _show_plan_choice(plan_choice)
                            selected_index: int | None = None
                            while selected_index is None and not plan_choice.ready.is_set():
                                if background_input_exhausted:
                                    break
                                # Plan 运行期间不预读 act 命令，因此这里总是新建
                                # 一条专属的、无命令补全的三选一读取。
                                broker.request_line(
                                    prompt_text, complete_commands=False
                                )
                                answer = broker.get_line()
                                prompt_text = "请选择 1、2 或 3："
                                if answer is _INPUT_EOF:
                                    background_input_exhausted = True
                                    break
                                if answer is _INPUT_INTERRUPT:
                                    break
                                assert isinstance(answer, str)
                                normalized = answer.strip().casefold()
                                if normalized in {"exit", "quit"}:
                                    begin_emergency_exit()
                                    break
                                if normalized.startswith("/interrupt"):
                                    break
                                if normalized in {"1", "2", "3"}:
                                    selected_index = int(normalized) - 1
                                    break
                                ui.warning("无效选择；请输入数字 1、2 或 3。")
                            broker.resolve_plan_choice(plan_choice, selected_index)
                            active_plan_choice = None
                            if exit_requested:
                                raise _EmergencyExit
                            continue

                        approval = broker.poll_approval()
                        if approval is not None:
                            active_approval = approval
                            print(_show_confirmation(approval.request), end="", flush=True)
                            if background_input_exhausted:
                                # 当前抢先读取通道已经耗尽，不能把下一轮稍后到达的
                                # 用户消息误当成这一轮工具审批；无明确 yes 即拒绝。
                                broker.resolve_approval(approval, "no")
                                active_approval = None
                                continue
                            broker.request_or_adopt_approval_line()
                            answer = broker.get_line()
                            if answer is _INPUT_INTERRUPT:
                                try:
                                    active_agent.interrupt("用户在审批期间通过 Ctrl+C 发出紧急中断。")
                                except agent_session_error_type:
                                    pass
                                answer = "no"
                            elif answer is _INPUT_EOF:
                                background_input_exhausted = True
                                answer = "no"
                            elif isinstance(answer, str) and answer.strip().casefold() in {"exit", "quit"}:
                                begin_emergency_exit()
                                answer = "no"
                            elif isinstance(answer, str) and answer.strip().startswith("/interrupt"):
                                _async_command(
                                    active_agent, answer, reinvoke_after_interrupt=False
                                )
                                answer = "no"
                            broker.resolve_approval(approval, answer)
                            active_approval = None
                            if exit_requested:
                                raise _EmergencyExit
                            continue

                        if plan_request:
                            # 首次请求与每次澄清后的下一模型请求都保持终端安静；
                            # 只有新的 clarification 到达时才创建专属选择读取。
                            try:
                                result = future.result(timeout=0.05)
                            except FutureTimeout:
                                continue
                            break

                        if background_input_exhausted:
                            try:
                                result = future.result(timeout=0.05)
                            except FutureTimeout:
                                continue
                            break
                        # 保持一个可跨越 future 完成边界的主命令读取。真实 TTY
                        # 始终使用同一 PromptSession/completer；plain/non-TTY 虽然
                        # 退化为 input，也会把随后到达的行交给下一次 idle 循环，
                        # 不会留下一个语义上无主的读取吞掉命令。
                        broker.request_line(
                            ui.prompt_text(), complete_commands=True
                        )
                        incoming = broker.poll_line()
                        if incoming is None:
                            continue
                        if future.done():
                            # The model may finish in the narrow interval after
                            # stdin became readable. Treat that line as the next
                            # idle command instead of turning a normal `exit`
                            # into an emergency shutdown.
                            if isinstance(incoming, str) and incoming.strip():
                                deferred.append(incoming)
                            elif incoming is _INPUT_INTERRUPT:
                                deferred.append("/interrupt")
                            break
                        if incoming is _INPUT_EOF:
                            background_input_exhausted = True
                            continue
                        if incoming is _INPUT_INTERRUPT:
                            interrupt_requests += 1
                            if interrupt_requests >= 2:
                                begin_emergency_exit()
                                raise _EmergencyExit
                            active_agent.interrupt("用户通过 Ctrl+C 中断当前模型响应。")
                            continue
                        assert isinstance(incoming, str)
                        lowered = incoming.strip().lower()
                        if lowered in {"exit", "quit"}:
                            # A fast local/fake provider can finish in the
                            # tiny scheduling window between poll_line() and
                            # this check. Give that already-finishing request
                            # one short grace period so a queued idle ``exit``
                            # is not misclassified as an emergency command.
                            try:
                                future.result(timeout=0.05)
                            except FutureTimeout:
                                pass
                            except BaseException:
                                deferred.append(incoming)
                                break
                            else:
                                deferred.append(incoming)
                                break
                            begin_emergency_exit()
                            raise _EmergencyExit
                        elif not _async_command(
                            active_agent, incoming, reinvoke_after_interrupt=False
                        ):
                            if incoming.strip():
                                deferred.append(incoming)
                    if exit_requested:
                        raise _EmergencyExit
                    result = future.result()
            except _EmergencyExit:
                if streamed:
                    print()
                ui.status("已取消活动请求并进入有界退出。")
            except agent_plan_cancelled_type:
                if streamed:
                    print()
                ui.warning("规划已取消；未完成规划没有写入会话。")
            except agent_session_error_type as exc:
                if streamed:
                    print()
                print(f"会话已停止：{exc}", file=sys.stderr)
                return 3
            except agent_error_type as exc:
                if streamed:
                    print()
                print(f"Agent 错误：{exc}", file=sys.stderr)
            except KeyboardInterrupt:
                if streamed:
                    print()
                interrupt_requests += 1
                if interrupt_requests >= 2:
                    begin_emergency_exit()
                    ui.status("已取消活动请求并进入有界退出。")
                    break
                if plan_request:
                    try:
                        active_agent.cancel_current_plan()
                    except KeyboardInterrupt:
                        interrupt_requests += 1
                        begin_emergency_exit()
                        ui.status("已取消活动请求并进入有界退出。")
                        break
                    outcome, _payload = wait_after_main_interrupt()
                    if outcome == "emergency":
                        ui.status("已取消活动请求并进入有界退出。")
                        break
                    else:
                        ui.warning("规划已取消；未完成规划没有写入会话。")
                    continue
                try:
                    active_agent.interrupt("用户通过 Ctrl+C 中断当前请求。")
                except KeyboardInterrupt:
                    interrupt_requests += 1
                    begin_emergency_exit()
                    ui.status("已取消活动请求并进入有界退出。")
                    break
                except agent_session_error_type as exc:
                    print(f"会话已停止：{exc}", file=sys.stderr)
                    return 3
                print("\n当前 generation 已中断，紧急事件已经保存；等待模型重新评估。", file=sys.stderr)
                outcome, payload = wait_after_main_interrupt()
                if outcome == "emergency":
                    ui.status("已取消活动请求并进入有界退出。")
                elif outcome == "error" and isinstance(payload, agent_error_type):
                    print(f"Agent 错误：{payload}", file=sys.stderr)
                elif outcome == "error":
                    print("Agent 错误：重新评估失败。", file=sys.stderr)
                else:
                    assert isinstance(payload, str)
                    ui.start_assistant()
                    ui.text(payload)
            except Exception:
                if streamed:
                    print()
                print("Agent 错误：处理请求失败。", file=sys.stderr)
            else:
                if streamed:
                    print()
                else:
                    ui.start_assistant()
                    ui.text(result)
            if exit_requested:
                break
    finally:
        broker.close()
        close = getattr(client, "close", None)
        if emergency_exit and callable(close):
            start_daemon_cleanup(close, name="agent-client-close")
        if agent is not None:
            try:
                if emergency_exit:
                    agent.close(wait=False)
                else:
                    # Keep compatibility with embedders/test doubles whose
                    # close() predates the emergency-only ``wait`` option.
                    agent.close()
            except Exception:
                pass
        elif session_workflow is not None:
            try:
                session_workflow.close()
            except Exception:
                pass
        if executor is not None:
            try:
                executor.shutdown(wait=not emergency_exit, cancel_futures=True)
            except Exception:
                pass
        if maintenance is not None:
            if emergency_exit:
                start_daemon_cleanup(
                    maintenance.close, name="agent-maintenance-close"
                )
            else:
                try:
                    maintenance.close()
                except Exception:
                    pass
        if observability is not None:
            if emergency_exit:
                start_daemon_cleanup(
                    observability.flush_pending, name="agent-observability-flush"
                )
            else:
                try:
                    observability.flush_pending()
                except Exception:
                    pass
        if not emergency_exit and callable(close):
            try:
                close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
