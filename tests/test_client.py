"""OpenAI 兼容客户端构造的独立离线测试。"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.client import create_client
from app.config import Settings


class ClientTests(unittest.TestCase):
    def test_explicit_settings_are_forwarded_without_network_request(self):
        settings = Settings(
            api_key="fake-key",
            base_url="https://example.test/v1",
            model="deepseek-test",
            timeout_seconds=60.0,
        )
        fake_client = object()
        with patch("app.client.OpenAI", return_value=fake_client) as constructor:
            self.assertIs(create_client(settings), fake_client)
        constructor.assert_called_once_with(
            api_key="fake-key",
            base_url="https://example.test/v1",
            timeout=60.0,
            max_retries=2,
        )

    def test_omitted_settings_are_loaded_once(self):
        settings = Settings(api_key="fake-key")
        with (
            patch("app.client.load_settings", return_value=settings) as loader,
            patch("app.client.OpenAI", return_value=Mock()) as constructor,
        ):
            create_client()
        loader.assert_called_once_with()
        constructor.assert_called_once()

    def test_explicit_settings_skip_loader_and_wrong_type_is_rejected(self):
        with (
            patch("app.client.load_settings", side_effect=AssertionError("must not load")),
            patch("app.client.OpenAI", return_value=Mock()),
        ):
            create_client(Settings(api_key="fake"))
            with self.assertRaises(TypeError):
                create_client({"api_key": "fake"})  # type: ignore[arg-type]

    def test_sdk_constructor_exception_is_not_rewritten_with_secrets(self):
        error = RuntimeError("constructor failed")
        with patch("app.client.OpenAI", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                create_client(Settings(api_key="fake"))
        self.assertIs(raised.exception, error)

    def test_mutated_frozen_settings_are_revalidated_before_sdk_construction(self):
        settings = Settings(api_key="fake")
        object.__setattr__(settings, "timeout_seconds", 1)
        with patch("app.client.OpenAI") as constructor, self.assertRaises(ValueError):
            create_client(settings)
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
