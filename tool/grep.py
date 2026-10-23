"""在项目内 UTF-8 普通文件中执行有界的单行字面量搜索。"""

from __future__ import annotations

import os
from typing import BinaryIO

from app.schemas import GrepArguments, GrepMatch, GrepOutput
from tool.file_access import ToolAccessDenied, ToolExecutionError, open_project_file
from tool.project_scan import scan_project_files


MAX_SEARCHABLE_FILE_BYTES = 2 * 1024 * 1024
MAX_EXCERPT_CHARACTERS = 500


def _read_at_most(stream: BinaryIO, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stable_state(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _positions(line: str, pattern: str, case_sensitive: bool) -> list[int]:
    """返回原始字符串中的字符下标，大小写不敏感时保留 Unicode 列映射。"""
    if case_sensitive:
        positions: list[int] = []
        start = 0
        while True:
            position = line.find(pattern, start)
            if position < 0:
                return positions
            positions.append(position)
            start = position + max(1, len(pattern))

    folded_parts: list[str] = []
    original_indexes: list[int] = []
    for index, character in enumerate(line):
        folded = character.casefold()
        folded_parts.append(folded)
        original_indexes.extend([index] * len(folded))
    folded_line = "".join(folded_parts)
    folded_pattern = pattern.casefold()
    positions = []
    start = 0
    while True:
        position = folded_line.find(folded_pattern, start)
        if position < 0:
            return positions
        original = original_indexes[position]
        if not positions or positions[-1] != original:
            positions.append(original)
        start = position + max(1, len(folded_pattern))


def _excerpt(line: str, position: int, pattern: str) -> str:
    if len(line) <= MAX_EXCERPT_CHARACTERS:
        return line
    half = MAX_EXCERPT_CHARACTERS // 2
    start = max(0, position - half)
    end = min(len(line), max(start + MAX_EXCERPT_CHARACTERS, position + len(pattern)))
    start = max(0, end - MAX_EXCERPT_CHARACTERS)
    return line[start:end]


def grep(arguments: GrepArguments) -> GrepOutput:
    """搜索字面量；过大、含 NUL 或无法解码为 UTF-8 的文件会被跳过。"""
    arguments = GrepArguments.model_validate(arguments.model_dump())
    scanned = scan_project_files(arguments.file_pattern)
    results: list[GrepMatch] = []
    files_searched = 0
    result_truncated = False

    for item in scanned.files:
        if item.size > MAX_SEARCHABLE_FILE_BYTES:
            continue
        try:
            with open_project_file(item.path) as opened:
                actual_path = opened.relative_path
                same_path = (
                    os.path.normcase(actual_path) == os.path.normcase(item.path)
                    if os.name == "nt"
                    else actual_path == item.path
                )
                if not same_path:
                    continue
                before = os.fstat(opened.stream.fileno())
                if before.st_size > MAX_SEARCHABLE_FILE_BYTES:
                    continue
                raw = _read_at_most(opened.stream, before.st_size + 1)
                after = os.fstat(opened.stream.fileno())
                if _stable_state(before) != _stable_state(after) or len(raw) != before.st_size:
                    continue
        except (ToolAccessDenied, ToolExecutionError, FileNotFoundError, PermissionError, OSError):
            continue
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue

        files_searched += 1
        for line_number, line in enumerate(text.splitlines(), start=1):
            for position in _positions(line, arguments.pattern, arguments.case_sensitive):
                if len(results) >= arguments.max_results:
                    result_truncated = True
                    break
                results.append(
                    GrepMatch(
                        path=item.path,
                        line_number=line_number,
                        column=position + 1,
                        text=_excerpt(line, position, arguments.pattern),
                    )
                )
            if result_truncated:
                break
        if result_truncated:
            break

    return GrepOutput(
        pattern=arguments.pattern,
        file_pattern=arguments.file_pattern,
        case_sensitive=arguments.case_sensitive,
        matches=results,
        count=len(results),
        files_searched=files_searched,
        truncated=scanned.truncated or result_truncated,
    )
