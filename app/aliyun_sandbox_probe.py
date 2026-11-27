"""阿里云 Agent Sandbox 极速版与 OSS 动态挂载的最小连通性探针。"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from app.sandbox_config import (
    SandboxProbeDisabledError,
    SandboxSettings,
    load_sandbox_settings,
)


_MARKER_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")


class SandboxProbeError(RuntimeError):
    """探针自身检测到安全且可向操作者展示的失败。"""


class SandboxProbeResult(BaseModel):
    """不含密钥和 marker 正文的安全探针结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_id: StrictStr
    marker_path: StrictStr
    marker_removed: StrictBool


def build_oss_metadata(settings: SandboxSettings) -> dict[str, str]:
    """构造阿里云要求的 metadata；OSS 配置值必须是 JSON 字符串。"""
    if not isinstance(settings, SandboxSettings):
        raise TypeError("settings 必须是 SandboxSettings 实例。")
    settings.require_enabled()
    assert settings.oss_bucket is not None
    assert settings.oss_endpoint is not None
    assert settings.oss_bucket_path is not None
    assert settings.oss_role_arn is not None

    storage_config = {
        "mountPoints": [
            {
                "bucketName": settings.oss_bucket,
                "mountDir": settings.oss_mount_dir,
                "bucketPath": settings.oss_bucket_path,
                "endpoint": settings.oss_endpoint,
                "readOnly": settings.oss_read_only,
            }
        ]
    }
    return {
        "fc.sandbox.storage.oss": json.dumps(
            storage_config,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "fc.sandbox.auth.role": settings.oss_role_arn,
    }


def _create_sandbox(settings: SandboxSettings, metadata: dict[str, str]) -> Any:
    """延迟导入 E2B SDK，确保普通 Agent 启动不承担依赖和云调用成本。"""
    from e2b import Sandbox

    assert settings.api_key is not None
    assert settings.api_url is not None
    assert settings.domain is not None
    options: dict[str, Any] = {
        "api_key": settings.api_key.get_secret_value(),
        "api_url": settings.api_url,
        "domain": settings.domain,
        "timeout": settings.timeout_seconds,
        "request_timeout": 10,
        "lifecycle": {"on_timeout": "kill", "auto_resume": False},
        "metadata": metadata,
        "allow_internet_access": settings.allow_internet_access,
    }
    if settings.template is not None:
        options["template"] = settings.template
    return Sandbox.create(**options)


def run_connectivity_probe(
    settings: SandboxSettings,
    *,
    sandbox_factory: Callable[[SandboxSettings, dict[str, str]], Any] | None = None,
    token_factory: Callable[[], str] | None = None,
) -> SandboxProbeResult:
    """创建沙箱、写读删 OSS marker，并且无论成功失败都销毁沙箱。"""
    if not isinstance(settings, SandboxSettings):
        raise TypeError("settings 必须是 SandboxSettings 实例。")
    settings.require_enabled()
    metadata = build_oss_metadata(settings)
    factory = sandbox_factory or _create_sandbox

    token = (token_factory or (lambda: uuid.uuid4().hex))()
    if not isinstance(token, str) or not _MARKER_TOKEN_RE.fullmatch(token):
        raise ValueError("marker token 必须是 32 位小写十六进制文本。")
    marker_path = f"{settings.oss_mount_dir}/.agnet-study-probe-{token}.txt"
    marker_body = f"agnet-study aliyun sandbox probe {token}\n"

    sandbox: Any | None = None
    write_attempted = False
    marker_removed = False
    result: SandboxProbeResult | None = None
    primary_error: BaseException | None = None
    try:
        sandbox = factory(settings, metadata)
        sandbox_id = getattr(sandbox, "sandbox_id", None)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise SandboxProbeError("Sandbox 创建结果缺少 sandbox_id。")

        # 远端可能已经完成写入，但本地因响应丢失收到异常。必须在发出请求前
        # 记录尝试状态，让 finally 对这个不确定窗口执行幂等、尽力清理。
        write_attempted = True
        sandbox.files.write(marker_path, marker_body)
        observed = sandbox.files.read(marker_path)
        if not isinstance(observed, str) or not secrets.compare_digest(
            observed, marker_body
        ):
            raise SandboxProbeError("OSS marker 回读内容与写入内容不一致。")
        sandbox.files.remove(marker_path)
        marker_removed = True
        result = SandboxProbeResult(
            sandbox_id=sandbox_id,
            marker_path=marker_path,
            marker_removed=True,
        )
    except BaseException as error:
        primary_error = error
    finally:
        if sandbox is not None:
            cleanup_error: BaseException | None = None
            kill_error: BaseException | None = None
            if write_attempted and not marker_removed:
                try:
                    sandbox.files.remove(marker_path)
                except BaseException as error:
                    cleanup_error = error
            try:
                kill_result = sandbox.kill()
                if kill_result is False:
                    kill_error = SandboxProbeError("Sandbox kill() 返回失败状态。")
            except BaseException as error:
                kill_error = error

            if primary_error is not None:
                if cleanup_error is not None:
                    primary_error.add_note(
                        "失败后的 OSS marker 清理也失败："
                        f"{type(cleanup_error).__name__}。"
                    )
                if kill_error is not None:
                    primary_error.add_note(
                        "失败后的 Sandbox 销毁也失败："
                        f"{type(kill_error).__name__}。"
                    )
            elif cleanup_error is not None:
                primary_error = cleanup_error
                if kill_error is not None:
                    primary_error.add_note(
                        "Sandbox 销毁也失败：" f"{type(kill_error).__name__}。"
                    )
            elif kill_error is not None:
                primary_error = kill_error

    if primary_error is not None:
        raise primary_error
    if result is None:
        raise SandboxProbeError("Sandbox 探针未返回结果。")
    return result


def _safe_error_text(
    error: Exception,
    settings: SandboxSettings | None,
) -> str:
    """仅显示可信本地错误；第三方异常可能包含请求上下文，统一收敛。"""
    safe_local_error = isinstance(
        error,
        (SandboxProbeDisabledError, SandboxProbeError),
    ) or (settings is None and isinstance(error, (ValueError, TypeError)))
    if not safe_local_error:
        return "云端请求或第三方 SDK 调用失败；请检查接入点、模板、RAM Role 与 OSS 权限。"

    message = " ".join(str(error).split())
    if settings is not None and settings.api_key is not None:
        message = message.replace(
            settings.api_key.get_secret_value(),
            "[REDACTED]",
        )
    return message[:1000] or "本地配置校验失败。"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式验证阿里云 Agent Sandbox 极速版与 OSS 动态挂载。"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="确认创建真实云沙箱、写读删一个 OSS marker，并立即销毁沙箱。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print("未执行真实云调用。确认配置后请显式追加 --execute。")
        return 0

    settings: SandboxSettings | None = None
    try:
        settings = load_sandbox_settings()
        result = run_connectivity_probe(settings)
    except Exception as error:
        print(
            "阿里云 Sandbox 连通性验证失败"
            f"（{type(error).__name__}）：{_safe_error_text(error, settings)}",
            file=sys.stderr,
        )
        return 1

    print(
        "阿里云 Sandbox 与 OSS 动态挂载读写验证成功；"
        f"sandbox_id={result.sandbox_id}，marker 已删除，Sandbox 已销毁。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
