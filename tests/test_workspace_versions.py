"""Stage 5C3/5C5 durable remote mutation tests (offline only)."""

from __future__ import annotations

import hashlib
import json
import posixpath
import unittest

from app.execution_backend import ExecutionLease, WorkspaceDescription
from app.sandbox_transport import (
    SandboxFormatJsonFile,
    SandboxFormatJsonResult,
    SandboxFileEntry,
    SandboxListResult,
    SandboxPersistenceEntry,
    SandboxPersistenceListResult,
    SandboxReadResult,
    SandboxWriteResult,
)
from app.schemas import BashShellArguments, WriteFileArguments
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from app.workspace_versions import (
    WorkspaceCommandManifest,
    WorkspaceVersionChange,
    WorkspaceVersionCoordinator,
    WorkspaceVersionError,
    WorkspaceVersionFencingRequired,
    WorkspaceVersionFile,
    WorkspaceVersionManifest,
    parse_manifest,
)


def _seed_entry(path: str, body: bytes) -> WorkspaceSeedEntry:
    return WorkspaceSeedEntry(path, len(body), hashlib.sha256(body).hexdigest(), body)


class _MemoryTransport:
    def __init__(self, workspace_root: str, persistence: dict[str, bytes] | None = None):
        self.workspace_root = workspace_root
        self.active: dict[str, bytes] = {}
        self.persistence = persistence if persistence is not None else {}
        self.persistence_directories: set[str] = set()
        for existing_path in self.persistence:
            parent = posixpath.dirname(existing_path)
            while parent.startswith("/mnt/oss/"):
                self.persistence_directories.add(parent)
                parent = posixpath.dirname(parent)
        self.calls: list[tuple[str, str]] = []
        self.fail_manifest = False
        self.uncertain_manifest = False
        self.raise_after_active_write = False
        self.fail_next_active_read = False
        self.fail_readback_after_write = False
        self.fail_object_cleanup_for: set[str] = set()
        self.hide_published_manifest = False
        self.tamper_published_manifest = False
        self.fail_final_active_read_after_manifest = False
        self.fail_rollback_after_manifest = False
        self.rollback_failure_armed = False
        self.fail_active_write_for: set[str] = set()
        self.fail_version_object_write_for: set[str] = set()
        self.fail_gc_delete_for: set[str] = set()
        self.retain_after_gc_delete_for: set[str] = set()

    def list_files(self, pattern, *, workspace_root, max_results, timeout_seconds, path_predicate):
        del pattern, timeout_seconds
        if workspace_root != self.workspace_root:
            raise ValueError
        entries = [
            SandboxFileEntry(path, len(body))
            for path, body in sorted(self.active.items())
            if path_predicate(path[len(workspace_root) + 1:], False)
        ]
        return SandboxListResult(tuple(entries[:max_results]), len(entries) > max_results)

    def read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del timeout_seconds
        if self.fail_next_active_read:
            self.fail_next_active_read = False
            raise ValueError("active read unknown")
        body = self.active[path]
        return SandboxReadResult(body[offset:offset + max_bytes], len(body))

    def write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        if self.rollback_failure_armed:
            self.rollback_failure_armed = False
            raise ValueError("active rollback unknown")
        if (path in self.active) != overwrite:
            raise FileExistsError
        if path in self.fail_active_write_for:
            raise ValueError("synthetic active write failure")
        self.calls.append(("active", path))
        self.active[path] = bytes(data)
        if self.fail_readback_after_write:
            self.fail_next_active_read = True
        if self.raise_after_active_write:
            raise ValueError("active write unknown")
        return SandboxWriteResult(len(data))

    def format_json_candidates(self, paths, *, timeout_seconds):
        del timeout_seconds
        files = []
        for path in sorted(paths):
            before = self.active[path]
            value = json.loads(before.decode("utf-8-sig"))
            after = (
                json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n"
            ).encode("utf-8")
            files.append(SandboxFormatJsonFile(
                path,
                hashlib.sha256(before).hexdigest(),
                after,
            ))
        return SandboxFormatJsonResult(tuple(files))

    def rollback_delete_file(self, path, *, expected_sha256, timeout_seconds):
        del timeout_seconds
        body = self.active[path]
        if hashlib.sha256(body).hexdigest() != expected_sha256:
            raise ValueError
        self.calls.append(("rollback", path))
        del self.active[path]

    def rollback_delete_workspace_version_object(
        self,
        path,
        *,
        expected_size,
        expected_sha256,
        timeout_seconds,
    ):
        del timeout_seconds
        if path in self.fail_object_cleanup_for:
            raise ValueError("synthetic object cleanup failure")
        body = self.persistence[path]
        if (
            "/objects/" not in path
            or posixpath.basename(path) != expected_sha256
            or len(body) != expected_size
            or hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            raise ValueError
        self.calls.append(("object_cleanup", path))
        del self.persistence[path]
        if path in self.persistence:
            raise ValueError

    def gc_delete_workspace_version_object(
        self,
        path,
        *,
        expected_size,
        expected_sha256,
        timeout_seconds,
    ):
        del timeout_seconds
        if path in self.fail_gc_delete_for:
            raise ValueError("provider secret GC failure")
        body = self.persistence[path]
        if (
            "/objects/" not in path
            or posixpath.basename(path) != expected_sha256
            or len(body) != expected_size
            or hashlib.sha256(body).hexdigest() != expected_sha256
        ):
            raise ValueError("provider secret exact mismatch")
        self.calls.append(("gc_delete", path))
        if path not in self.retain_after_gc_delete_for:
            del self.persistence[path]
        if path in self.persistence:
            raise ValueError("provider secret retained object")

    def persistence_write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        if overwrite or path in self.persistence:
            raise FileExistsError
        self.persistence_directories.add(posixpath.dirname(path))
        self.persistence[path] = bytes(data)
        return SandboxWriteResult(len(data))

    def persistence_read_file(self, path, *, offset, max_bytes, timeout_seconds):
        del timeout_seconds
        body = self.persistence[path]
        return SandboxReadResult(body[offset:offset + max_bytes], len(body))

    def _list(self, root: str, max_results: int):
        directories: set[str] = {
            path
            for path in self.persistence_directories
            if path.startswith(root + "/")
        }
        for path in self.persistence:
            parent = posixpath.dirname(path)
            while parent.startswith(root + "/"):
                directories.add(parent)
                parent = posixpath.dirname(parent)
        entries = [SandboxPersistenceEntry(path, True, 0) for path in directories]
        entries.extend(
            SandboxPersistenceEntry(path, False, len(body))
            for path, body in self.persistence.items()
            if path.startswith(root + "/")
        )
        entries.sort(key=lambda item: item.path)
        return SandboxPersistenceListResult(tuple(entries[:max_results]), len(entries) > max_results)

    def persistence_list_files(self, checkpoint_root, *, max_results, max_depth, timeout_seconds):
        del max_depth, timeout_seconds
        return self._list(checkpoint_root, max_results)

    def workspace_version_list_files(self, session_root, *, max_results, max_depth, timeout_seconds):
        del max_depth, timeout_seconds
        return self._list(session_root, max_results)

    def workspace_version_probe_file(self, path, *, max_bytes, timeout_seconds):
        del timeout_seconds
        if self.uncertain_manifest and "/generations/" in path:
            raise ValueError("unknown")
        if self.hide_published_manifest and "/generations/" in path:
            return None
        body = self.persistence.get(path)
        if body is None:
            return None
        if len(body) > max_bytes:
            raise ValueError
        return SandboxReadResult(body, len(body))

    def workspace_version_read_file(self, path, *, offset, max_bytes, timeout_seconds):
        observed = self.workspace_version_probe_file(
            path, max_bytes=max_bytes, timeout_seconds=timeout_seconds
        )
        if observed is None:
            raise FileNotFoundError
        return SandboxReadResult(observed.data[offset:offset + max_bytes], observed.total_bytes)

    def workspace_version_write_file(self, path, data, *, overwrite, timeout_seconds):
        del timeout_seconds
        if overwrite or path in self.persistence:
            raise FileExistsError
        if path in self.fail_version_object_write_for:
            raise ValueError("synthetic object write failure")
        if self.fail_manifest and "/generations/" in path:
            self.rollback_failure_armed = self.fail_rollback_after_manifest
            raise ValueError("publish failed")
        self.calls.append(("manifest" if "/generations/" in path else "object", path))
        self.persistence_directories.add(posixpath.dirname(path))
        self.persistence[path] = (
            b"tampered" if self.tamper_published_manifest and "/generations/" in path
            else bytes(data)
        )
        if self.fail_final_active_read_after_manifest and "/generations/" in path:
            self.fail_next_active_read = True
        return SandboxWriteResult(len(data))


class WorkspaceVersionTests(unittest.TestCase):
    session_id = "a" * 32
    workspace_root = "/workspace/agnet-study/" + session_id
    persistence_root = "/mnt/oss"

    def setUp(self):
        self.snapshot = WorkspaceSeedSnapshot((_seed_entry("a.py", b"old\n"),))
        self.lease = ExecutionLease(
            WorkspaceDescription(
                "aliyun-sandbox", self.session_id, self.workspace_root,
                self.persistence_root, True,
            ),
            sandbox_id="sandbox-test",
        )

    def _prepared(self, coordinator, transport, content="new\n", operation="b" * 64):
        return coordinator.prepare(
            transport,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(path="a.py", content=content, mode="overwrite"),
            operation_id=operation,
            arguments_digest="c" * 64,
            permission_mode="partial-control",
            runtime_generation=0,
        )

    def _command_fixture(self):
        a_before = b'{"z":1,"a":2}\n'
        b_before = b'{"nested":{"b":2,"a":1}}\n'
        snapshot = WorkspaceSeedSnapshot((
            _seed_entry("a.json", a_before),
            _seed_entry("b.json", b_before),
        ))
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(snapshot)
        coordinator(transport, self.lease)
        arguments = BashShellArguments(
            command="format_json",
            paths=["b.json", "a.json"],
            timeout_seconds=7,
        )
        prepared = coordinator.prepare_command(
            transport,
            self.lease,
            tool_name="bash_shell",
            arguments=arguments,
            operation_id="d" * 64,
            arguments_digest="e" * 64,
            permission_mode="full-access",
            runtime_generation=3,
        )
        return (
            snapshot,
            transport,
            coordinator,
            arguments,
            prepared,
            a_before,
            b_before,
        )

    def _persist_command_manifest(
        self,
        persistence: dict[str, bytes],
        manifest: WorkspaceCommandManifest,
        bodies: dict[str, bytes],
    ) -> None:
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        for entry in manifest.files:
            body = bodies[entry.path]
            self.assertEqual(len(body), entry.size)
            self.assertEqual(hashlib.sha256(body).hexdigest(), entry.sha256)
            persistence[root + "/objects/" + entry.sha256] = body
        persistence[
            root + f"/generations/{manifest.generation:016d}-{manifest.digest}.json"
        ] = manifest.canonical_bytes()

    def test_command_candidate_is_isolated_and_commits_one_v3_generation(self):
        (
            snapshot,
            transport,
            coordinator,
            _arguments,
            prepared,
            a_before,
            b_before,
        ) = self._command_fixture()
        a_path = self.workspace_root + "/a.json"
        b_path = self.workspace_root + "/b.json"
        self.assertEqual(transport.active[a_path], a_before)
        self.assertEqual(transport.active[b_path], b_before)
        self.assertIn("a/a.json", prepared.diff_preview)
        self.assertIn("a/b.json", prepared.diff_preview)

        output = coordinator.commit_command(
            transport,
            self.lease,
            prepared.candidate,
            runtime_generation=3,
        )
        self.assertEqual(output.command, "format_json")
        self.assertEqual(output.paths, ["b.json", "a.json"])
        self.assertEqual(output.stdout, "已规范格式化 2 个 JSON 文件。")
        manifests = [
            body
            for path, body in transport.persistence.items()
            if "/generations/" in path
        ]
        self.assertEqual(len(manifests), 1)
        manifest = parse_manifest(manifests[0])
        self.assertIsInstance(manifest, WorkspaceCommandManifest)
        self.assertEqual(manifest.generation, 1)
        self.assertEqual(
            [change.path for change in manifest.changes],
            ["a.json", "b.json"],
        )

        restored_transport = _MemoryTransport(
            self.workspace_root, transport.persistence
        )
        WorkspaceVersionCoordinator(snapshot)(restored_transport, self.lease)
        self.assertEqual(restored_transport.active, transport.active)

    def test_v3_manifest_rejects_duplicate_keys_and_noncanonical_bytes(self):
        (
            _snapshot,
            transport,
            coordinator,
            _arguments,
            prepared,
            _a_before,
            _b_before,
        ) = self._command_fixture()
        coordinator.commit_command(
            transport,
            self.lease,
            prepared.candidate,
            runtime_generation=3,
        )
        raw = next(
            body
            for path, body in transport.persistence.items()
            if "/generations/" in path
        )
        manifest = parse_manifest(raw)
        self.assertIsInstance(manifest, WorkspaceCommandManifest)

        top_level_duplicate = raw.replace(
            b'"schema_version":3',
            b'"schema_version":3,"schema_version":3',
            1,
        )
        nested_duplicate = raw.replace(
            b'"path":"a.json"',
            b'"path":"a.json","path":"a.json"',
            1,
        )
        noncanonical = raw + b" "
        for forged in (top_level_duplicate, nested_duplicate, noncanonical):
            with self.subTest(forged=forged):
                with self.assertRaises(WorkspaceVersionError):
                    parse_manifest(forged)

    def test_restore_rejects_v3_undeclared_file_transition(self):
        a_before = b'{"a":0}\n'
        b_before = b'{"b":0}\n'
        a_after = b'{\n  "a": 1\n}\n'
        b_after = b'{\n  "b": 1\n}\n'
        snapshot = WorkspaceSeedSnapshot((
            _seed_entry("a.json", a_before),
            _seed_entry("b.json", b_before),
        ))
        files = tuple(
            WorkspaceVersionFile(path, len(body), hashlib.sha256(body).hexdigest())
            for path, body in (("a.json", a_after), ("b.json", b_after))
        )
        forged = WorkspaceCommandManifest(
            session_id=self.session_id,
            baseline_digest=snapshot.digest,
            generation=1,
            parent_manifest_sha256=None,
            operation_id="1" * 64,
            tool_name="bash_shell",
            operation="format_json",
            arguments_digest="2" * 64,
            permission_mode="full-access",
            changes=(WorkspaceVersionChange(
                "a.json",
                hashlib.sha256(a_before).hexdigest(),
                hashlib.sha256(a_after).hexdigest(),
            ),),
            files=files,
        )
        canonical = forged.canonical_bytes()
        self.assertEqual(parse_manifest(canonical), forged)
        persistence: dict[str, bytes] = {}
        self._persist_command_manifest(
            persistence,
            forged,
            {"a.json": a_after, "b.json": b_after},
        )

        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "generation 修改了目标以外的文件状态。",
        ):
            WorkspaceVersionCoordinator(snapshot)(
                _MemoryTransport(self.workspace_root, persistence),
                self.lease,
            )

    def test_restore_rejects_v3_before_digest_not_bound_to_prior_generation(self):
        a_before = b'{"a":0}\n'
        b_before = b'{"b":0}\n'
        a_after = b'{\n  "a": 1\n}\n'
        snapshot = WorkspaceSeedSnapshot((
            _seed_entry("a.json", a_before),
            _seed_entry("b.json", b_before),
        ))
        files = tuple(
            WorkspaceVersionFile(path, len(body), hashlib.sha256(body).hexdigest())
            for path, body in (("a.json", a_after), ("b.json", b_before))
        )
        forged = WorkspaceCommandManifest(
            session_id=self.session_id,
            baseline_digest=snapshot.digest,
            generation=1,
            parent_manifest_sha256=None,
            operation_id="3" * 64,
            tool_name="bash_shell",
            operation="format_json",
            arguments_digest="4" * 64,
            permission_mode="full-access",
            changes=(WorkspaceVersionChange(
                "a.json",
                hashlib.sha256(b"forged prior body\n").hexdigest(),
                hashlib.sha256(a_after).hexdigest(),
            ),),
            files=files,
        )
        canonical = forged.canonical_bytes()
        self.assertEqual(parse_manifest(canonical), forged)
        persistence: dict[str, bytes] = {}
        self._persist_command_manifest(
            persistence,
            forged,
            {"a.json": a_after, "b.json": b_before},
        )

        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "generation command 变更未绑定上一代。",
        ):
            WorkspaceVersionCoordinator(snapshot)(
                _MemoryTransport(self.workspace_root, persistence),
                self.lease,
            )

    def test_command_candidate_rejects_noncanonical_or_invalid_json_bytes(self):
        for after in (b'{"a":2,"z":1}\n', b"not-json\n", b'{"a":NaN}\n'):
            with self.subTest(after=after):
                (
                    _snapshot,
                    transport,
                    coordinator,
                    arguments,
                    _prepared,
                    a_before,
                    b_before,
                ) = self._command_fixture()
                a_path = self.workspace_root + "/a.json"
                b_path = self.workspace_root + "/b.json"

                def invalid_candidates(paths, *, timeout_seconds):
                    del timeout_seconds
                    files = []
                    for path in sorted(paths):
                        before = transport.active[path]
                        candidate = after if path == a_path else (
                            json.dumps(
                                json.loads(before.decode("utf-8")),
                                ensure_ascii=False,
                                sort_keys=True,
                                indent=2,
                            )
                            + "\n"
                        ).encode("utf-8")
                        files.append(SandboxFormatJsonFile(
                            path,
                            hashlib.sha256(before).hexdigest(),
                            candidate,
                        ))
                    return SandboxFormatJsonResult(tuple(files))

                transport.format_json_candidates = invalid_candidates  # type: ignore[method-assign]
                with self.assertRaises(WorkspaceVersionError):
                    coordinator.prepare_command(
                        transport,
                        self.lease,
                        tool_name="bash_shell",
                        arguments=arguments,
                        operation_id="1" * 64,
                        arguments_digest="2" * 64,
                        permission_mode="full-access",
                        runtime_generation=3,
                    )
                self.assertEqual(transport.active[a_path], a_before)
                self.assertEqual(transport.active[b_path], b_before)

    def test_command_active_failure_rolls_back_all_applied_in_reverse(self):
        (
            _snapshot,
            transport,
            coordinator,
            _arguments,
            prepared,
            a_before,
            b_before,
        ) = self._command_fixture()
        a_path = self.workspace_root + "/a.json"
        b_path = self.workspace_root + "/b.json"
        transport.calls.clear()
        transport.fail_active_write_for.add(b_path)

        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "command active 写入失败，已完整回滚。",
        ):
            coordinator.commit_command(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=3,
            )

        self.assertEqual(transport.active[a_path], a_before)
        self.assertEqual(transport.active[b_path], b_before)
        active_calls = [path for kind, path in transport.calls if kind == "active"]
        self.assertEqual(active_calls, [a_path, a_path])
        self.assertFalse(any("/objects/" in path for path in transport.persistence))
        self.assertFalse(any("/generations/" in path for path in transport.persistence))

    def test_command_object_preparation_failure_cleans_only_proven_created(self):
        (
            _snapshot,
            transport,
            coordinator,
            _arguments,
            prepared,
            a_before,
            b_before,
        ) = self._command_fixture()
        sorted_changes = sorted(prepared.candidate.changes, key=lambda item: item.path)
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}/objects/"
        second_path = root + sorted_changes[1].after_sha256
        transport.fail_version_object_write_for.add(second_path)
        transport.calls.clear()

        with self.assertRaisesRegex(ValueError, "synthetic object write failure"):
            coordinator.commit_command(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=3,
            )

        self.assertEqual(
            transport.active[self.workspace_root + "/a.json"], a_before
        )
        self.assertEqual(
            transport.active[self.workspace_root + "/b.json"], b_before
        )
        self.assertFalse(any("/objects/" in path for path in transport.persistence))
        self.assertFalse(any(kind == "active" for kind, _path in transport.calls))
        self.assertEqual(
            len([kind for kind, _path in transport.calls if kind == "object_cleanup"]),
            1,
        )

    def test_command_manifest_failure_distinguishes_definite_and_uncertain(self):
        for uncertainty in (False, True):
            with self.subTest(uncertainty=uncertainty):
                (
                    _snapshot,
                    transport,
                    coordinator,
                    _arguments,
                    prepared,
                    a_before,
                    b_before,
                ) = self._command_fixture()
                transport.fail_manifest = True
                transport.uncertain_manifest = uncertainty
                transport.calls.clear()
                expected = (
                    WorkspaceVersionFencingRequired
                    if uncertainty
                    else WorkspaceVersionError
                )
                with self.assertRaises(expected):
                    coordinator.commit_command(
                        transport,
                        self.lease,
                        prepared.candidate,
                        runtime_generation=3,
                    )
                if uncertainty:
                    self.assertNotEqual(
                        transport.active[self.workspace_root + "/a.json"],
                        a_before,
                    )
                    self.assertTrue(any(
                        "/objects/" in path for path in transport.persistence
                    ))
                    self.assertEqual(
                        [path for kind, path in transport.calls if kind == "active"],
                        [
                            self.workspace_root + "/a.json",
                            self.workspace_root + "/b.json",
                        ],
                    )
                else:
                    self.assertEqual(
                        transport.active[self.workspace_root + "/a.json"],
                        a_before,
                    )
                    self.assertEqual(
                        transport.active[self.workspace_root + "/b.json"],
                        b_before,
                    )
                    self.assertFalse(any(
                        "/objects/" in path for path in transport.persistence
                    ))
                    self.assertEqual(
                        [path for kind, path in transport.calls if kind == "active"],
                        [
                            self.workspace_root + "/a.json",
                            self.workspace_root + "/b.json",
                            self.workspace_root + "/b.json",
                            self.workspace_root + "/a.json",
                        ],
                    )

    def test_command_post_manifest_verification_failure_requires_fence(self):
        (
            _snapshot,
            transport,
            coordinator,
            _arguments,
            prepared,
            _a_before,
            _b_before,
        ) = self._command_fixture()
        transport.fail_final_active_read_after_manifest = True

        with self.assertRaisesRegex(
            WorkspaceVersionFencingRequired,
            "durable command commit 后 active 状态无法确认。",
        ):
            coordinator.commit_command(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=3,
            )

        self.assertTrue(any(
            "/generations/" in path for path in transport.persistence
        ))
        self.assertTrue(any("/objects/" in path for path in transport.persistence))
        self.assertFalse(any(
            kind == "object_cleanup" for kind, _path in transport.calls
        ))

    def test_version_entries_reject_sealed_secret_paths(self):
        for path in (".envrc", "private/server.key", "credentials", "token"):
            with self.subTest(path=path):
                with self.assertRaises(WorkspaceVersionError):
                    WorkspaceVersionFile(
                        path,
                        1,
                        hashlib.sha256(b"x").hexdigest(),
                    )

    def test_manifest_is_canonical_and_rejects_tamper(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        coordinator.commit(transport, self.lease, prepared.candidate, runtime_generation=0)
        raw = next(body for path, body in transport.persistence.items() if "/generations/" in path)
        manifest = parse_manifest(raw)
        self.assertIsInstance(manifest, WorkspaceVersionManifest)
        with self.assertRaises(WorkspaceVersionError):
            parse_manifest(raw + b" ")

    def test_object_then_active_then_manifest_and_cross_sandbox_restore(self):
        persistence: dict[str, bytes] = {}
        first = _MemoryTransport(self.workspace_root, persistence)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(first, self.lease)
        prepared = self._prepared(coordinator, first)
        first.calls.clear()
        output = coordinator.commit(first, self.lease, prepared.candidate, runtime_generation=0)
        self.assertEqual(output.sha256, hashlib.sha256(b"new\n").hexdigest())
        order = [kind for kind, _path in first.calls if kind in {"object", "active", "manifest"}]
        self.assertLess(order.index("object"), order.index("active"))
        self.assertLess(order.index("active"), order.index("manifest"))

        second = _MemoryTransport(self.workspace_root, persistence)
        restored = WorkspaceVersionCoordinator(self.snapshot)
        restored(second, self.lease)
        self.assertEqual(second.active[self.workspace_root + "/a.py"], b"new\n")

    def test_first_generation_persists_every_baseline_object(self):
        snapshot = WorkspaceSeedSnapshot(
            (_seed_entry("a.py", b"a0\n"), _seed_entry("b.py", b"b0\n"))
        )
        persistence: dict[str, bytes] = {}
        first = _MemoryTransport(self.workspace_root, persistence)
        coordinator = WorkspaceVersionCoordinator(snapshot)
        coordinator(first, self.lease)
        prepared = coordinator.prepare(
            first,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(path="a.py", content="a1\n", mode="overwrite"),
            operation_id="d" * 64,
            arguments_digest="e" * 64,
            permission_mode="partial-control",
            runtime_generation=0,
        )
        coordinator.commit(first, self.lease, prepared.candidate, runtime_generation=0)
        object_paths = [path for path in persistence if "/objects/" in path]
        self.assertEqual(len(object_paths), 2)
        second = _MemoryTransport(self.workspace_root, persistence)
        WorkspaceVersionCoordinator(snapshot)(second, self.lease)
        self.assertEqual(second.active[self.workspace_root + "/a.py"], b"a1\n")
        self.assertEqual(second.active[self.workspace_root + "/b.py"], b"b0\n")

    def test_restore_rejects_manifest_that_changes_unrelated_file(self):
        snapshot = WorkspaceSeedSnapshot(
            (_seed_entry("a.py", b"a0\n"), _seed_entry("b.py", b"b0\n"))
        )
        persistence: dict[str, bytes] = {}
        seed_transport = _MemoryTransport(self.workspace_root, persistence)
        WorkspaceVersionCoordinator(snapshot)(seed_transport, self.lease)
        a1, evil = b"a1\n", b"evil\n"
        files = (
            WorkspaceVersionFile("a.py", len(a1), hashlib.sha256(a1).hexdigest()),
            WorkspaceVersionFile("b.py", len(evil), hashlib.sha256(evil).hexdigest()),
        )
        forged = WorkspaceVersionManifest(
            session_id=self.session_id,
            baseline_digest=snapshot.digest,
            generation=1,
            parent_manifest_sha256=None,
            operation_id="1" * 64,
            tool_name="write_file",
            operation="write_overwrite",
            arguments_digest="2" * 64,
            permission_mode="partial-control",
            path="a.py",
            before_sha256=hashlib.sha256(b"a0\n").hexdigest(),
            after_sha256=hashlib.sha256(a1).hexdigest(),
            files=files,
        )
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        persistence[root + "/objects/" + files[0].sha256] = a1
        persistence[root + "/objects/" + files[1].sha256] = evil
        persistence[
            root + f"/generations/{forged.generation:016d}-{forged.digest}.json"
        ] = forged.canonical_bytes()
        second = _MemoryTransport(self.workspace_root, persistence)
        with self.assertRaises(WorkspaceVersionError):
            WorkspaceVersionCoordinator(snapshot)(second, self.lease)

    def test_mutation_larger_than_durable_file_limit_is_rejected_before_write(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        with self.assertRaises(WorkspaceVersionError):
            coordinator.prepare(
                transport,
                self.lease,
                tool_name="write_file",
                arguments=WriteFileArguments(
                    path="a.py", content="x" * (1024 * 1024 + 1), mode="overwrite"
                ),
                operation_id="3" * 64,
                arguments_digest="4" * 64,
                permission_mode="partial-control",
                runtime_generation=0,
            )

    def test_confirmed_manifest_failure_rolls_back_active(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        transport.fail_manifest = True
        with self.assertRaises(WorkspaceVersionError):
            coordinator.commit(transport, self.lease, prepared.candidate, runtime_generation=0)
        self.assertEqual(transport.active[self.workspace_root + "/a.py"], b"old\n")
        self.assertFalse(any("/objects/" in path for path in transport.persistence))
        self.assertEqual(
            len([kind for kind, _path in transport.calls if kind == "object_cleanup"]),
            1,
        )

    def test_unknown_manifest_publish_requires_fence(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        transport.fail_manifest = True
        transport.uncertain_manifest = True
        with self.assertRaises(WorkspaceVersionFencingRequired):
            coordinator.commit(transport, self.lease, prepared.candidate, runtime_generation=0)
        self.assertTrue(any("/objects/" in path for path in transport.persistence))
        self.assertFalse(any(kind == "object_cleanup" for kind, _path in transport.calls))

    def test_confirmed_failure_preserves_preexisting_content_object(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}/objects/"
        existing_path = root + prepared.candidate.after_sha256
        transport.persistence[existing_path] = prepared.candidate.after
        transport.fail_manifest = True

        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "generation manifest 未发布，active 已回滚。",
        ):
            coordinator.commit(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=0,
            )

        self.assertEqual(transport.persistence[existing_path], prepared.candidate.after)
        self.assertFalse(any(kind == "object_cleanup" for kind, _path in transport.calls))

    def test_first_generation_failure_cleans_new_baseline_objects_and_deduplicates_digest(self):
        shared = b"shared\n"
        snapshot = WorkspaceSeedSnapshot(
            (_seed_entry("a.py", b"before\n"), _seed_entry("b.py", shared), _seed_entry("c.py", shared))
        )
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(snapshot)
        coordinator(transport, self.lease)
        prepared = coordinator.prepare(
            transport,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(path="a.py", content="after\n", mode="overwrite"),
            operation_id="d" * 64,
            arguments_digest="e" * 64,
            permission_mode="partial-control",
            runtime_generation=0,
        )
        transport.fail_manifest = True

        with self.assertRaises(WorkspaceVersionError):
            coordinator.commit(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=0,
            )

        object_writes = [path for kind, path in transport.calls if kind == "object"]
        object_cleanups = [path for kind, path in transport.calls if kind == "object_cleanup"]
        self.assertEqual(len(object_writes), 2)
        self.assertCountEqual(object_cleanups, object_writes)
        self.assertFalse(any("/objects/" in path for path in transport.persistence))

    def test_write_success_probe_absent_is_fenced_without_object_cleanup(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        transport.hide_published_manifest = True

        with self.assertRaisesRegex(
            WorkspaceVersionFencingRequired,
            "generation manifest 写入后确认不存在。",
        ):
            coordinator.commit(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=0,
            )

        self.assertTrue(any("/objects/" in path for path in transport.persistence))
        self.assertTrue(any("/generations/" in path for path in transport.persistence))
        self.assertFalse(any(kind == "object_cleanup" for kind, _path in transport.calls))

    def test_rollback_uncertainty_never_cleans_created_objects(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        transport.fail_manifest = True
        transport.fail_rollback_after_manifest = True

        with self.assertRaises(WorkspaceVersionFencingRequired):
            coordinator.commit(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=0,
            )

        self.assertTrue(any("/objects/" in path for path in transport.persistence))
        self.assertFalse(any(kind == "object_cleanup" for kind, _path in transport.calls))

    def test_partial_object_cleanup_failure_keeps_primary_failure_and_other_cleanup(self):
        snapshot = WorkspaceSeedSnapshot(
            (_seed_entry("a.py", b"a0\n"), _seed_entry("b.py", b"b0\n"))
        )
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(snapshot)
        coordinator(transport, self.lease)
        prepared = coordinator.prepare(
            transport,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(path="a.py", content="a1\n", mode="overwrite"),
            operation_id="5" * 64,
            arguments_digest="6" * 64,
            permission_mode="partial-control",
            runtime_generation=0,
        )
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}/objects/"
        retained = root + hashlib.sha256(b"a1\n").hexdigest()
        removed = root + hashlib.sha256(b"b0\n").hexdigest()
        transport.fail_object_cleanup_for.add(retained)
        transport.fail_manifest = True

        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "generation manifest 未发布，active 已回滚。",
        ):
            coordinator.commit(
                transport,
                self.lease,
                prepared.candidate,
                runtime_generation=0,
            )

        self.assertIn(retained, transport.persistence)
        self.assertNotIn(removed, transport.persistence)
        self.assertIn(("object_cleanup", removed), transport.calls)

    def test_failed_second_generation_preserves_all_first_generation_objects(self):
        snapshot = WorkspaceSeedSnapshot(
            (_seed_entry("a.py", b"a0\n"), _seed_entry("b.py", b"b0\n"))
        )
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(snapshot)
        coordinator(transport, self.lease)
        first = coordinator.prepare(
            transport,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(path="a.py", content="a1\n", mode="overwrite"),
            operation_id="7" * 64,
            arguments_digest="8" * 64,
            permission_mode="partial-control",
            runtime_generation=0,
        )
        coordinator.commit(transport, self.lease, first.candidate, runtime_generation=0)
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}/objects/"
        first_generation_objects = {
            path: body
            for path, body in transport.persistence.items()
            if path.startswith(root)
        }
        second = coordinator.prepare(
            transport,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(path="a.py", content="a2\n", mode="overwrite"),
            operation_id="9" * 64,
            arguments_digest="a" * 64,
            permission_mode="partial-control",
            runtime_generation=0,
        )
        second_object = root + second.candidate.after_sha256
        transport.fail_manifest = True

        with self.assertRaises(WorkspaceVersionError):
            coordinator.commit(
                transport,
                self.lease,
                second.candidate,
                runtime_generation=0,
            )

        self.assertNotIn(second_object, transport.persistence)
        for path, body in first_generation_objects.items():
            self.assertEqual(transport.persistence[path], body)

    def test_manifest_mismatch_and_durable_commit_uncertainty_never_clean_objects(self):
        for failure in ("manifest_mismatch", "final_active_read"):
            with self.subTest(failure=failure):
                transport = _MemoryTransport(self.workspace_root)
                coordinator = WorkspaceVersionCoordinator(self.snapshot)
                coordinator(transport, self.lease)
                prepared = self._prepared(coordinator, transport)
                if failure == "manifest_mismatch":
                    transport.tamper_published_manifest = True
                else:
                    transport.fail_final_active_read_after_manifest = True

                with self.assertRaises(WorkspaceVersionFencingRequired):
                    coordinator.commit(
                        transport,
                        self.lease,
                        prepared.candidate,
                        runtime_generation=0,
                    )

                self.assertTrue(
                    any("/objects/" in path for path in transport.persistence)
                )
                self.assertTrue(
                    any("/generations/" in path for path in transport.persistence)
                )
                self.assertFalse(
                    any(kind == "object_cleanup" for kind, _path in transport.calls)
                )

    def test_active_write_and_readback_uncertainty_require_fence(self):
        for failure in ("write", "read"):
            with self.subTest(failure=failure):
                transport = _MemoryTransport(self.workspace_root)
                coordinator = WorkspaceVersionCoordinator(self.snapshot)
                coordinator(transport, self.lease)
                prepared = self._prepared(coordinator, transport)
                if failure == "write":
                    transport.raise_after_active_write = True
                else:
                    transport.fail_readback_after_write = True
                with self.assertRaises(WorkspaceVersionFencingRequired):
                    coordinator.commit(
                        transport,
                        self.lease,
                        prepared.candidate,
                        runtime_generation=0,
                    )

    def test_generation_candidate_rejects_replay_after_commit(self):
        transport = _MemoryTransport(self.workspace_root)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(transport, self.lease)
        prepared = self._prepared(coordinator, transport)
        coordinator.commit(transport, self.lease, prepared.candidate, runtime_generation=0)
        with self.assertRaises(WorkspaceVersionError):
            coordinator.commit(transport, self.lease, prepared.candidate, runtime_generation=0)

    def test_orphan_object_is_ignored_but_referenced_object_tamper_fails(self):
        persistence: dict[str, bytes] = {}
        first = _MemoryTransport(self.workspace_root, persistence)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(first, self.lease)
        prepared = self._prepared(coordinator, first)
        coordinator.commit(first, self.lease, prepared.candidate, runtime_generation=0)
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}/objects/"
        orphan = b"uncommitted orphan"
        persistence[root + hashlib.sha256(orphan).hexdigest()] = orphan

        WorkspaceVersionCoordinator(self.snapshot)(
            _MemoryTransport(self.workspace_root, persistence), self.lease
        )

        manifest_raw = next(
            body for path, body in persistence.items() if "/generations/" in path
        )
        manifest = parse_manifest(manifest_raw)
        referenced = root + manifest.after_sha256
        persistence[referenced] = b"x" * len(persistence[referenced])
        with self.assertRaises(WorkspaceVersionError):
            WorkspaceVersionCoordinator(self.snapshot)(
                _MemoryTransport(self.workspace_root, persistence), self.lease
            )

    def test_tampered_orphan_is_ignored_by_default_but_gc_fails_without_delete(self):
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        declared = b"unpublished-valid-body"
        object_path = root + "/objects/" + hashlib.sha256(declared).hexdigest()
        tampered = b"x" * len(declared)
        persistence = {object_path: tampered}

        default_transport = _MemoryTransport(self.workspace_root, persistence)
        default = WorkspaceVersionCoordinator(self.snapshot)
        default(default_transport, self.lease)
        self.assertEqual(persistence[object_path], tampered)
        self.assertIsNone(default.last_orphan_gc_report)

        gc_transport = _MemoryTransport(self.workspace_root, persistence)
        collector = WorkspaceVersionCoordinator(
            self.snapshot,
            collect_orphans=True,
        )
        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "orphan GC 扫描验证失败。",
        ):
            collector(gc_transport, self.lease)
        self.assertEqual(persistence[object_path], tampered)
        self.assertFalse(any(
            kind == "gc_delete" for kind, _path in gc_transport.calls
        ))
        self.assertIsNone(collector.last_orphan_gc_report)

    def test_gc_retains_union_of_historical_v2_and_v3_manifest_objects(self):
        snapshot = WorkspaceSeedSnapshot((
            _seed_entry("a.json", b'{"x":0}\n'),
            _seed_entry("b.json", b'{"z":2,"a":0}\n'),
        ))
        persistence: dict[str, bytes] = {}
        producer_transport = _MemoryTransport(self.workspace_root, persistence)
        producer = WorkspaceVersionCoordinator(snapshot)
        producer(producer_transport, self.lease)
        first = producer.prepare(
            producer_transport,
            self.lease,
            tool_name="write_file",
            arguments=WriteFileArguments(
                path="a.json",
                content='{"x":1}\n',
                mode="overwrite",
            ),
            operation_id="5" * 64,
            arguments_digest="6" * 64,
            permission_mode="partial-control",
            runtime_generation=1,
        )
        producer.commit(
            producer_transport,
            self.lease,
            first.candidate,
            runtime_generation=1,
        )
        command_arguments = BashShellArguments(
            command="format_json",
            paths=["a.json", "b.json"],
        )
        second = producer.prepare_command(
            producer_transport,
            self.lease,
            tool_name="bash_shell",
            arguments=command_arguments,
            operation_id="7" * 64,
            arguments_digest="8" * 64,
            permission_mode="full-access",
            runtime_generation=2,
        )
        producer.commit_command(
            producer_transport,
            self.lease,
            second.candidate,
            runtime_generation=2,
        )
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        crash_orphan = b"crash-before-manifest"
        crash_digest = hashlib.sha256(crash_orphan).hexdigest()
        persistence[root + "/objects/" + crash_digest] = crash_orphan
        manifest_paths = sorted(
            path for path in persistence if "/generations/" in path
        )
        referenced = {
            item.sha256
            for path in manifest_paths
            for item in parse_manifest(persistence[path]).files
        }

        gc_transport = _MemoryTransport(self.workspace_root, persistence)
        collector = WorkspaceVersionCoordinator(snapshot, collect_orphans=True)
        collector(gc_transport, self.lease)

        remaining_objects = {
            posixpath.basename(path)
            for path in persistence
            if "/objects/" in path
        }
        self.assertEqual(remaining_objects, referenced)
        self.assertNotIn(root + "/objects/" + crash_digest, persistence)
        self.assertEqual(
            sorted(path for path in persistence if "/generations/" in path),
            manifest_paths,
        )
        report = collector.last_orphan_gc_report
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.generations, 2)
        self.assertEqual(report.retained_object_count, len(referenced))
        self.assertEqual(report.orphan_object_count, 1)
        self.assertEqual(report.deleted_object_count, 1)
        self.assertEqual(report.orphan_bytes, len(crash_orphan))
        self.assertEqual(report.deleted_bytes, len(crash_orphan))
        self.assertEqual(report.baseline_digest, snapshot.digest)

    def test_gc_default_is_disabled_and_no_manifest_collects_all_objects(self):
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        orphan = b"unpublished"
        object_path = root + "/objects/" + hashlib.sha256(orphan).hexdigest()
        persistence = {object_path: orphan}
        default_transport = _MemoryTransport(self.workspace_root, persistence)
        default = WorkspaceVersionCoordinator(self.snapshot)
        default(default_transport, self.lease)
        self.assertIn(object_path, persistence)
        self.assertIsNone(default.last_orphan_gc_report)
        self.assertFalse(any(
            kind == "gc_delete" for kind, _path in default_transport.calls
        ))

        gc_transport = _MemoryTransport(self.workspace_root, persistence)
        collector = WorkspaceVersionCoordinator(
            self.snapshot,
            collect_orphans=True,
        )
        collector(gc_transport, self.lease)
        self.assertNotIn(object_path, persistence)
        report = collector.last_orphan_gc_report
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.generations, 0)
        self.assertEqual(report.retained_object_count, 0)
        self.assertEqual(report.orphan_object_count, 1)

    def test_gc_scan_drift_fails_before_any_delete(self):
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        first_body = b"first orphan"
        first_path = root + "/objects/" + hashlib.sha256(first_body).hexdigest()
        persistence = {first_path: first_body}
        transport = _MemoryTransport(self.workspace_root, persistence)
        stable_list = transport.workspace_version_list_files
        list_calls = 0
        drift_body = b"appeared between scans"
        drift_path = root + "/objects/" + hashlib.sha256(drift_body).hexdigest()

        def drifting_list(*args, **kwargs):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 3:
                persistence[drift_path] = drift_body
            return stable_list(*args, **kwargs)

        transport.workspace_version_list_files = drifting_list  # type: ignore[method-assign]
        collector = WorkspaceVersionCoordinator(
            self.snapshot,
            collect_orphans=True,
        )
        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "orphan GC 两次扫描不稳定。",
        ):
            collector(transport, self.lease)
        self.assertIn(first_path, persistence)
        self.assertIn(drift_path, persistence)
        self.assertFalse(any(
            kind == "gc_delete" for kind, _path in transport.calls
        ))
        self.assertIsNone(collector.last_orphan_gc_report)

    def test_restore_rejects_non_tuple_version_listing_before_read_or_delete(self):
        transport = _MemoryTransport(self.workspace_root, {})

        def invalid_list(*_args, **_kwargs):
            return SandboxPersistenceListResult([], False)  # type: ignore[arg-type]

        transport.workspace_version_list_files = invalid_list  # type: ignore[method-assign]
        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "v2 version 清单无法完整读取。",
        ):
            WorkspaceVersionCoordinator(
                self.snapshot,
                collect_orphans=True,
            )(transport, self.lease)
        self.assertFalse(any(
            kind == "gc_delete" for kind, _path in transport.calls
        ))

    def test_gc_invalid_generation_chain_deletes_nothing(self):
        persistence: dict[str, bytes] = {}
        producer_transport = _MemoryTransport(self.workspace_root, persistence)
        producer = WorkspaceVersionCoordinator(self.snapshot)
        producer(producer_transport, self.lease)
        one = self._prepared(
            producer,
            producer_transport,
            content="one\n",
            operation="9" * 64,
        )
        producer.commit(
            producer_transport,
            self.lease,
            one.candidate,
            runtime_generation=0,
        )
        two = self._prepared(
            producer,
            producer_transport,
            content="two\n",
            operation="a" * 64,
        )
        producer.commit(
            producer_transport,
            self.lease,
            two.candidate,
            runtime_generation=0,
        )
        first_manifest = sorted(
            path for path in persistence if "/generations/" in path
        )[0]
        del persistence[first_manifest]
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        orphan = b"must survive invalid chain"
        orphan_path = root + "/objects/" + hashlib.sha256(orphan).hexdigest()
        persistence[orphan_path] = orphan
        before = dict(persistence)
        transport = _MemoryTransport(self.workspace_root, persistence)

        with self.assertRaises(WorkspaceVersionError):
            WorkspaceVersionCoordinator(
                self.snapshot,
                collect_orphans=True,
            )(transport, self.lease)
        self.assertEqual(persistence, before)
        self.assertFalse(any(
            kind == "gc_delete" for kind, _path in transport.calls
        ))

    def test_gc_delete_failure_is_sanitized_and_post_scan_failure_is_closed(self):
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        orphan = b"orphan"
        object_path = root + "/objects/" + hashlib.sha256(orphan).hexdigest()

        failed_persistence = {object_path: orphan}
        failed_transport = _MemoryTransport(
            self.workspace_root,
            failed_persistence,
        )
        failed_transport.fail_gc_delete_for.add(object_path)
        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "orphan GC 精确删除失败。",
        ) as caught:
            WorkspaceVersionCoordinator(
                self.snapshot,
                collect_orphans=True,
            )(failed_transport, self.lease)
        self.assertNotIn("provider secret", str(caught.exception))
        self.assertIn(object_path, failed_persistence)

        post_persistence = {object_path: orphan}
        post_transport = _MemoryTransport(self.workspace_root, post_persistence)
        stable_list = post_transport.workspace_version_list_files
        list_calls = 0

        def fail_post_scan(*args, **kwargs):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 4:
                return SandboxPersistenceListResult((), True)
            return stable_list(*args, **kwargs)

        post_transport.workspace_version_list_files = fail_post_scan  # type: ignore[method-assign]
        collector = WorkspaceVersionCoordinator(
            self.snapshot,
            collect_orphans=True,
        )
        with self.assertRaisesRegex(
            WorkspaceVersionError,
            "orphan GC 删除后复核失败。",
        ):
            collector(post_transport, self.lease)
        self.assertNotIn(object_path, post_persistence)
        self.assertIsNone(collector.last_orphan_gc_report)

    def test_restore_rejects_gap_and_split_brain(self):
        persistence: dict[str, bytes] = {}
        first = _MemoryTransport(self.workspace_root, persistence)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(first, self.lease)
        one = self._prepared(coordinator, first, content="one\n", operation="1" * 64)
        coordinator.commit(first, self.lease, one.candidate, runtime_generation=0)
        two = self._prepared(coordinator, first, content="two\n", operation="2" * 64)
        coordinator.commit(first, self.lease, two.candidate, runtime_generation=0)
        generation_paths = sorted(path for path in persistence if "/generations/" in path)
        saved_first = persistence.pop(generation_paths[0])
        with self.assertRaises(WorkspaceVersionError):
            WorkspaceVersionCoordinator(self.snapshot)(
                _MemoryTransport(self.workspace_root, persistence), self.lease
            )

        persistence[generation_paths[0]] = saved_first
        branch_body = b"branch\n"
        branch_file = WorkspaceVersionFile(
            "a.py", len(branch_body), hashlib.sha256(branch_body).hexdigest()
        )
        branch = WorkspaceVersionManifest(
            session_id=self.session_id,
            baseline_digest=self.snapshot.digest,
            generation=1,
            parent_manifest_sha256=None,
            operation_id="3" * 64,
            tool_name="write_file",
            operation="write_overwrite",
            arguments_digest="4" * 64,
            permission_mode="partial-control",
            path="a.py",
            before_sha256=hashlib.sha256(b"old\n").hexdigest(),
            after_sha256=branch_file.sha256,
            files=(branch_file,),
        )
        root = f"/mnt/oss/.agnet-study/checkpoints/v2/{self.session_id}"
        persistence[root + "/objects/" + branch_file.sha256] = branch_body
        persistence[
            root + f"/generations/{branch.generation:016d}-{branch.digest}.json"
        ] = branch.canonical_bytes()
        with self.assertRaises(WorkspaceVersionError):
            WorkspaceVersionCoordinator(self.snapshot)(
                _MemoryTransport(self.workspace_root, persistence), self.lease
            )

    def test_restore_rejects_host_seed_drift(self):
        persistence: dict[str, bytes] = {}
        first = _MemoryTransport(self.workspace_root, persistence)
        coordinator = WorkspaceVersionCoordinator(self.snapshot)
        coordinator(first, self.lease)
        prepared = self._prepared(coordinator, first)
        coordinator.commit(first, self.lease, prepared.candidate, runtime_generation=0)
        changed_seed = WorkspaceSeedSnapshot((_seed_entry("a.py", b"host drift\n"),))
        with self.assertRaises(Exception):
            WorkspaceVersionCoordinator(changed_seed)(
                _MemoryTransport(self.workspace_root, persistence), self.lease
            )


if __name__ == "__main__":
    unittest.main()
