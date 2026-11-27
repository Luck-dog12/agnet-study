"""Host-owned, crash-conservative residency budget for one sandbox pair.

Seconds describe the lifetime of a pair, not a price or the sum of its two VMs.
Reserve before every cloud create/renew; refund only after confirmed destruction.
The ledger uses its own OS lock and owner token, independent of the session lock.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable, Iterator
from uuid import uuid4


class SandboxBudgetError(RuntimeError):
    """Budget persistence or ownership cannot be trusted; refuse cloud allocation."""


class SandboxBudgetExceeded(SandboxBudgetError):
    """The named session has no whole seconds left to allocate."""


_MAX_LEDGER_BYTES = 4096
_LEDGER_KEYS = {"version", "session_id", "owner", "used_seconds", "reserved_seconds"}
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")


def _safe_stat(path: Path, *, directory: bool = False) -> os.stat_result:
    info = os.lstat(path)
    reparse = getattr(info, "st_file_attributes", 0) & 0x400
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or stat.S_ISLNK(info.st_mode) or reparse:
        raise SandboxBudgetError("沙箱预算存储路径不安全。")
    if not directory and info.st_nlink != 1:
        raise SandboxBudgetError("沙箱预算文件不允许硬链接。")
    return info


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SandboxBudgetError("沙箱预算记录格式无效。")
        result[key] = value
    return result


@contextmanager
def _storage_lock(path: Path) -> Iterator[Callable[[], None]]:
    """Pin Windows parent directories and lock a stable, separate lock file."""
    identities: list[tuple[Path, os.stat_result]] = []
    handles: list[int] = []
    descriptor: int | None = None
    locked = False
    try:
        for directory in [*reversed(path.parent.parents), path.parent]:
            info = _safe_stat(directory, directory=True)
            identities.append((directory, info))
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                create = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
                create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
                create.restype = wintypes.HANDLE
                handle = create(str(directory), 0x80, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise SandboxBudgetError("无法固定沙箱预算目录。")
                handles.append(handle)

        def verify() -> None:
            for directory, before in identities:
                if _identity(_safe_stat(directory, directory=True)) != _identity(before):
                    raise SandboxBudgetError("沙箱预算目录已发生变化。")
            if descriptor is not None:
                current = _safe_stat(lock_path)
                if _identity(current) != _identity(os.fstat(descriptor)):
                    raise SandboxBudgetError("沙箱预算锁文件已发生变化。")

        verify()
        lock_path = path.with_name(path.name + ".lock")
        try:
            _safe_stat(lock_path)
        except FileNotFoundError:
            pass
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT
                             | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        verify()
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        elif os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise SandboxBudgetError("当前平台不支持可靠沙箱预算锁。")
        locked = True
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        verify()
        yield verify
    finally:
        try:
            if descriptor is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        elif os.name == "posix":
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            if handles:
                import ctypes
                from ctypes import wintypes

                close = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
                close.argtypes = [wintypes.HANDLE]
                close.restype = wintypes.BOOL
                for handle in reversed(handles):
                    close(handle)


class SandboxBudget:
    """Track cumulative residency across idle recreation and CLI process restarts.

    Opening a persistent budget transfers ownership and charges any unconfirmed
    prior reservation in full. A superseded object can never renew or refund it.
    Configuration may change the limit; previously used seconds are never reset.
    """

    def __init__(self, max_seconds: int, *, path: Path | None = None,
                 session_id: str, clock: Callable[[], float] = time.monotonic) -> None:
        if type(max_seconds) is not int or not 1 <= max_seconds <= 86400:
            raise ValueError("沙箱会话预算必须是 1..86400 的整数秒。")
        if not isinstance(session_id, str) or _ID_RE.fullmatch(session_id) is None:
            raise ValueError("沙箱预算需要有效的会话标识。")
        self._maximum = max_seconds
        self._session_id = session_id
        self._path = None if path is None else Path(os.path.abspath(path))
        self._clock = clock
        self._lock = threading.RLock()
        self._owner = uuid4().hex
        self._used = 0.0
        self._reserved = 0.0
        self._started: float | None = None
        self._last_clock: float | None = None
        self._failed = False
        if self._path is not None:
            self._persist(0.0, 0.0, take_ownership=True)

    def _now(self) -> float:
        current = self._clock()
        if (type(current) not in {int, float} or not math.isfinite(current)
                or (self._last_clock is not None and current < self._last_clock)):
            self._failed = True
            raise SandboxBudgetError("沙箱预算计时不可用。")
        self._last_clock = float(current)
        return float(current)

    def _elapsed(self) -> float:
        return 0.0 if self._started is None else self._now() - self._started

    def _check(self) -> None:
        if self._failed:
            raise SandboxBudgetError("沙箱预算不可用，已停止创建和续期。")

    def _read(self) -> dict[str, object] | None:
        assert self._path is not None
        try:
            before = _safe_stat(self._path)
        except FileNotFoundError:
            return None
        if before.st_size > _MAX_LEDGER_BYTES:
            raise SandboxBudgetError("沙箱预算记录超过大小限制。")
        descriptor = os.open(self._path, os.O_RDONLY | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try:
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise SandboxBudgetError("沙箱预算文件已发生变化。")
            raw = os.read(descriptor, _MAX_LEDGER_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > _MAX_LEDGER_BYTES:
            raise SandboxBudgetError("沙箱预算记录超过大小限制。")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or set(data) != _LEDGER_KEYS:
            raise SandboxBudgetError("沙箱预算记录格式无效。")
        if type(data["version"]) is not int or data["version"] != 1:
            raise SandboxBudgetError("沙箱预算记录版本无效。")
        if data["session_id"] != self._session_id:
            raise SandboxBudgetError("沙箱预算记录与当前会话不匹配。")
        if not isinstance(data["owner"], str) or not _ID_RE.fullmatch(data["owner"]):
            raise SandboxBudgetError("沙箱预算记录所有者无效。")
        for key in ("used_seconds", "reserved_seconds"):
            value = data[key]
            if (type(value) not in {int, float} or not math.isfinite(value)
                    or not 0 <= value <= 1e15):
                raise SandboxBudgetError("沙箱预算记录时长无效。")
        return data

    def _write(self, used: float, reserved: float,
               verify: Callable[[], None]) -> None:
        assert self._path is not None
        payload = json.dumps({"version": 1, "session_id": self._session_id,
                              "owner": self._owner, "used_seconds": used,
                              "reserved_seconds": reserved},
                             ensure_ascii=True, allow_nan=False).encode("utf-8")
        temporary = self._path.with_name(self._path.name + "." + uuid4().hex + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_BINARY", 0)
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            verify()
            try:
                _safe_stat(self._path)
            except FileNotFoundError:
                pass
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
                move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
                move.restype = wintypes.BOOL
                if not move(str(temporary), str(self._path), 0x1 | 0x8):
                    raise OSError("atomic budget replace failed")
            else:
                os.replace(temporary, self._path)
                directory_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            verify()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _persist(self, used: float, reserved: float, *, take_ownership: bool = False) -> None:
        if self._path is None:
            return
        try:
            with _storage_lock(self._path) as verify:
                existing = self._read()
                if take_ownership:
                    if existing is not None:
                        used = float(existing["used_seconds"]) + float(existing["reserved_seconds"])
                    self._write(used, 0.0, verify)
                    self._used = used
                else:
                    if existing is None or existing["owner"] != self._owner:
                        raise SandboxBudgetError("沙箱预算已由新的会话进程接管。")
                    self._write(used, reserved, verify)
        except Exception as error:
            self._failed = True
            if isinstance(error, SandboxBudgetError):
                raise
            raise SandboxBudgetError("无法安全读写沙箱预算，已停止创建和续期。") from None

    def _grant(self, requested_seconds: int, elapsed: float) -> int:
        if type(requested_seconds) is not int or requested_seconds <= 0:
            raise ValueError("沙箱预留时长必须是正整数。")
        grant = min(requested_seconds, math.floor(self._maximum - self._used - elapsed))
        if grant < 1:
            raise SandboxBudgetExceeded("当前会话的沙箱累计时长预算已用尽。")
        return grant

    def begin(self, requested_seconds: int) -> int:
        """Reserve before creating either VM; return the approved integer TTL."""
        with self._lock:
            self._check()
            if self._started is not None:
                raise SandboxBudgetError("沙箱预算已有活动预留。")
            grant = self._grant(requested_seconds, 0.0)
            started = self._now()
            self._persist(self._used, float(grant))
            self._reserved = float(grant)
            self._started = started
            return grant

    def extend(self, requested_seconds: int) -> int:
        """Persist a worst-case reservation before resetting either VM timeout."""
        with self._lock:
            self._check()
            if self._started is None:
                raise SandboxBudgetError("沙箱预算没有活动预留。")
            elapsed = self._elapsed()
            grant = self._grant(requested_seconds, elapsed)
            reserved = max(self._reserved, elapsed + grant)
            self._persist(self._used, reserved)
            self._reserved = reserved
            return grant

    def finish(self, confirmed: bool) -> None:
        """Settle once; unknown destruction keeps the entire reservation charged."""
        if type(confirmed) is not bool:
            raise ValueError("沙箱销毁确认必须是布尔值。")
        with self._lock:
            self._check()
            if self._started is None:
                return
            elapsed = self._elapsed()
            used = self._used + (elapsed if confirmed else max(elapsed, self._reserved))
            self._persist(used, 0.0)
            self._used = used
            self._reserved = 0.0
            self._started = None

    def snapshot(self) -> dict[str, int | float | bool]:
        """Return local, read-only scalar status without taking ledger ownership."""
        with self._lock:
            self._check()
            elapsed = self._elapsed()
            return {"max_seconds": self._maximum,
                    "used_seconds": self._used + elapsed,
                    "remaining_seconds": max(0.0, self._maximum - self._used - elapsed),
                    "reserved_seconds": max(0.0, self._reserved - elapsed),
                    "active": self._started is not None}

    def remaining_seconds(self) -> float:
        return float(self.snapshot()["remaining_seconds"])

    def reservation_remaining_seconds(self) -> float:
        return float(self.snapshot()["reserved_seconds"])
