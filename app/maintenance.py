"""Agent 运行期间的低频、安全后台维护。"""

from __future__ import annotations

from threading import Event, Lock, Thread

from app.observability import Observability


_DEFAULT_INTERVAL_SECONDS = 60 * 60
_DEFAULT_JOIN_TIMEOUT_SECONDS = 1.0


class MaintenanceService:
    """立即并周期执行独立 fail-soft 的维护任务。

    构造对象不会访问文件系统或创建线程。只有 ``start`` 才运行任务并启动
    daemon 线程；``close`` 通过 Event 唤醒等待，因而无需等待完整周期。
    """

    def __init__(
        self,
        observability: Observability,
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        join_timeout_seconds: float = _DEFAULT_JOIN_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or interval_seconds <= 0
        ):
            raise ValueError("interval_seconds 必须大于 0")
        if (
            isinstance(join_timeout_seconds, bool)
            or not isinstance(join_timeout_seconds, (int, float))
            or join_timeout_seconds < 0
        ):
            raise ValueError("join_timeout_seconds 不能小于 0")
        self._observability = observability
        self._interval_seconds = float(interval_seconds)
        self._join_timeout_seconds = float(join_timeout_seconds)
        self._stop_event = Event()
        self._lifecycle_lock = Lock()
        self._thread: Thread | None = None

    @property
    def is_running(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> None:
        """依次运行所有任务；一个任务失败不会阻止其它任务。"""
        tasks = (
            self._observability.cleanup_expired_logs,
            self._observability.flush_pending,
        )
        for task in tasks:
            try:
                task()
            except Exception:
                continue

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self.run_once()

    def start(self) -> None:
        """立即维护一次，然后至多启动一个周期线程。"""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self.run_once()
            thread = Thread(
                target=self._run,
                name="agent-maintenance",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._stop_event.set()
                raise

    def close(self) -> None:
        """快速唤醒并尽力 join 后台线程；重复调用安全。"""
        with self._lifecycle_lock:
            thread = self._thread
            self._stop_event.set()
        if thread is None:
            return
        thread.join(timeout=self._join_timeout_seconds)
        if not thread.is_alive():
            with self._lifecycle_lock:
                if self._thread is thread:
                    self._thread = None
