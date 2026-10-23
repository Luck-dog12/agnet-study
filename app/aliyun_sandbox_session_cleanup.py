"""Bounded, session-scoped cleanup for abandoned Aliyun Sandboxes.

The cleaner is deliberately separate from a live ``Sandbox`` instance.  It
discovers resources only by the two execution-backend ownership metadata
fields, validates every provider result again, reconnects by an exact validated
ID, and proves that no matching active resource remains.  Provider exceptions
and identifiers never cross this module's public error boundary.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from app.sandbox_config import SandboxSettings


SANDBOX_OWNER_METADATA_KEY: Final[str] = "agnet-study.owner"
SANDBOX_OWNER_METADATA_VALUE: Final[str] = "coding-agent-execution"
SANDBOX_SESSION_METADATA_KEY: Final[str] = "agnet-study.session"

_SESSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE_STATES: Final[tuple[str, str]] = ("running", "paused")
_PAGE_SIZE: Final[int] = 32
_MAX_PAGES: Final[int] = 8
_MAX_OBJECTS: Final[int] = 128
_REQUEST_TIMEOUT_SECONDS: Final[float] = 15.0
_TOTAL_TIMEOUT_SECONDS: Final[float] = 60.0
_SAFE_ERROR: Final[str] = "Aliyun Sandbox 会话资源清理未能确认完成。"


class SandboxSessionCleanupError(RuntimeError):
    """A fixed-text failure that cannot reveal provider data or credentials."""


@dataclass(frozen=True, slots=True)
class SandboxRecoveryPage:
    """One provider page returned through the narrow offline-test seam."""

    items: Sequence[Any]
    next_token: str | None = None


class SandboxRecoveryProvider(Protocol):
    """Minimal provider surface needed by the recovery cleaner."""

    def list_page(
        self,
        *,
        metadata: Mapping[str, str],
        states: tuple[str, str],
        limit: int,
        next_token: str | None,
        request_timeout: float,
    ) -> SandboxRecoveryPage: ...

    def connect(self, sandbox_id: str, *, request_timeout: float) -> Any: ...

    def kill(self, sandbox: Any, *, request_timeout: float) -> bool: ...


class _E2BSandboxRecoveryProvider:
    """Production provider; SDK imports remain lazy until cleanup is required."""

    __slots__ = ("_settings",)

    def __init__(self, settings: SandboxSettings) -> None:
        self._settings = settings

    def _options(self, request_timeout: float) -> dict[str, Any]:
        settings = self._settings
        assert settings.api_key is not None
        assert settings.api_url is not None
        assert settings.domain is not None
        return {
            "api_key": settings.api_key.get_secret_value(),
            "api_url": settings.api_url,
            "domain": settings.domain,
            "request_timeout": request_timeout,
        }

    def list_page(
        self,
        *,
        metadata: Mapping[str, str],
        states: tuple[str, str],
        limit: int,
        next_token: str | None,
        request_timeout: float,
    ) -> SandboxRecoveryPage:
        from e2b import Sandbox
        from e2b.sandbox.sandbox_api import SandboxQuery, SandboxState

        if states != _ACTIVE_STATES:
            raise ValueError("unsupported recovery state filter")
        paginator = Sandbox.list(
            query=SandboxQuery(
                metadata=dict(metadata),
                state=[SandboxState.RUNNING, SandboxState.PAUSED],
            ),
            limit=limit,
            next_token=next_token,
            order="asc",
            **self._options(request_timeout),
        )
        items = paginator.next_items(request_timeout=request_timeout)
        return SandboxRecoveryPage(
            items=tuple(items),
            next_token=paginator.next_token,
        )

    def connect(self, sandbox_id: str, *, request_timeout: float) -> Any:
        from e2b import Sandbox

        # Do not pass ``timeout`` here: in E2B it changes Sandbox lifetime,
        # whereas ``request_timeout`` bounds only this control-plane request.
        return Sandbox.connect(
            sandbox_id,
            **self._options(request_timeout),
        )

    def kill(self, sandbox: Any, *, request_timeout: float) -> bool:
        return sandbox.kill(request_timeout=request_timeout)

    def __repr__(self) -> str:
        return "_E2BSandboxRecoveryProvider()"


def _safe_failure() -> SandboxSessionCleanupError:
    return SandboxSessionCleanupError(_SAFE_ERROR)


def _validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("session_id 必须是 32 位小写十六进制文本。")
    return session_id


def _validate_sandbox_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) < 32 or character.isspace() for character in value)
    ):
        raise _safe_failure()
    return value


def _validate_token(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(ord(character) < 32 or character.isspace() for character in value)
    ):
        raise _safe_failure()
    return value


def _state_text(value: Any) -> str:
    state = getattr(value, "value", value)
    if not isinstance(state, str) or state not in _ACTIVE_STATES:
        raise _safe_failure()
    return state


def _validated_item_id(
    item: Any,
    *,
    expected_metadata: Mapping[str, str],
) -> str:
    metadata = getattr(item, "metadata", None)
    if not isinstance(metadata, Mapping):
        raise _safe_failure()
    for key, expected in expected_metadata.items():
        value = metadata.get(key)
        if not isinstance(value, str) or value != expected:
            raise _safe_failure()
    session_value = metadata.get(SANDBOX_SESSION_METADATA_KEY)
    if not isinstance(session_value, str) or not _SESSION_ID_RE.fullmatch(session_value):
        raise _safe_failure()
    _state_text(getattr(item, "state", None))
    return _validate_sandbox_id(getattr(item, "sandbox_id", None))


def _request_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _safe_failure()
    return min(_REQUEST_TIMEOUT_SECONDS, remaining)


def _scan_active(
    provider: SandboxRecoveryProvider,
    *,
    expected_metadata: Mapping[str, str],
    deadline: float,
) -> tuple[str, ...]:
    next_token: str | None = None
    seen_tokens: set[str] = set()
    object_count = 0
    ordered_ids: list[str] = []
    seen_ids: set[str] = set()

    for page_number in range(_MAX_PAGES):
        timeout = _request_timeout(deadline)
        page = provider.list_page(
            metadata=expected_metadata,
            states=_ACTIVE_STATES,
            limit=_PAGE_SIZE,
            next_token=next_token,
            request_timeout=timeout,
        )
        _request_timeout(deadline)
        if not isinstance(page, SandboxRecoveryPage):
            raise _safe_failure()
        if not isinstance(page.items, Sequence) or isinstance(
            page.items, (str, bytes, bytearray)
        ):
            raise _safe_failure()
        if len(page.items) > _PAGE_SIZE:
            raise _safe_failure()
        object_count += len(page.items)
        if object_count > _MAX_OBJECTS:
            raise _safe_failure()
        page_ids = [
            _validated_item_id(item, expected_metadata=expected_metadata)
            for item in page.items
        ]
        for sandbox_id in page_ids:
            if sandbox_id not in seen_ids:
                seen_ids.add(sandbox_id)
                ordered_ids.append(sandbox_id)

        next_token = _validate_token(page.next_token)
        if next_token is None:
            return tuple(ordered_ids)
        if next_token in seen_tokens or page_number + 1 >= _MAX_PAGES:
            raise _safe_failure()
        seen_tokens.add(next_token)

    raise _safe_failure()


def cleanup_session_sandboxes(
    settings: SandboxSettings,
    session_id: str,
    *,
    provider: SandboxRecoveryProvider | None = None,
) -> None:
    """Kill all validated active Sandboxes for one persistent host session.

    The operation has fixed page, object, request, and wall-clock bounds.  A
    successful return means a final provider query observed zero matching
    running or paused Sandboxes.
    """
    if not isinstance(settings, SandboxSettings):
        raise TypeError("settings 必须是 SandboxSettings 实例。")
    safe_session_id = _validate_session_id(session_id)
    settings.require_enabled()
    selected_provider = provider or _E2BSandboxRecoveryProvider(settings)
    expected_metadata = {
        SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
        SANDBOX_SESSION_METADATA_KEY: safe_session_id,
    }
    deadline = time.monotonic() + _TOTAL_TIMEOUT_SECONDS

    try:
        sandbox_ids = _scan_active(
            selected_provider,
            expected_metadata=expected_metadata,
            deadline=deadline,
        )
        for sandbox_id in sandbox_ids:
            connected = selected_provider.connect(
                sandbox_id,
                request_timeout=_request_timeout(deadline),
            )
            _request_timeout(deadline)
            connected_id = _validate_sandbox_id(
                getattr(connected, "sandbox_id", None)
            )
            if connected_id != sandbox_id:
                raise _safe_failure()
            result = selected_provider.kill(
                connected,
                request_timeout=_request_timeout(deadline),
            )
            _request_timeout(deadline)
            if result is not True:
                raise _safe_failure()

        remaining = _scan_active(
            selected_provider,
            expected_metadata=expected_metadata,
            deadline=deadline,
        )
        if remaining:
            raise _safe_failure()
    except SandboxSessionCleanupError:
        raise
    except Exception:
        raise _safe_failure() from None


__all__ = [
    "SANDBOX_OWNER_METADATA_KEY",
    "SANDBOX_OWNER_METADATA_VALUE",
    "SANDBOX_SESSION_METADATA_KEY",
    "SandboxRecoveryPage",
    "SandboxRecoveryProvider",
    "SandboxSessionCleanupError",
    "cleanup_session_sandboxes",
]
