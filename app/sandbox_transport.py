"""Typed transport seam and narrow E2B 2.49.1 adapter.

The raw SDK object is accepted only by :class:`E2BSandboxTransport` and is
never returned by the execution backend or tool group.  Importing this module
does not import E2B; the SDK is reached only after a sandbox has been created.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import json
import posixpath
import shlex
import unicodedata
from pathlib import Path
from pathlib import PurePosixPath
from secrets import token_hex
from threading import Lock
from typing import Any, Protocol

from tool.editor_file import _apply_edit
from tool.grep import MAX_EXCERPT_CHARACTERS, MAX_SEARCHABLE_FILE_BYTES, _excerpt, _positions


SandboxPathPredicate = Callable[[str, bool], bool]

_TRUSTED_RUNNER_TIMEOUT_MAX_SECONDS = 120
_PROVIDER_RUNNER_TIMEOUT_GRACE_SECONDS = 5
_PROVIDER_RUNNER_TIMEOUT_MAX_SECONDS = (
    _TRUSTED_RUNNER_TIMEOUT_MAX_SECONDS
    + _PROVIDER_RUNNER_TIMEOUT_GRACE_SECONDS
)


def _provider_runner_timeout_seconds(timeout_seconds: int) -> int:
    """Bound provider waiting beyond, but not user code beyond, its deadline."""

    if (
        type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= _TRUSTED_RUNNER_TIMEOUT_MAX_SECONDS
    ):
        raise ValueError("可信 runner 超时必须在允许范围内。")
    provider_timeout = timeout_seconds + _PROVIDER_RUNNER_TIMEOUT_GRACE_SECONDS
    if provider_timeout > _PROVIDER_RUNNER_TIMEOUT_MAX_SECONDS:
        raise ValueError("provider runner 超时超出允许范围。")
    return provider_timeout


def validate_sandbox_relative_path(value: str) -> str:
    """Validate a user workspace path without consulting either filesystem."""
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or value.startswith("\\")
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
        or len(value.encode("utf-8")) > 4096
        or posixpath.normpath(value) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("远端路径必须是安全的 POSIX 相对路径。")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 对象包含重复键。")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class SandboxReadResult:
    data: bytes
    total_bytes: int


@dataclass(frozen=True, slots=True)
class SandboxWriteResult:
    bytes_written: int


@dataclass(frozen=True, slots=True)
class SandboxEditResult:
    encoding: str
    replacements: int
    bytes_before: int
    bytes_after: int
    sha256_before: str
    sha256_after: str


@dataclass(frozen=True, slots=True)
class SandboxFileEntry:
    path: str
    size: int


@dataclass(frozen=True, slots=True)
class SandboxListResult:
    entries: tuple[SandboxFileEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SandboxPersistenceEntry:
    """One regular file or directory below an immutable checkpoint root."""

    path: str
    is_directory: bool
    size: int


@dataclass(frozen=True, slots=True)
class SandboxPersistenceListResult:
    entries: tuple[SandboxPersistenceEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class SandboxGrepMatch:
    path: str
    line_number: int
    column: int
    text: str


@dataclass(frozen=True, slots=True)
class SandboxGrepResult:
    matches: tuple[SandboxGrepMatch, ...]
    files_searched: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class SandboxCommandResult:
    exit_code: int
    stdout: str
    stderr: str
    result: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SandboxFormatJsonFile:
    path: str
    before_sha256: str
    after: bytes


@dataclass(frozen=True, slots=True)
class SandboxFormatJsonResult:
    files: tuple[SandboxFormatJsonFile, ...]


@dataclass(frozen=True, slots=True)
class SandboxRuntimeIdentity:
    """Verified, non-secret identity of the control-plane Python runtime."""

    python_path: str
    system_python_path: str
    version: tuple[int, int, int]
    resolved_python_path: str
    executable_sha256: str
    metadata_digest: str
    runtime_user: str
    digest: str


class SandboxRunHandle(Protocol):
    """One already-started remote command with physical cancellation."""

    def wait(self) -> SandboxCommandResult: ...

    def cancel(self) -> None: ...


class SandboxTransport(Protocol):
    """Typed operations bound to exactly one Sandbox instance."""

    def read_file(
        self,
        path: str,
        *,
        offset: int,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult: ...

    def write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ) -> SandboxWriteResult: ...

    def edit_file(
        self,
        path: str,
        *,
        operation: str,
        old_text: str,
        new_text: str | None,
        expected_replacements: int,
        position: str | None,
        timeout_seconds: int,
    ) -> SandboxEditResult: ...

    def list_files(
        self,
        pattern: str,
        *,
        workspace_root: str,
        max_results: int,
        timeout_seconds: int,
        path_predicate: SandboxPathPredicate,
    ) -> SandboxListResult: ...

    def grep(
        self,
        pattern: str,
        *,
        workspace_root: str,
        file_pattern: str,
        case_sensitive: bool,
        max_results: int,
        timeout_seconds: int,
        path_predicate: SandboxPathPredicate,
    ) -> SandboxGrepResult: ...

    def run(
        self,
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
    ) -> SandboxCommandResult: ...

    def start_run(
        self,
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
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> SandboxRunHandle: ...

    def format_json_candidates(
        self,
        paths: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> SandboxFormatJsonResult: ...

    def persistence_read_file(
        self,
        path: str,
        *,
        offset: int,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult: ...

    def persistence_write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ) -> SandboxWriteResult: ...

    def persistence_list_files(
        self,
        checkpoint_root: str,
        *,
        max_results: int,
        max_depth: int,
        timeout_seconds: int,
    ) -> SandboxPersistenceListResult: ...

    def workspace_version_read_file(
        self,
        path: str,
        *,
        offset: int,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult: ...

    def workspace_version_probe_file(
        self,
        path: str,
        *,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult | None: ...

    def workspace_version_write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ) -> SandboxWriteResult: ...

    def workspace_version_list_files(
        self,
        session_root: str,
        *,
        max_results: int,
        max_depth: int,
        timeout_seconds: int,
    ) -> SandboxPersistenceListResult: ...

    def rollback_delete_workspace_version_object(
        self,
        path: str,
        *,
        expected_size: int,
        expected_sha256: str,
        timeout_seconds: int,
    ) -> None: ...

    def gc_delete_workspace_version_object(
        self,
        path: str,
        *,
        expected_size: int,
        expected_sha256: str,
        timeout_seconds: int,
    ) -> None: ...

    def rollback_delete_file(
        self,
        path: str,
        *,
        expected_sha256: str,
        timeout_seconds: int,
    ) -> None: ...


class SandboxPersistenceTransport(Protocol):
    """Host-only durable storage operations bound to a persistence Sandbox.

    This deliberately omits workspace and command methods.  The persistence
    Sandbox is a control-plane storage endpoint, not another execution target.
    """

    def persistence_read_file(self, *args: Any, **kwargs: Any) -> SandboxReadResult: ...

    def persistence_write_file(self, *args: Any, **kwargs: Any) -> SandboxWriteResult: ...

    def persistence_list_files(
        self, *args: Any, **kwargs: Any
    ) -> SandboxPersistenceListResult: ...

    def workspace_version_read_file(
        self, *args: Any, **kwargs: Any
    ) -> SandboxReadResult: ...

    def workspace_version_probe_file(
        self, *args: Any, **kwargs: Any
    ) -> SandboxReadResult | None: ...

    def workspace_version_write_file(
        self, *args: Any, **kwargs: Any
    ) -> SandboxWriteResult: ...

    def workspace_version_list_files(
        self, *args: Any, **kwargs: Any
    ) -> SandboxPersistenceListResult: ...

    def rollback_delete_workspace_version_object(
        self, *args: Any, **kwargs: Any
    ) -> None: ...

    def gc_delete_workspace_version_object(
        self, *args: Any, **kwargs: Any
    ) -> None: ...


class _E2BRunHandle:
    """Narrow wrapper around E2B's synchronous ``CommandHandle`` surface."""

    def __init__(
        self,
        raw_handle: Any,
        *,
        decode: Callable[[Any], SandboxCommandResult],
        cleanup: Callable[[], None],
    ) -> None:
        if not callable(getattr(raw_handle, "wait", None)) or not callable(
            getattr(raw_handle, "kill", None)
        ):
            raise ValueError("远端后台命令没有可等待和取消的句柄。")
        self._raw_handle = raw_handle
        self._decode = decode
        self._cleanup = cleanup
        self._state_lock = Lock()
        self._cleanup_lock = Lock()
        self._cleanup_done = False
        self._state = "running"
        self._result: SandboxCommandResult | None = None

    def _cleanup_once(self) -> None:
        with self._cleanup_lock:
            if self._cleanup_done:
                return
            self._cleanup_done = True
            self._cleanup()

    def wait(self) -> SandboxCommandResult:
        with self._state_lock:
            if self._state == "completed":
                assert self._result is not None
                return self._result
            if self._state == "cancelled":
                raise ValueError("远端后台命令已取消。")
            if self._state != "running":
                raise ValueError("远端后台命令句柄状态无效。")
            self._state = "waiting"
        try:
            try:
                raw_result = self._raw_handle.wait()
            except Exception as error:
                if not all(hasattr(error, name) for name in ("stdout", "stderr", "exit_code")):
                    raise ValueError("远端后台命令等待失败。") from None
                raw_result = error
            result = self._decode(raw_result)
            self._cleanup_once()
        except Exception:
            try:
                self._cleanup_once()
            except Exception:
                pass
            with self._state_lock:
                if self._state != "cancelled":
                    self._state = "failed"
            raise
        with self._state_lock:
            if self._state == "cancelled":
                raise ValueError("远端后台命令在完成竞争中被取消。")
            self._result = result
            self._state = "completed"
            return result

    def cancel(self) -> None:
        with self._state_lock:
            if self._state in {"completed", "cancelled"}:
                return
            if self._state == "failed":
                raise ValueError("远端后台命令终态无法确认。")
            self._state = "cancelled"
        try:
            killed = self._raw_handle.kill()
            if killed is not True:
                raise ValueError("远端后台命令 kill 返回失败状态。")
            self._cleanup_once()
        except Exception:
            raise ValueError("远端后台命令无法确认已终止。") from None


class E2BSandboxTransport:
    """Small synchronous adapter for the installed E2B 2.49.1 surface.

    ``workspace_root`` is fixed at construction.  Every SDK call first checks
    the path chain with ``Filesystem.list`` so a symlink or special file cannot
    redirect an operation into the OSS mount or another sandbox directory.
    """

    _PERSISTENCE_NAMESPACE = ".agnet-study/checkpoints/v1"
    _VERSION_NAMESPACE = ".agnet-study/checkpoints/v2"
    # Snapshot bodies are independently capped at 1 MiB.  Persistence must
    # also carry the canonical manifest, whose worst case includes 1,000
    # bounded paths and therefore needs a separate, still-bounded allowance.
    _MAX_PERSISTENCE_FILE_BYTES = 8 * 1024 * 1024
    _MAX_VERSION_OBJECT_BYTES = 1 * 1024 * 1024
    _MAX_PERSISTENCE_RESULTS = 20_000
    _MAX_PERSISTENCE_DEPTH = 64

    def __init__(
        self,
        sandbox: Any,
        workspace_root: str,
        persistence_root: str | None = None,
        runtime_venv_dir: str = "/opt/agnet-study/runtime/venv",
        runtime_user: str = "user",
    ) -> None:
        if not isinstance(workspace_root, str) or not workspace_root.startswith("/"):
            raise ValueError("workspace_root 必须是绝对 POSIX 路径。")
        self._sandbox = sandbox
        self._workspace_root = workspace_root.rstrip("/")
        if persistence_root is None:
            self._persistence_root = None
        else:
            validated_persistence_root = self._validate_absolute_root(
                persistence_root,
                "persistence_root",
            )
            if not validated_persistence_root.startswith("/mnt/"):
                raise ValueError("persistence_root 必须位于可信 /mnt 后代。")
            self._persistence_root = validated_persistence_root
        self._runtime_venv_dir = self._validate_runtime_root(runtime_venv_dir)
        self._runtime_python = self._runtime_venv_dir + "/bin/python"
        if (
            not isinstance(runtime_user, str)
            or runtime_user in {"", "root", "0"}
            or len(runtime_user) > 32
            or runtime_user[0] not in "abcdefghijklmnopqrstuvwxyz_"
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in runtime_user
            )
        ):
            raise ValueError("runtime_user 必须是固定非特权 Unix 用户。")
        self._runtime_user = runtime_user
        self._code_dir = self._workspace_root + "/.agent-internal"
        self._ensure_workspace(30)
        if self._persistence_root is not None:
            self._assert_persistence_directory(self._persistence_root, 30)
        system_python = self._discover_system_python(30)
        if self._persistence_root is not None:
            self._isolate_persistence_root(system_python[0], 30)
        self._runtime_identity = self._initialize_trusted_runtime(
            30,
            discovered=system_python,
        )

    def __repr__(self) -> str:
        return "E2BSandboxTransport(workspace_root={!r}, persistence_bound={!r})".format(
            self._workspace_root,
            self._persistence_root is not None,
        )

    @property
    def runtime_identity(self) -> SandboxRuntimeIdentity:
        return self._runtime_identity

    def _validate_runtime_root(self, value: str) -> str:
        root = self._validate_absolute_root(value, "runtime_venv_dir")
        if (
            root == "/workspace"
            or root.startswith("/workspace/")
            or root == "/mnt"
            or root.startswith("/mnt/")
            or root == self._workspace_root
            or root.startswith(self._workspace_root + "/")
            or (
                self._persistence_root is not None
                and (root == self._persistence_root or root.startswith(self._persistence_root + "/"))
            )
        ):
            raise ValueError("可信 runtime 必须位于 active workspace 和 OSS 之外。")
        return root

    @staticmethod
    def _validate_absolute_root(value: str, field_name: str) -> str:
        if (
            not isinstance(value, str)
            or value in {"", "/"}
            or not value.startswith("/")
            or value.startswith("//")
            or value.endswith("/")
            or "\\" in value
            or "\x00" in value
            or any(unicodedata.category(character) == "Cc" for character in value)
            or len(value.encode("utf-8")) > 4096
            or posixpath.normpath(value) != value
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])
        ):
            raise ValueError(f"{field_name} 必须是安全的绝对 POSIX 路径。")
        return value

    def _validate_rooted(self, path: str) -> str:
        relative = path[len(self._workspace_root) + 1 :] if isinstance(path, str) and path.startswith(self._workspace_root + "/") else ""
        if (
            not isinstance(path, str)
            or not path.startswith(self._workspace_root + "/")
            or "\\" in path
            or "\x00" in path
            or posixpath.normpath(path) != path
            or any(part in {"", ".", ".."} for part in relative.split("/"))
        ):
            raise ValueError("远端路径不在固定 workspace_root 内。")
        return path

    def _reject_internal(self, path: str) -> None:
        if path == self._code_dir or path.startswith(self._code_dir + "/"):
            raise ValueError("远端内部 runner 路径不可作为用户工作区操作数。")

    @staticmethod
    def _entry_type(entry: Any) -> str:
        value = getattr(entry, "type", None)
        value = getattr(value, "value", value)
        return value if isinstance(value, str) else ""

    def _children(self, directory: str, timeout_seconds: int) -> list[Any]:
        self._validate_rooted(directory) if directory != self._workspace_root else None
        return list(
            self._sandbox.files.list(
                directory,
                depth=1,
                request_timeout=timeout_seconds,
            )
        )

    def _assert_chain(self, path: str, *, final_may_be_missing: bool, timeout_seconds: int) -> bool:
        self._validate_rooted(path)
        self._reject_internal(path)
        relative = path[len(self._workspace_root) + 1 :]
        current = self._workspace_root
        parts = relative.split("/")
        for index, part in enumerate(parts):
            expected = current + "/" + part
            candidates = [
                item for item in self._children(current, timeout_seconds)
                if getattr(item, "path", None) == expected
                or getattr(item, "name", None) == part
            ]
            if len(candidates) != 1:
                if index == len(parts) - 1 and final_may_be_missing and not candidates:
                    return False
                raise ValueError("远端路径不存在或无法安全验证。")
            entry = candidates[0]
            entry_type = self._entry_type(entry)
            if entry_type in {"symlink", "link"} or getattr(entry, "symlink_target", None):
                raise ValueError("远端路径链包含符号链接。")
            if index < len(parts) - 1 and entry_type not in {"dir", "directory"}:
                raise ValueError("远端路径父项不是普通目录。")
            if index == len(parts) - 1 and entry_type not in {"file", "regular"}:
                raise ValueError("远端目标不是普通文件。")
            current = expected
        return True

    def _assert_workspace(self, timeout_seconds: int) -> None:
        self._assert_directory(self._workspace_root, timeout_seconds)

    def _assert_directory(self, path: str, timeout_seconds: int) -> None:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or "\\" in path
            or "\x00" in path
            or posixpath.normpath(path) != path
            or any(part in {"", ".", ".."} for part in path.split("/")[1:])
        ):
            raise ValueError("远端目录路径不安全。")
        get_info = getattr(self._sandbox.files, "get_info", None)
        if callable(get_info):
            info = get_info(path, request_timeout=timeout_seconds)
            if (
                self._entry_type(info) not in {"dir", "directory"}
                or getattr(info, "symlink_target", None)
            ):
                raise ValueError("workspace_root 不是普通目录。")
            return
        self._children(path, timeout_seconds)

    def _ensure_workspace(self, timeout_seconds: int) -> None:
        self._ensure_directory_chain(self._workspace_root, timeout_seconds)

    def _ensure_directory_chain(self, target: str, timeout_seconds: int) -> None:
        if not target.startswith("/workspace/"):
            raise ValueError("workspace 目录必须位于可信 /workspace 下。")
        make_dir = getattr(self._sandbox.files, "make_dir", None)
        get_info = getattr(self._sandbox.files, "get_info", None)
        exists_method = getattr(self._sandbox.files, "exists", None)
        if not callable(make_dir) or not callable(get_info):
            raise ValueError("远端 Filesystem 不支持安全 workspace 初始化。")
        # Aliyun's default fast-start template does not necessarily contain
        # /workspace.  This is the only root we create without first walking
        # an existing parent chain; immediately verify what the SDK exposes so
        # an existing symlink or other special entry still fails closed.
        make_dir("/workspace", request_timeout=timeout_seconds)
        self._assert_directory("/workspace", timeout_seconds)
        current = "/workspace"
        for part in target[len("/workspace/") :].split("/"):
            current += "/" + part
            if callable(exists_method):
                exists = exists_method(current, request_timeout=timeout_seconds)
            else:
                try:
                    get_info(current, request_timeout=timeout_seconds)
                    exists = True
                except FileNotFoundError:
                    exists = False
            if not exists:
                make_dir(current, request_timeout=timeout_seconds)
            self._assert_directory(current, timeout_seconds)

    @staticmethod
    def _python_probe_source() -> str:
        return (
            "import json,sys;"
            "print(json.dumps({'executable':sys.executable,'version':"
            "[sys.version_info[0],sys.version_info[1],sys.version_info[2]]},"
            "separators=(',',':')))"
        )

    def _probe_python(
        self,
        executable: str,
        timeout_seconds: int,
        *,
        user: str,
    ) -> tuple[str, tuple[int, int, int]]:
        command = (
            shlex.quote(executable)
            + " -I -S -X utf8 -c "
            + shlex.quote(self._python_probe_source())
        )
        raw = self._run_command(
            command,
            cwd="/",
            timeout_seconds=timeout_seconds,
            user=user,
        )
        if raw.exit_code != 0 or raw.stderr != "":
            raise ValueError("可信 Python 身份探测失败。")
        lines = raw.stdout.splitlines()
        if len(lines) != 1 or len(lines[0].encode("utf-8")) > 1024:
            raise ValueError("可信 Python 身份探测输出无效。")
        try:
            value = json.loads(lines[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("可信 Python 身份探测输出无效。") from None
        if not isinstance(value, dict) or set(value) != {"executable", "version"}:
            raise ValueError("可信 Python 身份字段无效。")
        if not isinstance(value["executable"], str):
            raise ValueError("可信 Python executable 字段无效。")
        resolved = self._validate_absolute_root(value["executable"], "Python executable")
        version = value["version"]
        if (
            not isinstance(version, list)
            or len(version) != 3
            or any(type(part) is not int or not 0 <= part <= 999 for part in version)
            or tuple(version) < (3, 11, 0)
        ):
            raise ValueError("可信 Python 版本无效或过低。")
        if (
            resolved == self._workspace_root
            or resolved.startswith(self._workspace_root + "/")
            or (
                self._persistence_root is not None
                and (
                    resolved == self._persistence_root
                    or resolved.startswith(self._persistence_root + "/")
                )
            )
        ):
            raise ValueError("可信 Python 不能位于 active workspace 或 OSS。")
        return resolved, tuple(version)  # type: ignore[return-value]

    def _discover_system_python(
        self,
        timeout_seconds: int,
    ) -> tuple[str, tuple[int, int, int]]:
        # PATH discovery is permitted only in this control-plane bootstrap.
        # All Agent-triggered work uses the verified absolute venv path.
        for candidate in ("python3", "python"):
            try:
                discovered = self._probe_python(
                    candidate,
                    timeout_seconds,
                    user="root",
                )
            except Exception:
                continue
            return discovered
        raise ValueError("Sandbox 中没有可用于建立可信 venv 的 Python。")

    def _isolate_persistence_root(
        self,
        system_python: str,
        timeout_seconds: int,
    ) -> None:
        """Deny direct runtime-user access while retaining root control-plane IO."""

        persistence_root = self._require_persistence_root()
        root_literal = json.dumps(persistence_root, ensure_ascii=True)
        token = token_hex(16)
        read_probe = persistence_root + "/.agnet-study-runtime-read-" + token
        write_probe = persistence_root + "/.agnet-study-runtime-write-" + token
        read_literal = json.dumps(read_probe, ensure_ascii=True)
        write_literal = json.dumps(write_probe, ensure_ascii=True)
        harden_source = (
            "import os,stat\nroot="
            + root_literal
            + "\ns=os.lstat(root)\n"
            "if not stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode): raise RuntimeError()\n"
            "os.chown(root,0,0)\n"
            "os.chmod(root,0o700)\n"
            "s=os.lstat(root)\n"
            "if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_gid!=0 or stat.S_IMODE(s.st_mode)!=0o700: raise RuntimeError()\n"
            "fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
            "try: os.listdir(fd)\n"
            "finally: os.close(fd)\n"
            "print('persistence-root-ok')"
        )
        runtime_source = (
            "import errno,os\nroot="
            + root_literal
            + "\nread_probe="
            + read_literal
            + "\nwrite_probe="
            + write_literal
            + "\ndef denied(call):\n"
            " try: value=call()\n"
            " except OSError as exc:\n"
            "  if exc.errno in (errno.EACCES,errno.EPERM): return\n"
            "  raise\n"
            " if isinstance(value,int): os.close(value)\n"
            " raise RuntimeError()\n"
            "denied(lambda:os.listdir(root))\n"
            "denied(lambda:os.open(read_probe,os.O_RDONLY|os.O_NOFOLLOW))\n"
            "denied(lambda:os.open(write_probe,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600))\n"
            "print('persistence-runtime-denied')"
        )
        cleanup_source = (
            "import os\nmarker='persistence-probe-cleanup'\npaths=("
            + read_literal
            + ","
            + write_literal
            + ")\n"
            "for path in paths:\n"
            " try: os.unlink(path)\n"
            " except FileNotFoundError: pass\n"
            " if os.path.lexists(path): raise RuntimeError()"
        )

        def command(source: str) -> str:
            return (
                shlex.quote(system_python)
                + " -I -S -X utf8 -c "
                + shlex.quote(source)
            )

        try:
            hardened = self._run_command(
                command(harden_source),
                cwd="/",
                timeout_seconds=timeout_seconds,
                user="root",
            )
            if (
                hardened.exit_code != 0
                or hardened.stdout != "persistence-root-ok\n"
                or hardened.stderr != ""
            ):
                raise ValueError
            try:
                denied = self._run_command(
                    command(runtime_source),
                    cwd="/",
                    timeout_seconds=timeout_seconds,
                    user=self._runtime_user,
                )
                if (
                    denied.exit_code != 0
                    or denied.stdout != "persistence-runtime-denied\n"
                    or denied.stderr != ""
                ):
                    raise ValueError
            finally:
                cleaned = self._run_command(
                    command(cleanup_source),
                    cwd="/",
                    timeout_seconds=timeout_seconds,
                    user="root",
                )
                if (
                    cleaned.exit_code != 0
                    or cleaned.stdout != ""
                    or cleaned.stderr != ""
                ):
                    raise ValueError
            # The SDK Files API is the host control-plane checkpoint path.  It
            # must remain usable as root after the POSIX mode is tightened.
            self._assert_persistence_directory(persistence_root, timeout_seconds)
            self._persistence_children(persistence_root, timeout_seconds)
        except Exception:
            raise ValueError("persistence 访问隔离失败。") from None

    def _freeze_runtime(self, system_python: str, timeout_seconds: int) -> None:
        root_literal = json.dumps(self._runtime_venv_dir, ensure_ascii=True)
        source = (
            "import os,stat;root="
            + root_literal
            + ";"
            "items=[];"
            "[(items.append(os.path.join(d,n))) for d,ds,fs in os.walk(root,followlinks=False) for n in ds+fs];"
            "items.sort(key=lambda p:p.count('/'),reverse=True);"
            "[(os.chown(p,0,0,follow_symlinks=False),"
            "None if stat.S_ISLNK(os.lstat(p).st_mode) else os.chmod(p,os.lstat(p).st_mode&~0o022)) for p in items];"
            "os.chown(root,0,0);os.chmod(root,os.lstat(root).st_mode&~0o022)"
        )
        result = self._run_command(
            shlex.quote(system_python)
            + " -I -S -X utf8 -c "
            + shlex.quote(source),
            cwd="/",
            timeout_seconds=timeout_seconds,
            user="root",
        )
        if result.exit_code != 0 or result.stdout != "" or result.stderr != "":
            raise ValueError("可信 runtime 权限固化失败。")

    def _inspect_runtime(
        self,
        system_python: str,
        timeout_seconds: int,
    ) -> dict[str, str]:
        root_literal = json.dumps(self._runtime_venv_dir, ensure_ascii=True)
        python_literal = json.dumps(self._runtime_python, ensure_ascii=True)
        source = (
            "import hashlib,json,os,stat\nroot="
            + root_literal
            + "\np="
            + python_literal
            + "\nrows=[]\n"
            "rs=os.lstat(root)\n"
            "if not stat.S_ISDIR(rs.st_mode) or rs.st_uid!=0 or (rs.st_mode&0o022): raise RuntimeError()\n"
            "rp=os.path.realpath(p)\n"
            "ps=os.stat(rp)\n"
            "if not stat.S_ISREG(ps.st_mode) or ps.st_uid!=0 or (ps.st_mode&0o022): raise RuntimeError()\n"
            "rows.append(['@resolved_python',ps.st_dev,ps.st_ino,ps.st_mode,ps.st_size,ps.st_mtime_ns,ps.st_uid,ps.st_gid])\n"
            "paths=[root]\n"
            "for d,ds,fs in os.walk(root,followlinks=False): paths.extend(os.path.join(d,n) for n in sorted(ds+fs))\n"
            "def check(q):\n"
            " s=os.lstat(q)\n"
            " if s.st_uid!=0: raise RuntimeError()\n"
            " if stat.S_ISLNK(s.st_mode):\n"
            "  target=os.path.realpath(q)\n"
            "  try: inside=os.path.commonpath((root,target))==root\n"
            "  except ValueError: inside=False\n"
            "  if not inside and target!=rp: raise RuntimeError()\n"
            "  ts=os.stat(target)\n"
            "  if (not stat.S_ISREG(ts.st_mode) and not stat.S_ISDIR(ts.st_mode)) or ts.st_uid!=0 or (ts.st_mode&0o022): raise RuntimeError()\n"
            "  if not inside and not stat.S_ISREG(ts.st_mode): raise RuntimeError()\n"
            " elif (not stat.S_ISREG(s.st_mode) and not stat.S_ISDIR(s.st_mode)) or (s.st_mode&0o022): raise RuntimeError()\n"
            " rows.append([q,s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_uid,s.st_gid])\n"
            "for q in sorted(set(paths)): check(q)\n"
            "h=hashlib.sha256()\n"
            "with open(rp,'rb') as f:\n"
            " for b in iter(lambda:f.read(1048576),b''): h.update(b)\n"
            "print(json.dumps({'resolved_python_path':rp,'executable_sha256':h.hexdigest(),'metadata_digest':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()},separators=(',',':')))"
        )
        result = self._run_command(
            shlex.quote(system_python)
            + " -I -S -X utf8 -c "
            + shlex.quote(source),
            cwd="/",
            timeout_seconds=timeout_seconds,
            user="root",
        )
        if result.exit_code != 0 or result.stderr != "":
            raise ValueError("可信 runtime 文件身份校验失败。")
        lines = result.stdout.splitlines()
        if len(lines) != 1 or len(lines[0].encode("utf-8")) > 2048:
            raise ValueError("可信 runtime 文件身份输出无效。")
        try:
            value = json.loads(lines[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("可信 runtime 文件身份输出无效。") from None
        if not isinstance(value, dict) or set(value) != {
            "resolved_python_path",
            "executable_sha256",
            "metadata_digest",
        }:
            raise ValueError("可信 runtime 文件身份字段无效。")
        if not isinstance(value["resolved_python_path"], str):
            raise ValueError("可信 runtime realpath 字段无效。")
        resolved = self._validate_absolute_root(
            value["resolved_python_path"],
            "resolved_python_path",
        )
        if (
            resolved == self._workspace_root
            or resolved.startswith(self._workspace_root + "/")
            or resolved == "/mnt"
            or resolved.startswith("/mnt/")
        ):
            raise ValueError("可信 runtime 解释器 realpath 越出控制面。")
        for field_name in ("executable_sha256", "metadata_digest"):
            digest = value[field_name]
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("可信 runtime 摘要无效。")
        return {
            "resolved_python_path": resolved,
            "executable_sha256": value["executable_sha256"],
            "metadata_digest": value["metadata_digest"],
        }

    def _initialize_trusted_runtime(
        self,
        timeout_seconds: int,
        *,
        discovered: tuple[str, tuple[int, int, int]],
    ) -> SandboxRuntimeIdentity:
        system_python, _system_version = discovered
        if system_python == self._runtime_python or system_python.startswith(
            self._runtime_venv_dir + "/"
        ):
            raise ValueError("可信 venv 不能从自身引导。")
        files = self._sandbox.files
        exists_method = getattr(files, "exists", None)
        get_info = getattr(files, "get_info", None)
        if not callable(exists_method) or not callable(get_info):
            raise ValueError("远端 Filesystem 不支持可信 runtime 路径检查。")
        try:
            runtime_exists = exists_method(
                self._runtime_venv_dir,
                request_timeout=timeout_seconds,
            )
        except Exception:
            raise ValueError("可信 runtime 路径状态无法确认。") from None
        if type(runtime_exists) is not bool:
            raise ValueError("可信 runtime 路径状态无法确认。")
        if runtime_exists:
            try:
                runtime_entry = get_info(
                    self._runtime_venv_dir,
                    request_timeout=timeout_seconds,
                )
            except Exception:
                # exists=True followed by a missing/error result is a TOCTOU
                # or provider inconsistency.  Never reinterpret it as a safe
                # create case and never match provider exception class names.
                raise ValueError("可信 runtime 路径状态无法确认。") from None
        else:
            runtime_entry = None
        if runtime_entry is None:
            create_command = (
                shlex.quote(system_python)
                + " -I -m venv "
                + shlex.quote(self._runtime_venv_dir)
            )
            created = self._run_command(
                create_command,
                cwd="/",
                timeout_seconds=timeout_seconds,
                user="root",
            )
            if created.exit_code != 0 or created.stderr != "":
                raise ValueError("可信 Python venv 初始化失败。")
            self._freeze_runtime(system_python, timeout_seconds)
        elif (
            self._entry_type(runtime_entry) not in {"dir", "directory"}
            or getattr(runtime_entry, "symlink_target", None)
        ):
            raise ValueError("预置可信 runtime 不是普通目录。")
        python_path, version = self._probe_python(
            self._runtime_python,
            timeout_seconds,
            user=self._runtime_user,
        )
        if python_path != self._runtime_python:
            raise ValueError("可信 Python venv 返回了不同的解释器路径。")
        inspected = self._inspect_runtime(system_python, timeout_seconds)
        identity_payload = json.dumps(
            {
                "python_path": python_path,
                "system_python_path": system_python,
                "version": list(version),
                **inspected,
                "runtime_user": self._runtime_user,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return SandboxRuntimeIdentity(
            python_path=python_path,
            system_python_path=system_python,
            version=version,
            resolved_python_path=inspected["resolved_python_path"],
            executable_sha256=inspected["executable_sha256"],
            metadata_digest=inspected["metadata_digest"],
            runtime_user=self._runtime_user,
            digest=hashlib.sha256(identity_payload).hexdigest(),
        )

    def _verify_runtime_identity(self, timeout_seconds: int) -> None:
        python_path, version = self._probe_python(
            self._runtime_python,
            timeout_seconds,
            user=self._runtime_user,
        )
        inspected = self._inspect_runtime(
            self._runtime_identity.system_python_path,
            timeout_seconds,
        )
        if (
            python_path != self._runtime_identity.python_path
            or version != self._runtime_identity.version
            or inspected["resolved_python_path"]
            != self._runtime_identity.resolved_python_path
            or inspected["executable_sha256"]
            != self._runtime_identity.executable_sha256
            or inspected["metadata_digest"]
            != self._runtime_identity.metadata_digest
        ):
            raise ValueError("可信 Python runtime 身份发生变化。")

    def _file_size(self, path: str, timeout_seconds: int) -> int:
        get_info = getattr(self._sandbox.files, "get_info", None)
        if callable(get_info):
            info = get_info(path, request_timeout=timeout_seconds)
            if (
                self._entry_type(info) not in {"file", "regular"}
                or getattr(info, "symlink_target", None)
                or not isinstance(getattr(info, "size", None), int)
                or isinstance(getattr(info, "size", None), bool)
            ):
                raise ValueError("远端目标不是普通文件。")
            size = info.size
        else:
            parent = posixpath.dirname(path)
            name = posixpath.basename(path)
            entries = [item for item in self._children(parent, timeout_seconds) if getattr(item, "name", None) == name]
            if len(entries) != 1 or self._entry_type(entries[0]) not in {"file", "regular"}:
                raise ValueError("远端目标不是普通文件。")
            size = getattr(entries[0], "size", -1)
        if size < 0 or size > 8 * 1024 * 1024:
            raise ValueError("远端文件超过 8 MiB 安全上限。")
        return size

    def _ensure_parent(self, path: str, timeout_seconds: int) -> None:
        parent = posixpath.dirname(path)
        self._validate_rooted(parent + "/placeholder") if parent != self._workspace_root else None
        if parent != self._workspace_root:
            self._ensure_directory_chain(parent, timeout_seconds)
        else:
            self._assert_directory(parent, timeout_seconds)

    def _require_persistence_root(self) -> str:
        if self._persistence_root is None:
            raise ValueError("transport 未绑定 persistence_root。")
        return self._persistence_root

    def _assert_persistence_directory(self, path: str, timeout_seconds: int) -> None:
        self._validate_absolute_root(path, "persistence 目录")
        get_info = getattr(self._sandbox.files, "get_info", None)
        if not callable(get_info):
            raise ValueError("远端 Filesystem 不支持 persistence 目录身份校验。")
        try:
            info = get_info(
                path,
                user="root",
                request_timeout=timeout_seconds,
            )
        except Exception:
            raise ValueError("persistence 目录不存在或无法安全验证。") from None
        if (
            self._entry_type(info) not in {"dir", "directory"}
            or getattr(info, "symlink_target", None)
        ):
            raise ValueError("persistence 目录不是普通目录。")

    def _persistence_coordinates(self, checkpoint_root: str) -> tuple[str, str]:
        persistence_root = self._require_persistence_root()
        prefix = persistence_root + "/" + self._PERSISTENCE_NAMESPACE + "/"
        if (
            not isinstance(checkpoint_root, str)
            or not checkpoint_root.startswith(prefix)
            or "\\" in checkpoint_root
            or "\x00" in checkpoint_root
            or any(unicodedata.category(character) == "Cc" for character in checkpoint_root)
            or posixpath.normpath(checkpoint_root) != checkpoint_root
        ):
            raise ValueError("checkpoint_root 不在固定 persistence namespace 内。")
        suffix = checkpoint_root[len(prefix) :]
        parts = suffix.split("/")
        if (
            len(parts) != 2
            or len(parts[0]) != 32
            or len(parts[1]) != 64
            or any(character not in "0123456789abcdef" for character in parts[0])
            or any(character not in "0123456789abcdef" for character in parts[1])
        ):
            raise ValueError("checkpoint_root 坐标无效。")
        return parts[0], parts[1]

    def _validate_persistence_target(self, path: str) -> tuple[str, str]:
        persistence_root = self._require_persistence_root()
        prefix = persistence_root + "/" + self._PERSISTENCE_NAMESPACE + "/"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ValueError("persistence 文件不在固定 checkpoint namespace 内。")
        if len(path.encode("utf-8")) > 8192:
            raise ValueError("persistence 文件路径超过安全上限。")
        remainder = path[len(prefix) :]
        components = remainder.split("/")
        if len(components) < 3:
            raise ValueError("persistence 文件必须位于 checkpoint_root 后代。")
        checkpoint_root = prefix + components[0] + "/" + components[1]
        self._persistence_coordinates(checkpoint_root)
        relative = "/".join(components[2:])
        try:
            validate_sandbox_relative_path(relative)
        except (TypeError, ValueError):
            raise ValueError("persistence 文件路径无效。") from None
        return checkpoint_root, relative

    def _persistence_children(self, directory: str, timeout_seconds: int) -> list[Any]:
        try:
            return list(
                self._sandbox.files.list(
                    directory,
                    depth=1,
                    user="root",
                    request_timeout=timeout_seconds,
                )
            )
        except Exception:
            raise ValueError("persistence 目录无法安全列举。") from None

    def _persistence_child(
        self,
        parent: str,
        expected: str,
        timeout_seconds: int,
    ) -> Any | None:
        name = posixpath.basename(expected)
        matches = [
            entry
            for entry in self._persistence_children(parent, timeout_seconds)
            if getattr(entry, "path", None) == expected
            or getattr(entry, "name", None) == name
        ]
        if len(matches) > 1:
            raise ValueError("persistence 路径身份不唯一。")
        return matches[0] if matches else None

    def _ensure_or_probe_persistence_directory(
        self,
        path: str,
        *,
        create: bool,
        timeout_seconds: int,
    ) -> bool:
        persistence_root = self._require_persistence_root()
        self._assert_persistence_directory(persistence_root, timeout_seconds)
        if path == persistence_root:
            return True
        if not path.startswith(persistence_root + "/"):
            raise ValueError("persistence 目录越界。")
        relative = path[len(persistence_root) + 1 :]
        current = persistence_root
        make_dir = getattr(self._sandbox.files, "make_dir", None)
        if create and not callable(make_dir):
            raise ValueError("远端 Filesystem 不支持安全 checkpoint 目录初始化。")
        for part in relative.split("/"):
            if part in {"", ".", ".."}:
                raise ValueError("persistence 目录路径无效。")
            expected = current + "/" + part
            entry = self._persistence_child(current, expected, timeout_seconds)
            if entry is None:
                if not create:
                    return False
                try:
                    make_dir(
                        expected,
                        user="root",
                        request_timeout=timeout_seconds,
                    )
                except Exception:
                    raise ValueError("checkpoint 目录创建失败。") from None
                self._assert_persistence_directory(expected, timeout_seconds)
            else:
                if (
                    self._entry_type(entry) not in {"dir", "directory"}
                    or getattr(entry, "symlink_target", None)
                ):
                    raise ValueError("checkpoint 路径链包含符号链接或特殊文件。")
                self._assert_persistence_directory(expected, timeout_seconds)
            current = expected
        return True

    def _assert_persistence_file(
        self,
        path: str,
        *,
        final_may_be_missing: bool,
        create_parents: bool,
        timeout_seconds: int,
    ) -> bool:
        checkpoint_root, _relative = self._validate_persistence_target(path)
        parent = posixpath.dirname(path)
        if not self._ensure_or_probe_persistence_directory(
            parent,
            create=create_parents,
            timeout_seconds=timeout_seconds,
        ):
            if final_may_be_missing:
                return False
            raise ValueError("persistence 文件不存在或无法安全验证。")
        if parent != checkpoint_root and not parent.startswith(checkpoint_root + "/"):
            raise ValueError("persistence 文件父目录越界。")
        entry = self._persistence_child(parent, path, timeout_seconds)
        if entry is None:
            if final_may_be_missing:
                return False
            raise ValueError("persistence 文件不存在或无法安全验证。")
        if (
            self._entry_type(entry) not in {"file", "regular"}
            or getattr(entry, "symlink_target", None)
        ):
            raise ValueError("persistence 目标不是普通文件。")
        return True

    def _persistence_file_size(self, path: str, timeout_seconds: int) -> int:
        get_info = getattr(self._sandbox.files, "get_info", None)
        if not callable(get_info):
            raise ValueError("远端 Filesystem 不支持 persistence 文件身份校验。")
        try:
            info = get_info(
                path,
                user="root",
                request_timeout=timeout_seconds,
            )
        except Exception:
            raise ValueError("persistence 文件无法安全验证。") from None
        size = getattr(info, "size", None)
        if (
            self._entry_type(info) not in {"file", "regular"}
            or getattr(info, "symlink_target", None)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > self._MAX_PERSISTENCE_FILE_BYTES
        ):
            raise ValueError("persistence 目标不是有界普通文件。")
        return size

    def persistence_read_file(
        self,
        path: str,
        *,
        offset: int,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult:
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 0 <= max_bytes <= self._MAX_PERSISTENCE_FILE_BYTES
            or not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("persistence 读取边界无效。")
        self._assert_persistence_file(
            path,
            final_may_be_missing=False,
            create_parents=False,
            timeout_seconds=timeout_seconds,
        )
        size = self._persistence_file_size(path, timeout_seconds)
        try:
            raw = bytes(
                self._sandbox.files.read(
                    path,
                    format="bytes",
                    user="root",
                    request_timeout=timeout_seconds,
                )
            )
        except Exception:
            raise ValueError("persistence 文件读取失败。") from None
        if len(raw) != size:
            raise ValueError("persistence 文件在读取期间发生变化。")
        return SandboxReadResult(raw[offset : offset + max_bytes], len(raw))

    def persistence_write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ) -> SandboxWriteResult:
        if (
            not isinstance(data, bytes)
            or len(data) > self._MAX_PERSISTENCE_FILE_BYTES
            or overwrite is not False
            or not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("persistence 只允许有界 create-only 写入。")
        exists = self._assert_persistence_file(
            path,
            final_may_be_missing=True,
            create_parents=True,
            timeout_seconds=timeout_seconds,
        )
        if exists:
            raise FileExistsError("persistence checkpoint 文件已存在。")
        try:
            self._sandbox.files.write(
                path,
                data,
                user="root",
                request_timeout=timeout_seconds,
            )
        except FileExistsError:
            raise
        except Exception:
            raise ValueError("persistence checkpoint 文件写入失败。") from None
        return SandboxWriteResult(len(data))

    def persistence_list_files(
        self,
        checkpoint_root: str,
        *,
        max_results: int,
        max_depth: int,
        timeout_seconds: int,
    ) -> SandboxPersistenceListResult:
        self._persistence_coordinates(checkpoint_root)
        if (
            not isinstance(max_results, int)
            or isinstance(max_results, bool)
            or not 1 <= max_results <= self._MAX_PERSISTENCE_RESULTS
            or not isinstance(max_depth, int)
            or isinstance(max_depth, bool)
            or not 1 <= max_depth <= self._MAX_PERSISTENCE_DEPTH
            or not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("persistence 列举边界无效。")
        if not self._ensure_or_probe_persistence_directory(
            checkpoint_root,
            create=False,
            timeout_seconds=timeout_seconds,
        ):
            return SandboxPersistenceListResult((), False)

        pending: list[tuple[str, int]] = [(checkpoint_root, 0)]
        output: list[SandboxPersistenceEntry] = []
        truncated = False
        while pending:
            directory, depth = pending.pop(0)
            children = self._persistence_children(directory, timeout_seconds)
            for entry in children:
                path = getattr(entry, "path", None)
                if not isinstance(path, str):
                    raise ValueError("persistence 列举返回了无效路径。")
                expected_prefix = directory + "/"
                if (
                    not path.startswith(expected_prefix)
                    or "/" in path[len(expected_prefix) :]
                ):
                    raise ValueError("persistence 列举返回了越界路径。")
                self._validate_persistence_target(path)
                kind = self._entry_type(entry)
                if kind in {"symlink", "link"} or getattr(entry, "symlink_target", None):
                    raise ValueError("persistence 列举发现符号链接。")
                if kind in {"dir", "directory"}:
                    item = SandboxPersistenceEntry(path, True, 0)
                    if depth + 1 >= max_depth:
                        if self._persistence_children(path, timeout_seconds):
                            truncated = True
                    else:
                        pending.append((path, depth + 1))
                elif kind in {"file", "regular"}:
                    size = getattr(entry, "size", None)
                    if (
                        not isinstance(size, int)
                        or isinstance(size, bool)
                        or size < 0
                        or size > self._MAX_PERSISTENCE_FILE_BYTES
                    ):
                        raise ValueError("persistence 列举发现无界文件。")
                    item = SandboxPersistenceEntry(path, False, size)
                else:
                    raise ValueError("persistence 列举发现特殊文件。")
                output.append(item)
                if len(output) > max_results:
                    truncated = True
                    pending.clear()
                    break
            if truncated and len(output) > max_results:
                break
        output.sort(key=lambda item: item.path)
        return SandboxPersistenceListResult(tuple(output[:max_results]), truncated)

    def _version_coordinates(self, session_root: str) -> str:
        persistence_root = self._require_persistence_root()
        prefix = persistence_root + "/" + self._VERSION_NAMESPACE + "/"
        if (
            not isinstance(session_root, str)
            or not session_root.startswith(prefix)
            or posixpath.normpath(session_root) != session_root
            or "\\" in session_root
            or "\x00" in session_root
        ):
            raise ValueError("version session_root 不在固定 v2 namespace 内。")
        session_id = session_root[len(prefix):]
        if (
            len(session_id) != 32
            or "/" in session_id
            or any(character not in "0123456789abcdef" for character in session_id)
        ):
            raise ValueError("version session_root 坐标无效。")
        return session_id

    def _validate_version_target(self, path: str) -> tuple[str, str]:
        persistence_root = self._require_persistence_root()
        prefix = persistence_root + "/" + self._VERSION_NAMESPACE + "/"
        if (
            not isinstance(path, str)
            or not path.startswith(prefix)
            or len(path.encode("utf-8")) > 8192
            or posixpath.normpath(path) != path
            or "\\" in path
            or "\x00" in path
        ):
            raise ValueError("version 文件不在固定 v2 namespace 内。")
        remainder = path[len(prefix):]
        parts = remainder.split("/")
        if len(parts) != 3:
            raise ValueError("version 文件层级无效。")
        session_root = prefix + parts[0]
        self._version_coordinates(session_root)
        category, name = parts[1], parts[2]
        hexadecimal = "0123456789abcdef"
        if category == "objects":
            valid = len(name) == 64 and all(character in hexadecimal for character in name)
        elif category == "generations":
            valid = (
                len(name) == 86
                and name[16] == "-"
                and name.endswith(".json")
                and name[:16].isdigit()
                and name[:16].isascii()
                and all(character in hexadecimal for character in name[17:81])
            )
        else:
            valid = False
        if not valid:
            raise ValueError("version 文件坐标无效。")
        return session_root, category

    def _assert_version_file(
        self,
        path: str,
        *,
        final_may_be_missing: bool,
        create_parents: bool,
        timeout_seconds: int,
    ) -> bool:
        session_root, _category = self._validate_version_target(path)
        parent = posixpath.dirname(path)
        if not self._ensure_or_probe_persistence_directory(
            parent,
            create=create_parents,
            timeout_seconds=timeout_seconds,
        ):
            if final_may_be_missing:
                return False
            raise ValueError("version 文件不存在或无法安全验证。")
        if not parent.startswith(session_root + "/"):
            raise ValueError("version 文件父目录越界。")
        entry = self._persistence_child(parent, path, timeout_seconds)
        if entry is None:
            if final_may_be_missing:
                return False
            raise ValueError("version 文件不存在或无法安全验证。")
        if (
            self._entry_type(entry) not in {"file", "regular"}
            or getattr(entry, "symlink_target", None)
        ):
            raise ValueError("version 目标不是普通文件。")
        return True

    def workspace_version_probe_file(
        self,
        path: str,
        *,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult | None:
        if (
            type(max_bytes) is not int
            or not 0 <= max_bytes <= self._MAX_PERSISTENCE_FILE_BYTES
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
        ):
            raise ValueError("version 探测边界无效。")
        exists = self._assert_version_file(
            path,
            final_may_be_missing=True,
            create_parents=False,
            timeout_seconds=timeout_seconds,
        )
        if not exists:
            return None
        size = self._persistence_file_size(path, timeout_seconds)
        if size > max_bytes:
            raise ValueError("version 文件超过读取上限。")
        try:
            raw = bytes(
                self._sandbox.files.read(
                    path,
                    format="bytes",
                    user="root",
                    request_timeout=timeout_seconds,
                )
            )
        except Exception:
            raise ValueError("version 文件读取失败。") from None
        if len(raw) != size:
            raise ValueError("version 文件在读取期间发生变化。")
        return SandboxReadResult(raw, size)

    def workspace_version_read_file(
        self,
        path: str,
        *,
        offset: int,
        max_bytes: int,
        timeout_seconds: int,
    ) -> SandboxReadResult:
        if type(offset) is not int or offset < 0:
            raise ValueError("version 读取 offset 无效。")
        observed = self.workspace_version_probe_file(
            path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
        )
        if observed is None:
            raise FileNotFoundError("version 文件不存在。")
        return SandboxReadResult(
            observed.data[offset:offset + max_bytes], observed.total_bytes
        )

    def workspace_version_write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ) -> SandboxWriteResult:
        if (
            not isinstance(data, bytes)
            or len(data) > self._MAX_PERSISTENCE_FILE_BYTES
            or overwrite is not False
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
        ):
            raise ValueError("version 只允许有界 create-only 写入。")
        exists = self._assert_version_file(
            path,
            final_may_be_missing=True,
            create_parents=True,
            timeout_seconds=timeout_seconds,
        )
        if exists:
            raise FileExistsError("version 文件已经存在。")
        try:
            self._sandbox.files.write(
                path,
                data,
                user="root",
                request_timeout=timeout_seconds,
            )
        except FileExistsError:
            raise
        except Exception:
            raise ValueError("version 文件写入失败。") from None
        return SandboxWriteResult(len(data))

    def workspace_version_list_files(
        self,
        session_root: str,
        *,
        max_results: int,
        max_depth: int,
        timeout_seconds: int,
    ) -> SandboxPersistenceListResult:
        self._version_coordinates(session_root)
        if (
            type(max_results) is not int
            or not 1 <= max_results <= self._MAX_PERSISTENCE_RESULTS
            or type(max_depth) is not int
            or not 1 <= max_depth <= self._MAX_PERSISTENCE_DEPTH
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
        ):
            raise ValueError("version 列举边界无效。")
        if not self._ensure_or_probe_persistence_directory(
            session_root, create=False, timeout_seconds=timeout_seconds
        ):
            return SandboxPersistenceListResult((), False)
        pending: list[tuple[str, int]] = [(session_root, 0)]
        output: list[SandboxPersistenceEntry] = []
        truncated = False
        while pending:
            directory, depth = pending.pop(0)
            for entry in self._persistence_children(directory, timeout_seconds):
                path = getattr(entry, "path", None)
                if (
                    not isinstance(path, str)
                    or not path.startswith(directory + "/")
                    or "/" in path[len(directory) + 1:]
                ):
                    raise ValueError("version 列举返回了越界路径。")
                kind = self._entry_type(entry)
                if kind in {"symlink", "link"} or getattr(entry, "symlink_target", None):
                    raise ValueError("version 列举发现符号链接。")
                if kind in {"dir", "directory"}:
                    if path not in {session_root + "/objects", session_root + "/generations"}:
                        raise ValueError("version 列举发现未知目录。")
                    item = SandboxPersistenceEntry(path, True, 0)
                    if depth + 1 >= max_depth:
                        if self._persistence_children(path, timeout_seconds):
                            truncated = True
                    else:
                        pending.append((path, depth + 1))
                elif kind in {"file", "regular"}:
                    self._validate_version_target(path)
                    size = getattr(entry, "size", None)
                    if type(size) is not int or not 0 <= size <= self._MAX_PERSISTENCE_FILE_BYTES:
                        raise ValueError("version 列举发现无界文件。")
                    item = SandboxPersistenceEntry(path, False, size)
                else:
                    raise ValueError("version 列举发现特殊文件。")
                output.append(item)
                if len(output) > max_results:
                    truncated = True
                    pending.clear()
                    break
        output.sort(key=lambda item: item.path)
        return SandboxPersistenceListResult(tuple(output[:max_results]), truncated)

    def _delete_exact_workspace_version_object(
        self,
        path: str,
        *,
        expected_size: int,
        expected_sha256: str,
        timeout_seconds: int,
        failure_message: str,
    ) -> None:
        try:
            _session_root, category = self._validate_version_target(path)
            digest = posixpath.basename(path)
            if (
                category != "objects"
                or type(expected_size) is not int
                or not 0 <= expected_size <= self._MAX_VERSION_OBJECT_BYTES
                or not isinstance(expected_sha256, str)
                or len(expected_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_sha256
                )
                or digest != expected_sha256
                or type(timeout_seconds) is not int
                or timeout_seconds <= 0
            ):
                raise ValueError
            if not self._assert_version_file(
                path,
                final_may_be_missing=False,
                create_parents=False,
                timeout_seconds=timeout_seconds,
            ):
                raise ValueError
            size = self._persistence_file_size(path, timeout_seconds)
            if size != expected_size:
                raise ValueError
            raw = bytes(
                self._sandbox.files.read(
                    path,
                    format="bytes",
                    user="root",
                    request_timeout=timeout_seconds,
                )
            )
            if (
                len(raw) != expected_size
                or hashlib.sha256(raw).hexdigest() != expected_sha256
            ):
                raise ValueError
            removed = self._sandbox.files.remove(
                path,
                user="root",
                request_timeout=timeout_seconds,
            )
            if removed is False:
                raise ValueError
            if self._assert_version_file(
                path,
                final_may_be_missing=True,
                create_parents=False,
                timeout_seconds=timeout_seconds,
            ):
                raise ValueError
        except Exception:
            raise ValueError(failure_message) from None

    def rollback_delete_workspace_version_object(
        self,
        path: str,
        *,
        expected_size: int,
        expected_sha256: str,
        timeout_seconds: int,
    ) -> None:
        """Delete one exact object created by a failed transaction attempt."""

        self._delete_exact_workspace_version_object(
            path,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            timeout_seconds=timeout_seconds,
            failure_message="version object rollback 清理失败。",
        )

    def gc_delete_workspace_version_object(
        self,
        path: str,
        *,
        expected_size: int,
        expected_sha256: str,
        timeout_seconds: int,
    ) -> None:
        """Delete one twice-scanned orphan for host maintenance only."""

        self._delete_exact_workspace_version_object(
            path,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            timeout_seconds=timeout_seconds,
            failure_message="version object GC 清理失败。",
        )

    def rollback_delete_file(
        self,
        path: str,
        *,
        expected_sha256: str,
        timeout_seconds: int,
    ) -> None:
        """Delete only an exact just-created active file during rollback."""
        self._validate_rooted(path)
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
            or type(timeout_seconds) is not int
            or timeout_seconds <= 0
        ):
            raise ValueError("rollback 删除绑定无效。")
        if not self._assert_chain(
            path,
            final_may_be_missing=False,
            timeout_seconds=timeout_seconds,
        ):
            raise ValueError("rollback 删除目标不存在。")
        size = self._file_size(path, timeout_seconds)
        try:
            raw = bytes(
                self._sandbox.files.read(
                    path, format="bytes", request_timeout=timeout_seconds
                )
            )
        except Exception:
            raise ValueError("rollback 删除前读取失败。") from None
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("rollback 删除前摘要不匹配。")
        try:
            result = self._sandbox.files.remove(path, request_timeout=timeout_seconds)
        except Exception:
            raise ValueError("rollback 删除失败。") from None
        if result is False:
            raise ValueError("rollback 删除返回失败状态。")
        if self._assert_chain(
            path,
            final_may_be_missing=True,
            timeout_seconds=timeout_seconds,
        ):
            raise ValueError("rollback 删除后目标仍存在。")

    def read_file(
        self, path: str, *, offset: int, max_bytes: int, timeout_seconds: int
    ) -> SandboxReadResult:
        self._assert_workspace(timeout_seconds)
        self._assert_chain(path, final_may_be_missing=False, timeout_seconds=timeout_seconds)
        self._file_size(path, timeout_seconds)
        raw = bytes(
            self._sandbox.files.read(
                path,
                format="bytes",
                request_timeout=timeout_seconds,
            )
        )
        return SandboxReadResult(raw[offset : offset + max_bytes], len(raw))

    def write_file(
        self,
        path: str,
        data: bytes,
        *,
        overwrite: bool,
        timeout_seconds: int,
    ) -> SandboxWriteResult:
        self._assert_workspace(timeout_seconds)
        self._ensure_parent(path, timeout_seconds)
        exists = self._assert_chain(
            path,
            final_may_be_missing=True,
            timeout_seconds=timeout_seconds,
        )
        if exists is not overwrite:
            raise FileExistsError("远端文件 create/overwrite 状态不匹配。")
        self._sandbox.files.write(path, data, request_timeout=timeout_seconds)
        return SandboxWriteResult(len(data))

    def edit_file(
        self,
        path: str,
        *,
        operation: str,
        old_text: str,
        new_text: str | None,
        expected_replacements: int,
        position: str | None,
        timeout_seconds: int,
    ) -> SandboxEditResult:
        self._assert_workspace(timeout_seconds)
        self._assert_chain(path, final_may_be_missing=False, timeout_seconds=timeout_seconds)
        self._file_size(path, timeout_seconds)
        original = bytes(
            self._sandbox.files.read(path, format="bytes", request_timeout=timeout_seconds)
        )
        bom = original.startswith(b"\xef\xbb\xbf")
        try:
            text = original[3:].decode("utf-8") if bom else original.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("只能编辑 UTF-8 文本文件。") from None
        from app.schemas import EditorFileArguments

        arguments = EditorFileArguments(
            path="placeholder.txt",
            operation=operation,
            old_text=old_text,
            new_text=new_text,
            expected_replacements=expected_replacements,
            position=position,
        )
        replacement, replacements = _apply_edit(arguments, text)
        updated = (b"\xef\xbb\xbf" if bom else b"") + replacement.encode("utf-8")
        if len(updated) > 8 * 1024 * 1024:
            raise ValueError("编辑后的远端文件超过 8 MiB 安全上限。")
        self._sandbox.files.write(path, updated, request_timeout=timeout_seconds)
        return SandboxEditResult(
            encoding="utf-8-sig" if bom else "utf-8",
            replacements=replacements,
            bytes_before=len(original),
            bytes_after=len(updated),
            sha256_before=hashlib.sha256(original).hexdigest(),
            sha256_after=hashlib.sha256(updated).hexdigest(),
        )

    def list_files(
        self,
        pattern: str,
        *,
        workspace_root: str,
        max_results: int,
        timeout_seconds: int,
        path_predicate: SandboxPathPredicate,
    ) -> SandboxListResult:
        if not callable(path_predicate):
            raise ValueError("远端扫描缺少路径策略。")
        if workspace_root != self._workspace_root:
            raise ValueError("transport workspace_root 不匹配。")
        self._assert_workspace(timeout_seconds)
        pending: list[tuple[str, int]] = [(self._workspace_root, 0)]
        entries: list[Any] = []
        scanned = 0
        truncated = False
        while pending:
            directory, depth = pending.pop(0)
            children = self._sandbox.files.list(
                directory,
                depth=1,
                request_timeout=timeout_seconds,
            )
            for entry in children:
                scanned += 1
                if scanned > 20_000:
                    truncated = True
                    pending.clear()
                    break
                path = getattr(entry, "path", None)
                if not isinstance(path, str):
                    raise ValueError("远端列举返回了无效路径。")
                self._validate_rooted(path)
                if self._entry_type(entry) in {"symlink", "link"} or getattr(entry, "symlink_target", None):
                    raise ValueError("远端列举发现符号链接。")
                relative = path[len(self._workspace_root) + 1 :]
                if relative == ".agent-internal" or relative.startswith(".agent-internal/"):
                    continue
                kind = self._entry_type(entry)
                if kind in {"dir", "directory"}:
                    if path_predicate(relative, True) is not True:
                        continue
                    if depth + 1 >= 32:
                        truncated = True
                    else:
                        pending.append((path, depth + 1))
                    continue
                if kind not in {"file", "regular"}:
                    raise ValueError("远端列举发现特殊文件。")
                if path_predicate(relative, False) is not True:
                    continue
                entries.append(entry)
        output: list[SandboxFileEntry] = []
        for entry in entries:
            path = getattr(entry, "path", None)
            if not isinstance(path, str) or not path.startswith(self._workspace_root + "/"):
                raise ValueError("远端列举返回 workspace 外路径。")
            size = getattr(entry, "size", None)
            if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > 8 * 1024 * 1024:
                raise ValueError("远端列举文件大小无效或超过上限。")
            relative = path[len(self._workspace_root) + 1 :]
            if relative == self._code_dir[len(self._workspace_root) + 1 :] or relative.startswith(
                self._code_dir[len(self._workspace_root) + 1 :] + "/"
            ):
                continue
            if path_predicate(relative, False) is not True:
                raise ValueError("远端列举返回了策略禁止的路径。")
            if PurePosixPath(relative).full_match(pattern):
                output.append(SandboxFileEntry(path, size))
        output.sort(key=lambda item: item.path)
        truncated = truncated or len(output) > max_results
        return SandboxListResult(tuple(output[:max_results]), truncated)

    def grep(
        self,
        pattern: str,
        *,
        workspace_root: str,
        file_pattern: str,
        case_sensitive: bool,
        max_results: int,
        timeout_seconds: int,
        path_predicate: SandboxPathPredicate,
    ) -> SandboxGrepResult:
        listed = self.list_files(
            file_pattern,
            workspace_root=workspace_root,
            max_results=20_000,
            timeout_seconds=timeout_seconds,
            path_predicate=path_predicate,
        )
        matches: list[SandboxGrepMatch] = []
        total_bytes = 0
        files_searched = 0
        for entry in listed.entries:
            relative = entry.path[len(self._workspace_root) + 1 :]
            if path_predicate(relative, False) is not True:
                raise ValueError("远端搜索尝试读取策略禁止的路径。")
            try:
                size = self._file_size(entry.path, timeout_seconds)
            except Exception:
                continue
            if size > MAX_SEARCHABLE_FILE_BYTES:
                continue
            total_bytes += size
            if total_bytes > 64 * 1024 * 1024:
                return SandboxGrepResult(tuple(matches), files_searched, True)
            raw = bytes(self._sandbox.files.read(entry.path, format="bytes", request_timeout=timeout_seconds))
            if len(raw) > MAX_SEARCHABLE_FILE_BYTES or b"\x00" in raw:
                continue
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                continue
            files_searched += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                for position in _positions(line, pattern, case_sensitive):
                    if len(matches) >= max_results:
                        return SandboxGrepResult(tuple(matches), files_searched, True)
                    excerpt = _excerpt(line, position, pattern)
                    if len(excerpt) > MAX_EXCERPT_CHARACTERS:
                        excerpt = excerpt[:MAX_EXCERPT_CHARACTERS]
                    matches.append(SandboxGrepMatch(entry.path, line_number, position + 1, excerpt))
        matches.sort(key=lambda item: (item.path, item.line_number, item.column))
        return SandboxGrepResult(tuple(matches), files_searched, listed.truncated)

    def run(
        self,
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
    ) -> SandboxCommandResult:
        return self.start_run(
            command,
            paths=paths,
            code=code,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            max_output_characters=max_output_characters,
            max_memory_mb=max_memory_mb,
            max_processes=max_processes,
        ).wait()

    def format_json_candidates(
        self,
        paths: Sequence[str],
        *,
        timeout_seconds: int,
    ) -> SandboxFormatJsonResult:
        """Generate normalized JSON bytes inside a random internal staging dir."""

        if (
            type(timeout_seconds) is not int
            or not 1 <= timeout_seconds <= _TRUSTED_RUNNER_TIMEOUT_MAX_SECONDS
            or not 1 <= len(paths) <= 8
        ):
            raise ValueError("format_json 请求超出安全上限。")
        normalized = tuple(sorted(self._validate_rooted(path) for path in paths))
        if len(normalized) != len(set(normalized)) or any(
            path.startswith(self._workspace_root + "/.agent-internal/")
            or not path.casefold().endswith(".json")
            for path in normalized
        ):
            raise ValueError("format_json 路径无效。")
        for path in normalized:
            self._assert_chain(path, final_may_be_missing=False, timeout_seconds=timeout_seconds)

        source = (
            Path(__file__).resolve().parent.parent / "tool" / "_sandbox_format_json_runner.py"
        ).read_bytes()
        token = token_hex(16)
        transaction_parent = self._workspace_root + "/.agent-internal/transactions"
        staging_root = transaction_parent + "/" + token
        # The trusted protocol fixes every possible staging filename from the
        # requested count.  Track that complete set before dispatch so malformed
        # runner output cannot make an already-created staging file invisible
        # to cleanup.
        staged_paths = [
            staging_root + "/" + f"{index:02d}.json"
            for index in range(len(normalized))
        ]
        payload = json.dumps(
            {"cwd": self._workspace_root, "paths": list(normalized), "staging_root": staging_root},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._ensure_directory_chain(staging_root, timeout_seconds)

        def cleanup() -> None:
            failed = False
            exists_method = getattr(self._sandbox.files, "exists", None)
            if not callable(exists_method):
                raise ValueError("format_json staging 清理无法确认。")
            for path in reversed(staged_paths):
                try:
                    removed = self._sandbox.files.remove(path, request_timeout=timeout_seconds)
                    exists = exists_method(path, request_timeout=timeout_seconds)
                    failed = (
                        failed
                        or removed is False
                        or type(exists) is not bool
                        or exists
                    )
                except Exception:
                    failed = True
            try:
                removed = self._sandbox.files.remove(staging_root, request_timeout=timeout_seconds)
                exists = exists_method(
                    staging_root, request_timeout=timeout_seconds
                )
                failed = (
                    failed
                    or removed is False
                    or type(exists) is not bool
                    or exists
                )
            except Exception:
                failed = True
            if failed:
                raise ValueError("format_json staging 清理无法确认。")

        def decode(raw_value: Any) -> SandboxCommandResult:
            raw = self._sdk_command_result(raw_value)
            if raw.exit_code != 0 or raw.stderr:
                raise ValueError("format_json 可信 runner 执行失败。")
            if len(raw.stdout.encode("utf-8")) > 64 * 1024:
                raise ValueError("format_json 可信 runner 输出超限。")
            return SandboxCommandResult(0, raw.stdout, "")

        try:
            raw = self._start_runner(
                runner_name="format_json_runner.py",
                runner_source=source,
                payload=payload,
                timeout_seconds=timeout_seconds,
                decode=decode,
            ).wait()
            try:
                value = json.loads(raw.stdout, object_pairs_hook=lambda pairs: _unique_json_object(pairs))
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ValueError("format_json 可信 runner 返回无效。") from None
            if type(value) is not dict or set(value) != {"files"} or type(value["files"]) is not list:
                raise ValueError("format_json 可信 runner 字段无效。")
            if len(value["files"]) != len(normalized):
                raise ValueError("format_json 可信 runner 文件数量不匹配。")
            results: list[SandboxFormatJsonFile] = []
            for index, (source_path, item) in enumerate(zip(normalized, value["files"], strict=True)):
                expected_staged = staged_paths[index]
                if (
                    type(item) is not dict
                    or set(item) != {"after_sha256", "before_sha256", "path", "size", "staged"}
                    or item["path"] != source_path
                    or item["staged"] != expected_staged
                    or type(item["size"]) is not int
                    or not 0 <= item["size"] <= self._MAX_VERSION_OBJECT_BYTES
                ):
                    raise ValueError("format_json 可信 runner 绑定无效。")
                before_digest = item["before_sha256"]
                after_digest = item["after_sha256"]
                for digest in (before_digest, after_digest):
                    if not isinstance(digest, str) or len(digest) != 64 or any(
                        character not in "0123456789abcdef" for character in digest
                    ):
                        raise ValueError("format_json 可信 runner 摘要无效。")
                get_info = getattr(self._sandbox.files, "get_info", None)
                if not callable(get_info):
                    raise ValueError("format_json staging 文件无法验证。")
                before_info = get_info(
                    expected_staged, request_timeout=timeout_seconds
                )
                if (
                    self._entry_type(before_info) not in {"file", "regular"}
                    or getattr(before_info, "symlink_target", None)
                    or getattr(before_info, "size", None) != item["size"]
                ):
                    raise ValueError("format_json staging 文件无效。")
                staged_data = bytes(self._sandbox.files.read(
                    expected_staged,
                    format="bytes",
                    request_timeout=timeout_seconds,
                ))
                after_info = get_info(
                    expected_staged, request_timeout=timeout_seconds
                )
                staged = SandboxReadResult(staged_data, len(staged_data))
                if (
                    self._entry_type(after_info) not in {"file", "regular"}
                    or getattr(after_info, "symlink_target", None)
                    or getattr(after_info, "size", None) != item["size"]
                    or self._entry_type(after_info) != self._entry_type(before_info)
                    or staged.total_bytes != item["size"]
                    or len(staged.data) != item["size"]
                    or hashlib.sha256(staged.data).hexdigest() != after_digest
                ):
                    raise ValueError("format_json staging 文件无效。")
                results.append(SandboxFormatJsonFile(source_path, before_digest, staged.data))
            cleanup()
            staged_paths.clear()
            return SandboxFormatJsonResult(tuple(results))
        except BaseException as error:
            try:
                cleanup()
            except BaseException:
                pass
            if not isinstance(error, Exception):
                raise
            raise ValueError("format_json 候选生成失败。") from None

    def start_run(
        self,
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
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> SandboxRunHandle:
        if cwd != self._workspace_root:
            raise ValueError("命令 cwd 必须固定为 workspace_root。")
        self._assert_workspace(timeout_seconds)
        self._verify_runtime_identity(timeout_seconds)
        for path in paths:
            if command == "unittest":
                self._assert_directory(path, timeout_seconds)
            elif command == "compileall":
                try:
                    self._assert_chain(path, final_may_be_missing=False, timeout_seconds=timeout_seconds)
                except ValueError:
                    self._assert_directory(path, timeout_seconds)
            else:
                self._assert_chain(path, final_may_be_missing=False, timeout_seconds=timeout_seconds)
        quoted = [shlex.quote(path) for path in paths]
        if command == "python":
            if code is None:
                raise ValueError("python 命令缺少代码。")
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= 10
                or isinstance(max_memory_mb, bool)
                or not isinstance(max_memory_mb, int)
                or not 64 <= max_memory_mb <= 512
                or isinstance(max_processes, bool)
                or not isinstance(max_processes, int)
                or not 1 <= max_processes <= 2
                or isinstance(max_output_characters, bool)
                or not isinstance(max_output_characters, int)
                or not 1024 <= max_output_characters <= 16_384
            ):
                raise ValueError("python 命令缺少有效的资源限制。")
            return self._run_restricted_code(
                code,
                timeout_seconds,
                max_output_bytes,
                max_output_characters,
                max_memory_mb,
                max_processes,
                start_only=True,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        if any(
            value is not None
            for value in (max_output_characters, max_memory_mb, max_processes)
        ):
            raise ValueError("非 Python 命令不能设置 Code Interpreter 资源限制。")
        elif command == "compileall":
            return self._run_structured_command(
                command,
                paths,
                timeout_seconds,
                max_output_bytes,
                start_only=True,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        elif command == "unittest":
            if len(quoted) != 1:
                raise ValueError("unittest 只接受一个目录路径。")
            return self._run_structured_command(
                command,
                paths,
                timeout_seconds,
                max_output_bytes,
                start_only=True,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        elif command == "validate_json":
            if not quoted:
                raise ValueError("validate_json 至少需要一个文件。")
            for raw_path in paths:
                if not raw_path.casefold().endswith(".json"):
                    raise ValueError("validate_json 只接受 JSON 文件。")
            return self._run_structured_command(
                command,
                paths,
                timeout_seconds,
                max_output_bytes,
                start_only=True,
                dispatch_attempt_consumer=dispatch_attempt_consumer,
            )
        else:
            raise ValueError("远端 command 不在白名单内。")

    def _new_invocation_paths(self, runner_name: str) -> tuple[str, str, str]:
        token = token_hex(16)
        invocation_dir = self._code_dir + "/invocations/" + token
        return (
            invocation_dir,
            invocation_dir + "/" + runner_name,
            invocation_dir + "/payload.json",
        )

    def _verified_write_internal(
        self,
        path: str,
        data: bytes,
        timeout_seconds: int,
    ) -> None:
        files = self._sandbox.files
        files.write(path, data, request_timeout=timeout_seconds)
        get_info = getattr(files, "get_info", None)
        if not callable(get_info):
            raise ValueError("远端 Filesystem 不支持内部文件身份校验。")
        info = get_info(path, request_timeout=timeout_seconds)
        if (
            self._entry_type(info) not in {"file", "regular"}
            or getattr(info, "symlink_target", None)
            or getattr(info, "size", None) != len(data)
        ):
            raise ValueError("远端内部文件身份无效。")
        actual = bytes(
            files.read(path, format="bytes", request_timeout=timeout_seconds)
        )
        if len(actual) != len(data) or not hashlib.sha256(actual).digest() == hashlib.sha256(data).digest():
            raise ValueError("远端内部文件内容校验失败。")

    def _start_runner(
        self,
        *,
        runner_name: str,
        runner_source: bytes,
        payload: bytes,
        timeout_seconds: int,
        decode: Callable[[Any], SandboxCommandResult],
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> SandboxRunHandle:
        provider_timeout_seconds = _provider_runner_timeout_seconds(
            timeout_seconds
        )
        invocation_dir, runner_path, payload_path = self._new_invocation_paths(
            runner_name
        )
        files = self._sandbox.files
        cleanup_paths = (payload_path, runner_path, invocation_dir)

        def cleanup() -> None:
            failed = False
            for path in cleanup_paths:
                try:
                    result = files.remove(path, request_timeout=timeout_seconds)
                    if result is False:
                        failed = True
                except Exception:
                    failed = True
            if failed:
                raise ValueError("远端命令临时资源清理失败。")

        try:
            self._ensure_directory_chain(invocation_dir, timeout_seconds)
            self._verified_write_internal(runner_path, runner_source, timeout_seconds)
            self._verified_write_internal(payload_path, payload, timeout_seconds)
            command = (
                shlex.quote(self._runtime_python)
                + " -I -S -X utf8 "
                + shlex.quote(runner_path)
                + " < "
                + shlex.quote(payload_path)
            )
            if dispatch_attempt_consumer is not None:
                if not callable(dispatch_attempt_consumer):
                    raise ValueError("远端派发观察器无效。")
                # This is the exact uncertainty boundary: after the callback
                # returns, a provider failure may mean the command started even
                # when no SDK handle reaches the host.
                dispatch_attempt_consumer()
            raw_handle = self._sandbox.commands.run(
                command,
                cwd=self._workspace_root,
                # The payload still carries ``timeout_seconds``.  This grace
                # only lets the trusted runner kill/drain its child, serialize
                # the bounded result, and return it through the SDK.
                timeout=provider_timeout_seconds,
                request_timeout=provider_timeout_seconds,
                background=True,
                user=self._runtime_user,
            )
            return _E2BRunHandle(raw_handle, decode=decode, cleanup=cleanup)
        except Exception:
            try:
                cleanup()
            except Exception:
                pass
            raise ValueError("远端后台命令启动失败。") from None

    def _run_restricted_code(
        self,
                code,
                timeout_seconds,
                max_output_bytes,
                max_output_characters,
                max_memory_mb,
                max_processes,
        *,
        start_only: bool = False,
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> SandboxCommandResult | SandboxRunHandle:
        runner_source = (
            Path(__file__).resolve().parent.parent / "tool" / "_code_interpreter_runner.py"
        ).read_bytes()
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
        ).encode("utf-8")

        def decode(raw: Any) -> SandboxCommandResult:
            result = self._sdk_command_result(raw)
            if result.exit_code != 0:
                return SandboxCommandResult(1, "", "远端受限 Python 执行失败。")
            try:
                response = json.loads(result.stdout)
                if not isinstance(response, dict) or set(response) != {
                    "exit_code", "stdout", "result", "stderr", "truncated"
                }:
                    raise ValueError
                if (
                    type(response["exit_code"]) is not int
                    or not isinstance(response["stdout"], str)
                    or not isinstance(response["result"], str)
                    or not isinstance(response["stderr"], str)
                    or type(response["truncated"]) is not bool
                ):
                    raise ValueError
                return self._bounded_command_result(
                    response["exit_code"], response["stdout"], response["stderr"],
                    max_output_bytes,
                    response["result"],
                    source_truncated=response["truncated"],
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return SandboxCommandResult(1, "", "远端受限 Python 返回无效结果。")

        handle = self._start_runner(
            runner_name="code_runner.py",
            runner_source=runner_source,
            payload=payload,
            timeout_seconds=timeout_seconds,
            decode=decode,
            dispatch_attempt_consumer=dispatch_attempt_consumer,
        )
        return handle if start_only else handle.wait()

    def _run_structured_command(
        self,
        command: str,
        paths: Sequence[str],
        timeout_seconds: int,
        max_output_bytes: int,
        *,
        start_only: bool = False,
        dispatch_attempt_consumer: Callable[[], None] | None = None,
    ) -> SandboxCommandResult | SandboxRunHandle:
        source = (
            Path(__file__).resolve().parent.parent / "tool" / "_sandbox_command_runner.py"
        ).read_bytes()
        payload = json.dumps(
            {
                "command": command,
                "paths": list(paths),
                "cwd": self._workspace_root,
                "timeout": timeout_seconds,
                "max_output_bytes": max_output_bytes,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        def decode(raw_value: Any) -> SandboxCommandResult:
            raw = self._sdk_command_result(raw_value)
            if raw.exit_code != 0:
                return SandboxCommandResult(1, "", "远端结构化命令执行失败。")
            try:
                response = json.loads(raw.stdout)
                if not isinstance(response, dict) or set(response) != {"exit_code", "stdout", "stderr", "truncated"}:
                    raise ValueError
                if (
                    type(response["exit_code"]) is not int
                    or not isinstance(response["stdout"], str)
                    or not isinstance(response["stderr"], str)
                    or type(response["truncated"]) is not bool
                ):
                    raise ValueError
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ValueError("远端结构化命令返回无效结果。") from None
            return self._bounded_command_result(
                response["exit_code"], response["stdout"], response["stderr"],
                max_output_bytes,
                source_truncated=response["truncated"],
            )

        handle = self._start_runner(
            runner_name="command_runner.py",
            runner_source=source,
            payload=payload,
            timeout_seconds=timeout_seconds,
            decode=decode,
            dispatch_attempt_consumer=dispatch_attempt_consumer,
        )
        return handle if start_only else handle.wait()

    @staticmethod
    def _sdk_command_result(result: Any) -> SandboxCommandResult:
        stdout = getattr(result, "stdout", None)
        stderr = getattr(result, "stderr", None)
        exit_code = getattr(result, "exit_code", None)
        if (
            not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or type(exit_code) is not int
        ):
            raise ValueError("远端命令输出类型无效。")
        return SandboxCommandResult(exit_code, stdout, stderr)

    def _run_command(
        self,
        cmd: str,
        *,
        cwd: str,
        timeout_seconds: int,
        user: str,
    ) -> SandboxCommandResult:
        result = self._sandbox.commands.run(
            cmd,
            cwd=cwd,
            timeout=timeout_seconds,
            request_timeout=timeout_seconds,
            user=user,
        )
        return self._sdk_command_result(result)

    @staticmethod
    def _bounded_command_result(
        exit_code: int,
        stdout: str,
        stderr: str,
        max_output_bytes: int,
        result: str = "",
        *,
        source_truncated: bool = False,
    ) -> SandboxCommandResult:
        if type(source_truncated) is not bool:
            raise ValueError("远端命令截断标记类型无效。")
        raw = (stdout + stderr).encode("utf-8", errors="replace")
        host_output_truncated = len(raw) > max_output_bytes
        truncated = source_truncated or host_output_truncated
        if host_output_truncated:
            stdout = raw[:max_output_bytes].decode("utf-8", errors="ignore")
            stderr = ""
        if len(result.encode("utf-8", errors="replace")) > max_output_bytes:
            result = result.encode("utf-8", errors="replace")[:max_output_bytes].decode(
                "utf-8", errors="ignore"
            )
            truncated = True
        return SandboxCommandResult(
            exit_code, stdout, stderr, result=result, truncated=truncated
        )

_EXECUTION_METHODS = (
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "grep",
    "run",
    "start_run",
    "format_json_candidates",
    "rollback_delete_file",
)
_PERSISTENCE_METHODS = (
    "persistence_read_file",
    "persistence_write_file",
    "persistence_list_files",
    "workspace_version_read_file",
    "workspace_version_probe_file",
    "workspace_version_write_file",
    "workspace_version_list_files",
    "rollback_delete_workspace_version_object",
    "gc_delete_workspace_version_object",
)


class E2BPersistenceTransport:
    """Narrow root-Files-API adapter for a storage-only Sandbox.

    Construction validates only the configured OSS mount.  It intentionally
    does not create a workspace, discover system Python, initialize a venv,
    create a runtime user, or execute chmod/chown probes.  The wrapped core is
    private so command and active-workspace methods are not exposed by this
    control-plane boundary.
    """

    def __init__(self, sandbox: Any, persistence_root: str) -> None:
        if not hasattr(sandbox, "files"):
            raise ValueError("persistence Sandbox 缺少受支持的 Filesystem API。")
        root = E2BSandboxTransport._validate_absolute_root(
            persistence_root, "persistence_root"
        )
        if not root.startswith("/mnt/"):
            raise ValueError("persistence_root 必须位于可信 /mnt 后代。")
        core = object.__new__(E2BSandboxTransport)
        core._sandbox = sandbox
        core._workspace_root = "/workspace/persistence-control-plane"
        core._persistence_root = root
        core._assert_persistence_directory(root, 30)
        self._core = core

    def __repr__(self) -> str:
        return "E2BPersistenceTransport(persistence_bound=True)"

    def persistence_read_file(self, *args: Any, **kwargs: Any) -> SandboxReadResult:
        return self._core.persistence_read_file(*args, **kwargs)

    def persistence_write_file(self, *args: Any, **kwargs: Any) -> SandboxWriteResult:
        return self._core.persistence_write_file(*args, **kwargs)

    def persistence_list_files(
        self, *args: Any, **kwargs: Any
    ) -> SandboxPersistenceListResult:
        return self._core.persistence_list_files(*args, **kwargs)

    def workspace_version_read_file(
        self, *args: Any, **kwargs: Any
    ) -> SandboxReadResult:
        return self._core.workspace_version_read_file(*args, **kwargs)

    def workspace_version_probe_file(
        self, *args: Any, **kwargs: Any
    ) -> SandboxReadResult | None:
        return self._core.workspace_version_probe_file(*args, **kwargs)

    def workspace_version_write_file(
        self, *args: Any, **kwargs: Any
    ) -> SandboxWriteResult:
        return self._core.workspace_version_write_file(*args, **kwargs)

    def workspace_version_list_files(
        self, *args: Any, **kwargs: Any
    ) -> SandboxPersistenceListResult:
        return self._core.workspace_version_list_files(*args, **kwargs)

    def rollback_delete_workspace_version_object(
        self, *args: Any, **kwargs: Any
    ) -> None:
        self._core.rollback_delete_workspace_version_object(*args, **kwargs)

    def gc_delete_workspace_version_object(
        self, *args: Any, **kwargs: Any
    ) -> None:
        self._core.gc_delete_workspace_version_object(*args, **kwargs)


class SplitSandboxTransport:
    """Explicit facade joining one execution and one persistence transport."""

    def __init__(
        self,
        execution: SandboxTransport,
        persistence: SandboxPersistenceTransport,
    ) -> None:
        if execution is None or persistence is None:
            raise ValueError("split transport 两端都必须存在。")
        self._execution = execution
        self._persistence = persistence

    @property
    def runtime_identity(self) -> SandboxRuntimeIdentity | None:
        value = getattr(self._execution, "runtime_identity", None)
        return value if isinstance(value, SandboxRuntimeIdentity) else None

    def __getattr__(self, name: str) -> Any:
        if name in _EXECUTION_METHODS:
            return getattr(self._execution, name)
        if name in _PERSISTENCE_METHODS:
            return getattr(self._persistence, name)
        raise AttributeError(name)

    def __repr__(self) -> str:
        return "SplitSandboxTransport(execution_bound=True, persistence_bound=True)"


def create_e2b_transport(
    sandbox: Any,
    workspace_root: str,
    persistence_root: str | None = None,
    runtime_venv_dir: str = "/opt/agnet-study/runtime/venv",
    runtime_user: str = "user",
) -> SandboxTransport:
    """Build the lazy concrete adapter without importing E2B at module load."""
    if not hasattr(sandbox, "files") or not hasattr(sandbox, "commands"):
        raise ValueError("远端 Sandbox 缺少受支持的 Filesystem/Commands API。")
    return E2BSandboxTransport(
        sandbox,
        workspace_root,
        persistence_root,
        runtime_venv_dir,
        runtime_user,
    )


def create_e2b_persistence_transport(
    sandbox: Any,
    persistence_root: str,
) -> SandboxPersistenceTransport:
    """Build a storage-only adapter without requiring a Commands API."""

    return E2BPersistenceTransport(sandbox, persistence_root)


__all__ = [
    "SandboxCommandResult",
    "SandboxEditResult",
    "SandboxFileEntry",
    "SandboxFormatJsonFile",
    "SandboxFormatJsonResult",
    "SandboxGrepMatch",
    "SandboxGrepResult",
    "SandboxListResult",
    "SandboxPersistenceEntry",
    "SandboxPersistenceListResult",
    "SandboxReadResult",
    "SandboxRunHandle",
    "SandboxRuntimeIdentity",
    "SandboxPersistenceTransport",
    "SandboxTransport",
    "SandboxWriteResult",
    "E2BSandboxTransport",
    "E2BPersistenceTransport",
    "SplitSandboxTransport",
    "validate_sandbox_relative_path",
    "create_e2b_transport",
    "create_e2b_persistence_transport",
]
