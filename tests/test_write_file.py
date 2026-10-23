"""write_file 的写入语义、JSON 协议和项目目录权限测试。"""

from dataclasses import replace
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.schemas import ToolMessage, WriteFileArguments, WriteFileOutput
from app.workflow import FileApprovalWorkflow
from tool.registry import TOOLSCHEMAS, call_tool


access = importlib.import_module("tool.file_access")
registry = importlib.import_module("tool.registry")
writer = importlib.import_module("tool.write_file")


class _TrackedStream:
    """确认句柄权限复核之前没有发生读写或定位操作。"""

    def __init__(self, stream):
        self.stream = stream
        self.read_count = 0
        self.write_count = 0
        self.seek_count = 0
        self.truncate_count = 0

    def fileno(self):
        return self.stream.fileno()

    def read(self, size=-1):
        self.read_count += 1
        return self.stream.read(size)

    def write(self, raw):
        self.write_count += 1
        return self.stream.write(raw)

    def seek(self, offset, whence=0):
        self.seek_count += 1
        return self.stream.seek(offset, whence)

    def truncate(self, size=None):
        self.truncate_count += 1
        return self.stream.truncate(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class WriteFileTests(unittest.TestCase):
    def setUp(self):
        test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(
            prefix="write-file-tests-", dir=test_directory
        )
        self.fixture = Path(self.temporary.name).resolve()
        self.root = self.fixture / "project"
        self.outside = self.fixture / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        self.root_patch = patch.object(access, "_PROJECT_ROOT", self.root)
        self.root_patch.start()
        self.links: list[Path] = []

    def tearDown(self):
        self.root_patch.stop()
        for link in self.links:
            if os.path.lexists(link):
                if link.is_symlink():
                    link.unlink()
                elif link.is_dir():
                    os.rmdir(link)
                else:
                    link.unlink()
        self.temporary.cleanup()

    def invoke(self, arguments=None, name="write_file") -> dict:
        if arguments is None:
            arguments = {"path": "new.txt", "content": "hello", "mode": "create"}
        raw = call_tool({"id": "write_1", "name": name, "arguments": arguments})
        message = ToolMessage.model_validate_json(raw)
        self.assertEqual(message.tool_call_id, "write_1")
        return json.loads(raw)["result"]

    def invoke_approved(self, arguments=None, name="write_file") -> dict:
        if arguments is None:
            arguments = {"path": "new.txt", "content": "hello", "mode": "create"}
        raw = call_tool(
            {"id": "write_1", "name": name, "arguments": arguments},
            confirmation_callback=lambda request: True,
            workflow=FileApprovalWorkflow(),
        )
        message = ToolMessage.model_validate_json(raw)
        self.assertEqual(message.tool_call_id, "write_1")
        return json.loads(raw)["result"]

    def assert_failure(self, result: dict, status="error"):
        self.assertEqual(result["status"], status)
        self.assertEqual(result["output"], "")
        self.assertTrue(result["error"])
        self.assertNotIn(str(self.fixture), result["error"])
        self.assertNotIn("OUTSIDE_SECRET", result["error"])

    def test_missing_workflow_or_confirmation_cannot_modify(self):
        target = self.root / "new.txt"
        self.assert_failure(self.invoke(), "denied")
        self.assertFalse(target.exists())
        raw = call_tool(
            {
                "id": "write_1",
                "name": "write_file",
                "arguments": {"path": "new.txt", "content": "hello", "mode": "create"},
            },
            confirmation_callback=lambda request: True,
        )
        self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "denied")
        self.assertFalse(target.exists())

    def test_create_nested_json_at_exact_path_and_return_metadata(self):
        (self.root / "data").mkdir()
        content = '{\r\n  "city": "广州",\r\n  "temperature": 28\r\n}\r\n'
        result = self.invoke_approved({
            "path": "data/weather.json",
            "content": content,
            "mode": "create",
        })
        raw = content.encode("utf-8")
        target = self.root / "data/weather.json"
        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["city"], "广州")
        self.assertEqual(result["output"], {
            "path": "data/weather.json",
            "encoding": "utf-8",
            "operation": "created",
            "bytes_written": len(raw),
            "sha256": sha256(raw).hexdigest(),
        })

    def test_create_python_preserves_crlf_chinese_and_compiles(self):
        (self.root / "app").mkdir()
        content = '# 说明\r\ndef greet():\r\n    return "你好"\r\n'
        result = self.invoke_approved({"path": "app/generated.py", "content": content, "mode": "create"})
        target = self.root / "app/generated.py"
        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_bytes(), content.encode("utf-8"))
        compile(target.read_text(encoding="utf-8"), str(target), "exec")

    def test_empty_and_bom_files_are_exact(self):
        empty = self.invoke_approved({"path": "empty.txt", "content": "", "mode": "create"})
        self.assertEqual(empty["status"], "success")
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"")
        self.assertEqual(empty["output"]["bytes_written"], 0)

        bom = self.invoke_approved({
            "path": "bom.md", "content": "标题\n", "mode": "create", "encoding": "utf-8-sig"
        })
        expected = "标题\n".encode("utf-8-sig")
        self.assertEqual((self.root / "bom.md").read_bytes(), expected)
        self.assertEqual(bom["output"]["encoding"], "utf-8-sig")
        self.assertEqual(bom["output"]["bytes_written"], len(expected))

    def test_overwrite_replaces_entire_file_and_truncates_old_tail(self):
        target = self.root / "existing.txt"
        target.write_bytes(b"new plus a very long stale tail")
        result = self.invoke_approved({"path": "existing.txt", "content": "短", "mode": "overwrite"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["operation"], "overwritten")
        self.assertEqual(target.read_bytes(), "短".encode("utf-8"))

    def test_modes_keep_create_and_overwrite_semantics_and_create_parents(self):
        existing = self.root / "existing.txt"
        existing.write_bytes(b"keep")
        self.assert_failure(self.invoke({
            "path": "existing.txt", "content": "replace", "mode": "create"
        }))
        self.assertEqual(existing.read_bytes(), b"keep")

        missing = self.root / "missing.txt"
        self.assert_failure(self.invoke({
            "path": "missing.txt", "content": "new", "mode": "overwrite"
        }))
        self.assertFalse(missing.exists())

        nested = self.root / "missing-dir/new.txt"
        denied = self.invoke({
            "path": "missing-dir/new.txt", "content": "new", "mode": "create"
        })
        self.assert_failure(denied, "denied")
        self.assertFalse(nested.parent.exists())
        created = self.invoke_approved({
            "path": "missing-dir/deeper/new.txt", "content": "new", "mode": "create"
        })
        self.assertEqual(created["status"], "success")
        self.assertEqual((self.root / "missing-dir/deeper/new.txt").read_bytes(), b"new")

    def test_nested_create_failure_removes_only_new_empty_parents(self):
        existing = self.root / "existing"
        existing.mkdir()
        with patch.object(writer, "_write_all", side_effect=OSError("simulated failure")):
            result = self.invoke_approved({
                "path": "existing/new-a/new-b/file.txt", "content": "new", "mode": "create"
            })
        self.assert_failure(result)
        self.assertTrue(existing.is_dir())
        self.assertFalse((existing / "new-a").exists())

    def test_parent_creation_happens_only_after_exact_approval(self):
        workflow = FileApprovalWorkflow()
        seen = []

        def reject(request):
            seen.append((request.operation, request.paths))
            self.assertFalse((self.root / "a").exists())
            return False

        raw = call_tool(
            {"id": "write_1", "name": "write_file", "arguments": {
                "path": "a/b/file.txt", "content": "x", "mode": "create"
            }},
            confirmation_callback=reject,
            workflow=workflow,
        )
        self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "denied")
        self.assertEqual(seen, [("write_create", ("a/b/file.txt",))])
        self.assertFalse((self.root / "a").exists())

    def test_create_second_no_shows_diff_keeps_candidate_and_creates_no_target_parents(self):
        requests = []

        def confirm(request):
            requests.append(request)
            return request.phase == "operation"

        raw = call_tool(
            {"id": "write_1", "name": "write_file", "arguments": {
                "path": "future/tree/file.txt", "content": "完整候选\n", "mode": "create"
            }},
            confirmation_callback=confirm,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "denied")
        self.assertFalse((self.root / "future").exists())
        self.assertEqual([request.phase for request in requests], ["operation", "diff"])
        self.assertIn("+完整候选", requests[1].diff_preview)
        self.assertEqual(
            (self.root / requests[1].pending_file).read_bytes(),
            "完整候选\n".encode("utf-8"),
        )

    def test_success_removes_its_candidate_after_verified_commit(self):
        requests = []
        raw = call_tool(
            {"id": "write_1", "name": "write_file", "arguments": {
                "path": "approved.txt", "content": "approved", "mode": "create"
            }},
            confirmation_callback=lambda request: requests.append(request) or True,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "success")
        diff_request = next(request for request in requests if request.phase == "diff")
        self.assertFalse((self.root / diff_request.pending_file).exists())

    def test_create_under_initially_missing_data_directory_survives_internal_staging(self):
        self.assertFalse((self.root / "data").exists())
        result = self.invoke_approved({
            "path": "data/result.txt", "content": "result", "mode": "create"
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual((self.root / "data/result.txt").read_bytes(), b"result")

    def test_root_is_independent_of_working_directory(self):
        previous = Path.cwd()
        try:
            os.chdir(self.outside)
            result = self.invoke_approved()
        finally:
            os.chdir(previous)
        self.assertEqual(result["status"], "success")
        self.assertEqual((self.root / "new.txt").read_text(encoding="utf-8"), "hello")

    def test_sensitive_paths_cannot_be_created_or_overwritten(self):
        for path in (
            ".env", ".env.local", ".git/config", ".venv/config",
            ".agent/tool_policy.json",
        ):
            with self.subTest(path=path):
                target = self.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"KEEP")
                result = self.invoke({
                    "path": path, "content": "bad", "mode": "overwrite"
                })
                self.assert_failure(result, "denied")
                self.assertEqual(target.read_bytes(), b"KEEP")
        result = self.invoke({
            "path": ".env.created", "content": "bad", "mode": "create"
        })
        self.assert_failure(result, "denied")
        self.assertFalse((self.root / ".env.created").exists())

    def test_invalid_and_extra_arguments_never_reach_handler(self):
        invalid = [
            {},
            {"path": "x", "content": "x"},
            {"path": 1, "content": "x", "mode": "create"},
            {"path": "x", "content": 1, "mode": "create"},
            {"path": "x", "content": "x", "mode": "append"},
            {"path": "x", "content": "x", "mode": "create", "encoding": "ascii"},
            {"path": "x", "content": "a\x00b", "mode": "create"},
            {"path": "x", "content": "x", "mode": "create", "root": str(self.outside)},
            {"path": "x", "content": "x", "mode": "create", "create_parents": True},
            {"path": "x", "content": "x", "mode": "create", "append": True},
            {"path": "x", "content": "x", "mode": "create", "unknown": True},
        ]
        handler = Mock(side_effect=AssertionError("must not execute"))
        entry = replace(registry._REGISTRY["write_file"], handler=handler)
        with patch.dict(registry._REGISTRY, {"write_file": entry}):
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    self.assert_failure(self.invoke(arguments))
        handler.assert_not_called()

    def test_tool_name_must_match_exactly(self):
        handler = Mock(side_effect=AssertionError("must not execute"))
        entry = replace(registry._REGISTRY["write_file"], handler=handler)
        with patch.dict(registry._REGISTRY, {"write_file": entry}):
            for name in ("WRITE_FILE", "Write_File", "write-file", "create_file"):
                with self.subTest(name=name):
                    self.assert_failure(self.invoke(name=name))
        handler.assert_not_called()

    def test_unsafe_paths_are_denied_before_resolution_or_open(self):
        paths = [
            "../outside.txt", "nested/../../outside.txt", "nested\\..\\outside.txt",
            str(self.outside / "secret.txt"), "C:/Windows/win.ini", "C:secret.txt",
            "\\Windows\\win.ini", "/etc/passwd", "\\\\server\\share\\secret.txt",
            "file.txt:stream", "NUL", "CON.txt", "file.txt.", "file.txt ",
            "bad\x00file", "bad\nfile", ".", "..", "./file", "nested//file",
        ]
        with patch.object(Path, "resolve", side_effect=AssertionError("must not resolve")) as resolve:
            for path in paths:
                for mode in ("create", "overwrite"):
                    with self.subTest(path=path, mode=mode):
                        self.assert_failure(self.invoke({
                            "path": path, "content": "secret", "mode": mode
                        }), "denied")
        resolve.assert_not_called()

    def test_external_parent_link_and_target_link_are_denied(self):
        secret = self.outside / "secret.txt"
        secret.write_bytes(b"OUTSIDE_SECRET")
        link = self.root / "escape"
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(self.outside)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, "Test fixture junction creation failed")
        else:
            link.symlink_to(self.outside, target_is_directory=True)
        self.links.append(link)
        self.assert_failure(self.invoke({
            "path": "escape/new.txt", "content": "bad", "mode": "create"
        }), "denied")
        self.assert_failure(self.invoke({
            "path": "escape/secret.txt", "content": "bad", "mode": "overwrite"
        }), "denied")
        self.assertEqual(secret.read_bytes(), b"OUTSIDE_SECRET")
        self.assertFalse((self.outside / "new.txt").exists())

    def test_internal_directory_link_is_also_denied_without_modification(self):
        real = self.root / "real"
        real.mkdir()
        existing = real / "existing.txt"
        existing.write_bytes(b"KEEP")
        link = self.root / "alias"
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(real)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, "Test fixture junction creation failed")
        else:
            link.symlink_to(real, target_is_directory=True)
        self.links.append(link)

        created = self.invoke({
            "path": "alias/new.txt", "content": "bad", "mode": "create"
        })
        overwritten = self.invoke({
            "path": "alias/existing.txt", "content": "bad", "mode": "overwrite"
        })
        self.assert_failure(created, "denied")
        self.assert_failure(overwritten, "denied")
        self.assertFalse((real / "new.txt").exists())
        self.assertEqual(existing.read_bytes(), b"KEEP")

    def test_external_hard_link_overwrite_is_denied(self):
        secret = self.outside / "secret.txt"
        secret.write_bytes(b"OUTSIDE_SECRET")
        link = self.root / "linked.txt"
        os.link(secret, link)
        self.links.append(link)
        self.assert_failure(self.invoke({
            "path": "linked.txt", "content": "bad", "mode": "overwrite"
        }), "denied")
        self.assertEqual(secret.read_bytes(), b"OUTSIDE_SECRET")

    def test_actual_outside_handle_is_denied_before_any_content_operation(self):
        secret = self.outside / "secret.txt"
        secret.write_bytes(b"OUTSIDE_SECRET")
        (self.root / "inside.txt").write_bytes(b"inside")
        tracked = _TrackedStream(secret.open("r+b", buffering=0))
        with patch.object(access, "_open_windows_for_update", return_value=tracked):
            result = self.invoke_approved({
                "path": "inside.txt", "content": "bad", "mode": "overwrite"
            })
        self.assert_failure(result, "denied")
        self.assertEqual(
            (tracked.read_count, tracked.write_count, tracked.seek_count, tracked.truncate_count),
            (0, 0, 0, 0),
        )
        self.assertTrue(tracked.stream.closed)
        self.assertEqual(secret.read_bytes(), b"OUTSIDE_SECRET")

        create_secret = self.outside / "create-secret.txt"
        create_secret.write_bytes(b"OUTSIDE_SECRET")
        create_tracked = _TrackedStream(create_secret.open("r+b", buffering=0))
        with patch.object(access, "_open_windows_for_create", return_value=create_tracked):
            create_result = self.invoke_approved({
                "path": "new-outside.txt", "content": "bad", "mode": "create"
            })
        self.assert_failure(create_result, "denied")
        self.assertEqual(
            (
                create_tracked.read_count,
                create_tracked.write_count,
                create_tracked.seek_count,
                create_tracked.truncate_count,
            ),
            (0, 0, 0, 0),
        )
        self.assertTrue(create_tracked.stream.closed)
        self.assertEqual(create_secret.read_bytes(), b"OUTSIDE_SECRET")
        self.assertFalse((self.root / "new-outside.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows handle-path verification test")
    def test_create_handle_verification_failure_removes_new_file(self):
        with patch.object(
            access,
            "_windows_handle_path",
            side_effect=access.ToolAccessDenied("simulated verification failure"),
        ):
            result = self.invoke_approved({
                "path": "unverified.txt", "content": "must not remain", "mode": "create"
            })
        self.assert_failure(result, "denied")
        self.assertFalse((self.root / "unverified.txt").exists())

    def test_stream_wrapper_failure_removes_exclusively_created_file(self):
        with patch.object(access.os, "fdopen", side_effect=OSError("simulated fdopen failure")):
            result = self.invoke_approved({
                "path": "fdopen-failed.txt", "content": "must not remain", "mode": "create"
            })
        self.assert_failure(result)
        self.assertFalse((self.root / "fdopen-failed.txt").exists())

    def test_short_writes_are_completed(self):
        original_write_all = writer._write_all

        def one_byte_at_a_time(stream, raw):
            view = memoryview(raw)
            for index in range(len(view)):
                count = stream.write(view[index:index + 1])
                self.assertEqual(count, 1)

        with patch.object(writer, "_write_all", side_effect=one_byte_at_a_time):
            result = self.invoke_approved({"path": "short.txt", "content": "中文abc", "mode": "create"})
        self.assertEqual(result["status"], "success")
        self.assertEqual((self.root / "short.txt").read_bytes(), "中文abc".encode())
        self.assertIsNotNone(original_write_all)

    def test_partial_overwrite_failure_restores_original(self):
        target = self.root / "existing.txt"
        original = b"original content with tail"
        target.write_bytes(original)
        real_write_all = writer._write_all
        calls = 0

        def fail_first(stream, raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                stream.write(raw[:2])
                raise OSError("simulated partial failure")
            return real_write_all(stream, raw)

        with patch.object(writer, "_write_all", side_effect=fail_first):
            result = self.invoke_approved({
                "path": "existing.txt", "content": "new", "mode": "overwrite"
            })
        self.assert_failure(result)
        self.assertEqual(calls, 2)
        self.assertEqual(target.read_bytes(), original)

    def test_overwrite_recovery_failure_and_create_failure_are_explicit(self):
        target = self.root / "existing.txt"
        target.write_bytes(b"original")

        def always_fail(stream, raw):
            stream.write(b"X")
            raise OSError("simulated permanent failure")

        with patch.object(writer, "_write_all", side_effect=always_fail):
            result = self.invoke_approved({
                "path": "existing.txt", "content": "new", "mode": "overwrite"
            })
        self.assert_failure(result)
        self.assertIn("无法确认原始内容已恢复", result["error"])

        with patch.object(writer, "_write_all", side_effect=OSError("create failed")):
            created = self.invoke_approved({"path": "failed.txt", "content": "new", "mode": "create"})
        self.assert_failure(created)
        self.assertFalse((self.root / "failed.txt").exists())

    def test_registry_revalidates_full_output_and_request_correlation(self):
        raw = "hello".encode("utf-8")
        valid = {
            "path": "new.txt", "encoding": "utf-8", "operation": "created",
            "bytes_written": len(raw), "sha256": sha256(raw).hexdigest(),
        }
        invalid = [
            None, "text", {}, valid | {"path": "other.txt"},
            valid | {"encoding": "utf-8-sig"}, valid | {"operation": "overwritten"},
            valid | {"bytes_written": len(raw) + 1}, valid | {"sha256": sha256(b"other").hexdigest()},
            valid | {"unknown": True},
            WriteFileOutput.model_construct(**(valid | {"sha256": "invalid"})),
        ]
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                entry = replace(registry._REGISTRY["write_file"], handler=Mock(return_value=value))
                with patch.dict(registry._REGISTRY, {"write_file": entry}):
                    self.assert_failure(self.invoke_approved())

        bom_raw = "hello".encode("utf-8-sig")
        forged_without_bom = valid | {
            "encoding": "utf-8-sig",
            "bytes_written": len("hello".encode("utf-8")),
            "sha256": sha256("hello".encode("utf-8")).hexdigest(),
        }
        entry = replace(
            registry._REGISTRY["write_file"], handler=Mock(return_value=forged_without_bom)
        )
        with patch.dict(registry._REGISTRY, {"write_file": entry}):
            self.assert_failure(self.invoke_approved({
                "path": "new.txt", "content": "hello", "mode": "create", "encoding": "utf-8-sig"
            }))
        self.assertEqual(len(bom_raw), len(raw) + 3)

    def test_models_and_toolschema_are_synchronized(self):
        self.assertEqual(
            [item["function"]["name"] for item in TOOLSCHEMAS],
            ["read_file", "editor_file", "list_files", "grep", "write_file", "list_pending_changes", "apply_pending_change", "inspect_recovery", "resolve_recovery", "load_skill", "read_skill_resource", "bash_shell", "code_interpreter", "web_fetch", "web_search", "get_async_task", "cancel_async_task"],
        )
        schema = next(item for item in TOOLSCHEMAS if item["function"]["name"] == "write_file")
        self.assertEqual(schema["function"]["parameters"], WriteFileArguments.model_json_schema())
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        self.assertNotIn("strict", schema["function"])
        with self.assertRaises(ValidationError):
            WriteFileOutput(
                path="../x", encoding="utf-8", operation="created",
                bytes_written=1, sha256="a" * 64,
            )
        with self.assertRaises(ValidationError):
            WriteFileArguments(path="x", content="x", mode="create", root="outside")


if __name__ == "__main__":
    unittest.main()
