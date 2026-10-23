"""通过 OpenAI SDK 创建兼容 DeepSeek 接口的客户端。

SDK 是有意在函数内导入的：查看帮助、列出会话和单纯退出都不应为模型运行时付出启动成本。
"""

from typing import Any

from app.config import Settings, load_settings


def __getattr__(name: str) -> Any:
    """为测试/嵌入方保留可 patch 的 OpenAI 名称，访问时才导入 SDK。"""
    if name == "OpenAI":
        from openai import OpenAI

        return OpenAI
    raise AttributeError(name)


def create_client(settings: Settings | None = None) -> Any:
    """创建客户端但不发请求；调用者须使用 with 或 close() 释放资源。"""
    config = settings if settings is not None else load_settings()
    if not isinstance(config, Settings):
        raise TypeError("settings 必须是 Settings 实例。")
    # frozen dataclass 仍可能被低层嵌入方用 object.__setattr__ 绕过；在 SDK
    # 构造边界再次执行纯校验，避免非法超时/重试值进入第三方客户端。
    config.__post_init__()
    constructor = globals().get("OpenAI")
    if constructor is None:
        from openai import OpenAI as constructor

    return constructor(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )
