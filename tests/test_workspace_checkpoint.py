"""Stage 5B4b immutable OSS baseline checkpoint tests (offline only)."""

from __future__ import annotations

import hashlib
import json
import posixpath
import unittest

from app.execution_backend import ExecutionLease, WorkspaceDescription
from app.sandbox_transport import (
    SandboxFileEntry,
    SandboxListResult,
    SandboxPersistenceEntry,
    SandboxPersistenceListResult,
    SandboxReadResult,
    SandboxWriteResult,
)
from app.workspace_checkpoint import (
    CHECKPOINT_MANIFEST_NAME,
    WorkspaceCheckpointError,
    canonical_checkpoint_manifest,
    checkpoint_root_for,
    create_workspace_checkpoint_initializer,
    initialize_workspace_from_checkpoint,
    validate_checkpoint_manifest,
)
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot


def _entry(path: str, data: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


class _MemoryTransport:
    def __init__(
        self,
        workspace_root: str,
        *,
        persistent_files: dict[str, bytes] | None = None,
        persistent_dirs: set[str] | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.active: dict[str, bytes] = {}
        self.persistent_files = persistent_files if persistent_files is not None else {}
        self.persistent_dirs = persistent_dirs if persistent_dirs is not None else set()
        self.calls: list[tuple[object, ...]] = []
        self.force_truncated = False
        self.injected_entries: tuple[SandboxPersistenceEntry, ...] = ()

    def list_files(
        self,
        pattern,
        *,
        workspace_root,
        max_results,
        timeout_seconds,
        path_predicate,
    ):
        del pattern, timeout_seconds
        if workspace_root != self.workspace_root:
            raise ValueError
        entries = tuple(
            SandboxFileEntry(path, len(data))
            for path, data in sorted(self.active.items())
            if path_predicate(path[len(workspace_root) + 1 :], False)
        )
        return SandboxListResult(entries[:max_results], len(entries) > max_results)

    def write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        self.calls.append(("active-write", path, overwrite))
        if overwrite or path in self.active:
            raise FileExistsError
        self.active[path] = bytes(data)
        return SandboxWriteResult(len(data))

    def read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del timeout_seconds
        data = self.active[path]
        return SandboxReadResult(data[offset : offset + max_bytes], len(data))

    def persistence_write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        self.calls.append(("persistence-write", path, overwrite))
        if overwrite or path in self.persistent_files:
            raise FileExistsError
        self.persistent_files[path] = bytes(data)
        checkpoint_root = self._checkpoint_root(path)
        parent = posixpath.dirname(path)
        while parent.startswith(checkpoint_root + "/"):
            self.persistent_dirs.add(parent)
            parent = posixpath.dirname(parent)
        return SandboxWriteResult(len(data))

    def persistence_read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del timeout_seconds
        self.calls.append(("persistence-read", path))
        data = self.persistent_files[path]
        return SandboxReadResult(data[offset : offset + max_bytes], len(data))

    def persistence_list_files(
        self,
        checkpoint_root,
        *,
        max_results,
        max_depth,
        timeout_seconds,
    ):
        del max_depth, timeout_seconds
        self.calls.append(("persistence-list", checkpoint_root))
        entries = [
            SandboxPersistenceEntry(path, True, 0)
            for path in self.persistent_dirs
            if path.startswith(checkpoint_root + "/")
        ]
        entries.extend(
            SandboxPersistenceEntry(path, False, len(data))
            for path, data in self.persistent_files.items()
            if path.startswith(checkpoint_root + "/")
        )
        entries.extend(self.injected_entries)
        entries.sort(key=lambda item: item.path)
        return SandboxPersistenceListResult(
            tuple(entries[:max_results]),
            self.force_truncated or len(entries) > max_results,
        )

    @staticmethod
    def _checkpoint_root(path: str) -> str:
        marker = "/.agnet-study/checkpoints/v1/"
        prefix, remainder = path.split(marker, 1)
        session_id, digest, _tail = remainder.split("/", 2)
        return f"{prefix}{marker}{session_id}/{digest}"


class WorkspaceCheckpointTests(unittest.TestCase):
    session_id = "a" * 32
    workspace_root = "/workspace/agnet-study/" + session_id
    persistence_root = "/mnt/oss"

    def _lease(self) -> ExecutionLease:
        return ExecutionLease(
            workspace=WorkspaceDescription(
                backend_name="aliyun-sandbox",
                session_id=self.session_id,
                workspace_root=self.workspace_root,
                persistence_root=self.persistence_root,
                control_plane_isolated=True,
            ),
            sandbox_id="sandbox-test",
        )

    def test_manifest_is_canonical_strict_and_bound_to_host_snapshot(self):
        snapshot = WorkspaceSeedSnapshot((_entry("a.py", b"x"),))
        manifest = canonical_checkpoint_manifest(snapshot, self.session_id)
        self.assertEqual(
            manifest,
            json.dumps(
                json.loads(manifest),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        validate_checkpoint_manifest(manifest, snapshot, self.session_id)

        invalid = (
            manifest.replace(b'"schema_version":1', b'"schema_version":true'),
            manifest[:-1] + b',"extra":1}',
            manifest.replace(b'"total_bytes":1', b'"total_bytes":NaN'),
            b'{"schema_version":1,"schema_version":1}',
            manifest.replace(b'"session_id":"' + b"a" * 32 + b'",', b""),
            canonical_checkpoint_manifest(
                WorkspaceSeedSnapshot((_entry("a.py", b"y"),)),
                self.session_id,
            ),
        )
        for data in invalid:
            with self.subTest(data=data[:40]), self.assertRaises(WorkspaceCheckpointError):
                validate_checkpoint_manifest(data, snapshot, self.session_id)

    def test_manifest_has_separate_bounded_capacity_above_one_mibibyte(self):
        entries = tuple(
            _entry("p" * 3_980 + f"{index:04}.txt", b"")
            for index in range(300)
        )
        snapshot = WorkspaceSeedSnapshot(entries)
        manifest = canonical_checkpoint_manifest(snapshot, self.session_id)
        self.assertGreater(len(manifest), 1 * 1024 * 1024)
        self.assertLess(len(manifest), 8 * 1024 * 1024)
        validate_checkpoint_manifest(manifest, snapshot, self.session_id)

    def test_checkpoint_root_rejects_escape_and_noncanonical_coordinates(self):
        snapshot = WorkspaceSeedSnapshot(())
        expected = (
            f"/mnt/oss/.agnet-study/checkpoints/v1/{self.session_id}/{snapshot.digest}"
        )
        self.assertEqual(
            checkpoint_root_for(self.persistence_root, self.session_id, snapshot.digest),
            expected,
        )
        for root, session, digest in (
            ("/mnt/oss/../escape", self.session_id, snapshot.digest),
            ("/mnt/oss\\escape", self.session_id, snapshot.digest),
            ("/", self.session_id, snapshot.digest),
            ("/workspace/oss", self.session_id, snapshot.digest),
            (self.persistence_root, "A" * 32, snapshot.digest),
            (self.persistence_root, self.session_id, "0" * 63),
        ):
            with self.subTest(root=root), self.assertRaises(WorkspaceCheckpointError):
                checkpoint_root_for(root, session, digest)

    def test_new_checkpoint_publishes_manifest_last_and_restores_across_transports(self):
        snapshot = WorkspaceSeedSnapshot(
            (_entry("a.txt", b"alpha"), _entry("pkg/b.bin", b"\x00beta"))
        )
        shared_files: dict[str, bytes] = {}
        shared_dirs: set[str] = set()
        first = _MemoryTransport(
            self.workspace_root,
            persistent_files=shared_files,
            persistent_dirs=shared_dirs,
        )
        initialize_workspace_from_checkpoint(first, self._lease(), snapshot)
        self.assertEqual(
            first.active,
            {
                self.workspace_root + "/a.txt": b"alpha",
                self.workspace_root + "/pkg/b.bin": b"\x00beta",
            },
        )
        writes = [call for call in first.calls if call[0] == "persistence-write"]
        self.assertTrue(writes[-1][1].endswith("/" + CHECKPOINT_MANIFEST_NAME))
        self.assertTrue(all(call[2] is False for call in writes))

        second = _MemoryTransport(
            self.workspace_root,
            persistent_files=shared_files,
            persistent_dirs=shared_dirs,
        )
        initialize_workspace_from_checkpoint(second, self._lease(), snapshot)
        self.assertEqual(second.active, first.active)
        self.assertFalse(
            any(call[0] == "persistence-write" for call in second.calls)
        )

    def test_partial_checkpoint_safely_resumes_only_matching_files(self):
        snapshot = WorkspaceSeedSnapshot(
            (_entry("a.txt", b"alpha"), _entry("pkg/b.txt", b"beta"))
        )
        root = checkpoint_root_for(
            self.persistence_root,
            self.session_id,
            snapshot.digest,
        )
        existing = root + "/files/a.txt"
        transport = _MemoryTransport(self.workspace_root)
        transport.persistent_files[existing] = b"alpha"
        transport.persistent_dirs.add(root + "/files")
        initialize_workspace_from_checkpoint(transport, self._lease(), snapshot)
        created = [call[1] for call in transport.calls if call[0] == "persistence-write"]
        self.assertNotIn(existing, created)
        self.assertIn(root + "/files/pkg/b.txt", created)
        self.assertTrue(created[-1].endswith("/manifest.json"))

    def test_tamper_extra_wrong_type_and_truncation_fail_closed(self):
        snapshot = WorkspaceSeedSnapshot((_entry("a.txt", b"alpha"),))
        root = checkpoint_root_for(
            self.persistence_root,
            self.session_id,
            snapshot.digest,
        )
        path = root + "/files/a.txt"

        tampered = _MemoryTransport(self.workspace_root)
        tampered.persistent_dirs.add(root + "/files")
        tampered.persistent_files[path] = b"wrong"

        extra = _MemoryTransport(self.workspace_root)
        extra.persistent_files[root + "/unexpected.txt"] = b"x"

        wrong_type = _MemoryTransport(self.workspace_root)
        wrong_type.injected_entries = (SandboxPersistenceEntry(path, True, 0),)

        truncated = _MemoryTransport(self.workspace_root)
        truncated.force_truncated = True

        for transport in (tampered, extra, wrong_type, truncated):
            with self.subTest(kind=type(transport).__name__), self.assertRaises(
                WorkspaceCheckpointError
            ):
                initialize_workspace_from_checkpoint(transport, self._lease(), snapshot)
            self.assertEqual(transport.active, {})

    def test_third_party_transport_error_is_sanitized(self):
        snapshot = WorkspaceSeedSnapshot((_entry("a.txt", b"alpha"),))

        class ExplodingTransport(_MemoryTransport):
            def persistence_list_files(self, *args, **kwargs):
                raise OSError("SECRET provider credential detail")

        transport = ExplodingTransport(self.workspace_root)
        with self.assertRaises(WorkspaceCheckpointError) as caught:
            initialize_workspace_from_checkpoint(transport, self._lease(), snapshot)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_manifest_tamper_empty_snapshot_and_nonempty_active_workspace(self):
        empty = WorkspaceSeedSnapshot(())
        transport = _MemoryTransport(self.workspace_root)
        initialize_workspace_from_checkpoint(transport, self._lease(), empty)
        self.assertEqual(transport.active, {})
        manifest_path = next(iter(transport.persistent_files))
        transport.persistent_files[manifest_path] += b" "

        second = _MemoryTransport(
            self.workspace_root,
            persistent_files=transport.persistent_files,
            persistent_dirs=transport.persistent_dirs,
        )
        with self.assertRaises(WorkspaceCheckpointError):
            initialize_workspace_from_checkpoint(second, self._lease(), empty)

        occupied = _MemoryTransport(self.workspace_root)
        occupied.active[self.workspace_root + "/old.txt"] = b"old"
        with self.assertRaises(WorkspaceCheckpointError):
            initialize_workspace_from_checkpoint(occupied, self._lease(), empty)
        self.assertEqual(occupied.persistent_files, {})

    def test_deep_seed_path_within_fixed_64_level_boundary_restores(self):
        path = "/".join([f"d{index}" for index in range(40)] + ["file.txt"])
        snapshot = WorkspaceSeedSnapshot((_entry(path, b"deep"),))
        transport = _MemoryTransport(self.workspace_root)
        initialize_workspace_from_checkpoint(transport, self._lease(), snapshot)
        self.assertEqual(transport.active[self.workspace_root + "/" + path], b"deep")

    def test_initializer_preflights_layout_before_backend_creation(self):
        too_deep = "/".join([f"d{index}" for index in range(64)] + ["file.txt"])
        too_many_directories = tuple(
            _entry(
                "/".join(
                    [f"tree{index:04}_{depth:02}" for depth in range(20)]
                    + ["file.txt"]
                ),
                b"",
            )
            for index in range(1_000)
        )
        cases = (
            WorkspaceSeedSnapshot((_entry(too_deep, b"x"),)),
            WorkspaceSeedSnapshot(too_many_directories),
        )
        for snapshot in cases:
            with self.subTest(files=len(snapshot.entries)), self.assertRaises(
                WorkspaceCheckpointError
            ):
                create_workspace_checkpoint_initializer(snapshot)


if __name__ == "__main__":
    unittest.main()
