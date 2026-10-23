"""外置工具白名单的严格加载与统一路径判定。

策略文件只决定可以进一步收紧的项目路径和命令集合。代码内的敏感路径底线
不可由 JSON 配置放宽；策略缺失、被链接、具有多重硬链接或内容无效时一律拒绝。
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.security_domain import path_is_sealed_secret
from tool.path_safety import is_windows_reserved


_POLICY_FILE = Path(__file__).resolve().parent.parent / ".agent" / "tool_policy.json"
_MAX_POLICY_BYTES = 64 * 1024

# 即使策略文件被错误配置，这些边界也不能被关闭。比较统一使用 casefold，
# 从而在 Windows 和大小写敏感平台上都不会把 .ENV 当作安全文件。
_INTRINSIC_PROTECTED_SEGMENTS = frozenset({".agent", ".git", ".venv"})
_INTRINSIC_PROTECTED_FILE_NAMES = frozenset({".env"})
_INTRINSIC_PROTECTED_FILE_PREFIXES = (".env.",)
_INTRINSIC_PROTECTED_RELATIVE_PATHS = (
    "data/pending", "data/sessions", "code.md", "skills"
)


class PolicyLoadError(RuntimeError):
    """策略不可验证；调用方必须 fail closed。"""


class _StrictPolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FileAccessPolicy(_StrictPolicyModel):
    allowed_path_patterns: list[str] = Field(min_length=1, max_length=64)
    protected_path_segments: list[str] = Field(max_length=64)
    protected_file_names: list[str] = Field(max_length=64)
    protected_file_prefixes: list[str] = Field(max_length=64)
    protected_relative_paths: list[str] = Field(default_factory=list, max_length=64)

    @field_validator(
        "allowed_path_patterns",
        "protected_path_segments",
        "protected_file_names",
        "protected_file_prefixes",
        "protected_relative_paths",
    )
    @classmethod
    def validate_nonblank_unique_values(cls, values: list[str]) -> list[str]:
        if any(not value or not value.strip() or "\x00" in value for value in values):
            raise ValueError("策略条目不能为空或包含 NUL")
        folded = [value.casefold() for value in values]
        if len(folded) != len(set(folded)):
            raise ValueError("策略条目不能重复")
        return values

    @field_validator("protected_relative_paths")
    @classmethod
    def validate_protected_relative_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            parts = value.split("/")
            windows_path = PureWindowsPath(value)
            if (
                "\\" in value
                or windows_path.drive
                or windows_path.root
                or value.startswith("/")
                or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
                or any(character in value for character in ':*?<>|"')
                or any(ord(character) < 32 for character in value)
                or is_windows_reserved(value)
                or len(value.encode("utf-8")) > 512
            ):
                raise ValueError("受保护相对路径必须使用规范 POSIX 相对路径")
        return values


class ShellCommandPolicy(_StrictPolicyModel):
    enabled: bool
    requires_confirmation: bool
    max_paths: int = Field(ge=1, le=8)


class CodeInterpreterPolicy(_StrictPolicyModel):
    enabled: bool
    max_ast_nodes: int = Field(ge=32, le=512)
    max_timeout_seconds: int = Field(ge=1, le=10)
    max_output_characters: int = Field(ge=1024, le=16384)
    max_memory_mb: int = Field(ge=64, le=512)
    # Windows 的 venv launcher 会短暂保留自身并启动基础解释器，因此上限 2
    # 才能启动一个 Python 工作进程；AST 能力层仍完全不暴露创建子进程的入口。
    max_processes: int = Field(ge=1, le=2)


class NetworkPolicy(_StrictPolicyModel):
    fetch_enabled: bool
    search_enabled: bool
    allow_http: bool
    max_response_bytes: int = Field(ge=1024, le=4 * 1024 * 1024)
    max_timeout_seconds: int = Field(ge=1, le=60)
    max_redirects: int = Field(ge=0, le=3)
    search_max_response_bytes: int = Field(ge=1024, le=2 * 1024 * 1024)
    search_max_timeout_seconds: int = Field(ge=1, le=60)


class ToolPolicy(_StrictPolicyModel):
    version: Literal[5]
    file_access: FileAccessPolicy
    shell_commands: dict[str, ShellCommandPolicy]
    code_interpreter: CodeInterpreterPolicy
    network: NetworkPolicy

    @model_validator(mode="after")
    def validate_command_names(self) -> "ToolPolicy":
        if not self.shell_commands:
            raise ValueError("shell 命令白名单不能为空")
        for name in self.shell_commands:
            if not name or not name.replace("_", "").isalnum() or name.casefold() != name:
                raise ValueError("shell 命令名称无效")
        return self


def load_tool_policy() -> ToolPolicy:
    """从固定位置加载并完整校验策略；任何异常都归一化为 PolicyLoadError。"""
    try:
        parent_info = os.lstat(_POLICY_FILE.parent)
        parent_attributes = getattr(parent_info, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or parent_attributes & 0x400
        ):
            raise ValueError("策略目录状态不安全")
        before = os.lstat(_POLICY_FILE)
        attributes = getattr(before, "st_file_attributes", 0)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or attributes & 0x400
            or before.st_nlink != 1
            or before.st_size > _MAX_POLICY_BYTES
        ):
            raise ValueError("策略文件状态不安全")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(_POLICY_FILE, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                (before.st_dev, before.st_ino, before.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
                or opened.st_nlink != 1
                or not stat.S_ISREG(opened.st_mode)
            ):
                raise ValueError("打开的策略文件身份不一致")
            chunks: list[bytes] = []
            remaining = opened.st_size + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        after = os.lstat(_POLICY_FILE)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or len(raw) != before.st_size
        ):
            raise ValueError("策略文件在读取期间发生变化")
        data = json.loads(raw.decode("utf-8"))
        return ToolPolicy.model_validate(data)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
        raise PolicyLoadError("工具权限策略不可用。") from None


def path_is_protected(relative_path: str, policy: ToolPolicy) -> bool:
    """判断规范 POSIX 相对路径是否属于内置或配置的敏感范围。"""
    parts = PurePosixPath(relative_path).parts
    folded_parts = tuple(part.casefold() for part in parts)
    configured_segments = {
        value.casefold() for value in policy.file_access.protected_path_segments
    }
    if any(
        part in _INTRINSIC_PROTECTED_SEGMENTS or part in configured_segments
        for part in folded_parts
    ):
        return True

    leaf = folded_parts[-1] if folded_parts else ""
    protected_names = _INTRINSIC_PROTECTED_FILE_NAMES | {
        value.casefold() for value in policy.file_access.protected_file_names
    }
    protected_prefixes = _INTRINSIC_PROTECTED_FILE_PREFIXES + tuple(
        value.casefold() for value in policy.file_access.protected_file_prefixes
    )
    if leaf in protected_names or any(leaf.startswith(prefix) for prefix in protected_prefixes):
        return True

    normalized = "/".join(folded_parts)
    protected_relative_paths = _INTRINSIC_PROTECTED_RELATIVE_PATHS + tuple(
        value.casefold()
        for value in policy.file_access.protected_relative_paths
    )
    if any(
        normalized == protected or normalized.startswith(protected + "/")
        for protected in protected_relative_paths
    ):
        return True

    return False


def path_matches_allowed_pattern(relative_path: str, policy: ToolPolicy) -> bool:
    """判断规范 POSIX 相对路径是否位于配置的项目白名单。"""
    path = PurePosixPath(relative_path)
    return any(
        path.full_match(pattern, case_sensitive=False)
        for pattern in policy.file_access.allowed_path_patterns
    )


def path_is_allowed(relative_path: str, policy: ToolPolicy) -> bool:
    """判断规范 POSIX 相对路径是否同时通过白名单和敏感路径底线。"""
    if path_is_sealed_secret(relative_path) or path_is_protected(relative_path, policy):
        return False

    return path_matches_allowed_pattern(relative_path, policy)


def require_allowed_relative_path(
    relative_path: str, *, allow_protected: bool = False
) -> ToolPolicy:
    """加载最新策略并要求路径在白名单内；策略失败也表现为权限拒绝。"""
    policy = load_tool_policy()
    if (
        path_is_sealed_secret(relative_path)
        or not path_matches_allowed_pattern(relative_path, policy)
        or (not allow_protected and path_is_protected(relative_path, policy))
    ):
        raise PolicyLoadError("路径不在工具白名单内。")
    return policy
