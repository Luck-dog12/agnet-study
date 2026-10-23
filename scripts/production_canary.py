"""Run the fixed production Sandbox acceptance gates with a safe summary.

This entry point is intentionally separate from the product Agent.  Child
unittest output is captured and discarded so provider messages, credentials,
resource coordinates, and identifiers can never cross the reporting boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Final

from app.sandbox_config import SANDBOX_ENV_NAMES


_PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_OPT_IN_ARGUMENT: Final[str] = "--run-live-canary"
_TOTAL_TIMEOUT_SECONDS: Final[float] = 900.0

_RAM_GATE_NAME: Final[str] = "oss_ram_policy"
_SECURITY_GATE_NAME: Final[str] = "security_boundaries"
_DEFAULT_REMOTE_GATE_NAME: Final[str] = "default_remote_rollback"
_RAM_GATE_ENV: Final[str] = "RUN_ALIYUN_SANDBOX_LIVE_OSS_RAM_POLICY_E2E"
_SECURITY_GATE_ENV: Final[str] = (
    "RUN_ALIYUN_SANDBOX_LIVE_SECURITY_BOUNDARIES_E2E"
)
_DEFAULT_REMOTE_GATE_ENV: Final[str] = (
    "RUN_ALIYUN_SANDBOX_LIVE_DEFAULT_REMOTE_CANARY_E2E"
)
_RAM_TARGET: Final[str] = (
    "tests.test_aliyun_sandbox_oss_ram_policy_live_e2e."
    "AliyunSandboxOssRamPolicyLiveE2ETests."
    "test_real_oss_ram_role_is_limited_to_the_configured_prefix"
)
_SECURITY_TARGET: Final[str] = (
    "tests.test_aliyun_sandbox_security_boundaries_live_e2e."
    "AliyunSandboxSecurityBoundariesLiveE2ETests."
    "test_real_security_boundaries_are_enforced_and_cleaned"
)
_DEFAULT_REMOTE_TARGET: Final[str] = (
    "tests.test_aliyun_sandbox_default_remote_canary_live_e2e."
    "AliyunSandboxDefaultRemoteCanaryLiveE2ETests."
    "test_new_default_remote_session_mutates_reads_rolls_back_and_cleans"
)

_EXIT_SUCCESS: Final[int] = 0
_EXIT_GATE_FAILED: Final[int] = 20
_EXIT_TIMEOUT: Final[int] = 21
_EXIT_SUBPROCESS_ERROR: Final[int] = 22
_EXIT_CONFIGURATION_ERROR: Final[int] = 23

_SYSTEM_ENV_ALLOWLIST: Final[tuple[str, ...]] = (
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
)

_ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _safe_report(
    status: str,
    exit_code: int,
    gate_states: Mapping[str, str],
) -> dict[str, object]:
    """Build a bounded report containing only fixed vocabulary and counts."""
    ordered_states = {
        _RAM_GATE_NAME: gate_states[_RAM_GATE_NAME],
        _SECURITY_GATE_NAME: gate_states[_SECURITY_GATE_NAME],
        _DEFAULT_REMOTE_GATE_NAME: gate_states[_DEFAULT_REMOTE_GATE_NAME],
    }
    return {
        "schema": "agnet-study-production-canary/v1",
        "status": status,
        "exit_code": exit_code,
        "alert_required": exit_code != _EXIT_SUCCESS,
        "completed_gates": sum(
            value != "not_run" for value in ordered_states.values()
        ),
        "total_gates": len(ordered_states),
        "gates": ordered_states,
    }


def _child_environment(
    source: Mapping[str, str],
    *,
    live_gate: str,
) -> dict[str, str]:
    """Copy only required configuration and force the safety switches."""
    by_upper = {str(key).upper(): str(value) for key, value in source.items()}
    child: dict[str, str] = {}
    for name in (*_SYSTEM_ENV_ALLOWLIST, *SANDBOX_ENV_NAMES):
        value = by_upper.get(name)
        if value is not None:
            child[name] = value

    child.update(
        {
            "ALIYUN_SANDBOX_PROBE_ENABLED": "true",
            "ALIYUN_SANDBOX_RECOVERY_PROBE_ENABLED": "true",
            "ALIYUN_SANDBOX_ALLOW_INTERNET_ACCESS": "false",
            "ALIYUN_SANDBOX_TIMEOUT_SECONDS": "300",
            "ALIYUN_OSS_READ_ONLY": "false",
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            live_gate: "1",
        }
    )
    return child


def _run_production_canary(
    *,
    environ: Mapping[str, str] | None = None,
    process_runner: _ProcessRunner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, object], int]:
    """Run three fixed gates sequentially and fail fast on the first problem."""
    source = os.environ if environ is None else environ
    states = {
        _RAM_GATE_NAME: "not_run",
        _SECURITY_GATE_NAME: "not_run",
        _DEFAULT_REMOTE_GATE_NAME: "not_run",
    }
    deadline = clock() + _TOTAL_TIMEOUT_SECONDS
    gates = (
        (_RAM_GATE_NAME, _RAM_GATE_ENV, _RAM_TARGET),
        (_SECURITY_GATE_NAME, _SECURITY_GATE_ENV, _SECURITY_TARGET),
        (
            _DEFAULT_REMOTE_GATE_NAME,
            _DEFAULT_REMOTE_GATE_ENV,
            _DEFAULT_REMOTE_TARGET,
        ),
    )

    for name, live_gate, target in gates:
        remaining = deadline - clock()
        if remaining <= 0:
            states[name] = "timeout"
            return _safe_report("timeout", _EXIT_TIMEOUT, states), _EXIT_TIMEOUT

        command = (sys.executable, "-m", "unittest", target)
        try:
            completed = process_runner(
                command,
                cwd=_PROJECT_ROOT,
                env=_child_environment(source, live_gate=live_gate),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
                text=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            states[name] = "timeout"
            return _safe_report("timeout", _EXIT_TIMEOUT, states), _EXIT_TIMEOUT
        except Exception:
            states[name] = "subprocess_error"
            return (
                _safe_report(
                    "subprocess_error",
                    _EXIT_SUBPROCESS_ERROR,
                    states,
                ),
                _EXIT_SUBPROCESS_ERROR,
            )

        return_code = getattr(completed, "returncode", None)
        if type(return_code) is not int:
            states[name] = "subprocess_error"
            return (
                _safe_report(
                    "subprocess_error",
                    _EXIT_SUBPROCESS_ERROR,
                    states,
                ),
                _EXIT_SUBPROCESS_ERROR,
            )
        if return_code != 0:
            states[name] = "failed"
            return (
                _safe_report("gate_failed", _EXIT_GATE_FAILED, states),
                _EXIT_GATE_FAILED,
            )
        states[name] = "passed"

    return _safe_report("success", _EXIT_SUCCESS, states), _EXIT_SUCCESS


def _emit(report: Mapping[str, object]) -> None:
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    process_runner: _ProcessRunner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """CLI boundary: one explicit opt-in argument, one safe JSON line."""
    selected = tuple(sys.argv[1:] if argv is None else argv)
    if selected != (_OPT_IN_ARGUMENT,):
        states = {
            _RAM_GATE_NAME: "not_run",
            _SECURITY_GATE_NAME: "not_run",
            _DEFAULT_REMOTE_GATE_NAME: "not_run",
        }
        _emit(
            _safe_report(
                "configuration_error",
                _EXIT_CONFIGURATION_ERROR,
                states,
            )
        )
        return _EXIT_CONFIGURATION_ERROR

    report, exit_code = _run_production_canary(
        environ=environ,
        process_runner=process_runner,
        clock=clock,
    )
    _emit(report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
