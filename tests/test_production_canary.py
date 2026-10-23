"""Offline contract tests for the production canary entry point."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock

from scripts import production_canary


class ProductionCanaryTests(unittest.TestCase):
    def _invoke(self, runner, *, environ=None, clock=None):
        output = StringIO()
        keyword = {
            "argv": ["--run-live-canary"],
            "environ": {} if environ is None else environ,
            "process_runner": runner,
        }
        if clock is not None:
            keyword["clock"] = clock
        with redirect_stdout(output):
            exit_code = production_canary.main(**keyword)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return exit_code, json.loads(lines[0]), lines[0]

    def test_success_runs_exact_fixed_targets_and_emits_safe_json(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b"provider-success-secret",
                stderr=b"role bucket path sandbox-id",
            )

        exit_code, report, raw = self._invoke(runner)

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "success")
        self.assertFalse(report["alert_required"])
        self.assertEqual(
            report["gates"],
            {
                "oss_ram_policy": "passed",
                "security_boundaries": "passed",
                "default_remote_rollback": "passed",
            },
        )
        self.assertEqual(report["completed_gates"], 3)
        self.assertEqual(report["total_gates"], 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [call[0] for call in calls],
            [
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    production_canary._RAM_TARGET,
                ),
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    production_canary._SECURITY_TARGET,
                ),
                (
                    sys.executable,
                    "-m",
                    "unittest",
                    production_canary._DEFAULT_REMOTE_TARGET,
                ),
            ],
        )
        self.assertNotIn("provider-success-secret", raw)
        self.assertNotIn("sandbox-id", raw)

    def test_nonzero_gate_fails_fast_and_redacts_all_child_output(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ("unsafe",),
                7,
                stdout=b"E2B_API_KEY=TOP_SECRET",
                stderr=b"acs:ram::123456789012:role/secret /private/prefix",
            )
        )

        exit_code, report, raw = self._invoke(runner)

        self.assertEqual(exit_code, 20)
        self.assertEqual(report["status"], "gate_failed")
        self.assertTrue(report["alert_required"])
        self.assertEqual(
            report["gates"],
            {
                "oss_ram_policy": "failed",
                "security_boundaries": "not_run",
                "default_remote_rollback": "not_run",
            },
        )
        runner.assert_called_once()
        for secret in ("TOP_SECRET", "123456789012", "private", "prefix"):
            self.assertNotIn(secret, raw)

    def test_timeout_is_nonzero_safe_and_stops(self):
        def runner(command, **kwargs):
            raise subprocess.TimeoutExpired(
                command,
                kwargs["timeout"],
                output=b"provider-timeout-secret",
                stderr=b"sandbox-secret",
            )

        exit_code, report, raw = self._invoke(runner)

        self.assertEqual(exit_code, 21)
        self.assertEqual(report["status"], "timeout")
        self.assertEqual(report["gates"]["oss_ram_policy"], "timeout")
        self.assertNotIn("provider-timeout-secret", raw)
        self.assertNotIn("sandbox-secret", raw)

    def test_subprocess_exception_is_nonzero_and_redacted(self):
        runner = Mock(side_effect=OSError("credential-and-provider-secret"))

        exit_code, report, raw = self._invoke(runner)

        self.assertEqual(exit_code, 22)
        self.assertEqual(report["status"], "subprocess_error")
        self.assertEqual(
            report["gates"]["oss_ram_policy"], "subprocess_error"
        )
        self.assertNotIn("credential-and-provider-secret", raw)

    def test_environment_is_allowlisted_and_each_process_has_one_live_gate(self):
        calls = []
        source = {
            "SystemRoot": r"C:\Windows",
            "PATH": r"C:\safe",
            "E2B_API_KEY": "allowed-secret",
            "ALIYUN_OSS_BUCKET": "allowed-bucket",
            "DEEPSEEK_API_KEY": "must-not-pass",
            "AGENT_DEFAULT_EXECUTION_BACKEND": "aliyun-sandbox",
            "PYTHONPATH": "attacker-path",
            "HTTP_PROXY": "http://attacker.invalid",
            "RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_E2E": "1",
            "UNRELATED_SECRET": "must-not-pass",
        }

        def runner(command, **kwargs):
            calls.append(kwargs["env"])
            return subprocess.CompletedProcess(command, 0, b"", b"")

        exit_code, _, _ = self._invoke(runner, environ=source)

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 3)
        for child in calls:
            self.assertEqual(child["E2B_API_KEY"], "allowed-secret")
            self.assertEqual(child["ALIYUN_OSS_BUCKET"], "allowed-bucket")
            self.assertEqual(child["ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS"], "false")
            self.assertEqual(child["ALIYUN_SANDBOX_TIMEOUT_SECONDS"], "300")
            for forbidden in (
                "DEEPSEEK_API_KEY",
                "AGENT_DEFAULT_EXECUTION_BACKEND",
                "PYTHONPATH",
                "HTTP_PROXY",
                "UNRELATED_SECRET",
            ):
                self.assertNotIn(forbidden, child)
        self.assertEqual(calls[0][production_canary._RAM_GATE_ENV], "1")
        self.assertNotIn(production_canary._SECURITY_GATE_ENV, calls[0])
        self.assertNotIn(production_canary._DEFAULT_REMOTE_GATE_ENV, calls[0])
        self.assertEqual(calls[1][production_canary._SECURITY_GATE_ENV], "1")
        self.assertNotIn(production_canary._RAM_GATE_ENV, calls[1])
        self.assertNotIn(production_canary._DEFAULT_REMOTE_GATE_ENV, calls[1])
        self.assertEqual(
            calls[2][production_canary._DEFAULT_REMOTE_GATE_ENV],
            "1",
        )
        self.assertNotIn(production_canary._RAM_GATE_ENV, calls[2])
        self.assertNotIn(production_canary._SECURITY_GATE_ENV, calls[2])

    def test_total_deadline_exhaustion_never_starts_second_gate(self):
        calls = []
        clock_values = iter((0.0, 1.0, 901.0))

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        exit_code, report, _ = self._invoke(
            runner,
            clock=lambda: next(clock_values),
        )

        self.assertEqual(exit_code, 21)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            report["gates"],
            {
                "oss_ram_policy": "passed",
                "security_boundaries": "timeout",
                "default_remote_rollback": "not_run",
            },
        )

    def test_third_gate_failure_is_distinct_and_uses_only_its_live_switch(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs["env"]))
            return subprocess.CompletedProcess(
                command,
                9 if len(calls) == 3 else 0,
                b"remote-provider-secret",
                b"rollback-provider-secret",
            )

        exit_code, report, raw = self._invoke(runner)

        self.assertEqual(exit_code, 20)
        self.assertEqual(report["status"], "gate_failed")
        self.assertEqual(
            report["gates"],
            {
                "oss_ram_policy": "passed",
                "security_boundaries": "passed",
                "default_remote_rollback": "failed",
            },
        )
        self.assertEqual(calls[2][0][-1], production_canary._DEFAULT_REMOTE_TARGET)
        self.assertEqual(
            calls[2][1][production_canary._DEFAULT_REMOTE_GATE_ENV],
            "1",
        )
        self.assertNotIn(production_canary._RAM_GATE_ENV, calls[2][1])
        self.assertNotIn(production_canary._SECURITY_GATE_ENV, calls[2][1])
        self.assertNotIn("remote-provider-secret", raw)
        self.assertNotIn("rollback-provider-secret", raw)

    def test_missing_or_extra_opt_in_is_safe_configuration_error(self):
        for argv in ([], ["--unknown"], ["--run-live-canary", "extra"]):
            with self.subTest(argv=argv):
                runner = Mock(side_effect=AssertionError("must not run"))
                output = StringIO()
                with redirect_stdout(output):
                    exit_code = production_canary.main(
                        argv,
                        environ={},
                        process_runner=runner,
                    )
                report = json.loads(output.getvalue())
                self.assertEqual(exit_code, 23)
                self.assertEqual(report["status"], "configuration_error")
                self.assertTrue(report["alert_required"])
                runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
