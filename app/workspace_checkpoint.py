"""Immutable, content-addressed OSS baseline checkpoints for remote workspaces.

The host snapshot remains the authority.  A checkpoint manifest is accepted
only when its strict canonical bytes exactly match that snapshot and session.
All persistence access goes through :class:`SandboxTransport`; this module has
no raw SDK, shell, archive, pointer, mutation-sync, deletion, or GC path.
"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import posixpath
from typing import TYPE_CHECKING, Any, Final

from app.sandbox_transport import (
    SandboxFileEntry,
    SandboxListResult,
    SandboxPersistenceEntry,
    SandboxPersistenceListResult,
    SandboxReadResult,
    SandboxTransport,
    SandboxWriteResult,
)
from app.workspace_seed import (
    MAX_SEED_FILES,
    WorkspaceSeedEntry,
    WorkspaceSeedSnapshot,
    _validate_remote_workspace_root,
    _validate_snapshot,
)

if TYPE_CHECKING:
    from app.execution_backend import ExecutionLease


CHECKPOINT_SCHEMA_VERSION: Final[int] = 1
CHECKPOINT_NAMESPACE: Final[str] = ".agnet-study/checkpoints/v1"
CHECKPOINT_MANIFEST_NAME: Final[str] = "manifest.json"
CHECKPOINT_FILES_DIRECTORY: Final[str] = "files"
MAX_CHECKPOINT_MANIFEST_BYTES: Final[int] = 8 * 1024 * 1024
MAX_CHECKPOINT_ENTRIES: Final[int] = 20_000
MAX_CHECKPOINT_DEPTH: Final[int] = 64
_IO_TIMEOUT_SECONDS: Final[int] = 30
_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")


class WorkspaceCheckpointError(RuntimeError):
    """A baseline checkpoint could not be safely established or restored."""


def _validate_hex(value: str, length: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in _HEX for character in value)
    ):
        raise WorkspaceCheckpointError(f"{label} 无效。")
    return value


def _validate_persistence_root(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or value in {"", "/"}
        or not value.startswith("/")
        or value.startswith("//")
        or value.endswith("/")
        or not value.startswith("/mnt/")
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or posixpath.normpath(value) != value
        or any(part in {"", ".", ".."} for part in value.split("/")[1:])
    ):
        raise WorkspaceCheckpointError("远端 persistence_root 无效。")
    return value


def checkpoint_root_for(
    persistence_root: str,
    session_id: str,
    snapshot_digest: str,
) -> str:
    """Return the only checkpoint coordinate accepted by this schema."""
    root = _validate_persistence_root(persistence_root)
    session = _validate_hex(session_id, 32, "session_id")
    digest = _validate_hex(snapshot_digest, 64, "snapshot digest")
    return posixpath.join(root, CHECKPOINT_NAMESPACE, session, digest)


def canonical_checkpoint_manifest(
    snapshot: WorkspaceSeedSnapshot,
    session_id: str,
) -> bytes:
    """Build the strict canonical manifest; file bodies are never included."""
    _validate_snapshot(snapshot)
    session = _validate_hex(session_id, 32, "session_id")
    payload = {
        "files": [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size": entry.size,
            }
            for entry in snapshot.entries
        ],
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "session_id": session,
        "snapshot_digest": snapshot.digest,
        "total_bytes": snapshot.total_bytes,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_CHECKPOINT_MANIFEST_BYTES:
        raise WorkspaceCheckpointError("checkpoint manifest 超过安全上限。")
    return encoded


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise WorkspaceCheckpointError("checkpoint manifest 包含重复键。")
        output[key] = value
    return output


def _reject_json_number(_value: str) -> Any:
    raise WorkspaceCheckpointError("checkpoint manifest 包含不允许的数值。")


def validate_checkpoint_manifest(
    data: bytes,
    snapshot: WorkspaceSeedSnapshot,
    session_id: str,
) -> None:
    """Strictly parse OSS bytes, then require exact host-derived canonical bytes."""
    expected = canonical_checkpoint_manifest(snapshot, session_id)
    if not isinstance(data, bytes) or len(data) > MAX_CHECKPOINT_MANIFEST_BYTES:
        raise WorkspaceCheckpointError("checkpoint manifest 不是有界 bytes。")
    try:
        text = data.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except WorkspaceCheckpointError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkspaceCheckpointError("checkpoint manifest JSON 无效。") from None

    required = {
        "files",
        "schema_version",
        "session_id",
        "snapshot_digest",
        "total_bytes",
    }
    if type(parsed) is not dict or set(parsed) != required:
        raise WorkspaceCheckpointError("checkpoint manifest 字段无效。")
    if type(parsed["schema_version"]) is not int or parsed["schema_version"] != 1:
        raise WorkspaceCheckpointError("checkpoint manifest schema 无效。")
    if type(parsed["session_id"]) is not str:
        raise WorkspaceCheckpointError("checkpoint manifest session_id 无效。")
    _validate_hex(parsed["session_id"], 32, "manifest session_id")
    if type(parsed["snapshot_digest"]) is not str:
        raise WorkspaceCheckpointError("checkpoint manifest digest 无效。")
    _validate_hex(parsed["snapshot_digest"], 64, "manifest digest")
    if type(parsed["total_bytes"]) is not int or parsed["total_bytes"] < 0:
        raise WorkspaceCheckpointError("checkpoint manifest total_bytes 无效。")
    files = parsed["files"]
    if type(files) is not list or len(files) > MAX_SEED_FILES:
        raise WorkspaceCheckpointError("checkpoint manifest files 无效。")
    for item in files:
        if type(item) is not dict or set(item) != {"path", "sha256", "size"}:
            raise WorkspaceCheckpointError("checkpoint manifest file 字段无效。")
        if (
            type(item["path"]) is not str
            or type(item["sha256"]) is not str
            or type(item["size"]) is not int
            or item["size"] < 0
        ):
            raise WorkspaceCheckpointError("checkpoint manifest file 元数据无效。")
    if data != expected:
        raise WorkspaceCheckpointError("checkpoint manifest 与宿主快照不匹配。")


def _checkpoint_paths(
    checkpoint_root: str,
    snapshot: WorkspaceSeedSnapshot,
) -> tuple[
    dict[str, WorkspaceSeedEntry],
    dict[str, int],
    dict[str, int],
]:
    """Return data files, allowed partial entries, and exact finalized entries."""
    files: dict[str, WorkspaceSeedEntry] = {}
    allowed: dict[str, int] = {}
    for entry in snapshot.entries:
        remote = posixpath.join(
            checkpoint_root,
            CHECKPOINT_FILES_DIRECTORY,
            entry.path,
        )
        files[remote] = entry
        allowed[remote] = entry.size
        relative_parts = remote[len(checkpoint_root) + 1 :].split("/")[:-1]
        current = checkpoint_root
        for part in relative_parts:
            current += "/" + part
            allowed[current] = -1
    manifest_path = posixpath.join(checkpoint_root, CHECKPOINT_MANIFEST_NAME)
    finalized = dict(allowed)
    finalized[manifest_path] = -2
    if len(finalized) > MAX_CHECKPOINT_ENTRIES:
        raise WorkspaceCheckpointError("checkpoint 清单条目超过安全上限。")
    for path in finalized:
        depth = len(path[len(checkpoint_root) + 1 :].split("/"))
        if depth > MAX_CHECKPOINT_DEPTH:
            raise WorkspaceCheckpointError("checkpoint 路径深度超过安全上限。")
    return files, allowed, finalized


def preflight_workspace_checkpoint(snapshot: WorkspaceSeedSnapshot) -> None:
    """Reject deterministic manifest/layout failures before any cloud object exists."""
    _validate_snapshot(snapshot)
    dummy_session = "0" * 32
    canonical_checkpoint_manifest(snapshot, dummy_session)
    dummy_root = checkpoint_root_for(
        "/mnt/oss",
        dummy_session,
        snapshot.digest,
    )
    _checkpoint_paths(dummy_root, snapshot)


def _validate_persistence_listing(
    result: SandboxPersistenceListResult,
    checkpoint_root: str,
) -> dict[str, SandboxPersistenceEntry]:
    if type(result) is not SandboxPersistenceListResult or result.truncated:
        raise WorkspaceCheckpointError("checkpoint 清单无法完整验证。")
    output: dict[str, SandboxPersistenceEntry] = {}
    previous = ""
    for item in result.entries:
        if (
            type(item) is not SandboxPersistenceEntry
            or not isinstance(item.path, str)
            or not item.path.startswith(checkpoint_root + "/")
            or posixpath.normpath(item.path) != item.path
            or "\\" in item.path
            or "\x00" in item.path
            or type(item.is_directory) is not bool
            or type(item.size) is not int
            or item.size < 0
            or (item.is_directory and item.size != 0)
            or item.path <= previous
            or item.path in output
        ):
            raise WorkspaceCheckpointError("checkpoint 清单包含无效条目。")
        output[item.path] = item
        previous = item.path
    return output


def _list_checkpoint(
    transport: SandboxTransport,
    checkpoint_root: str,
) -> dict[str, SandboxPersistenceEntry]:
    result = transport.persistence_list_files(
        checkpoint_root,
        max_results=MAX_CHECKPOINT_ENTRIES,
        max_depth=MAX_CHECKPOINT_DEPTH,
        timeout_seconds=_IO_TIMEOUT_SECONDS,
    )
    return _validate_persistence_listing(result, checkpoint_root)


def _read_persistence_exact(
    transport: SandboxTransport,
    path: str,
    expected: WorkspaceSeedEntry,
) -> bytes:
    result = transport.persistence_read_file(
        path,
        offset=0,
        max_bytes=expected.size,
        timeout_seconds=_IO_TIMEOUT_SECONDS,
    )
    if (
        type(result) is not SandboxReadResult
        or not isinstance(result.data, bytes)
        or type(result.total_bytes) is not int
        or result.total_bytes != expected.size
        or len(result.data) != expected.size
        or hashlib.sha256(result.data).hexdigest() != expected.sha256
        or result.data != expected.data
    ):
        raise WorkspaceCheckpointError("checkpoint 文件内容校验失败。")
    return result.data


def _read_manifest_exact(
    transport: SandboxTransport,
    path: str,
    snapshot: WorkspaceSeedSnapshot,
    session_id: str,
) -> bytes:
    expected = canonical_checkpoint_manifest(snapshot, session_id)
    result = transport.persistence_read_file(
        path,
        offset=0,
        max_bytes=MAX_CHECKPOINT_MANIFEST_BYTES,
        timeout_seconds=_IO_TIMEOUT_SECONDS,
    )
    if (
        type(result) is not SandboxReadResult
        or not isinstance(result.data, bytes)
        or type(result.total_bytes) is not int
        or result.total_bytes != len(result.data)
    ):
        raise WorkspaceCheckpointError("checkpoint manifest 回读不完整。")
    validate_checkpoint_manifest(result.data, snapshot, session_id)
    if result.data != expected:
        raise WorkspaceCheckpointError("checkpoint manifest 回读不匹配。")
    return result.data


def _assert_listing_matches(
    listing: dict[str, SandboxPersistenceEntry],
    expected: dict[str, int],
    *,
    manifest_size: int | None,
) -> None:
    if set(listing) != set(expected):
        raise WorkspaceCheckpointError("checkpoint 清单不匹配。")
    for path, expected_size in expected.items():
        item = listing[path]
        if expected_size == -1:
            valid = item.is_directory and item.size == 0
        elif expected_size == -2:
            valid = (
                manifest_size is not None
                and not item.is_directory
                and item.size == manifest_size
            )
        else:
            valid = not item.is_directory and item.size == expected_size
        if not valid:
            raise WorkspaceCheckpointError("checkpoint 清单条目类型或大小不匹配。")


def _list_active_workspace(
    transport: SandboxTransport,
    workspace_root: str,
) -> SandboxListResult:
    result = transport.list_files(
        "**/*",
        workspace_root=workspace_root,
        max_results=MAX_SEED_FILES,
        timeout_seconds=_IO_TIMEOUT_SECONDS,
        path_predicate=lambda _path, _is_directory: True,
    )
    if type(result) is not SandboxListResult or result.truncated:
        raise WorkspaceCheckpointError("活动 workspace 清单无法完整验证。")
    return result


def _validate_active_listing(
    result: SandboxListResult,
    workspace_root: str,
    snapshot: WorkspaceSeedSnapshot,
) -> None:
    expected = tuple(
        (posixpath.join(workspace_root, entry.path), entry.size)
        for entry in snapshot.entries
    )
    actual: list[tuple[str, int]] = []
    for item in result.entries:
        if (
            type(item) is not SandboxFileEntry
            or not isinstance(item.path, str)
            or not item.path.startswith(workspace_root + "/")
            or type(item.size) is not int
            or item.size < 0
        ):
            raise WorkspaceCheckpointError("活动 workspace 清单包含无效条目。")
        actual.append((item.path, item.size))
    if tuple(actual) != expected:
        raise WorkspaceCheckpointError("活动 workspace 清单不匹配。")


def initialize_workspace_from_checkpoint(
    transport: SandboxTransport,
    lease: ExecutionLease,
    snapshot: WorkspaceSeedSnapshot,
) -> None:
    """Safely resume/create one baseline, then restore it into an empty workspace."""
    try:
        _validate_snapshot(snapshot)
        workspace_root = _validate_remote_workspace_root(lease.workspace_root)
        session_id = _validate_hex(lease.session_id, 32, "session_id")
        if not workspace_root.endswith("/" + session_id):
            raise WorkspaceCheckpointError("workspace_root 与 session_id 不匹配。")
        persistence_root = _validate_persistence_root(lease.persistence_root)
        checkpoint_root = checkpoint_root_for(
            persistence_root,
            session_id,
            snapshot.digest,
        )
        data_files, partial_expected, final_expected = _checkpoint_paths(
            checkpoint_root,
            snapshot,
        )
        manifest_path = posixpath.join(checkpoint_root, CHECKPOINT_MANIFEST_NAME)
        manifest = canonical_checkpoint_manifest(snapshot, session_id)

        active_before = _list_active_workspace(transport, workspace_root)
        if active_before.entries:
            raise WorkspaceCheckpointError("活动 workspace 初始化前包含普通文件。")

        listing = _list_checkpoint(transport, checkpoint_root)
        manifest_present = manifest_path in listing
        if manifest_present:
            _assert_listing_matches(
                listing,
                final_expected,
                manifest_size=len(manifest),
            )
            _read_manifest_exact(
                transport,
                manifest_path,
                snapshot,
                session_id,
            )
            for path, entry in data_files.items():
                _read_persistence_exact(transport, path, entry)
        else:
            if not set(listing).issubset(partial_expected):
                raise WorkspaceCheckpointError("未完成 checkpoint 包含额外路径。")
            for path, item in listing.items():
                expected_size = partial_expected[path]
                if expected_size == -1:
                    if not item.is_directory or item.size != 0:
                        raise WorkspaceCheckpointError("checkpoint 路径类型不匹配。")
                else:
                    if item.is_directory or item.size != expected_size:
                        raise WorkspaceCheckpointError("checkpoint 文件大小或类型不匹配。")
                    _read_persistence_exact(transport, path, data_files[path])

            for path, entry in data_files.items():
                if path in listing:
                    continue
                written = transport.persistence_write_file(
                    path,
                    entry.data,
                    overwrite=False,
                    timeout_seconds=_IO_TIMEOUT_SECONDS,
                )
                if (
                    type(written) is not SandboxWriteResult
                    or type(written.bytes_written) is not int
                    or written.bytes_written != entry.size
                ):
                    raise WorkspaceCheckpointError("checkpoint create-only 写入结果无效。")
                _read_persistence_exact(transport, path, entry)

            data_listing = _list_checkpoint(transport, checkpoint_root)
            _assert_listing_matches(
                data_listing,
                partial_expected,
                manifest_size=None,
            )
            written_manifest = transport.persistence_write_file(
                manifest_path,
                manifest,
                overwrite=False,
                timeout_seconds=_IO_TIMEOUT_SECONDS,
            )
            if (
                type(written_manifest) is not SandboxWriteResult
                or type(written_manifest.bytes_written) is not int
                or written_manifest.bytes_written != len(manifest)
            ):
                raise WorkspaceCheckpointError("checkpoint manifest 发布结果无效。")
            _read_manifest_exact(
                transport,
                manifest_path,
                snapshot,
                session_id,
            )

        final_listing = _list_checkpoint(transport, checkpoint_root)
        _assert_listing_matches(
            final_listing,
            final_expected,
            manifest_size=len(manifest),
        )

        for path, entry in data_files.items():
            body = _read_persistence_exact(transport, path, entry)
            active_path = posixpath.join(workspace_root, entry.path)
            written = transport.write_file(
                active_path,
                body,
                overwrite=False,
                timeout_seconds=_IO_TIMEOUT_SECONDS,
            )
            if (
                type(written) is not SandboxWriteResult
                or type(written.bytes_written) is not int
                or written.bytes_written != entry.size
            ):
                raise WorkspaceCheckpointError("活动 workspace create-only 写入结果无效。")
            read_back = transport.read_file(
                active_path,
                offset=0,
                max_bytes=entry.size,
                timeout_seconds=_IO_TIMEOUT_SECONDS,
            )
            if (
                type(read_back) is not SandboxReadResult
                or not isinstance(read_back.data, bytes)
                or type(read_back.total_bytes) is not int
                or read_back.total_bytes != entry.size
                or read_back.data != body
                or hashlib.sha256(read_back.data).hexdigest() != entry.sha256
            ):
                raise WorkspaceCheckpointError("活动 workspace 回读校验失败。")

        active_after = _list_active_workspace(transport, workspace_root)
        _validate_active_listing(active_after, workspace_root, snapshot)
    except WorkspaceCheckpointError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise WorkspaceCheckpointError(
            "远端 immutable baseline checkpoint 初始化失败。"
        ) from None


def create_workspace_checkpoint_initializer(
    snapshot: WorkspaceSeedSnapshot,
    *,
    collect_orphans: bool = False,
) -> Callable[[SandboxTransport, ExecutionLease], None]:
    """Bind baseline restore plus the v2 durable mutation coordinator.

    The returned object remains a callable initializer for the backend, while
    also exposing the private prepare/commit seam consumed by the session
    runtime.  No raw transport or OSS coordinate is exposed to the Agent.
    """
    preflight_workspace_checkpoint(snapshot)
    from app.workspace_versions import WorkspaceVersionCoordinator

    return WorkspaceVersionCoordinator(
        snapshot,
        collect_orphans=collect_orphans,
    )


__all__ = [
    "CHECKPOINT_FILES_DIRECTORY",
    "CHECKPOINT_MANIFEST_NAME",
    "CHECKPOINT_NAMESPACE",
    "CHECKPOINT_SCHEMA_VERSION",
    "MAX_CHECKPOINT_DEPTH",
    "MAX_CHECKPOINT_ENTRIES",
    "MAX_CHECKPOINT_MANIFEST_BYTES",
    "WorkspaceCheckpointError",
    "canonical_checkpoint_manifest",
    "checkpoint_root_for",
    "create_workspace_checkpoint_initializer",
    "initialize_workspace_from_checkpoint",
    "preflight_workspace_checkpoint",
    "validate_checkpoint_manifest",
]
