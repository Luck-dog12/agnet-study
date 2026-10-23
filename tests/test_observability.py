"""工具可观测性的离线安全测试。"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest.mock import patch

from app.observability import Observability, tool_uuid
from app.schemas import ToolCall, ToolMessage, ToolResult
from app.workflow import FileApprovalWorkflow
from tool import file_access
from tool.registry import call_tool


class ObservabilityTests(unittest.TestCase):
    fixed_now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def observer(self, project: Path) -> Observability:
        log_dir = project / "data" / "log"
        self.project_patch = patch("app.observability._PROJECT_ROOT", project)
        self.directory_patch = patch("app.observability._LOG_DIRECTORY", log_dir)
        self.project_patch.start()
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)
        self.addCleanup(self.project_patch.stop)
        return Observability(clock=lambda: self.fixed_now)

    def records(self, project: Path) -> tuple[Path, list[dict[str, object]]]:
        files = list((project / "data" / "log").glob("tool-calls-*.json"))
        self.assertEqual(len(files), 1)
        return files[0], json.loads(files[0].read_text(encoding="utf-8"))

    def test_stable_tool_uuid_and_unique_invocation_uuid(self):
        observability_module = importlib.import_module("app.observability")
        registry_module = importlib.import_module("tool.registry")
        self.assertEqual(observability_module._REGISTERED_TOOL_NAMES, frozenset(registry_module._REGISTRY))
        with TemporaryDirectory() as directory:
            project = Path(directory)
            observer = self.observer(project)
            first = observer.start_tool("read_file", {"path": "secret-marker.txt"})
            second = observer.start_tool("read_file", {"path": "other.txt"})
            third = observer.start_tool("grep", {"pattern": "needle-marker"})
        self.assertEqual(first.tool_uuid, second.tool_uuid)
        self.assertNotEqual(first.tool_uuid, third.tool_uuid)
        self.assertEqual(first.tool_uuid, tool_uuid("read_file"))
        self.assertEqual(uuid.UUID(first.tool_uuid).version, 5)
        self.assertEqual(uuid.UUID(first.invocation_uuid).version, 4)
        self.assertNotEqual(first.invocation_uuid, second.invocation_uuid)

    def test_all_events_write_one_daily_json_array_without_terminal_output(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            terminal = io.StringIO()
            observer = self.observer(project)
            with patch("sys.stderr", terminal):
                invocation = observer.start_tool("write_file", {
                    "path": ".env.SENSITIVE_PATH", "content": "SENSITIVE_CONTENT",
                    "mode": "create", "encoding": "utf-8",
                })
                observer.finish_tool(invocation, ToolResult(status="denied", error="SENSITIVE_ERROR"))
            log_file, records = self.records(project)
            self.assertRegex(log_file.name, r"^tool-calls-\d{4}-\d{2}-\d{2}\.json$")
            self.assertEqual(terminal.getvalue(), "")
            self.assertEqual([item["event"] for item in records], ["tool_call", "tool_result"])
            self.assertEqual(records[0]["invocation_uuid"], records[1]["invocation_uuid"])
            rendered = log_file.read_text(encoding="utf-8")
            self.assertIsInstance(json.loads(rendered), list)
            # write_file 本身有 create/overwrite 的安全输入摘要；这里只验证
            # 已取消的 observability 顶层记录模式没有残留。
            self.assertTrue(all("mode" not in record for record in records))
            for secret in (".env.SENSITIVE_PATH", "SENSITIVE_CONTENT", "SENSITIVE_ERROR"):
                self.assertNotIn(secret, rendered)

    def test_record_limit_rotates_to_numbered_daily_shard_before_overflow(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            observer = self.observer(project)
            with patch("app.observability._MAX_LOG_RECORDS", 2):
                first = observer.start_tool("read_file", {"path": "hidden"})
                observer.finish_tool(first, ToolResult(status="success", output="ok"))
                observer.start_tool("grep", {"pattern": "hidden"})
            log_dir = project / "data/log"
            first_records = json.loads(
                (log_dir / "tool-calls-2026-09-09.json").read_text(encoding="utf-8")
            )
            second_records = json.loads(
                (log_dir / "tool-calls-2026-09-09-2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(first_records), 2)
            self.assertEqual(len(second_records), 1)
            from app import observability as module
            self.assertEqual(module._MAX_LOG_RECORDS, 2_000)

    def test_byte_limit_rotates_and_every_shard_stays_within_limit(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            observer = self.observer(project)
            byte_limit = 700
            with patch("app.observability._MAX_LOG_FILE_BYTES", byte_limit):
                for _ in range(8):
                    observer.start_tool("read_file", {"path": "hidden"})
            files = sorted((project / "data/log").glob("tool-calls-*.json"))
            self.assertGreater(len(files), 1)
            for log_file in files:
                self.assertLessEqual(log_file.stat().st_size, byte_limit)
                self.assertIsInstance(json.loads(log_file.read_text(encoding="utf-8")), list)

    def test_next_day_starts_again_from_unnumbered_file(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            current = [self.fixed_now]
            log_dir = project / "data/log"
            self.project_patch = patch("app.observability._PROJECT_ROOT", project)
            self.directory_patch = patch("app.observability._LOG_DIRECTORY", log_dir)
            self.project_patch.start()
            self.directory_patch.start()
            self.addCleanup(self.directory_patch.stop)
            self.addCleanup(self.project_patch.stop)
            observer = Observability(clock=lambda: current[0])
            with patch("app.observability._MAX_LOG_RECORDS", 1):
                observer.start_tool("read_file", {"path": "hidden"})
                observer.start_tool("read_file", {"path": "hidden"})
                current[0] = self.fixed_now.replace(day=10)
                observer.start_tool("read_file", {"path": "hidden"})
            self.assertTrue((log_dir / "tool-calls-2026-09-09-2.json").exists())
            self.assertTrue((log_dir / "tool-calls-2026-09-10.json").exists())
            self.assertFalse((log_dir / "tool-calls-2026-09-10-2.json").exists())

    def test_registry_records_unregistered_call_and_result_safely(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            observer = self.observer(project)
            raw = call_tool(
                ToolCall(id="call-1", name="SENSITIVE_FAKE_TOOL", arguments={"token": "SENSITIVE_TOKEN"}),
                observability=observer,
            )
            self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "error")
            log_file, records = self.records(project)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["tool_name"], "unregistered")
            self.assertEqual(records[0]["invocation_uuid"], records[1]["invocation_uuid"])
            rendered = log_file.read_text(encoding="utf-8")
            self.assertNotIn("SENSITIVE_FAKE_TOOL", rendered)
            self.assertNotIn("SENSITIVE_TOKEN", rendered)

    def test_diff_and_pending_candidate_never_enter_observability_log(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / "source.txt"
            target.write_text("SENSITIVE_BEFORE", encoding="utf-8")
            observer = self.observer(project)
            requests = []
            with patch.object(file_access, "_PROJECT_ROOT", project):
                raw = call_tool(
                    ToolCall(
                        id="call-1",
                        name="editor_file",
                        arguments={
                            "path": "source.txt",
                            "old_text": "SENSITIVE_BEFORE",
                            "new_text": "SENSITIVE_AFTER",
                        },
                    ),
                    confirmation_callback=lambda request: requests.append(request) or (
                        request.phase == "operation"
                    ),
                    workflow=FileApprovalWorkflow(),
                    observability=observer,
                )
            self.assertEqual(ToolMessage.model_validate_json(raw).result.status, "denied")
            self.assertEqual(target.read_text(encoding="utf-8"), "SENSITIVE_BEFORE")
            log_file, _ = self.records(project)
            rendered = log_file.read_text(encoding="utf-8")
            for secret in (
                "SENSITIVE_BEFORE",
                "SENSITIVE_AFTER",
                requests[1].diff_preview,
                requests[1].pending_file,
            ):
                self.assertNotIn(secret, rendered)

    def test_unvalidated_enumerations_and_scalars_are_never_echoed(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            observer = self.observer(project)
            observer.start_tool("bash_shell", {
                "command": "SENSITIVE_COMMAND", "paths": ["SENSITIVE_PATH"],
                "code": "SENSITIVE_CODE", "timeout_seconds": "SENSITIVE_TIMEOUT",
            })
            observer.start_tool("write_file", {
                "path": "SENSITIVE_PATH", "content": "SENSITIVE_CONTENT",
                "mode": "SENSITIVE_MODE", "encoding": "SENSITIVE_ENCODING",
            })
            observer.start_tool("grep", {
                "pattern": "SENSITIVE_PATTERN", "file_pattern": "SENSITIVE_GLOB",
                "case_sensitive": "SENSITIVE_BOOL", "max_results": "SENSITIVE_COUNT",
            })
            log_file, _ = self.records(project)
            rendered = log_file.read_text(encoding="utf-8")
            for secret in (
                "SENSITIVE_COMMAND", "SENSITIVE_PATH", "SENSITIVE_CODE", "SENSITIVE_TIMEOUT",
                "SENSITIVE_CONTENT", "SENSITIVE_MODE", "SENSITIVE_ENCODING",
                "SENSITIVE_PATTERN", "SENSITIVE_GLOB", "SENSITIVE_BOOL", "SENSITIVE_COUNT",
            ):
                self.assertNotIn(secret, rendered)

    def test_retention_keeps_today_and_previous_six_days_only(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            log_dir = project / "data" / "log"
            log_dir.mkdir(parents=True)
            for day in range(1, 10):
                (log_dir / f"tool-calls-2026-09-{day:02d}.json").write_text("[]", encoding="utf-8")
            (log_dir / "tool-calls-2026-09-01-2.json").write_text("[]", encoding="utf-8")
            (log_dir / "tool-calls-2026-09-09-2.json").write_text("[]", encoding="utf-8")
            unrelated = log_dir / "keep-me.json"
            unrelated.write_text("KEEP", encoding="utf-8")
            legacy = log_dir / "tool_calls.jsonl"
            legacy.write_text("legacy", encoding="utf-8")
            old_timestamp = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()
            os.utime(legacy, (old_timestamp, old_timestamp))
            observer = self.observer(project)
            observer.start_tool("read_file", {"path": "hidden"})
            self.assertFalse((log_dir / "tool-calls-2026-09-01.json").exists())
            self.assertFalse((log_dir / "tool-calls-2026-09-02.json").exists())
            self.assertEqual(
                sorted(path.name for path in log_dir.glob("tool-calls-*.json")),
                sorted(
                    [f"tool-calls-2026-09-{day:02d}.json" for day in range(3, 10)]
                    + ["tool-calls-2026-09-09-2.json"]
                ),
            )
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "KEEP")
            self.assertFalse(legacy.exists())

    def test_retention_does_not_delete_old_hardlink_or_symlink(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            log_dir = project / "data" / "log"
            log_dir.mkdir(parents=True)
            outside = Path(directory) / "outside.json"
            outside.write_text("KEEP", encoding="utf-8")
            hardlink = log_dir / "tool-calls-2026-01-01.json"
            try:
                hardlink.hardlink_to(outside)
            except OSError as exc:
                self.skipTest(f"当前文件系统不支持硬链接测试：{type(exc).__name__}")
            symlink = log_dir / "tool-calls-2026-01-02.json"
            symlink_created = False
            try:
                symlink.symlink_to(outside)
                symlink_created = True
            except OSError:
                pass
            observer = self.observer(project)
            observer.start_tool("read_file", {"path": "hidden"})
            self.assertTrue(hardlink.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "KEEP")
            if symlink_created:
                self.assertTrue(os.path.lexists(symlink))

    def test_hard_link_log_is_rejected_without_modifying_target(self):
        with TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            outside = Path(directory) / "outside.txt"
            log_dir = project / "data" / "log"
            log_dir.mkdir(parents=True)
            outside.write_text("KEEP", encoding="utf-8")
            observer = self.observer(project)
            expected_name = f"tool-calls-{self.fixed_now.astimezone().date().isoformat()}.json"
            log_file = log_dir / expected_name
            try:
                log_file.hardlink_to(outside)
            except OSError as exc:
                self.skipTest(f"当前文件系统不支持硬链接测试：{type(exc).__name__}")
            observer.start_tool("read_file", {"path": "hidden"})
            self.assertEqual(outside.read_text(encoding="utf-8"), "KEEP")
            second_shard = log_dir / f"tool-calls-{self.fixed_now.date().isoformat()}-2.json"
            self.assertFalse(second_shard.exists())
            self.assertEqual(observer.pending_count, 1)

    def test_corrupt_first_shard_is_preserved_and_current_event_stops(self):
        with TemporaryDirectory() as directory:
            project = Path(directory)
            log_dir = project / "data" / "log"
            log_dir.mkdir(parents=True)
            first_shard = log_dir / f"tool-calls-{self.fixed_now.date().isoformat()}.json"
            first_shard.write_text("SENSITIVE_CORRUPT_JSON", encoding="utf-8")
            observer = self.observer(project)

            observer.start_tool("read_file", {"path": "SENSITIVE_PATH"})

            self.assertEqual(first_shard.read_text(encoding="utf-8"), "SENSITIVE_CORRUPT_JSON")
            second_shard = log_dir / f"tool-calls-{self.fixed_now.date().isoformat()}-2.json"
            self.assertFalse(second_shard.exists())
            self.assertEqual(observer.pending_count, 1)

    def test_temporary_write_failure_is_buffered_and_retried_in_order(self):
        attempts: list[str] = []
        unavailable = [True]

        def append(record, _day):
            attempts.append(record["event"])
            if unavailable[0]:
                raise OSError("SENSITIVE_DISK_FAILURE")

        observer = Observability(clock=lambda: self.fixed_now)
        with patch("app.observability._append_record", side_effect=append):
            invocation = observer.start_tool("read_file", {"path": "SENSITIVE_PATH"})
            self.assertEqual(observer.pending_count, 1)
            unavailable[0] = False
            observer.finish_tool(
                invocation,
                ToolResult(status="error", error="SENSITIVE_ERROR"),
            )

        self.assertEqual(attempts, ["tool_call", "tool_call", "tool_result"])
        self.assertEqual(observer.pending_count, 0)
        self.assertEqual(observer.dropped_count, 0)
        self.assertTrue(observer.healthy)

    def test_pending_buffer_is_bounded_and_drops_newest_with_safe_health_counts(self):
        terminal = io.StringIO()
        observer = Observability(clock=lambda: self.fixed_now, pending_limit=2)
        with (
            patch("app.observability._append_record", side_effect=OSError("SENSITIVE_FAILURE")),
            patch("sys.stderr", terminal),
        ):
            invocation = observer.start_tool("read_file", {"path": "SENSITIVE_PATH"})
            observer.finish_tool(invocation, ToolResult(status="error", error="SENSITIVE_ERROR"))
            observer.start_tool("grep", {"pattern": "SENSITIVE_PATTERN"})
            flushed = observer.flush_pending()

        self.assertEqual(flushed, 0)
        self.assertEqual(observer.pending_count, 2)
        self.assertEqual(observer.dropped_count, 1)
        self.assertFalse(observer.healthy)
        self.assertEqual(terminal.getvalue(), "")
        health = repr(observer.health)
        for secret in ("SENSITIVE_FAILURE", "SENSITIVE_PATH", "SENSITIVE_ERROR", "SENSITIVE_PATTERN"):
            self.assertNotIn(secret, health)

    def test_partial_flush_removes_only_successfully_written_prefix(self):
        observer = Observability(clock=lambda: self.fixed_now, pending_limit=3)
        with patch("app.observability._append_record", side_effect=OSError("unavailable")):
            invocation = observer.start_tool("read_file", {"path": "hidden"})
            observer.finish_tool(invocation, ToolResult(status="success", output="ok"))
            observer.start_tool("grep", {"pattern": "hidden"})
        self.assertEqual(observer.pending_count, 3)

        attempts = [0]

        def partial_append(_record, _day):
            attempts[0] += 1
            if attempts[0] == 2:
                raise OSError("still unavailable")

        with patch("app.observability._append_record", side_effect=partial_append):
            self.assertEqual(observer.flush_pending(), 1)
        self.assertEqual(observer.pending_count, 2)

        with patch("app.observability._append_record"):
            self.assertEqual(observer.flush_pending(), 2)
        self.assertEqual(observer.pending_count, 0)

    def test_logging_failure_is_fail_soft_and_silent(self):
        terminal = io.StringIO()
        observer = Observability(clock=lambda: self.fixed_now)
        with patch("app.observability._append_record", side_effect=OSError("SENSITIVE_FAILURE")), patch("sys.stderr", terminal):
            invocation = observer.start_tool("read_file", {"path": "SENSITIVE_PATH"})
            observer.finish_tool(invocation, ToolResult(status="error", error="SENSITIVE_ERROR"))
        self.assertEqual(terminal.getvalue(), "")

    def test_import_and_construction_have_no_write_side_effect(self):
        with patch("pathlib.Path.mkdir") as mkdir, patch("os.open") as open_file:
            Observability()
        mkdir.assert_not_called()
        open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
