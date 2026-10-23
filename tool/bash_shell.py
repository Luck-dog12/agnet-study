"""结构化、跨平台的受限命令工具。

名称保留为 ``bash_shell`` 以符合 Agent 工具界面，但实现不依赖 Bash/WSL：
它只调度策略文件中开启的固定操作，不解析自由命令字符串，也不开放 ``-c``、
任意脚本路径、重定向、管道或环境变量注入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
from secrets import token_urlsafe
import shutil
import stat
import subprocess
import tempfile

from app.schemas import BashShellArguments, BashShellOutput, CodeInterpreterArguments
from app.permissions import current_permission_mode, mutation_access_active
from app.security_domain import path_is_sealed_secret
from app.workflow import (
    ConfirmationCallback,
    FileApprovalWorkflow,
    ToolConfirmationRequest,
)
from tool import file_access
from tool.code_interpreter import code_interpreter
from tool.file_access import (
    ToolAccessDenied,
    ToolExecutionError,
    open_project_file,
    resolve_project_directory,
    resolve_project_file,
    verify_open_file,
)
from tool.policy import PolicyLoadError, ToolPolicy, load_tool_policy, path_is_allowed
from tool.process_cancel import ProcessCancellationHandle, WindowsJob


MAX_OUTPUT_CHARACTERS = 32_768
MAX_ISOLATED_FILES = 20_000
MAX_ISOLATED_BYTES = 64 * 1024 * 1024
_REPARSE_POINT = 0x400
ASYNC_COMMANDS = frozenset({"unittest", "compileall", "validate_json"})


@dataclass(frozen=True)
class _DirectorySnapshot:
    files: tuple[tuple[Path, tuple[int, int, int, int, int, int]], ...]
    directories: tuple[tuple[Path, tuple[int, int, int, int, int, int]], ...]


def _normalized(candidate: Path) -> str:
    return file_access.display_host_path(candidate)


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
    )


def _resolve_existing(path: str) -> Path:
    try:
        return resolve_project_file(path)
    except ToolExecutionError:
        return resolve_project_directory(path)


def _resolve_operands(arguments: BashShellArguments) -> tuple[Path, ...]:
    paths = arguments.paths
    if arguments.command == "python":
        if paths or arguments.code is None:
            raise ToolExecutionError("python 命令参数无效。")
        return ()
    if arguments.command in {"unittest", "clear_directory"}:
        if len(paths) != 1:
            raise ToolExecutionError("该命令只接受一个目录路径。")
        return (resolve_project_directory(paths[0]),)
    if arguments.command in {"remove_file", "clear_file"}:
        if len(paths) != 1:
            raise ToolExecutionError("该命令只接受一个文件路径。")
        return (resolve_project_file(paths[0]),)
    if arguments.command in {"validate_json", "format_json"}:
        resolved = tuple(resolve_project_file(path) for path in paths)
        if any(path.suffix.casefold() != ".json" for path in resolved):
            raise ToolExecutionError("该命令只接受 JSON 文件。")
        return resolved
    if arguments.command == "compileall":
        resolved = tuple(_resolve_existing(path) for path in paths)
        if any(path.is_file() and path.suffix.casefold() != ".py" for path in resolved):
            raise ToolExecutionError("compileall 的文件操作数必须是 Python 文件。")
        return resolved
    raise ToolExecutionError("命令未实现。")


def _confirmation_description(command: str) -> str:
    descriptions = {
        "unittest": "执行工作副本中的测试代码",
        "compileall": "编译检查工作副本中的 Python 源码",
        "validate_json": "校验工作副本中的 JSON 文件",
        "format_json": "格式化工作副本中的 JSON 文件",
        "remove_file": "永久删除一个项目文件",
        "clear_file": "清空一个项目文件的全部内容",
        "clear_directory": "递归删除目录内的全部内容并保留目录本身",
        "python": "在 Code Interpreter 受限子进程中执行 Python 计算",
    }
    return descriptions[command]


def _require_confirmation(
    arguments: BashShellArguments,
    normalized_paths: tuple[str, ...],
    policy: ToolPolicy,
    callback: ConfirmationCallback | None,
) -> None:
    command_policy = policy.shell_commands.get(arguments.command)
    if command_policy is None or not command_policy.enabled:
        raise ToolAccessDenied("命令不在白名单内。")
    if len(normalized_paths) > command_policy.max_paths:
        raise ToolAccessDenied("命令路径数量超过策略上限。")
    if not command_policy.requires_confirmation:
        return
    if callback is None:
        raise ToolAccessDenied("命令需要用户二次确认。")
    request = ToolConfirmationRequest(
        tool_name="bash_shell",
        operation=arguments.command,
        paths=normalized_paths,
        description=_confirmation_description(arguments.command),
        request_id=token_urlsafe(18),
    )
    try:
        approved = callback(request)
    except Exception:
        raise ToolAccessDenied("用户确认过程失败，命令未执行。") from None
    if approved is not True:
        raise ToolAccessDenied("用户未批准，命令未执行。")


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _snapshot_directory(root: Path, policy: ToolPolicy) -> _DirectorySnapshot:
    """在审批前后建立相同快照；遇到敏感项、链接或特殊文件即整体拒绝。"""
    files: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    directories: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    pending = [root]
    scanned = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ToolExecutionError("无法安全扫描待清空目录。") from exc
        for entry in entries:
            scanned += 1
            if scanned > MAX_ISOLATED_FILES:
                raise ToolExecutionError("待清空目录的条目数超过安全上限。")
            candidate = Path(entry.path)
            relative = _normalized(candidate)
            if path_is_sealed_secret(relative):
                raise ToolAccessDenied("待清空目录包含密封秘密路径。")
            if (
                current_permission_mode() != "full-access"
                and not mutation_access_active()
                and not path_is_allowed(relative, policy)
            ):
                raise ToolAccessDenied("待清空目录包含受保护路径。")
            try:
                info = os.stat(candidate, follow_symlinks=False)
            except OSError as exc:
                raise ToolExecutionError("待清空目录在检查期间发生变化。") from exc
            if _is_link_or_reparse(info):
                raise ToolAccessDenied("待清空目录包含链接或重解析点。")
            identity = _identity(info)
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ToolAccessDenied("待清空目录包含多重硬链接文件。")
                files.append((candidate, identity))
            elif stat.S_ISDIR(info.st_mode):
                directories.append((candidate, identity))
                pending.append(candidate)
            else:
                raise ToolAccessDenied("待清空目录包含不支持的特殊文件。")
    files.sort(key=lambda item: _normalized(item[0]))
    directories.sort(key=lambda item: (-len(item[0].parts), _normalized(item[0])))
    return _DirectorySnapshot(tuple(files), tuple(directories))


def _remove_file(candidate: Path, before: os.stat_result) -> None:
    current = resolve_project_file(_normalized(candidate))
    info = os.stat(current, follow_symlinks=False)
    if current != candidate or _identity(info) != _identity(before):
        raise ToolExecutionError("文件在确认后发生变化，删除已取消。")
    current.unlink()


def _clear_file(candidate: Path, before: os.stat_result) -> None:
    with open_project_file(_normalized(candidate), writable=True) as opened:
        actual, current = verify_open_file(opened.stream, opened.path, writable=True)
        if actual != candidate or _identity(current) != _identity(before):
            raise ToolExecutionError("文件在确认后发生变化，清空已取消。")
        opened.stream.seek(0)
        opened.stream.truncate(0)
        opened.stream.flush()
        os.fsync(opened.stream.fileno())
        _, after = verify_open_file(opened.stream, actual, writable=True)
        if after.st_size != 0:
            raise ToolExecutionError("无法确认文件已被清空。")


def _clear_directory(candidate: Path, before: _DirectorySnapshot, policy: ToolPolicy) -> int:
    after = _snapshot_directory(candidate, policy)
    if before != after:
        raise ToolExecutionError("目录在确认后发生变化，清空已取消。")
    for path, identity in after.files:
        info = os.stat(path, follow_symlinks=False)
        if _identity(info) != identity:
            raise ToolExecutionError("目录在执行期间发生变化，清空已停止。")
        path.unlink()
    for path, identity in after.directories:
        info = os.stat(path, follow_symlinks=False)
        # 删除子项会按预期改变父目录的大小/mtime；目录对象身份和类型仍须一致。
        if _identity(info)[:4] != identity[:4]:
            raise ToolExecutionError("目录在执行期间发生变化，清空已停止。")
        path.rmdir()
    return len(after.files) + len(after.directories)


def _python_executable() -> Path:
    root = Path(__file__).resolve().parent.parent
    candidate = (
        root / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else root / ".venv" / "bin" / "python"
    )
    if not candidate.is_file():
        raise ToolExecutionError("项目虚拟环境解释器不可用。")
    return candidate


def _copy_isolated_workspace(destination: Path, policy: ToolPolicy) -> None:
    """复制允许的普通单链接文件；敏感项、缓存和虚拟环境不会进入副本。"""
    source_root = file_access._PROJECT_ROOT
    pending = [source_root]
    copied_files = 0
    copied_bytes = 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise ToolExecutionError("无法创建隔离工作副本。") from exc
        for entry in entries:
            source = Path(entry.path)
            relative = source.relative_to(source_root).as_posix()
            if not path_is_allowed(relative, policy):
                continue
            try:
                info = os.stat(source, follow_symlinks=False)
            except OSError:
                continue
            if _is_link_or_reparse(info):
                continue
            target = destination.joinpath(*Path(relative).parts)
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(parents=True, exist_ok=True)
                pending.append(source)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                copied_files += 1
                copied_bytes += info.st_size
                if copied_files > MAX_ISOLATED_FILES or copied_bytes > MAX_ISOLATED_BYTES:
                    raise ToolExecutionError("项目超过隔离工作副本的安全上限。")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target, follow_symlinks=False)

    # 测试副本中的工具模块需要一份策略才能保持 fail-closed。这里只复制已校验的
    # 非秘密配置；副本是一次性的，不能影响宿主项目策略。
    isolated_policy = destination / ".agent" / "tool_policy.json"
    isolated_policy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parent.parent / ".agent" / "tool_policy.json", isolated_policy)


def _subprocess_environment() -> dict[str, str]:
    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return environment


def _bounded(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARACTERS:
        return text, False
    marker = "\n...[输出已截断]"
    return text[: MAX_OUTPUT_CHARACTERS - len(marker)] + marker, True


def _run_once(
    argv: list[str],
    workspace: Path,
    timeout: int,
    cancellation_handle: ProcessCancellationHandle | None = None,
) -> tuple[int, str, str]:
    if cancellation_handle is None:
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace,
                env=_subprocess_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ToolExecutionError("命令执行超时，子进程已终止。") from None
        except OSError:
            raise ToolExecutionError("无法启动受限命令。") from None
        return completed.returncode, completed.stdout, completed.stderr

    if cancellation_handle.cancellation_requested:
        raise ToolExecutionError("命令执行已取消。")
    process: subprocess.Popen[str] | None = None
    job: WindowsJob | None = None
    bound = False
    try:
        popen_options: dict[str, object] = {}
        if os.name == "nt":
            job = WindowsJob()
            popen_options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            env=_subprocess_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **popen_options,
        )
        if job is not None:
            job.assign(process)
        cancellation_handle.bind(process, windows_job=job)
        bound = True
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            cancellation_handle.terminate_bound()
            process.communicate()
            raise ToolExecutionError("命令执行超时，子进程已终止。") from None
        return process.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        raise ToolExecutionError("命令执行超时，子进程已终止。") from None
    except ToolExecutionError:
        raise
    except OSError:
        raise ToolExecutionError("无法启动受限命令。") from None
    finally:
        if process is not None:
            if process.poll() is None:
                if bound:
                    cancellation_handle.terminate_bound()
                elif job is not None:
                    job.terminate()
                else:
                    process.kill()
                try:
                    process.communicate(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if bound:
                cancellation_handle.release(process)
        if job is not None:
            job.close(terminate=False)


def _run_in_isolated_workspace(
    arguments: BashShellArguments,
    normalized_paths: tuple[str, ...],
    policy: ToolPolicy,
    cancellation_handle: ProcessCancellationHandle | None = None,
) -> BashShellOutput:
    python = str(_python_executable())
    with tempfile.TemporaryDirectory(prefix="agent-shell-") as temporary:
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        _copy_isolated_workspace(workspace, policy)

        if arguments.command == "unittest":
            argv = [
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                normalized_paths[0],
                "-p",
                "test*.py",
                "-v",
            ]
            exit_code, stdout, stderr = _run_once(
                argv, workspace, arguments.timeout_seconds, cancellation_handle
            )
        elif arguments.command == "compileall":
            argv = [python, "-m", "compileall", "-q", *normalized_paths]
            exit_code, stdout, stderr = _run_once(
                argv, workspace, arguments.timeout_seconds, cancellation_handle
            )
        else:
            exit_code = 0
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []
            for path in normalized_paths:
                code, command_stdout, command_stderr = _run_once(
                    [python, "-m", "json.tool", path],
                    workspace,
                    arguments.timeout_seconds,
                    cancellation_handle,
                )
                exit_code = exit_code or code
                stdout_parts.append(command_stdout)
                stderr_parts.append(command_stderr)
            stdout = "".join(stdout_parts)
            stderr = "".join(stderr_parts)

    bounded_stdout, stdout_truncated = _bounded(stdout)
    bounded_stderr, stderr_truncated = _bounded(stderr)
    return BashShellOutput(
        command=arguments.command,
        paths=list(normalized_paths),
        exit_code=exit_code,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
        truncated=stdout_truncated or stderr_truncated,
    )


def prepare_bash_shell_async(
    arguments: BashShellArguments,
    confirmation_callback: ConfirmationCallback | None = None,
) -> tuple[BashShellArguments, bool]:
    """Validate and obtain any required approval before background scheduling."""
    arguments = BashShellArguments.model_validate(arguments.model_dump())
    if arguments.command not in ASYNC_COMMANDS:
        raise ToolExecutionError("该命令不允许后台执行。")
    try:
        policy = load_tool_policy()
    except PolicyLoadError:
        raise ToolAccessDenied("工具权限策略不可用。") from None
    resolved = _resolve_operands(arguments)
    normalized_paths = tuple(_normalized(path) for path in resolved)
    _require_confirmation(arguments, normalized_paths, policy, confirmation_callback)
    command_policy = policy.shell_commands[arguments.command]
    return arguments, command_policy.requires_confirmation


def bash_shell(
    arguments: BashShellArguments,
    confirmation_callback: ConfirmationCallback | None = None,
    workflow: FileApprovalWorkflow | None = None,
    cancellation_handle: ProcessCancellationHandle | None = None,
) -> BashShellOutput:
    """执行一个固定结构的白名单操作；危险操作没有明确批准就不会执行。"""
    arguments = BashShellArguments.model_validate(arguments.model_dump())
    try:
        policy = load_tool_policy()
    except PolicyLoadError:
        raise ToolAccessDenied("工具权限策略不可用。") from None
    command_policy = policy.shell_commands.get(arguments.command)
    if command_policy is None or not command_policy.enabled:
        raise ToolAccessDenied("命令不在白名单内。")
    if len(arguments.paths) > command_policy.max_paths:
        raise ToolAccessDenied("命令路径数量超过策略上限。")
    if arguments.command == "format_json":
        raise ToolAccessDenied("format_json 只允许通过远端 mutation transaction 执行。")

    if arguments.command == "python":
        if arguments.code is None:
            raise ToolExecutionError("python 命令缺少计算代码。")
        output = code_interpreter(CodeInterpreterArguments(
            code=arguments.code,
            timeout_seconds=arguments.timeout_seconds,
        ), cancellation_handle)
        return BashShellOutput(
            command="python",
            paths=[],
            exit_code=output.exit_code,
            stdout=output.stdout,
            stderr=output.stderr,
            truncated=output.truncated,
            result=output.result,
        )

    resolved = _resolve_operands(arguments)
    normalized_paths = tuple(_normalized(path) for path in resolved)

    file_before: os.stat_result | None = None
    directory_before: _DirectorySnapshot | None = None
    if arguments.command in {"remove_file", "clear_file"}:
        file_before = os.stat(resolved[0], follow_symlinks=False)
    elif arguments.command == "clear_directory":
        directory_before = _snapshot_directory(resolved[0], policy)

    if arguments.command not in {"remove_file", "clear_file", "clear_directory"}:
        _require_confirmation(arguments, normalized_paths, policy, confirmation_callback)
        return _run_in_isolated_workspace(
            arguments, normalized_paths, policy, cancellation_handle
        )

    if workflow is None:
        from app.workflow import WorkflowApprovalDenied

        raise WorkflowApprovalDenied("破坏性文件命令缺少审批状态机。")

    def execute_file_change() -> BashShellOutput:
        if arguments.command == "remove_file":
            _remove_file(resolved[0], file_before)  # type: ignore[arg-type]
            output = "已删除 1 个文件。"
        elif arguments.command == "clear_file":
            _clear_file(resolved[0], file_before)  # type: ignore[arg-type]
            output = "已清空 1 个文件。"
        else:
            count = _clear_directory(
                resolved[0], directory_before, policy  # type: ignore[arg-type]
            )
            output = f"已删除目录内 {count} 个条目。"
        return BashShellOutput(
            command=arguments.command,
            paths=list(normalized_paths),
            exit_code=0,
            stdout=output,
            stderr="",
            truncated=False,
        )

    return workflow.run_approved(
        tool_name="bash_shell",
        operation=arguments.command,
        paths=normalized_paths,
        description=_confirmation_description(arguments.command),
        confirmation_callback=confirmation_callback,
        action=execute_file_change,
    )
