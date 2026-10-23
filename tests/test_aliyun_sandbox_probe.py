"""阿里云 Sandbox + OSS 探针的纯离线测试。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from pydantic import SecretStr, ValidationError

from app.aliyun_sandbox_probe import (
    _create_sandbox,
    build_oss_metadata,
    main,
    run_connectivity_probe,
)
from app.sandbox_config import (
    SANDBOX_ENV_NAMES,
    SandboxSettings,
    load_sandbox_settings,
)


def valid_settings(**changes: object) -> SandboxSettings:
    values: dict[str, object] = {
        "enabled": True,
        "api_key": SecretStr("fake-e2b-key"),
        "api_url": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        "domain": "cn-hangzhou.e2b.fc.aliyuncs.com",
        "template": "code-interpreter-v1",
        "timeout_seconds": 300,
        "oss_bucket": "agent-probe-bucket",
        "oss_endpoint": "https://oss-cn-hangzhou-internal.aliyuncs.com",
        "oss_bucket_path": "/projects/test/runs/connectivity-probe",
        "oss_mount_dir": "/mnt/oss",
        "oss_role_arn": "acs:ram::1234567890123456:role/agent-sandbox-probe",
        "oss_read_only": False,
    }
    values.update(changes)
    return SandboxSettings(**values)


class SandboxConfigTests(unittest.TestCase):
    def load(
        self,
        local: dict[str, object],
        environment: dict[str, str] | None = None,
    ) -> tuple[SandboxSettings, object]:
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if key not in SANDBOX_ENV_NAMES
        }
        clean_environment.update(environment or {})
        with (
            patch.dict(os.environ, clean_environment, clear=True),
            patch(
                "app.sandbox_config.dotenv_values", return_value=local
            ) as dotenv,
        ):
            settings = load_sandbox_settings()
        return settings, dotenv

    def test_disabled_defaults_do_not_require_cloud_configuration(self):
        settings, dotenv = self.load({})
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.recovery_probe_enabled)
        self.assertFalse(settings.allow_internet_access)
        self.assertIsNone(settings.api_key)
        self.assertEqual(settings.oss_mount_dir, "/mnt/oss")
        self.assertEqual(settings.timeout_seconds, 300)
        self.assertEqual(
            settings.runtime_venv_dir,
            "/opt/agnet-study/runtime/venv",
        )
        self.assertEqual(settings.runtime_user, "user")
        args, kwargs = dotenv.call_args
        self.assertEqual(args[0], Path(__file__).resolve().parent.parent / ".env")
        self.assertEqual(kwargs, {"encoding": "utf-8-sig", "interpolate": False})

    def test_environment_wins_and_secret_is_not_represented(self):
        local = {
            "ALIYUN_SANDBOX_PROBE_ENABLED": "true",
            "E2B_API_KEY": "local-fake-key",
            "E2B_API_URL": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
            "E2B_DOMAIN": "cn-hangzhou.e2b.fc.aliyuncs.com",
            "E2B_TEMPLATE": "code-interpreter-v1",
            "ALIYUN_OSS_BUCKET": "agent-probe-bucket",
            "ALIYUN_OSS_ENDPOINT": "https://oss-cn-hangzhou-internal.aliyuncs.com",
            "ALIYUN_OSS_BUCKET_PATH": "/projects/test/runs/probe",
            "ALIYUN_OSS_ROLE_ARN": (
                "acs:ram::1234567890123456:role/agent-sandbox-probe"
            ),
        }
        settings, _ = self.load(local, {"E2B_API_KEY": "environment-fake-key"})
        self.assertTrue(settings.enabled)
        self.assertEqual(
            settings.api_key.get_secret_value(),  # type: ignore[union-attr]
            "environment-fake-key",
        )
        self.assertNotIn("environment-fake-key", repr(settings))
        self.assertNotIn("environment-fake-key", settings.model_dump_json())

    def test_enabled_configuration_is_strict_and_region_bound(self):
        with self.assertRaises(ValidationError):
            SandboxSettings(enabled=True)
        with self.assertRaises(ValidationError):
            SandboxSettings(recovery_probe_enabled=True)
        for bucket in (
            "a",
            "ab",
            "a" * 64,
            "-abc",
            "abc-",
            "ABC",
            "ab_cd",
        ):
            with self.subTest(bucket=bucket), self.assertRaises(ValidationError):
                valid_settings(oss_bucket=bucket)
        self.assertEqual(valid_settings(oss_bucket="abc").oss_bucket, "abc")
        self.assertEqual(
            valid_settings(oss_bucket="a" * 63).oss_bucket,
            "a" * 63,
        )
        with self.assertRaises(ValidationError):
            valid_settings(oss_read_only=True)
        with self.assertRaises(ValidationError):
            valid_settings(oss_bucket_path="/")
        with self.assertRaises(ValidationError):
            valid_settings(oss_mount_dir="/workspace")
        with self.assertRaises(ValidationError):
            valid_settings(
                oss_endpoint="https://oss-cn-beijing-internal.aliyuncs.com"
            )
        with self.assertRaises(ValidationError):
            valid_settings(api_url="https://api.e2b.dev")
        for runtime_path in (
            "/workspace/runtime",
            "/mnt/oss/runtime",
            "/",
            "relative/runtime",
        ):
            with self.subTest(runtime_path=runtime_path), self.assertRaises(
                ValidationError
            ):
                valid_settings(runtime_venv_dir=runtime_path)
        for runtime_user in ("root", "0", "BadUser", "has space", ""):
            with self.subTest(runtime_user=runtime_user), self.assertRaises(
                ValidationError
            ):
                valid_settings(runtime_user=runtime_user)

    def test_loader_rejects_ambiguous_boolean_and_non_integer_timeout(self):
        with self.assertRaises(ValueError):
            self.load({"ALIYUN_SANDBOX_PROBE_ENABLED": "1"})
        for raw in ("1", "yes", "", "enabled"):
            with self.subTest(allow_internet_access=raw), self.assertRaises(
                ValueError
            ):
                self.load({"ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS": raw})
        with self.assertRaises(ValueError):
            self.load({"ALIYUN_SANDBOX_TIMEOUT_SECONDS": "3.5"})

    def test_internet_access_is_strict_and_explicit_boolean_only(self):
        self.assertFalse(SandboxSettings().allow_internet_access)
        self.assertFalse(valid_settings().allow_internet_access)
        self.assertTrue(
            valid_settings(allow_internet_access=True).allow_internet_access
        )
        for invalid in (0, 1, "true", "false", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                valid_settings(allow_internet_access=invalid)

        disabled, _ = self.load({
            "ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS": "false"
        })
        enabled, _ = self.load({
            "ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS": "true"
        })
        self.assertFalse(disabled.allow_internet_access)
        self.assertTrue(enabled.allow_internet_access)


class SandboxProbeTests(unittest.TestCase):
    def test_metadata_contains_stringified_oss_configuration_without_api_key(self):
        settings = valid_settings()
        metadata = build_oss_metadata(settings)
        self.assertEqual(
            set(metadata),
            {"fc.sandbox.storage.oss", "fc.sandbox.auth.role"},
        )
        self.assertIsInstance(metadata["fc.sandbox.storage.oss"], str)
        mount = json.loads(metadata["fc.sandbox.storage.oss"])["mountPoints"][0]
        self.assertEqual(
            mount,
            {
                "bucketName": "agent-probe-bucket",
                "mountDir": "/mnt/oss",
                "bucketPath": "/projects/test/runs/connectivity-probe",
                "endpoint": "https://oss-cn-hangzhou-internal.aliyuncs.com",
                "readOnly": False,
            },
        )
        self.assertNotIn("fake-e2b-key", repr(metadata))

    def test_sdk_factory_forwards_only_validated_options(self):
        captured: dict[str, object] = {}
        fake_sandbox = object()

        class FakeSandboxType:
            @classmethod
            def create(cls, **options: object) -> object:
                captured.update(options)
                return fake_sandbox

        fake_e2b = ModuleType("e2b")
        fake_e2b.Sandbox = FakeSandboxType  # type: ignore[attr-defined]
        settings = valid_settings()
        metadata = build_oss_metadata(settings)
        with patch.dict(sys.modules, {"e2b": fake_e2b}):
            created = _create_sandbox(settings, metadata)

        self.assertIs(created, fake_sandbox)
        self.assertEqual(
            captured,
            {
                "api_key": "fake-e2b-key",
                "api_url": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
                "domain": "cn-hangzhou.e2b.fc.aliyuncs.com",
                "timeout": 300,
                "metadata": metadata,
                "allow_internet_access": False,
                "template": "code-interpreter-v1",
            },
        )

    def test_sdk_factory_can_explicitly_enable_internet_access(self):
        captured: dict[str, object] = {}

        class FakeSandboxType:
            @classmethod
            def create(cls, **options: object) -> object:
                captured.update(options)
                return object()

        fake_e2b = ModuleType("e2b")
        fake_e2b.Sandbox = FakeSandboxType  # type: ignore[attr-defined]
        settings = valid_settings(allow_internet_access=True)
        with patch.dict(sys.modules, {"e2b": fake_e2b}):
            _create_sandbox(settings, build_oss_metadata(settings))
        self.assertIs(captured["allow_internet_access"], True)

    def test_success_writes_reads_removes_marker_and_kills_sandbox(self):
        class FakeFiles:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}
                self.removed: list[str] = []

            def write(self, path: str, value: str) -> None:
                self.values[path] = value

            def read(self, path: str) -> str:
                return self.values[path]

            def remove(self, path: str) -> None:
                self.removed.append(path)
                self.values.pop(path)

        class FakeSandbox:
            sandbox_id = "sandbox-test-id"

            def __init__(self) -> None:
                self.files = FakeFiles()
                self.killed = False

            def kill(self) -> None:
                self.killed = True

        sandbox = FakeSandbox()
        captured_metadata: dict[str, str] = {}

        def factory(
            settings: SandboxSettings, metadata: dict[str, str]
        ) -> FakeSandbox:
            self.assertEqual(settings, valid_settings())
            captured_metadata.update(metadata)
            return sandbox

        result = run_connectivity_probe(
            valid_settings(),
            sandbox_factory=factory,
            token_factory=lambda: "a" * 32,
        )
        expected_path = "/mnt/oss/.agnet-study-probe-" + "a" * 32 + ".txt"
        self.assertEqual(result.sandbox_id, "sandbox-test-id")
        self.assertEqual(result.marker_path, expected_path)
        self.assertTrue(result.marker_removed)
        self.assertEqual(sandbox.files.removed, [expected_path])
        self.assertTrue(sandbox.killed)
        self.assertIn("fc.sandbox.storage.oss", captured_metadata)

    def test_read_failure_still_attempts_marker_cleanup_and_kills_sandbox(self):
        class FailingFiles:
            def __init__(self) -> None:
                self.removed = False

            def write(self, path: str, value: str) -> None:
                del path, value

            def read(self, path: str) -> str:
                del path
                raise OSError("synthetic read failure")

            def remove(self, path: str) -> None:
                del path
                self.removed = True

        class FakeSandbox:
            sandbox_id = "sandbox-test-id"

            def __init__(self) -> None:
                self.files = FailingFiles()
                self.killed = False

            def kill(self) -> None:
                self.killed = True

        sandbox = FakeSandbox()
        with self.assertRaisesRegex(OSError, "synthetic read failure"):
            run_connectivity_probe(
                valid_settings(),
                sandbox_factory=lambda settings, metadata: sandbox,
                token_factory=lambda: "b" * 32,
            )
        self.assertTrue(sandbox.files.removed)
        self.assertTrue(sandbox.killed)

    def test_write_response_failure_still_attempts_remove_and_kill(self):
        class UncertainWriteFiles:
            def __init__(self) -> None:
                self.remove_paths: list[str] = []

            def write(self, path: str, value: str) -> None:
                del path, value
                raise TimeoutError("synthetic response loss after remote write")

            def remove(self, path: str) -> None:
                self.remove_paths.append(path)
                raise OSError("synthetic cleanup failure")

        class FakeSandbox:
            sandbox_id = "sandbox-test-id"

            def __init__(self) -> None:
                self.files = UncertainWriteFiles()
                self.killed = False

            def kill(self) -> None:
                self.killed = True

        sandbox = FakeSandbox()
        with self.assertRaisesRegex(
            TimeoutError, "synthetic response loss"
        ) as caught:
            run_connectivity_probe(
                valid_settings(),
                sandbox_factory=lambda settings, metadata: sandbox,
                token_factory=lambda: "c" * 32,
            )
        self.assertEqual(
            sandbox.files.remove_paths,
            ["/mnt/oss/.agnet-study-probe-" + "c" * 32 + ".txt"],
        )
        self.assertTrue(
            any("marker 清理也失败" in note for note in caught.exception.__notes__)
        )
        self.assertTrue(sandbox.killed)

    def test_disabled_settings_fail_before_factory_is_called(self):
        called = False

        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> object:
            nonlocal called
            called = True
            return object()

        with self.assertRaisesRegex(RuntimeError, "默认关闭"):
            run_connectivity_probe(SandboxSettings(), sandbox_factory=factory)
        self.assertFalse(called)

    def test_execute_with_disabled_settings_never_reaches_sdk_factory(self):
        stderr = io.StringIO()
        with (
            patch("sys.stderr", stderr),
            patch(
                "app.aliyun_sandbox_probe.load_sandbox_settings",
                return_value=SandboxSettings(),
            ),
            patch("app.aliyun_sandbox_probe._create_sandbox") as create_sandbox,
        ):
            exit_code = main(["--execute"])
        self.assertEqual(exit_code, 1)
        self.assertIn("默认关闭", stderr.getvalue())
        create_sandbox.assert_not_called()

    def test_cli_validation_error_hides_the_rejected_input(self):
        try:
            SandboxSettings.model_validate(
                {"enabled": "raw-value-that-must-not-leak"}
            )
        except ValidationError as error:
            validation_error = error
        else:  # pragma: no cover - StrictBool 必须拒绝该值。
            self.fail("expected ValidationError")

        stderr = io.StringIO()
        with (
            patch("sys.stderr", stderr),
            patch(
                "app.aliyun_sandbox_probe.load_sandbox_settings",
                side_effect=validation_error,
            ),
        ):
            exit_code = main(["--execute"])
        self.assertEqual(exit_code, 1)
        self.assertNotIn("raw-value-that-must-not-leak", stderr.getvalue())
        self.assertNotIn("input_value", stderr.getvalue())

    def test_cli_without_execute_is_a_safe_noop(self):
        stdout = io.StringIO()
        with (
            patch("sys.stdout", stdout),
            patch(
                "app.aliyun_sandbox_probe.load_sandbox_settings"
            ) as load_settings,
            patch(
                "app.aliyun_sandbox_probe.run_connectivity_probe"
            ) as run_probe,
        ):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("未执行真实云调用", stdout.getvalue())
        load_settings.assert_not_called()
        run_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
