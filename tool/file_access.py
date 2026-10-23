"""读写工具共用的文件访问边界；根目录由代码位置固定，模型不可覆盖。"""

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import stat
from typing import BinaryIO, Iterator

from app.permissions import (
    current_permission_mode,
    mutation_access_active,
)
from app.security_domain import SealedSecretPathError, require_unsealed_secret_path
from tool.policy import PolicyLoadError, require_allowed_relative_path
from tool.path_safety import is_windows_reserved


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ToolAccessDenied(Exception):
    """权限拒绝；不向外回传输入路径或原始异常。"""


class ToolExecutionError(Exception):
    """文件内容或状态不符合工具要求。"""


class ToolWriteRecoveryError(ToolExecutionError):
    """写入失败，并且无法确认原始字节已恢复或新文件已清理。"""


@dataclass(frozen=True)
class OpenedProjectFile:
    stream: BinaryIO
    path: Path

    @property
    def relative_path(self) -> str:
        return display_host_path(self.path)


def display_host_path(path: Path) -> str:
    try:
        return path.relative_to(_PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _absolute_input(path: str) -> bool:
    return Path(path).is_absolute()


def _absolute_candidate(path: str) -> Path:
    _require_unsealed_path(path)
    if current_permission_mode() != "full-access" or not _absolute_input(path):
        raise ToolAccessDenied("绝对路径只能由 full-access 宿主模式使用。")
    if "\x00" in path or "\\" in path and os.name != "nt":
        raise ToolAccessDenied("绝对路径表示无效。")
    candidate = Path(os.path.abspath(path))
    if any(part in {"", ".", ".."} for part in candidate.parts[1:]):
        raise ToolAccessDenied("绝对路径表示无效。")
    return candidate


def _path_anchor_and_parts(candidate: Path) -> tuple[Path, tuple[str, ...]]:
    if candidate.is_relative_to(_PROJECT_ROOT):
        return _PROJECT_ROOT, candidate.relative_to(_PROJECT_ROOT).parts
    anchor = Path(candidate.anchor)
    return anchor, candidate.relative_to(anchor).parts


def _verify_candidate_components(candidate: Path, *, include_leaf: bool) -> None:
    base, parts = _path_anchor_and_parts(candidate)
    current = base
    selected = parts if include_leaf else parts[:-1]
    for index, part in enumerate(selected):
        current /= part
        info = os.lstat(current)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            raise ToolAccessDenied("不允许通过链接或重解析点访问文件。")
        if index < len(selected) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ToolExecutionError("目标文件的父目录不存在或不是目录。")


def _resolve_allowed_path(path: str) -> Path:
    if _absolute_input(path):
        candidate = _absolute_candidate(path)
        _verify_candidate_components(candidate, include_leaf=True)
        if not candidate.is_file():
            raise ToolExecutionError("只能操作普通文件。")
        return candidate
    parts = _split_allowed_path(path)
    candidate = _PROJECT_ROOT.joinpath(*parts).resolve(strict=True)
    if not candidate.is_relative_to(_PROJECT_ROOT):
        raise ToolAccessDenied("拒绝访问项目根目录之外的文件。")
    _verify_no_link_components(parts)
    if not candidate.is_file():
        raise ToolExecutionError("只能操作普通文件。")
    return candidate


def _split_allowed_path(path: str) -> tuple[str, ...]:
    """纯词法校验；必须在任何 resolve、open 或创建操作之前执行。"""
    _require_unsealed_path(path)
    windows_path = PureWindowsPath(path)
    parts = path.replace("\\", "/").split("/")
    if (
        windows_path.drive
        or windows_path.root
        or is_windows_reserved(path)
        or any(character in path for character in ':*?<>|"')
        or any(ord(character) < 32 for character in path)
        or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
    ):
        raise ToolAccessDenied("只允许项目根目录内的正常相对文件路径。")
    normalized = "/".join(parts)
    if current_permission_mode() != "full-access":
        try:
            require_allowed_relative_path(
                normalized,
                allow_protected=mutation_access_active(),
            )
        except PolicyLoadError:
            raise ToolAccessDenied("路径不在工具白名单内，或权限策略不可用。") from None
    return tuple(parts)


def _require_unsealed_path(path: object) -> None:
    try:
        require_unsealed_secret_path(path)
    except SealedSecretPathError:
        raise ToolAccessDenied("密封秘密路径拒绝访问。") from None


def _verify_no_link_components(parts: tuple[str, ...]) -> None:
    """拒绝路径中的软链接、目录联接和其它重解析点。"""
    current = _PROJECT_ROOT
    for index, part in enumerate(parts):
        current /= part
        info = os.lstat(current)
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            raise ToolAccessDenied("不允许通过链接或重解析点访问文件。")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ToolExecutionError("目标文件的父目录不存在或不是目录。")


def _resolve_allowed_new_path(path: str) -> Path:
    """解析已存在的父目录，但不跟随或要求最后一个文件名存在。"""
    if _absolute_input(path):
        candidate = _absolute_candidate(path)
        _verify_candidate_components(candidate, include_leaf=False)
        if not candidate.parent.is_dir():
            raise ToolExecutionError("目标文件的父目录不存在或不是目录。")
        return candidate
    parts = _split_allowed_path(path)
    parent = _PROJECT_ROOT.joinpath(*parts[:-1]).resolve(strict=True)
    if not parent.is_relative_to(_PROJECT_ROOT):
        raise ToolAccessDenied("拒绝访问项目根目录之外的文件。")
    _verify_no_link_components(parts[:-1])
    if not parent.is_dir():
        raise ToolExecutionError("目标文件的父目录不存在或不是目录。")
    return parent / parts[-1]


def _resolve_allowed_new_path_with_parents(path: str) -> Path:
    """解析允许创建的词法路径；缺失父目录可存在，但已有组件必须安全。"""
    if _absolute_input(path):
        candidate = _absolute_candidate(path)
        base, raw_parts = _path_anchor_and_parts(candidate)
        parts = tuple(raw_parts)
        current = base
    else:
        parts = _split_allowed_path(path)
        current = _PROJECT_ROOT
        candidate = _PROJECT_ROOT.joinpath(*parts)
    missing_parent_seen = False
    for part in parts[:-1]:
        current /= part
        if missing_parent_seen:
            continue
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            missing_parent_seen = True
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if stat.S_ISLNK(info.st_mode) or attributes & 0x400:
            raise ToolAccessDenied("不允许通过链接或重解析点创建文件。")
        if not stat.S_ISDIR(info.st_mode):
            raise ToolExecutionError("目标文件的父路径不是目录。")
    if os.path.lexists(candidate):
        raise FileExistsError("目标文件已经存在。")
    return candidate


def _windows_handle_path(stream: BinaryIO, candidate: Path | None = None) -> Path:
    """以实际句柄的最终路径复核权限；不再次 resolve 遍历可被替换的路径。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    size = get_final_path(handle, None, 0, 0)
    if not size:
        raise ToolAccessDenied("无法验证文件句柄的项目访问权限。")
    for _ in range(3):
        buffer = ctypes.create_unicode_buffer(size + 1)
        length = get_final_path(handle, buffer, len(buffer), 0)
        if not length:
            raise ToolAccessDenied("无法验证文件句柄的项目访问权限。")
        if length < len(buffer):
            break
        size = length
    else:
        raise ToolAccessDenied("无法验证文件句柄的项目访问权限。")
    final_path = buffer.value
    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    resolved = Path(final_path)
    if not resolved.is_absolute() or (
        candidate is not None
        and os.path.normcase(str(resolved)) != os.path.normcase(str(candidate))
    ):
        raise ToolAccessDenied("文件句柄的最终路径与请求不一致。")
    return resolved


def _open_windows_for_update(candidate: Path) -> BinaryIO:
    """OPEN_EXISTING + 独占共享模式，阻止通常的并发读写和重命名。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    filename = str(candidate)
    if filename.startswith("\\\\"):
        filename = "\\\\?\\UNC\\" + filename[2:]
    else:
        filename = "\\\\?\\" + filename
    # GENERIC_READ | GENERIC_WRITE, share_mode=0, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL。
    handle = create_file(filename, 0xC0000000, 0, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 3):
            raise FileNotFoundError("文件不存在。")
        if error in (5, 32, 33):
            raise PermissionError("无法独占打开目标文件。")
        raise ToolExecutionError("无法打开目标文件。")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    try:
        # CRT 接管 handle 所有权；后续关闭 descriptor 即关闭原始 handle。
        return os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_for_create(candidate: Path) -> BinaryIO:
    """CREATE_NEW + 独占共享模式，绝不覆盖调用前已经存在的目录项。"""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    filename = str(candidate)
    if filename.startswith("\\\\"):
        filename = "\\\\?\\UNC\\" + filename[2:]
    else:
        filename = "\\\\?\\" + filename
    # GENERIC_READ | GENERIC_WRITE, share_mode=0, CREATE_NEW, FILE_ATTRIBUTE_NORMAL。
    handle = create_file(filename, 0xC0000000, 0, None, 1, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (80, 183):
            raise FileExistsError("目标文件已经存在。")
        if error in (2, 3):
            raise FileNotFoundError("目标文件的父目录不存在。")
        if error in (5, 32, 33):
            raise PermissionError("无法创建目标文件。")
        raise ToolExecutionError("无法创建目标文件。")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    created: os.stat_result | None = None
    try:
        created = os.fstat(descriptor)
        return os.fdopen(descriptor, "r+b", buffering=0)
    except BaseException as exc:
        try:
            os.close(descriptor)
        except OSError:
            raise ToolWriteRecoveryError("创建失败，且无法关闭新文件句柄。") from exc
        if created is None or not _cleanup_created_windows(candidate, created):
            raise ToolWriteRecoveryError("创建失败，且无法确认新文件已清理。") from exc
        raise


def _open_posix(candidate: Path, writable: bool) -> BinaryIO:
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ToolAccessDenied("当前平台无法安全验证文件访问权限。")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    base, parts = _path_anchor_and_parts(candidate)
    directory_fd = os.open(base, directory_flags)
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
        # 不包含 O_CREAT/O_TRUNC；工具不能借此创建或提前截断文件。
        file_fd = os.open(parts[-1], flags, dir_fd=directory_fd)
        try:
            return os.fdopen(file_fd, "r+b" if writable else "rb", buffering=0 if writable else -1)
        except BaseException:
            os.close(file_fd)
            raise
    finally:
        os.close(directory_fd)


def _open_posix_for_create(candidate: Path) -> tuple[BinaryIO, int, str]:
    """通过逐层 dir_fd 遍历和 O_EXCL 原子创建，保留父目录句柄用于清理。"""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ToolAccessDenied("当前平台无法安全验证文件访问权限。")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    base, parts = _path_anchor_and_parts(candidate)
    directory_fd = os.open(base, directory_flags)
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
        file_fd = os.open(parts[-1], flags, 0o600, dir_fd=directory_fd)
        created: os.stat_result | None = None
        try:
            created = os.fstat(file_fd)
            stream = os.fdopen(file_fd, "r+b", buffering=0)
        except BaseException as exc:
            try:
                os.close(file_fd)
            except OSError:
                raise ToolWriteRecoveryError("创建失败，且无法关闭新文件句柄。") from exc
            if created is None or not _cleanup_created_posix(
                directory_fd, parts[-1], created
            ):
                raise ToolWriteRecoveryError("创建失败，且无法确认新文件已清理。") from exc
            raise
        return stream, directory_fd, parts[-1]
    except BaseException:
        os.close(directory_fd)
        raise


def verify_open_file(stream: BinaryIO, candidate: Path, *, writable: bool) -> tuple[Path, os.stat_result]:
    """在同一已打开句柄上验证类型、最终位置和硬链接限制。"""
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode):
        raise ToolExecutionError("只能操作普通文件。")
    actual_path = _windows_handle_path(stream, candidate) if os.name == "nt" else candidate
    if info.st_nlink != 1:
        raise ToolAccessDenied("不能访问具有多个硬链接的文件。")
    return actual_path, info


def resolve_project_directory(path: str) -> Path:
    """解析白名单内的已有目录，并拒绝任意链接或重解析点。"""
    if _absolute_input(path):
        candidate = _absolute_candidate(path)
        _verify_candidate_components(candidate, include_leaf=True)
        if not candidate.is_dir():
            raise ToolExecutionError("目标不是目录。")
        return candidate
    parts = _split_allowed_path(path)
    candidate = _PROJECT_ROOT.joinpath(*parts).resolve(strict=True)
    if not candidate.is_relative_to(_PROJECT_ROOT):
        raise ToolAccessDenied("拒绝访问项目根目录之外的目录。")
    _verify_no_link_components(parts)
    if not candidate.is_dir():
        raise ToolExecutionError("目标不是目录。")
    return candidate


def resolve_project_file(path: str) -> Path:
    """公开只读文件路径解析，供结构化命令在执行前验证全部操作数。"""
    candidate = _resolve_allowed_path(path)
    info = os.stat(candidate, follow_symlinks=False)
    if info.st_nlink != 1:
        raise ToolAccessDenied("不能访问具有多个硬链接的文件。")
    return candidate


def resolve_project_new_file(path: str) -> Path:
    """只解析允许创建的新文件路径；不创建目录项，也不跟随链接。"""
    candidate = _resolve_allowed_new_path(path)
    try:
        os.lstat(candidate)
    except FileNotFoundError:
        return candidate
    raise FileExistsError("目标文件已经存在。")


def resolve_project_new_file_with_parents(path: str) -> Path:
    """解析允许创建的新文件；只允许安全已有目录或缺失父目录。"""
    return _resolve_allowed_new_path_with_parents(path)


@contextmanager
def open_project_file(path: str, *, writable: bool = False) -> Iterator[OpenedProjectFile]:
    candidate = _resolve_allowed_path(path)
    if os.name == "nt":
        stream = _open_windows_for_update(candidate) if writable else candidate.open("rb")
    elif os.name == "posix":
        stream = _open_posix(candidate, writable)
    else:
        raise ToolAccessDenied("当前平台无法安全验证文件访问权限。")
    with stream:
        actual_path, _ = verify_open_file(stream, candidate, writable=writable)
        yield OpenedProjectFile(stream=stream, path=actual_path)


def _same_file_identity(current: os.stat_result, created: os.stat_result) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == created.st_dev
        and current.st_ino == created.st_ino
    )


def _cleanup_created_windows(candidate: Path, created: os.stat_result) -> bool:
    try:
        current = candidate.lstat()
        if not _same_file_identity(current, created):
            return False
        candidate.unlink()
        return not os.path.lexists(candidate)
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _cleanup_created_posix(parent_fd: int, leaf: str, created: os.stat_result) -> bool:
    try:
        current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_file_identity(current, created):
            return False
        os.unlink(leaf, dir_fd=parent_fd)
        try:
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _same_directory_identity(current: os.stat_result, created: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(current.st_mode)
        and current.st_dev == created.st_dev
        and current.st_ino == created.st_ino
        and not (getattr(current, "st_file_attributes", 0) & 0x400)
    )


def _cleanup_created_directories(created: list[tuple[Path, os.stat_result]]) -> bool:
    """反向删除仍为本次创建身份的空目录；绝不递归删除或跟随链接。"""
    confirmed = True
    for directory, identity in reversed(created):
        try:
            current = os.lstat(directory)
            if not _same_directory_identity(current, identity):
                confirmed = False
                continue
            directory.rmdir()
            if os.path.lexists(directory):
                confirmed = False
        except FileNotFoundError:
            continue
        except OSError:
            confirmed = False
    return confirmed


def _create_missing_parent_directories(candidate: Path) -> list[tuple[Path, os.stat_result]]:
    """逐层创建父目录，并记录身份以便失败时进行精确空目录清理。"""
    base, all_parts = _path_anchor_and_parts(candidate)
    parts = all_parts[:-1]
    current = base
    created: list[tuple[Path, os.stat_result]] = []
    try:
        for part in parts:
            current /= part
            created_here = False
            try:
                current.mkdir()
                created_here = True
            except FileExistsError:
                pass
            info = os.lstat(current)
            attributes = getattr(info, "st_file_attributes", 0)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or attributes & 0x400
            ):
                raise ToolAccessDenied("不允许通过链接或非目录父路径创建文件。")
            if created_here:
                created.append((current, info))
        return created
    except BaseException:
        if not _cleanup_created_directories(created):
            raise ToolWriteRecoveryError("父目录创建失败，且无法确认本次目录已清理。")
        raise


@contextmanager
def create_project_file(path: str) -> Iterator[OpenedProjectFile]:
    """独占创建项目内新文件；调用体失败时只清理本次创建的同一文件。"""
    candidate = _resolve_allowed_new_path(path)
    parent_fd: int | None = None
    leaf: str | None = None
    if os.name == "nt":
        stream = _open_windows_for_create(candidate)
    elif os.name == "posix":
        stream, parent_fd, leaf = _open_posix_for_create(candidate)
    else:
        raise ToolAccessDenied("当前平台无法安全验证文件访问权限。")

    created: os.stat_result | None = None
    failed = True
    try:
        with stream:
            # 先记录本次独占创建得到的句柄身份，使后续验证失败时也能安全清理。
            created = os.fstat(stream.fileno())
            actual_path, _ = verify_open_file(stream, candidate, writable=True)
            yield OpenedProjectFile(stream=stream, path=actual_path)
            failed = False
    finally:
        cleanup_confirmed = True
        if failed and created is not None:
            cleanup_confirmed = (
                _cleanup_created_windows(candidate, created)
                if os.name == "nt"
                else _cleanup_created_posix(parent_fd, leaf, created)  # type: ignore[arg-type]
            )
        if parent_fd is not None:
            os.close(parent_fd)
        if failed and not cleanup_confirmed:
            raise ToolWriteRecoveryError("写入失败，且无法确认新建文件已清理。")


@contextmanager
def create_project_file_with_parents(path: str) -> Iterator[OpenedProjectFile]:
    """批准后创建缺失父目录并独占创建文件；失败时清理本次空目录。"""
    candidate = _resolve_allowed_new_path_with_parents(path)
    created_directories = _create_missing_parent_directories(candidate)
    succeeded = False
    try:
        with create_project_file(path) as opened:
            yield opened
        succeeded = True
    finally:
        if not succeeded and not _cleanup_created_directories(created_directories):
            raise ToolWriteRecoveryError("写入失败，且无法确认本次创建的父目录已清理。")
