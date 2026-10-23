"""Internal remote tool-group adapter for one Aliyun Sandbox lease.

This layer is deliberately not registered with the Agent.  It translates the
existing tool-shaped models to a typed ``SandboxTransport`` while keeping the
control plane's approval, policy, and scheduling decisions outside the remote
execution primitive.  No E2B SDK object or credential is returned here.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import Any, Final

from pydantic import BaseModel

from app.execution_backend import (
    AliyunSandboxExecutionBackend,
    ExecutionBackendClosedError,
    ExecutionLease,
)
from app.security_domain import path_is_sealed_secret
from app.schemas import (
    BashShellArguments,
    BashShellOutput,
    CodeInterpreterArguments,
    CodeInterpreterOutput,
    EditorFileArguments,
    EditorFileOutput,
    GrepArguments,
    GrepMatch,
    GrepOutput,
    ListFileEntry,
    ListFilesArguments,
    ListFilesOutput,
    ReadFileArguments,
    ReadFileOutput,
    WriteFileArguments,
    WriteFileOutput,
)
from app.sandbox_transport import (
    SandboxCommandResult,
    SandboxEditResult,
    SandboxFileEntry,
    SandboxGrepResult,
    SandboxGrepMatch,
    SandboxListResult,
    SandboxReadResult,
    SandboxRunHandle,
    SandboxTransport,
    SandboxWriteResult,
    validate_sandbox_relative_path,
)
from tool.code_interpreter import validate_code_interpreter_policy
from tool.policy import (
    CodeInterpreterPolicy,
    PolicyLoadError,
    load_tool_policy,
    path_is_allowed,
    path_is_protected,
)
from tool.project_scan import DEFAULT_IGNORED_DIRECTORIES, matches_glob
from tool.file_access import ToolAccessDenied, ToolExecutionError


_MAX_REMOTE_FILE_BYTES: Final[int] = 8 * 1024 * 1024
_MAX_REMOTE_LIST_RESULTS: Final[int] = 1000
_MAX_REMOTE_GREP_RESULTS: Final[int] = 1000
_MAX_REMOTE_GREP_FILES: Final[int] = 20_000
_MAX_COMMAND_OUTPUT_CHARS: Final[int] = 32_768
_MAX_CODE_ERROR_CHARS: Final[int] = 1_024
_MAX_TRANSPORT_OUTPUT_BYTES: Final[int] = 128 * 1024


class RemoteSandboxToolError(RuntimeError):
    """Safe local error for a rejected or failed remote primitive."""


class RemoteSandboxClosedError(RemoteSandboxToolError):
    """The associated execution backend is closed or unavailable."""


class RemoteSandboxApprovalRequired(RemoteSandboxToolError):
    """The control plane did not explicitly authorize an execution primitive."""


class RemoteSandboxPolicyDenied(RemoteSandboxToolError):
    """The latest host policy rejected an authorized remote primitive."""


def _safe_relative_path(value: str, *, field_name: str = "路径") -> str:
    try:
        normalized = validate_sandbox_relative_path(value)
    except (TypeError, UnicodeError, ValueError):
        raise RemoteSandboxToolError(
            f"{field_name} 必须是安全的 POSIX 相对路径。"
        ) from None
    if path_is_sealed_secret(normalized):
        raise RemoteSandboxPolicyDenied("远端工作区密封秘密路径拒绝访问。")
    return normalized


def _safe_glob(value: str, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.startswith("\\")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or len(value.encode("utf-8")) > 256
        or any(
            ord(character) < 32 or unicodedata.category(character) == "Cc"
            for character in value
        )
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise RemoteSandboxToolError(
            f"{field_name} 必须是安全的 POSIX 相对 glob。"
        )
    if path_is_sealed_secret(value):
        raise RemoteSandboxPolicyDenied("远端工作区密封秘密路径拒绝扫描。")
    return value


def _under_workspace(workspace_root: str, relative_path: str) -> str:
    safe_relative = _safe_relative_path(relative_path)
    candidate = posixpath.join(workspace_root, safe_relative)
    prefix = workspace_root.rstrip("/") + "/"
    if not candidate.startswith(prefix):
        raise RemoteSandboxToolError("远端路径越出 lease workspace_root。")
    return candidate


def _relative_from_workspace(workspace_root: str, value: str) -> str:
    if not isinstance(value, str):
        raise RemoteSandboxToolError("远端 transport 返回了无效路径。")
    prefix = workspace_root.rstrip("/") + "/"
    if not value.startswith(prefix):
        raise RemoteSandboxToolError("远端 transport 返回了 workspace 外路径。")
    return _safe_relative_path(value[len(prefix):], field_name="远端返回路径")


def _workspace_path_predicate() -> Callable[[str, bool], bool]:
    """Snapshot the current host policy for one remote list/search operation."""
    try:
        policy = load_tool_policy()
    except PolicyLoadError:
        raise RemoteSandboxToolError("远端扫描的宿主路径策略不可用。") from None

    def allowed(relative_path: str, is_directory: bool) -> bool:
        if type(is_directory) is not bool:
            return False
        try:
            normalized = _safe_relative_path(
                relative_path, field_name="远端策略路径"
            )
        except RemoteSandboxToolError:
            return False
        if is_directory:
            leaf = normalized.rsplit("/", 1)[-1]
            return (
                leaf not in DEFAULT_IGNORED_DIRECTORIES
                and not path_is_protected(normalized, policy)
            )
        return path_is_allowed(normalized, policy)

    return allowed


def _bounded_text(value: Any, maximum: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise RemoteSandboxToolError("远端 transport 返回了非文本输出。")
    if len(value) <= maximum:
        return value, False
    marker = "\n...[远端输出已截断]"
    return value[: maximum - len(marker)] + marker, True


def _validate_timeout(value: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise RemoteSandboxToolError("远端操作超出允许的超时范围。")
    return value


class AliyunSandboxToolGroup:
    """Typed read/write/search/command primitives bound to one lease."""

    def __init__(
        self,
        backend: AliyunSandboxExecutionBackend,
        *,
        authorization_consumer: Callable[[str, BaseModel, object | None, str], None]
        | None = None,
    ) -> None:
        if not isinstance(backend, AliyunSandboxExecutionBackend):
            raise TypeError("backend 必须是 AliyunSandboxExecutionBackend。")
        if authorization_consumer is not None and not callable(authorization_consumer):
            raise TypeError("authorization_consumer 必须可调用。")
        self._backend = backend
        self._authorization_consumer = authorization_consumer

    def _consume_authorization(
        self,
        tool_name: str,
        arguments: BaseModel,
        authorization: object | None,
        authorization_kind: str,
    ) -> None:
        if self._authorization_consumer is None:
            raise RemoteSandboxApprovalRequired(
                "远端工具没有绑定可信控制面授权消费者。"
            )
        try:
            self._authorization_consumer(
                tool_name,
                arguments,
                authorization,
                authorization_kind,
            )
        except RemoteSandboxApprovalRequired:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise RemoteSandboxApprovalRequired("远端工具授权无效或已失效。") from None

    def ensure_ready(self) -> ExecutionLease:
        return self._backend.ensure_ready()

    def close(self) -> None:
        self._backend.close()

    def _workspace_root(self) -> str:
        try:
            return self._backend.ensure_ready().workspace_root
        except ExecutionBackendClosedError:
            raise RemoteSandboxClosedError(
                "远端 Sandbox backend 已关闭或未绑定 transport。"
            ) from None
        except Exception:
            raise RemoteSandboxToolError("远端 Sandbox backend 不可用。") from None

    def _call(self, operation: Callable[[SandboxTransport], Any]) -> Any:
        try:
            self._backend.ensure_ready()
            return self._backend._with_transport(operation)
        except RemoteSandboxToolError:
            raise
        except ExecutionBackendClosedError as error:
            raise RemoteSandboxClosedError(
                "远端 Sandbox backend 已关闭或未绑定 transport。"
            ) from None
        except Exception:
            raise RemoteSandboxToolError("远端 Sandbox 操作失败。") from None

    @staticmethod
    def _run_transport(
        transport: SandboxTransport,
        command: str,
        *,
        paths: Sequence[str],
        code: str | None,
        cwd: str,
        timeout_seconds: int,
        max_output_bytes: int,
        max_output_characters: int | None = None,
        max_memory_mb: int | None = None,
        max_processes: int | None = None,
        handle_consumer: Callable[[SandboxRunHandle], None] | None = None,
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> SandboxCommandResult:
        keyword_arguments = {
            "paths": paths,
            "code": code,
            "cwd": cwd,
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "max_output_characters": max_output_characters,
            "max_memory_mb": max_memory_mb,
            "max_processes": max_processes,
        }
        if handle_consumer is None and dispatch_attempt_consumer is None:
            return transport.run(command, **keyword_arguments)
        if handle_consumer is None or dispatch_attempt_consumer is None:
            raise RemoteSandboxToolError("远端执行生命周期消费者不完整。")
        handle = transport.start_run(
            command,
            **keyword_arguments,
            dispatch_attempt_consumer=dispatch_attempt_consumer,
        )
        handle_consumer(handle)
        return handle.wait()

    def read_file(self, arguments: ReadFileArguments) -> ReadFileOutput:
        arguments = ReadFileArguments.model_validate(arguments.model_dump())
        timeout = 30
        relative_path = _safe_relative_path(arguments.path)
        remote_path = _under_workspace(self._workspace_root(), relative_path)

        def operation(transport: SandboxTransport) -> SandboxReadResult:
            return transport.read_file(
                remote_path,
                offset=arguments.offset,
                max_bytes=arguments.max_bytes,
                timeout_seconds=timeout,
            )

        result = self._call(operation)
        if not isinstance(result, SandboxReadResult):
            raise RemoteSandboxToolError("远端读取返回了无效结果。")
        if (
            not isinstance(result.total_bytes, int)
            or isinstance(result.total_bytes, bool)
            or result.total_bytes < 0
            or result.total_bytes > _MAX_REMOTE_FILE_BYTES
            or not isinstance(result.data, bytes)
            or len(result.data) > arguments.max_bytes
            or arguments.offset + len(result.data) > result.total_bytes
        ):
            raise RemoteSandboxToolError("远端读取结果超出安全范围。")
        try:
            content = result.data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(result.data).decode("ascii")
            encoding = "base64"
        return ReadFileOutput(
            path=arguments.path,
            encoding=encoding,
            content=content,
            offset=arguments.offset,
            bytes_read=len(result.data),
            total_bytes=result.total_bytes,
            truncated=arguments.offset + len(result.data) < result.total_bytes,
        )

    def write_file(
        self,
        arguments: WriteFileArguments,
        *,
        authorization: object | None = None,
    ) -> WriteFileOutput:
        arguments = WriteFileArguments.model_validate(arguments.model_dump())
        self._consume_authorization(
            "write_file", arguments, authorization, "approval"
        )
        raw = arguments.content.encode(arguments.encoding)
        if len(raw) > _MAX_REMOTE_FILE_BYTES:
            raise RemoteSandboxToolError("远端写入内容超过大小上限。")
        relative_path = _safe_relative_path(arguments.path)
        workspace_root = self._workspace_root()
        remote_path = _under_workspace(workspace_root, relative_path)

        def operation(transport: SandboxTransport):
            return transport.write_file(
                remote_path,
                raw,
                overwrite=arguments.mode == "overwrite",
                timeout_seconds=30,
            )

        result = self._call(operation)
        if (
            not isinstance(result, SandboxWriteResult)
            or not isinstance(result.bytes_written, int)
            or isinstance(result.bytes_written, bool)
            or result.bytes_written != len(raw)
        ):
            raise RemoteSandboxToolError("远端写入结果与请求不一致。")
        return WriteFileOutput(
            path=arguments.path,
            encoding=arguments.encoding,
            operation="created" if arguments.mode == "create" else "overwritten",
            bytes_written=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    def editor_file(
        self,
        arguments: EditorFileArguments,
        *,
        authorization: object | None = None,
    ) -> EditorFileOutput:
        arguments = EditorFileArguments.model_validate(arguments.model_dump())
        self._consume_authorization(
            "editor_file", arguments, authorization, "approval"
        )
        assert arguments.old_text is not None
        relative_path = _safe_relative_path(arguments.path)
        workspace_root = self._workspace_root()
        remote_path = _under_workspace(workspace_root, relative_path)
        timeout = 30

        def operation(transport: SandboxTransport) -> SandboxEditResult:
            return transport.edit_file(
                remote_path,
                operation=arguments.operation,
                old_text=arguments.old_text,
                new_text=arguments.new_text,
                expected_replacements=arguments.expected_replacements,
                position=arguments.position,
                timeout_seconds=timeout,
            )

        result = self._call(operation)
        if not isinstance(result, SandboxEditResult):
            raise RemoteSandboxToolError("远端编辑返回了无效结果。")
        if (
            not isinstance(result.encoding, str)
            or not isinstance(result.replacements, int)
            or isinstance(result.replacements, bool)
            or result.encoding not in {"utf-8", "utf-8-sig"}
            or result.replacements != arguments.expected_replacements
            or not 1 <= result.replacements <= 1000
            or not isinstance(result.bytes_before, int)
            or isinstance(result.bytes_before, bool)
            or not isinstance(result.bytes_after, int)
            or isinstance(result.bytes_after, bool)
            or not 0 <= result.bytes_before <= _MAX_REMOTE_FILE_BYTES
            or not 0 <= result.bytes_after <= _MAX_REMOTE_FILE_BYTES
            or not isinstance(result.sha256_before, str)
            or not isinstance(result.sha256_after, str)
            or len(result.sha256_before) != 64
            or len(result.sha256_after) != 64
            or result.sha256_before == result.sha256_after
        ):
            raise RemoteSandboxToolError("远端编辑结果超出安全范围。")
        return EditorFileOutput(
            path=arguments.path,
            encoding=result.encoding,
            operation=result.operation if hasattr(result, "operation") else arguments.operation,
            replacements=result.replacements,
            bytes_before=result.bytes_before,
            bytes_after=result.bytes_after,
            sha256_before=result.sha256_before,
            sha256_after=result.sha256_after,
        )

    def list_files(self, arguments: ListFilesArguments) -> ListFilesOutput:
        arguments = ListFilesArguments.model_validate(arguments.model_dump())
        pattern = _safe_glob(arguments.pattern, field_name="list_files pattern")
        path_predicate = _workspace_path_predicate()
        timeout = 30
        workspace_root = self._workspace_root()
        result = self._call(
            lambda transport: transport.list_files(
                pattern,
                workspace_root=workspace_root,
                max_results=arguments.max_results,
                timeout_seconds=timeout,
                path_predicate=path_predicate,
            )
        )
        if not isinstance(result, SandboxListResult):
            raise RemoteSandboxToolError("远端列举返回了无效结果。")
        if not isinstance(result.entries, tuple) or not isinstance(result.truncated, bool):
            raise RemoteSandboxToolError("远端列举返回了无效结果。")
        if len(result.entries) > min(arguments.max_results, _MAX_REMOTE_LIST_RESULTS):
            raise RemoteSandboxToolError("远端列举结果超过数量上限。")
        entries: list[ListFileEntry] = []
        for item in result.entries:
            if not isinstance(item, SandboxFileEntry):
                raise RemoteSandboxToolError("远端列举返回了无效条目。")
            path = _relative_from_workspace(workspace_root, item.path)
            if path_predicate(path, False) is not True:
                raise RemoteSandboxToolError("远端列举返回了策略禁止的路径。")
            if (
                not isinstance(item.size, int)
                or isinstance(item.size, bool)
                or item.size < 0
                or item.size > _MAX_REMOTE_FILE_BYTES
            ):
                raise RemoteSandboxToolError("远端列举返回了无效文件大小。")
            if not matches_glob(path, pattern):
                raise RemoteSandboxToolError("远端列举返回了不匹配的文件。")
            entries.append(ListFileEntry(path=path, size=item.size))
        entries.sort(key=lambda item: item.path)
        if len({item.path for item in entries}) != len(entries):
            raise RemoteSandboxToolError("远端列举返回了重复路径。")
        return ListFilesOutput(
            pattern=pattern,
            entries=entries,
            count=len(entries),
            truncated=bool(result.truncated),
        )

    def grep(self, arguments: GrepArguments) -> GrepOutput:
        arguments = GrepArguments.model_validate(arguments.model_dump())
        pattern = arguments.pattern
        file_pattern = _safe_glob(arguments.file_pattern, field_name="grep file_pattern")
        path_predicate = _workspace_path_predicate()
        workspace_root = self._workspace_root()
        result = self._call(
            lambda transport: transport.grep(
                pattern,
                workspace_root=workspace_root,
                file_pattern=file_pattern,
                case_sensitive=arguments.case_sensitive,
                max_results=arguments.max_results,
                timeout_seconds=30,
                path_predicate=path_predicate,
            )
        )
        if not isinstance(result, SandboxGrepResult):
            raise RemoteSandboxToolError("远端搜索返回了无效结果。")
        if not isinstance(result.matches, tuple) or not isinstance(result.truncated, bool):
            raise RemoteSandboxToolError("远端搜索返回了无效结果。")
        if (
            not isinstance(result.files_searched, int)
            or isinstance(result.files_searched, bool)
            or not 0 <= result.files_searched <= _MAX_REMOTE_GREP_FILES
        ):
            raise RemoteSandboxToolError("远端搜索文件数超过上限。")
        matches: list[GrepMatch] = []
        for item in result.matches:
            if (
                not isinstance(item, SandboxGrepMatch)
                or not isinstance(item.line_number, int)
                or isinstance(item.line_number, bool)
                or item.line_number < 1
                or not isinstance(item.column, int)
                or isinstance(item.column, bool)
                or item.column < 1
                or not isinstance(item.text, str)
                or not item.text
            ):
                raise RemoteSandboxToolError("远端搜索返回了无效匹配。")
            path = _relative_from_workspace(workspace_root, item.path)
            if path_predicate(path, False) is not True:
                raise RemoteSandboxToolError("远端搜索返回了策略禁止的路径。")
            text, text_truncated = _bounded_text(item.text, 500)
            if text_truncated or "\n" in text or "\r" in text or "\x00" in text:
                raise RemoteSandboxToolError("远端搜索片段超出安全范围。")
            haystack = text if arguments.case_sensitive else text.casefold()
            needle = pattern if arguments.case_sensitive else pattern.casefold()
            if needle not in haystack or not matches_glob(path, file_pattern):
                raise RemoteSandboxToolError("远端搜索返回了不匹配结果。")
            matches.append(
                GrepMatch(
                    path=path,
                    line_number=item.line_number,
                    column=item.column,
                    text=text,
                )
            )
        if len(matches) > min(arguments.max_results, _MAX_REMOTE_GREP_RESULTS):
            raise RemoteSandboxToolError("远端搜索结果超过数量上限。")
        match_keys = [(item.path, item.line_number, item.column) for item in matches]
        if match_keys != sorted(match_keys) or len(match_keys) != len(set(match_keys)):
            raise RemoteSandboxToolError("远端搜索结果未稳定排序或存在重复。")
        return GrepOutput(
            pattern=pattern,
            file_pattern=file_pattern,
            case_sensitive=arguments.case_sensitive,
            matches=matches,
            count=len(matches),
            files_searched=result.files_searched,
            truncated=bool(result.truncated),
        )

    def command(
        self,
        arguments: BashShellArguments,
        *,
        authorization: object | None = None,
        run_handle_consumer: Callable[[SandboxRunHandle], None] | None = None,
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> BashShellOutput:
        arguments = BashShellArguments.model_validate(arguments.model_dump())
        self._consume_authorization(
            "bash_shell", arguments, authorization, "approval"
        )
        try:
            policy = load_tool_policy()
        except PolicyLoadError:
            raise RemoteSandboxPolicyDenied(
                "远端命令的最新宿主策略不可用。"
            ) from None
        command_policy = policy.shell_commands.get(arguments.command)
        if (
            command_policy is None
            or not command_policy.enabled
            or len(arguments.paths) > command_policy.max_paths
        ):
            raise RemoteSandboxPolicyDenied("远端命令未通过最新宿主策略。")
        if arguments.command in {"remove_file", "clear_file", "clear_directory"}:
            raise RemoteSandboxApprovalRequired(
                "远端命令组不执行需要宿主文件审批的破坏性操作。"
            )
        relative_paths = tuple(
            _safe_relative_path(path, field_name="命令路径") for path in arguments.paths
        )
        workspace_root = self._workspace_root()
        remote_paths = tuple(
            _under_workspace(workspace_root, path) for path in relative_paths
        )
        result = self._call(
            lambda transport: self._run_transport(
                transport,
                arguments.command,
                paths=remote_paths,
                code=None,
                cwd=workspace_root,
                timeout_seconds=_validate_timeout(arguments.timeout_seconds, 120),
                max_output_bytes=_MAX_TRANSPORT_OUTPUT_BYTES,
                handle_consumer=run_handle_consumer,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        )
        return self._command_output(arguments, result, relative_paths)

    run_command = command

    def code_interpreter(
        self,
        arguments: CodeInterpreterArguments,
        *,
        authorization: object | None = None,
        run_handle_consumer: Callable[[SandboxRunHandle], None] | None = None,
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> CodeInterpreterOutput:
        arguments = CodeInterpreterArguments.model_validate(arguments.model_dump())
        self._consume_authorization(
            "code_interpreter", arguments, authorization, "policy"
        )
        settings = self._validate_code_policy(arguments)
        workspace_root = self._workspace_root()
        timeout_seconds = _validate_timeout(
            arguments.timeout_seconds,
            settings.max_timeout_seconds,
        )
        result = self._call(
            lambda transport: self._run_transport(
                transport,
                "python",
                paths=(),
                code=arguments.code,
                cwd=workspace_root,
                timeout_seconds=timeout_seconds,
                max_output_bytes=_MAX_TRANSPORT_OUTPUT_BYTES,
                max_output_characters=settings.max_output_characters,
                max_memory_mb=settings.max_memory_mb,
                max_processes=settings.max_processes,
                handle_consumer=run_handle_consumer,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        )
        if not isinstance(result, SandboxCommandResult):
            raise RemoteSandboxToolError("远端 Python 返回了无效结果。")
        stdout, stdout_truncated = _bounded_text(
            result.stdout,
            settings.max_output_characters,
        )
        expression, expression_truncated = _bounded_text(
            result.result,
            settings.max_output_characters,
        )
        stderr, stderr_truncated = _bounded_text(result.stderr, _MAX_CODE_ERROR_CHARS)
        if not isinstance(result.exit_code, int) or isinstance(result.exit_code, bool):
            raise RemoteSandboxToolError("远端 Python 返回了无效退出码。")
        if not isinstance(result.truncated, bool):
            raise RemoteSandboxToolError("远端 Python 返回了无效截断标记。")
        if result.exit_code not in {0, 1}:
            raise RemoteSandboxToolError("远端 Python 返回了无效退出码。")
        return CodeInterpreterOutput(
            exit_code=result.exit_code,
            stdout=stdout,
            result=expression,
            stderr=stderr,
            truncated=bool(result.truncated)
            or stdout_truncated
            or expression_truncated
            or stderr_truncated,
        )

    run_code = code_interpreter

    @staticmethod
    def _validate_code_policy(
        arguments: CodeInterpreterArguments,
    ) -> CodeInterpreterPolicy:
        try:
            return validate_code_interpreter_policy(arguments)
        except (ToolAccessDenied, ToolExecutionError):
            raise RemoteSandboxPolicyDenied(
                "远端 Python 代码未通过安全策略。"
            ) from None
        except Exception:
            raise RemoteSandboxToolError("远端 Python 安全策略不可用。") from None

    @staticmethod
    def _command_output(
        arguments: BashShellArguments,
        result: Any,
        paths: Sequence[str],
    ) -> BashShellOutput:
        if not isinstance(result, SandboxCommandResult):
            raise RemoteSandboxToolError("远端命令返回了无效结果。")
        if not isinstance(result.truncated, bool):
            raise RemoteSandboxToolError("远端命令返回了无效截断标记。")
        stdout, stdout_truncated = _bounded_text(result.stdout, _MAX_COMMAND_OUTPUT_CHARS)
        stderr, stderr_truncated = _bounded_text(result.stderr, _MAX_COMMAND_OUTPUT_CHARS)
        if not isinstance(result.exit_code, int) or isinstance(result.exit_code, bool):
            raise RemoteSandboxToolError("远端命令返回了无效退出码。")
        return BashShellOutput(
            command=arguments.command,
            paths=list(paths),
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=bool(result.truncated) or stdout_truncated or stderr_truncated,
            result="",
        )

    def __repr__(self) -> str:
        return "AliyunSandboxToolGroup(backend=private-lease)"


__all__ = [
    "AliyunSandboxToolGroup",
    "RemoteSandboxApprovalRequired",
    "RemoteSandboxClosedError",
    "RemoteSandboxPolicyDenied",
    "RemoteSandboxToolError",
]
