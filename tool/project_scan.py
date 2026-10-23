"""list_files 与 grep 共用的有界项目文件枚举。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import unicodedata

from tool import file_access
from app.security_domain import path_is_sealed_secret
from tool.file_access import ToolAccessDenied, ToolExecutionError
from tool.policy import PolicyLoadError, load_tool_policy, path_is_allowed


MAX_SCANNED_ENTRIES = 20_000
MAX_SCAN_DEPTH = 32
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
DEFAULT_IGNORED_DIRECTORIES = frozenset({
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
})


@dataclass(frozen=True)
class ScannedFile:
    path: str
    size: int


@dataclass(frozen=True)
class ScanResult:
    files: list[ScannedFile]
    truncated: bool


def matches_glob(path: str, pattern: str) -> bool:
    """使用完整 POSIX 路径匹配；``**`` 可跨越任意数量的目录。"""
    return PurePosixPath(path).full_match(pattern, case_sensitive=True)


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_safe_name(name: str) -> bool:
    """只输出能安全表示为项目相对 JSON 路径的目录项名称。"""
    return (
        "\\" not in name
        and ":" not in name
        and not any(unicodedata.category(character) == "Cc" for character in name)
    )


def _validate_project_pattern(pattern: str) -> None:
    """在任何目录访问之前执行项目相对权限检查。"""
    windows_path = PureWindowsPath(pattern)
    parts = pattern.replace("\\", "/").split("/")
    if (
        windows_path.drive
        or windows_path.root
        or "\\" in pattern
        or any(character in pattern for character in ':<>|"')
        or any(unicodedata.category(character) == "Cc" for character in pattern)
        or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
    ):
        raise ToolAccessDenied("只允许使用项目相对 POSIX glob 模式。")
    if path_is_sealed_secret(pattern):
        raise ToolAccessDenied("密封秘密路径拒绝扫描。")


def scan_project_files(pattern: str) -> ScanResult:
    """枚举匹配的普通文件，不跟随符号链接或 Windows reparse point。"""
    _validate_project_pattern(pattern)
    try:
        policy = load_tool_policy()
    except PolicyLoadError:
        raise ToolAccessDenied("工具权限策略不可用。") from None
    recursive = "**" in pattern.split("/")
    max_depth = MAX_SCAN_DEPTH if recursive else len(pattern.split("/"))
    root = file_access._PROJECT_ROOT
    pending = deque([(root, ())])
    matches: list[ScannedFile] = []
    scanned = 0
    scan_truncated = False

    while pending and not scan_truncated:
        directory, prefix = pending.popleft()
        remaining = MAX_SCANNED_ENTRIES - scanned
        if remaining <= 0:
            scan_truncated = True
            break
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if len(entries) >= remaining:
                        scan_truncated = True
                        break
                    entries.append(entry)
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            if not prefix:
                raise ToolExecutionError("无法扫描项目目录。") from exc
            # 子目录在扫描期间变化或不可读时，结果不是完整扫描。
            scan_truncated = True
            break

        entries.sort(key=lambda item: item.name)
        directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            scanned += 1
            if not _is_safe_name(entry.name):
                continue
            parts = (*prefix, entry.name)
            relative = PurePosixPath(*parts).as_posix()
            # 敏感路径既不会作为结果出现，也不会因为 pattern 明确点名而遍历。
            if not path_is_allowed(relative, policy):
                continue
            try:
                # CPython 3.14 在 Windows 上的 DirEntry.stat() 可能把普通文件
                # st_nlink 报为 0；os.stat(path) 能返回正确链接数，仍不跟随链接。
                info = os.stat(entry.path, follow_symlinks=False)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            try:
                is_link = entry.is_symlink()
            except OSError:
                continue
            if is_link or _is_reparse_point(info):
                continue
            if stat.S_ISREG(info.st_mode):
                # 多硬链接可能把根外 inode 暴露为项目内别名，因此不交给搜索工具。
                if info.st_nlink == 1 and matches_glob(relative, pattern):
                    matches.append(ScannedFile(path=relative, size=info.st_size))
            elif stat.S_ISDIR(info.st_mode):
                if (
                    entry.name in DEFAULT_IGNORED_DIRECTORIES
                ):
                    continue
                if len(parts) < max_depth:
                    directories.append((Path(entry.path), parts))
                elif recursive:
                    scan_truncated = True
        # 广度优先可在扫描上限内优先覆盖项目源码，而不是先陷入某个大目录。
        pending.extend(directories)

    matches.sort(key=lambda item: item.path)
    return ScanResult(files=matches, truncated=scan_truncated)
