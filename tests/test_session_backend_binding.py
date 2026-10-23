"""Execution-backend binding tests for persistent SessionEvent stores."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.schemas import TurnStartEventData
from app.workflow import (
    SessionEventError,
    SessionEventWorkflow,
    canonical_json_snapshot,
)


class SessionBackendBindingTests(unittest.TestCase):
    def test_new_default_local_and_explicit_remote_are_bound(self) -> None:
        for requested, expected in ((None, "local"), ("aliyun-sandbox", "aliyun-sandbox")):
            with self.subTest(requested=requested), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "session"
                workflow = SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name=requested,
                    recover=False,
                )
                try:
                    self.assertEqual(workflow.execution_backend_name, expected)
                    binding = (path / "execution.json").read_bytes()
                    self.assertEqual(
                        binding,
                        canonical_json_snapshot(
                            {
                                "backend_name": expected,
                                "session_id": workflow.session_id,
                                "version": 1,
                            }
                        ).encode("utf-8"),
                    )
                    with self.assertRaises(AttributeError):
                        workflow.execution_backend_name = "local"  # type: ignore[misc]
                finally:
                    workflow.close()

    def test_new_default_publishes_local_binding_before_header(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            published: list[str] = []
            original_atomic_publish = SessionEventWorkflow._atomic_publish

            def record_publish(
                workflow: SessionEventWorkflow,
                target: Path,
                payload: bytes,
                *,
                seq: int = 0,
            ) -> None:
                published.append(target.name)
                original_atomic_publish(
                    workflow,
                    target,
                    payload,
                    seq=seq,
                )

            with patch.object(
                SessionEventWorkflow,
                "_atomic_publish",
                new=record_publish,
            ):
                with SessionEventWorkflow(
                    storage_directory=path,
                    recover=False,
                ) as workflow:
                    self.assertEqual(workflow.execution_backend_name, "local")

            self.assertEqual(published[:2], ["execution.json", "session.json"])
            binding = json.loads(
                (path / "execution.json").read_text(encoding="utf-8")
            )
            self.assertEqual(binding["backend_name"], "local")

    def test_in_memory_binding_is_instance_only(self) -> None:
        local = SessionEventWorkflow.in_memory()
        remote = SessionEventWorkflow.in_memory(execution_backend_name="aliyun-sandbox")
        try:
            self.assertEqual(local.execution_backend_name, "local")
            self.assertEqual(remote.execution_backend_name, "aliyun-sandbox")
            self.assertNotEqual(local.session_id, remote.session_id)
        finally:
            local.close()
            remote.close()

    def test_new_session_default_is_used_only_when_identity_is_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(
                storage_directory=path,
                new_session_backend_name="aliyun-sandbox",
                recover=False,
            ) as created:
                session_id = created.session_id
                self.assertEqual(
                    created.execution_backend_name,
                    "aliyun-sandbox",
                )

            binding = (path / "execution.json").read_bytes()
            with SessionEventWorkflow(
                storage_directory=path,
                new_session_backend_name="local",
                require_existing_identity=True,
                recover=False,
            ) as restored:
                self.assertEqual(restored.session_id, session_id)
                self.assertEqual(
                    restored.execution_backend_name,
                    "aliyun-sandbox",
                )
            self.assertEqual((path / "execution.json").read_bytes(), binding)

        memory_only = SessionEventWorkflow(
            memory_only=True,
            new_session_backend_name="aliyun-sandbox",
        )
        try:
            self.assertEqual(memory_only.execution_backend_name, "local")
        finally:
            memory_only.close()

    def test_require_existing_identity_never_creates_a_fallback_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            path.mkdir()
            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    new_session_backend_name="aliyun-sandbox",
                    require_existing_identity=True,
                    recover=False,
                )
            self.assertFalse((path / "execution.json").exists())
            self.assertFalse((path / "session.json").exists())

            # Constructor failure released writer.lock.
            with SessionEventWorkflow(
                storage_directory=path,
                new_session_backend_name="local",
                recover=False,
            ) as created:
                self.assertEqual(created.execution_backend_name, "local")

    def test_reopen_adopts_binding_when_omitted_and_explicit_mismatch_fails(
        self,
    ) -> None:
        for bound, mismatch in (
            ("local", "aliyun-sandbox"),
            ("aliyun-sandbox", "local"),
        ):
            with self.subTest(bound=bound), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "session"
                with SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name=bound,
                    recover=False,
                ) as first:
                    session_id = first.session_id

                with SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name=bound,
                    recover=False,
                ) as restored:
                    self.assertEqual(restored.session_id, session_id)
                    self.assertEqual(restored.execution_backend_name, bound)

                with SessionEventWorkflow(
                    storage_directory=path,
                    recover=False,
                ) as adopted:
                    self.assertEqual(adopted.session_id, session_id)
                    self.assertEqual(adopted.execution_backend_name, bound)

                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(
                        storage_directory=path,
                        execution_backend_name=mismatch,
                        recover=False,
                    )

                # A rejected opener must release the single-writer lease.
                with SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name=bound,
                    recover=False,
                ):
                    pass

    def test_legacy_header_and_events_are_permanently_local(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(storage_directory=path, recover=False) as legacy:
                legacy.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
            event_path = path / "0000000000000001.json"
            original_event = event_path.read_bytes()
            (path / "execution.json").unlink()

            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name="aliyun-sandbox",
                    recover=False,
                )
            self.assertEqual(event_path.read_bytes(), original_event)
            self.assertEqual(
                json.loads((path / "execution.json").read_text(encoding="utf-8"))[
                    "backend_name"
                ],
                "local",
            )

            with SessionEventWorkflow(storage_directory=path, recover=False) as restored:
                self.assertEqual(restored.execution_backend_name, "local")
                self.assertEqual(restored.events[0].seq, 1)
            self.assertEqual(event_path.read_bytes(), original_event)

    def test_legacy_empty_header_is_local_not_a_new_remote_session(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(storage_directory=path, recover=False):
                pass
            (path / "execution.json").unlink()

            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name="aliyun-sandbox",
                    recover=False,
                )
            binding = json.loads((path / "execution.json").read_text(encoding="utf-8"))
            self.assertEqual(binding["backend_name"], "local")

    def test_sidecar_without_header_recovers_interrupted_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(
                storage_directory=path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as first:
                session_id = first.session_id
            (path / "session.json").unlink()
            binding = (path / "execution.json").read_bytes()

            with SessionEventWorkflow(storage_directory=path, recover=False) as restored:
                self.assertEqual(restored.session_id, session_id)
                self.assertEqual(restored.execution_backend_name, "aliyun-sandbox")
            header = json.loads((path / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(header["session_id"], session_id)
            self.assertEqual((path / "execution.json").read_bytes(), binding)

    def test_header_publish_crash_leaves_adoptable_binding_and_releases_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            original_atomic_publish = SessionEventWorkflow._atomic_publish

            def fail_header_publish(
                workflow: SessionEventWorkflow,
                target: Path,
                payload: bytes,
                *,
                seq: int = 0,
            ) -> None:
                if target.name == "session.json":
                    raise OSError("simulated header publication crash")
                original_atomic_publish(
                    workflow,
                    target,
                    payload,
                    seq=seq,
                )

            with patch.object(
                SessionEventWorkflow,
                "_atomic_publish",
                new=fail_header_publish,
            ):
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(
                        storage_directory=path,
                        execution_backend_name="aliyun-sandbox",
                        recover=False,
                    )

            self.assertFalse((path / "session.json").exists())
            binding = (path / "execution.json").read_bytes()
            binding_value = json.loads(binding.decode("utf-8"))
            self.assertEqual(binding_value["backend_name"], "aliyun-sandbox")

            # A conflicting explicit request fails without manufacturing a
            # header or rewriting the durable crash-window binding.
            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name="local",
                    recover=False,
                )
            self.assertFalse((path / "session.json").exists())
            self.assertEqual((path / "execution.json").read_bytes(), binding)

            # The rejected opener and failed creator both released writer.lock.
            with SessionEventWorkflow(
                storage_directory=path,
                recover=False,
            ) as restored:
                self.assertEqual(
                    restored.execution_backend_name,
                    "aliyun-sandbox",
                )
                self.assertEqual(
                    restored.session_id,
                    binding_value["session_id"],
                )
            self.assertEqual((path / "execution.json").read_bytes(), binding)

    def test_unknown_object_is_rejected_before_identity_files_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            path.mkdir()
            (path / "unknown.bin").write_bytes(b"do not touch")
            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name="aliyun-sandbox",
                    recover=False,
                )
            self.assertFalse((path / "execution.json").exists())
            self.assertFalse((path / "session.json").exists())

    def test_binding_without_header_cannot_repair_existing_events(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(
                storage_directory=path,
                execution_backend_name="aliyun-sandbox",
                recover=False,
            ) as workflow:
                workflow.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
            (path / "session.json").unlink()
            event = (path / "0000000000000001.json").read_bytes()

            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name="aliyun-sandbox",
                    recover=False,
                )
            self.assertFalse((path / "session.json").exists())
            self.assertEqual((path / "0000000000000001.json").read_bytes(), event)

    def test_legacy_sidecar_creation_failure_preserves_events_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with SessionEventWorkflow(storage_directory=path, recover=False) as workflow:
                workflow.append("turn/start", TurnStartEventData(turn_id=uuid4().hex))
            binding = path / "execution.json"
            binding.unlink()
            event_path = path / "0000000000000001.json"
            event = event_path.read_bytes()
            original_atomic_publish = SessionEventWorkflow._atomic_publish

            def fail_binding_publish(self, target: Path, payload: bytes, *, seq: int = 0):
                if target.name == "execution.json":
                    raise OSError("simulated binding failure")
                return original_atomic_publish(self, target, payload, seq=seq)

            with patch.object(
                SessionEventWorkflow,
                "_atomic_publish",
                new=fail_binding_publish,
            ):
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=path, recover=False)
            self.assertEqual(event_path.read_bytes(), event)
            self.assertFalse(binding.exists())

            with SessionEventWorkflow(storage_directory=path, recover=False):
                pass
            self.assertEqual(event_path.read_bytes(), event)

    def test_tampered_noncanonical_extra_and_wrong_session_sidecars_fail_closed(self) -> None:
        mutations = {
            "noncanonical": b'{"version":1, "session_id":"' + b"a" * 32
            + b'","backend_name":"local"}',
            "extra": canonical_json_snapshot(
                {
                    "backend_name": "local",
                    "extra": True,
                    "session_id": "a" * 32,
                    "version": 1,
                }
            ).encode("utf-8"),
            "wrong_session": canonical_json_snapshot(
                {
                    "backend_name": "local",
                    "session_id": "f" * 32,
                    "version": 1,
                }
            ).encode("utf-8"),
            "oversized": b"x" * 1025,
        }
        for name, payload in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "session"
                with SessionEventWorkflow(storage_directory=path, recover=False):
                    pass
                binding_path = path / "execution.json"
                original = binding_path.read_bytes()
                binding_path.write_bytes(payload)
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=path, recover=False)
                binding_path.write_bytes(original)
                with SessionEventWorkflow(storage_directory=path, recover=False):
                    pass

    def test_symlink_directory_and_hard_link_sidecars_are_rejected(self) -> None:
        exercised: list[str] = []
        for attack in ("symlink", "directory", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "session"
                with SessionEventWorkflow(storage_directory=path, recover=False):
                    pass
                binding = path / "execution.json"
                payload = binding.read_bytes()
                binding.unlink()
                external = Path(root) / "external"
                try:
                    if attack == "symlink":
                        external.write_bytes(payload)
                        binding.symlink_to(external)
                    elif attack == "directory":
                        binding.mkdir()
                    else:
                        external.write_bytes(payload)
                        os.link(external, binding)
                except (OSError, NotImplementedError) as exc:
                    if attack == "directory":
                        self.fail(f"创建测试目录失败：{type(exc).__name__}")
                    continue
                exercised.append(attack)
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=path, recover=False)
        self.assertIn("directory", exercised)

    def test_sidecar_publish_never_overwrites_a_racing_preoccupied_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            original_publish = SessionEventWorkflow._publish_no_replace
            attacker_payload = b"preoccupied"

            def occupy_then_publish(source: Path, target: Path) -> None:
                if target.name == "execution.json":
                    target.write_bytes(attacker_payload)
                original_publish(source, target)

            with patch.object(
                SessionEventWorkflow,
                "_publish_no_replace",
                side_effect=occupy_then_publish,
            ):
                with self.assertRaises(SessionEventError):
                    SessionEventWorkflow(storage_directory=path, recover=False)
            self.assertEqual((path / "execution.json").read_bytes(), attacker_payload)
            self.assertFalse((path / "session.json").exists())

            # The failed constructor must release its writer lock.
            (path / "execution.json").unlink()
            with SessionEventWorkflow(storage_directory=path, recover=False):
                pass

    def test_no_replace_publication_has_one_concurrent_winner(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            sources = [directory / "one", directory / "two"]
            target = directory / "execution.json"
            sources[0].write_bytes(b"one")
            sources[1].write_bytes(b"two")
            barrier = threading.Barrier(2)
            outcomes: list[str] = []
            outcome_lock = threading.Lock()

            def publish(source: Path) -> None:
                barrier.wait()
                try:
                    SessionEventWorkflow._publish_no_replace(source, target)
                except OSError:
                    result = "lost"
                else:
                    result = "won"
                with outcome_lock:
                    outcomes.append(result)

            threads = [threading.Thread(target=publish, args=(source,)) for source in sources]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(outcomes), ["lost", "won"])
            self.assertIn(target.read_bytes(), {b"one", b"two"})

    def test_invalid_backend_name_is_rejected_without_creating_storage(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "session"
            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    execution_backend_name="auto",  # type: ignore[arg-type]
                )
            self.assertFalse(path.exists())

            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    new_session_backend_name="SECRET-invalid",  # type: ignore[arg-type]
                )
            self.assertFalse(path.exists())

            with self.assertRaises(SessionEventError):
                SessionEventWorkflow(
                    storage_directory=path,
                    require_existing_identity=1,  # type: ignore[arg-type]
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
