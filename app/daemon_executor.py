"""Small daemon-thread executor for work that must not block interpreter exit."""

from __future__ import annotations

from concurrent.futures import Future
from threading import BoundedSemaphore, Lock, Thread
from typing import Callable, TypeVar


T = TypeVar("T")


def start_daemon_cleanup(function: Callable[[], object], *, name: str) -> None:
    """Run best-effort cleanup without making emergency exit wait for it."""
    if not callable(function):
        raise TypeError("function must be callable")

    def invoke() -> None:
        try:
            function()
        except BaseException:
            pass

    Thread(target=invoke, name=name, daemon=True).start()


class DaemonExecutor:
    def __init__(self, max_workers: int, *, thread_name_prefix: str) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._slots = BoundedSemaphore(max_workers)
        self._prefix = thread_name_prefix
        self._lock = Lock()
        self._closed = False
        self._counter = 0
        self._threads: set[Thread] = set()
        self._futures: set[Future[object]] = set()

    def submit(self, function: Callable[..., T], /, *args: object, **kwargs: object) -> Future[T]:
        future: Future[T] = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("executor is shut down")
            self._counter += 1
            name = f"{self._prefix}_{self._counter}"
            self._futures.add(future)  # type: ignore[arg-type]

        def run() -> None:
            self._slots.acquire()
            try:
                with self._lock:
                    closed = self._closed
                if closed or not future.set_running_or_notify_cancel():
                    return
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._slots.release()
                with self._lock:
                    self._futures.discard(future)  # type: ignore[arg-type]
                    # Thread object is removed by identity below without relying on globals.
                    self._threads.discard(thread)

        thread = Thread(target=run, name=name, daemon=True)
        with self._lock:
            self._threads.add(thread)
        thread.start()
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = True) -> None:
        with self._lock:
            self._closed = True
            futures = tuple(self._futures)
            threads = tuple(self._threads)
        if cancel_futures:
            for future in futures:
                future.cancel()
        if wait:
            for thread in threads:
                thread.join()

    def __enter__(self) -> "DaemonExecutor":
        return self

    def __exit__(self, *_args: object) -> None:
        self.shutdown(wait=True)
