"""配置加载的独立离线测试；不读取项目真实 .env。"""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app.config import Settings, load_default_execution_backend, load_settings


class ConfigTests(unittest.TestCase):
    def load(self, local: dict[str, object], environment: dict[str, str] | None = None):
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("DEEPSEEK_")
            and key != "AGENT_DEFAULT_EXECUTION_BACKEND"
        }
        clean_environment.update(environment or {})
        with (
            patch.dict(os.environ, clean_environment, clear=True),
            patch("app.config.dotenv_values", return_value=local) as dotenv,
        ):
            settings = load_settings()
        return settings, dotenv

    def load_backend(
        self,
        local: dict[str, object],
        environment: dict[str, str] | None = None,
    ):
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if key != "AGENT_DEFAULT_EXECUTION_BACKEND"
        }
        clean_environment.update(environment or {})
        with (
            patch.dict(os.environ, clean_environment, clear=True),
            patch("app.config.dotenv_values", return_value=local) as dotenv,
        ):
            backend = load_default_execution_backend()
        return backend, dotenv

    def test_local_values_defaults_and_safe_repr(self):
        settings, dotenv = self.load({"DEEPSEEK_API_KEY": "  fake-test-key  "})
        self.assertEqual(settings.api_key, "fake-test-key")
        self.assertEqual(settings.base_url, "https://api.deepseek.com")
        self.assertEqual(settings.model, "deepseek-v4-flash")
        self.assertFalse(hasattr(settings, "default_execution_backend"))
        self.assertFalse(hasattr(settings, "observability_mode"))
        self.assertNotIn("fake-test-key", repr(settings))
        args, kwargs = dotenv.call_args
        self.assertEqual(args[0], Path(__file__).resolve().parent.parent / ".env")
        self.assertEqual(kwargs, {"encoding": "utf-8-sig", "interpolate": False})

    def test_environment_has_priority_without_mutating_it(self):
        environment = {
            "DEEPSEEK_API_KEY": "env-key",
            "DEEPSEEK_BASE_URL": "https://example.test/v1",
            "DEEPSEEK_MODEL": "deepseek-test",
            "AGENT_DEFAULT_EXECUTION_BACKEND": "local",
        }
        settings, _ = self.load(
            {
                "DEEPSEEK_API_KEY": "file-key",
                "DEEPSEEK_BASE_URL": "https://file.invalid",
                "DEEPSEEK_MODEL": "file-model",
                "AGENT_DEFAULT_EXECUTION_BACKEND": "aliyun-sandbox",
            },
            environment,
        )
        self.assertEqual(settings, Settings(
            api_key="env-key", base_url="https://example.test/v1", model="deepseek-test"
        ))

    def test_model_settings_ignore_invalid_default_execution_backend(self):
        invalid = "SECRET-invalid-default-backend"
        settings, _ = self.load(
            {
                "DEEPSEEK_API_KEY": "fake",
                "AGENT_DEFAULT_EXECUTION_BACKEND": invalid,
            },
            {"AGENT_DEFAULT_EXECUTION_BACKEND": invalid},
        )
        self.assertEqual(settings, Settings(api_key="fake"))

    def test_default_execution_backend_loader_precedence_and_validation(self):
        backend, dotenv = self.load_backend({})
        self.assertEqual(backend, "local")
        args, kwargs = dotenv.call_args
        self.assertEqual(args[0], Path(__file__).resolve().parent.parent / ".env")
        self.assertEqual(kwargs, {"encoding": "utf-8-sig", "interpolate": False})

        for value in ("local", "aliyun-sandbox"):
            with self.subTest(value=value):
                backend, _ = self.load_backend({
                    "AGENT_DEFAULT_EXECUTION_BACKEND": value,
                })
                self.assertEqual(backend, value)

        backend, dotenv = self.load_backend(
            {"AGENT_DEFAULT_EXECUTION_BACKEND": "aliyun-sandbox"},
            {"AGENT_DEFAULT_EXECUTION_BACKEND": " local "},
        )
        self.assertEqual(backend, "local")
        dotenv.assert_not_called()

        invalid = "SECRET-invalid-default-backend"
        with self.assertRaises(ValueError) as caught:
            self.load_backend({
                "AGENT_DEFAULT_EXECUTION_BACKEND": invalid,
            })
        rendered = repr(caught.exception)
        self.assertEqual(
            str(caught.exception),
            "AGENT_DEFAULT_EXECUTION_BACKEND 必须是 local 或 aliyun-sandbox。",
        )
        self.assertNotIn(invalid, rendered)

        with self.assertRaises(ValueError) as environment_override:
            self.load_backend(
                {"AGENT_DEFAULT_EXECUTION_BACKEND": "aliyun-sandbox"},
                {"AGENT_DEFAULT_EXECUTION_BACKEND": invalid},
            )
        self.assertNotIn(invalid, repr(environment_override.exception))

        with self.assertRaises(ValueError) as non_text:
            self.load_backend({"AGENT_DEFAULT_EXECUTION_BACKEND": None})
        self.assertNotIn("None", repr(non_text.exception))

    def test_missing_or_non_text_api_key_is_rejected(self):
        for local in ({}, {"DEEPSEEK_API_KEY": None}, {"DEEPSEEK_API_KEY": " \n"}, {"DEEPSEEK_API_KEY": 123}):
            with self.subTest(local_type=type(local.get("DEEPSEEK_API_KEY")).__name__):
                with self.assertRaises(ValueError):
                    self.load(local)

    def test_invalid_base_urls_are_rejected(self):
        invalid = (
            "", "ftp://example.test", "https://", "https://user@example.test",
            "https://example.test?token=fake", "https://example.test#fragment",
            "https://example.test:bad", "https://example.test:70000",
            "https://exa mple.test", "//example.test/v1",
        )
        for base_url in invalid:
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                self.load({"DEEPSEEK_API_KEY": "fake", "DEEPSEEK_BASE_URL": base_url})

    def test_valid_http_urls_and_model_validation(self):
        for base_url in ("http://localhost:8000/v1", "https://api.example.test/v1"):
            with self.subTest(base_url=base_url):
                settings, _ = self.load({
                    "DEEPSEEK_API_KEY": "fake", "DEEPSEEK_BASE_URL": base_url,
                    "DEEPSEEK_MODEL": "model-1",
                })
                self.assertEqual(settings.base_url, base_url)
        for model in ("", " ", "bad model", "bad\nmodel", "x" * 201):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.load({"DEEPSEEK_API_KEY": "fake", "DEEPSEEK_MODEL": model})

    def test_removed_observability_mode_is_ignored(self):
        settings, _ = self.load(
            {"DEEPSEEK_API_KEY": "fake", "OBSERVABILITY_MODE": "legacy-value"}
        )
        self.assertEqual(settings, Settings(api_key="fake"))
        self.assertFalse(hasattr(settings, "observability_mode"))


if __name__ == "__main__":
    unittest.main()
