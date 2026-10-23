"""Code Interpreter 的能力白名单、隔离执行与资源边界测试。"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.schemas import CodeInterpreterArguments
from tool.file_access import ToolAccessDenied, ToolExecutionError


interpreter = importlib.import_module("tool.code_interpreter")
policy_module = importlib.import_module("tool.policy")


class CodeInterpreterTests(unittest.TestCase):
    def run_code(self, code: str, timeout: int = 5):
        return interpreter.code_interpreter(
            CodeInterpreterArguments(code=code, timeout_seconds=timeout)
        )

    def test_safe_calculation_stdout_and_final_expression(self):
        output = self.run_code(
            "values = [1, 2, 3, 4]\n"
            "print(statistics.mean(values))\n"
            "math.factorial(6) + sum(values)"
        )
        self.assertEqual(output.exit_code, 0)
        self.assertEqual(output.stdout, "2.5\n")
        self.assertEqual(output.result, "730")
        self.assertEqual(output.stderr, "")

    def test_syntax_and_runtime_errors_are_sanitized(self):
        with self.assertRaises(ToolExecutionError):
            self.run_code("if:")
        output = self.run_code("1 / 0")
        self.assertEqual(output.exit_code, 1)
        self.assertEqual(output.stdout, "")
        self.assertEqual(output.result, "")
        self.assertEqual(output.stderr, "代码运行失败：ZeroDivisionError。")
        self.assertNotIn("Traceback", output.stderr)

    def test_forbidden_capabilities_are_rejected_before_process_start(self):
        attacks = (
            "import os",
            "open('marker.txt', 'w')",
            "os.system('whoami')",
            "__import__('os')",
            "getattr(1, '__class__')",
            "(1).__class__",
            "eval('1 + 1')",
            "exec('x = 1')",
            "compile('1', 'x', 'eval')",
            "while True:\n    pass",
            "[x for x in range(3)]",
            "bytes([111,115]).decode()",
        )
        with patch.object(
            interpreter.subprocess,
            "Popen",
            side_effect=AssertionError("越权代码不得启动执行器"),
        ) as popen:
            for code in attacks:
                with self.subTest(code=code), self.assertRaises(ToolAccessDenied):
                    self.run_code(code)
        popen.assert_not_called()

    def test_file_network_and_system_side_effect_marker_is_never_created(self):
        with tempfile.TemporaryDirectory(prefix="code-interpreter-marker-") as temporary:
            marker = Path(temporary) / "marker.txt"
            code = f"open({str(marker)!r}, 'w')"
            with self.assertRaises(ToolAccessDenied):
                self.run_code(code)
            self.assertFalse(marker.exists())

    def test_unbounded_loop_times_out_without_harming_parent(self):
        with self.assertRaisesRegex(ToolExecutionError, "超时"):
            self.run_code("for item in range(10 ** 100):\n    pass", timeout=1)
        # 超时后父进程和下一次隔离执行仍可正常工作。
        self.assertEqual(self.run_code("6 * 7").result, "42")

    def test_large_output_is_stopped_and_bounded(self):
        output = self.run_code("print('x' * 20000)")
        self.assertEqual(output.exit_code, 1)
        self.assertTrue(output.truncated)
        self.assertEqual(len(output.stdout), 16384)
        self.assertLessEqual(len(output.stderr), 1024)

    def test_policy_can_tighten_output_limit(self):
        policy = policy_module.load_tool_policy()
        tightened = policy.model_copy(
            update={
                "code_interpreter": policy.code_interpreter.model_copy(
                    update={"max_output_characters": 1024}
                )
            }
        )
        with patch.object(interpreter, "load_tool_policy", return_value=tightened):
            output = self.run_code("print('x' * 2000)")
        self.assertTrue(output.truncated)
        self.assertEqual(len(output.stdout), 1024)

    def test_sensitive_environment_is_not_forwarded(self):
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "FAKE_TEST_SECRET", "RUN_DEEPSEEK_E2E": "1"},
        ):
            environment = interpreter._environment()
        self.assertNotIn("DEEPSEEK_API_KEY", environment)
        self.assertNotIn("RUN_DEEPSEEK_E2E", environment)
        self.assertNotIn("PATH", environment)

    def test_policy_missing_invalid_or_disabled_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="code-policy-") as temporary:
            missing = Path(temporary) / "missing.json"
            invalid = Path(temporary) / "invalid.json"
            invalid.write_text("{}", encoding="utf-8")
            for path in (missing, invalid):
                with (
                    self.subTest(path=path.name),
                    patch.object(policy_module, "_POLICY_FILE", path),
                    patch.object(
                        interpreter.subprocess,
                        "Popen",
                        side_effect=AssertionError("策略失败不得启动执行器"),
                    ) as popen,
                    self.assertRaises(ToolAccessDenied),
                ):
                    self.run_code("1 + 1")
                popen.assert_not_called()

        policy = policy_module.load_tool_policy()
        disabled = policy.model_copy(
            update={"code_interpreter": policy.code_interpreter.model_copy(update={"enabled": False})}
        )
        with (
            patch.object(interpreter, "load_tool_policy", return_value=disabled),
            patch.object(
                interpreter.subprocess,
                "Popen",
                side_effect=AssertionError("禁用工具不得启动执行器"),
            ) as popen,
            self.assertRaises(ToolAccessDenied),
        ):
            self.run_code("1 + 1")
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object resource test")
    def test_windows_resource_limit_failure_is_fail_closed(self):
        with (
            patch.object(
                interpreter,
                "_create_windows_job",
                side_effect=ToolExecutionError("simulated limit failure"),
            ),
            patch.object(
                interpreter.subprocess,
                "Popen",
                side_effect=AssertionError("限制失败不得启动执行器"),
            ) as popen,
            self.assertRaises(ToolExecutionError),
        ):
            self.run_code("1 + 1")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
