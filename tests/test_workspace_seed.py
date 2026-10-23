"""Stage 5B4a host snapshot and typed bootstrap uploader tests."""

from __future__ import annotations

import base64
import hashlib
import unittest
from unittest.mock import Mock, patch

from app import workspace_seed as seed_module
from app.sandbox_transport import (
    SandboxFileEntry,
    SandboxListResult,
    SandboxReadResult,
    SandboxWriteResult,
)
from app.schemas import ReadFileOutput
from app.workspace_seed import (
    MAX_SEED_FILE_BYTES,
    MAX_SEED_FILES,
    MAX_SEED_TOTAL_BYTES,
    WorkspaceSeedEntry,
    WorkspaceSeedError,
    WorkspaceSeedSnapshot,
    WorkspaceSeedUploadError,
    capture_workspace_seed,
    upload_workspace_seed,
)
from tool.project_scan import ScanResult, ScannedFile


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


def _read_output(path: str, data: bytes) -> ReadFileOutput:
    try:
        content = data.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(data).decode("ascii")
        encoding = "base64"
    return ReadFileOutput(
        path=path,
        encoding=encoding,
        content=content,
        offset=0,
        bytes_read=len(data),
        total_bytes=len(data),
        truncated=False,
    )


class _MemoryTransport:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[object, ...]] = []
        self.fail_write_at: int | None = None

    def write_file(self, path, data, *, overwrite, timeout_seconds):
        self.calls.append(("write", path, data, overwrite, timeout_seconds))
        if self.fail_write_at == len(self.files):
            raise OSError("SECRET upload failure")
        if path in self.files or overwrite is not False:
            raise FileExistsError
        self.files[path] = data
        return SandboxWriteResult(len(data))

    def read_file(self, path, *, offset, max_bytes, timeout_seconds):
        self.calls.append(("read", path, offset, max_bytes, timeout_seconds))
        data = self.files[path]
        return SandboxReadResult(data[offset : offset + max_bytes], len(data))

    def list_files(
        self,
        pattern,
        *,
        workspace_root,
        max_results,
        timeout_seconds,
        path_predicate,
    ):
        self.calls.append(
            ("list", pattern, workspace_root, max_results, timeout_seconds)
        )
        entries = tuple(
            SandboxFileEntry(path, len(data))
            for path, data in sorted(self.files.items())
            if path_predicate(path[len(workspace_root) + 1 :], False)
        )
        return SandboxListResult(entries[:max_results], len(entries) > max_results)


class WorkspaceSeedCaptureTests(unittest.TestCase):
    def test_capture_sorts_hashes_binary_and_has_body_free_repr(self):
        files = {"z.bin": b"\x00\xff", "app/a.py": b"print('a')\n"}
        scan = ScanResult(
            files=[
                ScannedFile("z.bin", len(files["z.bin"])),
                ScannedFile("app/a.py", len(files["app/a.py"])),
            ],
            truncated=False,
        )

        def fake_read(arguments):
            return _read_output(arguments.path, files[arguments.path])

        with patch.object(seed_module, "scan_project_files", return_value=scan), patch.object(
            seed_module, "read_file", side_effect=fake_read
        ):
            snapshot = capture_workspace_seed()

        self.assertEqual([item.path for item in snapshot.entries], ["app/a.py", "z.bin"])
        self.assertEqual(snapshot.entries[1].data, b"\x00\xff")
        self.assertEqual(snapshot.total_bytes, sum(map(len, files.values())))
        self.assertEqual(len(snapshot.digest), 64)
        self.assertNotIn("print", repr(snapshot))
        self.assertNotIn("print", repr(snapshot.entries[0]))

        changed_snapshot = WorkspaceSeedSnapshot(
            (_entry("app/a.py", b"changed"), _entry("z.bin", b"\x00\xff"))
        )
        self.assertNotEqual(snapshot.digest, changed_snapshot.digest)

    def test_capture_hard_excludes_control_plane_env_and_cache_paths(self):
        paths = (
            "safe.py",
            "data/public.txt",
            "skills/demo/SKILL.md",
            ".agent/tool_policy.json",
            ".git/config",
            ".venv/pyvenv.cfg",
            "code.md",
            ".env",
            ".env.example",
            ".envrc",
            "pkg/.ENV.production",
            "pkg/server.key",
            "pkg/credentials",
            "pkg/token",
            "pkg/__pycache__/x.pyc",
            "pkg/.cache/value",
        )
        scan = ScanResult(
            files=[ScannedFile(path, 1) for path in paths],
            truncated=False,
        )
        seen: list[str] = []

        def fake_read(arguments):
            seen.append(arguments.path)
            return _read_output(arguments.path, b"x")

        with patch.object(seed_module, "scan_project_files", return_value=scan), patch.object(
            seed_module, "read_file", side_effect=fake_read
        ):
            snapshot = capture_workspace_seed()
        self.assertEqual(seen, ["safe.py"])
        self.assertEqual([item.path for item in snapshot.entries], ["safe.py"])

    def test_seed_entries_reject_sealed_paths_before_any_upload(self):
        for path in (".envrc", "private/server.key", "credentials", "token"):
            with self.subTest(path=path):
                with self.assertRaises(WorkspaceSeedError):
                    _entry(path, b"x")

    def test_capture_rejects_truncated_count_file_and_total_limits_before_read(self):
        cases = (
            ScanResult([], True),
            ScanResult(
                [ScannedFile(f"f{index:04}.txt", 0) for index in range(MAX_SEED_FILES + 1)],
                False,
            ),
            ScanResult([ScannedFile("large.bin", MAX_SEED_FILE_BYTES + 1)], False),
            ScanResult(
                [
                    ScannedFile(f"part{index:02}.bin", MAX_SEED_FILE_BYTES)
                    for index in range(MAX_SEED_TOTAL_BYTES // MAX_SEED_FILE_BYTES + 1)
                ],
                False,
            ),
        )
        for scan in cases:
            with self.subTest(files=len(scan.files), truncated=scan.truncated):
                reader = Mock(side_effect=AssertionError("must reject before reading"))
                with patch.object(seed_module, "scan_project_files", return_value=scan), patch.object(
                    seed_module, "read_file", reader
                ), self.assertRaises(WorkspaceSeedError):
                    capture_workspace_seed()
                reader.assert_not_called()

    def test_capture_rejects_scan_read_race_and_incomplete_read(self):
        scan = ScanResult([ScannedFile("app/a.py", 3)], False)
        incomplete = ReadFileOutput(
            path="app/a.py",
            encoding="utf-8",
            content="xy",
            offset=0,
            bytes_read=2,
            total_bytes=2,
            truncated=False,
        )
        with patch.object(seed_module, "scan_project_files", return_value=scan), patch.object(
            seed_module, "read_file", return_value=incomplete
        ), self.assertRaises(WorkspaceSeedError):
            capture_workspace_seed()

    def test_entry_and_snapshot_reject_invalid_metadata_order_and_duplicates(self):
        with self.assertRaises(WorkspaceSeedError):
            _entry(".env.example", b"secret")
        with self.assertRaises(WorkspaceSeedError):
            WorkspaceSeedEntry("a.txt", 1, "0" * 64, b"x")
        a = _entry("a.txt", b"a")
        b = _entry("b.txt", b"b")
        for entries in ((b, a), (a, a)):
            with self.subTest(entries=entries), self.assertRaises(WorkspaceSeedError):
                WorkspaceSeedSnapshot(entries)


class WorkspaceSeedUploadTests(unittest.TestCase):
    root = "/workspace/agnet-study/" + "a" * 32

    def test_upload_create_only_writes_and_exactly_reads_every_file(self):
        snapshot = WorkspaceSeedSnapshot((_entry("a.txt", b"a"), _entry("pkg/b.bin", b"\x00b")))
        transport = _MemoryTransport()
        upload_workspace_seed(transport, self.root, snapshot)
        self.assertEqual(
            transport.files,
            {
                self.root + "/a.txt": b"a",
                self.root + "/pkg/b.bin": b"\x00b",
            },
        )
        writes = [call for call in transport.calls if call[0] == "write"]
        reads = [call for call in transport.calls if call[0] == "read"]
        lists = [call for call in transport.calls if call[0] == "list"]
        self.assertTrue(all(call[3] is False for call in writes))
        self.assertEqual([call[3] for call in reads], [2, 3])
        self.assertEqual(len(lists), 2)

    def test_upload_rejects_wrong_root_and_tampered_path_size_hash_before_write(self):
        mutators = (
            lambda entry, snapshot: object.__setattr__(entry, "path", "../escape"),
            lambda entry, snapshot: object.__setattr__(entry, "size", 2),
            lambda entry, snapshot: object.__setattr__(entry, "sha256", "0" * 64),
            lambda entry, snapshot: object.__setattr__(snapshot, "digest", "0" * 64),
        )
        for mutate in mutators:
            with self.subTest(mutate=mutate):
                entry = _entry("a.txt", b"a")
                snapshot = WorkspaceSeedSnapshot((entry,))
                mutate(entry, snapshot)
                transport = _MemoryTransport()
                with self.assertRaises((WorkspaceSeedError, WorkspaceSeedUploadError)):
                    upload_workspace_seed(transport, self.root, snapshot)
                self.assertEqual(transport.calls, [])

        transport = _MemoryTransport()
        with self.assertRaises(WorkspaceSeedUploadError):
            upload_workspace_seed(transport, "/mnt/oss/" + "a" * 32, WorkspaceSeedSnapshot(()))
        self.assertEqual(transport.calls, [])

    def test_upload_rejects_malicious_transport_results_and_sanitizes_partial_failure(self):
        snapshot = WorkspaceSeedSnapshot((_entry("a.txt", b"a"), _entry("b.txt", b"b")))

        bad_write = _MemoryTransport()
        bad_write.write_file = Mock(return_value=SandboxWriteResult(0))
        with self.assertRaises(WorkspaceSeedUploadError):
            upload_workspace_seed(bad_write, self.root, snapshot)

        bad_read = _MemoryTransport()
        original_read = bad_read.read_file
        bad_read.read_file = Mock(
            side_effect=lambda *args, **kwargs: SandboxReadResult(b"x", 1)
        )
        with self.assertRaises(WorkspaceSeedUploadError):
            upload_workspace_seed(bad_read, self.root, snapshot)
        self.assertIsNotNone(original_read)

        partial = _MemoryTransport()
        partial.fail_write_at = 1
        with self.assertRaises(WorkspaceSeedUploadError) as caught:
            upload_workspace_seed(partial, self.root, snapshot)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(set(partial.files), {self.root + "/a.txt"})
        self.assertFalse(any(call[1].endswith("b.txt") for call in partial.calls if call[0] == "read"))

    def test_upload_requires_empty_workspace_and_exact_final_manifest(self):
        snapshot = WorkspaceSeedSnapshot((_entry("a.txt", b"a"),))

        occupied = _MemoryTransport()
        occupied.files[self.root + "/residual.txt"] = b"old"
        with self.assertRaises(WorkspaceSeedUploadError):
            upload_workspace_seed(occupied, self.root, snapshot)
        self.assertFalse(any(call[0] == "write" for call in occupied.calls))

        class ExtraFileTransport(_MemoryTransport):
            def list_files(self, *args, **kwargs):
                result = super().list_files(*args, **kwargs)
                if self.files:
                    return SandboxListResult(
                        result.entries
                        + (SandboxFileEntry(self.root + "/extra.txt", 1),),
                        False,
                    )
                return result

        extra = ExtraFileTransport()
        extra.root = self.root
        with self.assertRaises(WorkspaceSeedUploadError):
            upload_workspace_seed(extra, self.root, snapshot)

        truncated = _MemoryTransport()
        truncated.list_files = Mock(return_value=SandboxListResult((), True))
        with self.assertRaises(WorkspaceSeedUploadError):
            upload_workspace_seed(truncated, self.root, WorkspaceSeedSnapshot(()))


if __name__ == "__main__":
    unittest.main()
