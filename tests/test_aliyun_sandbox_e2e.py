"""必须双重显式启用的阿里云 Sandbox + OSS 真实端到端测试。"""

from __future__ import annotations

import os
import unittest

from app.aliyun_sandbox_probe import run_connectivity_probe
from app.sandbox_config import load_sandbox_settings


@unittest.skipUnless(
    os.environ.get("RUN_ALIYUN_SANDBOX_E2E") == "1",
    "设置 RUN_ALIYUN_SANDBOX_E2E=1 后才运行真实阿里云 Sandbox E2E",
)
class AliyunSandboxE2ETests(unittest.TestCase):
    def test_real_sandbox_can_write_read_and_remove_oss_marker(self):
        settings = load_sandbox_settings()
        settings.require_enabled()
        result = run_connectivity_probe(settings)
        self.assertTrue(result.sandbox_id)
        self.assertTrue(result.marker_removed)


if __name__ == "__main__":
    unittest.main()
