"""工具调用的最小化、安全可观测性记录。

所有事件都写入项目固定 ``data/log`` 目录。每个分片都是合法 JSON 数组，
单个分片最多 2,000 条或 8 MiB；日志只包含固定字段和数量/状态类摘要，不序列化工具参数、
模型消息或工具输出。导入和构造本模块中的对象不会创建目录或文件。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
from threading import Lock
from typing import Literal
import uuid
from urllib.parse import urlsplit

from app.schemas import ToolResult


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIRECTORY = _PROJECT_ROOT / "data" / "log"
_LOG_FILE_PATTERN = re.compile(
    r"^tool-calls-(\d{4}-\d{2}-\d{2})(?:-([2-9][0-9]*))?\.json$"
)
_LEGACY_LOG_FILE_NAME = "tool_calls.jsonl"
_TOOL_NAMESPACE = uuid.UUID("4eacde5e-5238-4a2c-a24d-5fc4665dfd60")
_WRITE_LOCK = Lock()
_RETENTION_DAYS = 7
_MAX_LOG_FILE_BYTES = 8 * 1024 * 1024
_MAX_LOG_RECORDS = 2_000
_DEFAULT_PENDING_LIMIT = 512
_REGISTERED_TOOL_NAMES = frozenset(
    {
        "read_file", "editor_file", "list_files", "grep", "write_file",
        "list_pending_changes", "apply_pending_change", "bash_shell", "code_interpreter",
        "get_async_task", "cancel_async_task",
        "web_fetch", "web_search",
        "inspect_recovery", "resolve_recovery",
        "load_skill", "read_skill_resource",
    }
)


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具执行的稳定工具标识和随机调用标识。"""

    tool_name: str
    tool_uuid: str
    invocation_uuid: str


@dataclass(frozen=True)
class ObservabilityHealth:
    """不含日志正文或底层错误的有界运行状态。"""

    pending_events: int
    dropped_events: int
    healthy: bool


def tool_uuid(tool_name: str) -> str:
    """返回某一工具跨进程稳定的 UUIDv5。"""
    return str(uuid.uuid5(_TOOL_NAMESPACE, tool_name))


def _bounded_int(
    value: object, *, default: int, minimum: int, maximum: int
) -> int | Literal["invalid"]:
    candidate = default if value is None else value
    if type(candidate) is int and minimum <= candidate <= maximum:
        return candidate
    return "invalid"


def _strict_bool(value: object, *, default: bool) -> bool | Literal["invalid"]:
    candidate = default if value is None else value
    return candidate if type(candidate) is bool else "invalid"


def _input_summary(tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
    """从白名单字段生成摘要；路径、内容、代码和搜索词永不进入日志。"""
    summary: dict[str, object] = {"argument_count": len(arguments)}
    if tool_name == "read_file":
        summary.update(
            offset=_bounded_int(
                arguments.get("offset"), default=0, minimum=0, maximum=2**63 - 1
            ),
            max_bytes=_bounded_int(
                arguments.get("max_bytes"), default=65536, minimum=1, maximum=1048576
            ),
        )
    elif tool_name == "editor_file":
        operation = arguments.get("operation", "replace_text")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        summary.update(
            operation=(operation if operation in {
                "replace_text", "replace_lines", "insert_lines", "delete_lines"
            } else "invalid"),
            old_text_characters=len(old_text) if isinstance(old_text, str) else 0,
            new_text_characters=len(new_text) if isinstance(new_text, str) else 0,
            expected_replacements=_bounded_int(
                arguments.get("expected_replacements"), default=1, minimum=1, maximum=1000
            ),
            position=(
                arguments.get("position")
                if arguments.get("position") in {"before", "after"}
                else (None if arguments.get("position") is None else "invalid")
            ),
        )
    elif tool_name == "list_files":
        summary["max_results"] = _bounded_int(
            arguments.get("max_results"), default=200, minimum=1, maximum=1000
        )
    elif tool_name == "grep":
        summary.update(
            case_sensitive=_strict_bool(arguments.get("case_sensitive"), default=True),
            max_results=_bounded_int(
                arguments.get("max_results"), default=100, minimum=1, maximum=1000
            ),
        )
    elif tool_name == "write_file":
        content = arguments.get("content")
        mode = arguments.get("mode")
        encoding = arguments.get("encoding", "utf-8")
        summary.update(
            mode=mode if mode in {"create", "overwrite"} else "invalid",
            encoding=encoding if encoding in {"utf-8", "utf-8-sig"} else "invalid",
            content_characters=len(content) if isinstance(content, str) else 0,
        )
    elif tool_name == "list_pending_changes":
        summary["max_results"] = _bounded_int(
            arguments.get("max_results"), default=100, minimum=1, maximum=1000
        )
    elif tool_name == "apply_pending_change":
        # 候选 UUID、目标、清单和 diff 都不得进入日志。
        pass
    elif tool_name == "load_skill":
        # Skill 名称和正文不进入日志。
        pass
    elif tool_name == "read_skill_resource":
        # Skill 名称、token、路径和正文不进入日志。
        summary.update(
            offset=_bounded_int(
                arguments.get("offset"), default=0, minimum=0, maximum=4 * 1024 * 1024
            ),
            max_bytes=_bounded_int(
                arguments.get("max_bytes"), default=16384, minimum=1, maximum=65536
            ),
        )
    elif tool_name == "bash_shell":
        paths = arguments.get("paths")
        code = arguments.get("code")
        command = arguments.get("command")
        summary.update(
            command=(command if command in {
                "unittest", "compileall", "validate_json", "remove_file", "clear_file",
                "clear_directory", "python",
            } else "invalid"),
            path_count=len(paths) if isinstance(paths, list) else 0,
            code_characters=len(code) if isinstance(code, str) else 0,
            timeout_seconds=_bounded_int(
                arguments.get("timeout_seconds"), default=5, minimum=1, maximum=120
            ),
        )
    elif tool_name == "code_interpreter":
        code = arguments.get("code")
        summary.update(
            code_characters=len(code) if isinstance(code, str) else 0,
            timeout_seconds=_bounded_int(
                arguments.get("timeout_seconds"), default=5, minimum=1, maximum=10
            ),
        )
    elif tool_name == "web_fetch":
        raw_url = arguments.get("url")
        scheme = "invalid"
        host_valid = False
        if isinstance(raw_url, str):
            try:
                parsed = urlsplit(raw_url)
                if parsed.scheme in {"http", "https"}:
                    scheme = parsed.scheme
                host_valid = bool(parsed.hostname)
            except ValueError:
                pass
        summary.update(
            scheme=scheme,
            host_valid=host_valid,
            max_bytes=_bounded_int(
                arguments.get("max_bytes"), default=262144,
                minimum=1, maximum=4 * 1024 * 1024,
            ),
            timeout_seconds=_bounded_int(
                arguments.get("timeout_seconds"), default=15, minimum=1, maximum=60
            ),
        )
    elif tool_name == "web_search":
        query = arguments.get("query")
        freshness = arguments.get("freshness")
        country = arguments.get("country")
        language = arguments.get("search_lang")
        summary.update(
            query_characters=len(query) if isinstance(query, str) else 0,
            count=_bounded_int(arguments.get("count"), default=5, minimum=1, maximum=10),
            freshness=(freshness if freshness in {None, "pd", "pw", "pm", "py"} else "invalid"),
            country_valid=(country is None or (
                isinstance(country, str) and len(country) == 2
                and country.isascii() and country.isalpha()
            )),
            search_lang_valid=(language is None or (
                isinstance(language, str) and 2 <= len(language) <= 8
                and language.isascii()
                and re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?", language) is not None
            )),
            safesearch=(
                arguments.get("safesearch", "moderate")
                if arguments.get("safesearch", "moderate") in {"off", "moderate", "strict"}
                else "invalid"
            ),
        )
    return summary


def _output_summary(result: ToolResult) -> dict[str, object]:
    """只记录结构和计数，不记录正文、路径、哈希、错误或进程输出。"""
    summary: dict[str, object] = {"status": result.status}
    output = result.output
    if result.status != "success" or isinstance(output, str):
        return summary
    for field_name in (
        "bytes_read", "total_bytes", "truncated", "replacements", "bytes_before",
        "bytes_after", "count", "files_searched", "bytes_written", "operation",
        "exit_code",
    ):
        value = getattr(output, field_name, None)
        if isinstance(value, (bool, int)):
            summary[field_name] = value
        elif field_name == "operation" and value in {
            "replace_text", "replace_lines", "insert_lines", "delete_lines",
            "created", "overwritten",
        }:
            summary[field_name] = value
    for field_name in ("stdout", "stderr", "result"):
        value = getattr(output, field_name, None)
        if isinstance(value, str):
            summary[f"{field_name}_characters"] = len(value)
    entries = getattr(output, "entries", None)
    matches = getattr(output, "matches", None)
    results = getattr(output, "results", None)
    if isinstance(entries, list):
        summary["entry_count"] = len(entries)
    if isinstance(matches, list):
        summary["match_count"] = len(matches)
    if isinstance(results, list):
        # 搜索结果正文和 URL 永不写日志，只保留数量。
        summary["result_count"] = len(results)
    return summary


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _ensure_safe_log_directory() -> Path:
    """创建固定目录，并在每一层拒绝链接、重解析点和非目录对象。"""
    expected_data = _PROJECT_ROOT / "data"
    if _LOG_DIRECTORY.parent != expected_data or expected_data.parent != _PROJECT_ROOT:
        raise OSError("日志目录配置无效")
    for directory in (expected_data, _LOG_DIRECTORY):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        info = os.lstat(directory)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse_point(directory)
        ):
            raise OSError("日志目录状态不安全")
    return _LOG_DIRECTORY


def _safe_regular_file(path: Path, *, may_be_missing: bool) -> os.stat_result | None:
    """复核日志目录中的固定文件，拒绝链接、特殊文件和多重硬链接。"""
    if path.parent != _LOG_DIRECTORY:
        raise OSError("日志文件路径无效")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if may_be_missing:
            return None
        raise
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse_point(path)
        or info.st_nlink != 1
    ):
        raise OSError("日志文件状态不安全")
    return info


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode, left.st_nlink) == (
        right.st_dev, right.st_ino, right.st_mode, right.st_nlink
    )


def _reject_json_constant(_value: str) -> None:
    raise ValueError("日志 JSON 包含非标准数值")


def _read_records(path: Path, expected: os.stat_result | None) -> list[dict[str, object]]:
    if expected is None:
        return []
    if expected.st_size > _MAX_LOG_FILE_BYTES:
        raise OSError("日志文件超过大小限制")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = _safe_regular_file(path, may_be_missing=False)
        if current is None or not _same_identity(expected, opened) or not _same_identity(opened, current):
            raise OSError("日志文件身份发生变化")
        remaining = expected.st_size + 1
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != expected.st_size:
            raise OSError("日志文件读取期间发生变化")
    finally:
        os.close(descriptor)
    value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(value, list) or len(value) > _MAX_LOG_RECORDS:
        raise OSError("日志 JSON 结构无效")
    if any(not isinstance(item, dict) for item in value):
        raise OSError("日志 JSON 记录无效")
    return value


def _write_records(path: Path, records: list[dict[str, object]], expected: os.stat_result | None) -> None:
    payload = json.dumps(
        records, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(payload) > _MAX_LOG_FILE_BYTES or len(records) > _MAX_LOG_RECORDS:
        raise OSError("日志文件超过大小限制")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        current_temp = _safe_regular_file(temporary, may_be_missing=False)
        if current_temp is None or not _same_identity(opened, current_temp):
            raise OSError("临时日志文件身份不安全")
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("日志写入失败")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = _safe_regular_file(path, may_be_missing=True)
        if (expected is None) != (current is None) or (
            expected is not None and current is not None and not _same_identity(expected, current)
        ):
            raise OSError("日志文件在写入期间发生变化")
        os.replace(temporary, path)
        _safe_regular_file(path, may_be_missing=False)
    finally:
        try:
            temp_info = _safe_regular_file(temporary, may_be_missing=True)
            if temp_info is not None:
                temporary.unlink()
        except OSError:
            pass


def _remove_expired_logs(today: date) -> None:
    """仅删除本日志器命名且早于七日窗口的安全普通单链接文件。"""
    cutoff = today - timedelta(days=_RETENTION_DAYS - 1)
    for entry in os.scandir(_LOG_DIRECTORY):
        match = _LOG_FILE_PATTERN.fullmatch(entry.name)
        if match is None:
            continue
        try:
            log_day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if log_day >= cutoff:
            continue
        path = _LOG_DIRECTORY / entry.name
        try:
            before = _safe_regular_file(path, may_be_missing=False)
            current = _safe_regular_file(path, may_be_missing=False)
            if before is not None and current is not None and _same_identity(before, current):
                path.unlink()
        except OSError:
            continue

    # 升级前的固定 JSONL 文件没有日期后缀，只按其本地 mtime 进入同一保留窗口。
    legacy = _LOG_DIRECTORY / _LEGACY_LOG_FILE_NAME
    try:
        before = _safe_regular_file(legacy, may_be_missing=True)
        if before is None:
            return
        legacy_day = datetime.fromtimestamp(before.st_mtime, timezone.utc).astimezone().date()
        if legacy_day >= cutoff:
            return
        current = _safe_regular_file(legacy, may_be_missing=False)
        if current is not None and _same_identity(before, current):
            legacy.unlink()
    except (OSError, OverflowError, ValueError):
        return


def _append_record(record: dict[str, object], local_day: date) -> None:
    _ensure_safe_log_directory()
    _remove_expired_logs(local_day)
    shard = 1
    while shard <= 1_000_000:
        suffix = "" if shard == 1 else f"-{shard}"
        log_file = _LOG_DIRECTORY / f"tool-calls-{local_day.isoformat()}{suffix}.json"
        if _LOG_FILE_PATTERN.fullmatch(log_file.name) is None:
            raise OSError("日志文件名无效")
        # 只有通过完整安全检查且因条数/字节阈值正常封存的分片才能进入下一编号。
        # 损坏或状态不安全时向上报告，让 Observability fail-soft 地停止本次落盘；
        # 不能把“不可信旧文件”误当成一个已经正常轮转的分片。
        before = _safe_regular_file(log_file, may_be_missing=True)
        records = _read_records(log_file, before)
        candidate = [*records, record]
        payload_size = len(
            json.dumps(
                candidate,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        if len(candidate) <= _MAX_LOG_RECORDS and payload_size <= _MAX_LOG_FILE_BYTES:
            _write_records(log_file, candidate, before)
            return
        shard += 1
    raise OSError("当日日志分片数量超过限制")


class Observability:
    """写入按日 JSON 文件；失败时有界缓冲且不影响 Agent 执行。"""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        pending_limit: int = _DEFAULT_PENDING_LIMIT,
    ) -> None:
        if isinstance(pending_limit, bool) or not isinstance(pending_limit, int):
            raise TypeError("pending_limit 必须是整数")
        if pending_limit <= 0:
            raise ValueError("pending_limit 必须大于 0")
        self._clock = clock if clock is not None else lambda: datetime.now().astimezone()
        self._pending_limit = pending_limit
        self._pending: deque[tuple[dict[str, object], date]] = deque()
        self._dropped_count = 0

    @property
    def pending_count(self) -> int:
        with _WRITE_LOCK:
            return len(self._pending)

    @property
    def dropped_count(self) -> int:
        with _WRITE_LOCK:
            return self._dropped_count

    @property
    def healthy(self) -> bool:
        with _WRITE_LOCK:
            return not self._pending and self._dropped_count == 0

    @property
    def health(self) -> ObservabilityHealth:
        """返回仅包含计数和布尔值的安全状态快照。"""
        with _WRITE_LOCK:
            return ObservabilityHealth(
                pending_events=len(self._pending),
                dropped_events=self._dropped_count,
                healthy=not self._pending and self._dropped_count == 0,
            )

    def _flush_pending_locked(self) -> int:
        flushed = 0
        while self._pending:
            record, local_day = self._pending[0]
            try:
                _append_record(record, local_day)
            except Exception:
                break
            self._pending.popleft()
            flushed += 1
        return flushed

    def flush_pending(self) -> int:
        """按原顺序尽力补写内存事件，返回本次成功写入数量。"""
        with _WRITE_LOCK:
            return self._flush_pending_locked()

    def cleanup_expired_logs(self) -> bool:
        """尽力执行日志保留维护；失败时静默返回 ``False``。"""
        try:
            now = self._clock()
            if not isinstance(now, datetime):
                raise TypeError("clock 必须返回 datetime")
            local_day = now.astimezone().date()
            with _WRITE_LOCK:
                _ensure_safe_log_directory()
                _remove_expired_logs(local_day)
        except Exception:
            return False
        return True

    def _queue_locked(self, record: dict[str, object], local_day: date) -> None:
        if len(self._pending) >= self._pending_limit:
            # 保留更早事件的顺序；容量耗尽时明确丢弃最新事件。
            self._dropped_count += 1
            return
        self._pending.append((record, local_day))

    def start_tool(self, tool_name: str, arguments: Mapping[str, object]) -> ToolInvocation:
        safe_name = tool_name if tool_name in _REGISTERED_TOOL_NAMES else "unregistered"
        invocation = ToolInvocation(
            tool_name=safe_name,
            tool_uuid=tool_uuid(safe_name),
            invocation_uuid=str(uuid.uuid4()),
        )
        try:
            summary = _input_summary(safe_name, arguments)
        except Exception:
            summary = {"summary_unavailable": True}
        self._emit(invocation, "tool_call", summary)
        return invocation

    def finish_tool(self, invocation: ToolInvocation, result: ToolResult) -> None:
        try:
            summary = _output_summary(result)
        except Exception:
            summary = {"summary_unavailable": True}
        self._emit(invocation, "tool_result", summary)

    def _emit(
        self,
        invocation: ToolInvocation,
        event: Literal["tool_call", "tool_result"],
        summary: Mapping[str, object],
    ) -> None:
        try:
            now = self._clock()
            if not isinstance(now, datetime):
                raise TypeError("clock 必须返回 datetime")
            local_now = now.astimezone()
            record = {
                "timestamp": local_now.astimezone(timezone.utc).isoformat(),
                "event": event,
                "tool_name": invocation.tool_name,
                "tool_uuid": invocation.tool_uuid,
                "invocation_uuid": invocation.invocation_uuid,
                "summary": dict(summary),
            }
            with _WRITE_LOCK:
                self._flush_pending_locked()
                if self._pending:
                    self._queue_locked(record, local_now.date())
                    return
                try:
                    _append_record(record, local_now.date())
                except Exception:
                    self._queue_locked(record, local_now.date())
        except Exception:
            return
