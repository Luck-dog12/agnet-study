"""Offline tests for cloud residency accounting and crash-safe reservations."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.sandbox_budget import SandboxBudget, SandboxBudgetError, SandboxBudgetExceeded


SESSION = "a" * 32


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SandboxBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "sandbox-budget.json"
        self.clock = Clock()

    def budget(self, maximum: int = 600, *, persistent: bool = True) -> SandboxBudget:
        return SandboxBudget(maximum, session_id=SESSION, clock=self.clock,
                             path=self.path if persistent else None)

    def ledger(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_confirmed_cleanup_charges_elapsed_and_idle_gap_is_free(self):
        budget = self.budget()
        self.assertEqual(budget.begin(300), 300)
        self.clock.advance(42)
        self.assertEqual(budget.snapshot()["used_seconds"], 42)
        self.assertEqual(budget.reservation_remaining_seconds(), 258)
        budget.finish(confirmed=True)
        self.clock.advance(1000)
        self.assertEqual(budget.remaining_seconds(), 558)
        self.assertEqual(budget.begin(300), 300)
        self.clock.advance(8)
        budget.finish(confirmed=True)
        self.assertEqual(budget.snapshot()["used_seconds"], 50)
        self.assertEqual(self.ledger()["reserved_seconds"], 0)

    def test_extension_is_saved_before_return_and_capped_by_total_budget(self):
        budget = self.budget(360)
        self.assertEqual(budget.begin(300), 300)
        self.clock.advance(250)
        self.assertEqual(budget.extend(300), 110)
        self.assertEqual(self.ledger()["reserved_seconds"], 360)
        self.clock.advance(109.2)
        with self.assertRaises(SandboxBudgetExceeded):
            budget.extend(300)
        self.assertAlmostEqual(budget.reservation_remaining_seconds(), 0.8)

    def test_short_extension_does_not_refund_previous_cloud_timeout(self):
        budget = self.budget()
        budget.begin(300)
        self.clock.advance(10)
        self.assertEqual(budget.extend(30), 30)
        self.assertEqual(self.ledger()["reserved_seconds"], 300)

    def test_crash_resume_charges_entire_reservation_without_reset(self):
        previous = self.budget()
        previous.begin(300)
        self.clock.advance(20)
        restored = self.budget()
        self.assertEqual(restored.snapshot()["used_seconds"], 300)
        self.assertFalse(restored.snapshot()["active"])
        restored.begin(300)
        self.assertEqual(self.ledger()["used_seconds"], 300)
        self.assertEqual(self.ledger()["reserved_seconds"], 300)

    def test_old_cleanup_cannot_refund_new_owner_reservation(self):
        previous = self.budget()
        previous.begin(300)
        current = self.budget()
        current.begin(100)
        expected = self.path.read_bytes()
        with self.assertRaisesRegex(SandboxBudgetError, "接管"):
            previous.finish(confirmed=True)
        self.assertEqual(self.path.read_bytes(), expected)

    def test_old_renewal_and_old_idle_object_cannot_overwrite_new_owner(self):
        for active in (False, True):
            with self.subTest(active=active):
                self.path.unlink(missing_ok=True)
                previous = self.budget()
                if active:
                    previous.begin(100)
                current = self.budget()
                current.begin(50)
                expected = self.path.read_bytes()
                with self.assertRaisesRegex(SandboxBudgetError, "接管"):
                    previous.extend(100) if active else previous.begin(100)
                self.assertEqual(self.path.read_bytes(), expected)

    def test_unknown_cleanup_charges_full_reservation_once(self):
        budget = self.budget()
        budget.begin(300)
        self.clock.advance(20)
        budget.finish(confirmed=False)
        budget.finish(confirmed=True)
        self.assertEqual(budget.snapshot()["used_seconds"], 300)
        self.assertEqual(self.budget().snapshot()["used_seconds"], 300)

    def test_actual_overrun_is_charged_and_blocks_next_create(self):
        budget = self.budget(30)
        self.assertEqual(budget.begin(300), 30)
        self.clock.advance(35)
        budget.finish(confirmed=True)
        self.assertEqual(budget.snapshot()["used_seconds"], 35)
        with self.assertRaises(SandboxBudgetExceeded):
            budget.begin(300)

    def test_changed_limit_does_not_reset_accounting(self):
        previous = self.budget()
        previous.begin(300)
        self.clock.advance(25)
        previous.finish(confirmed=True)
        current = self.budget(30)
        self.assertEqual(current.begin(300), 5)

    def test_write_failure_blocks_create_and_renew_without_refunding(self):
        budget = self.budget()
        with patch.object(budget, "_write", side_effect=OSError("private detail")):
            with self.assertRaises(SandboxBudgetError) as error:
                budget.begin(300)
        self.assertNotIn("private detail", str(error.exception))
        with self.assertRaises(SandboxBudgetError):
            budget.begin(300)
        current = self.budget()
        current.begin(100)
        expected = self.path.read_bytes()
        with patch.object(current, "_write", side_effect=OSError):
            with self.assertRaises(SandboxBudgetError):
                current.extend(300)
        self.assertEqual(self.path.read_bytes(), expected)
        with self.assertRaises(SandboxBudgetError):
            current.finish(confirmed=True)

    def test_invalid_missing_or_mismatched_ledger_is_not_overwritten(self):
        budget = self.budget()
        original = self.ledger()
        invalid = [b"{}", b"x" * 4097,
                   json.dumps(dict(original, session_id="b" * 32)).encode(),
                   json.dumps(dict(original, version=True)).encode(),
                   json.dumps(dict(original, used_seconds=True)).encode(),
                   json.dumps(dict(original, used_seconds=-1)).encode(),
                   json.dumps(dict(original, reserved_seconds=float("inf"))).encode(),
                   json.dumps(dict(original, unexpected=1)).encode(),
                   self.path.read_bytes().replace(b'"version": 1', b'"version": 1, "version": 1')]
        for raw in invalid:
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                with self.assertRaises(SandboxBudgetError):
                    self.budget()
                self.assertEqual(self.path.read_bytes(), raw)
        self.path.unlink()
        with self.assertRaises(SandboxBudgetError):
            budget.begin(30)

    def test_reparse_directory_is_rejected_before_writing(self):
        actual = os.lstat

        def fake_lstat(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            if Path(path) == self.path.parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch("app.sandbox_budget.os.lstat", side_effect=fake_lstat):
            with self.assertRaisesRegex(SandboxBudgetError, "不安全"):
                self.budget()
        self.assertFalse(self.path.exists())

    def test_hard_linked_ledger_is_rejected(self):
        self.budget()
        linked = self.path.with_name("linked.json")
        os.link(self.path, linked)
        with self.assertRaisesRegex(SandboxBudgetError, "硬链接"):
            self.budget()

    def test_os_lock_prevents_other_process_from_taking_ownership(self):
        from app.sandbox_budget import _storage_lock

        budget = self.budget()
        budget.begin(100)
        expected = self.path.read_bytes()
        script = (
            "from pathlib import Path; import sys; "
            "from app.sandbox_budget import SandboxBudget, SandboxBudgetError\n"
            "try: SandboxBudget(600, path=Path(sys.argv[1]), session_id='a'*32)\n"
            "except SandboxBudgetError: sys.exit(23)\n"
        )
        with _storage_lock(self.path):
            result = subprocess.run([sys.executable, "-c", script, str(self.path)],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.path.read_bytes(), expected)

    def test_in_memory_budget_and_strict_clock_and_input_validation(self):
        budget = self.budget(persistent=False)
        self.assertEqual(budget.begin(300), 300)
        self.assertFalse(self.path.exists())
        with self.assertRaises(SandboxBudgetError):
            budget.begin(30)
        with self.assertRaises(ValueError):
            budget.extend(True)
        self.clock.now = math.nan
        with self.assertRaisesRegex(SandboxBudgetError, "计时"):
            budget.snapshot()
        with self.assertRaises(ValueError):
            SandboxBudget(True, session_id=SESSION)
        with self.assertRaises(ValueError):
            SandboxBudget(30, session_id="invalid")


if __name__ == "__main__":
    unittest.main()
