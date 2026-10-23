"""在项目目录边界内创建或完全覆盖 UTF-8 文本文件。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
from typing import BinaryIO

from app.schemas import WriteFileArguments, WriteFileOutput
from tool.file_access import (
    ToolExecutionError,
    ToolWriteRecoveryError,
    create_project_file_with_parents,
    open_project_file,
    resolve_project_new_file_with_parents,
    verify_open_file,
)
from tool.pending_changes import (
    PendingCandidate,
    discard_candidate,
    load_candidate,
    store_candidate,
    store_manifest,
    unified_diff_preview,
)


MAX_WRITABLE_FILE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class StagedWriteChange:
    candidate: PendingCandidate
    path: str
    mode: str
    original_size: int | None
    original_sha256: str | None


def _read_all(stream: BinaryIO, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size:
        raise ToolExecutionError("文件在写入期间发生变化。")
    return raw


def _write_all(stream: BinaryIO, raw: bytes) -> None:
    view = memoryview(raw)
    written = 0
    while written < len(view):
        count = stream.write(view[written:])
        if count is None or count <= 0:
            raise OSError("文件写入未取得进展")
        written += count


def _replace_stream_contents(stream: BinaryIO, raw: bytes) -> None:
    stream.seek(0)
    _write_all(stream, raw)
    stream.truncate(len(raw))
    stream.flush()
    os.fsync(stream.fileno())


def _restore_original(stream: BinaryIO, candidate: Path, original: bytes) -> bool:
    try:
        verify_open_file(stream, candidate, writable=True)
        _replace_stream_contents(stream, original)
        verify_open_file(stream, candidate, writable=True)
        stream.seek(0)
        return _read_all(stream, len(original)) == original
    except Exception:
        return False


def _verify_written(stream: BinaryIO, candidate: Path, expected: bytes) -> None:
    _, info = verify_open_file(stream, candidate, writable=True)
    if info.st_size != len(expected):
        raise ToolExecutionError("写入后的文件大小不一致。")
    stream.seek(0)
    if _read_all(stream, len(expected)) != expected:
        raise ToolExecutionError("写入后的内容校验失败。")


def _write_created(arguments: WriteFileArguments, raw: bytes) -> WriteFileOutput:
    with create_project_file_with_parents(arguments.path) as opened:
        stream = opened.stream
        verify_open_file(stream, opened.path, writable=True)
        _replace_stream_contents(stream, raw)
        _verify_written(stream, opened.path, raw)
        relative_path = opened.relative_path
    return WriteFileOutput(
        path=relative_path,
        encoding=arguments.encoding,
        operation="created",
        bytes_written=len(raw),
        sha256=sha256(raw).hexdigest(),
    )


def _write_overwritten(
    arguments: WriteFileArguments,
    raw: bytes,
    staged: StagedWriteChange,
) -> WriteFileOutput:
    with open_project_file(arguments.path, writable=True) as opened:
        stream = opened.stream
        actual_path, before = verify_open_file(stream, opened.path, writable=True)
        if before.st_size > MAX_WRITABLE_FILE_BYTES:
            raise ToolExecutionError("原文件超过可安全恢复的大小限制。")
        if before.st_size != staged.original_size:
            raise ToolExecutionError("文件在 diff 审批期间发生变化。")
        stream.seek(0)
        original = _read_all(stream, before.st_size)
        if sha256(original).hexdigest() != staged.original_sha256:
            raise ToolExecutionError("文件在 diff 审批期间发生变化。")
        stream.seek(0)
        if _read_all(stream, before.st_size) != original:
            raise ToolExecutionError("文件在覆盖前发生变化。")
        verify_open_file(stream, actual_path, writable=True)

        write_started = False
        try:
            stream.seek(0)
            write_started = True
            _write_all(stream, raw)
            stream.truncate(len(raw))
            stream.flush()
            os.fsync(stream.fileno())
            _verify_written(stream, actual_path, raw)
        except Exception as exc:
            if write_started and not _restore_original(stream, actual_path, original):
                raise ToolWriteRecoveryError("覆盖失败，且无法确认原始内容已恢复。") from exc
            raise ToolExecutionError("覆盖失败，原始内容已恢复。") from exc
        relative_path = opened.relative_path

    return WriteFileOutput(
        path=relative_path,
        encoding=arguments.encoding,
        operation="overwritten",
        bytes_written=len(raw),
        sha256=sha256(raw).hexdigest(),
    )


def prepare_write_file(
    arguments: WriteFileArguments,
    pending_file_id: str,
) -> tuple[StagedWriteChange, str]:
    """只保存完整候选并生成有界 diff，不创建父目录或修改正式目标。"""
    arguments = WriteFileArguments.model_validate(arguments.model_dump())
    raw = arguments.content.encode(arguments.encoding)
    if len(raw) > MAX_WRITABLE_FILE_BYTES:
        raise ToolExecutionError("写入内容超过大小限制。")
    original: bytes | None = None
    if arguments.mode == "create":
        from tool import file_access

        target = resolve_project_new_file_with_parents(arguments.path)
        normalized = file_access.display_host_path(target)
        before_text = ""
    else:
        with open_project_file(arguments.path, writable=False) as opened:
            stream = opened.stream
            actual_path, before = verify_open_file(stream, opened.path, writable=False)
            if before.st_size > MAX_WRITABLE_FILE_BYTES:
                raise ToolExecutionError("原文件超过可安全恢复的大小限制。")
            original = _read_all(stream, before.st_size)
            verify_open_file(stream, actual_path, writable=False)
            stream.seek(0)
            if _read_all(stream, before.st_size) != original:
                raise ToolExecutionError("文件在生成候选期间发生变化。")
            normalized = opened.relative_path
        before_text = original.decode("utf-8-sig", errors="backslashreplace")

    diff_preview = unified_diff_preview(before_text, arguments.content)
    candidate = store_candidate(pending_file_id, raw)
    try:
        store_manifest(
            candidate,
            path=normalized,
            source_tool="write_file",
            operation=f"write_{arguments.mode}",
            encoding=arguments.encoding,
            original_size=None if original is None else len(original),
            original_sha256=None if original is None else sha256(original).hexdigest(),
        )
    except Exception:
        try:
            discard_candidate(candidate)
        except Exception:
            pass
        raise
    staged = StagedWriteChange(
        candidate=candidate,
        path=normalized,
        mode=arguments.mode,
        original_size=None if original is None else len(original),
        original_sha256=None if original is None else sha256(original).hexdigest(),
    )
    return staged, diff_preview


def write_file(
    arguments: WriteFileArguments,
    staged: StagedWriteChange | None = None,
) -> WriteFileOutput:
    """提交已批准候选；create 仅在第二次批准后才创建父目录和文件。"""
    arguments = WriteFileArguments.model_validate(arguments.model_dump())
    if not isinstance(staged, StagedWriteChange) or staged.mode != arguments.mode:
        raise ToolExecutionError("写入缺少已批准的文件候选。")
    raw = load_candidate(staged.candidate)
    if raw != arguments.content.encode(arguments.encoding):
        raise ToolExecutionError("文件候选与写入请求不一致。")
    if len(raw) > MAX_WRITABLE_FILE_BYTES:
        raise ToolExecutionError("写入内容超过大小限制。")
    if arguments.mode == "create":
        return _write_created(arguments, raw)
    return _write_overwritten(arguments, raw, staged)
