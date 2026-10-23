"""显式启用的真实 DeepSeek API 端到端测试。

默认 unittest discover 会安全跳过。仅当操作者主动设置 RUN_DEEPSEEK_E2E=1 时，
测试才通过 app.config/app.client/CodingAgent 发起一次真实网络请求；任何输出都不
包含 API Key。
"""

from __future__ import annotations

import os
import unittest

from app.agent import CodingAgent
from app.client import create_client
from app.config import load_settings
from app.workflow import SessionEventWorkflow


@unittest.skipUnless(
    os.environ.get("RUN_DEEPSEEK_E2E") == "1",
    "设置 RUN_DEEPSEEK_E2E=1 后才运行真实 DeepSeek API E2E",
)
class DeepSeekE2ETests(unittest.TestCase):
    def test_real_api_returns_nonblank_final_text(self):
        settings = load_settings()
        client = create_client(settings)
        try:
            agent = CodingAgent(
                client,
                settings,
                session_workflow=SessionEventWorkflow.in_memory(),
                system_prompt="你正在执行连通性测试。不要调用工具，只回复 E2E_OK。",
                max_tool_rounds=0,
            )
            response = agent.run("请回复 E2E_OK")
        finally:
            client.close()
        self.assertIsInstance(response, str)
        self.assertTrue(response.strip())


if __name__ == "__main__":
    unittest.main()
