"""只读检索和一次确认应用已保留的文件候选。"""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from typing import BinaryIO

from app.schemas import (
    ApplyPendingChangeArguments,
    ApplyPendingChangeOutput,
    ListPendingChangesArguments,
    ListPendingChangesOutput,
    PendingChangeManifest,
)
from app.workflow import ConfirmationCallback, FileApprovalWorkflow, WorkflowApprovalDenied
from tool import file_access
from tool.file_access import (
    ToolExecutionError,
    ToolWriteRecoveryError,
    create_project_file_with_parents,
    open_project_file,
    resolve_project_new_file_with_parents,
    verify_open_file,
)
from tool.pending_changes import (
    StoredPendingChange,
    discard_pending_change,
    list_pending_entries,
    load_pending_change,
    unified_diff_preview,
)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size + 1
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != size:
        raise ToolExecutionError("正式文件状态已经变化。")
    return raw


def _write_all(stream: BinaryIO, raw: bytes) -> None:
    view = memoryview(raw)
    offset = 0
    while offset < len(view):
        count = stream.write(view[offset:])
        if count is None or count <= 0:
            raise OSError("文件写入未取得进展")
        offset += count


def _replace(stream: BinaryIO, raw: bytes) -> None:
    stream.seek(0)
    _write_all(stream, raw)
    stream.truncate(len(raw))
    stream.flush()
    os.fsync(stream.fileno())


def _restore(stream: BinaryIO, path: Path, original: bytes) -> bool:
    try:
        verify_open_file(stream, path, writable=True)
        _replace(stream, original)
        verify_open_file(stream, path, writable=True)
        stream.seek(0)
        return _read_exact(stream, len(original)) == original
    except Exception:
        return False


def _normalized_new_path(path: str) -> str:
    target = resolve_project_new_file_with_parents(path)
    return file_access.display_host_path(target)


def _read_original(manifest: PendingChangeManifest) -> bytes:
    if not manifest.original_exists:
        if _normalized_new_path(manifest.path) != manifest.path:
            raise ToolExecutionError("候选目标路径已经变化。")
        return b""
    assert manifest.original_size is not None and manifest.original_sha256 is not None
    with open_project_file(manifest.path, writable=False) as opened:
        stream = opened.stream
        actual_path, info = verify_open_file(stream, opened.path, writable=False)
        if opened.relative_path != manifest.path or info.st_size != manifest.original_size:
            raise ToolExecutionError("正式文件已不再是生成候选时的状态。")
        original = _read_exact(stream, info.st_size)
        if sha256(original).hexdigest() != manifest.original_sha256:
            raise ToolExecutionError("正式文件已不再是生成候选时的状态。")
        verify_open_file(stream, actual_path, writable=False)
        stream.seek(0)
        if _read_exact(stream, info.st_size) != original:
            raise ToolExecutionError("正式文件在复核期间发生变化。")
        return original


def _verify_stored(expected: StoredPendingChange) -> StoredPendingChange:
    current = load_pending_change(expected.manifest.pending_file_id)
    if current.manifest != expected.manifest or current.raw != expected.raw:
        raise ToolExecutionError("保留的候选或清单已经变化。")
    _read_original(current.manifest)
    return current


def _decode_preview(raw: bytes, encoding: str) -> str:
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ToolExecutionError("候选不再是声明编码的文本。") from exc


def _preview(stored: StoredPendingChange) -> str:
    original = _read_original(stored.manifest)
    # write_file 的首次 overwrite 预览允许原文件不是 UTF-8，并用可逆的
    # backslash 转义展示无效字节；恢复预览必须保持相同语义。候选内容仍按
    # manifest.encoding 严格解码，不能把被篡改候选伪装为文本。
    before = original.decode("utf-8-sig", errors="backslashreplace") if original else ""
    after = _decode_preview(stored.raw, stored.manifest.encoding)
    return unified_diff_preview(before, after)


def _output(stored: StoredPendingChange) -> ApplyPendingChangeOutput:
    manifest = stored.manifest
    operation = (
        "edited"
        if manifest.source_tool == "editor_file"
        else ("created" if manifest.operation == "write_create" else "overwritten")
    )
    return ApplyPendingChangeOutput(
        pending_file_id=manifest.pending_file_id,
        path=manifest.path,
        source_tool=manifest.source_tool,
        operation=operation,
        encoding=manifest.encoding,
        bytes_written=len(stored.raw),
        sha256=sha256(stored.raw).hexdigest(),
    )


def _commit_new(stored: StoredPendingChange) -> ApplyPendingChangeOutput:
    manifest = stored.manifest
    if manifest.operation != "write_create" or manifest.original_exists:
        raise ToolExecutionError("候选创建操作元数据无效。")
    with create_project_file_with_parents(manifest.path) as opened:
        verify_open_file(opened.stream, opened.path, writable=True)
        _replace(opened.stream, stored.raw)
        _, info = verify_open_file(opened.stream, opened.path, writable=True)
        opened.stream.seek(0)
        if info.st_size != len(stored.raw) or _read_exact(opened.stream, info.st_size) != stored.raw:
            raise ToolExecutionError("候选写入后的内容校验失败。")
    return _output(stored)


def _commit_existing(stored: StoredPendingChange) -> ApplyPendingChangeOutput:
    manifest = stored.manifest
    assert manifest.original_size is not None and manifest.original_sha256 is not None
    with open_project_file(manifest.path, writable=True) as opened:
        stream = opened.stream
        actual_path, info = verify_open_file(stream, opened.path, writable=True)
        if opened.relative_path != manifest.path or info.st_size != manifest.original_size:
            raise ToolExecutionError("正式文件已不再是生成候选时的状态。")
        original = _read_exact(stream, info.st_size)
        if sha256(original).hexdigest() != manifest.original_sha256:
            raise ToolExecutionError("正式文件已不再是生成候选时的状态。")
        verify_open_file(stream, actual_path, writable=True)
        stream.seek(0)
        if _read_exact(stream, len(original)) != original:
            raise ToolExecutionError("正式文件在写入前发生变化。")

        started = False
        try:
            started = True
            _replace(stream, stored.raw)
            _, after = verify_open_file(stream, actual_path, writable=True)
            stream.seek(0)
            if after.st_size != len(stored.raw) or _read_exact(stream, after.st_size) != stored.raw:
                raise ToolExecutionError("候选写入后的内容校验失败。")
        except Exception as exc:
            if started and not _restore(stream, actual_path, original):
                raise ToolWriteRecoveryError("应用候选失败，且无法确认原始内容已恢复。") from exc
            raise ToolExecutionError("应用候选失败，原始内容已恢复。") from exc
    return _output(stored)


def _commit(stored: StoredPendingChange) -> ApplyPendingChangeOutput:
    return _commit_new(stored) if not stored.manifest.original_exists else _commit_existing(stored)


def list_pending_changes(arguments: ListPendingChangesArguments) -> ListPendingChangesOutput:
    arguments = ListPendingChangesArguments.model_validate(arguments.model_dump())
    entries, truncated = list_pending_entries(arguments.max_results)
    return ListPendingChangesOutput(
        entries=entries,
        count=len(entries),
        truncated=truncated,
    )


def apply_pending_change(
    arguments: ApplyPendingChangeArguments,
    confirmation_callback: ConfirmationCallback | None = None,
    workflow: FileApprovalWorkflow | None = None,
) -> ApplyPendingChangeOutput:
    """对既有候选只展示一次 diff；明确批准后写入并安全清理候选对。"""
    arguments = ApplyPendingChangeArguments.model_validate(arguments.model_dump())
    if workflow is None:
        raise WorkflowApprovalDenied("恢复候选缺少审批状态机。")
    stored = load_pending_change(arguments.pending_file_id)
    preview = _preview(stored)

    return workflow.run_recovery_diff_approved(
        tool_name="apply_pending_change",
        operation=stored.manifest.operation,
        paths=(stored.manifest.path,),
        pending_file_id=stored.manifest.pending_file_id,
        diff_preview=preview,
        confirmation_callback=confirmation_callback,
        verify_before_commit=lambda: _verify_stored(stored),
        action=lambda: _commit(_verify_stored(stored)),
        discard_after_commit=lambda: discard_pending_change(stored),
    )
