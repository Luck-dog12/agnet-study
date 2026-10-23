"""候选文件存储、完整性和终端 diff 边界测试。"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tool.file_access import ToolAccessDenied, ToolExecutionError
from tool.pending_changes import (
    MAX_DIFF_CHARACTERS,
    MAX_DIFF_LINES,
    discard_candidate,
    load_candidate,
    store_candidate,
    unified_diff_preview,
)


access = importlib.import_module("tool.file_access")


class PendingChangesTests(unittest.TestCase):
    def setUp(self):
        test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="pending-tests-", dir=test_directory)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch.object(access, "_PROJECT_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def test_candidate_round_trip_uses_only_uuid_filename_and_exact_cleanup(self):
        identifier = "a" * 32
        candidate = store_candidate(identifier, b"complete\x00candidate")
        path = self.root / "data/pending" / f"{identifier}.bin"
        self.assertEqual(load_candidate(candidate), b"complete\x00candidate")
        self.assertEqual([item.name for item in path.parent.iterdir()], [f"{identifier}.bin"])
        discard_candidate(candidate)
        self.assertFalse(path.exists())

    def test_invalid_id_link_and_tampered_candidate_are_rejected(self):
        for identifier in ("../escape", "A" * 32, "a" * 31, "a" * 33):
            with self.subTest(identifier=identifier), self.assertRaises(ToolAccessDenied):
                store_candidate(identifier, b"x")
        candidate = store_candidate("b" * 32, b"safe")
        path = self.root / "data/pending" / ("b" * 32 + ".bin")
        path.write_bytes(b"evil")
        with self.assertRaises(ToolExecutionError):
            load_candidate(candidate)

        target = self.root / "outside.bin"
        target.write_bytes(b"outside")
        link = self.root / "data/pending" / ("c" * 32 + ".bin")
        try:
            os.link(target, link)
        except OSError:
            self.skipTest("hard links unavailable")
        with self.assertRaises((ToolAccessDenied, FileExistsError)):
            store_candidate("c" * 32, b"bad")
        self.assertEqual(target.read_bytes(), b"outside")

    def test_diff_is_bounded_and_escapes_terminal_control_characters(self):
        before = "old\x1b[31m\u0085\u200b\r\n" + "same\n" * (MAX_DIFF_LINES + 100)
        after = "new\x1b[32m\u0085\u200b\rhidden\n" + "changed\n" * (MAX_DIFF_LINES + 100)
        preview = unified_diff_preview(before, after)
        self.assertNotIn("\x1b", preview)
        self.assertNotIn("\r", preview)
        self.assertNotIn("\u0085", preview)
        self.assertNotIn("\u200b", preview)
        self.assertIn(r"\x1b", preview)
        self.assertIn(r"\x85", preview)
        self.assertIn(r"\u200b", preview)
        self.assertLessEqual(len(preview), MAX_DIFF_CHARACTERS + 100)
        self.assertLessEqual(preview.count("\n"), MAX_DIFF_LINES + 3)


if __name__ == "__main__":
    unittest.main()
