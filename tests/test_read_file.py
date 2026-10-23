"""注册表、文件读取与权限边界测试；只使用隔离的假数据，不读取真实 .env。"""

import base64
import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.schemas import ReadFileArguments, ReadFileOutput, ToolCall, ToolMessage
from tool.registry import TOOLSCHEMAS, call_tool


reader = importlib.import_module("tool.read_file")
access = importlib.import_module("tool.file_access")
registry = importlib.import_module("tool.registry")


class _TrackedStream:
    """将真的文件句柄交给 Windows 检查，并记录读取是否已经发生。"""

    def __init__(self, stream):
        self.stream = stream
        self.read_count = 0
        self.seek_count = 0

    def fileno(self):
        return self.stream.fileno()

    def read(self, size=-1):
        self.read_count += 1
        return self.stream.read(size)

    def seek(self, offset):
        self.seek_count += 1
        return self.stream.seek(offset)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="read-file-tests-", dir=self.test_directory)
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
        # 在清理任何目录前，确认真实目标仍在本次测试创建的夹具范围内。
        self.assertEqual(self.fixture.parent, self.test_directory)
        self.assertTrue(self.fixture.name.startswith("read-file-tests-"))
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

    def invoke(self, arguments=None, name="read_file") -> dict:
        if arguments is None:
            arguments = {"path": "data.txt"}
        text = call_tool({"id": "call_1", "name": name, "arguments": arguments})
        message = ToolMessage.model_validate_json(text)
        self.assertEqual(message.tool_call_id, "call_1")
        self.assertEqual(message.role, "tool")
        return json.loads(text)["result"]

    def assert_failure(self, result: dict, status="error"):
        self.assertEqual(result["status"], status)
        self.assertEqual(result["output"], "")
        self.assertTrue(result["error"])
        self.assertNotIn(str(self.fixture), result["error"])
        self.assertNotIn("OUTSIDE_SECRET", result["error"])

    def test_utf8_chinese_path_and_original_newlines(self):
        raw = "甲\r\n乙\n    print('中文')\r\n".encode("utf-8")
        self.write("资料/示例.py", raw)
        result = self.invoke({"path": "资料/示例.py"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"], {
            "path": "资料/示例.py", "encoding": "utf-8", "content": raw.decode("utf-8"),
            "offset": 0, "bytes_read": len(raw), "total_bytes": len(raw), "truncated": False,
        })

    def test_backslash_relative_path_is_normalized(self):
        self.write("nested/data.txt", b"text")
        result = self.invoke({"path": "nested\\data.txt"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["path"], "nested/data.txt")

    def test_empty_file(self):
        self.write("data.txt", b"")
        output = self.invoke()["output"]
        self.assertEqual(output["content"], "")
        self.assertEqual(output["bytes_read"], 0)
        self.assertEqual(output["total_bytes"], 0)
        self.assertFalse(output["truncated"])

    def test_non_utf8_binary_round_trips(self):
        raw = b"\x00\xff\xfe\x80BIN"
        self.write("data.txt", raw)
        output = self.invoke()["output"]
        self.assertEqual(output["encoding"], "base64")
        self.assertEqual(base64.b64decode(output["content"]), raw)

    def test_non_sensitive_hidden_files_are_allowed(self):
        for name in (".gitignore", ".hidden/config"):
            with self.subTest(path=name):
                self.write(name, b"FAKE_TEST_VALUE=example")
                self.assertEqual(self.invoke({"path": name})["status"], "success")

    def test_sensitive_files_and_policy_are_denied_without_reading(self):
        for name in (
            ".env", ".ENV", ".env.local", "nested/.env.test",
            ".git/config", ".venv/pyvenv.cfg", ".agent/tool_policy.json",
        ):
            with self.subTest(path=name):
                self.write(name, b"FAKE_TEST_SECRET=never-read")
                self.assert_failure(self.invoke({"path": name}), "denied")

    def test_large_file_is_paged_and_reconstructed(self):
        raw = b"a" * 1048576 + b"last-page"
        self.write("data.txt", raw)
        default = self.invoke()["output"]
        self.assertEqual(default["bytes_read"], 65536)
        self.assertTrue(default["truncated"])
        first = self.invoke({"path": "data.txt", "max_bytes": 1048576})["output"]
        second = self.invoke({"path": "data.txt", "offset": first["bytes_read"]})["output"]
        self.assertTrue(first["truncated"])
        self.assertFalse(second["truncated"])
        self.assertEqual((first["content"] + second["content"]).encode(), raw)

    def test_utf8_split_at_byte_boundary_uses_base64(self):
        self.write("data.txt", "中文".encode("utf-8"))
        output = self.invoke({"path": "data.txt", "offset": 1, "max_bytes": 2})["output"]
        self.assertEqual(output["encoding"], "base64")
        self.assertEqual(base64.b64decode(output["content"]), "中".encode()[1:])

    def test_all_byte_values_can_be_reconstructed_from_pages(self):
        raw = bytes(range(256)) * 13
        self.write("data.txt", raw)
        rebuilt = bytearray()
        offset = 0
        while offset < len(raw):
            output = self.invoke({"path": "data.txt", "offset": offset, "max_bytes": 257})["output"]
            rebuilt.extend(
                output["content"].encode("utf-8") if output["encoding"] == "utf-8"
                else base64.b64decode(output["content"])
            )
            offset += output["bytes_read"]
        self.assertEqual(bytes(rebuilt), raw)

    def test_offset_at_or_beyond_eof(self):
        self.write("data.txt", b"abc")
        for offset in (3, 10):
            with self.subTest(offset=offset):
                output = self.invoke({"path": "data.txt", "offset": offset})["output"]
                self.assertEqual(output["content"], "")
                self.assertEqual(output["bytes_read"], 0)
                self.assertFalse(output["truncated"])

    def test_root_does_not_depend_on_working_directory(self):
        self.write("data.txt", b"inside")
        (self.outside / "data.txt").write_bytes(b"OUTSIDE_SECRET")
        previous = Path.cwd()
        try:
            os.chdir(self.outside)
            self.assertEqual(self.invoke()["output"]["content"], "inside")
        finally:
            os.chdir(previous)

    def test_schemas_match_the_registry(self):
        self.assertEqual(
            [schema["function"]["name"] for schema in TOOLSCHEMAS],
            [
                "read_file", "editor_file", "list_files", "grep", "write_file",
                "list_pending_changes", "apply_pending_change", "inspect_recovery",
                "resolve_recovery", "load_skill", "read_skill_resource",
                "bash_shell", "code_interpreter",
                "web_fetch", "web_search", "get_async_task", "cancel_async_task",
            ],
        )
        schema = next(
            schema for schema in TOOLSCHEMAS if schema["function"]["name"] == "read_file"
        )
        self.assertEqual(schema["type"], "function")
        self.assertEqual(schema["function"]["name"], "read_file")
        self.assertEqual(schema["function"]["parameters"], ReadFileArguments.model_json_schema())
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        self.assertNotIn("strict", schema["function"])

    def test_unknown_and_case_mismatched_tools_are_rejected(self):
        self.write("data.txt", b"inside")
        handler = Mock(side_effect=AssertionError("must not execute"))
        entry = replace(registry._REGISTRY["read_file"], handler=handler)
        with patch.dict(registry._REGISTRY, {"read_file": entry}):
            for name in ("write_file", "READ_FILE", "Read_File", "__import__"):
                with self.subTest(name=name):
                    self.assert_failure(self.invoke(name=name))
        handler.assert_not_called()

    def test_bad_arguments_do_not_execute_handler(self):
        invalid = [
            {}, {"path": ""}, {"path": " \n"}, {"path": 1},
            {"path": "data.txt", "extra": 1}, {"path": "data.txt", "root": str(self.outside)},
            {"path": "data.txt", "offset": -1}, {"path": "data.txt", "offset": "1"},
            {"path": "data.txt", "offset": True}, {"path": "data.txt", "offset": 1.0},
            {"path": "data.txt", "max_bytes": 0}, {"path": "data.txt", "max_bytes": 1048577},
            {"path": "data.txt", "max_bytes": "3"}, {"path": "data.txt", "max_bytes": False},
        ]
        handler = Mock(side_effect=AssertionError("must not execute"))
        entry = replace(registry._REGISTRY["read_file"], handler=handler)
        with patch.dict(registry._REGISTRY, {"read_file": entry}):
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    result = json.loads(call_tool({"id": "call_1", "name": "read_file", "arguments": arguments}))["result"]
                    self.assert_failure(result)
        handler.assert_not_called()

    def test_invalid_call_envelope_raises_validation_error(self):
        for envelope in ({}, {"name": "read_file", "arguments": {}},
                         {"id": "call_1", "name": "read_file", "arguments": "{}"}):
            with self.subTest(envelope=envelope), self.assertRaises(ValidationError):
                call_tool(envelope)

    def test_constructed_invalid_call_is_revalidated(self):
        call = ToolCall.model_construct(id="", name="read_file", arguments={"path": "data.txt"})
        with self.assertRaises(ValidationError):
            call_tool(call)

    def test_unsafe_path_forms_are_denied(self):
        paths = [
            "../outside/secret.txt", "nested/../../secret.txt", "nested\\..\\secret.txt",
            str(self.outside / "secret.txt"), "C:/Windows/win.ini", "C:secret.txt",
            "\\Windows\\win.ini", "/etc/passwd", "\\\\server\\share\\secret.txt",
            "\\\\?\\C:\\secret.txt", "data.txt:secret", "data.txt::$DATA",
            "NUL", "NUL.txt", "CON .txt", "CONIN$", "CONOUT$", "COM¹", "LPT³.txt",
            "data.txt.", "data.txt ", "folder /data.txt", "bad\x00file", "bad\nfile",
            "*", "?", ".", "..", "./data.txt", "nested//data.txt",
        ]
        with patch.object(Path, "open", side_effect=AssertionError("must not open")) as open_file:
            for path in paths:
                with self.subTest(path=path):
                    self.assert_failure(self.invoke({"path": path}), "denied")
        open_file.assert_not_called()

    def test_missing_file_and_directory_are_errors(self):
        (self.root / "directory").mkdir()
        self.assert_failure(self.invoke({"path": "missing.txt"}))
        self.assert_failure(self.invoke({"path": "directory"}))

    def test_reserved_paths_are_rejected_before_filesystem_resolution(self):
        with patch.object(Path, "resolve", side_effect=AssertionError("must not resolve")) as resolve:
            for path in ("CON .txt", "COM¹", "LPT³.txt", "NUL.txt", "data.txt:secret"):
                with self.subTest(path=path):
                    self.assert_failure(self.invoke({"path": path}), "denied")
        resolve.assert_not_called()

    def test_external_directory_link_is_denied(self):
        (self.outside / "secret.txt").write_bytes(b"OUTSIDE_SECRET")
        link = self.root / "escape"
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(self.outside)],
                capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, "Test fixture junction creation failed")
        else:
            link.symlink_to(self.outside, target_is_directory=True)
        self.links.append(link)
        self.assert_failure(self.invoke({"path": "escape/secret.txt"}), "denied")

    def test_hard_link_to_outside_is_denied_before_read(self):
        secret = self.outside / "secret.txt"
        secret.write_bytes(b"OUTSIDE_SECRET")
        link = self.root / "linked.txt"
        os.link(secret, link)
        self.links.append(link)
        self.assert_failure(self.invoke({"path": "linked.txt"}), "denied")

    def test_similarly_prefixed_sibling_is_outside(self):
        sibling = self.fixture / "project-other"
        sibling.mkdir()
        target = sibling / "secret.txt"
        target.write_bytes(b"OUTSIDE_SECRET")
        with patch.object(Path, "resolve", return_value=target):
            self.assert_failure(self.invoke(), "denied")

    @unittest.skipUnless(os.name == "nt", "Windows handle security test")
    def test_actual_outside_handle_is_denied_before_any_read(self):
        self.write("data.txt", b"inside")
        secret = self.outside / "secret.txt"
        secret.write_bytes(b"OUTSIDE_SECRET")
        tracked = _TrackedStream(secret.open("rb"))
        try:
            # 前置路径检查正常通过；模拟 open 时文件被替换，返回真的根外句柄。
            with patch.object(Path, "open", return_value=tracked):
                self.assert_failure(self.invoke(), "denied")
            self.assertEqual(tracked.read_count, 0)
            self.assertEqual(tracked.seek_count, 0)
            self.assertTrue(tracked.stream.closed)
        finally:
            tracked.stream.close()

    @unittest.skipUnless(os.name == "nt", "Windows handle security test")
    def test_windows_handle_validation_failure_is_closed(self):
        target = self.write("data.txt", b"inside")
        for api_results in ([0], [32, 0]):
            with self.subTest(api_results=api_results):
                tracked = _TrackedStream(target.open("rb"))
                fake_kernel = Mock()
                fake_kernel.GetFinalPathNameByHandleW.side_effect = api_results
                try:
                    with patch.object(Path, "open", return_value=tracked), patch("ctypes.WinDLL", return_value=fake_kernel):
                        self.assert_failure(self.invoke(), "denied")
                    self.assertEqual(tracked.read_count, 0)
                    self.assertEqual(tracked.seek_count, 0)
                finally:
                    tracked.stream.close()

    @unittest.skipUnless(os.name == "nt", "Windows handle security test")
    def test_windows_handle_unstable_buffer_size_is_closed(self):
        target = self.write("data.txt", b"inside")
        tracked = _TrackedStream(target.open("rb"))
        fake_kernel = Mock()
        fake_kernel.GetFinalPathNameByHandleW.side_effect = [2, 8, 16, 32]
        try:
            with patch.object(Path, "open", return_value=tracked), patch("ctypes.WinDLL", return_value=fake_kernel):
                self.assert_failure(self.invoke(), "denied")
            self.assertEqual(tracked.read_count, 0)
            self.assertEqual(tracked.seek_count, 0)
            self.assertEqual(fake_kernel.GetFinalPathNameByHandleW.call_count, 4)
        finally:
            tracked.stream.close()

    def test_invalid_handler_output_is_rejected(self):
        output = {
            "path": "data.txt", "encoding": "utf-8", "content": "abc", "offset": 0,
            "bytes_read": 3, "total_bytes": 3, "truncated": False,
        }
        invalid = [
            "text", None, {}, output | {"bytes_read": 1}, output | {"unknown": 1},
            output | {"path": "../secret.txt"}, output | {"truncated": True},
            output | {"encoding": "base64", "content": "%%%"},
            ReadFileOutput.model_construct(**(output | {"bytes_read": 1})),
            # 数据本身合法，但与这次请求的 offset 或 max_bytes 不符。
            output | {"offset": 1, "total_bytes": 4},
        ]
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                entry = replace(registry._REGISTRY["read_file"], handler=Mock(return_value=value))
                with patch.dict(registry._REGISTRY, {"read_file": entry}):
                    self.assert_failure(self.invoke())
        entry = replace(registry._REGISTRY["read_file"], handler=Mock(return_value=output))
        with patch.dict(registry._REGISTRY, {"read_file": entry}):
            self.assert_failure(self.invoke({"path": "data.txt", "max_bytes": 2}))

    def test_handler_exception_does_not_leak_details(self):
        entry = replace(
            registry._REGISTRY["read_file"],
            handler=Mock(side_effect=RuntimeError(f"{self.outside}: OUTSIDE_SECRET")),
        )
        with patch.dict(registry._REGISTRY, {"read_file": entry}):
            self.assert_failure(self.invoke())

    def test_return_model_checks_encoding_size_and_fields(self):
        valid = {"path": "data.txt", "encoding": "utf-8", "content": "中", "offset": 0,
                 "bytes_read": 3, "total_bytes": 3, "truncated": False}
        self.assertEqual(ReadFileOutput.model_validate(valid).bytes_read, 3)
        # Absolute output paths are valid data for the trusted full-access
        # mode; partial-control still rejects them in the execution layer.
        self.assertEqual(
            ReadFileOutput.model_validate(valid | {"path": "/outside"}).path,
            "/outside",
        )
        for changes in (
            {"bytes_read": 1}, {"total_bytes": 2}, {"truncated": True}, {"offset": -1},
            {"bytes_read": True}, {"content": 1}, {"extra": 1},
            {"encoding": "base64", "content": "%%%"}, {"encoding": "ascii"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                ReadFileOutput.model_validate(valid | changes)


if __name__ == "__main__":
    unittest.main()
