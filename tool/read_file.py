"""有项目目录边界的分段文件读取；不执行文件中的任何内容。"""

import base64
import os
from typing import BinaryIO

from app.schemas import ReadFileArguments, ReadFileOutput
from tool.file_access import ToolAccessDenied, ToolExecutionError, open_project_file


def _read_at_most(stream: BinaryIO, maximum: int) -> bytes:
    """读取最多 maximum 字节，并正确处理底层短读。"""
    chunks: list[bytes] = []
    remaining = maximum
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stable_file_state(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def read_file(arguments: ReadFileArguments) -> ReadFileOutput:
    """读取最多 max_bytes 个字节；敏感路径由统一策略拒绝。"""
    arguments = ReadFileArguments.model_validate(arguments.model_dump())

    with open_project_file(arguments.path) as opened:
        stream = opened.stream
        before = os.fstat(stream.fileno())
        stream.seek(arguments.offset)
        raw = _read_at_most(stream, arguments.max_bytes)
        after = os.fstat(stream.fileno())
        if _stable_file_state(before) != _stable_file_state(after):
            raise ToolExecutionError("文件在读取期间发生变化，请重试。")
        expected = min(arguments.max_bytes, max(0, before.st_size - arguments.offset))
        if len(raw) != expected:
            raise ToolExecutionError("文件在读取期间发生变化，请重试。")

    try:
        content = raw.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(raw).decode("ascii")
        encoding = "base64"
    return ReadFileOutput(
        path=opened.relative_path,
        encoding=encoding,
        content=content,
        offset=arguments.offset,
        bytes_read=len(raw),
        total_bytes=before.st_size,
        truncated=arguments.offset + len(raw) < before.st_size,
    )
