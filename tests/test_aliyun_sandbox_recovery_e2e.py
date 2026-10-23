"""必须显式多重启用的阿里云 Sandbox 跨实例恢复真实 E2E。"""

from __future__ import annotations

import os
import unittest

from app.aliyun_sandbox_recovery_probe import run_recovery_probe
from app.sandbox_config import load_sandbox_settings


@unittest.skipUnless(
    os.environ.get("RUN_ALIYUN_SANDBOX_RECOVERY_E2E") == "1",
    "设置 RUN_ALIYUN_SANDBOX_RECOVERY_E2E=1 后才运行真实恢复 E2E",
)
class AliyunSandboxRecoveryE2ETests(unittest.TestCase):
    def test_checkpoint_survives_a_and_restores_into_b_local_workspace(self):
        settings = load_sandbox_settings()
        settings.require_recovery_enabled()
        result = run_recovery_probe(settings)
        self.assertTrue(result.source_killed_before_restore)
        self.assertTrue(result.checkpoint_removed)
        self.assertTrue(result.restored_sandbox_killed)
        self.assertGreaterEqual(result.file_count, 3)


if __name__ == "__main__":
    unittest.main()
