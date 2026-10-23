"""Trusted-host permission mode and centralized mutation approval routing."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import os
from typing import Iterator, Literal, TypeAlias


PermissionMode: TypeAlias = Literal["partial-control", "full-access"]
DEFAULT_PERMISSION_MODE: PermissionMode = "partial-control"
PERMISSION_MODES = ("partial-control", "full-access")

_mode: ContextVar[PermissionMode] = ContextVar(
    "agent_permission_mode", default=DEFAULT_PERMISSION_MODE
)
_mutation: ContextVar[bool] = ContextVar("agent_mutation_access", default=False)

_SENSITIVE_SEGMENTS = frozenset({
    ".agent", ".git", ".venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache",
})
_SENSITIVE_NAMES = frozenset({".env", "code.md", "agents.md"})
_SENSITIVE_SUFFIXES = frozenset({
    ".key", ".pem", ".p12", ".pfx", ".crt", ".cer", ".der", ".jks",
})
_SENSITIVE_RELATIVE_ROOTS = ("data/pending", "data/sessions", "data/logs")


def validate_permission_mode(value: object) -> PermissionMode:
    if value not in PERMISSION_MODES:
        raise ValueError("permission_mode 必须是 partial-control 或 full-access。")
    return value  # type: ignore[return-value]


def current_permission_mode() -> PermissionMode:
    return _mode.get()


def mutation_access_active() -> bool:
    return _mutation.get()


@contextmanager
def permission_scope(
    mode: PermissionMode, *, mutation: bool = False
) -> Iterator[None]:
    validated = validate_permission_mode(mode)
    mode_token = _mode.set(validated)
    mutation_token = _mutation.set(bool(mutation))
    try:
        yield
    finally:
        _mutation.reset(mutation_token)
        _mode.reset(mode_token)


def _project_relative(path: str, project_root: Path) -> str | None:
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            return path.replace("\\", "/")
        absolute = Path(os.path.abspath(candidate))
        return absolute.relative_to(project_root).as_posix()
    except (OSError, ValueError):
        return None


def is_sensitive_mutation_path(path: str, *, project_root: Path) -> bool:
    """Classify without resolving or following the target path."""
    relative = _project_relative(path, project_root)
    if relative is None:
        # partial-control cannot target outside the project; full-access never asks
        # for approval. Treat unclassifiable input as sensitive/fail closed.
        return True
    pure = PurePosixPath(relative)
    folded_parts = tuple(part.casefold() for part in pure.parts)
    if any(part in _SENSITIVE_SEGMENTS for part in folded_parts):
        return True
    leaf = folded_parts[-1] if folded_parts else ""
    if (
        leaf in _SENSITIVE_NAMES
        or leaf.startswith(".env.")
        or PureWindowsPath(leaf).suffix.casefold() in _SENSITIVE_SUFFIXES
    ):
        return True
    normalized = "/".join(folded_parts)
    if normalized == "data/log" or normalized.startswith("data/log"):
        return True
    return any(
        normalized == root or normalized.startswith(root + "/")
        for root in _SENSITIVE_RELATIVE_ROOTS
    )


@dataclass(frozen=True, slots=True)
class MutationApprovalDecision:
    requires_approval: bool
    reason: str


def route_mutation_approval(
    mode: PermissionMode,
    paths: tuple[str, ...],
    *,
    project_root: Path,
    unknown_target: bool = False,
) -> MutationApprovalDecision:
    """Only the trusted host supplies mode; model arguments only supply paths."""
    validated = validate_permission_mode(mode)
    if validated == "full-access":
        return MutationApprovalDecision(False, "full-access")
    if unknown_target or not paths:
        return MutationApprovalDecision(True, "unknown mutation target")
    if any(is_sensitive_mutation_path(path, project_root=project_root) for path in paths):
        return MutationApprovalDecision(True, "sensitive path")
    return MutationApprovalDecision(False, "ordinary project path")
