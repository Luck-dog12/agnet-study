"""阿里云 Agent Sandbox 极速版连通性探针配置。

该模块只读取配置并做本地校验，不导入 E2B SDK，也不会创建云端资源。
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)


_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_ALIYUN_HOST_SUFFIX: Final[str] = ".aliyuncs.com"
_OSS_BUCKET_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\Z"
)
_DOMAIN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_ROLE_ARN_RE: Final[re.Pattern[str]] = re.compile(
    r"acs:ram::[0-9]{12,20}:role/[A-Za-z0-9+=,.@_-]{1,64}\Z"
)

SANDBOX_ENV_NAMES: Final[tuple[str, ...]] = (
    "ALIYUN_SANDBOX_PROBE_ENABLED",
    "ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED",
    "ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS",
    "E2B_API_KEY",
    "E2B_API_URL",
    "E2B_DOMAIN",
    "E2B_TEMPLATE",
    "ALIYUN_SANDBOX_TIMEOUT_SECONDS",
    "ALIYUN_SANDBOX_IDLE_TIMEOUT_SECONDS",
    "ALIYUN_SANDBOX_MAX_SESSION_SECONDS",
    "ALIYUN_SANDBOX_RUNTIME_VENV_DIR",
    "ALIYUN_SANDBOX_RUNTIME_USER",
    "ALIYUN_OSS_BUCKET",
    "ALIYUN_OSS_ENDPOINT",
    "ALIYUN_OSS_BUCKET_PATH",
    "ALIYUN_OSS_MOUNT_DIR",
    "ALIYUN_OSS_ROLE_ARN",
    "ALIYUN_OSS_READ_ONLY",
)


class SandboxProbeDisabledError(RuntimeError):
    """探针未被操作者显式启用。"""


def _validated_http_url(value: str, *, field_name: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in value)
            and parsed.path in {"", "/"}
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{field_name} 必须是无凭据、查询参数和路径的 HTTP(S) 地址。")
    return value.rstrip("/")


def _validated_absolute_posix_path(
    value: str,
    *,
    field_name: str,
    allow_root: bool,
) -> str:
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or (not allow_root and value == "/")
        or posixpath.normpath(value) != value
    ):
        raise ValueError(f"{field_name} 必须是规范化的绝对 Unix 路径。")
    return value


class SandboxSettings(BaseModel):
    """严格的探针配置；关闭时允许云端字段留空。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        validate_default=True,
    )

    enabled: StrictBool = False
    recovery_probe_enabled: StrictBool = False
    allow_internet_access: StrictBool = False
    api_key: SecretStr | None = Field(default=None, repr=False)
    api_url: StrictStr | None = None
    domain: StrictStr | None = None
    template: StrictStr | None = None
    timeout_seconds: StrictInt = 300
    idle_timeout_seconds: StrictInt = 120
    max_session_seconds: StrictInt = 3600
    runtime_venv_dir: StrictStr = "/opt/agnet-study/runtime/venv"
    runtime_user: StrictStr = "user"
    oss_bucket: StrictStr | None = None
    oss_endpoint: StrictStr | None = None
    oss_bucket_path: StrictStr | None = None
    oss_mount_dir: StrictStr = "/mnt/oss"
    oss_role_arn: StrictStr | None = None
    oss_read_only: StrictBool = False

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if (
            not raw
            or raw != raw.strip()
            or len(raw) > 4096
            or any(ord(character) < 32 for character in raw)
        ):
            raise ValueError("E2B_API_KEY 必须是非空且不含首尾空白或控制字符的文本。")
        return value

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _validated_http_url(value, field_name="E2B_API_URL")
        hostname = urlsplit(normalized).hostname or ""
        if not hostname.lower().endswith(_ALIYUN_HOST_SUFFIX):
            raise ValueError("E2B_API_URL 必须指向阿里云 aliyuncs.com 接入点。")
        return normalized

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _DOMAIN_RE.fullmatch(value) or not value.lower().endswith(
            _ALIYUN_HOST_SUFFIX
        ):
            raise ValueError("E2B_DOMAIN 必须是阿里云 aliyuncs.com 域名后缀。")
        return value.lower()

    @field_validator("template")
    @classmethod
    def validate_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value
            or len(value) > 200
            or any(character.isspace() or ord(character) < 32 for character in value)
        ):
            raise ValueError("E2B_TEMPLATE 必须是非空且不含空白或控制字符的模板名。")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("ALIYUN_SANDBOX_TIMEOUT_SECONDS 必须在 30..3600 之间。")
        return value

    @field_validator("idle_timeout_seconds")
    @classmethod
    def validate_idle_timeout(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("ALIYUN_SANDBOX_IDLE_TIMEOUT_SECONDS 必须在 30..3600 之间。")
        return value

    @field_validator("max_session_seconds")
    @classmethod
    def validate_max_session(cls, value: int) -> int:
        if not 60 <= value <= 86400:
            raise ValueError("ALIYUN_SANDBOX_MAX_SESSION_SECONDS 必须在 60..86400 之间。")
        return value

    @field_validator("runtime_venv_dir")
    @classmethod
    def validate_runtime_venv_dir(cls, value: str) -> str:
        path = _validated_absolute_posix_path(
            value,
            field_name="ALIYUN_SANDBOX_RUNTIME_VENV_DIR",
            allow_root=False,
        )
        if (
            path == "/workspace"
            or path.startswith("/workspace/")
            or path == "/mnt"
            or path.startswith("/mnt/")
            or len(path.encode("utf-8")) > 1024
        ):
            raise ValueError(
                "ALIYUN_SANDBOX_RUNTIME_VENV_DIR 必须位于 workspace 和 OSS 挂载点之外。"
            )
        return path

    @field_validator("runtime_user")
    @classmethod
    def validate_runtime_user(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value)
            or value in {"root", "0"}
        ):
            raise ValueError("ALIYUN_SANDBOX_RUNTIME_USER 必须是固定非特权 Unix 用户名。")
        return value

    @field_validator("oss_bucket")
    @classmethod
    def validate_oss_bucket(cls, value: str | None) -> str | None:
        if value is not None and not _OSS_BUCKET_RE.fullmatch(value):
            raise ValueError("ALIYUN_OSS_BUCKET 必须是合法的 OSS Bucket 名称。")
        return value

    @field_validator("oss_endpoint")
    @classmethod
    def validate_oss_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _validated_http_url(value, field_name="ALIYUN_OSS_ENDPOINT")
        hostname = urlsplit(normalized).hostname or ""
        if not hostname.lower().endswith(_ALIYUN_HOST_SUFFIX):
            raise ValueError("ALIYUN_OSS_ENDPOINT 必须指向阿里云 aliyuncs.com 接入点。")
        return normalized

    @field_validator("oss_bucket_path")
    @classmethod
    def validate_oss_bucket_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = _validated_absolute_posix_path(
            value,
            field_name="ALIYUN_OSS_BUCKET_PATH",
            allow_root=False,
        )
        if len(path) > 1024:
            raise ValueError("ALIYUN_OSS_BUCKET_PATH 过长。")
        return path

    @field_validator("oss_mount_dir")
    @classmethod
    def validate_oss_mount_dir(cls, value: str) -> str:
        path = _validated_absolute_posix_path(
            value,
            field_name="ALIYUN_OSS_MOUNT_DIR",
            allow_root=False,
        )
        if not path.startswith("/mnt/"):
            raise ValueError("ALIYUN_OSS_MOUNT_DIR 必须位于 /mnt 下。")
        return path

    @field_validator("oss_role_arn")
    @classmethod
    def validate_oss_role_arn(cls, value: str | None) -> str | None:
        if value is not None and not _ROLE_ARN_RE.fullmatch(value):
            raise ValueError("ALIYUN_OSS_ROLE_ARN 必须是合法的 RAM Role ARN。")
        return value

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> "SandboxSettings":
        if self.recovery_probe_enabled and not self.enabled:
            raise ValueError(
                "ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED=true 要求先启用基础探针。"
            )
        if not self.enabled:
            return self

        required = {
            "E2B_API_KEY": self.api_key,
            "E2B_API_URL": self.api_url,
            "E2B_DOMAIN": self.domain,
            "ALIYUN_OSS_BUCKET": self.oss_bucket,
            "ALIYUN_OSS_ENDPOINT": self.oss_endpoint,
            "ALIYUN_OSS_BUCKET_PATH": self.oss_bucket_path,
            "ALIYUN_OSS_ROLE_ARN": self.oss_role_arn,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError("已启用沙箱探针，但缺少配置：" + ", ".join(missing))
        if self.oss_read_only:
            raise ValueError("读写连通性探针要求 ALIYUN_OSS_READ_ONLY=false。")

        assert self.api_url is not None
        assert self.domain is not None
        api_hostname = (urlsplit(self.api_url).hostname or "").lower()
        if api_hostname != f"api.{self.domain}":
            raise ValueError("E2B_API_URL 与 E2B_DOMAIN 不属于同一接入点。")

        assert self.oss_endpoint is not None
        region = self.domain.split(".", 1)[0]
        oss_hostname = (urlsplit(self.oss_endpoint).hostname or "").lower()
        expected_endpoint = re.compile(
            rf"(?:^|\.)oss-{re.escape(region)}(?:-internal)?\.aliyuncs\.com\Z"
        )
        if not expected_endpoint.search(oss_hostname):
            raise ValueError("ALIYUN_OSS_ENDPOINT 与 Sandbox 地域不一致。")
        return self

    def require_enabled(self) -> None:
        """在任何第三方 SDK 导入或云调用之前执行显式开关检查。"""
        if not self.enabled:
            raise SandboxProbeDisabledError(
                "沙箱探针默认关闭；请先显式设置 ALIYUN_SANDBOX_PROBE_ENABLED=true。"
            )

    def require_recovery_enabled(self) -> None:
        """在阶段 2 的任何 SDK 导入或云调用之前检查独立开关。"""
        self.require_enabled()
        if not self.recovery_probe_enabled:
            raise SandboxProbeDisabledError(
                "恢复探针默认关闭；请先显式设置 "
                "ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED=true。"
            )


def load_sandbox_settings() -> SandboxSettings:
    """从系统环境变量或项目 .env 读取探针配置，不修改进程环境。"""
    local_values = dotenv_values(
        _PROJECT_ROOT / ".env",
        encoding="utf-8-sig",
        interpolate=False,
    )

    def read_value(name: str, default: str = "") -> str:
        value = os.environ[name] if name in os.environ else local_values.get(name, default)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是文本。")
        return value.strip()

    def read_optional(name: str) -> str | None:
        value = read_value(name)
        return value or None

    def read_boolean(name: str, default: bool) -> bool:
        raw = read_value(name, "true" if default else "false").lower()
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise ValueError(f"{name} 只能是 true 或 false。")

    def read_integer(name: str, default: int) -> int:
        raw = read_value(name, str(default))
        if not raw.isascii() or not raw.isdigit():
            raise ValueError(f"{name} 必须是整数。")
        return int(raw)

    api_key = read_optional("E2B_API_KEY")
    return SandboxSettings(
        enabled=read_boolean("ALIYUN_SANDBOX_PROBE_ENABLED", False),
        recovery_probe_enabled=read_boolean(
            "ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED", False
        ),
        allow_internet_access=read_boolean(
            "ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS", False
        ),
        api_key=SecretStr(api_key) if api_key is not None else None,
        api_url=read_optional("E2B_API_URL"),
        domain=read_optional("E2B_DOMAIN"),
        template=read_optional("E2B_TEMPLATE"),
        timeout_seconds=read_integer("ALIYUN_SANDBOX_TIMEOUT_SECONDS", 300),
        idle_timeout_seconds=read_integer("ALIYUN_SANDBOX_IDLE_TIMEOUT_SECONDS", 120),
        max_session_seconds=read_integer("ALIYUN_SANDBOX_MAX_SESSION_SECONDS", 3600),
        runtime_venv_dir=read_value(
            "ALIYUN_SANDBOX_RUNTIME_VENV_DIR",
            "/opt/agnet-study/runtime/venv",
        ),
        runtime_user=read_value("ALIYUN_SANDBOX_RUNTIME_USER", "user"),
        oss_bucket=read_optional("ALIYUN_OSS_BUCKET"),
        oss_endpoint=read_optional("ALIYUN_OSS_ENDPOINT"),
        oss_bucket_path=read_optional("ALIYUN_OSS_BUCKET_PATH"),
        oss_mount_dir=read_value("ALIYUN_OSS_MOUNT_DIR", "/mnt/oss"),
        oss_role_arn=read_optional("ALIYUN_OSS_ROLE_ARN"),
        oss_read_only=read_boolean("ALIYUN_OSS_READ_ONLY", False),
    )
