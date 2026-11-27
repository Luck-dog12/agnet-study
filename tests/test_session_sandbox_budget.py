"""Budget sidecars remain distinct from events during named-session recovery."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.sandbox_budget import SandboxBudget
from app.workflow import SessionEventError, SessionEventWorkflow


class SessionSandboxBudgetTests(unittest.TestCase):
    def test_named_remote_session_resumes_with_budget_lock_and_partial_temp(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / "named-cloud-task"
            with SessionEventWorkflow(storage_directory=directory, recover=False,
                                      execution_backend_name="aliyun-sandbox") as workflow:
                session_id = workflow.session_id
                budget = SandboxBudget(3600, session_id=session_id,
                                       path=directory / "sandbox-budget.json")
                budget.begin(310)
            temporary = directory / ("sandbox-budget.json." + "b" * 32 + ".tmp")
            temporary.write_bytes(b'{"partial":')
            before = (directory / "sandbox-budget.json").read_bytes()
            with SessionEventWorkflow(storage_directory=directory, recover=False,
                                      require_existing_identity=True) as resumed:
                self.assertEqual(resumed.session_id, session_id)
                self.assertEqual(resumed.execution_backend_name, "aliyun-sandbox")
                self.assertEqual(resumed.events, ())
                self.assertEqual((directory / "sandbox-budget.json").read_bytes(), before)
                restored_budget = SandboxBudget(3600, session_id=session_id,
                                               path=directory / "sandbox-budget.json")
                self.assertEqual(restored_budget.snapshot()["used_seconds"], 310)
            self.assertEqual(temporary.read_bytes(), b'{"partial":')

    def test_similarly_named_unknown_objects_remain_rejected(self):
        invalid_names = (
            "sandbox-budget.json.bak", "sandbox-budget.json.lock.bak",
            "sandbox-budget.json." + "B" * 32 + ".tmp",
            "sandbox-budget.json." + "a" * 31 + ".tmp",
            "sandbox-budget.json." + "a" * 32 + ".tmp.extra",
            "sandbox-budget.json.tmp", "sandbox-budget-other.json",
        )
        for name in invalid_names:
            with self.subTest(name=name), TemporaryDirectory() as root:
                directory = Path(root) / "named-cloud-task"
                with SessionEventWorkflow(storage_directory=directory, recover=False):
                    pass
                payload = directory / name
                payload.write_bytes(b"do not modify")
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=directory, recover=False)
                self.assertEqual(payload.read_bytes(), b"do not modify")

    def test_budget_sidecars_and_temps_must_be_safe_regular_files(self):
        names = ("sandbox-budget.json", "sandbox-budget.json.lock",
                 "sandbox-budget.json." + "a" * 32 + ".tmp")
        for name in names:
            for attack in ("directory", "hardlink", "symlink"):
                with self.subTest(name=name, attack=attack), TemporaryDirectory() as root:
                    directory = Path(root) / "named-cloud-task"
                    with SessionEventWorkflow(storage_directory=directory, recover=False):
                        pass
                    target = directory / name
                    outside = Path(root) / "outside.json"
                    outside.write_bytes(b"outside")
                    try:
                        if attack == "directory":
                            target.mkdir()
                        elif attack == "hardlink":
                            os.link(outside, target)
                        else:
                            target.symlink_to(outside)
                    except (OSError, NotImplementedError):
                        if attack == "symlink":
                            continue
                        raise
                    with self.assertRaises(SessionEventError):
                        SessionEventWorkflow(storage_directory=directory, recover=False)
                    self.assertEqual(outside.read_bytes(), b"outside")

    def test_event_loader_rechecks_budget_sidecar_file_type(self):
        with TemporaryDirectory() as root:
            directory = Path(root) / "named-cloud-task"
            with SessionEventWorkflow(storage_directory=directory, recover=False) as workflow:
                (directory / "sandbox-budget.json.lock").mkdir()
                with self.assertRaises(OSError):
                    workflow._load_events()


if __name__ == "__main__":
    unittest.main()
