"""保留候选检索、一次 diff 恢复审批和七日清理测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.schemas import ToolMessage
from app.workflow import FileApprovalState, FileApprovalStatus, FileApprovalWorkflow
from tool.pending_changes import (
    cleanup_expired_pending,
    list_pending_entries,
    store_candidate,
    store_manifest,
)
from tool.registry import call_tool


access = importlib.import_module("tool.file_access")


class PendingToolsTests(unittest.TestCase):
    def setUp(self):
        test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="pending-tool-tests-", dir=test_directory)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch.object(access, "_PROJECT_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def invoke(self, name: str, arguments: dict, callback=None) -> dict:
        raw = call_tool(
            {"id": "pending-1", "name": name, "arguments": arguments},
            confirmation_callback=callback,
            workflow=FileApprovalWorkflow(),
        )
        return json.loads(ToolMessage.model_validate_json(raw).model_dump_json())["result"]

    def rejected_create(self) -> tuple[str, list]:
        requests = []
        result = self.invoke(
            "write_file",
            {"path": "future/file.txt", "content": "saved candidate\n", "mode": "create"},
            lambda request: requests.append(request) or request.phase == "operation",
        )
        self.assertEqual(result["status"], "denied")
        identifier = Path(requests[1].pending_file).stem
        return identifier, requests

    def test_rejected_candidate_is_listed_with_safe_manifest_metadata(self):
        identifier, _ = self.rejected_create()
        result = self.invoke("list_pending_changes", {"max_results": 10})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["count"], 1)
        entry = result["output"]["entries"][0]
        self.assertEqual(entry["pending_file_id"], identifier)
        self.assertEqual(entry["path"], "future/file.txt")
        self.assertEqual(entry["operation"], "write_create")
        manifest = self.root / "data/pending" / f"{identifier}.json"
        rendered = manifest.read_text(encoding="utf-8")
        self.assertNotIn("saved candidate", rendered)
        self.assertNotIn("diff", rendered.casefold())

    def test_recovery_no_keeps_exact_pair_and_yes_applies_without_new_candidate(self):
        identifier, _ = self.rejected_create()
        pending = self.root / "data/pending"
        before_names = sorted(path.name for path in pending.iterdir())
        requests = []
        denied = self.invoke(
            "apply_pending_change",
            {"pending_file_id": identifier},
            lambda request: requests.append(request) or False,
        )
        self.assertEqual(denied["status"], "denied")
        self.assertEqual([request.phase for request in requests], ["recovery_diff"])
        self.assertIn("+saved candidate", requests[0].diff_preview)
        self.assertEqual(sorted(path.name for path in pending.iterdir()), before_names)
        self.assertFalse((self.root / "future/file.txt").exists())

        states = []
        workflow = FileApprovalWorkflow()

        def approve(request):
            state = workflow.state_for("future/file.txt")
            states.append(state.status)
            return True

        raw = call_tool(
            {"id": "pending-2", "name": "apply_pending_change", "arguments": {
                "pending_file_id": identifier
            }},
            confirmation_callback=approve,
            workflow=workflow,
        )
        result = json.loads(raw)["result"]
        self.assertEqual(result["status"], "success")
        self.assertEqual(states, [FileApprovalStatus.RECOVERY_DIFF_PENDING])
        self.assertEqual((self.root / "future/file.txt").read_text(encoding="utf-8"), "saved candidate\n")
        self.assertFalse((pending / f"{identifier}.bin").exists())
        self.assertFalse((pending / f"{identifier}.json").exists())
        self.assertEqual(workflow.state_for("future/file.txt"), FileApprovalState(path="future/file.txt"))

    def test_changed_target_tampered_manifest_and_missing_confirmation_do_not_apply(self):
        identifier, _ = self.rejected_create()
        target = self.root / "future/file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("someone else", encoding="utf-8")
        callback = Mock(return_value=True)
        result = self.invoke("apply_pending_change", {"pending_file_id": identifier}, callback)
        self.assertEqual(result["status"], "error")
        callback.assert_not_called()
        self.assertEqual(target.read_text(encoding="utf-8"), "someone else")

        manifest = self.root / "data/pending" / f"{identifier}.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["candidate_sha256"] = "0" * 64
        manifest.write_text(json.dumps(value), encoding="utf-8")
        listing = self.invoke("list_pending_changes", {})
        self.assertEqual(listing["output"]["entries"], [])
        self.assertTrue((self.root / "data/pending" / f"{identifier}.bin").exists())

    def test_existing_editor_candidate_can_be_recovered_and_target_change_is_rejected(self):
        target = self.root / "source.txt"
        target.write_text("old\n", encoding="utf-8")
        requests = []
        denied = self.invoke(
            "editor_file",
            {"path": "source.txt", "old_text": "old", "new_text": "new"},
            lambda request: requests.append(request) or request.phase == "operation",
        )
        self.assertEqual(denied["status"], "denied")
        identifier = Path(requests[1].pending_file).stem
        target.write_text("changed\n", encoding="utf-8")
        result = self.invoke(
            "apply_pending_change", {"pending_file_id": identifier}, lambda request: True
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(target.read_text(encoding="utf-8"), "changed\n")
        self.assertTrue((self.root / "data/pending" / f"{identifier}.bin").exists())

    def test_non_utf8_overwrite_candidate_can_be_listed_and_recovered(self):
        target = self.root / "binary-original.dat"
        target.write_bytes(b"\xff\xfeold\x80")
        requests = []
        denied = self.invoke(
            "write_file",
            {
                "path": "binary-original.dat",
                "content": "replacement\n",
                "mode": "overwrite",
            },
            lambda request: requests.append(request) or request.phase == "operation",
        )
        self.assertEqual(denied["status"], "denied")
        identifier = Path(requests[1].pending_file).stem

        listing = self.invoke("list_pending_changes", {"max_results": 10})
        self.assertEqual(listing["status"], "success")
        self.assertEqual(listing["output"]["entries"][0]["pending_file_id"], identifier)

        recovered_requests = []
        recovered = self.invoke(
            "apply_pending_change",
            {"pending_file_id": identifier},
            lambda request: recovered_requests.append(request) or True,
        )
        self.assertEqual(recovered["status"], "success")
        self.assertEqual([request.phase for request in recovered_requests], ["recovery_diff"])
        self.assertIn(r"\xff", recovered_requests[0].diff_preview)
        self.assertEqual(target.read_bytes(), b"replacement\n")

    def test_seven_day_cleanup_removes_only_safe_exact_uuid_files(self):
        created = datetime(2026, 9, 1, tzinfo=timezone.utc)
        identifier = "a" * 32
        candidate = store_candidate(identifier, b"content")
        store_manifest(
            candidate,
            path="new.txt",
            source_tool="write_file",
            operation="write_create",
            encoding="utf-8",
            original_size=None,
            original_sha256=None,
            clock=lambda: created,
        )
        pending = self.root / "data/pending"
        unrelated = pending / "keep.txt"
        unrelated.write_text("KEEP", encoding="utf-8")
        cleanup_expired_pending(now=created + timedelta(days=7))
        self.assertFalse((pending / f"{identifier}.bin").exists())
        self.assertFalse((pending / f"{identifier}.json").exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "KEEP")
        entries, truncated = list_pending_entries(10, now=created + timedelta(days=7))
        self.assertEqual(entries, [])
        self.assertFalse(truncated)

    def test_cleanup_and_listing_ignore_hardlinked_manifest(self):
        pending = self.root / "data/pending"
        pending.mkdir(parents=True)
        outside = self.root / "outside.json"
        outside.write_text("KEEP", encoding="utf-8")
        identifier = "b" * 32
        linked = pending / f"{identifier}.json"
        try:
            os.link(outside, linked)
        except OSError:
            self.skipTest("hard links unavailable")
        (pending / f"{identifier}.bin").write_bytes(b"orphan")
        old = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(linked, (old, old))
        os.utime(pending / f"{identifier}.bin", (old, old))
        cleanup_expired_pending(now=datetime(2026, 9, 9, tzinfo=timezone.utc))
        entries, _ = list_pending_entries(10)
        self.assertEqual(entries, [])
        self.assertEqual(outside.read_text(encoding="utf-8"), "KEEP")
        self.assertTrue(linked.exists())

    def test_cleanup_keeps_fresh_legacy_bin_orphan_and_removes_expired_one(self):
        pending = self.root / "data/pending"
        pending.mkdir(parents=True)
        now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
        fresh = pending / ("c" * 32 + ".bin")
        expired = pending / ("d" * 32 + ".bin")
        unrelated = pending / "legacy-candidate.bin"
        fresh.write_bytes(b"fresh")
        expired.write_bytes(b"expired")
        unrelated.write_bytes(b"KEEP")
        fresh_timestamp = (now - timedelta(days=6, hours=23)).timestamp()
        expired_timestamp = (now - timedelta(days=7)).timestamp()
        os.utime(fresh, (fresh_timestamp, fresh_timestamp))
        os.utime(expired, (expired_timestamp, expired_timestamp))
        os.utime(unrelated, (expired_timestamp, expired_timestamp))

        cleanup_expired_pending(now=now)

        self.assertTrue(fresh.exists())
        self.assertFalse(expired.exists())
        self.assertEqual(unrelated.read_bytes(), b"KEEP")


if __name__ == "__main__":
    unittest.main()
