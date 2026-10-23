"""list_files 与 grep 的离线功能、协议和目录边界测试。"""

from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.schemas import (
    GrepArguments,
    GrepMatch,
    GrepOutput,
    ListFileEntry,
    ListFilesArguments,
    ListFilesOutput,
    ToolMessage,
)
from tool.registry import TOOLSCHEMAS, call_tool


access = importlib.import_module("tool.file_access")
project_scan = importlib.import_module("tool.project_scan")
registry = importlib.import_module("tool.registry")
grep_module = importlib.import_module("tool.grep")


class SearchToolTests(unittest.TestCase):
    def setUp(self):
        test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="search-tools-", dir=test_directory)
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
                os.rmdir(link) if link.is_dir() else link.unlink()
        self.temporary.cleanup()

    def write(self, path: str, content: str | bytes) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return target

    def invoke(self, name: str, arguments: dict, call_id: str = "search_1") -> dict:
        raw = call_tool({"id": call_id, "name": name, "arguments": arguments})
        message = ToolMessage.model_validate_json(raw)
        self.assertEqual(message.tool_call_id, call_id)
        return json.loads(raw)["result"]

    def assert_failure(self, result: dict, status: str = "error") -> None:
        self.assertEqual(result["status"], status)
        self.assertEqual(result["output"], "")
        self.assertTrue(result["error"])
        self.assertNotIn(str(self.fixture), result["error"])
        self.assertNotIn("OUTSIDE_SECRET", result["error"])

    def populate_tree(self):
        self.write("z.txt", "root")
        self.write("a.py", "root python")
        self.write("nested/readme.md", "nested markdown")
        self.write("nested/code.py", "nested python")
        self.write("nested/deep/info.md", "deep markdown")

    def test_list_root_nonrecursive_and_stable_json(self):
        self.populate_tree()
        result = self.invoke("list_files", {})
        self.assertEqual(result["status"], "success")
        output = result["output"]
        self.assertEqual(output["pattern"], "*")
        self.assertEqual([entry["path"] for entry in output["entries"]], ["a.py", "z.txt"])
        self.assertEqual(output["count"], 2)
        self.assertFalse(output["truncated"])
        self.assertEqual(output["entries"][0]["size"], len(b"root python"))

    def test_list_recursive_and_directory_scoped_patterns(self):
        self.populate_tree()
        recursive = self.invoke("list_files", {"pattern": "**/*.py"})["output"]
        self.assertEqual([item["path"] for item in recursive["entries"]], ["a.py", "nested/code.py"])
        direct = self.invoke("list_files", {"pattern": "nested/*.md"})["output"]
        self.assertEqual([item["path"] for item in direct["entries"]], ["nested/readme.md"])
        deep = self.invoke("list_files", {"pattern": "nested/**/*.md"})["output"]
        self.assertEqual(
            [item["path"] for item in deep["entries"]],
            ["nested/deep/info.md", "nested/readme.md"],
        )

    def test_sensitive_dependencies_and_caches_are_hidden_even_when_explicit(self):
        self.write("docs/guide.md", "project needle")
        self.write(".venv/lib/dependency.md", "dependency needle")
        self.write("src/__pycache__/cached.md", "cache needle")
        self.write(".git/config", "git needle")
        self.write("nested/.env.local", "secret needle")

        broad = self.invoke("list_files", {"pattern": "**/*.md"})["output"]
        self.assertEqual([item["path"] for item in broad["entries"]], ["docs/guide.md"])
        broad_grep = self.invoke("grep", {"pattern": "needle"})["output"]
        self.assertEqual([item["path"] for item in broad_grep["matches"]], ["docs/guide.md"])

        explicit = self.invoke("list_files", {"pattern": ".venv/**/*.md"})["output"]
        self.assertEqual(explicit["entries"], [])
        self.assertEqual(
            self.invoke("list_files", {"pattern": ".git/**/*"})["output"]["entries"],
            [],
        )
        self.assertEqual(
            self.invoke("grep", {"pattern": "secret", "file_pattern": "**/*"})["output"]["matches"],
            [],
        )

    def test_list_max_results_and_scan_cap_report_truncated(self):
        for name in ("d.txt", "b.txt", "c.txt", "a.txt"):
            self.write(name, name)
        output = self.invoke("list_files", {"pattern": "*", "max_results": 2})["output"]
        self.assertEqual([item["path"] for item in output["entries"]], ["a.txt", "b.txt"])
        self.assertTrue(output["truncated"])
        with patch.object(project_scan, "MAX_SCANNED_ENTRIES", 2):
            limited = self.invoke("list_files", {"pattern": "*", "max_results": 10})["output"]
        self.assertTrue(limited["truncated"])
        self.assertLessEqual(limited["count"], 2)

    def test_grep_multiple_utf8_formats_line_column_and_filter(self):
        self.write("README.md", "title\nfind me here\n尾行")
        self.write("app/code.py", "# find me\nvalue = 'find me'\n")
        self.write("web/data.json", '{"text": "find me"}\n')
        self.write("web/style.css", ".find { color: red; }\n")
        output = self.invoke("grep", {
            "pattern": "find me", "file_pattern": "**/*", "max_results": 10,
        })["output"]
        self.assertEqual(
            [(item["path"], item["line_number"], item["column"]) for item in output["matches"]],
            [("README.md", 2, 1), ("app/code.py", 1, 3), ("app/code.py", 2, 10), ("web/data.json", 1, 11)],
        )
        self.assertEqual(output["count"], 4)
        filtered = self.invoke("grep", {
            "pattern": "find me", "file_pattern": "**/*.md",
        })["output"]
        self.assertEqual([item["path"] for item in filtered["matches"]], ["README.md"])

    def test_grep_chinese_phrase_spaces_tabs_and_case_sensitivity(self):
        self.write("notes.txt", "前缀 中文 短语 后缀\nName\tValue\nhello WORLD\n")
        chinese = self.invoke("grep", {"pattern": "中文 短语"})["output"]
        self.assertEqual((chinese["matches"][0]["line_number"], chinese["matches"][0]["column"]), (1, 4))
        tabbed = self.invoke("grep", {"pattern": "Name\tValue"})["output"]
        self.assertEqual(tabbed["count"], 1)
        sensitive = self.invoke("grep", {"pattern": "world"})["output"]
        self.assertEqual(sensitive["count"], 0)
        insensitive = self.invoke("grep", {"pattern": "world", "case_sensitive": False})["output"]
        self.assertEqual(insensitive["matches"][0]["column"], 7)

    def test_grep_max_results_skip_binary_invalid_utf8_and_large(self):
        self.write("a.txt", "hit hit hit")
        self.write("binary.bin", b"hit\x00tail")
        self.write("invalid.txt", b"hit\xff")
        self.write("large.txt", b"hit" + b"x" * grep_module.MAX_SEARCHABLE_FILE_BYTES)
        output = self.invoke("grep", {"pattern": "hit", "max_results": 2})["output"]
        self.assertEqual(output["count"], 2)
        self.assertTrue(output["truncated"])
        self.assertEqual({item["path"] for item in output["matches"]}, {"a.txt"})

    def test_tools_are_independent_of_working_directory(self):
        self.write("data.txt", "needle")
        previous = Path.cwd()
        try:
            os.chdir(self.outside)
            listed = self.invoke("list_files", {"pattern": "*"})["output"]
            searched = self.invoke("grep", {"pattern": "needle"})["output"]
        finally:
            os.chdir(previous)
        self.assertEqual(listed["entries"][0]["path"], "data.txt")
        self.assertEqual(searched["matches"][0]["path"], "data.txt")

    def test_unsafe_globs_are_denied_before_scanning(self):
        unsafe = [
            "../*", "nested/../*", "/**", "C:/*", "C:/Windows/*",
            "\\\\server\\share\\*", "file:*", "./*", "nested//*.py", "name.",
        ]
        with patch.object(project_scan.os, "scandir", side_effect=AssertionError("must not scan")) as scan:
            for pattern in unsafe:
                with self.subTest(pattern=pattern):
                    self.assert_failure(self.invoke("list_files", {"pattern": pattern}), "denied")
                    self.assert_failure(
                        self.invoke("grep", {"pattern": "text", "file_pattern": pattern}),
                        "denied",
                    )
            # 非法 glob 语法和控制字符由 schema 拒绝，也同样不能触发扫描。
            for pattern in ("bad\n*", "a/**b", "[", "bad\x00*"):
                with self.subTest(invalid_syntax=pattern):
                    self.assert_failure(self.invoke("list_files", {"pattern": pattern}))
        scan.assert_not_called()

    def test_hard_link_to_outside_is_not_listed_or_searched(self):
        outside = self.outside / "secret.txt"
        outside.write_text("OUTSIDE_SECRET needle", encoding="utf-8")
        link = self.root / "linked.txt"
        os.link(outside, link)
        self.links.append(link)
        self.assertGreaterEqual(link.stat().st_nlink, 2)

        listed = self.invoke("list_files", {"pattern": "**/*"})["output"]
        searched = self.invoke("grep", {"pattern": "needle"})["output"]

        self.assertEqual(listed["entries"], [])
        self.assertEqual(searched["matches"], [])
        self.assertEqual(searched["files_searched"], 0)

    def test_recursive_depth_limit_sets_truncated(self):
        self.write("level/inside.txt", "content")
        with patch.object(project_scan, "MAX_SCAN_DEPTH", 1):
            output = self.invoke("list_files", {"pattern": "**/*"})["output"]
        self.assertEqual(output["entries"], [])
        self.assertTrue(output["truncated"])

    def test_external_link_or_junction_is_never_traversed(self):
        (self.outside / "secret.txt").write_text("OUTSIDE_SECRET needle", encoding="utf-8")
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
        listed = self.invoke("list_files", {"pattern": "**/*"})["output"]
        searched = self.invoke("grep", {"pattern": "needle"})["output"]
        self.assertEqual(listed["entries"], [])
        self.assertEqual(searched["matches"], [])

    def test_extra_wrong_type_and_unregistered_names_never_call_handlers(self):
        cases = {
            "list_files": [
                {"root": "outside"}, {"pattern": 1}, {"max_results": "2"},
                {"pattern": "*", "encoding": "utf-8"},
            ],
            "grep": [
                {}, {"pattern": ""}, {"pattern": " \t"}, {"pattern": "a\nb"},
                {"pattern": 1}, {"pattern": "x", "case_sensitive": 1},
                {"pattern": "x", "max_results": "2"}, {"pattern": "x", "root": "outside"},
                {"pattern": "x", "encoding": "utf-8"}, {"pattern": "x", "regex": True},
            ],
        }
        for name, arguments_list in cases.items():
            handler = Mock(side_effect=AssertionError("must not execute"))
            entry = replace(registry._REGISTRY[name], handler=handler)
            with patch.dict(registry._REGISTRY, {name: entry}):
                for arguments in arguments_list:
                    with self.subTest(name=name, arguments=arguments):
                        self.assert_failure(self.invoke(name, arguments))
            handler.assert_not_called()
        for name in ("LIST_FILES", "List_Files", "GREP", "grep_file"):
            with self.subTest(name=name):
                self.assert_failure(self.invoke(name, {"pattern": "x"}))

    def test_registry_revalidates_outputs_and_request_correlation(self):
        list_valid = {
            "pattern": "*", "entries": [{"path": "a.txt", "size": 1}],
            "count": 1, "truncated": False,
        }
        grep_valid = {
            "pattern": "hit", "file_pattern": "**/*", "case_sensitive": True,
            "matches": [{"path": "a.txt", "line_number": 1, "column": 1, "text": "hit"}],
            "count": 1, "files_searched": 1, "truncated": False,
        }
        invalid_values = {
            "list_files": [
                list_valid | {"pattern": "*.py"},
                list_valid | {"entries": [{"path": "z.txt", "size": 1}, {"path": "a.txt", "size": 1}], "count": 2},
                list_valid | {"entries": [{"path": "nested/a.txt", "size": 1}]},
                ListFilesOutput.model_construct(
                    pattern="*",
                    entries=[ListFileEntry(path="a.txt", size=1)],
                    count=2,
                    truncated=False,
                ),
            ],
            "grep": [
                grep_valid | {"pattern": "other"},
                grep_valid | {"file_pattern": "*.py"},
                grep_valid | {"case_sensitive": False},
                grep_valid | {"matches": [{"path": "a.txt", "line_number": 0, "column": 1, "text": "hit"}]},
                grep_valid | {"matches": [{"path": "a.txt", "line_number": 1, "column": 1, "text": "other"}]},
                GrepOutput.model_construct(
                    pattern="hit",
                    file_pattern="**/*",
                    case_sensitive=True,
                    matches=[GrepMatch(path="a.txt", line_number=1, column=1, text="hit")],
                    count=2,
                    files_searched=1,
                    truncated=False,
                ),
            ],
        }
        requests = {
            "list_files": {"pattern": "*"},
            "grep": {"pattern": "hit"},
        }
        for name, values in invalid_values.items():
            for value in values:
                with self.subTest(name=name, value=type(value).__name__):
                    entry = replace(registry._REGISTRY[name], handler=Mock(return_value=value))
                    with patch.dict(registry._REGISTRY, {name: entry}):
                        self.assert_failure(self.invoke(name, requests[name]))

    def test_schema_cross_fields_and_tool_schemas(self):
        self.assertEqual(
            [item["function"]["name"] for item in TOOLSCHEMAS],
            [
                "read_file", "editor_file", "list_files", "grep", "write_file",
                "list_pending_changes", "apply_pending_change", "inspect_recovery",
                "resolve_recovery", "load_skill", "read_skill_resource",
                "bash_shell", "code_interpreter",
                "web_fetch", "web_search", "get_async_task", "cancel_async_task",
            ],
        )
        models = {"list_files": ListFilesArguments, "grep": GrepArguments}
        for name, model in models.items():
            schema = next(item for item in TOOLSCHEMAS if item["function"]["name"] == name)
            self.assertEqual(schema["function"]["parameters"], model.model_json_schema())
            self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
            self.assertNotIn("strict", schema["function"])
        with self.assertRaises(ValidationError):
            ListFilesOutput(pattern="*", entries=[ListFileEntry(path="a", size=1)], count=0, truncated=False)
        with self.assertRaises(ValidationError):
            GrepOutput(
                pattern="hit", file_pattern="**/*", case_sensitive=True,
                matches=[GrepMatch(path="a", line_number=1, column=1, text="hit")],
                count=0, files_searched=1, truncated=False,
            )


if __name__ == "__main__":
    unittest.main()
