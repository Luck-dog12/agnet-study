"""读取项目根目录的 DeepSeek 配置，系统环境变量优先。"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    """客户端配置；密钥不参与对象的文本表示。"""

    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout_seconds: float = 180.0
    max_retries: int = 2

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("api_key 必须是非空文本。")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not 10 <= self.timeout_seconds <= 600
        ):
            raise ValueError("timeout_seconds 必须在 10..600 之间。")
        if type(self.max_retries) is not int or not 0 <= self.max_retries <= 5:
            raise ValueError("max_retries 必须是 0..5 之间的整数。")


def load_default_execution_backend() -> Literal["local", "aliyun-sandbox"]:
    """按需读取新会话的默认执行后端。

    该配置不属于模型客户端 ``Settings``，因此恢复已绑定会话或
    CLI 显式选择后端时完全不会触发它的解析和校验。
    """
    if "AGENT_DEFAULT_EXECUTION_BACKEND" in os.environ:
        raw_value = os.environ["AGENT_DEFAULT_EXECUTION_BACKEND"]
    else:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        local_values = dotenv_values(
            env_path, encoding="utf-8-sig", interpolate=False
        )
        raw_value = local_values.get("AGENT_DEFAULT_EXECUTION_BACKEND", "local")
    if not isinstance(raw_value, str):
        raise ValueError(
            "AGENT_DEFAULT_EXECUTION_BACKEND 必须是 local 或 aliyun-sandbox。"
        )
    value = raw_value.strip()
    if value not in {"local", "aliyun-sandbox"}:
        raise ValueError(
            "AGENT_DEFAULT_EXECUTION_BACKEND 必须是 local 或 aliyun-sandbox。"
        )
    return value


def load_settings() -> Settings:
    """读取本地 .env，不修改进程环境，也不发送网络请求。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    local_values = dotenv_values(
        env_path, encoding="utf-8-sig", interpolate=False
    )

    def read_value(name: str, default: str = "") -> str:
        value = os.environ[name] if name in os.environ else local_values.get(name, default)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是文本。")
        return value.strip()

    api_key = read_value("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("请在项目根目录 .env 或系统环境变量中填写 DEEPSEEK_API_KEY。")

    base_url = read_value("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    try:
        parsed = urlsplit(base_url)
        valid_url = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(character.isspace() for character in base_url)
        )
        parsed.port  # 检查端口是否为有效整数及范围。
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("DEEPSEEK_BASE_URL 必须是有效的 HTTP 或 HTTPS 接口地址。")

    model = read_value("DEEPSEEK_MODEL", "deepseek-v4-flash")
    if (
        not model
        or len(model) > 200
        or any(ord(character) < 32 or character.isspace() for character in model)
    ):
        raise ValueError("DEEPSEEK_MODEL 必须是非空且不含空白或控制字符的模型名。")

    def read_integer(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = read_value(name, str(default))
        if not raw.isascii() or not raw.isdigit():
            raise ValueError(f"{name} 必须是 {minimum}..{maximum} 之间的整数。")
        value = int(raw)
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须是 {minimum}..{maximum} 之间的整数。")
        return value

    timeout_seconds = read_integer("DEEPSEEK_TIMEOUT_SECONDS", 180, 10, 600)
    max_retries = read_integer("DEEPSEEK_MAX_RETRIES", 2, 0, 5)
    return Settings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_seconds=float(timeout_seconds),
        max_retries=max_retries,
    )
