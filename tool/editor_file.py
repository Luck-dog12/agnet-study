"""在项目目录边界内，对已有 UTF-8 文本文件执行精确或行级编辑。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
from typing import BinaryIO

from app.schemas import EditorFileArguments, EditorFileOutput
from tool.file_access import (
    ToolExecutionError,
    ToolWriteRecoveryError,
    open_project_file,
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


MAX_EDITABLE_FILE_BYTES = 8 * 1024 * 1024
_UTF8_BOM = b"\xef\xbb\xbf"
_LINE_ENDING_PATTERN = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class StagedEditorChange:
    candidate: PendingCandidate
    path: str
    encoding: str
    operation: str
    replacements: int
    original_size: int
    original_sha256: str


def _read_limited(stream: BinaryIO, limit: int) -> bytes:
    """读取到 EOF 或 limit；兼容底层一次只返回部分字节的句柄。"""
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all(stream: BinaryIO, raw: bytes) -> None:
    """处理短写；零字节写入视为失败。"""
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


def _read_current_bytes(stream: BinaryIO, expected_size: int) -> bytes:
    stream.seek(0)
    raw = _read_limited(stream, expected_size + 1)
    if len(raw) != expected_size:
        raise ToolExecutionError("文件在编辑期间发生变化。")
    return raw


def _restore_original(stream: BinaryIO, candidate: Path, original: bytes) -> bool:
    """只在同一已验证句柄上恢复，并通过重新读取确认恢复结果。"""
    try:
        verify_open_file(stream, candidate, writable=True)
        _replace_stream_contents(stream, original)
        verify_open_file(stream, candidate, writable=True)
        return _read_current_bytes(stream, len(original)) == original
    except Exception:
        return False


def _split_preserved_lines(text: str) -> list[tuple[str, str]]:
    """拆出行内容和原换行符；不制造文件末尾的虚拟空行。"""
    lines: list[tuple[str, str]] = []
    offset = 0
    for match in _LINE_ENDING_PATTERN.finditer(text):
        lines.append((text[offset:match.start()], match.group(0)))
        offset = match.end()
    if offset < len(text):
        lines.append((text[offset:], ""))
    return lines


def _preferred_newline(lines: list[tuple[str, str]]) -> str:
    """选择最常见的换行；数量相同时使用文件中最先出现的样式。"""
    endings = [ending for _, ending in lines if ending]
    if not endings:
        return "\n"
    counts = {ending: endings.count(ending) for ending in set(endings)}
    maximum = max(counts.values())
    return next(ending for ending in endings if counts[ending] == maximum)


def _normalize_newlines(value: str, newline: str) -> str:
    return _LINE_ENDING_PATTERN.sub(newline, value)


def _anchor_lines(old_text: str) -> list[str]:
    """将允许带或不带末尾换行的锚点转换为完整行内容。"""
    normalized = _normalize_newlines(old_text, "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    return normalized.split("\n")


def _find_line_matches(contents: list[str], anchor: list[str]) -> list[int]:
    """使用 KMP 在线性时间内查找所有行块匹配，包括重叠匹配。"""
    prefix = [0] * len(anchor)
    matched = 0
    for index in range(1, len(anchor)):
        while matched and anchor[index] != anchor[matched]:
            matched = prefix[matched - 1]
        if anchor[index] == anchor[matched]:
            matched += 1
        prefix[index] = matched

    starts: list[int] = []
    matched = 0
    for index, content in enumerate(contents):
        while matched and content != anchor[matched]:
            matched = prefix[matched - 1]
        if content == anchor[matched]:
            matched += 1
        if matched == len(anchor):
            starts.append(index - len(anchor) + 1)
            matched = prefix[matched - 1]
    return starts


def _fit_terminal_newline(value: str, newline: str, *, terminated: bool) -> str:
    """让完整行块自动继承所在位置是否以换行结束。"""
    normalized = _normalize_newlines(value, newline)
    if terminated:
        return normalized if normalized.endswith(newline) else normalized + newline
    return normalized[:-len(newline)] if normalized.endswith(newline) else normalized


def _apply_edit(arguments: EditorFileArguments, text: str) -> tuple[str, int]:
    """计算纯文本变更；行操作用完整行内容定位，不依赖易漂移的行号。"""
    lines = _split_preserved_lines(text)
    newline = _preferred_newline(lines)
    if arguments.operation == "replace_text":
        assert arguments.old_text is not None and arguments.new_text is not None
        old_text = _normalize_newlines(arguments.old_text, newline)
        new_text = _normalize_newlines(arguments.new_text, newline)
        matches = text.count(old_text)
        if matches != arguments.expected_replacements:
            raise ToolExecutionError("实际匹配次数与 expected_replacements 不一致。")
        replacement = text.replace(old_text, new_text)
        if replacement == text:
            raise ToolExecutionError("编辑没有产生任何内容变化。")
        return replacement, matches

    assert arguments.old_text is not None
    anchor = _anchor_lines(arguments.old_text)
    starts = _find_line_matches([content for content, _ in lines], anchor) if lines else []
    if len(starts) != arguments.expected_replacements:
        raise ToolExecutionError("实际行块匹配次数与 expected_replacements 不一致。")
    if any(current < previous + len(anchor) for previous, current in zip(starts, starts[1:])):
        raise ToolExecutionError("行锚点产生重叠匹配，无法安全编辑。")

    segments = [content + ending for content, ending in lines]
    for start_index in reversed(starts):
        end_index = start_index + len(anchor)
        if arguments.operation == "replace_lines":
            assert arguments.new_text is not None
            block = _fit_terminal_newline(
                arguments.new_text,
                newline,
                terminated=bool(lines[end_index - 1][1]),
            )
            segments[start_index:end_index] = [block]
        elif arguments.operation == "delete_lines":
            segments[start_index:end_index] = []
        else:
            assert arguments.operation == "insert_lines"
            assert arguments.new_text is not None and arguments.position is not None
            if arguments.position == "before":
                block = _fit_terminal_newline(arguments.new_text, newline, terminated=True)
                segments[start_index:start_index] = [block]
            else:
                anchor_terminated = bool(lines[end_index - 1][1])
                block = _fit_terminal_newline(
                    arguments.new_text,
                    newline,
                    terminated=anchor_terminated,
                )
                if not anchor_terminated:
                    block = newline + block
                segments[end_index:end_index] = [block]
    replacement = "".join(segments)
    if replacement == text:
        raise ToolExecutionError("行编辑没有产生任何内容变化。")
    return replacement, len(starts)


def prepare_editor_file(
    arguments: EditorFileArguments,
    pending_file_id: str,
) -> tuple[StagedEditorChange, str]:
    """只计算并保存候选和 diff；不会以写模式打开或修改正式文件。"""
    arguments = EditorFileArguments.model_validate(arguments.model_dump())
    with open_project_file(arguments.path, writable=False) as opened:
        stream = opened.stream
        actual_path, before_info = verify_open_file(stream, opened.path, writable=False)
        if before_info.st_size > MAX_EDITABLE_FILE_BYTES:
            raise ToolExecutionError("文件超过可编辑大小限制。")

        original = _read_current_bytes(stream, before_info.st_size)
        _, after_read_info = verify_open_file(stream, actual_path, writable=False)
        if after_read_info.st_size != before_info.st_size:
            raise ToolExecutionError("文件在编辑期间发生变化。")

        has_bom = original.startswith(_UTF8_BOM)
        body = original[len(_UTF8_BOM):] if has_bom else original
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("只能编辑 UTF-8 文本文件。") from exc
        if "\x00" in text:
            raise ToolExecutionError("只能编辑 UTF-8 文本文件。")

        replacement_text, changes = _apply_edit(arguments, text)
        replacement_body = replacement_text.encode("utf-8")
        replacement = (_UTF8_BOM if has_bom else b"") + replacement_body
        if len(replacement) > MAX_EDITABLE_FILE_BYTES:
            raise ToolExecutionError("编辑后的文件超过大小限制。")

        # 候选落盘前再次确认正式文件仍是用于生成 diff 的同一内容。
        verify_open_file(stream, actual_path, writable=False)
        if _read_current_bytes(stream, len(original)) != original:
            raise ToolExecutionError("文件在生成候选期间发生变化。")

    diff_preview = unified_diff_preview(text, replacement_text)
    candidate = store_candidate(pending_file_id, replacement)
    try:
        store_manifest(
            candidate,
            path=opened.relative_path,
            source_tool="editor_file",
            operation=f"edit_{arguments.operation}",
            encoding="utf-8-sig" if has_bom else "utf-8",
            original_size=len(original),
            original_sha256=sha256(original).hexdigest(),
        )
    except Exception:
        try:
            discard_candidate(candidate)
        except Exception:
            pass
        raise
    staged = StagedEditorChange(
        candidate=candidate,
        path=opened.relative_path,
        encoding="utf-8-sig" if has_bom else "utf-8",
        operation=arguments.operation,
        replacements=changes,
        original_size=len(original),
        original_sha256=sha256(original).hexdigest(),
    )
    return staged, diff_preview


def editor_file(
    arguments: EditorFileArguments,
    staged: StagedEditorChange | None = None,
) -> EditorFileOutput:
    """提交已通过 diff 审批的候选；缺少绑定候选时拒绝直接编辑。"""
    arguments = EditorFileArguments.model_validate(arguments.model_dump())
    if not isinstance(staged, StagedEditorChange):
        raise ToolExecutionError("编辑缺少已批准的文件候选。")
    replacement = load_candidate(staged.candidate)
    if (
        staged.operation != arguments.operation
        or staged.candidate.sha256 != sha256(replacement).hexdigest()
    ):
        raise ToolExecutionError("文件候选与编辑请求不一致。")

    with open_project_file(arguments.path, writable=True) as opened:
        stream = opened.stream
        actual_path, before_info = verify_open_file(stream, opened.path, writable=True)
        if before_info.st_size != staged.original_size:
            raise ToolExecutionError("文件在 diff 审批期间发生变化。")
        original = _read_current_bytes(stream, before_info.st_size)
        if sha256(original).hexdigest() != staged.original_sha256:
            raise ToolExecutionError("文件在 diff 审批期间发生变化。")
        has_bom = original.startswith(_UTF8_BOM)
        body = original[len(_UTF8_BOM):] if has_bom else original
        try:
            current_text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("文件在 diff 审批期间发生变化。") from exc
        expected_text, expected_changes = _apply_edit(arguments, current_text)
        expected = (_UTF8_BOM if has_bom else b"") + expected_text.encode("utf-8")
        if expected != replacement or expected_changes != staged.replacements:
            raise ToolExecutionError("文件候选与编辑请求不一致。")

        # 紧邻写入前再次读取同一句柄，避免基于已变化的旧内容覆盖文件。
        verify_open_file(stream, actual_path, writable=True)
        if _read_current_bytes(stream, len(original)) != original:
            raise ToolExecutionError("文件在编辑期间发生变化。")
        verify_open_file(stream, actual_path, writable=True)

        write_started = False
        try:
            stream.seek(0)
            write_started = True
            _write_all(stream, replacement)
            stream.truncate(len(replacement))
            stream.flush()
            os.fsync(stream.fileno())
            verify_open_file(stream, actual_path, writable=True)
            if _read_current_bytes(stream, len(replacement)) != replacement:
                raise ToolExecutionError("写入后的内容校验失败。")
        except Exception as exc:
            if write_started and not _restore_original(stream, actual_path, original):
                raise ToolWriteRecoveryError("编辑失败，且无法确认原始内容已恢复。") from exc
            raise ToolExecutionError("编辑失败，原始内容已恢复。") from exc

    return EditorFileOutput(
        path=opened.relative_path,
        encoding=staged.encoding,
        operation=arguments.operation,
        replacements=staged.replacements,
        bytes_before=len(original),
        bytes_after=len(replacement),
        sha256_before=staged.original_sha256,
        sha256_after=sha256(replacement).hexdigest(),
    )
