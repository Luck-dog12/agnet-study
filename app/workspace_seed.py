"""Bounded, immutable host workspace snapshot and remote bootstrap uploader.

The snapshot is captured in the host control plane.  Only the already-typed
``SandboxTransport`` receives its bytes; this module never reaches the raw
Sandbox SDK, an archive utility, a shell, or the OSS persistence mount.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import posixpath
from typing import TYPE_CHECKING, Final

from app.schemas import ReadFileArguments
from app.security_domain import path_is_sealed_secret
from app.sandbox_transport import (
    SandboxReadResult,
    SandboxFileEntry,
    SandboxListResult,
    SandboxTransport,
    SandboxWriteResult,
    validate_sandbox_relative_path,
)
from tool.project_scan import scan_project_files
from tool.read_file import read_file

if TYPE_CHECKING:
    from app.execution_backend import ExecutionLease


MAX_SEED_FILES: Final[int] = 1_000
MAX_SEED_FILE_BYTES: Final[int] = 1 * 1024 * 1024
MAX_SEED_TOTAL_BYTES: Final[int] = 32 * 1024 * 1024
_UPLOAD_TIMEOUT_SECONDS: Final[int] = 30
_WORKSPACE_PREFIX: Final[str] = "/workspace/agnet-study"

# These exclusions are code-owned and therefore cannot be relaxed by
# ``.agent/tool_policy.json``.  Comparisons are case-insensitive so a snapshot
# captured on one host cannot expose a control-plane directory after upload to
# a case-sensitive Sandbox.
_EXCLUDED_TOP_LEVEL_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {"data", "skills", ".agent", ".git", ".venv"}
)
_EXCLUDED_CACHE_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {
        "__pycache__",
        ".cache",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)


class WorkspaceSeedError(RuntimeError):
    """A host snapshot could not be captured safely and completely."""


class WorkspaceSeedUploadError(RuntimeError):
    """A remote workspace could not be initialized and verified."""


def _path_is_hard_excluded(path: str) -> bool:
    if path_is_sealed_secret(path):
        return True
    parts = tuple(part.casefold() for part in path.split("/"))
    if not parts:
        return True
    if parts[0] in _EXCLUDED_TOP_LEVEL_DIRECTORIES:
        return True
    if any(part in _EXCLUDED_CACHE_DIRECTORIES for part in parts[:-1]):
        return True
    leaf = parts[-1]
    return leaf == "code.md" or leaf.startswith(".env")


def _validate_seed_path(path: str) -> str:
    try:
        normalized = validate_sandbox_relative_path(path)
    except (TypeError, ValueError):
        raise WorkspaceSeedError("工作区 seed 包含无效路径。") from None
    if _path_is_hard_excluded(normalized):
        raise WorkspaceSeedError("工作区 seed 包含控制面或敏感路径。")
    return normalized


def _entry_manifest(entry: "WorkspaceSeedEntry") -> list[object]:
    return [entry.path, entry.size, entry.sha256]


def _snapshot_digest(entries: tuple["WorkspaceSeedEntry", ...]) -> str:
    manifest = json.dumps(
        [_entry_manifest(entry) for entry in entries],
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(manifest).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceSeedEntry:
    """One immutable file body plus its independently verifiable metadata."""

    path: str
    size: int
    sha256: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_seed_path(self.path)
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
            or self.size > MAX_SEED_FILE_BYTES
            or not isinstance(self.data, bytes)
            or len(self.data) != self.size
            or not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
            or hashlib.sha256(self.data).hexdigest() != self.sha256
        ):
            raise WorkspaceSeedError("工作区 seed 文件元数据无效。")


@dataclass(frozen=True, slots=True)
class WorkspaceSeedSnapshot:
    """An immutable, uniquely sorted collection of bounded host files."""

    entries: tuple[WorkspaceSeedEntry, ...]
    total_bytes: int = field(init=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple):
            raise WorkspaceSeedError("工作区 seed 条目集合必须不可变。")
        _validate_snapshot(self, verify_derived=False)
        object.__setattr__(self, "total_bytes", sum(item.size for item in self.entries))
        object.__setattr__(self, "digest", _snapshot_digest(self.entries))

    def __repr__(self) -> str:
        return (
            "WorkspaceSeedSnapshot(files={!r}, total_bytes={!r}, digest={!r})"
        ).format(len(self.entries), self.total_bytes, self.digest)


def _validate_snapshot(
    snapshot: WorkspaceSeedSnapshot,
    *,
    verify_derived: bool = True,
) -> None:
    if type(snapshot) is not WorkspaceSeedSnapshot or not isinstance(snapshot.entries, tuple):
        raise WorkspaceSeedError("工作区 seed 快照类型无效。")
    if len(snapshot.entries) > MAX_SEED_FILES:
        raise WorkspaceSeedError("工作区 seed 文件数量超过安全上限。")
    paths: list[str] = []
    total_bytes = 0
    for entry in snapshot.entries:
        if type(entry) is not WorkspaceSeedEntry:
            raise WorkspaceSeedError("工作区 seed 条目类型无效。")
        # Revalidate all fields because frozen dataclasses are a convention,
        # not a trust boundary against a hostile in-process caller.
        WorkspaceSeedEntry.__post_init__(entry)
        paths.append(entry.path)
        total_bytes += entry.size
        if total_bytes > MAX_SEED_TOTAL_BYTES:
            raise WorkspaceSeedError("工作区 seed 总大小超过安全上限。")
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise WorkspaceSeedError("工作区 seed 路径必须唯一并按规范顺序排列。")
    if verify_derived and (
        isinstance(snapshot.total_bytes, bool)
        or snapshot.total_bytes != total_bytes
        or snapshot.digest != _snapshot_digest(snapshot.entries)
    ):
        raise WorkspaceSeedError("工作区 seed 快照摘要无效。")


def _decode_read_result(content: str, encoding: str) -> bytes:
    try:
        if encoding == "utf-8":
            return content.encode("utf-8")
        if encoding == "base64":
            return base64.b64decode(content, validate=True)
    except (UnicodeError, ValueError):
        pass
    raise WorkspaceSeedError("工作区 seed 文件读取结果无效。")


def capture_workspace_seed() -> WorkspaceSeedSnapshot:
    """Capture all currently allowed host project files as one stable seed."""
    try:
        scan = scan_project_files("**/*")
        if scan.truncated:
            raise WorkspaceSeedError("项目扫描不完整，拒绝建立远端工作区 seed。")

        selected = [item for item in scan.files if not _path_is_hard_excluded(item.path)]
        if len(selected) > MAX_SEED_FILES:
            raise WorkspaceSeedError("工作区 seed 文件数量超过安全上限。")

        declared_total = 0
        for scanned in selected:
            if (
                isinstance(scanned.size, bool)
                or not isinstance(scanned.size, int)
                or scanned.size < 0
                or scanned.size > MAX_SEED_FILE_BYTES
            ):
                raise WorkspaceSeedError("工作区 seed 单文件超过安全上限。")
            declared_total += scanned.size
            if declared_total > MAX_SEED_TOTAL_BYTES:
                raise WorkspaceSeedError("工作区 seed 总大小超过安全上限。")

        entries: list[WorkspaceSeedEntry] = []
        total_bytes = 0
        seen: set[str] = set()
        for scanned in sorted(selected, key=lambda item: item.path):
            path = _validate_seed_path(scanned.path)
            if path in seen:
                raise WorkspaceSeedError("工作区 seed 路径不唯一。")
            seen.add(path)
            total_bytes += scanned.size
            if total_bytes > MAX_SEED_TOTAL_BYTES:
                raise WorkspaceSeedError("工作区 seed 总大小超过安全上限。")

            result = read_file(
                ReadFileArguments(
                    path=path,
                    offset=0,
                    max_bytes=MAX_SEED_FILE_BYTES,
                )
            )
            data = _decode_read_result(result.content, result.encoding)
            if (
                result.path != path
                or result.offset != 0
                or result.total_bytes != scanned.size
                or result.bytes_read != scanned.size
                or result.truncated
                or len(data) != scanned.size
            ):
                raise WorkspaceSeedError(
                    "项目文件在 seed 捕获期间变化或读取不完整。"
                )
            entries.append(
                WorkspaceSeedEntry(
                    path=path,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    data=data,
                )
            )
        return WorkspaceSeedSnapshot(tuple(entries))
    except WorkspaceSeedError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise WorkspaceSeedError("无法安全捕获宿主项目工作区 seed。") from None


def _validate_remote_workspace_root(workspace_root: str) -> str:
    if (
        not isinstance(workspace_root, str)
        or not workspace_root.startswith(_WORKSPACE_PREFIX + "/")
        or posixpath.normpath(workspace_root) != workspace_root
        or "\\" in workspace_root
        or "\x00" in workspace_root
    ):
        raise WorkspaceSeedUploadError("远端工作区根目录无效。")
    suffix = workspace_root[len(_WORKSPACE_PREFIX) + 1 :]
    if (
        len(suffix) != 32
        or any(character not in "0123456789abcdef" for character in suffix)
        or "/" in suffix
    ):
        raise WorkspaceSeedUploadError("远端工作区根目录无效。")
    return workspace_root


def upload_workspace_seed(
    transport: SandboxTransport,
    workspace_root: str,
    snapshot: WorkspaceSeedSnapshot,
) -> None:
    """Create and verify every seed file inside one fixed active workspace."""
    try:
        root = _validate_remote_workspace_root(workspace_root)
        _validate_snapshot(snapshot)
        before = transport.list_files(
            "**/*",
            workspace_root=root,
            max_results=MAX_SEED_FILES,
            timeout_seconds=_UPLOAD_TIMEOUT_SECONDS,
            path_predicate=lambda _path, _is_directory: True,
        )
        if (
            type(before) is not SandboxListResult
            or before.truncated
            or before.entries
        ):
            raise WorkspaceSeedUploadError("远端工作区初始化前不是空工作区。")
        for entry in snapshot.entries:
            remote_path = posixpath.join(root, entry.path)
            if (
                not remote_path.startswith(root + "/")
                or posixpath.normpath(remote_path) != remote_path
            ):
                raise WorkspaceSeedUploadError("远端 seed 目标路径无效。")
            written = transport.write_file(
                remote_path,
                entry.data,
                overwrite=False,
                timeout_seconds=_UPLOAD_TIMEOUT_SECONDS,
            )
            if (
                type(written) is not SandboxWriteResult
                or isinstance(written.bytes_written, bool)
                or written.bytes_written != entry.size
            ):
                raise WorkspaceSeedUploadError("远端 seed 写入结果无法验证。")
            read_back = transport.read_file(
                remote_path,
                offset=0,
                max_bytes=entry.size + 1,
                timeout_seconds=_UPLOAD_TIMEOUT_SECONDS,
            )
            if (
                type(read_back) is not SandboxReadResult
                or not isinstance(read_back.data, bytes)
                or isinstance(read_back.total_bytes, bool)
                or read_back.total_bytes != entry.size
                or len(read_back.data) != entry.size
                or hashlib.sha256(read_back.data).hexdigest() != entry.sha256
            ):
                raise WorkspaceSeedUploadError("远端 seed 回读校验失败。")
        after = transport.list_files(
            "**/*",
            workspace_root=root,
            max_results=MAX_SEED_FILES,
            timeout_seconds=_UPLOAD_TIMEOUT_SECONDS,
            path_predicate=lambda _path, _is_directory: True,
        )
        if type(after) is not SandboxListResult or after.truncated:
            raise WorkspaceSeedUploadError("远端工作区 seed 清单无法完整验证。")
        expected = tuple((posixpath.join(root, item.path), item.size) for item in snapshot.entries)
        actual: list[tuple[str, int]] = []
        for item in after.entries:
            if (
                type(item) is not SandboxFileEntry
                or not isinstance(item.path, str)
                or isinstance(item.size, bool)
                or not isinstance(item.size, int)
                or item.size < 0
            ):
                raise WorkspaceSeedUploadError("远端工作区 seed 清单包含无效条目。")
            actual.append((item.path, item.size))
        if tuple(actual) != expected:
            raise WorkspaceSeedUploadError("远端工作区 seed 清单不匹配。")
    except WorkspaceSeedUploadError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise WorkspaceSeedUploadError("远端工作区 seed 初始化失败。") from None


def create_workspace_seed_initializer(
    snapshot: WorkspaceSeedSnapshot,
    *,
    collect_orphans: bool = False,
) -> Callable[[SandboxTransport, "ExecutionLease"], None]:
    """Bind one immutable seed through the OSS baseline checkpoint seam."""
    _validate_snapshot(snapshot)
    from app.workspace_checkpoint import create_workspace_checkpoint_initializer

    return create_workspace_checkpoint_initializer(
        snapshot,
        collect_orphans=collect_orphans,
    )


__all__ = [
    "MAX_SEED_FILES",
    "MAX_SEED_FILE_BYTES",
    "MAX_SEED_TOTAL_BYTES",
    "WorkspaceSeedEntry",
    "WorkspaceSeedError",
    "WorkspaceSeedSnapshot",
    "WorkspaceSeedUploadError",
    "capture_workspace_seed",
    "create_workspace_seed_initializer",
    "upload_workspace_seed",
]
