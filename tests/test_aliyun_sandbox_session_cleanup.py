"""Offline tests for bounded, session-scoped Sandbox recovery cleanup."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pydantic import SecretStr

from app.aliyun_sandbox_session_cleanup import (
    SANDBOX_OWNER_METADATA_KEY,
    SANDBOX_OWNER_METADATA_VALUE,
    SANDBOX_SESSION_METADATA_KEY,
    SandboxRecoveryPage,
    SandboxSessionCleanupError,
    _E2BSandboxRecoveryProvider,
    cleanup_session_sandboxes,
)
from app.sandbox_config import SandboxSettings


SESSION_ID = "a" * 32
SECRET_ID = "sandbox-sensitive-identifier"


def enabled_settings() -> SandboxSettings:
    return SandboxSettings(
        enabled=True,
        api_key=SecretStr("provider-sensitive-api-key"),
        api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        domain="cn-hangzhou.e2b.fc.aliyuncs.com",
        template="code-interpreter-v1",
        timeout_seconds=300,
        oss_bucket="agent-probe-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket_path="/projects/test/runs/backend",
        oss_mount_dir="/mnt/oss",
        oss_role_arn="acs:ram::1234567890123456:role/offline-role",
        oss_read_only=False,
    )


def item(
    sandbox_id: object = SECRET_ID,
    *,
    state: object = "running",
    owner: object = SANDBOX_OWNER_METADATA_VALUE,
    session_id: object = SESSION_ID,
    metadata: object | None = None,
) -> object:
    selected_metadata = (
        {
            SANDBOX_OWNER_METADATA_KEY: owner,
            SANDBOX_SESSION_METADATA_KEY: session_id,
            "provider.extra": "allowed",
        }
        if metadata is None
        else metadata
    )
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        state=state,
        metadata=selected_metadata,
    )


class ConnectedSandbox:
    def __init__(
        self,
        sandbox_id: str,
        *,
        kill_result: object = True,
        kill_error: BaseException | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.kill_result = kill_result
        self.kill_error = kill_error
        self.kill_calls = 0

    def kill(self) -> object:
        self.kill_calls += 1
        if self.kill_error is not None:
            raise self.kill_error
        return self.kill_result


class FakeRecoveryProvider:
    def __init__(self, pages: list[object]) -> None:
        self.pages = deque(pages)
        self.list_calls: list[dict[str, object]] = []
        self.connect_calls: list[tuple[str, float]] = []
        self.connected: dict[str, ConnectedSandbox] = {}
        self.connect_error: BaseException | None = None

    def list_page(self, **kwargs: object) -> SandboxRecoveryPage:
        self.list_calls.append(dict(kwargs))
        if not self.pages:
            raise AssertionError("unexpected list call")
        result = self.pages.popleft()
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    def connect(self, sandbox_id: str, *, request_timeout: float) -> object:
        self.connect_calls.append((sandbox_id, request_timeout))
        if self.connect_error is not None:
            raise self.connect_error
        return self.connected.setdefault(sandbox_id, ConnectedSandbox(sandbox_id))

    def kill(self, sandbox: object, *, request_timeout: float) -> bool:
        del request_timeout
        return sandbox.kill()  # type: ignore[no-any-return,union-attr]


class SandboxSessionCleanupTests(unittest.TestCase):
    def assert_safe_error(self, caught: BaseException) -> None:
        rendered = str(caught)
        representation = repr(caught)
        for secret in (
            SECRET_ID,
            "provider-sensitive-api-key",
            "provider-sensitive-error",
        ):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, representation)
        self.assertEqual(
            rendered,
            "Aliyun Sandbox 会话资源清理未能确认完成。",
        )

    def test_empty_initial_and_final_queries_prove_cleanup(self) -> None:
        provider = FakeRecoveryProvider([
            SandboxRecoveryPage(items=()),
            SandboxRecoveryPage(items=()),
        ])
        self.assertIsNone(
            cleanup_session_sandboxes(
                enabled_settings(), SESSION_ID, provider=provider
            )
        )
        self.assertEqual(len(provider.list_calls), 2)
        self.assertEqual(provider.connect_calls, [])
        for call in provider.list_calls:
            self.assertEqual(
                call["metadata"],
                {
                    SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
                    SANDBOX_SESSION_METADATA_KEY: SESSION_ID,
                },
            )
            self.assertEqual(call["states"], ("running", "paused"))
            self.assertLessEqual(call["request_timeout"], 15.0)

    def test_running_and_paused_are_deduplicated_connected_and_killed(self) -> None:
        first_id = "stale-running"
        second_id = "stale-paused"
        provider = FakeRecoveryProvider([
            SandboxRecoveryPage(
                items=(
                    item(first_id, state="running"),
                    item(first_id, state="running"),
                    item(second_id, state="paused"),
                )
            ),
            SandboxRecoveryPage(items=()),
        ])
        cleanup_session_sandboxes(enabled_settings(), SESSION_ID, provider=provider)
        self.assertEqual(
            [call[0] for call in provider.connect_calls],
            [first_id, second_id],
        )
        self.assertEqual(provider.connected[first_id].kill_calls, 1)
        self.assertEqual(provider.connected[second_id].kill_calls, 1)

    def test_malformed_or_mismatched_items_fail_before_any_kill(self) -> None:
        invalid_items = (
            item(owner="other-owner"),
            item(session_id="b" * 32),
            item(session_id="NOT-HEX"),
            item(state="stopped"),
            item(sandbox_id="bad id"),
            item(metadata=[]),
        )
        for invalid in invalid_items:
            with self.subTest(invalid=type(invalid).__name__):
                provider = FakeRecoveryProvider([
                    SandboxRecoveryPage(items=(item("valid-first"), invalid)),
                ])
                with self.assertRaises(SandboxSessionCleanupError) as caught:
                    cleanup_session_sandboxes(
                        enabled_settings(), SESSION_ID, provider=provider
                    )
                self.assert_safe_error(caught.exception)
                self.assertEqual(provider.connect_calls, [])

    def test_page_limit_and_total_object_limit_are_fail_closed(self) -> None:
        page_limited = FakeRecoveryProvider([
            SandboxRecoveryPage(items=(), next_token=f"page-{index}")
            for index in range(8)
        ])
        with self.assertRaises(SandboxSessionCleanupError):
            cleanup_session_sandboxes(
                enabled_settings(), SESSION_ID, provider=page_limited
            )
        self.assertEqual(len(page_limited.list_calls), 8)
        self.assertEqual(page_limited.connect_calls, [])

        pages: list[SandboxRecoveryPage] = []
        for page_number in range(5):
            count = 32 if page_number < 4 else 1
            pages.append(
                SandboxRecoveryPage(
                    items=tuple(
                        item(f"item-{page_number}-{offset}")
                        for offset in range(count)
                    ),
                    next_token=f"total-{page_number}",
                )
            )
        total_limited = FakeRecoveryProvider(pages)
        with self.assertRaises(SandboxSessionCleanupError):
            cleanup_session_sandboxes(
                enabled_settings(), SESSION_ID, provider=total_limited
            )
        self.assertEqual(total_limited.connect_calls, [])

    def test_list_connect_kill_and_false_results_are_sanitized(self) -> None:
        cases: list[FakeRecoveryProvider] = []
        cases.append(
            FakeRecoveryProvider([OSError("provider-sensitive-error")])
        )

        connect_failure = FakeRecoveryProvider([
            SandboxRecoveryPage(items=(item(),)),
        ])
        connect_failure.connect_error = OSError("provider-sensitive-error")
        cases.append(connect_failure)

        kill_failure = FakeRecoveryProvider([
            SandboxRecoveryPage(items=(item(),)),
        ])
        kill_failure.connected[SECRET_ID] = ConnectedSandbox(
            SECRET_ID,
            kill_error=OSError("provider-sensitive-error"),
        )
        cases.append(kill_failure)

        kill_false = FakeRecoveryProvider([
            SandboxRecoveryPage(items=(item(),)),
        ])
        kill_false.connected[SECRET_ID] = ConnectedSandbox(
            SECRET_ID,
            kill_result=False,
        )
        cases.append(kill_false)

        wrong_connect_id = FakeRecoveryProvider([
            SandboxRecoveryPage(items=(item(),)),
        ])
        wrong_connect_id.connected[SECRET_ID] = ConnectedSandbox("other-id")
        cases.append(wrong_connect_id)

        for provider in cases:
            with self.subTest(provider=provider):
                with self.assertRaises(SandboxSessionCleanupError) as caught:
                    cleanup_session_sandboxes(
                        enabled_settings(), SESSION_ID, provider=provider
                    )
                self.assert_safe_error(caught.exception)

    def test_final_nonzero_requery_fails_even_after_confirmed_kill(self) -> None:
        provider = FakeRecoveryProvider([
            SandboxRecoveryPage(items=(item(),)),
            SandboxRecoveryPage(items=(item(),)),
        ])
        with self.assertRaises(SandboxSessionCleanupError) as caught:
            cleanup_session_sandboxes(
                enabled_settings(), SESSION_ID, provider=provider
            )
        self.assert_safe_error(caught.exception)
        self.assertEqual(provider.connected[SECRET_ID].kill_calls, 1)
        self.assertEqual(len(provider.connect_calls), 1)

    def test_repeated_or_malformed_page_tokens_are_rejected(self) -> None:
        for pages in (
            [
                SandboxRecoveryPage(items=(), next_token="repeat"),
                SandboxRecoveryPage(items=(), next_token="repeat"),
            ],
            [SandboxRecoveryPage(items=(), next_token="bad token")],
        ):
            provider = FakeRecoveryProvider(pages)
            with self.assertRaises(SandboxSessionCleanupError):
                cleanup_session_sandboxes(
                    enabled_settings(), SESSION_ID, provider=provider
                )
            self.assertEqual(provider.connect_calls, [])

    def test_default_connect_uses_request_timeout_not_lifetime_timeout(self) -> None:
        provider = _E2BSandboxRecoveryProvider(enabled_settings())
        with patch("e2b.Sandbox.connect", return_value=object()) as connect:
            provider.connect("validated-id", request_timeout=7.5)
        _, kwargs = connect.call_args
        self.assertEqual(kwargs["request_timeout"], 7.5)
        self.assertNotIn("timeout", kwargs)

        sandbox = ConnectedSandbox("validated-id")
        with patch.object(sandbox, "kill", return_value=True) as kill:
            self.assertIs(provider.kill(sandbox, request_timeout=6.5), True)
        kill.assert_called_once_with(request_timeout=6.5)

    def test_default_list_uses_exact_filters_and_bounded_request_timeout(self) -> None:
        provider = _E2BSandboxRecoveryProvider(enabled_settings())
        paginator = Mock()
        paginator.next_items.return_value = []
        paginator.next_token = None
        metadata = {
            SANDBOX_OWNER_METADATA_KEY: SANDBOX_OWNER_METADATA_VALUE,
            SANDBOX_SESSION_METADATA_KEY: SESSION_ID,
        }
        with patch("e2b.Sandbox.list", return_value=paginator) as list_call:
            page = provider.list_page(
                metadata=metadata,
                states=("running", "paused"),
                limit=32,
                next_token=None,
                request_timeout=5.5,
            )
        self.assertEqual(tuple(page.items), ())
        kwargs = list_call.call_args.kwargs
        self.assertEqual(kwargs["query"].metadata, metadata)
        self.assertEqual(
            [state.value for state in kwargs["query"].state],
            ["running", "paused"],
        )
        self.assertEqual(kwargs["limit"], 32)
        self.assertEqual(kwargs["request_timeout"], 5.5)
        paginator.next_items.assert_called_once_with(request_timeout=5.5)

    def test_total_timeout_is_fixed_and_fails_before_provider_dispatch(self) -> None:
        provider = FakeRecoveryProvider([])
        with patch(
            "app.aliyun_sandbox_session_cleanup.time.monotonic",
            side_effect=(100.0, 161.0),
        ):
            with self.assertRaises(SandboxSessionCleanupError) as caught:
                cleanup_session_sandboxes(
                    enabled_settings(), SESSION_ID, provider=provider
                )
        self.assert_safe_error(caught.exception)
        self.assertEqual(provider.list_calls, [])

    def test_system_exceptions_are_not_rewritten(self) -> None:
        provider = FakeRecoveryProvider([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            cleanup_session_sandboxes(
                enabled_settings(), SESSION_ID, provider=provider
            )


if __name__ == "__main__":
    unittest.main()
