"""项目 CLI 入口的离线测试；不加载配置或读取真实 .env。"""

from __future__ import annotations

import importlib
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class CliLauncherTests(unittest.TestCase):
    def test_console_script_is_registered_to_main(self):
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)

        target = document["project"]["scripts"]["agent"]
        self.assertEqual(target, "app.main:main")

        module_name, function_name = target.split(":", maxsplit=1)
        module = importlib.import_module(module_name)
        with patch.object(module, function_name, return_value=17) as entry:
            self.assertEqual(getattr(module, function_name)(), 17)
        entry.assert_called_once_with()

    def test_cmd_launcher_uses_only_project_virtualenv(self):
        content = (PROJECT_ROOT / "agent.cmd").read_text(encoding="utf-8")
        lowered = content.casefold()
        self.assertIn(r"%project_dir%.venv\scripts\python.exe", lowered)
        self.assertIn("-m app.main", lowered)
        self.assertIn("%*", content)
        self.assertIn("pushd", lowered)
        self.assertNotIn("activate", lowered)
        self.assertNotIn("setx", lowered)

    def test_powershell_launcher_uses_only_project_virtualenv(self):
        content = (PROJECT_ROOT / "agent.ps1").read_text(encoding="utf-8")
        lowered = content.casefold()
        self.assertIn(r".venv\scripts\python.exe", lowered)
        self.assertIn("-m app.main", lowered)
        self.assertIn("@args", lowered)
        self.assertIn("push-location", lowered)
        self.assertNotIn("$profile", lowered)
        self.assertNotIn("[environment]::setenvironmentvariable", lowered)

    def test_posix_launcher_uses_only_project_virtualenv(self):
        content = (PROJECT_ROOT / "agent.sh").read_text(encoding="utf-8")
        lowered = content.casefold()
        self.assertTrue(content.startswith("#!/bin/sh\n"))
        self.assertIn(".venv/bin/python", lowered)
        self.assertIn("-m app.main", lowered)
        self.assertIn('"$@"', content)
        self.assertIn("exec", lowered)
        self.assertNotIn("activate", lowered)
        self.assertNotIn("curl", lowered)
        self.assertNotIn("wget", lowered)


if __name__ == "__main__":
    unittest.main()
