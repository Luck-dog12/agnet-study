"""bash_shell 的结构化命令、审批、权限、执行与边界测试。"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.schemas import BashShellArguments, ToolMessage
from app.workflow import FileApprovalWorkflow
from tool.registry import TOOLSCHEMAS, call_tool


access = importlib.import_module("tool.file_access")
shell = importlib.import_module("tool.bash_shell")
interpreter = importlib.import_module("tool.code_interpreter")


class BashShellTests(unittest.TestCase):
    def setUp(self):
        test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(
            prefix="bash-shell-tests-", dir=test_directory
        )
        self.fixture = Path(self.temporary.name).resolve()
        self.root = self.fixture / "project"
        self.outside = self.fixture / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        self.root_patch = patch.object(access, "_PROJECT_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.assertEqual(self.fixture.parent, Path(__file__).resolve().parent)
        self.temporary.cleanup()

    def write(self, path: str, content: str = "data") -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def invoke(self, arguments: dict, callback=None, workflow=None) -> dict:
        raw = call_tool(
            {"id": "shell_1", "name": "bash_shell", "arguments": arguments},
            confirmation_callback=callback,
            workflow=workflow,
        )
        message = ToolMessage.model_validate_json(raw)
        self.assertEqual(message.tool_call_id, "shell_1")
        return json.loads(raw)["result"]

    def assert_failure(self, result: dict, status: str = "error") -> None:
        self.assertEqual(result["status"], status)
        self.assertEqual(result["output"], "")
        self.assertTrue(result["error"])
        self.assertNotIn(str(self.fixture), result["error"])

    def test_schema_is_registered_and_rejects_free_shell_forms(self):
        schema = next(
            item for item in TOOLSCHEMAS if item["function"]["name"] == "bash_shell"
        )
        self.assertEqual(schema["function"]["parameters"], BashShellArguments.model_json_schema())
        self.assertFalse(schema["function"]["parameters"]["additionalProperties"])
        invalid = [
            {},
            {"command": "python", "paths": ["tests"]},
            {"command": "unittest", "paths": []},
            {"command": "unittest", "paths": ["tests"], "argv": ["-c", "bad"]},
            {"command": "compileall", "paths": ["app"], "command_line": "whoami"},
            {"command": "compileall", "paths": ["app"], "timeout_seconds": 0},
            {"command": "compileall", "paths": ["app"], "timeout_seconds": "3"},
        ]
        with patch.object(shell, "bash_shell", side_effect=AssertionError("must not call")):
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    self.assert_failure(self.invoke(arguments))

    def test_unittest_requires_callback_and_only_true_approves(self):
        self.write("tests/test_sample.py", "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n")
        arguments = {"command": "unittest", "paths": ["tests"]}
        with patch.object(shell.subprocess, "run", side_effect=AssertionError("must not run")):
            self.assert_failure(self.invoke(arguments), "denied")
            callback = Mock(return_value=False)
            self.assert_failure(self.invoke(arguments, callback), "denied")
            callback.assert_called_once()

        requests = []
        result = self.invoke(arguments, lambda request: requests.append(request) or True)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["exit_code"], 0)
        self.assertIn("Ran 1 test", result["output"]["stderr"])
        self.assertEqual(requests[0].command, "unittest")
        self.assertEqual(requests[0].paths, ("tests",))
        self.assertNotIn(str(self.root), requests[0].description)

    def test_unittest_uses_sanitized_isolated_workspace(self):
        self.write(".env", "FAKE_SECRET=do-not-copy")
        self.write(
            "tests/test_isolation.py",
            "import os\nfrom pathlib import Path\nimport unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_isolated(self):\n"
            "        self.assertFalse(Path('.env').exists())\n"
            "        self.assertNotIn('DEEPSEEK_API_KEY', os.environ)\n",
        )
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "FAKE_TEST_SECRET"}):
            result = self.invoke(
                {"command": "unittest", "paths": ["tests"]},
                lambda request: True,
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["exit_code"], 0)

    def test_compileall_runs_fixed_argv_and_reports_nonzero(self):
        self.write("src/good.py", "value = 1\n")
        good = self.invoke({"command": "compileall", "paths": ["src"]})
        self.assertEqual(good["status"], "success")
        self.assertEqual(good["output"]["exit_code"], 0)
        self.assertFalse((self.root / "src/__pycache__").exists())

        self.write("src/bad.py", "def broken(:\n")
        bad = self.invoke({"command": "compileall", "paths": ["src"]})
        self.assertEqual(bad["status"], "success")
        self.assertNotEqual(bad["output"]["exit_code"], 0)

    def test_validate_json_runs_and_rejects_wrong_suffix(self):
        self.write("data/good.json", '{"name":"中文"}')
        self.write("data/bad.json", "{")
        self.write("data/not-json.txt", "{}")
        good = self.invoke({"command": "validate_json", "paths": ["data/good.json"]})
        self.assertEqual(good["status"], "success")
        self.assertEqual(good["output"]["exit_code"], 0)
        self.assertIn("\\u4e2d\\u6587", good["output"]["stdout"])
        bad = self.invoke({"command": "validate_json", "paths": ["data/bad.json"]})
        self.assertEqual(bad["status"], "success")
        self.assertNotEqual(bad["output"]["exit_code"], 0)
        self.assert_failure(
            self.invoke({"command": "validate_json", "paths": ["data/not-json.txt"]})
        )

    def test_local_format_json_is_fail_closed_and_never_changes_the_file(self):
        target = self.write("data/unformatted.json", '{"z":1,"a":2}\n')
        before = target.read_bytes()
        with patch.object(
            shell,
            "_run_in_isolated_workspace",
            side_effect=AssertionError("local format_json must not execute"),
        ) as executor:
            result = self.invoke({
                "command": "format_json",
                "paths": ["data/unformatted.json"],
            })
        self.assert_failure(result, "denied")
        self.assertEqual(target.read_bytes(), before)
        executor.assert_not_called()

    def test_python_delegates_to_code_interpreter_and_returns_result(self):
        result = self.invoke({
            "command": "python",
            "code": "values = [2, 3, 5]\nprint(sum(values))\nmath.prod(values)",
        })
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["paths"], [])
        self.assertEqual(result["output"]["stdout"], "10\n")
        self.assertEqual(result["output"]["result"], "30")

    def test_python_delegation_rejects_forbidden_code_before_process_start(self):
        with patch.object(
            interpreter.subprocess,
            "Popen",
            side_effect=AssertionError("越权代码不得启动执行器"),
        ) as popen:
            result = self.invoke({"command": "python", "code": "import os"})
        self.assert_failure(result, "denied")
        popen.assert_not_called()

    def test_non_whitelisted_command_never_reaches_any_executor(self):
        with (
            patch.object(
                shell,
                "code_interpreter",
                side_effect=AssertionError("must not delegate"),
            ) as delegated,
            patch.object(
                shell.subprocess,
                "Popen",
                side_effect=AssertionError("must not start"),
            ) as popen,
        ):
            result = self.invoke({"command": "powershell", "paths": ["app"]})
        self.assert_failure(result)
        delegated.assert_not_called()
        popen.assert_not_called()

    def test_every_path_operand_is_checked_and_cannot_escape(self):
        self.write("src/good.py", "value = 1\n")
        self.write(".env.local", "FAKE_SECRET=hidden")
        self.write(".venv/hidden.py", "SECRET = True\n")
        paths = [
            ["src/good.py", "../outside.py"],
            ["src/good.py", ".venv/hidden.py"],
            ["src/good.py", ".env.local"],
            ["src/good.py", str(self.outside / "bad.py")],
        ]
        with patch.object(shell.subprocess, "run", side_effect=AssertionError("must not run")):
            for operands in paths:
                with self.subTest(paths=operands):
                    self.assert_failure(
                        self.invoke({"command": "compileall", "paths": operands}),
                        "denied",
                    )

    def test_dangerous_file_commands_require_fresh_approval(self):
        removable = self.write("work/remove.txt", "remove")
        clearable = self.write("work/clear.txt", "keep")

        self.assert_failure(self.invoke({
            "command": "remove_file", "paths": ["work/remove.txt"]
        }), "denied")
        self.assertTrue(removable.exists())
        self.assert_failure(self.invoke({
            "command": "clear_file", "paths": ["work/clear.txt"]
        }, lambda request: False, FileApprovalWorkflow()), "denied")
        self.assertEqual(clearable.read_text(encoding="utf-8"), "keep")

        removed = self.invoke(
            {"command": "remove_file", "paths": ["work/remove.txt"]},
            lambda request: True,
            FileApprovalWorkflow(),
        )
        cleared = self.invoke(
            {"command": "clear_file", "paths": ["work/clear.txt"]},
            lambda request: True,
            FileApprovalWorkflow(),
        )
        self.assertEqual(removed["status"], "success")
        self.assertFalse(removable.exists())
        self.assertEqual(cleared["status"], "success")
        self.assertEqual(clearable.read_bytes(), b"")

    def test_clear_directory_approval_sensitive_child_and_correct_execution(self):
        self.write("work/tree/a.txt", "a")
        self.write("work/tree/nested/b.txt", "b")
        denied = self.invoke(
            {"command": "clear_directory", "paths": ["work/tree"]},
            lambda request: False,
            FileApprovalWorkflow(),
        )
        self.assert_failure(denied, "denied")
        self.assertTrue((self.root / "work/tree/nested/b.txt").exists())
        approved = self.invoke(
            {"command": "clear_directory", "paths": ["work/tree"]},
            lambda request: True,
            FileApprovalWorkflow(),
        )
        self.assertEqual(approved["status"], "success")
        self.assertEqual(list((self.root / "work/tree").iterdir()), [])

        self.write("protected/.envrc", "FAKE_SECRET=keep")
        self.write("protected/normal.txt", "keep")
        callback = Mock(return_value=True)
        protected = self.invoke(
            {"command": "clear_directory", "paths": ["protected"]}, callback
        )
        self.assert_failure(protected, "denied")
        callback.assert_not_called()
        self.assertTrue((self.root / "protected/.envrc").exists())
        self.assertTrue((self.root / "protected/normal.txt").exists())

        self.write("protected-full/nested/server.key", "keep")
        self.write("protected-full/normal.txt", "keep")
        full_callback = Mock(return_value=True)
        full_raw = call_tool(
            {
                "id": "shell_full",
                "name": "bash_shell",
                "arguments": {
                    "command": "clear_directory",
                    "paths": ["protected-full"],
                },
            },
            permission_mode="full-access",
            confirmation_callback=full_callback,
            workflow=FileApprovalWorkflow(),
        )
        full = json.loads(full_raw)["result"]
        self.assert_failure(full, "denied")
        full_callback.assert_not_called()
        self.assertTrue((self.root / "protected-full/nested/server.key").exists())
        self.assertTrue((self.root / "protected-full/normal.txt").exists())

    def test_confirmation_exception_is_denied_and_file_unchanged(self):
        target = self.write("work/file.txt", "keep")
        result = self.invoke(
            {"command": "clear_file", "paths": ["work/file.txt"]},
            Mock(side_effect=RuntimeError("callback secret")),
            FileApprovalWorkflow(),
        )
        self.assert_failure(result, "denied")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    def test_output_is_bounded_and_timeout_is_sanitized(self):
        self.write("src/code.py", "value = 1\n")
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="x" * 40000, stderr="y" * 40000
        )
        with patch.object(shell.subprocess, "run", return_value=completed) as run:
            result = self.invoke({"command": "compileall", "paths": ["src"]})
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["output"]["truncated"])
        self.assertLessEqual(len(result["output"]["stdout"]), shell.MAX_OUTPUT_CHARACTERS)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertNotIn("-c", run.call_args.args[0])

        with patch.object(
            shell.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="hidden", timeout=1),
        ):
            timeout = self.invoke({
                "command": "compileall", "paths": ["src"], "timeout_seconds": 1
            })
        self.assert_failure(timeout)
        self.assertNotIn("hidden", timeout["error"])


if __name__ == "__main__":
    unittest.main()
