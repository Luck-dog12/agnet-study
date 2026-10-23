"""后台维护服务的离线生命周期与候选迁移测试。"""

from __future__ import annotations

from threading import Event
import unittest
from unittest.mock import Mock, patch

from app.maintenance import MaintenanceService
from app.observability import Observability


class MaintenanceServiceTests(unittest.TestCase):
    def test_construction_has_no_tasks_or_thread_side_effects_and_lifecycle_is_idempotent(self):
        observer = Mock(spec=Observability)
        service = MaintenanceService(
            observer,
            interval_seconds=3_600,
        )

        observer.cleanup_expired_logs.assert_not_called()
        observer.flush_pending.assert_not_called()
        self.assertFalse(service.is_running)

        service.start()
        service.start()
        self.assertTrue(service.is_running)
        observer.cleanup_expired_logs.assert_called_once_with()
        observer.flush_pending.assert_called_once_with()

        service.close()
        service.close()
        self.assertFalse(service.is_running)

    def test_tasks_are_independently_fail_soft(self):
        observer = Mock(spec=Observability)
        observer.cleanup_expired_logs.side_effect = OSError("SENSITIVE_LOG_FAILURE")
        service = MaintenanceService(observer)

        service.run_once()

        observer.cleanup_expired_logs.assert_called_once_with()
        observer.flush_pending.assert_called_once_with()

    def test_background_loop_uses_injected_interval_and_runs_periodically(self):
        observer = Mock(spec=Observability)
        second_run = Event()
        call_count = [0]

        def cleanup_logs():
            call_count[0] += 1
            if call_count[0] >= 2:
                second_run.set()

        observer.cleanup_expired_logs.side_effect = cleanup_logs

        service = MaintenanceService(
            observer,
            interval_seconds=0.01,
        )
        service.start()
        try:
            self.assertTrue(second_run.wait(timeout=1.0))
        finally:
            service.close()
        self.assertGreaterEqual(call_count[0], 2)
        self.assertFalse(service.is_running)


if __name__ == "__main__":
    unittest.main()
