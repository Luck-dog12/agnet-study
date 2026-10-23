"""Durable remote workspace mutations on immutable OSS generation manifests.

The host control plane keeps candidates only in memory.  File bodies are
content addressed, generation manifests are create-only, and a manifest is
published only after the active workspace mutation has been verified.  There
is deliberately no mutable "latest" pointer: restore accepts only one
continuous, unbranched generation chain.

The optional host-maintenance garbage collector is disabled by default.  When
explicitly enabled it runs only after checkpoint initialization and a complete
generation restore.  It retains the union of every published full-state
manifest and deletes only stable, twice-scanned unreferenced v2 objects through
a separate exact-delete transport primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import posixpath
from threading import RLock
from time import monotonic
from typing import Any, Final, Literal

from app.execution_backend import ExecutionLease
from app.permissions import PermissionMode, validate_permission_mode
from app.sandbox_transport import (
    SandboxFormatJsonFile,
    SandboxFormatJsonResult,
    SandboxFileEntry,
    SandboxListResult,
    SandboxPersistenceEntry,
    SandboxPersistenceListResult,
    SandboxReadResult,
    SandboxTransport,
    SandboxWriteResult,
    validate_sandbox_relative_path,
)
from app.schemas import (
    BashShellArguments,
    BashShellOutput,
    EditorFileArguments,
    EditorFileOutput,
    WriteFileArguments,
    WriteFileOutput,
)
from app.workspace_seed import (
    MAX_SEED_FILES,
    MAX_SEED_FILE_BYTES,
    MAX_SEED_TOTAL_BYTES,
    WorkspaceSeedSnapshot,
    _path_is_hard_excluded,
    _validate_snapshot,
)
from tool.editor_file import MAX_EDITABLE_FILE_BYTES, _apply_edit
from tool.pending_changes import combined_unified_diff_preview, unified_diff_preview


VERSION_SCHEMA: Final[int] = 2
COMMAND_VERSION_SCHEMA: Final[int] = 3
VERSION_NAMESPACE: Final[str] = ".agnet-study/checkpoints/v2"
MAX_VERSION_FILE_BYTES: Final[int] = MAX_SEED_FILE_BYTES
MAX_VERSION_GENERATIONS: Final[int] = 1_000
MAX_VERSION_MANIFEST_BYTES: Final[int] = 4 * 1024 * 1024
# Must not exceed E2BSandboxTransport's bounded persistence listing ceiling.
# Keeping the coordinator at the stricter transport boundary makes the limit
# enforceable before any provider I/O instead of relying on a looser fake.
MAX_VERSION_ENTRIES: Final[int] = 20_000
MAX_VERSION_DEPTH: Final[int] = 64
_IO_TIMEOUT: Final[int] = 30
_HEX: Final[frozenset[str]] = frozenset("0123456789abcdef")


class WorkspaceVersionError(RuntimeError):
    """A remote mutation or generation restore failed safely."""


class WorkspaceVersionFencingRequired(WorkspaceVersionError):
    """The active and durable states can no longer be proven consistent."""


@dataclass(frozen=True, slots=True)
class WorkspaceOrphanGcReport:
    """Frozen result of one explicitly authorized initialization-time GC."""

    generations: int
    retained_object_count: int
    orphan_object_count: int
    deleted_object_count: int
    orphan_bytes: int
    deleted_bytes: int
    baseline_digest: str

    def __post_init__(self) -> None:
        counts = (
            self.generations,
            self.retained_object_count,
            self.orphan_object_count,
            self.deleted_object_count,
            self.orphan_bytes,
            self.deleted_bytes,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise WorkspaceVersionError("orphan GC report 字段无效。")
        if (
            self.deleted_object_count != self.orphan_object_count
            or self.deleted_bytes != self.orphan_bytes
        ):
            raise WorkspaceVersionError("orphan GC report 删除统计不完整。")
        _hex(self.baseline_digest, 64, "orphan GC baseline digest")


def _hex(value: object, length: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in _HEX for character in value)
    ):
        raise WorkspaceVersionError(f"{label} 无效。")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise WorkspaceVersionError("generation manifest 无法规范编码。") from None


def _workspace_digest(files: tuple["WorkspaceVersionFile", ...]) -> str:
    return sha256(
        _canonical_json([[item.path, item.size, item.sha256] for item in files])
    ).hexdigest()


def version_session_root(persistence_root: str, session_id: str) -> str:
    if (
        not isinstance(persistence_root, str)
        or not persistence_root.startswith("/mnt/")
        or persistence_root.endswith("/")
        or posixpath.normpath(persistence_root) != persistence_root
        or "\\" in persistence_root
    ):
        raise WorkspaceVersionError("persistence_root 无效。")
    return posixpath.join(
        persistence_root,
        VERSION_NAMESPACE,
        _hex(session_id, 32, "session_id"),
    )


@dataclass(frozen=True, slots=True)
class WorkspaceVersionFile:
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        try:
            normalized = validate_sandbox_relative_path(self.path)
        except (TypeError, ValueError):
            raise WorkspaceVersionError("generation 文件路径无效。") from None
        if normalized != self.path or _path_is_hard_excluded(normalized):
            raise WorkspaceVersionError("generation 包含控制面、敏感或缓存路径。")
        if (
            type(self.size) is not int
            or not 0 <= self.size <= MAX_VERSION_FILE_BYTES
        ):
            raise WorkspaceVersionError("generation 文件大小无效。")
        _hex(self.sha256, 64, "generation 文件摘要")


@dataclass(frozen=True, slots=True)
class WorkspaceVersionManifest:
    session_id: str
    baseline_digest: str
    generation: int
    parent_manifest_sha256: str | None
    operation_id: str
    tool_name: Literal["write_file", "editor_file"]
    operation: Literal[
        "write_create",
        "write_overwrite",
        "replace_text",
        "replace_lines",
        "insert_lines",
        "delete_lines",
    ]
    arguments_digest: str
    permission_mode: PermissionMode
    path: str
    before_sha256: str | None
    after_sha256: str
    files: tuple[WorkspaceVersionFile, ...]
    total_bytes: int = field(init=False)
    workspace_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _hex(self.session_id, 32, "manifest session_id")
        _hex(self.baseline_digest, 64, "manifest baseline digest")
        if type(self.generation) is not int or not 1 <= self.generation <= MAX_VERSION_GENERATIONS:
            raise WorkspaceVersionError("manifest generation 无效。")
        if self.generation == 1:
            if self.parent_manifest_sha256 is not None:
                raise WorkspaceVersionError("首个 generation 不能声明父 manifest。")
        else:
            _hex(self.parent_manifest_sha256, 64, "parent manifest digest")
        _hex(self.operation_id, 64, "operation_id")
        _hex(self.arguments_digest, 64, "arguments digest")
        validate_permission_mode(self.permission_mode)
        if self.tool_name not in {"write_file", "editor_file"}:
            raise WorkspaceVersionError("manifest 工具无效。")
        if self.tool_name == "write_file":
            if self.operation not in {"write_create", "write_overwrite"}:
                raise WorkspaceVersionError("manifest 写入操作无效。")
        elif self.operation not in {
            "replace_text", "replace_lines", "insert_lines", "delete_lines"
        }:
            raise WorkspaceVersionError("manifest 编辑操作无效。")
        try:
            normalized = validate_sandbox_relative_path(self.path)
        except (TypeError, ValueError):
            raise WorkspaceVersionError("manifest mutation 路径无效。") from None
        if normalized != self.path or _path_is_hard_excluded(normalized):
            raise WorkspaceVersionError("manifest mutation 路径被禁止。")
        if self.before_sha256 is not None:
            _hex(self.before_sha256, 64, "before digest")
        _hex(self.after_sha256, 64, "after digest")
        if not isinstance(self.files, tuple) or not 1 <= len(self.files) <= MAX_SEED_FILES:
            raise WorkspaceVersionError("manifest 文件数量无效。")
        paths = [item.path for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise WorkspaceVersionError("manifest 文件必须唯一且排序。")
        total = 0
        for item in self.files:
            if type(item) is not WorkspaceVersionFile:
                raise WorkspaceVersionError("manifest 文件条目类型无效。")
            WorkspaceVersionFile.__post_init__(item)
            total += item.size
            if total > MAX_SEED_TOTAL_BYTES:
                raise WorkspaceVersionError("manifest 总大小超过安全上限。")
        changed = next((item for item in self.files if item.path == self.path), None)
        if changed is None or changed.sha256 != self.after_sha256:
            raise WorkspaceVersionError("manifest 未绑定变更后的文件。")
        object.__setattr__(self, "total_bytes", total)
        object.__setattr__(self, "workspace_digest", _workspace_digest(self.files))

    def canonical_bytes(self) -> bytes:
        WorkspaceVersionManifest.__post_init__(self)
        raw = _canonical_json(
            {
                "after_sha256": self.after_sha256,
                "arguments_digest": self.arguments_digest,
                "baseline_digest": self.baseline_digest,
                "before_sha256": self.before_sha256,
                "files": [
                    {"path": item.path, "sha256": item.sha256, "size": item.size}
                    for item in self.files
                ],
                "generation": self.generation,
                "operation_id": self.operation_id,
                "operation": self.operation,
                "parent_manifest_sha256": self.parent_manifest_sha256,
                "path": self.path,
                "permission_mode": self.permission_mode,
                "schema_version": VERSION_SCHEMA,
                "session_id": self.session_id,
                "tool_name": self.tool_name,
                "total_bytes": self.total_bytes,
                "workspace_digest": self.workspace_digest,
            }
        )
        if len(raw) > MAX_VERSION_MANIFEST_BYTES:
            raise WorkspaceVersionError("generation manifest 超过安全上限。")
        return raw

    @property
    def digest(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceVersionChange:
    path: str
    before_sha256: str
    after_sha256: str

    def __post_init__(self) -> None:
        try:
            normalized = validate_sandbox_relative_path(self.path)
        except (TypeError, ValueError):
            raise WorkspaceVersionError("command manifest 变更路径无效。") from None
        if normalized != self.path or _path_is_hard_excluded(normalized):
            raise WorkspaceVersionError("command manifest 变更路径被禁止。")
        _hex(self.before_sha256, 64, "command before digest")
        _hex(self.after_sha256, 64, "command after digest")
        if self.before_sha256 == self.after_sha256:
            raise WorkspaceVersionError("command manifest 变更前后摘要相同。")


@dataclass(frozen=True, slots=True)
class WorkspaceCommandManifest:
    session_id: str
    baseline_digest: str
    generation: int
    parent_manifest_sha256: str | None
    operation_id: str
    tool_name: Literal["bash_shell"]
    operation: Literal["format_json"]
    arguments_digest: str
    permission_mode: PermissionMode
    changes: tuple[WorkspaceVersionChange, ...]
    files: tuple[WorkspaceVersionFile, ...]
    total_bytes: int = field(init=False)
    workspace_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _hex(self.session_id, 32, "manifest session_id")
        _hex(self.baseline_digest, 64, "manifest baseline digest")
        if type(self.generation) is not int or not 1 <= self.generation <= MAX_VERSION_GENERATIONS:
            raise WorkspaceVersionError("manifest generation 无效。")
        if self.generation == 1:
            if self.parent_manifest_sha256 is not None:
                raise WorkspaceVersionError("首个 generation 不能声明父 manifest。")
        else:
            _hex(self.parent_manifest_sha256, 64, "parent manifest digest")
        _hex(self.operation_id, 64, "operation_id")
        _hex(self.arguments_digest, 64, "arguments digest")
        validate_permission_mode(self.permission_mode)
        if self.tool_name != "bash_shell" or self.operation != "format_json":
            raise WorkspaceVersionError("command manifest 工具或操作无效。")
        if not isinstance(self.changes, tuple) or not 1 <= len(self.changes) <= 8:
            raise WorkspaceVersionError("command manifest 变更数量无效。")
        change_paths = [item.path for item in self.changes]
        if change_paths != sorted(change_paths) or len(change_paths) != len(set(change_paths)):
            raise WorkspaceVersionError("command manifest 变更必须唯一且排序。")
        for item in self.changes:
            if type(item) is not WorkspaceVersionChange:
                raise WorkspaceVersionError("command manifest 变更类型无效。")
            WorkspaceVersionChange.__post_init__(item)
        if not isinstance(self.files, tuple) or not 1 <= len(self.files) <= MAX_SEED_FILES:
            raise WorkspaceVersionError("manifest 文件数量无效。")
        paths = [item.path for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise WorkspaceVersionError("manifest 文件必须唯一且排序。")
        mapped = {item.path: item for item in self.files}
        total = 0
        for item in self.files:
            if type(item) is not WorkspaceVersionFile:
                raise WorkspaceVersionError("manifest 文件条目类型无效。")
            WorkspaceVersionFile.__post_init__(item)
            total += item.size
            if total > MAX_SEED_TOTAL_BYTES:
                raise WorkspaceVersionError("manifest 总大小超过安全上限。")
        if any(mapped.get(change.path) is None or mapped[change.path].sha256 != change.after_sha256 for change in self.changes):
            raise WorkspaceVersionError("command manifest 未绑定变更后文件。")
        object.__setattr__(self, "total_bytes", total)
        object.__setattr__(self, "workspace_digest", _workspace_digest(self.files))

    def canonical_bytes(self) -> bytes:
        WorkspaceCommandManifest.__post_init__(self)
        raw = _canonical_json({
            "arguments_digest": self.arguments_digest,
            "baseline_digest": self.baseline_digest,
            "changes": [
                {"after_sha256": item.after_sha256, "before_sha256": item.before_sha256, "path": item.path}
                for item in self.changes
            ],
            "files": [
                {"path": item.path, "sha256": item.sha256, "size": item.size}
                for item in self.files
            ],
            "generation": self.generation,
            "operation": self.operation,
            "operation_id": self.operation_id,
            "parent_manifest_sha256": self.parent_manifest_sha256,
            "permission_mode": self.permission_mode,
            "schema_version": COMMAND_VERSION_SCHEMA,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "total_bytes": self.total_bytes,
            "workspace_digest": self.workspace_digest,
        })
        if len(raw) > MAX_VERSION_MANIFEST_BYTES:
            raise WorkspaceVersionError("generation manifest 超过安全上限。")
        return raw

    @property
    def digest(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceVersionError("generation manifest 包含重复键。")
        result[key] = value
    return result


def _reject_number(_value: str) -> Any:
    raise WorkspaceVersionError("generation manifest 包含非法数值。")


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("JSON constant is not finite")


def parse_manifest(raw: bytes) -> WorkspaceVersionManifest | WorkspaceCommandManifest:
    if not isinstance(raw, bytes) or len(raw) > MAX_VERSION_MANIFEST_BYTES:
        raise WorkspaceVersionError("generation manifest 不是有界 bytes。")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except WorkspaceVersionError:
        raise
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise WorkspaceVersionError("generation manifest JSON 无效。") from None
    if type(value) is not dict:
        raise WorkspaceVersionError("generation manifest 字段无效。")
    schema_version = value.get("schema_version")
    if schema_version == COMMAND_VERSION_SCHEMA:
        expected_v3 = {
            "arguments_digest", "baseline_digest", "changes", "files", "generation",
            "operation", "operation_id", "parent_manifest_sha256", "permission_mode",
            "schema_version", "session_id", "tool_name", "total_bytes", "workspace_digest",
        }
        if set(value) != expected_v3 or type(value["files"]) is not list or type(value["changes"]) is not list:
            raise WorkspaceVersionError("generation command manifest 字段无效。")
        try:
            files = tuple(WorkspaceVersionFile(**item) for item in value["files"] if type(item) is dict and set(item) == {"path", "sha256", "size"})
            changes = tuple(WorkspaceVersionChange(**item) for item in value["changes"] if type(item) is dict and set(item) == {"after_sha256", "before_sha256", "path"})
            if len(files) != len(value["files"]) or len(changes) != len(value["changes"]):
                raise WorkspaceVersionError("generation command manifest 条目无效。")
            manifest_v3 = WorkspaceCommandManifest(
                session_id=value["session_id"],
                baseline_digest=value["baseline_digest"],
                generation=value["generation"],
                parent_manifest_sha256=value["parent_manifest_sha256"],
                operation_id=value["operation_id"],
                tool_name=value["tool_name"],
                operation=value["operation"],
                arguments_digest=value["arguments_digest"],
                permission_mode=value["permission_mode"],
                changes=changes,
                files=files,
            )
        except (TypeError, ValueError):
            raise WorkspaceVersionError("generation command manifest 值无效。") from None
        if (
            type(value["total_bytes"]) is not int
            or type(value["workspace_digest"]) is not str
            or value["total_bytes"] != manifest_v3.total_bytes
            or value["workspace_digest"] != manifest_v3.workspace_digest
            or raw != manifest_v3.canonical_bytes()
        ):
            raise WorkspaceVersionError("generation command manifest 非规范或派生值不匹配。")
        return manifest_v3

    expected = {
        "after_sha256", "arguments_digest", "baseline_digest", "before_sha256",
        "files", "generation", "operation", "operation_id", "parent_manifest_sha256", "path",
        "permission_mode", "schema_version", "session_id", "tool_name", "total_bytes",
        "workspace_digest",
    }
    if set(value) != expected or schema_version != VERSION_SCHEMA:
        raise WorkspaceVersionError("generation manifest 字段无效。")
    if type(value["files"]) is not list:
        raise WorkspaceVersionError("generation manifest files 无效。")
    files: list[WorkspaceVersionFile] = []
    for item in value["files"]:
        if type(item) is not dict or set(item) != {"path", "sha256", "size"}:
            raise WorkspaceVersionError("generation manifest file 字段无效。")
        files.append(WorkspaceVersionFile(**item))
    try:
        manifest = WorkspaceVersionManifest(
            session_id=value["session_id"],
            baseline_digest=value["baseline_digest"],
            generation=value["generation"],
            parent_manifest_sha256=value["parent_manifest_sha256"],
            operation_id=value["operation_id"],
            tool_name=value["tool_name"],
            operation=value["operation"],
            arguments_digest=value["arguments_digest"],
            permission_mode=value["permission_mode"],
            path=value["path"],
            before_sha256=value["before_sha256"],
            after_sha256=value["after_sha256"],
            files=tuple(files),
        )
    except (TypeError, ValueError):
        raise WorkspaceVersionError("generation manifest 值无效。") from None
    if (
        type(value["total_bytes"]) is not int
        or type(value["workspace_digest"]) is not str
        or value["total_bytes"] != manifest.total_bytes
        or value["workspace_digest"] != manifest.workspace_digest
        or raw != manifest.canonical_bytes()
    ):
        raise WorkspaceVersionError("generation manifest 非规范或派生值不匹配。")
    return manifest


@dataclass(frozen=True, slots=True)
class RemoteMutationCandidate:
    session_id: str
    runtime_generation: int
    workspace_generation: int
    workspace_digest: str
    tool_name: Literal["write_file", "editor_file"]
    operation_id: str
    arguments_digest: str
    permission_mode: PermissionMode
    path: str
    operation: str
    before: bytes | None = field(repr=False)
    after: bytes = field(repr=False)
    replacements: int | None
    encoding: str
    expires_at: float = field(repr=False)

    @property
    def before_sha256(self) -> str | None:
        return None if self.before is None else sha256(self.before).hexdigest()

    @property
    def after_sha256(self) -> str:
        return sha256(self.after).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceMutation:
    candidate: RemoteMutationCandidate
    diff_preview: str


@dataclass(frozen=True, slots=True)
class RemoteCommandChange:
    path: str
    before: bytes = field(repr=False)
    after: bytes = field(repr=False)

    @property
    def before_sha256(self) -> str:
        return sha256(self.before).hexdigest()

    @property
    def after_sha256(self) -> str:
        return sha256(self.after).hexdigest()


@dataclass(frozen=True, slots=True)
class RemoteCommandMutationCandidate:
    session_id: str
    runtime_generation: int
    workspace_generation: int
    workspace_digest: str
    tool_name: Literal["bash_shell"]
    operation_id: str
    arguments_digest: str
    permission_mode: PermissionMode
    requested_paths: tuple[str, ...]
    changes: tuple[RemoteCommandChange, ...]
    expires_at: float = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparedWorkspaceCommandMutation:
    candidate: RemoteCommandMutationCandidate
    diff_preview: str


@dataclass(frozen=True, slots=True)
class _VersionState:
    generation: int
    manifest_digest: str | None
    workspace_digest: str
    files: tuple[WorkspaceVersionFile, ...]


@dataclass(frozen=True, slots=True)
class _VersionScan:
    inventory: tuple[SandboxPersistenceEntry, ...]
    manifest_chain: tuple[tuple[int, str], ...]
    referenced_objects: tuple[str, ...]
    orphan_objects: tuple[SandboxPersistenceEntry, ...]
    state: _VersionState
    latest_manifest: WorkspaceVersionManifest | WorkspaceCommandManifest | None


class WorkspaceVersionCoordinator:
    """One snapshot-bound initializer and mutation transaction coordinator."""

    def __init__(
        self,
        snapshot: WorkspaceSeedSnapshot,
        *,
        collect_orphans: bool = False,
    ) -> None:
        _validate_snapshot(snapshot)
        if type(collect_orphans) is not bool:
            raise TypeError("collect_orphans 必须是 bool。")
        self._snapshot = snapshot
        baseline_files = tuple(
            WorkspaceVersionFile(item.path, item.size, item.sha256)
            for item in snapshot.entries
        )
        self._baseline_state = _VersionState(
            0,
            None,
            _workspace_digest(baseline_files),
            baseline_files,
        )
        self._state = self._baseline_state
        self._collect_orphans = collect_orphans
        self._last_orphan_gc_report: WorkspaceOrphanGcReport | None = None
        self._lock = RLock()
        self._initialized_session: str | None = None

    def __call__(self, transport: SandboxTransport, lease: ExecutionLease) -> None:
        from app.workspace_checkpoint import initialize_workspace_from_checkpoint

        initialize_workspace_from_checkpoint(transport, lease, self._snapshot)
        with self._lock:
            self._state = self._restore_latest(transport, lease)
            if self._collect_orphans:
                self._last_orphan_gc_report = self._collect_stable_orphans(
                    transport,
                    lease,
                )
            self._initialized_session = lease.session_id

    @property
    def last_orphan_gc_report(self) -> WorkspaceOrphanGcReport | None:
        with self._lock:
            return self._last_orphan_gc_report

    def _require_initialized(self, lease: ExecutionLease) -> None:
        if self._initialized_session != lease.session_id:
            raise WorkspaceVersionError("workspace version coordinator 尚未初始化。")

    @staticmethod
    def _read_active(transport: SandboxTransport, path: str, maximum: int) -> bytes:
        result = transport.read_file(
            path, offset=0, max_bytes=maximum, timeout_seconds=_IO_TIMEOUT
        )
        if (
            type(result) is not SandboxReadResult
            or type(result.total_bytes) is not int
            or result.total_bytes != len(result.data)
            or len(result.data) > maximum
        ):
            raise WorkspaceVersionError("active workspace 文件读取不完整。")
        return result.data

    @staticmethod
    def _read_version(transport: SandboxTransport, path: str, maximum: int) -> bytes:
        result = transport.workspace_version_read_file(
            path, offset=0, max_bytes=maximum, timeout_seconds=_IO_TIMEOUT
        )
        if (
            type(result) is not SandboxReadResult
            or type(result.total_bytes) is not int
            or result.total_bytes != len(result.data)
            or len(result.data) > maximum
        ):
            raise WorkspaceVersionError("version 对象读取不完整。")
        return result.data

    def _scan_versions(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        *,
        validate_orphans: bool = False,
    ) -> _VersionScan:
        if type(validate_orphans) is not bool:
            raise TypeError("validate_orphans 必须是 bool。")
        root = version_session_root(lease.persistence_root or "", lease.session_id)
        listing = transport.workspace_version_list_files(
            root,
            max_results=MAX_VERSION_ENTRIES,
            max_depth=MAX_VERSION_DEPTH,
            timeout_seconds=_IO_TIMEOUT,
        )
        if (
            type(listing) is not SandboxPersistenceListResult
            or type(listing.entries) is not tuple
            or type(listing.truncated) is not bool
            or listing.truncated
        ):
            raise WorkspaceVersionError("v2 version 清单无法完整读取。")
        for item in listing.entries:
            if (
                type(item) is not SandboxPersistenceEntry
                or type(item.path) is not str
                or type(item.is_directory) is not bool
                or type(item.size) is not int
                or item.size < 0
            ):
                raise WorkspaceVersionError("v2 version 清单条目无效。")
        entries = tuple(sorted(listing.entries, key=lambda item: item.path))
        if len(entries) != len({item.path for item in entries}):
            raise WorkspaceVersionError("v2 version 清单路径不唯一。")
        manifests: dict[
            int, tuple[str, WorkspaceVersionManifest | WorkspaceCommandManifest]
        ] = {}
        objects: dict[str, SandboxPersistenceEntry] = {}
        generations_prefix = root + "/generations/"
        objects_prefix = root + "/objects/"
        allowed_directories = {root + "/objects", root + "/generations"}
        for item in entries:
            if item.is_directory:
                if item.path not in allowed_directories or item.size != 0:
                    raise WorkspaceVersionError("v2 version 清单包含未知目录。")
                continue
            if item.path.startswith(objects_prefix):
                digest = item.path[len(objects_prefix):]
                _hex(digest, 64, "version object digest")
                if item.size > MAX_VERSION_FILE_BYTES:
                    raise WorkspaceVersionError("version object 大小无效。")
                objects[digest] = item
                continue
            if not item.path.startswith(generations_prefix):
                raise WorkspaceVersionError("v2 version 清单包含未知文件。")
            name = item.path[len(generations_prefix):]
            if len(name) != 16 + 1 + 64 + 5 or name[16] != "-" or not name.endswith(".json"):
                raise WorkspaceVersionError("generation manifest 文件名无效。")
            generation_text, digest = name[:-5].split("-", 1)
            if not generation_text.isascii() or not generation_text.isdigit():
                raise WorkspaceVersionError("generation manifest 序号无效。")
            generation = int(generation_text)
            _hex(digest, 64, "manifest filename digest")
            raw = self._read_version(transport, item.path, MAX_VERSION_MANIFEST_BYTES)
            if len(raw) != item.size:
                raise WorkspaceVersionError("generation manifest 清单大小不匹配。")
            manifest = parse_manifest(raw)
            if manifest.digest != digest or manifest.generation != generation:
                raise WorkspaceVersionError("generation manifest 文件名与正文不匹配。")
            if generation in manifests:
                raise WorkspaceVersionError("检测到 generation split-brain。")
            manifests[generation] = (digest, manifest)
        if manifests and sorted(manifests) != list(range(1, max(manifests) + 1)):
            raise WorkspaceVersionError("generation 链不连续。")
        previous: str | None = None
        previous_files = {item.path: item for item in self._baseline_state.files}
        latest: WorkspaceVersionManifest | WorkspaceCommandManifest | None = None
        latest_digest: str | None = None
        seen_operations: set[str] = set()
        referenced_objects: set[str] = set()
        for generation in range(1, max(manifests, default=0) + 1):
            digest, manifest = manifests[generation]
            if (
                manifest.session_id != lease.session_id
                or manifest.baseline_digest != self._snapshot.digest
                or manifest.parent_manifest_sha256 != previous
                or manifest.operation_id in seen_operations
            ):
                raise WorkspaceVersionError("generation 链绑定无效或发生分叉。")
            next_files = dict(previous_files)
            if isinstance(manifest, WorkspaceVersionManifest):
                prior = previous_files.get(manifest.path)
                if manifest.before_sha256 is None:
                    if (
                        prior is not None
                        or manifest.tool_name != "write_file"
                        or manifest.operation != "write_create"
                    ):
                        raise WorkspaceVersionError("generation create 转移语义无效。")
                else:
                    if prior is None or prior.sha256 != manifest.before_sha256:
                        raise WorkspaceVersionError("generation before 摘要未绑定上一代。")
                    if manifest.tool_name == "write_file" and manifest.operation != "write_overwrite":
                        raise WorkspaceVersionError("generation overwrite 转移语义无效。")
                changed = next(
                    (item for item in manifest.files if item.path == manifest.path),
                    None,
                )
                if changed is None or changed.sha256 != manifest.after_sha256:
                    raise WorkspaceVersionError("generation after 摘要绑定无效。")
                next_files[manifest.path] = changed
            else:
                final_map = {item.path: item for item in manifest.files}
                for change in manifest.changes:
                    prior = previous_files.get(change.path)
                    changed = final_map.get(change.path)
                    if (
                        prior is None
                        or prior.sha256 != change.before_sha256
                        or changed is None
                        or changed.sha256 != change.after_sha256
                    ):
                        raise WorkspaceVersionError("generation command 变更未绑定上一代。")
                    next_files[change.path] = changed
            if tuple(sorted(next_files.values(), key=lambda item: item.path)) != manifest.files:
                raise WorkspaceVersionError("generation 修改了目标以外的文件状态。")
            seen_operations.add(manifest.operation_id)
            for item in manifest.files:
                referenced_objects.add(item.sha256)
                entry = objects.get(item.sha256)
                if entry is None or entry.is_directory or entry.size != item.size:
                    raise WorkspaceVersionError("generation 缺少内容对象。")
            previous_files = next_files
            previous, latest, latest_digest = digest, manifest, digest

        # Ordinary restore preserves the historical behavior of ignoring
        # unpublished objects.  Maintenance scans additionally validate every
        # orphan before any deletion can begin.
        verified_digests = (
            set(objects) if validate_orphans else referenced_objects
        )
        for digest in sorted(verified_digests):
            entry = objects[digest]
            body = self._read_version(
                transport,
                objects_prefix + digest,
                MAX_VERSION_FILE_BYTES,
            )
            if len(body) != entry.size or sha256(body).hexdigest() != digest:
                raise WorkspaceVersionError("generation 内容对象校验失败。")

        if latest is None:
            state = self._baseline_state
        else:
            assert latest_digest is not None
            state = _VersionState(
                latest.generation,
                latest_digest,
                latest.workspace_digest,
                latest.files,
            )
        referenced = tuple(sorted(referenced_objects))
        return _VersionScan(
            inventory=entries,
            manifest_chain=tuple(
                (generation, manifests[generation][0])
                for generation in sorted(manifests)
            ),
            referenced_objects=referenced,
            orphan_objects=tuple(
                objects[digest]
                for digest in sorted(set(objects).difference(referenced_objects))
            ),
            state=state,
            latest_manifest=latest,
        )

    def _restore_latest(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
    ) -> _VersionState:
        scan = self._scan_versions(transport, lease)
        if scan.latest_manifest is not None:
            root = version_session_root(
                lease.persistence_root or "",
                lease.session_id,
            )
            self._materialize_manifest(
                transport,
                lease,
                scan.latest_manifest,
                root + "/objects/",
            )
        return scan.state

    def _collect_stable_orphans(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
    ) -> WorkspaceOrphanGcReport:
        try:
            first = self._scan_versions(
                transport,
                lease,
                validate_orphans=True,
            )
            second = self._scan_versions(
                transport,
                lease,
                validate_orphans=True,
            )
        except Exception:
            raise WorkspaceVersionError("orphan GC 扫描验证失败。") from None
        if first != second:
            raise WorkspaceVersionError("orphan GC 两次扫描不稳定。")

        root = version_session_root(lease.persistence_root or "", lease.session_id)
        deleted_paths = {item.path for item in second.orphan_objects}
        orphan_bytes = sum(item.size for item in second.orphan_objects)
        for item in second.orphan_objects:
            digest = item.path[len(root + "/objects/"):]
            try:
                transport.gc_delete_workspace_version_object(
                    item.path,
                    expected_size=item.size,
                    expected_sha256=digest,
                    timeout_seconds=_IO_TIMEOUT,
                )
            except Exception:
                raise WorkspaceVersionError("orphan GC 精确删除失败。") from None

        try:
            after = self._scan_versions(
                transport,
                lease,
                validate_orphans=True,
            )
        except Exception:
            raise WorkspaceVersionError("orphan GC 删除后复核失败。") from None
        expected_inventory = tuple(
            item for item in second.inventory if item.path not in deleted_paths
        )
        if (
            after.inventory != expected_inventory
            or after.manifest_chain != second.manifest_chain
            or after.referenced_objects != second.referenced_objects
            or after.orphan_objects
            or after.state != second.state
            or after.latest_manifest != second.latest_manifest
        ):
            raise WorkspaceVersionError("orphan GC 删除后状态不匹配。")
        return WorkspaceOrphanGcReport(
            generations=len(second.manifest_chain),
            retained_object_count=len(second.referenced_objects),
            orphan_object_count=len(second.orphan_objects),
            deleted_object_count=len(second.orphan_objects),
            orphan_bytes=orphan_bytes,
            deleted_bytes=orphan_bytes,
            baseline_digest=self._snapshot.digest,
        )

    def _active_paths(self, transport: SandboxTransport, lease: ExecutionLease) -> dict[str, int]:
        result = transport.list_files(
            "**/*",
            workspace_root=lease.workspace_root,
            max_results=MAX_SEED_FILES,
            timeout_seconds=_IO_TIMEOUT,
            path_predicate=lambda path, is_directory: (
                is_directory
                or (
                    not _path_is_hard_excluded(path)
                    and not path.startswith(".agent-internal/")
                )
            ),
        )
        if type(result) is not SandboxListResult or result.truncated:
            raise WorkspaceVersionError("active workspace 清单不完整。")
        prefix = lease.workspace_root + "/"
        paths: dict[str, int] = {}
        for item in result.entries:
            if type(item) is not SandboxFileEntry or not item.path.startswith(prefix):
                raise WorkspaceVersionError("active workspace 清单无效。")
            relative = item.path[len(prefix):]
            if _path_is_hard_excluded(relative) or relative.startswith(".agent-internal/"):
                continue
            if type(item.size) is not int or not 0 <= item.size <= MAX_VERSION_FILE_BYTES:
                raise WorkspaceVersionError("active workspace 文件大小无效。")
            paths[relative] = item.size
        return paths

    def _materialize_manifest(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        manifest: WorkspaceVersionManifest | WorkspaceCommandManifest,
        objects_prefix: str,
    ) -> None:
        existing = self._active_paths(transport, lease)
        expected = {item.path for item in manifest.files}
        if not set(existing).issubset(expected):
            raise WorkspaceVersionError("active workspace 包含未纳入 generation 的文件。")
        for item in manifest.files:
            body = self._read_version(
                transport, objects_prefix + item.sha256, MAX_VERSION_FILE_BYTES
            )
            active = posixpath.join(lease.workspace_root, item.path)
            written = transport.write_file(
                active,
                body,
                overwrite=item.path in existing,
                timeout_seconds=_IO_TIMEOUT,
            )
            if type(written) is not SandboxWriteResult or written.bytes_written != item.size:
                raise WorkspaceVersionError("generation 恢复写入失败。")
            observed = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
            if observed != body or sha256(observed).hexdigest() != item.sha256:
                raise WorkspaceVersionError("generation 恢复回读失败。")
        final_listing = self._active_paths(transport, lease)
        expected_listing = {item.path: item.size for item in manifest.files}
        if final_listing != expected_listing:
            raise WorkspaceVersionError("generation 恢复后的 active 清单不匹配。")

    def prepare(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        *,
        tool_name: Literal["write_file", "editor_file"],
        arguments: WriteFileArguments | EditorFileArguments,
        operation_id: str,
        arguments_digest: str,
        permission_mode: PermissionMode,
        runtime_generation: int,
        lifetime_seconds: int = 300,
    ) -> PreparedWorkspaceMutation:
        with self._lock:
            self._require_initialized(lease)
            state = self._state
            _hex(operation_id, 64, "operation_id")
            _hex(arguments_digest, 64, "arguments digest")
            validate_permission_mode(permission_mode)
            path = validate_sandbox_relative_path(arguments.path)
            if _path_is_hard_excluded(path):
                raise WorkspaceVersionError("远端 mutation 不允许控制面、敏感或缓存路径。")
            known = {item.path: item for item in state.files}
            active = posixpath.join(lease.workspace_root, path)
            before: bytes | None
            replacements: int | None = None
            if isinstance(arguments, WriteFileArguments):
                if arguments.mode == "create":
                    if path in known:
                        raise FileExistsError("目标文件已经存在。")
                    before = None
                    after = arguments.content.encode(arguments.encoding)
                    before_text = ""
                else:
                    if path not in known:
                        raise FileNotFoundError("文件不存在。")
                    before = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
                    if sha256(before).hexdigest() != known[path].sha256:
                        raise WorkspaceVersionError("active 文件已偏离当前 generation。")
                    after = arguments.content.encode(arguments.encoding)
                    before_text = before.decode("utf-8-sig", errors="backslashreplace")
                after_text = arguments.content
                encoding = arguments.encoding
                operation = "write_" + arguments.mode
            else:
                if path not in known:
                    raise FileNotFoundError("文件不存在。")
                before = self._read_active(transport, active, MAX_EDITABLE_FILE_BYTES)
                if sha256(before).hexdigest() != known[path].sha256:
                    raise WorkspaceVersionError("active 文件已偏离当前 generation。")
                has_bom = before.startswith(b"\xef\xbb\xbf")
                body = before[3:] if has_bom else before
                try:
                    before_text = body.decode("utf-8")
                except UnicodeDecodeError:
                    raise WorkspaceVersionError("只能编辑 UTF-8 文本文件。") from None
                if "\x00" in before_text:
                    raise WorkspaceVersionError("只能编辑 UTF-8 文本文件。")
                after_text, replacements = _apply_edit(arguments, before_text)
                after = (b"\xef\xbb\xbf" if has_bom else b"") + after_text.encode("utf-8")
                encoding = "utf-8-sig" if has_bom else "utf-8"
                operation = arguments.operation
            if not 0 <= len(after) <= MAX_VERSION_FILE_BYTES:
                raise WorkspaceVersionError("mutation 候选超过文件大小上限。")
            if before is not None and after == before:
                raise WorkspaceVersionError("mutation 没有产生变化。")
            candidate = RemoteMutationCandidate(
                session_id=lease.session_id,
                runtime_generation=runtime_generation,
                workspace_generation=state.generation,
                workspace_digest=state.workspace_digest,
                tool_name=tool_name,
                operation_id=operation_id,
                arguments_digest=arguments_digest,
                permission_mode=permission_mode,
                path=path,
                operation=operation,
                before=before,
                after=after,
                replacements=replacements,
                encoding=encoding,
                expires_at=monotonic() + lifetime_seconds,
            )
            return PreparedWorkspaceMutation(
                candidate=candidate,
                diff_preview=unified_diff_preview(before_text, after_text),
            )

    def _verified_active_bodies(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        files: tuple[WorkspaceVersionFile, ...],
    ) -> dict[str, bytes]:
        listing = self._active_paths(transport, lease)
        expected_listing = {item.path: item.size for item in files}
        if listing != expected_listing:
            raise WorkspaceVersionError("active workspace 清单已偏离当前 generation。")
        bodies: dict[str, bytes] = {}
        for item in files:
            body = self._read_active(
                transport,
                posixpath.join(lease.workspace_root, item.path),
                MAX_VERSION_FILE_BYTES,
            )
            if len(body) != item.size or sha256(body).hexdigest() != item.sha256:
                raise WorkspaceVersionError("active workspace 已偏离当前 generation。")
            bodies[item.path] = body
        return bodies

    def prepare_command(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        *,
        tool_name: Literal["bash_shell"],
        arguments: BashShellArguments,
        operation_id: str,
        arguments_digest: str,
        permission_mode: PermissionMode,
        runtime_generation: int,
        lifetime_seconds: int = 300,
    ) -> PreparedWorkspaceCommandMutation:
        """Run one bounded transformer in the data plane without touching active files."""

        with self._lock:
            self._require_initialized(lease)
            state = self._state
            _hex(operation_id, 64, "operation_id")
            _hex(arguments_digest, 64, "arguments digest")
            validate_permission_mode(permission_mode)
            if tool_name != "bash_shell" or arguments.command != "format_json":
                raise WorkspaceVersionError("该命令不支持 mutation transaction。")
            requested = tuple(
                validate_sandbox_relative_path(path) for path in arguments.paths
            )
            sorted_requested = tuple(sorted(requested))
            if (
                not 1 <= len(requested) <= 8
                or len(requested) != len(set(requested))
                or any(_path_is_hard_excluded(path) or not path.casefold().endswith(".json") for path in requested)
            ):
                raise WorkspaceVersionError("format_json 路径无效。")
            known = {item.path: item for item in state.files}
            if any(path not in known for path in requested):
                raise FileNotFoundError("format_json 只允许修改已存在文件。")
            before_bodies = self._verified_active_bodies(transport, lease, state.files)
            remote_paths = tuple(
                posixpath.join(lease.workspace_root, path)
                for path in sorted_requested
            )
            generated = transport.format_json_candidates(
                remote_paths,
                timeout_seconds=arguments.timeout_seconds,
            )
            if type(generated) is not SandboxFormatJsonResult or len(generated.files) != len(requested):
                raise WorkspaceVersionError("format_json 候选返回类型无效。")
            changes: list[RemoteCommandChange] = []
            previews: list[tuple[str, str, str]] = []
            for relative, remote, item in zip(
                sorted_requested, remote_paths, generated.files, strict=True
            ):
                before = before_bodies[relative]
                if (
                    type(item) is not SandboxFormatJsonFile
                    or item.path != remote
                    or item.before_sha256 != sha256(before).hexdigest()
                    or not isinstance(item.after, bytes)
                    or len(item.after) > MAX_VERSION_FILE_BYTES
                    or sha256(item.after).hexdigest() == item.before_sha256
                    and item.after != before
                ):
                    raise WorkspaceVersionError("format_json 候选绑定无效。")
                if item.after == before:
                    continue
                try:
                    before_text = before.decode("utf-8-sig")
                    after_text = item.after.decode("utf-8")
                    parsed_after = json.loads(
                        after_text,
                        object_pairs_hook=_unique_object,
                        parse_constant=_reject_json_constant,
                    )
                    canonical_after = (
                        json.dumps(
                            parsed_after,
                            ensure_ascii=False,
                            allow_nan=False,
                            sort_keys=True,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8")
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    WorkspaceVersionError,
                ):
                    raise WorkspaceVersionError("format_json 候选不是 UTF-8 JSON。") from None
                if canonical_after != item.after:
                    raise WorkspaceVersionError(
                        "format_json 候选不是规范 JSON。"
                    )
                changes.append(RemoteCommandChange(relative, before, item.after))
                previews.append((relative, before_text, after_text))
            # A trusted candidate operation must leave the complete active tree untouched.
            after_prepare = self._verified_active_bodies(transport, lease, state.files)
            if after_prepare != before_bodies:
                raise WorkspaceVersionFencingRequired("format_json 候选执行修改了 active workspace。")
            if not changes:
                raise WorkspaceVersionError("format_json 没有产生变更。")
            candidate = RemoteCommandMutationCandidate(
                session_id=lease.session_id,
                runtime_generation=runtime_generation,
                workspace_generation=state.generation,
                workspace_digest=state.workspace_digest,
                tool_name="bash_shell",
                operation_id=operation_id,
                arguments_digest=arguments_digest,
                permission_mode=permission_mode,
                requested_paths=requested,
                changes=tuple(changes),
                expires_at=monotonic() + lifetime_seconds,
            )
            preview = combined_unified_diff_preview(tuple(previews))
            if not preview:
                raise WorkspaceVersionError("format_json 无法建立 diff。")
            return PreparedWorkspaceCommandMutation(candidate, preview)

    def _ensure_object(
        self, transport: SandboxTransport, path: str, body: bytes
    ) -> bool:
        """Ensure one immutable object and report whether this call created it."""

        observed = transport.workspace_version_probe_file(
            path, max_bytes=MAX_VERSION_FILE_BYTES, timeout_seconds=_IO_TIMEOUT
        )
        created = False
        if observed is None:
            written = transport.workspace_version_write_file(
                path, body, overwrite=False, timeout_seconds=_IO_TIMEOUT
            )
            if type(written) is not SandboxWriteResult or written.bytes_written != len(body):
                raise WorkspaceVersionError("version object 写入结果无效。")
            created = True
            observed = transport.workspace_version_probe_file(
                path, max_bytes=MAX_VERSION_FILE_BYTES, timeout_seconds=_IO_TIMEOUT
            )
        if (
            type(observed) is not SandboxReadResult
            or observed.total_bytes != len(body)
            or observed.data != body
            or sha256(observed.data).hexdigest() != sha256(body).hexdigest()
        ):
            raise WorkspaceVersionError("version object 校验失败。")
        return created

    @staticmethod
    def _cleanup_created_objects(
        transport: SandboxTransport,
        created_objects: tuple[tuple[str, int, str], ...],
    ) -> None:
        """Best-effort cleanup for objects proven new to this failed commit."""

        for path, expected_size, expected_sha256 in created_objects:
            try:
                transport.rollback_delete_workspace_version_object(
                    path,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    timeout_seconds=_IO_TIMEOUT,
                )
            except Exception:
                # The primary manifest failure is already certain.  A cleanup
                # failure leaves an inert orphan and must not create a false
                # claim that the mutation's durable outcome is uncertain.
                continue

    def _rollback_active(
        self,
        transport: SandboxTransport,
        active_path: str,
        candidate: RemoteMutationCandidate,
    ) -> None:
        if candidate.before is None:
            transport.rollback_delete_file(
                active_path,
                expected_sha256=candidate.after_sha256,
                timeout_seconds=_IO_TIMEOUT,
            )
            return
        current = self._read_active(transport, active_path, MAX_VERSION_FILE_BYTES)
        if sha256(current).hexdigest() != candidate.after_sha256:
            raise WorkspaceVersionFencingRequired("active rollback 前置摘要不匹配。")
        written = transport.write_file(
            active_path, candidate.before, overwrite=True, timeout_seconds=_IO_TIMEOUT
        )
        if type(written) is not SandboxWriteResult or written.bytes_written != len(candidate.before):
            raise WorkspaceVersionFencingRequired("active rollback 写入失败。")
        restored = self._read_active(transport, active_path, MAX_VERSION_FILE_BYTES)
        if restored != candidate.before:
            raise WorkspaceVersionFencingRequired("active rollback 无法确认。")

    def _rollback_command_active(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        applied: tuple[RemoteCommandChange, ...],
        expected_files: tuple[WorkspaceVersionFile, ...],
    ) -> None:
        try:
            for change in reversed(applied):
                active = posixpath.join(lease.workspace_root, change.path)
                current = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
                if current != change.after:
                    raise WorkspaceVersionFencingRequired("command rollback 前置内容不匹配。")
                written = transport.write_file(
                    active,
                    change.before,
                    overwrite=True,
                    timeout_seconds=_IO_TIMEOUT,
                )
                if type(written) is not SandboxWriteResult or written.bytes_written != len(change.before):
                    raise WorkspaceVersionFencingRequired("command rollback 写入失败。")
                restored = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
                if restored != change.before:
                    raise WorkspaceVersionFencingRequired("command rollback 回读不匹配。")
            self._verified_active_bodies(transport, lease, expected_files)
        except WorkspaceVersionFencingRequired:
            raise
        except BaseException:
            raise WorkspaceVersionFencingRequired("command rollback 无法完整确认。") from None

    def commit_command(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        candidate: RemoteCommandMutationCandidate,
        *,
        runtime_generation: int,
    ) -> BashShellOutput:
        """Commit all command changes as one v3 generation or roll all back."""

        with self._lock:
            self._require_initialized(lease)
            state = self._state
            if (
                type(candidate) is not RemoteCommandMutationCandidate
                or candidate.session_id != lease.session_id
                or candidate.runtime_generation != runtime_generation
                or candidate.workspace_generation != state.generation
                or candidate.workspace_digest != state.workspace_digest
                or candidate.tool_name != "bash_shell"
                or monotonic() > candidate.expires_at
                or not 1 <= len(candidate.changes) <= 8
            ):
                raise WorkspaceVersionError("远端 command mutation 候选已失效或绑定不匹配。")
            paths = tuple(change.path for change in candidate.changes)
            if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
                raise WorkspaceVersionError("远端 command mutation 变更路径无效。")
            before_bodies = self._verified_active_bodies(transport, lease, state.files)
            for change in candidate.changes:
                if (
                    type(change) is not RemoteCommandChange
                    or before_bodies.get(change.path) != change.before
                    or change.before == change.after
                    or len(change.after) > MAX_VERSION_FILE_BYTES
                ):
                    raise WorkspaceVersionError("文件在 diff 审批期间发生变化。")

            change_map = {item.path: item for item in candidate.changes}
            file_map = {item.path: item for item in state.files}
            for change in candidate.changes:
                file_map[change.path] = WorkspaceVersionFile(
                    change.path, len(change.after), change.after_sha256
                )
            files = tuple(sorted(file_map.values(), key=lambda item: item.path))
            manifest = WorkspaceCommandManifest(
                session_id=lease.session_id,
                baseline_digest=self._snapshot.digest,
                generation=state.generation + 1,
                parent_manifest_sha256=state.manifest_digest,
                operation_id=candidate.operation_id,
                tool_name="bash_shell",
                operation="format_json",
                arguments_digest=candidate.arguments_digest,
                permission_mode=candidate.permission_mode,
                changes=tuple(
                    WorkspaceVersionChange(item.path, item.before_sha256, item.after_sha256)
                    for item in candidate.changes
                ),
                files=files,
            )
            root = version_session_root(lease.persistence_root or "", lease.session_id)
            objects_prefix = root + "/objects/"
            created_objects: dict[str, tuple[int, str]] = {}
            try:
                for item in files:
                    body = (
                        change_map[item.path].after
                        if item.path in change_map
                        else before_bodies[item.path]
                    )
                    object_path = objects_prefix + item.sha256
                    if self._ensure_object(transport, object_path, body):
                        # Track only objects whose create and exact readback
                        # both completed. A failing current probe/write may be
                        # uncertain and must never be guessed safe to delete.
                        created_objects[object_path] = (item.size, item.sha256)
            except BaseException:
                self._cleanup_created_objects(
                    transport,
                    tuple(
                        (path, size, digest)
                        for path, (size, digest) in created_objects.items()
                    ),
                )
                raise

            applied: list[RemoteCommandChange] = []
            try:
                for change in candidate.changes:
                    active = posixpath.join(lease.workspace_root, change.path)
                    write_error: BaseException | None = None
                    try:
                        written = transport.write_file(
                            active,
                            change.after,
                            overwrite=True,
                            timeout_seconds=_IO_TIMEOUT,
                        )
                        if type(written) is not SandboxWriteResult or written.bytes_written != len(change.after):
                            raise WorkspaceVersionError("command active 写入结果无效。")
                    except BaseException as error:
                        write_error = error
                    try:
                        observed = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
                    except BaseException:
                        try:
                            observed = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
                        except BaseException:
                            raise WorkspaceVersionFencingRequired("command active 写入后状态无法判定。") from None
                    if observed == change.after:
                        applied.append(change)
                    elif observed != change.before:
                        raise WorkspaceVersionFencingRequired("command active 写入后内容不确定。")
                    if write_error is not None or observed != change.after:
                        raise WorkspaceVersionError("command active 写入失败。")
            except WorkspaceVersionFencingRequired:
                raise
            except BaseException as active_error:
                self._rollback_command_active(transport, lease, tuple(applied), state.files)
                self._cleanup_created_objects(
                    transport,
                    tuple((path, size, digest) for path, (size, digest) in created_objects.items()),
                )
                if not isinstance(active_error, Exception):
                    raise
                raise WorkspaceVersionError("command active 写入失败，已完整回滚。") from None

            manifest_raw = manifest.canonical_bytes()
            manifest_path = root + "/generations/" + f"{manifest.generation:016d}-{manifest.digest}.json"
            publish_error: BaseException | None = None
            try:
                published = transport.workspace_version_write_file(
                    manifest_path,
                    manifest_raw,
                    overwrite=False,
                    timeout_seconds=_IO_TIMEOUT,
                )
                if type(published) is not SandboxWriteResult or published.bytes_written != len(manifest_raw):
                    raise WorkspaceVersionError("generation manifest 发布结果无效。")
            except BaseException as error:
                publish_error = error
            try:
                observed_manifest = transport.workspace_version_probe_file(
                    manifest_path,
                    max_bytes=MAX_VERSION_MANIFEST_BYTES,
                    timeout_seconds=_IO_TIMEOUT,
                )
            except BaseException:
                raise WorkspaceVersionFencingRequired("generation manifest 发布结果无法判定。") from None
            if observed_manifest is None:
                self._rollback_command_active(transport, lease, tuple(applied), state.files)
                if publish_error is not None:
                    self._cleanup_created_objects(
                        transport,
                        tuple((path, size, digest) for path, (size, digest) in created_objects.items()),
                    )
                    if not isinstance(publish_error, Exception):
                        raise publish_error
                    raise WorkspaceVersionError("generation manifest 未发布，active 已完整回滚。") from None
                raise WorkspaceVersionFencingRequired("generation manifest 写入后确认不存在。")
            if (
                type(observed_manifest) is not SandboxReadResult
                or observed_manifest.total_bytes != len(manifest_raw)
                or observed_manifest.data != manifest_raw
            ):
                raise WorkspaceVersionFencingRequired("generation manifest 发布内容不匹配。")
            expected_bodies = dict(before_bodies)
            expected_bodies.update({item.path: item.after for item in candidate.changes})
            try:
                final_bodies = self._verified_active_bodies(
                    transport, lease, files
                )
            except BaseException:
                raise WorkspaceVersionFencingRequired(
                    "durable command commit 后 active 状态无法确认。"
                ) from None
            if final_bodies != expected_bodies:
                raise WorkspaceVersionFencingRequired("durable command commit 后 active 状态不匹配。")
            self._state = _VersionState(
                manifest.generation,
                manifest.digest,
                manifest.workspace_digest,
                manifest.files,
            )
            return BashShellOutput(
                command="format_json",
                paths=list(candidate.requested_paths),
                exit_code=0,
                stdout=f"已规范格式化 {len(candidate.changes)} 个 JSON 文件。",
                stderr="",
                truncated=False,
            )

    def commit(
        self,
        transport: SandboxTransport,
        lease: ExecutionLease,
        candidate: RemoteMutationCandidate,
        *,
        runtime_generation: int,
    ) -> WriteFileOutput | EditorFileOutput:
        with self._lock:
            self._require_initialized(lease)
            if (
                type(candidate) is not RemoteMutationCandidate
                or candidate.session_id != lease.session_id
                or candidate.runtime_generation != runtime_generation
                or candidate.workspace_generation != self._state.generation
                or candidate.workspace_digest != self._state.workspace_digest
                or monotonic() > candidate.expires_at
            ):
                raise WorkspaceVersionError("远端 mutation 候选已失效或绑定不匹配。")
            active = posixpath.join(lease.workspace_root, candidate.path)
            if candidate.before is None:
                if candidate.path in {item.path for item in self._state.files}:
                    raise WorkspaceVersionError("create 候选目标已经存在。")
            else:
                before = self._read_active(transport, active, MAX_VERSION_FILE_BYTES)
                if before != candidate.before or sha256(before).hexdigest() != candidate.before_sha256:
                    raise WorkspaceVersionError("文件在 diff 审批期间发生变化。")

            file_map = {item.path: item for item in self._state.files}
            file_map[candidate.path] = WorkspaceVersionFile(
                candidate.path, len(candidate.after), candidate.after_sha256
            )
            files = tuple(sorted(file_map.values(), key=lambda item: item.path))
            manifest = WorkspaceVersionManifest(
                session_id=lease.session_id,
                baseline_digest=self._snapshot.digest,
                generation=self._state.generation + 1,
                parent_manifest_sha256=self._state.manifest_digest,
                operation_id=candidate.operation_id,
                tool_name=candidate.tool_name,
                operation=candidate.operation,
                arguments_digest=candidate.arguments_digest,
                permission_mode=candidate.permission_mode,
                path=candidate.path,
                before_sha256=candidate.before_sha256,
                after_sha256=candidate.after_sha256,
                files=files,
            )
            root = version_session_root(lease.persistence_root or "", lease.session_id)
            objects_prefix = root + "/objects/"
            created_objects: dict[str, tuple[int, str]] = {}
            for item in files:
                if item.path == candidate.path:
                    body = candidate.after
                else:
                    body = self._read_active(
                        transport,
                        posixpath.join(lease.workspace_root, item.path),
                        MAX_VERSION_FILE_BYTES,
                    )
                    if len(body) != item.size or sha256(body).hexdigest() != item.sha256:
                        raise WorkspaceVersionError("active workspace 已偏离当前 generation。")
                object_path = objects_prefix + item.sha256
                if self._ensure_object(transport, object_path, body):
                    created_objects[object_path] = (item.size, item.sha256)

            try:
                written = transport.write_file(
                    active,
                    candidate.after,
                    overwrite=candidate.before is not None,
                    timeout_seconds=_IO_TIMEOUT,
                )
            except Exception:
                raise WorkspaceVersionFencingRequired(
                    "active mutation 写入结果无法判定。"
                ) from None
            if type(written) is not SandboxWriteResult or written.bytes_written != len(candidate.after):
                raise WorkspaceVersionFencingRequired("active mutation 写入结果不确定。")
            try:
                active_after = self._read_active(
                    transport, active, MAX_VERSION_FILE_BYTES
                )
            except Exception:
                raise WorkspaceVersionFencingRequired(
                    "active mutation 回读结果无法判定。"
                ) from None
            if active_after != candidate.after:
                raise WorkspaceVersionFencingRequired("active mutation 回读不匹配。")

            manifest_raw = manifest.canonical_bytes()
            manifest_path = (
                root
                + "/generations/"
                + f"{manifest.generation:016d}-{manifest.digest}.json"
            )
            publish_error: Exception | None = None
            try:
                published = transport.workspace_version_write_file(
                    manifest_path,
                    manifest_raw,
                    overwrite=False,
                    timeout_seconds=_IO_TIMEOUT,
                )
                if type(published) is not SandboxWriteResult or published.bytes_written != len(manifest_raw):
                    raise WorkspaceVersionError("generation manifest 发布结果无效。")
            except Exception as error:
                publish_error = error
            try:
                observed = transport.workspace_version_probe_file(
                    manifest_path,
                    max_bytes=MAX_VERSION_MANIFEST_BYTES,
                    timeout_seconds=_IO_TIMEOUT,
                )
            except Exception:
                raise WorkspaceVersionFencingRequired("generation manifest 发布结果无法判定。") from None
            if observed is None:
                try:
                    self._rollback_active(transport, active, candidate)
                except WorkspaceVersionFencingRequired:
                    raise
                except Exception:
                    raise WorkspaceVersionFencingRequired("generation 发布失败且 active 回滚不确定。") from None
                if publish_error is not None:
                    self._cleanup_created_objects(
                        transport,
                        tuple(
                            (path, expected_size, expected_sha256)
                            for path, (expected_size, expected_sha256)
                            in created_objects.items()
                        ),
                    )
                    raise WorkspaceVersionError("generation manifest 未发布，active 已回滚。") from None
                raise WorkspaceVersionFencingRequired("generation manifest 写入后确认不存在。")
            if (
                type(observed) is not SandboxReadResult
                or observed.total_bytes != len(manifest_raw)
                or observed.data != manifest_raw
            ):
                raise WorkspaceVersionFencingRequired("generation manifest 发布内容不匹配。")
            try:
                final_active = self._read_active(
                    transport, active, MAX_VERSION_FILE_BYTES
                )
            except Exception:
                raise WorkspaceVersionFencingRequired(
                    "durable commit 后 active 状态无法判定。"
                ) from None
            if final_active != candidate.after:
                raise WorkspaceVersionFencingRequired("durable commit 后 active 状态不匹配。")
            self._state = _VersionState(
                manifest.generation,
                manifest.digest,
                manifest.workspace_digest,
                manifest.files,
            )
            if candidate.tool_name == "write_file":
                return WriteFileOutput(
                    path=candidate.path,
                    encoding=candidate.encoding,
                    operation="created" if candidate.before is None else "overwritten",
                    bytes_written=len(candidate.after),
                    sha256=candidate.after_sha256,
                )
            assert candidate.before is not None and candidate.replacements is not None
            return EditorFileOutput(
                path=candidate.path,
                encoding=candidate.encoding,
                operation=candidate.operation,
                replacements=candidate.replacements,
                bytes_before=len(candidate.before),
                bytes_after=len(candidate.after),
                sha256_before=candidate.before_sha256,
                sha256_after=candidate.after_sha256,
            )


__all__ = [
    "MAX_VERSION_DEPTH",
    "MAX_VERSION_ENTRIES",
    "MAX_VERSION_FILE_BYTES",
    "MAX_VERSION_GENERATIONS",
    "MAX_VERSION_MANIFEST_BYTES",
    "COMMAND_VERSION_SCHEMA",
    "PreparedWorkspaceCommandMutation",
    "PreparedWorkspaceMutation",
    "RemoteCommandChange",
    "RemoteCommandMutationCandidate",
    "RemoteMutationCandidate",
    "VERSION_NAMESPACE",
    "VERSION_SCHEMA",
    "WorkspaceVersionCoordinator",
    "WorkspaceCommandManifest",
    "WorkspaceVersionChange",
    "WorkspaceVersionError",
    "WorkspaceVersionFencingRequired",
    "WorkspaceVersionFile",
    "WorkspaceVersionManifest",
    "WorkspaceOrphanGcReport",
    "parse_manifest",
    "version_session_root",
]
