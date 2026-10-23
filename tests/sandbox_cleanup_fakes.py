"""Offline recovery-provider fakes shared by execution-backend tests."""

from __future__ import annotations

from collections.abc import Mapping

from app.aliyun_sandbox_session_cleanup import SandboxRecoveryPage


class EmptySandboxRecoveryProvider:
    """Proves an empty provider result without importing or calling E2B."""

    def list_page(
        self,
        *,
        metadata: Mapping[str, str],
        states: tuple[str, str],
        limit: int,
        next_token: str | None,
        request_timeout: float,
    ) -> SandboxRecoveryPage:
        del metadata, states, limit, next_token, request_timeout
        return SandboxRecoveryPage(items=())

    def connect(self, sandbox_id: str, *, request_timeout: float) -> object:
        del sandbox_id, request_timeout
        raise AssertionError("empty provider must never connect")

    def kill(self, sandbox: object, *, request_timeout: float) -> bool:
        del sandbox, request_timeout
        raise AssertionError("empty provider must never kill")


def empty_recovery_provider() -> EmptySandboxRecoveryProvider:
    return EmptySandboxRecoveryProvider()


class FailingSandboxRecoveryProvider(EmptySandboxRecoveryProvider):
    def list_page(self, **kwargs) -> SandboxRecoveryPage:
        del kwargs
        raise OSError("provider-sensitive-error")


def failing_recovery_provider() -> FailingSandboxRecoveryProvider:
    return FailingSandboxRecoveryProvider()
