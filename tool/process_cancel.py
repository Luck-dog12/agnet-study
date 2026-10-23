"""Cross-platform process-tree cancellation for background tools.

``ProcessCancellationHandle`` is deliberately independent from task state.  The
task manager decides the durable/logical terminal state first, then calls this
object as a best-effort physical cancellation hook.  A cancellation request is
sticky so that a request arriving before ``Popen`` is bound still prevents the
new child from continuing.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import signal
import subprocess
from threading import RLock

from tool.file_access import ToolExecutionError


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _IOCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _windows_api():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class WindowsJob:
    """Owned Windows Job Object with kill-on-close and optional resource limits."""

    __slots__ = ("_handle", "_lock")

    def __init__(
        self,
        *,
        memory_bytes: int | None = None,
        max_processes: int | None = None,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows Job Object 只能在 Windows 上创建。")
        if (memory_bytes is None) != (max_processes is None):
            raise ValueError("内存与进程限制必须同时提供。")
        self._lock = RLock()
        kernel32 = _windows_api()
        raw_handle = kernel32.CreateJobObjectW(None, None)
        if not raw_handle:
            raise ToolExecutionError("无法建立隔离资源边界。")
        self._handle: int | None = int(raw_handle)
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_bytes is not None and max_processes is not None:
            if memory_bytes <= 0 or max_processes <= 0:
                self.close()
                raise ValueError("资源限制必须为正数。")
            limits.BasicLimitInformation.LimitFlags |= (
                _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | _JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            limits.BasicLimitInformation.ActiveProcessLimit = max_processes
            limits.ProcessMemoryLimit = memory_bytes
            limits.JobMemoryLimit = memory_bytes
        if not kernel32.SetInformationJobObject(
            raw_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            raise ToolExecutionError("无法建立隔离资源边界。")

    def assign(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                raise ToolExecutionError("进程资源边界已关闭。")
            kernel32 = _windows_api()
            if not kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(handle),
                wintypes.HANDLE(process._handle),  # type: ignore[attr-defined]
            ):
                raise ToolExecutionError("无法应用隔离资源边界。")

    def terminate(self) -> None:
        """Terminate every process in the job; repeated calls are harmless."""
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            _windows_api().TerminateJobObject(wintypes.HANDLE(handle), 1)

    def close(self, *, terminate: bool = False) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            kernel32 = _windows_api()
            if terminate:
                kernel32.TerminateJobObject(wintypes.HANDLE(handle), 1)
            kernel32.CloseHandle(wintypes.HANDLE(handle))


class _ProcessBinding:
    __slots__ = ("process", "windows_job", "_lock", "_released")

    def __init__(
        self,
        process: subprocess.Popen[str],
        windows_job: WindowsJob | None,
    ) -> None:
        self.process = process
        self.windows_job = windows_job
        self._lock = RLock()
        self._released = False

    def terminate(self) -> None:
        with self._lock:
            if self._released or self.process.poll() is not None:
                return
            try:
                if os.name == "nt":
                    if self.windows_job is None:
                        self.process.kill()
                    else:
                        self.windows_job.terminate()
                else:
                    os.killpg(self.process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                # The process may have crossed its terminal boundary between
                # poll() and termination.  Cancellation remains best-effort.
                pass

    def release(self) -> None:
        with self._lock:
            self._released = True


class ProcessCancellationHandle:
    """Sticky, thread-safe bridge from logical cancellation to one child tree."""

    __slots__ = ("_binding", "_cancel_requested", "_lock")

    def __init__(self) -> None:
        self._lock = RLock()
        self._binding: _ProcessBinding | None = None
        self._cancel_requested = False

    @property
    def cancellation_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True
            binding = self._binding
        if binding is not None:
            binding.terminate()

    def bind(
        self,
        process: subprocess.Popen[str],
        *,
        windows_job: WindowsJob | None = None,
    ) -> None:
        """Bind one active process; a prior cancel immediately terminates it."""
        if not isinstance(process, subprocess.Popen):
            raise TypeError("process 必须是 Popen 实例。")
        binding = _ProcessBinding(process, windows_job)
        with self._lock:
            if self._binding is not None:
                raise RuntimeError("取消句柄已绑定其他进程。")
            self._binding = binding
            cancelled = self._cancel_requested
        if cancelled:
            binding.terminate()

    def terminate_bound(self) -> None:
        """Terminate the current tree without making future binds cancelled."""
        with self._lock:
            binding = self._binding
        if binding is not None:
            binding.terminate()

    def release(self, process: subprocess.Popen[str]) -> None:
        """Detach a completed process; a mismatched release fails closed."""
        with self._lock:
            binding = self._binding
            if binding is None:
                return
            if binding.process is not process:
                raise RuntimeError("释放的进程与当前绑定不匹配。")
            self._binding = None
        binding.release()

