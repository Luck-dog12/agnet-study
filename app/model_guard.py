"""Bounded model-service checks, isolated from user history and tool runtimes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from math import isfinite
from threading import Event, Lock, Thread
from typing import Any, Literal

from app.client import create_client
from app.config import Settings


# Kept only to recognize old append-only pause/reset audit events.
MODEL_GUARD_SOURCE = "model_guard"
MODEL_CHECKING_MESSAGE = "正在检查模型服务；检查期间的需求不会排队，请等待检查完成。"
MODEL_UNAVAILABLE_MESSAGE = (
    "模型服务失败。当前输入已停用，请勿反复输入“继续”；故障期间的需求会直接丢弃。"
    "正在自动检查恢复，恢复后请重新输入完整需求。"
    "已执行的工具操作不会自动撤销，未知结果仍需核验。"
)
MODEL_READY_MESSAGE = "模型服务可用，请输入新需求；旧任务和故障期间的输入不会自动执行。"
MODEL_INPUT_DISCARDED_MESSAGE = "此条输入未被接受，已直接丢弃；请在模型服务可用后重新输入完整需求。"
MODEL_PROBE_TIMEOUT_SECONDS = 10.0
MODEL_RECOVERY_INTERVAL_SECONDS = 15.0
ModelServiceState = Literal["checking", "ready", "unavailable"]


def _field(value: object, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def probe_model_service(settings: Settings) -> bool:
    """One tiny inference request; no history, tools, retries or provider text leaks."""
    client = None
    try:
        bounded = replace(settings, timeout_seconds=MODEL_PROBE_TIMEOUT_SECONDS, max_retries=0)
        client = create_client(bounded)
        response = client.chat.completions.create(
            model=settings.model,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            stream=False,
            max_tokens=16,
            temperature=0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        choices = _field(response, "choices")
        if not isinstance(choices, (list, tuple)) or len(choices) != 1:
            return False
        message = _field(choices[0], "message")
        content = _field(message, "content")
        return (
            _field(message, "role") == "assistant"
            and isinstance(content, str)
            and bool(content.strip())
            and not _field(message, "tool_calls")
        )
    except Exception:
        return False
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class ModelServiceMonitor:
    """Check on startup/failure; healthy service causes no background traffic.

    Callbacks contain a fixed state token only. They run outside the state lock
    and are serialized so a late successful probe cannot reopen a newer fault.
    close() never joins a network worker, keeping CLI exit immediate.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        on_change: Callable[[ModelServiceState], None],
        probe: Callable[[Settings], bool] | None = None,
        retry_interval: float = MODEL_RECOVERY_INTERVAL_SECONDS,
    ) -> None:
        if (
            isinstance(retry_interval, bool)
            or not isinstance(retry_interval, (int, float))
            or not isfinite(retry_interval)
            or retry_interval <= 0
        ):
            raise ValueError("retry_interval must be positive")
        self._settings = settings
        self._on_change = on_change
        self._probe = probe_model_service if probe is None else probe
        self._retry_interval = retry_interval
        self._lock = Lock()
        self._notifications = Lock()
        self._wake = Event()
        self._closed = Event()
        self._state: ModelServiceState = "checking"
        self._revision = 0
        self._started = False
        self._thread = Thread(target=self._run, name="agent-model-service", daemon=True)

    @property
    def state(self) -> ModelServiceState:
        with self._lock:
            return self._state

    def _notify(self, state: ModelServiceState, revision: int) -> None:
        with self._notifications:
            with self._lock:
                if self._closed.is_set() or revision != self._revision or state != self._state:
                    return
            self._on_change(state)

    def start(self) -> None:
        with self._lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
            state, revision = self._state, self._revision
        self._notify(state, revision)
        self._thread.start()

    def mark_unavailable(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            self._state = "unavailable"
            self._revision += 1
            revision = self._revision
        self._notify("unavailable", revision)
        self._wake.set()

    def close(self) -> None:
        self._closed.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._closed.is_set():
            with self._lock:
                state, revision = self._state, self._revision
            if state == "ready":
                self._wake.wait()
                self._wake.clear()
                continue
            if state == "unavailable":
                # A real user request already established failure. Wait before
                # probing instead of immediately repeating a failed request.
                self._wake.clear()
                if self._closed.wait(self._retry_interval):
                    return
                with self._lock:
                    revision = self._revision
            try:
                available = self._probe(self._settings) is True
            except Exception:
                available = False
            with self._lock:
                if self._closed.is_set():
                    return
                if revision != self._revision:
                    continue
                new_state: ModelServiceState = "ready" if available else "unavailable"
                changed = new_state != self._state
                self._state = new_state
                self._revision += 1
                committed_revision = self._revision
            if changed:
                self._notify(new_state, committed_revision)
