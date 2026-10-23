"""阿里云 Sandbox 跨实例 OSS 恢复探针的纯离线测试。"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from pydantic import SecretStr

from app.aliyun_sandbox_recovery_probe import (
    SandboxRecoveryProbeError,
    main,
    parse_checkpoint_manifest,
    run_recovery_probe,
)
from app.sandbox_config import SandboxProbeDisabledError, SandboxSettings


def recovery_settings(**changes: object) -> SandboxSettings:
    values: dict[str, object] = {
        "enabled": True,
        "recovery_probe_enabled": True,
        "api_key": SecretStr("fake-e2b-key"),
        "api_url": "https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        "domain": "cn-hangzhou.e2b.fc.aliyuncs.com",
        "template": "code-interpreter-v1",
        "timeout_seconds": 300,
        "oss_bucket": "agent-probe-bucket",
        "oss_endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
        "oss_bucket_path": "/projects/test/runs/connectivity-probe",
        "oss_mount_dir": "/mnt/oss",
        "oss_role_arn": "acs:ram::1234567890123456:role/agent-sandbox-probe",
        "oss_read_only": False,
    }
    values.update(changes)
    return SandboxSettings(**values)


class FakeFilesystem:
    def __init__(
        self,
        shared_oss: dict[str, bytes],
        local_files: dict[str, bytes],
        events: list[str],
        name: str,
    ) -> None:
        self.shared_oss = shared_oss
        self.local_files = local_files
        self.events = events
        self.name = name

    def _store(self, path: str) -> dict[str, bytes]:
        return self.shared_oss if path.startswith("/mnt/oss/") else self.local_files

    def write(self, path: str, value: str | bytes) -> None:
        self.events.append(f"write:{self.name}:{path}")
        self._store(path)[path] = value.encode("utf-8") if isinstance(value, str) else value

    def read(self, path: str, format: str = "text") -> str | bytes:
        self.events.append(f"read:{self.name}:{path}")
        value = self._store(path)[path]
        return value if format == "bytes" else value.decode("utf-8")

    def exists(self, path: str) -> bool:
        store = self._store(path)
        prefix = path.rstrip("/") + "/"
        return path in store or any(item.startswith(prefix) for item in store)

    def remove(self, path: str) -> None:
        self.events.append(f"remove:{self.name}:{path}")
        store = self._store(path)
        prefix = path.rstrip("/") + "/"
        for item in tuple(store):
            if item == path or item.startswith(prefix):
                del store[item]


class FakeSandbox:
    def __init__(
        self,
        name: str,
        shared_oss: dict[str, bytes],
        events: list[str],
    ) -> None:
        self.name = name
        self.sandbox_id = f"sandbox-{name}"
        self.local_files: dict[str, bytes] = {}
        self.files = FakeFilesystem(shared_oss, self.local_files, events, name)
        self.events = events
        self.killed = False

    def kill(self) -> bool:
        self.events.append(f"kill:{self.name}")
        self.killed = True
        return True


class SandboxRecoveryManifestTests(unittest.TestCase):
    def valid_manifest_data(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "probe_id": "a" * 32,
            "files": [
                {
                    "path": "src/main.py",
                    "size": 3,
                    "sha256": hashlib.sha256(b"abc").hexdigest(),
                }
            ],
        }

    def test_manifest_rejects_tampering_and_duplicate_json_fields(self):
        tampered = self.valid_manifest_data()
        tampered["unexpected"] = "must-fail"
        with self.assertRaisesRegex(SandboxRecoveryProbeError, "严格校验失败"):
            parse_checkpoint_manifest(json.dumps(tampered))

        duplicate = (
            '{"schema_version":1,"schema_version":1,"probe_id":"'
            + "a" * 32
            + '","files":[]}'
        )
        with self.assertRaisesRegex(SandboxRecoveryProbeError, "严格校验失败"):
            parse_checkpoint_manifest(duplicate)

    def test_manifest_rejects_path_traversal(self):
        traversing = self.valid_manifest_data()
        files = traversing["files"]
        assert isinstance(files, list)
        assert isinstance(files[0], dict)
        files[0]["path"] = "../outside.txt"
        with self.assertRaisesRegex(SandboxRecoveryProbeError, "严格校验失败"):
            parse_checkpoint_manifest(json.dumps(traversing))

    def test_manifest_requires_sorted_unique_paths(self):
        data = self.valid_manifest_data()
        files = data["files"]
        assert isinstance(files, list)
        files.append(dict(files[0]))  # type: ignore[arg-type]
        with self.assertRaisesRegex(SandboxRecoveryProbeError, "严格校验失败"):
            parse_checkpoint_manifest(json.dumps(data))


class SandboxRecoveryProbeTests(unittest.TestCase):
    def make_factory(
        self,
        shared: dict[str, bytes],
        events: list[str],
        *,
        before_b: object | None = None,
    ) -> tuple[object, list[FakeSandbox]]:
        sandboxes: list[FakeSandbox] = []

        def factory(
            settings: SandboxSettings,
            metadata: dict[str, str],
        ) -> FakeSandbox:
            self.assertTrue(settings.recovery_probe_enabled)
            self.assertIn("fc.sandbox.storage.oss", metadata)
            name = "A" if not sandboxes else "B"
            if name == "B" and callable(before_b):
                before_b()
            events.append(f"create:{name}")
            sandbox = FakeSandbox(name, shared, events)
            sandboxes.append(sandbox)
            return sandbox

        return factory, sandboxes

    def test_success_kills_a_before_creating_b_and_restores_local_workspace(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []
        factory, sandboxes = self.make_factory(shared, events)

        result = run_recovery_probe(
            recovery_settings(),
            sandbox_factory=factory,  # type: ignore[arg-type]
            token_factory=lambda: "b" * 32,
        )

        self.assertLess(events.index("kill:A"), events.index("create:B"))
        self.assertEqual(result.source_sandbox_id, "sandbox-A")
        self.assertEqual(result.restored_sandbox_id, "sandbox-B")
        self.assertEqual(result.file_count, 3)
        self.assertTrue(result.source_killed_before_restore)
        self.assertTrue(result.checkpoint_removed)
        self.assertTrue(result.restored_sandbox_killed)
        self.assertEqual(shared, {})
        self.assertEqual(len(sandboxes), 2)
        restored_paths = {
            path for path in sandboxes[1].local_files if "/restored-" in path
        }
        self.assertEqual(len(restored_paths), 3)
        self.assertTrue(sandboxes[0].killed)
        self.assertTrue(sandboxes[1].killed)

    def test_hash_mismatch_in_b_cleans_checkpoint_and_kills_b(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []

        def corrupt_checkpoint() -> None:
            target = next(path for path in shared if path.endswith("/src/main.py"))
            shared[target] = b"tampered after sandbox A was killed"

        factory, sandboxes = self.make_factory(
            shared,
            events,
            before_b=corrupt_checkpoint,
        )
        with self.assertRaisesRegex(
            SandboxRecoveryProbeError,
            "size 或 sha256",
        ):
            run_recovery_probe(
                recovery_settings(),
                sandbox_factory=factory,  # type: ignore[arg-type]
                token_factory=lambda: "c" * 32,
            )

        self.assertLess(events.index("kill:A"), events.index("create:B"))
        self.assertEqual(shared, {})
        self.assertEqual(len(sandboxes), 2)
        self.assertTrue(sandboxes[0].killed)
        self.assertTrue(sandboxes[1].killed)
        self.assertTrue(any(event.startswith("remove:B:") for event in events))

    def test_consistently_tampered_file_and_manifest_are_rejected(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []

        def tamper_file_and_manifest() -> None:
            target = next(path for path in shared if path.endswith("/src/main.py"))
            manifest_path = next(
                path for path in shared if path.endswith("/manifest.json")
            )
            tampered = b"safe path but consistently tampered content\n"
            shared[target] = tampered
            manifest = json.loads(shared[manifest_path].decode("utf-8"))
            for entry in manifest["files"]:
                if entry["path"] == "src/main.py":
                    entry["size"] = len(tampered)
                    entry["sha256"] = hashlib.sha256(tampered).hexdigest()
            shared[manifest_path] = json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

        factory, sandboxes = self.make_factory(
            shared,
            events,
            before_b=tamper_file_and_manifest,
        )
        with self.assertRaisesRegex(
            SandboxRecoveryProbeError,
            "原始 manifest 不一致",
        ):
            run_recovery_probe(
                recovery_settings(),
                sandbox_factory=factory,  # type: ignore[arg-type]
                token_factory=lambda: "d" * 32,
            )

        self.assertEqual(shared, {})
        self.assertEqual(len(sandboxes), 2)
        self.assertTrue(sandboxes[0].killed)
        self.assertTrue(sandboxes[1].killed)

    def test_safe_path_and_matching_size_hash_manifest_tampering_is_rejected(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []

        def tamper_safe_path_and_manifest() -> None:
            old_target = next(path for path in shared if path.endswith("/src/main.py"))
            manifest_path = next(
                path for path in shared if path.endswith("/manifest.json")
            )
            new_target = old_target.removesuffix("/src/main.py") + "/src/other.py"
            tampered = b"safe path with matching metadata\n"
            del shared[old_target]
            shared[new_target] = tampered
            manifest = json.loads(shared[manifest_path].decode("utf-8"))
            for entry in manifest["files"]:
                if entry["path"] == "src/main.py":
                    entry["path"] = "src/other.py"
                    entry["size"] = len(tampered)
                    entry["sha256"] = hashlib.sha256(tampered).hexdigest()
            shared[manifest_path] = json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

        factory, sandboxes = self.make_factory(
            shared,
            events,
            before_b=tamper_safe_path_and_manifest,
        )
        with self.assertRaisesRegex(
            SandboxRecoveryProbeError,
            "原始 manifest 不一致",
        ):
            run_recovery_probe(
                recovery_settings(),
                sandbox_factory=factory,  # type: ignore[arg-type]
                token_factory=lambda: "8" * 32,
            )

        self.assertEqual(shared, {})
        self.assertEqual(len(sandboxes), 2)
        self.assertTrue(all(sandbox.killed for sandbox in sandboxes))

    def test_b_create_failure_uses_one_cleanup_sandbox_after_a_is_killed(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []
        sandboxes: list[FakeSandbox] = []
        calls = 0

        def factory(
            settings: SandboxSettings,
            metadata: dict[str, str],
        ) -> FakeSandbox:
            nonlocal calls
            del settings, metadata
            calls += 1
            if calls == 1:
                events.append("create:A")
                sandbox = FakeSandbox("A", shared, events)
                sandboxes.append(sandbox)
                return sandbox
            if calls == 2:
                events.append("create:B:failed")
                raise OSError("synthetic sandbox B create failure")
            if calls == 3:
                events.append("create:C")
                sandbox = FakeSandbox("C", shared, events)
                sandboxes.append(sandbox)
                return sandbox
            raise AssertionError("cleanup sandbox creation retried more than once")

        with self.assertRaisesRegex(OSError, "sandbox B create failure"):
            run_recovery_probe(
                recovery_settings(),
                sandbox_factory=factory,
                token_factory=lambda: "e" * 32,
            )

        self.assertEqual(calls, 3)
        self.assertLess(events.index("kill:A"), events.index("create:B:failed"))
        self.assertLess(events.index("create:B:failed"), events.index("create:C"))
        self.assertTrue(any(event.startswith("remove:C:") for event in events))
        self.assertIn("kill:C", events)
        self.assertEqual(shared, {})
        self.assertTrue(all(sandbox.killed for sandbox in sandboxes))

    def test_cleanup_sandbox_remove_failure_reports_possible_residual_checkpoint(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []
        sandboxes: list[FakeSandbox] = []
        calls = 0

        def factory(
            settings: SandboxSettings,
            metadata: dict[str, str],
        ) -> FakeSandbox:
            nonlocal calls
            del settings, metadata
            calls += 1
            if calls == 1:
                events.append("create:A")
                sandbox = FakeSandbox("A", shared, events)
                sandboxes.append(sandbox)
                return sandbox
            if calls == 2:
                events.append("create:B:failed")
                raise OSError("synthetic B failure")
            if calls == 3:
                events.append("create:C")
                sandbox = FakeSandbox("C", shared, events)

                def fail_remove(path: str) -> None:
                    events.append(f"remove-failed:C:{path}")
                    raise OSError("synthetic cleanup remove failure")

                sandbox.files.remove = fail_remove  # type: ignore[method-assign]
                sandboxes.append(sandbox)
                return sandbox
            raise AssertionError("cleanup sandbox creation retried more than once")

        token = "7" * 32
        with self.assertRaisesRegex(
            SandboxRecoveryProbeError,
            "checkpoint_path=/mnt/oss/.agnet-study-recovery-probe-" + token,
        ) as caught:
            run_recovery_probe(
                recovery_settings(),
                sandbox_factory=factory,
                token_factory=lambda: token,
            )

        self.assertEqual(calls, 3)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(shared)
        self.assertIn("kill:C", events)
        self.assertTrue(all(sandbox.killed for sandbox in sandboxes))

    def test_failed_b_and_cleanup_c_creation_reports_residual_checkpoint(self):
        shared: dict[str, bytes] = {}
        events: list[str] = []
        calls = 0

        def factory(
            settings: SandboxSettings,
            metadata: dict[str, str],
        ) -> FakeSandbox:
            nonlocal calls
            del settings, metadata
            calls += 1
            if calls == 1:
                events.append("create:A")
                return FakeSandbox("A", shared, events)
            if calls == 2:
                events.append("create:B:failed")
                raise OSError("synthetic B failure")
            if calls == 3:
                events.append("create:C:failed")
                raise ConnectionError("synthetic C failure")
            raise AssertionError("cleanup sandbox creation retried more than once")

        token = "f" * 32
        with self.assertRaisesRegex(
            SandboxRecoveryProbeError,
            "checkpoint_path=/mnt/oss/.agnet-study-recovery-probe-" + token,
        ) as caught:
            run_recovery_probe(
                recovery_settings(),
                sandbox_factory=factory,
                token_factory=lambda: token,
            )

        self.assertEqual(calls, 3)
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertTrue(shared)
        self.assertEqual(
            events[:4],
            ["create:A", events[1], events[2], events[3]],
        )
        self.assertLess(events.index("kill:A"), events.index("create:B:failed"))
        self.assertLess(
            events.index("create:B:failed"), events.index("create:C:failed")
        )

    def test_disabled_recovery_fails_before_factory(self):
        called = False

        def factory(settings: SandboxSettings, metadata: dict[str, str]) -> object:
            nonlocal called
            called = True
            return object()

        with self.assertRaisesRegex(SandboxProbeDisabledError, "恢复探针默认关闭"):
            run_recovery_probe(
                recovery_settings(recovery_probe_enabled=False),
                sandbox_factory=factory,
            )
        self.assertFalse(called)

    def test_cli_without_execute_is_noop_and_cannot_import_e2b(self):
        stdout = io.StringIO()
        original_import = builtins.__import__

        def guarded_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "e2b" or name.startswith("e2b."):
                raise AssertionError("default recovery probe imported e2b")
            return original_import(name, *args, **kwargs)

        with (
            patch("sys.stdout", stdout),
            patch("builtins.__import__", side_effect=guarded_import),
            patch(
                "app.aliyun_sandbox_recovery_probe.load_sandbox_settings"
            ) as load_settings,
            patch(
                "app.aliyun_sandbox_recovery_probe.run_recovery_probe"
            ) as run_probe,
        ):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("未执行真实云恢复探针", stdout.getvalue())
        load_settings.assert_not_called()
        run_probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
