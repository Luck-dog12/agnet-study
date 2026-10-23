"""受限 Python 计算工具。

这不是 Docker、VM 或 OpenAI 托管容器，而是本机上的能力隔离子进程：用户代码
只能通过严格 AST 白名单访问少量纯计算能力，并在空临时目录、净化环境和操作系统
资源限制下执行。任何策略或资源限制建立失败都会 fail closed。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import tempfile

from app.schemas import CodeInterpreterArguments, CodeInterpreterOutput
from tool import _code_interpreter_runner as runner
from tool.file_access import ToolAccessDenied, ToolExecutionError
from tool.policy import CodeInterpreterPolicy, PolicyLoadError, load_tool_policy
from tool.process_cancel import ProcessCancellationHandle, WindowsJob


_RUNNER = Path(__file__).resolve().parent / "_code_interpreter_runner.py"
_CREATE_NO_WINDOW = 0x08000000


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


def _environment() -> dict[str, str]:
    allowed = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    return environment


def _validate_ast(code: str, max_nodes: int) -> None:
    """父进程完成与 runner 等价的能力校验，越权代码不会启动子进程。"""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        raise ToolExecutionError("代码语法无效。") from None
    if sum(1 for _ in ast.walk(tree)) > max_nodes:
        raise ToolAccessDenied("代码结构超过策略上限。")
    try:
        runner.validate_and_transform(code)
    except runner.RejectedCode:
        raise ToolAccessDenied("代码包含白名单之外的能力。") from None


def validate_code_interpreter_policy(
    arguments: CodeInterpreterArguments,
) -> CodeInterpreterPolicy:
    """Load the latest host policy and validate code without starting a process."""
    validated = CodeInterpreterArguments.model_validate(arguments.model_dump())
    try:
        policy = load_tool_policy()
    except PolicyLoadError:
        raise ToolAccessDenied("工具权限策略不可用。") from None
    settings = policy.code_interpreter
    if not settings.enabled:
        raise ToolAccessDenied("Code Interpreter 已被策略禁用。")
    if validated.timeout_seconds > settings.max_timeout_seconds:
        raise ToolAccessDenied("执行超时超过策略上限。")
    _validate_ast(validated.code, settings.max_ast_nodes)
    return settings


def _create_windows_job(memory_bytes: int, max_processes: int) -> WindowsJob:
    """Compatibility seam retained for fail-closed resource-boundary tests."""
    return WindowsJob(memory_bytes=memory_bytes, max_processes=max_processes)


def _assign_windows_job(job: WindowsJob, process: subprocess.Popen[str]) -> None:
    job.assign(process)


def _close_windows_job(job: WindowsJob, *, terminate: bool = False) -> None:
    job.close(terminate=terminate)


def _posix_limit(memory_bytes: int, max_processes: int, timeout_seconds: int):
    def apply_limits() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
        if hasattr(resource, "RLIMIT_NPROC"):
            resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024, 64 * 1024))

    return apply_limits


def _run_child(
    code: str,
    *,
    timeout_seconds: int,
    max_memory_mb: int,
    max_processes: int,
    max_output_characters: int,
    cancellation_handle: ProcessCancellationHandle | None = None,
) -> CodeInterpreterOutput:
    if not _RUNNER.is_file():
        raise ToolExecutionError("隔离执行器不可用。")
    argv = [str(_python_executable()), "-I", "-S", "-X", "utf8", str(_RUNNER)]
    payload = json.dumps(
        {
            "code": code,
            "max_output_characters": max_output_characters,
            "max_memory_mb": max_memory_mb,
            "max_processes": max_processes,
            "timeout_seconds": timeout_seconds,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    memory_bytes = max_memory_mb * 1024 * 1024
    job: WindowsJob | None = None
    process: subprocess.Popen[str] | None = None
    bound = False
    process_handle = cancellation_handle or ProcessCancellationHandle()

    if process_handle.cancellation_requested:
        raise ToolExecutionError("代码执行已取消。")

    with tempfile.TemporaryDirectory(prefix="agent-code-") as temporary:
        try:
            if os.name == "nt":
                job = _create_windows_job(memory_bytes, max_processes)
                process = subprocess.Popen(
                    argv,
                    cwd=temporary,
                    env=_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    creationflags=_CREATE_NO_WINDOW,
                )
                try:
                    # Runner 在读取 stdin 前不会接触用户代码；只有资源限制成功后才发送代码。
                    _assign_windows_job(job, process)
                except Exception:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            else:
                process = subprocess.Popen(
                    argv,
                    cwd=temporary,
                    env=_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    start_new_session=True,
                    preexec_fn=_posix_limit(memory_bytes, max_processes, timeout_seconds),
                )
            process_handle.bind(process, windows_job=job)
            bound = True
            try:
                stdout, _ = process.communicate(payload, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                process_handle.terminate_bound()
                process.communicate()
                raise ToolExecutionError("代码执行超时，隔离进程已终止。") from None
        except ToolExecutionError:
            raise
        except OSError:
            raise ToolExecutionError("无法启动隔离执行器。") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    if bound:
                        process_handle.terminate_bound()
                    elif job is not None:
                        job.terminate()
                    else:
                        process.kill()
                    try:
                        process.communicate(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                if bound:
                    process_handle.release(process)
            if job is not None:
                _close_windows_job(job, terminate=False)

    if process is None or process.returncode != 0:
        raise ToolExecutionError("隔离执行器异常终止。")
    try:
        data = json.loads(stdout)
        return CodeInterpreterOutput.model_validate(data)
    except Exception:
        raise ToolExecutionError("隔离执行器返回无效结果。") from None


def code_interpreter(
    arguments: CodeInterpreterArguments,
    cancellation_handle: ProcessCancellationHandle | None = None,
) -> CodeInterpreterOutput:
    """执行白名单内的纯 Python 计算；越权代码在创建子进程前拒绝。"""
    arguments = CodeInterpreterArguments.model_validate(arguments.model_dump())
    settings = validate_code_interpreter_policy(arguments)
    return _run_child(
        arguments.code,
        timeout_seconds=arguments.timeout_seconds,
        max_memory_mb=settings.max_memory_mb,
        max_processes=settings.max_processes,
        max_output_characters=settings.max_output_characters,
        cancellation_handle=cancellation_handle,
    )
