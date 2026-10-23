from __future__ import annotations

import io
import builtins
import os
import unittest
from unittest.mock import patch

from app.terminal_input import SLASH_COMMANDS, create_terminal_reader, filter_command_candidates


class TerminalInputTests(unittest.TestCase):
    def test_slash_prefix_exposes_documented_commands_with_descriptions(self):
        candidates = filter_command_candidates("/")
        names = [name for name, _description in candidates]
        self.assertEqual(names, [name for name, _description in SLASH_COMMANDS if name.startswith("/")])
        self.assertTrue(all(description for _name, description in candidates))
        self.assertIn("/plan", names)
        self.assertNotIn("/mode plan", names)
        self.assertNotIn("/mode act", names)
        self.assertEqual(filter_command_candidates("/mo"), ())
        self.assertEqual(
            [name for name, _description in filter_command_candidates("/permissions f")],
            ["/permissions full-access"],
        )
        self.assertEqual(filter_command_candidates("普通问题"), ())

    def test_non_tty_fallback_uses_input_without_ansi_or_prompt_toolkit(self):
        reader = create_terminal_reader()
        output = io.StringIO()
        with (
            patch("app.terminal_input.sys.stdin", io.StringIO()),
            patch("app.terminal_input.sys.stdout", output),
            patch("builtins.input", return_value="/status") as fallback,
        ):
            self.assertEqual(reader("> ", True), "/status")
        fallback.assert_called_once_with("> ")
        self.assertNotIn("\x1b[", output.getvalue())

    def test_non_completion_reads_never_start_prompt_session_even_on_tty(self):
        reader = create_terminal_reader()
        with (
            patch("app.terminal_input._is_interactive_terminal", return_value=True),
            patch("builtins.input", return_value="yes") as fallback,
        ):
            self.assertEqual(reader("确认：", False), "yes")
        fallback.assert_called_once_with("确认：")

    def test_missing_prompt_toolkit_falls_back_to_builtin_input(self):
        reader = create_terminal_reader()
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "prompt_toolkit" or name.startswith("prompt_toolkit."):
                raise ImportError("dependency unavailable")
            return original_import(name, *args, **kwargs)

        with (
            patch("app.terminal_input._is_interactive_terminal", return_value=True),
            patch("builtins.__import__", side_effect=guarded_import),
            patch("builtins.input", return_value="/status") as fallback,
        ):
            self.assertEqual(reader("> ", True), "/status")
        fallback.assert_called_once_with("> ")

    def test_no_color_disables_interactive_completion_control_sequences(self):
        reader = create_terminal_reader()
        with (
            patch("app.terminal_input._is_interactive_terminal", return_value=True),
            patch.dict(os.environ, {"NO_COLOR": "1"}),
            patch("builtins.input", return_value="/status") as fallback,
        ):
            self.assertEqual(reader("> ", True), "/status")
        fallback.assert_called_once_with("> ")


if __name__ == "__main__":
    unittest.main()
