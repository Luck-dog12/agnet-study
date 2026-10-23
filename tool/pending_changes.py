"""受保护的文件候选存储与有界 unified diff 生成。

候选内容使用随机 UUID ``.bin`` 文件，最小元数据使用同名严格 JSON 清单。正文、
原文件内容及 diff 不会写入清单。普通 Agent 文件工具由策略层禁止访问该目录。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
import unicodedata

from pydantic import ValidationError

from app.schemas import PendingChangeEntry, PendingChangeManifest
from tool import file_access
from tool.file_access import ToolAccessDenied, ToolExecutionError


MAX_PENDING_BYTES = 8 * 1024 * 1024
MAX_DIFF_CHARACTERS = 64 * 1024
MAX_DIFF_LINES = 1000
_PENDING_ID = re.compile(r"^[0-9a-f]{32}$")
_PENDING_FILE_NAME = re.compile(r"^([0-9a-f]{32})\.(bin|json)$")
_REPARSE_POINT = 0x400
_MANIFEST_MAX_BYTES = 16 * 1024
_RETENTION = timedelta(days=7)
_PENDING_LOCK = RLock()


@dataclass(frozen=True)
class PendingCandidate:
    pending_file_id: str
    size: int
    sha256: str


@dataclass(frozen=True)
class StoredPendingChange:
    manifest: PendingChangeManifest
    candidate: PendingCandidate
    raw: bytes


class _DescriptorView:
    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor

    def fileno(self) -> int:
        return self._descriptor


def _safe_directory(path: Path) -> os.stat_result:
    info = os.lstat(path)
    attributes = getattr(info, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or attributes & _REPARSE_POINT
    ):
        raise ToolAccessDenied("候选文件目录状态不安全。")
    return info


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and not stat.S_ISLNK(right.st_mode)
        and not (getattr(right, "st_file_attributes", 0) & _REPARSE_POINT)
    )


def _pending_root() -> tuple[Path, tuple[os.stat_result, os.stat_result, os.stat_result]]:
    project_root = file_access._PROJECT_ROOT
    root_info = _safe_directory(project_root)
    data = project_root / "data"
    pending = data / "pending"
    for directory in (data, pending):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        _safe_directory(directory)
    return pending, (root_info, _safe_directory(data), _safe_directory(pending))


def _verify_directory_chain(
    pending: Path,
    identities: tuple[os.stat_result, os.stat_result, os.stat_result],
) -> None:
    paths = (file_access._PROJECT_ROOT, pending.parent, pending)
    for path, expected in zip(paths, identities, strict=True):
        if not _same_directory(expected, _safe_directory(path)):
            raise ToolAccessDenied("候选文件目录在操作期间发生变化。")


def _candidate_path(pending_file_id: str) -> Path:
    if not isinstance(pending_file_id, str) or not _PENDING_ID.fullmatch(pending_file_id):
        raise ToolAccessDenied("候选文件标识无效。")
    return file_access._PROJECT_ROOT / "data" / "pending" / f"{pending_file_id}.bin"


def _manifest_path(pending_file_id: str) -> Path:
    if not isinstance(pending_file_id, str) or not _PENDING_ID.fullmatch(pending_file_id):
        raise ToolAccessDenied("候选文件标识无效。")
    return file_access._PROJECT_ROOT / "data" / "pending" / f"{pending_file_id}.json"


def _verify_file(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
        or info.st_nlink != 1
    ):
        raise ToolAccessDenied("候选文件状态不安全。")


def _write_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise OSError("候选文件写入未取得进展")
        offset += written


def _read_safe_raw(path: Path, *, maximum: int) -> tuple[bytes, os.stat_result]:
    """通过无跟随句柄读取一个受保护文件，并绑定目录项身份。"""
    pending, identities = _pending_root()
    if path.parent != pending:
        raise ToolAccessDenied("候选文件位置无效。")
    before = os.lstat(path)
    _verify_file(before)
    if before.st_size > maximum:
        raise ToolExecutionError("候选元数据超过大小限制。")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        _verify_file(opened)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ToolExecutionError("候选文件在打开期间发生变化。")
        if os.name == "nt" and file_access._windows_handle_path(_DescriptorView(descriptor)) != path:
            raise ToolAccessDenied("候选文件句柄位置无效。")
        _verify_directory_chain(pending, identities)
        chunks: list[bytes] = []
        remaining = opened.st_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    after = os.lstat(path)
    _verify_file(after)
    if (
        len(raw) != opened.st_size
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise ToolExecutionError("候选文件在读取期间发生变化。")
    return raw, after


def _write_exclusive(path: Path, raw: bytes) -> os.stat_result:
    """在固定 pending 目录独占创建普通单链接文件。"""
    pending, identities = _pending_root()
    if path.parent != pending:
        raise ToolAccessDenied("候选文件位置无效。")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        created = os.fstat(descriptor)
        _verify_file(created)
        if os.name == "nt" and file_access._windows_handle_path(_DescriptorView(descriptor)) != path:
            raise ToolAccessDenied("候选文件句柄位置无效。")
        _verify_directory_chain(pending, identities)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = os.lstat(path)
        _verify_file(current)
        if (current.st_dev, current.st_ino, current.st_size) != (
            created.st_dev,
            created.st_ino,
            len(raw),
        ):
            raise ToolExecutionError("候选文件写入后发生变化。")
        _verify_directory_chain(pending, identities)
        return current
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            current = os.lstat(path)
            if created is not None and (
                current.st_dev == created.st_dev
                and current.st_ino == created.st_ino
                and stat.S_ISREG(current.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and current.st_nlink == 1
            ):
                path.unlink()
        except OSError:
            pass
        raise


def _unlink_safe(path: Path, expected: os.stat_result | None = None) -> None:
    """仅删除仍为同一普通单链接文件的精确目录项。"""
    before = os.lstat(path)
    _verify_file(before)
    if expected is not None and (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
    ):
        raise ToolExecutionError("候选文件在清理前发生变化。")
    current = os.lstat(path)
    _verify_file(current)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
    ):
        raise ToolExecutionError("候选文件在清理前发生变化。")
    path.unlink()


def store_candidate(pending_file_id: str, raw: bytes) -> PendingCandidate:
    """独占创建一个完整候选文件，失败时仅删除本次创建的同一目录项。"""
    if not isinstance(raw, bytes) or len(raw) > MAX_PENDING_BYTES:
        raise ToolExecutionError("候选文件超过大小限制。")
    pending, identities = _pending_root()
    candidate = _candidate_path(pending_file_id)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        descriptor = os.open(candidate, flags, 0o600)
        created = os.fstat(descriptor)
        _verify_file(created)
        if os.name == "nt" and file_access._windows_handle_path(_DescriptorView(descriptor)) != candidate:
            raise ToolAccessDenied("候选文件句柄位置无效。")
        _verify_directory_chain(pending, identities)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = os.lstat(candidate)
        _verify_file(current)
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
        ) != (
            created.st_dev,
            created.st_ino,
            len(raw),
        ):
            raise ToolExecutionError("候选文件写入后发生变化。")
        _verify_directory_chain(pending, identities)
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            current = os.lstat(candidate)
            if created is not None and (
                current.st_dev == created.st_dev
                and current.st_ino == created.st_ino
                and stat.S_ISREG(current.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and current.st_nlink == 1
            ):
                candidate.unlink()
        except OSError:
            pass
        raise
    return PendingCandidate(pending_file_id, len(raw), sha256(raw).hexdigest())


def load_candidate(candidate: PendingCandidate) -> bytes:
    """通过无跟随句柄读取候选，并验证随机 ID、大小和内容哈希。"""
    path = _candidate_path(candidate.pending_file_id)
    pending, identities = _pending_root()
    if path.parent != pending:
        raise ToolAccessDenied("候选文件位置无效。")
    before = os.lstat(path)
    _verify_file(before)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        _verify_file(info)
        if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
            raise ToolExecutionError("候选文件在打开期间发生变化。")
        if os.name == "nt" and file_access._windows_handle_path(_DescriptorView(descriptor)) != path:
            raise ToolAccessDenied("候选文件句柄位置无效。")
        if info.st_size != candidate.size or info.st_size > MAX_PENDING_BYTES:
            raise ToolExecutionError("候选文件状态已经变化。")
        _verify_directory_chain(pending, identities)
        chunks: list[bytes] = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) != candidate.size or sha256(raw).hexdigest() != candidate.sha256:
        raise ToolExecutionError("候选文件内容已经变化。")
    after = os.lstat(path)
    _verify_file(after)
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ToolExecutionError("候选文件在读取期间发生变化。")
    return raw


def discard_candidate(candidate: PendingCandidate) -> None:
    """成功提交后仅删除仍与候选记录完全一致的普通单链接文件。"""
    path = _candidate_path(candidate.pending_file_id)
    info = os.lstat(path)
    _verify_file(info)
    if info.st_size != candidate.size:
        raise ToolExecutionError("候选文件状态已经变化。")
    raw = load_candidate(candidate)
    if sha256(raw).hexdigest() != candidate.sha256:
        raise ToolExecutionError("候选文件内容已经变化。")
    current = os.lstat(path)
    if (current.st_dev, current.st_ino, current.st_mtime_ns) != (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
    ):
        raise ToolExecutionError("候选文件在清理前发生变化。")
    path.unlink()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone aware")
    return parsed.astimezone(timezone.utc)


def store_manifest(
    candidate: PendingCandidate,
    *,
    path: str,
    source_tool: str,
    operation: str,
    encoding: str,
    original_size: int | None,
    original_sha256: str | None,
    clock: Callable[[], datetime] | None = None,
) -> PendingChangeManifest:
    """为已写入的候选独占创建严格清单；失败时不会伪造可恢复条目。"""
    now_factory = clock if clock is not None else _utc_now
    now = now_factory()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ToolExecutionError("候选时钟状态无效。")
    created = now.astimezone(timezone.utc)
    manifest = PendingChangeManifest(
        pending_file_id=candidate.pending_file_id,
        path=path,
        source_tool=source_tool,
        operation=operation,
        encoding=encoding,
        original_exists=original_size is not None,
        original_size=original_size,
        original_sha256=original_sha256,
        candidate_size=candidate.size,
        candidate_sha256=candidate.sha256,
        created_at=created.isoformat(),
        expires_at=(created + _RETENTION).isoformat(),
    )
    raw = manifest.model_dump_json().encode("utf-8")
    if len(raw) > _MANIFEST_MAX_BYTES:
        raise ToolExecutionError("候选清单超过大小限制。")
    with _PENDING_LOCK:
        cleanup_expired_pending(now=created)
        _write_exclusive(_manifest_path(candidate.pending_file_id), raw)
        # 创建后重新从安全句柄解析，确保磁盘内容是同一份严格清单。
        loaded = load_manifest(candidate.pending_file_id, now=created, cleanup=False)
        if loaded != manifest:
            raise ToolExecutionError("候选清单写入后校验失败。")
    return manifest


def load_manifest(
    pending_file_id: str,
    *,
    now: datetime | None = None,
    cleanup: bool = True,
) -> PendingChangeManifest:
    """安全读取严格清单，并拒绝过期、标识不符或非规范 JSON。"""
    current = (now if now is not None else _utc_now()).astimezone(timezone.utc)
    with _PENDING_LOCK:
        if cleanup:
            cleanup_expired_pending(now=current)
        raw, _ = _read_safe_raw(_manifest_path(pending_file_id), maximum=_MANIFEST_MAX_BYTES)
        try:
            manifest = PendingChangeManifest.model_validate_json(raw)
        except (ValidationError, ValueError, UnicodeError) as exc:
            raise ToolExecutionError("候选清单无效。") from exc
        if manifest.pending_file_id != pending_file_id:
            raise ToolExecutionError("候选清单标识不一致。")
        if current >= _parse_timestamp(manifest.expires_at):
            raise ToolExecutionError("候选已经过期。")
        return manifest


def load_pending_change(
    pending_file_id: str,
    *,
    now: datetime | None = None,
) -> StoredPendingChange:
    """同时验证清单与 ``.bin`` 的大小、哈希、标识和保留期。"""
    with _PENDING_LOCK:
        manifest = load_manifest(pending_file_id, now=now)
        candidate = PendingCandidate(
            pending_file_id=manifest.pending_file_id,
            size=manifest.candidate_size,
            sha256=manifest.candidate_sha256,
        )
        raw = load_candidate(candidate)
        return StoredPendingChange(manifest=manifest, candidate=candidate, raw=raw)


def list_pending_entries(
    max_results: int,
    *,
    now: datetime | None = None,
) -> tuple[list[PendingChangeEntry], bool]:
    """列出完整且未过期的候选；损坏、孤立或不安全条目不会暴露。"""
    current = (now if now is not None else _utc_now()).astimezone(timezone.utc)
    with _PENDING_LOCK:
        pending, _ = _pending_root()
        cleanup_expired_pending(now=current)
        identifiers = sorted(
            (
                match.group(1)
                for entry in os.scandir(pending)
                if (match := re.fullmatch(r"([0-9a-f]{32})\.json", entry.name))
            )
        )
        manifests: list[PendingChangeManifest] = []
        for identifier in identifiers:
            try:
                stored = load_pending_change(identifier, now=current)
            except (OSError, ToolAccessDenied, ToolExecutionError):
                continue
            manifests.append(stored.manifest)
        manifests.sort(key=lambda item: (item.created_at, item.pending_file_id), reverse=True)
        truncated = len(manifests) > max_results
        entries = [
            PendingChangeEntry(
                pending_file_id=item.pending_file_id,
                path=item.path,
                source_tool=item.source_tool,
                operation=item.operation,
                encoding=item.encoding,
                candidate_size=item.candidate_size,
                created_at=item.created_at,
                expires_at=item.expires_at,
            )
            for item in manifests[:max_results]
        ]
        return entries, truncated


def _safe_mtime_expired(path: Path, cutoff: datetime) -> bool:
    info = os.lstat(path)
    _verify_file(info)
    modified = datetime.fromtimestamp(info.st_mtime, timezone.utc)
    return modified <= cutoff


def _cleanup_expired_pending_locked(*, now: datetime | None = None) -> None:
    """在 ``_PENDING_LOCK`` 内清理过期候选与孤儿。"""
    current = (now if now is not None else _utc_now()).astimezone(timezone.utc)
    cutoff = current - _RETENTION
    pending, _ = _pending_root()
    grouped: dict[str, dict[str, Path]] = {}
    for entry in os.scandir(pending):
        match = _PENDING_FILE_NAME.fullmatch(entry.name)
        if match is not None:
            grouped.setdefault(match.group(1), {})[match.group(2)] = pending / entry.name

    for identifier, paths in grouped.items():
        expired = False
        manifest_path = paths.get("json")
        if manifest_path is not None:
            try:
                raw, _ = _read_safe_raw(manifest_path, maximum=_MANIFEST_MAX_BYTES)
                manifest = PendingChangeManifest.model_validate_json(raw)
                expired = (
                    manifest.pending_file_id == identifier
                    and current >= _parse_timestamp(manifest.expires_at)
                )
            except (OSError, ValidationError, ValueError, ToolAccessDenied, ToolExecutionError):
                try:
                    expired = _safe_mtime_expired(manifest_path, cutoff)
                except (OSError, ToolAccessDenied, ToolExecutionError):
                    expired = False
        else:
            candidate_path = paths.get("bin")
            if candidate_path is not None:
                try:
                    expired = _safe_mtime_expired(candidate_path, cutoff)
                except (OSError, ToolAccessDenied, ToolExecutionError):
                    expired = False
        if not expired:
            continue
        for suffix in ("bin", "json"):
            path = paths.get(suffix)
            if path is None:
                continue
            try:
                _unlink_safe(path)
            except (OSError, ToolAccessDenied, ToolExecutionError):
                continue


def cleanup_expired_pending(*, now: datetime | None = None) -> None:
    """按七日窗口清理精确命名的过期候选与孤儿，逐项 fail-safe。"""
    with _PENDING_LOCK:
        _cleanup_expired_pending_locked(now=now)


def discard_pending_change(stored: StoredPendingChange) -> None:
    """成功应用后清理仍匹配同一清单和内容的候选文件对。"""
    with _PENDING_LOCK:
        current = load_pending_change(stored.manifest.pending_file_id)
        if current.manifest != stored.manifest or current.raw != stored.raw:
            raise ToolExecutionError("候选在清理前发生变化。")
        discard_candidate(current.candidate)
        manifest_path = _manifest_path(current.manifest.pending_file_id)
        manifest_raw, manifest_info = _read_safe_raw(
            manifest_path, maximum=_MANIFEST_MAX_BYTES
        )
        if PendingChangeManifest.model_validate_json(manifest_raw) != current.manifest:
            raise ToolExecutionError("候选清单在清理前发生变化。")
        _unlink_safe(manifest_path, manifest_info)


def _terminal_safe(text: str) -> str:
    # 保留正常换行，但不能让候选内容中的裸 CR 把终端光标移回行首、覆盖
    # 已显示的审批提示。先把常见 CRLF 归一化，再转义剩余控制字符。
    text = text.replace("\r\n", "\n")
    pieces: list[str] = []
    for character in text:
        if character in "\n\t":
            pieces.append(character)
        elif unicodedata.category(character) in {"Cc", "Cf"}:
            codepoint = ord(character)
            pieces.append(
                f"\\x{codepoint:02x}" if codepoint <= 0xFF else f"\\u{codepoint:04x}"
            )
        else:
            pieces.append(character)
    return "".join(pieces)


def unified_diff_preview(before: str, after: str) -> str:
    """返回适合终端展示的有界 diff；候选文件本身不会被截断。"""
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="before",
            tofile="after",
            n=3,
        )
    )
    preview_parts: list[str] = []
    characters = 0
    truncated = False
    for line in lines:
        safe_line = _terminal_safe(line)
        if len(preview_parts) >= MAX_DIFF_LINES or characters + len(safe_line) > MAX_DIFF_CHARACTERS:
            truncated = True
            break
        preview_parts.append(safe_line)
        characters += len(safe_line)
    preview = "".join(preview_parts)
    if truncated:
        preview += "\n... [diff 预览已截断；data/pending 中的候选文件保持完整] ...\n"
    return preview or "--- before\n+++ after\n@@ 内容字节已变化 @@\n"


def combined_unified_diff_preview(changes: tuple[tuple[str, str, str], ...]) -> str:
    """Build one stable, globally bounded preview for a multi-file candidate."""

    preview_parts: list[str] = []
    characters = 0
    lines_used = 0
    truncated = False
    for path, before, after in changes:
        raw_lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
        emitted = False
        for line in raw_lines:
            emitted = True
            safe_line = _terminal_safe(line)
            if lines_used >= MAX_DIFF_LINES or characters + len(safe_line) > MAX_DIFF_CHARACTERS:
                truncated = True
                break
            preview_parts.append(safe_line)
            characters += len(safe_line)
            lines_used += 1
        if truncated:
            break
        if not emitted:
            marker = f"--- a/{path}\n+++ b/{path}\n@@ 内容字节已变化 @@\n"
            if lines_used + 3 > MAX_DIFF_LINES or characters + len(marker) > MAX_DIFF_CHARACTERS:
                truncated = True
                break
            preview_parts.append(marker)
            characters += len(marker)
            lines_used += 3
    preview = "".join(preview_parts)
    if truncated:
        marker = "\n... [diff 预览已截断；远端内存候选保持完整] ...\n"
        preview = preview[: max(0, MAX_DIFF_CHARACTERS - len(marker))] + marker
    return preview
