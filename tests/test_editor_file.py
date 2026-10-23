"""editor_file 的功能、注册约束与项目目录权限测试。"""

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

from app.schemas import EditorFileArguments, EditorFileOutput, ToolMessage
from app.workflow import FileApprovalWorkflow
from tool.registry import TOOLSCHEMAS, call_tool


access = importlib.import_module("tool.file_access")
editor = importlib.import_module("tool.editor_file")
registry = importlib.import_module("tool.registry")


class _TrackedUpdateStream:
    """记录边界检查之后是否发生过内容操作。"""

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


class EditorFileTests(unittest.TestCase):
    def setUp(self):
        self.test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="editor-file-tests-", dir=self.test_directory)
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
        self.assertEqual(self.fixture.parent, self.test_directory)
        self.assertTrue(self.fixture.name.startswith("editor-file-tests-"))
        for link in self.links:
            self.assertEqual(link.parent, self.root)
            if os.path.lexists(link):
                os.rmdir(link) if link.is_dir() else link.unlink()
        self.temporary.cleanup()

    def write(self, path: str, raw: bytes) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return target

    def invoke(self, arguments=None, name="editor_file") -> dict:
        if arguments is None:
            arguments = {"path": "data.txt", "old_text": "old", "new_text": "new"}
        raw = call_tool({"id": "edit_1", "name": name, "arguments": arguments})
        message = ToolMessage.model_validate_json(raw)
        self.assertEqual(message.tool_call_id, "edit_1")
        return json.loads(raw)["result"]

    def invoke_approved(self, arguments=None, name="editor_file") -> dict:
        if arguments is None:
            arguments = {"path": "data.txt", "old_text": "old", "new_text": "new"}
        raw = call_tool(
            {"id": "edit_1", "name": name, "arguments": arguments},
            confirmation_callback=lambda request: True,
            workflow=FileApprovalWorkflow(),
        )
        return json.loads(ToolMessage.model_validate_json(raw).model_dump_json())["result"]

    def assert_failure(self, result: dict, status="error"):
        self.assertEqual(result["status"], status)
        self.assertEqual(result["output"], "")
        self.assertTrue(result["error"])
        self.assertNotIn(str(self.fixture), result["error"])
        self.assertNotIn("OUTSIDE_SECRET", result["error"])

    def test_missing_workflow_or_confirmation_cannot_modify(self):
        target = self.write("data.txt", b"old")
        self.assert_failure(self.invoke(), "denied")
        self.assertEqual(target.read_bytes(), b"old")
        raw = call_tool(
            {
                "id": "edit_1",
                "name": "editor_file",
                "arguments": {"path": "data.txt", "old_text": "old", "new_text": "new"},
            },
            confirmation_callback=lambda request: True,
        )
        self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "denied")
        self.assertEqual(target.read_bytes(), b"old")

    def test_diff_preview_second_no_keeps_original_and_complete_candidate(self):
        target = self.write("data.txt", b"old value\n")
        requests = []

        def confirm(request):
            requests.append(request)
            return request.phase == "operation"

        raw = call_tool(
            {"id": "edit_1", "name": "editor_file", "arguments": {
                "path": "data.txt", "old_text": "old", "new_text": "new"
            }},
            confirmation_callback=confirm,
            workflow=FileApprovalWorkflow(),
        )
        result = json.loads(raw)["result"]
        self.assert_failure(result, "denied")
        self.assertEqual(target.read_bytes(), b"old value\n")
        self.assertEqual([request.phase for request in requests], ["operation", "diff"])
        self.assertIn("-old value", requests[1].diff_preview)
        self.assertIn("+new value", requests[1].diff_preview)
        pending = self.root / requests[1].pending_file
        self.assertEqual(pending.read_bytes(), b"new value\n")

    def test_second_confirmation_exception_or_target_change_keeps_candidate(self):
        for mode in ("exception", "changed"):
            with self.subTest(mode=mode):
                target = self.write(f"{mode}.txt", b"old")
                diff_requests = []

                def confirm(request):
                    if request.phase == "operation":
                        return True
                    diff_requests.append(request)
                    if mode == "exception":
                        raise RuntimeError("UI failed")
                    target.write_bytes(b"changed outside")
                    return True

                raw = call_tool(
                    {"id": "edit_1", "name": "editor_file", "arguments": {
                        "path": f"{mode}.txt", "old_text": "old", "new_text": "new"
                    }},
                    confirmation_callback=confirm,
                    workflow=FileApprovalWorkflow(),
                )
                self.assertNotEqual(ToolMessage.model_validate_json(raw).result.status, "success")
                self.assertEqual(
                    target.read_bytes(),
                    b"old" if mode == "exception" else b"changed outside",
                )
                self.assertEqual((self.root / diff_requests[0].pending_file).read_bytes(), b"new")

    def test_first_no_creates_no_pending_directory(self):
        target = self.write("first-no.txt", b"old")
        raw = call_tool(
            {"id": "edit_1", "name": "editor_file", "arguments": {
                "path": "first-no.txt", "old_text": "old", "new_text": "new"
            }},
            confirmation_callback=lambda request: False,
            workflow=FileApprovalWorkflow(),
        )
        self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "denied")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse((self.root / "data/pending").exists())

    def test_edits_only_the_requested_file_and_reports_json_metadata(self):
        original = b"head\nvalue = 'old'\ntail\n"
        expected = b"head\nvalue = 'new'\ntail\n"
        target = self.write("app/target.py", original)
        decoy = self.write("app/decoy.py", original)

        result = self.invoke_approved({
            "path": "app/target.py", "old_text": "value = 'old'", "new_text": "value = 'new'"
        })

        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_bytes(), expected)
        self.assertEqual(decoy.read_bytes(), original)
        self.assertEqual(result["output"], {
            "path": "app/target.py",
            "encoding": "utf-8",
            "operation": "replace_text",
            "replacements": 1,
            "bytes_before": len(original),
            "bytes_after": len(expected),
            "sha256_before": sha256(original).hexdigest(),
            "sha256_after": sha256(expected).hexdigest(),
        })

    def test_multiple_replacements_preserve_bom_crlf_chinese_and_unrelated_text(self):
        bom = b"\xef\xbb\xbf"
        original = bom + "标题\r\nold\r\n保留 old\r\n结尾".encode("utf-8")
        expected = bom + "标题\r\n新值\r\n保留 新值\r\n结尾".encode("utf-8")
        target = self.write("资料/配置.txt", original)

        result = self.invoke_approved({
            "path": "资料\\配置.txt",
            "old_text": "old",
            "new_text": "新值",
            "expected_replacements": 2,
        })

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["encoding"], "utf-8-sig")
        self.assertEqual(result["output"]["path"], "资料/配置.txt")
        self.assertEqual(target.read_bytes(), expected)

    def test_empty_new_text_deletes_and_truncates(self):
        target = self.write("data.txt", b"before REMOVE after")
        result = self.invoke_approved({"path": "data.txt", "old_text": " REMOVE", "new_text": ""})
        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_bytes(), b"before after")
        self.assertEqual(result["output"]["bytes_after"], len(b"before after"))

    def test_line_anchor_replace_insert_delete_preserve_unaffected_bytes(self):
        bom = b"\xef\xbb\xbf"
        target = self.write("lines.txt", bom + "一\r\n二\r\n三\r\n四".encode("utf-8"))
        replaced = self.invoke_approved({
            "path": "lines.txt", "operation": "replace_lines",
            "old_text": "二\n三", "new_text": "新二\n新三",
        })
        self.assertEqual(replaced["status"], "success")
        self.assertEqual(replaced["output"]["operation"], "replace_lines")
        self.assertEqual(replaced["output"]["replacements"], 1)
        self.assertEqual(target.read_bytes(), bom + "一\r\n新二\r\n新三\r\n四".encode("utf-8"))

        inserted = self.invoke_approved({
            "path": "lines.txt", "operation": "insert_lines",
            "old_text": "新二", "new_text": "插入", "position": "before",
        })
        self.assertEqual(inserted["status"], "success")
        self.assertEqual(inserted["output"]["operation"], "insert_lines")
        self.assertEqual(target.read_bytes(), bom + "一\r\n插入\r\n新二\r\n新三\r\n四".encode("utf-8"))

        deleted = self.invoke_approved({
            "path": "lines.txt", "operation": "delete_lines",
            "old_text": "插入\n新二\n",
        })
        self.assertEqual(deleted["status"], "success")
        self.assertEqual(deleted["output"]["operation"], "delete_lines")
        self.assertEqual(target.read_bytes(), bom + "一\r\n新三\r\n四".encode("utf-8"))

    def test_line_operations_require_full_unambiguous_nonoverlapping_anchors(self):
        target = self.write("lines.txt", b"one\ntwo\n")
        original = target.read_bytes()
        invalid = (
            {"operation": "replace_lines", "old_text": "tw", "new_text": "x"},
            {"operation": "delete_lines", "old_text": "missing"},
            {"operation": "insert_lines", "old_text": "one", "new_text": "x", "position": "before", "expected_replacements": 2},
        )
        for values in invalid:
            with self.subTest(operation=values["operation"]):
                result = self.invoke_approved({"path": "lines.txt"} | values)
                self.assert_failure(result)
                self.assertEqual(target.read_bytes(), original)

        overlapping = self.write("overlap.txt", b"a\na\na\n")
        result = self.invoke_approved({
            "path": "overlap.txt", "operation": "replace_lines",
            "old_text": "a\na", "new_text": "x", "expected_replacements": 2,
        })
        self.assert_failure(result)
        self.assertEqual(overlapping.read_bytes(), b"a\na\na\n")

    def test_line_operations_support_multiple_matches_and_eof_without_newline(self):
        target = self.write("multiple.txt", b"same\r\nkeep\r\nsame\r\nend")
        result = self.invoke_approved({
            "path": "multiple.txt", "operation": "replace_lines",
            "old_text": "same\n", "new_text": "changed", "expected_replacements": 2,
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["replacements"], 2)
        self.assertEqual(target.read_bytes(), b"changed\r\nkeep\r\nchanged\r\nend")

        inserted = self.invoke_approved({
            "path": "multiple.txt", "operation": "insert_lines",
            "old_text": "end", "new_text": "tail\n", "position": "after",
        })
        self.assertEqual(inserted["status"], "success")
        self.assertEqual(target.read_bytes(), b"changed\r\nkeep\r\nchanged\r\nend\r\ntail")

    def test_replace_text_normalizes_request_newlines_to_target_style(self):
        target = self.write("crlf.txt", b"before\r\nold\r\nafter\r\n")
        result = self.invoke_approved({
            "path": "crlf.txt", "operation": "replace_text",
            "old_text": "before\nold\n", "new_text": "before\nnew\n",
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(target.read_bytes(), b"before\r\nnew\r\nafter\r\n")

    def test_sensitive_env_is_denied_and_other_hidden_file_is_editable(self):
        target = self.write(".env", b"FAKE_TOKEN=old")
        self.assertEqual(self.invoke({
            "path": ".env", "old_text": "old", "new_text": "example"
        })["status"], "denied")
        self.assertEqual(target.read_bytes(), b"FAKE_TOKEN=old")
        hidden = self.write(".hidden/config", b"old")
        self.assertEqual(self.invoke_approved({
            "path": ".hidden/config", "old_text": "old", "new_text": "new"
        })["status"], "success")
        self.assertEqual(hidden.read_bytes(), b"new")

    def test_count_mismatch_never_modifies_file(self):
        target = self.write("data.txt", b"old once")
        original = target.read_bytes()
        for expected in (2, 3):
            with self.subTest(expected=expected):
                result = self.invoke_approved({
                    "path": "data.txt", "old_text": "old", "new_text": "new",
                    "expected_replacements": expected,
                })
                self.assert_failure(result)
                self.assertEqual(target.read_bytes(), original)

    def test_root_does_not_depend_on_working_directory(self):
        target = self.write("data.txt", b"old")
        previous = Path.cwd()
        try:
            os.chdir(self.outside)
            self.assertEqual(self.invoke_approved()["status"], "success")
        finally:
            os.chdir(previous)
        self.assertEqual(target.read_bytes(), b"new")

    def test_schema_rejects_ambiguous_or_unrelated_arguments_before_handler(self):
        invalid = [
            {}, {"path": "data.txt", "old_text": "", "new_text": "new"},
            {"path": "data.txt", "old_text": "old", "new_text": "old"},
            {"path": "data.txt", "old_text": "a\x00b", "new_text": "new"},
            {"path": 1, "old_text": "old", "new_text": "new"},
            {"path": "data.txt", "old_text": "old", "new_text": 1},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "root": str(self.outside)},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "encoding": "utf-8"},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "unexpected": True},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "expected_replacements": 0},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "expected_replacements": 1001},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "expected_replacements": "1"},
            {"path": "data.txt", "old_text": "old", "new_text": "new", "expected_replacements": True},
            {"path": "data.txt", "operation": "replace_lines", "old_text": "old"},
            {"path": "data.txt", "operation": "replace_lines", "old_text": "old", "new_text": "x", "position": "before"},
            {"path": "data.txt", "operation": "insert_lines", "old_text": "old", "new_text": "x"},
            {"path": "data.txt", "operation": "insert_lines", "old_text": "old", "new_text": "x", "position": "middle"},
            {"path": "data.txt", "operation": "delete_lines", "old_text": "old", "new_text": "x"},
            {"path": "data.txt", "operation": "delete_lines", "old_text": "old", "position": "after"},
            {"path": "data.txt", "operation": "replace_lines", "old_text": "x\n" * 1001, "new_text": "x"},
            {"path": "data.txt", "operation": "replace_lines", "old_text": "old", "new_text": "x\n" * 1001},
            {"path": "data.txt", "operation": "replace_lines", "old_text": "old", "new_text": "x", "start_line": 1},
        ]
        handler = Mock(side_effect=AssertionError("must not execute"))
        entry = replace(registry._REGISTRY["editor_file"], handler=handler)
        with patch.dict(registry._REGISTRY, {"editor_file": entry}):
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    self.assert_failure(self.invoke(arguments))
        handler.assert_not_called()

    def test_tool_name_must_match_exactly(self):
        handler = Mock(side_effect=AssertionError("must not execute"))
        entry = replace(registry._REGISTRY["editor_file"], handler=handler)
        with patch.dict(registry._REGISTRY, {"editor_file": entry}):
            for name in ("edit_file", "EDITOR_FILE", "Editor_File", "editor-file"):
                with self.subTest(name=name):
                    self.assert_failure(self.invoke(name=name))
        handler.assert_not_called()

    def test_unsafe_paths_are_denied_before_filesystem_resolution(self):
        paths = [
            "../outside/secret.txt", "nested/../../secret.txt", "nested\\..\\secret.txt",
            str(self.outside / "secret.txt"), "C:/Windows/win.ini", "C:secret.txt",
            "\\Windows\\win.ini", "/etc/passwd", "\\\\server\\share\\secret.txt",
            "data.txt:stream", "NUL", "CON.txt", "data.txt.", "data.txt ",
            "bad\x00file", "bad\nfile", ".", "..", "./data.txt", "nested//data.txt",
        ]
        with patch.object(Path, "resolve", side_effect=AssertionError("must not resolve")) as resolve:
            for path in paths:
                with self.subTest(path=path):
                    self.assert_failure(self.invoke({
                        "path": path, "old_text": "old", "new_text": "new"
                    }), "denied")
        resolve.assert_not_called()

    def test_missing_directory_binary_and_large_files_are_not_modified(self):
        (self.root / "directory").mkdir()
        self.assert_failure(self.invoke({
            "path": "missing.txt", "old_text": "old", "new_text": "new"
        }))
        self.assert_failure(self.invoke({
            "path": "directory", "old_text": "old", "new_text": "new"
        }))
        for name, raw in (
            ("binary.bin", b"old\xff\xfe"),
            ("contains-nul.txt", b"old\x00text"),
            ("large.txt", b"old" + b"x" * editor.MAX_EDITABLE_FILE_BYTES),
        ):
            with self.subTest(name=name):
                target = self.write(name, raw)
                self.assert_failure(self.invoke_approved({"path": name, "old_text": "old", "new_text": "new"}))
                self.assertEqual(target.read_bytes(), raw)

    def test_external_directory_junction_is_denied(self):
        (self.outside / "secret.txt").write_bytes(b"OUTSIDE_SECRET old")
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
        result = self.invoke({"path": "escape/secret.txt", "old_text": "old", "new_text": "new"})
        self.assert_failure(result, "denied")
        self.assertEqual((self.outside / "secret.txt").read_bytes(), b"OUTSIDE_SECRET old")

    def test_hard_link_to_outside_file_is_denied(self):
        outside = self.outside / "secret.txt"
        outside.write_bytes(b"OUTSIDE_SECRET old")
        link = self.root / "linked.txt"
        os.link(outside, link)
        self.links.append(link)
        self.assertGreaterEqual(outside.stat().st_nlink, 2)
        self.assert_failure(self.invoke({
            "path": "linked.txt", "old_text": "old", "new_text": "new"
        }), "denied")
        self.assertEqual(outside.read_bytes(), b"OUTSIDE_SECRET old")

    @unittest.skipUnless(os.name == "nt", "Windows handle security test")
    def test_actual_outside_handle_is_denied_before_read_or_write(self):
        self.write("data.txt", b"inside old")
        secret = self.outside / "secret.txt"
        secret.write_bytes(b"OUTSIDE_SECRET old")
        tracked = _TrackedUpdateStream(secret.open("r+b"))
        try:
            with patch.object(access, "_open_windows_for_update", return_value=tracked):
                self.assert_failure(self.invoke_approved(), "denied")
            self.assertEqual(
                (tracked.read_count, tracked.write_count, tracked.seek_count, tracked.truncate_count),
                (0, 0, 0, 0),
            )
            self.assertTrue(tracked.stream.closed)
        finally:
            tracked.stream.close()
        self.assertEqual(secret.read_bytes(), b"OUTSIDE_SECRET old")

    def test_partial_write_failure_restores_original_bytes(self):
        target = self.write("data.txt", b"old content and tail")
        original = target.read_bytes()
        real_write_all = editor._write_all
        calls = 0

        def partial_then_restore(stream, raw):
            nonlocal calls
            calls += 1
            if calls == 1:
                stream.write(raw[:3])
                raise OSError("simulated partial write")
            return real_write_all(stream, raw)

        with patch.object(editor, "_write_all", side_effect=partial_then_restore):
            self.assert_failure(self.invoke_approved())
        self.assertEqual(calls, 2)
        self.assertEqual(target.read_bytes(), original)

    def test_recovery_failure_is_reported_explicitly(self):
        self.write("data.txt", b"old content")

        def always_fail(stream, raw):
            stream.write(b"X")
            raise OSError("simulated permanent failure")

        with patch.object(editor, "_write_all", side_effect=always_fail):
            result = self.invoke_approved()
        self.assert_failure(result)
        self.assertIn("无法确认原始内容已恢复", result["error"])

    def test_registry_revalidates_handler_output_and_request_correlation(self):
        self.write("data.txt", b"old")
        before = b"old"
        after = b"new"
        valid = {
            "path": "data.txt", "encoding": "utf-8", "operation": "replace_text", "replacements": 1,
            "bytes_before": len(before), "bytes_after": len(after),
            "sha256_before": sha256(before).hexdigest(),
            "sha256_after": sha256(after).hexdigest(),
        }
        invalid = [
            None, "text", {}, valid | {"path": "other.txt"},
            valid | {"replacements": 2}, valid | {"sha256_after": "invalid"},
            valid | {"unknown": 1},
            EditorFileOutput.model_construct(**(valid | {"sha256_after": "invalid"})),
        ]
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                entry = replace(registry._REGISTRY["editor_file"], handler=Mock(return_value=value))
                with patch.dict(registry._REGISTRY, {"editor_file": entry}):
                    self.assert_failure(self.invoke_approved())

    def test_handler_exception_does_not_leak_path_or_content(self):
        self.write("data.txt", b"old")
        entry = replace(
            registry._REGISTRY["editor_file"],
            handler=Mock(side_effect=RuntimeError(f"{self.outside}: OUTSIDE_SECRET")),
        )
        with patch.dict(registry._REGISTRY, {"editor_file": entry}):
            self.assert_failure(self.invoke_approved())

    def test_tool_schema_is_generated_from_editor_arguments(self):
        self.assertEqual(
            [item["function"]["name"] for item in TOOLSCHEMAS],
            ["read_file", "editor_file", "list_files", "grep", "write_file", "list_pending_changes", "apply_pending_change", "inspect_recovery", "resolve_recovery", "load_skill", "read_skill_resource", "bash_shell", "code_interpreter", "web_fetch", "web_search", "get_async_task", "cancel_async_task"],
        )
        schema = next(
            item for item in TOOLSCHEMAS if item["function"]["name"] == "editor_file"
        )
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["parameters"], EditorFileArguments.model_json_schema())
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        self.assertNotIn("strict", schema["function"])

    def test_argument_and_output_models_enforce_json_contract(self):
        arguments = EditorFileArguments(
            path="data.txt", old_text=" \n", new_text="", expected_replacements=1
        )
        self.assertEqual(arguments.old_text, " \n")
        valid_output = {
            "path": "data.txt", "encoding": "utf-8", "operation": "replace_text", "replacements": 1,
            "bytes_before": 3, "bytes_after": 3,
            "sha256_before": "a" * 64, "sha256_after": "b" * 64,
        }
        self.assertEqual(EditorFileOutput.model_validate(valid_output).path, "data.txt")
        for changes in (
            {"path": "../outside"}, {"encoding": "base64"}, {"replacements": 0},
            {"bytes_before": -1}, {"sha256_before": "A" * 64},
            {"sha256_after": "a" * 64}, {"unexpected": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                EditorFileOutput.model_validate(valid_output | changes)


if __name__ == "__main__":
    unittest.main()
