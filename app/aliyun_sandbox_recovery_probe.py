"""阿里云 Sandbox 跨实例 OSS 检查点恢复探针。

阶段 2 只使用 E2B Filesystem API。OSS 仅保存检查点，活跃工作区始终位于
各 Sandbox 自己的 ``/workspace`` 下。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.aliyun_sandbox_probe import _create_sandbox, build_oss_metadata
from app.sandbox_config import (
    SandboxProbeDisabledError,
    SandboxSettings,
    load_sandbox_settings,
)


_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES: Final[int] = 64 * 1024
_MAX_FILE_BYTES: Final[int] = 1024 * 1024
_MAX_FILES: Final[int] = 32
_WORKSPACE_FILES: Final[Mapping[str, bytes]] = {
    "README.md": "# Sandbox recovery probe\n\nLocal workspace only.\n".encode(
        "utf-8"
    ),
    "config/settings.json": b'{"mode":"recovery-probe","version":1}\n',
    "src/main.py": b'def answer() -> int:\n    return 42\n',
}


class SandboxRecoveryProbeError(RuntimeError):
    """恢复探针检测到可安全展示的协议或校验失败。"""


def _validated_relative_path(value: str) -> str:
    if (
        not value
        or value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
        or posixpath.normpath(value) != value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("manifest 文件路径必须是规范化的安全相对 POSIX 路径。")
    return value


def _join_under(root: str, relative_path: str) -> str:
    safe_relative = _validated_relative_path(relative_path)
    candidate = posixpath.normpath(f"{root}/{safe_relative}")
    if not candidate.startswith(root.rstrip("/") + "/"):
        raise SandboxRecoveryProbeError("恢复路径越出受控根目录。")
    return candidate


class CheckpointFileEntry(BaseModel):
    """检查点中一个文件的无内容清单项。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    path: StrictStr
    size: StrictInt = Field(ge=0, le=_MAX_FILE_BYTES)
    sha256: StrictStr

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validated_relative_path(value)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("sha256 必须是 64 位小写十六进制文本。")
        return value


class CheckpointManifest(BaseModel):
    """版本化、可严格验证的 OSS 检查点清单。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal[1]
    probe_id: StrictStr
    files: tuple[CheckpointFileEntry, ...] = Field(
        min_length=1,
        max_length=_MAX_FILES,
    )

    @field_validator("probe_id")
    @classmethod
    def validate_probe_id(cls, value: str) -> str:
        if not _TOKEN_RE.fullmatch(value):
            raise ValueError("probe_id 必须是 32 位小写十六进制文本。")
        return value

    @model_validator(mode="after")
    def validate_file_order(self) -> "CheckpointManifest":
        paths = [entry.path for entry in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("manifest 文件路径必须唯一并按字典序排列。")
        return self


class SandboxRecoveryProbeResult(BaseModel):
    """不含文件正文、API Key 或 Role 凭据的阶段 2 结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_sandbox_id: StrictStr
    restored_sandbox_id: StrictStr
    checkpoint_path: StrictStr
    restored_workspace_path: StrictStr
    file_count: StrictInt = Field(ge=1, le=_MAX_FILES)
    total_bytes: StrictInt = Field(ge=0)
    source_killed_before_restore: StrictBool
    checkpoint_removed: StrictBool
    restored_sandbox_killed: StrictBool


def _strict_json_object(raw: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"非法 JSON 常量：{value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON object 包含重复字段。")
            result[key] = value
        return result

    parsed = json.loads(
        raw,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(parsed, dict):
        raise ValueError("manifest 顶层必须是 JSON object。")
    return parsed


def parse_checkpoint_manifest(raw: str) -> CheckpointManifest:
    """解析来自 OSS 的有界严格 JSON，不回显不可信正文。"""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        raise SandboxRecoveryProbeError("checkpoint manifest 类型或大小无效。")
    try:
        return CheckpointManifest.model_validate(_strict_json_object(raw))
    except Exception as error:
        raise SandboxRecoveryProbeError("checkpoint manifest 严格校验失败。") from error


def _manifest_json(manifest: CheckpointManifest) -> str:
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _read_bytes(filesystem: Any, path: str) -> bytes:
    value = filesystem.read(path, format="bytes")
    if not isinstance(value, (bytes, bytearray)):
        raise SandboxRecoveryProbeError("Filesystem API 未返回 bytes。")
    data = bytes(value)
    if len(data) > _MAX_FILE_BYTES:
        raise SandboxRecoveryProbeError("检查点文件超过阶段 2 大小上限。")
    return data


def _file_entry(path: str, data: bytes) -> CheckpointFileEntry:
    return CheckpointFileEntry(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _verify_bytes(entry: CheckpointFileEntry, data: bytes) -> None:
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise SandboxRecoveryProbeError(
            "checkpoint 文件的 size 或 sha256 与 manifest 不一致。"
        )


def _build_checkpoint_paths(
    checkpoint_root: str,
    relative_paths: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    file_root = f"{checkpoint_root}/files"
    file_paths = tuple(
        _join_under(file_root, relative_path) for relative_path in relative_paths
    )
    directories = {file_root, checkpoint_root}
    for relative_path in relative_paths:
        parent = posixpath.dirname(relative_path)
        while parent:
            directories.add(_join_under(file_root, parent))
            parent = posixpath.dirname(parent)
    ordered_directories = tuple(
        sorted(directories, key=lambda item: (item.count("/"), item), reverse=True)
    )
    return file_paths, ordered_directories


def _cleanup_exact_paths(
    filesystem: Any,
    file_paths: Sequence[str],
    directory_paths: Sequence[str],
) -> list[BaseException]:
    """通过 Filesystem API 逐项清理已知路径，不执行 shell 或宽泛删除。"""
    errors: list[BaseException] = []
    for path in (*file_paths, *directory_paths):
        try:
            if filesystem.exists(path):
                filesystem.remove(path)
        except BaseException as error:
            errors.append(error)
    return errors


def _kill_sandbox(sandbox: Any) -> None:
    result = sandbox.kill()
    if result is False:
        raise SandboxRecoveryProbeError("Sandbox kill() 返回失败状态。")


def run_recovery_probe(
    settings: SandboxSettings,
    *,
    sandbox_factory: Callable[[SandboxSettings, dict[str, str]], Any] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> SandboxRecoveryProbeResult:
    """证明 OSS 检查点可在 A 销毁后恢复到 B 的本地工作区。"""
    if not isinstance(settings, SandboxSettings):
        raise TypeError("settings 必须是 SandboxSettings 实例。")
    settings.require_recovery_enabled()
    metadata = build_oss_metadata(settings)
    factory = sandbox_factory or _create_sandbox

    token = (token_factory or (lambda: uuid.uuid4().hex))()
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ValueError("recovery probe token 必须是 32 位小写十六进制文本。")

    checkpoint_root = (
        f"{settings.oss_mount_dir}/.agnet-study-recovery-probe-{token}"
    )
    source_root = f"/workspace/.agnet-study-recovery-source-{token}"
    # Keep the restored workspace in a probe-owned directory whose name is
    # intentionally distinct from the source workspace.  The short
    # ``restored-`` prefix also makes the resulting path easy to audit in
    # offline fakes and cloud-side diagnostics without exposing any config.
    restored_root = f"/workspace/restored-{token}"
    relative_paths = tuple(sorted(_WORKSPACE_FILES))
    checkpoint_file_paths, checkpoint_directories = _build_checkpoint_paths(
        checkpoint_root,
        relative_paths,
    )
    checkpoint_manifest_path = f"{checkpoint_root}/manifest.json"
    cleanup_file_paths = (*checkpoint_file_paths, checkpoint_manifest_path)

    source: Any | None = None
    restored: Any | None = None
    source_killed = False
    restored_killed = False
    restore_create_attempted = False
    checkpoint_started = False
    checkpoint_removed = False
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    result: SandboxRecoveryProbeResult | None = None

    try:
        source = factory(settings, metadata)
        source_id = getattr(source, "sandbox_id", None)
        if not isinstance(source_id, str) or not source_id:
            raise SandboxRecoveryProbeError("Sandbox A 缺少 sandbox_id。")

        source_bytes: dict[str, bytes] = {}
        for relative_path in relative_paths:
            intended = _WORKSPACE_FILES[relative_path]
            local_path = _join_under(source_root, relative_path)
            source.files.write(local_path, intended)
            observed = _read_bytes(source.files, local_path)
            if observed != intended:
                raise SandboxRecoveryProbeError("Sandbox A 本地工作区写入校验失败。")
            source_bytes[relative_path] = observed

        manifest = CheckpointManifest(
            schema_version=1,
            probe_id=token,
            files=tuple(
                _file_entry(path, source_bytes[path]) for path in relative_paths
            ),
        )
        checkpoint_started = True
        for entry, checkpoint_path in zip(
            manifest.files,
            checkpoint_file_paths,
            strict=True,
        ):
            source.files.write(checkpoint_path, source_bytes[entry.path])
        source.files.write(checkpoint_manifest_path, _manifest_json(manifest))

        # 这是阶段 2 的关键顺序边界：A 的 kill 成功后才允许创建 B。
        _kill_sandbox(source)
        source_killed = True

        restore_create_attempted = True
        restored = factory(settings, metadata)
        restored_id = getattr(restored, "sandbox_id", None)
        if not isinstance(restored_id, str) or not restored_id:
            raise SandboxRecoveryProbeError("Sandbox B 缺少 sandbox_id。")

        raw_manifest = restored.files.read(checkpoint_manifest_path)
        recovered_manifest = parse_checkpoint_manifest(raw_manifest)
        if recovered_manifest.probe_id != token:
            raise SandboxRecoveryProbeError("checkpoint manifest 的 probe_id 不匹配。")
        if recovered_manifest != manifest:
            raise SandboxRecoveryProbeError(
                "Sandbox B 读取的 manifest 与 Sandbox A 生成的原始 manifest 不一致。"
            )

        total_bytes = 0
        for entry in recovered_manifest.files:
            checkpoint_path = _join_under(
                f"{checkpoint_root}/files",
                entry.path,
            )
            checkpoint_data = _read_bytes(restored.files, checkpoint_path)
            _verify_bytes(entry, checkpoint_data)

            restored_path = _join_under(restored_root, entry.path)
            restored.files.write(restored_path, checkpoint_data)
            restored_data = _read_bytes(restored.files, restored_path)
            _verify_bytes(entry, restored_data)
            if restored_data != checkpoint_data:
                raise SandboxRecoveryProbeError("Sandbox B 恢复后的文件正文不一致。")
            total_bytes += len(restored_data)

        cleanup_errors = _cleanup_exact_paths(
            restored.files,
            cleanup_file_paths,
            checkpoint_directories,
        )
        if cleanup_errors:
            raise SandboxRecoveryProbeError("OSS 检查点清理未完整成功。")
        checkpoint_removed = True

        _kill_sandbox(restored)
        restored_killed = True
        result = SandboxRecoveryProbeResult(
            source_sandbox_id=source_id,
            restored_sandbox_id=restored_id,
            checkpoint_path=checkpoint_root,
            restored_workspace_path=restored_root,
            file_count=len(recovered_manifest.files),
            total_bytes=total_bytes,
            source_killed_before_restore=True,
            checkpoint_removed=True,
            restored_sandbox_killed=True,
        )
    except BaseException as error:
        primary_error = error
    finally:
        cleanup_sandbox: Any | None = None
        cleanup_sandbox_create_error: BaseException | None = None
        if checkpoint_started and not checkpoint_removed:
            cleanup_host = restored
            if cleanup_host is None and source is not None and not source_killed:
                cleanup_host = source
            if (
                cleanup_host is None
                and source_killed
                and restore_create_attempted
            ):
                # B 的 create 本身失败时，A 已按协议销毁。只进行一次有界 C
                # 创建尝试，用相同挂载删除精确已知路径；绝不无限重试。
                try:
                    cleanup_sandbox = factory(settings, metadata)
                    cleanup_host = cleanup_sandbox
                except BaseException as error:
                    cleanup_sandbox_create_error = error
            if cleanup_host is not None:
                final_cleanup_errors = _cleanup_exact_paths(
                    cleanup_host.files,
                    cleanup_file_paths,
                    checkpoint_directories,
                )
                cleanup_errors.extend(final_cleanup_errors)
                if not final_cleanup_errors:
                    checkpoint_removed = True

        kill_errors: list[BaseException] = []
        if restored is not None and not restored_killed:
            try:
                _kill_sandbox(restored)
                restored_killed = True
            except BaseException as error:
                kill_errors.append(error)
        if cleanup_sandbox is not None:
            try:
                _kill_sandbox(cleanup_sandbox)
            except BaseException as error:
                kill_errors.append(error)
        if source is not None and not source_killed:
            try:
                _kill_sandbox(source)
                source_killed = True
            except BaseException as error:
                kill_errors.append(error)

        if checkpoint_started and not checkpoint_removed:
            # The checkpoint is safety-sensitive: when exact deletion was not
            # confirmed, always replace any cloud/SDK exception with a local
            # error that tells the operator which bounded path may remain.
            # Keep the original only as an in-process cause and type-only notes
            # so the CLI cannot expose request URLs, headers, or credentials.
            residual_error = SandboxRecoveryProbeError(
                "OSS checkpoint_path="
                f"{checkpoint_root} 可能残留，未能确认已精确删除。"
            )
            if primary_error is not None:
                residual_error.add_note(
                    f"原始失败类型：{type(primary_error).__name__}。"
                )
                residual_error.__cause__ = primary_error
            if cleanup_sandbox_create_error is not None:
                residual_error.add_note(
                    "清理 Sandbox 创建失败类型："
                    f"{type(cleanup_sandbox_create_error).__name__}。"
                )
            if cleanup_errors:
                residual_error.add_note(
                    f"精确路径清理失败数量：{len(cleanup_errors)}。"
                )
            if kill_errors:
                residual_error.add_note(
                    f"Sandbox 销毁未确认数量：{len(kill_errors)}。"
                )
            primary_error = residual_error
        elif primary_error is not None:
            if cleanup_errors:
                primary_error.add_note(
                    f"异常清理中有 {len(cleanup_errors)} 个精确路径未能删除。"
                )
            if kill_errors:
                primary_error.add_note(
                    f"异常清理中有 {len(kill_errors)} 个 Sandbox 未能确认销毁。"
                )
        elif cleanup_errors:
            primary_error = SandboxRecoveryProbeError(
                "OSS 检查点清理未完整成功。"
            )
            if kill_errors:
                primary_error.add_note(
                    f"同时有 {len(kill_errors)} 个 Sandbox 未能确认销毁。"
                )
        elif kill_errors:
            primary_error = SandboxRecoveryProbeError(
                "Sandbox 生命周期清理未完整成功。"
            )

    if primary_error is not None:
        raise primary_error
    if result is None:
        raise SandboxRecoveryProbeError("恢复探针未返回结果。")
    return result


def _safe_error_text(
    error: Exception,
    settings: SandboxSettings | None,
) -> str:
    safe_local = isinstance(
        error,
        (SandboxProbeDisabledError, SandboxRecoveryProbeError),
    ) or (settings is None and isinstance(error, (ValueError, TypeError)))
    if not safe_local:
        return "云端请求或第三方 SDK 调用失败；请检查接入点、模板、RAM Role 与 OSS 权限。"
    return (" ".join(str(error).split())[:1000] or "恢复探针本地校验失败。")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式验证 OSS 检查点可在两个阿里云 Sandbox 之间恢复。"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="创建 A、写检查点、销毁 A、创建 B、恢复校验、清理并销毁 B。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print("未执行真实云恢复探针。确认配置后请显式追加 --execute。")
        return 0

    settings: SandboxSettings | None = None
    try:
        settings = load_sandbox_settings()
        result = run_recovery_probe(settings)
    except Exception as error:
        print(
            "阿里云 Sandbox 恢复验证失败"
            f"（{type(error).__name__}）：{_safe_error_text(error, settings)}",
            file=sys.stderr,
        )
        return 1

    print(
        "阿里云 Sandbox 跨实例 OSS 恢复验证成功；"
        f"files={result.file_count}，bytes={result.total_bytes}，"
        "检查点已删除，Sandbox A/B 已销毁。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
