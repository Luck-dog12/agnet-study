"""未知工具结果的只读检查和 Agent 运行期恢复能力。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_hex
import stat
from threading import Lock
from typing import Callable

from app.schemas import (
    ApplyPendingChangeArguments, BashShellArguments, EditorFileArguments,
    ExternalStateInspection, InspectRecoveryArguments, InspectRecoveryOutput,
    PendingChangeManifest, ResolveRecoveryArguments, ResolveRecoveryOutput,
    ToolCallEventData, WriteFileArguments,
)
from app.workflow import _read_exact_file, _safe_regular_file
from tool import file_access


def _directory(path: Path) -> tuple[int, int, int]:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("不安全目录")
    return info.st_dev, info.st_ino, info.st_mode


def _inspect_path(path: str, *, directory: bool = False) -> tuple[str, int | None, str | None]:
    parts = file_access._split_allowed_path(path)
    root = file_access._PROJECT_ROOT
    identities = [(root, _directory(root))]
    target = root
    for index, part in enumerate(parts):
        target /= part
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            for parent, identity in identities:
                if _directory(parent) != identity:
                    raise OSError("路径发生变化")
            return "missing", None, None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("不安全路径")
        if index < len(parts) - 1 or directory:
            identities.append((target, _directory(target)))
    if directory:
        with os.scandir(target) as entries:
            empty = next(entries, None) is None
        result = ("empty" if empty else "nonempty", None, None)
    else:
        with file_access.open_project_file(path) as opened:
            before = os.fstat(opened.stream.fileno())
            if before.st_size > 8 * 1024 * 1024:
                raise OSError("检查文件超过上限")
            raw = opened.stream.read(before.st_size + 1)
            _, after = file_access.verify_open_file(opened.stream, opened.path, writable=False)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) or len(raw) != before.st_size:
                raise OSError("读取期间发生变化")
            result = ("file", len(raw), sha256(raw).hexdigest())
    for parent, identity in identities:
        if _directory(parent) != identity:
            raise OSError("路径发生变化")
    return result


def _manifest(identifier: str) -> PendingChangeManifest:
    # identifier 已通过 ApplyPendingChangeArguments 校验，不允许任意受保护路径。
    root = file_access._PROJECT_ROOT
    chain = [(p, _directory(p)) for p in (root, root / "data", root / "data/pending")]
    path = root / "data/pending" / f"{identifier}.json"
    info = _safe_regular_file(path, missing_ok=False)
    if info is None:
        raise OSError("候选清单不存在")
    manifest = PendingChangeManifest.model_validate_json(_read_exact_file(path, info, maximum=16384))
    if manifest.pending_file_id != identifier:
        raise OSError("清单标识不匹配")
    for parent, identity in chain:
        if _directory(parent) != identity:
            raise OSError("候选目录变化")
    return manifest


def inspect_external_state(call: ToolCallEventData) -> ExternalStateInspection:
    """给出当下可验证的事实；只读观察本身不会重放旧调用。"""
    state = "unknown"
    evidence = "此操作没有可用的自动后置条件；Agent 必须根据可靠轨迹审慎判断。"
    try:
        args = json.loads(call.call.arguments_json)
        expected_hash = None
        expected_empty = False
        expected_missing = False
        directory = False
        if call.call.name == "write_file":
            value = WriteFileArguments.model_validate(args)
            path = value.path
            expected_hash = sha256(value.content.encode(value.encoding)).hexdigest()
        elif call.call.name == "editor_file":
            path = EditorFileArguments.model_validate(args).path
        elif call.call.name == "apply_pending_change":
            value = ApplyPendingChangeArguments.model_validate(args)
            manifest = _manifest(value.pending_file_id)
            path = manifest.path
            expected_hash = manifest.candidate_sha256
        elif call.call.name == "bash_shell":
            value = BashShellArguments.model_validate(args)
            if value.command not in {"remove_file", "clear_file", "clear_directory"} or len(value.paths) != 1:
                raise ValueError("没有可自动判定的文件后置条件")
            path = value.paths[0]
            directory = value.command == "clear_directory"
            expected_empty = value.command in {"clear_file", "clear_directory"}
            expected_missing = value.command == "remove_file"
        else:
            raise ValueError("没有自动检查器")
        observed, size, digest = _inspect_path(path, directory=directory)
        if observed == "missing":
            state = "matches_requested" if expected_missing else "missing"
            evidence = "当前目标不存在；这不能证明是上次调用删除或从未创建。"
        else:
            matches = (expected_hash is not None and digest == expected_hash) or (
                expected_empty and (observed == "empty" or size == 0)
            )
            has_expectation = expected_hash is not None or expected_empty or expected_missing
            state = "matches_requested" if matches else ("different" if has_expectation else "unknown")
            evidence = (
                f"当前文件 bytes={size}，sha256={digest}。"
                if digest is not None else f"当前目录状态={observed}。"
            ) + "当前状态不等同于上次调用的执行证明。"
    except Exception:
        evidence = "无法安全取得充分的外部状态证据；本次检查没有执行或修改任何目标。"
    return ExternalStateInspection(
        turn_id=call.turn_id, step=call.step, call_id=call.call.id,
        tool_name=call.call.name, state=state, evidence=evidence,
    )


class RecoveryToolDenied(RuntimeError):
    """恢复能力的身份、顺序或外部状态校验失败。"""


@dataclass(frozen=True)
class _RecoveryInspectionRecord:
    session_id: str
    turn_id: str
    step: int
    call_id: str
    inspection: ExternalStateInspection

    @property
    def target(self) -> tuple[str, int, str]:
        return self.turn_id, self.step, self.call_id


class RecoveryToolRuntime:
    """运行期绑定的恢复协调器；令牌不持久化，重启后必须重新检查。"""

    def __init__(
        self,
        *,
        session_id: str,
        pending_provider: Callable[[], tuple[ToolCallEventData, ...]],
        resolution_sink: Callable[[ResolveRecoveryArguments, str], None],
    ) -> None:
        self._session_id = session_id
        self._pending_provider = pending_provider
        self._resolution_sink = resolution_sink
        self._lock = Lock()
        self._inspections: dict[str, _RecoveryInspectionRecord] = {}

    @staticmethod
    def _target(
        arguments: InspectRecoveryArguments | ResolveRecoveryArguments,
    ) -> tuple[str, int, str]:
        return arguments.turn_id, arguments.step, arguments.call_id

    def _pending(self, target: tuple[str, int, str]) -> ToolCallEventData:
        matches = [
            item for item in self._pending_provider()
            if (item.turn_id, item.step, item.call.id) == target
        ]
        if len(matches) != 1:
            raise RecoveryToolDenied("指定的历史工具调用没有待确认的未知结果。")
        return matches[0]

    def inspect(self, arguments: InspectRecoveryArguments) -> InspectRecoveryOutput:
        target = self._target(arguments)
        with self._lock:
            item = self._pending(target)
            inspection = inspect_external_state(item)
            # 同一目标的新检查会废弃旧令牌，避免模型误用较早观察。
            for token, record in tuple(self._inspections.items()):
                if record.target == target:
                    self._inspections.pop(token, None)
            token = token_hex(32)
            self._inspections[token] = _RecoveryInspectionRecord(
                session_id=self._session_id,
                turn_id=arguments.turn_id,
                step=arguments.step,
                call_id=arguments.call_id,
                inspection=inspection,
            )
            return InspectRecoveryOutput(
                **inspection.model_dump(mode="python"), inspection_token=token
            )

    def resolve(self, arguments: ResolveRecoveryArguments) -> ResolveRecoveryOutput:
        target = self._target(arguments)
        with self._lock:
            # 一旦提交就消耗令牌；任何校验失败都必须重新 inspect。
            record = self._inspections.pop(arguments.inspection_token, None)
            if record is None:
                raise RecoveryToolDenied("检查令牌无效、已使用或属于之前的运行。")
            if record.session_id != self._session_id or record.target != target:
                raise RecoveryToolDenied("检查令牌与指定的历史调用不匹配。")
            item = self._pending(target)
            current = inspect_external_state(item)
            if current != record.inspection:
                raise RecoveryToolDenied(
                    "外部状态在检查后发生变化；请重新调用 inspect_recovery。"
                )
            contradictory = (
                current.state == "matches_requested"
                and arguments.resolution != "applied"
            ) or (
                current.state in {"different", "missing"}
                and arguments.resolution != "not_applied"
            )
            if contradictory:
                raise RecoveryToolDenied(
                    "恢复结论与当前可验证的外部状态矛盾；"
                    "请重新调用 inspect_recovery。"
                )
            durable_evidence = (
                f"Agent只读检查：state={current.state}；{current.evidence}\n"
                f"Agent判断依据：{arguments.evidence.strip()}"
            )
            self._resolution_sink(arguments, durable_evidence)
            resolution = (
                "confirmed_applied"
                if arguments.resolution == "applied"
                else "confirmed_not_applied"
            )
            return ResolveRecoveryOutput(
                turn_id=arguments.turn_id,
                step=arguments.step,
                call_id=arguments.call_id,
                resolution=resolution,
            )
