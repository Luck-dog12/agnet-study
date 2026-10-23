"""Remote Code Interpreter runner request and resource-boundary tests."""

from __future__ import annotations

from dataclasses import asdict
import builtins
import io
import json
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from tool import _code_interpreter_runner as runner


def request_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "code": "1 + 1",
        "max_memory_mb": 64,
        "max_processes": 2,
        "timeout_seconds": 3,
        "max_output_characters": 1024,
    }
    payload.update(updates)
    return payload


def encoded_request(**updates: object) -> str:
    return json.dumps(
        request_payload(**updates),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def fake_resource_module(
    limits: dict[int, tuple[int, int]],
    *,
    include_nproc: bool = True,
    set_error: Exception | None = None,
) -> tuple[ModuleType, list[tuple[int, tuple[int, int]]]]:
    module = ModuleType("resource")
    module.RLIM_INFINITY = -1
    module.RLIMIT_AS = 1
    module.RLIMIT_CPU = 2
    module.RLIMIT_FSIZE = 3
    if include_nproc:
        module.RLIMIT_NPROC = 4
    calls: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(kind: int) -> tuple[int, int]:
        return limits[kind]

    def setrlimit(kind: int, value: tuple[int, int]) -> None:
        calls.append((kind, value))
        if set_error is not None:
            raise set_error

    module.getrlimit = getrlimit  # type: ignore[attr-defined]
    module.setrlimit = setrlimit  # type: ignore[attr-defined]
    return module, calls


class CodeInterpreterRunnerTests(unittest.TestCase):
    def invoke_main(
        self,
        raw: str,
        *,
        stdin: io.StringIO | None = None,
    ) -> tuple[int, dict[str, object]]:
        input_stream = stdin if stdin is not None else io.StringIO(raw)
        stdout = io.StringIO()
        with (
            patch.object(runner.sys, "stdin", input_stream),
            patch.object(runner.sys, "stdout", stdout),
        ):
            status = runner.main()
        return status, json.loads(stdout.getvalue())

    def test_parse_request_preserves_all_dynamic_policy_and_call_limits(self) -> None:
        request = runner._parse_request(encoded_request())

        self.assertEqual(
            asdict(request),
            request_payload(),
        )

    def test_parse_request_rejects_malicious_missing_and_out_of_range_payloads(self) -> None:
        base = request_payload()
        cases: dict[str, str] = {
            "invalid_json": "{",
            "not_object": "[]",
            "duplicate_key": encoded_request().replace(
                '"code":"1 + 1",',
                '"code":"1 + 1","code":"2 + 2",',
                1,
            ),
            "nan": encoded_request(max_memory_mb=float("nan")),
            "missing_field": json.dumps(
                {key: value for key, value in base.items() if key != "max_processes"}
            ),
            "extra_field": json.dumps({**base, "approval_granted": True}),
            "empty_code": encoded_request(code=""),
            "oversized_utf8_code": encoded_request(code="你" * 2731),
            "code_wrong_type": encoded_request(code=["1 + 1"]),
            "memory_bool": encoded_request(max_memory_mb=True),
            "memory_low": encoded_request(max_memory_mb=63),
            "memory_high": encoded_request(max_memory_mb=513),
            "processes_bool": encoded_request(max_processes=False),
            "processes_low": encoded_request(max_processes=0),
            "processes_high": encoded_request(max_processes=3),
            "call_timeout_bool": encoded_request(timeout_seconds=True),
            "call_timeout_low": encoded_request(timeout_seconds=0),
            "call_timeout_high": encoded_request(timeout_seconds=11),
            "unexpected_policy_timeout": encoded_request(max_timeout_seconds=10),
            "output_bool": encoded_request(max_output_characters=True),
            "output_low": encoded_request(max_output_characters=1023),
            "output_high": encoded_request(max_output_characters=16_385),
        }

        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                runner._parse_request(raw)

    def test_main_applies_limits_before_run_and_never_exposes_request_fields(self) -> None:
        events: list[tuple[str, object]] = []
        safe_result = {
            "exit_code": 0,
            "stdout": "",
            "result": "2",
            "stderr": "",
            "truncated": False,
        }

        def apply_limits(request: object) -> None:
            events.append(("limits", request))

        def run_code(code: str, max_output_characters: int):
            events.append(("run", (code, max_output_characters)))
            return safe_result

        with (
            patch.object(runner, "_apply_resource_limits", side_effect=apply_limits),
            patch.object(runner, "_run", side_effect=run_code),
        ):
            status, response = self.invoke_main(encoded_request())

        self.assertEqual(status, 0)
        self.assertEqual(response, safe_result)
        self.assertEqual([name for name, _value in events], ["limits", "run"])
        self.assertEqual(events[1][1], ("1 + 1", 1024))
        self.assertEqual(asdict(events[0][1]), request_payload())  # type: ignore[arg-type]

    def test_main_reads_only_bounded_stdin_and_rejects_oversized_request(self) -> None:
        class TrackingInput(io.StringIO):
            def __init__(self, value: str) -> None:
                super().__init__(value)
                self.read_sizes: list[int] = []

            def read(self, size: int = -1) -> str:
                self.read_sizes.append(size)
                return super().read(size)

        stdin = TrackingInput("x" * (runner.MAX_REQUEST_BYTES + 100))
        run_code = Mock(side_effect=AssertionError("user code must not execute"))

        with patch.object(runner, "_run", run_code):
            status, response = self.invoke_main("", stdin=stdin)

        self.assertEqual(status, 0)
        self.assertEqual(stdin.read_sizes, [runner.MAX_REQUEST_BYTES + 1])
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(response["stderr"], "隔离执行请求无效。")
        run_code.assert_not_called()

    def test_posix_resource_limits_are_exact_and_include_optional_nproc(self) -> None:
        infinity = -1
        resource_module, calls = fake_resource_module(
            {
                1: (infinity, infinity),
                2: (infinity, infinity),
                3: (infinity, infinity),
                4: (infinity, infinity),
            }
        )
        request = runner._parse_request(encoded_request())

        with (
            patch.object(runner.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": resource_module}),
        ):
            runner._apply_resource_limits(request)

        memory_bytes = 64 * 1024 * 1024
        self.assertEqual(
            calls,
            [
                (resource_module.RLIMIT_AS, (memory_bytes, memory_bytes)),
                (resource_module.RLIMIT_CPU, (3, 4)),
                (resource_module.RLIMIT_NPROC, (2, 2)),
                (resource_module.RLIMIT_FSIZE, (64 * 1024, 64 * 1024)),
            ],
        )

    def test_posix_limits_never_raise_existing_lower_soft_or_hard_limits(self) -> None:
        resource_module, calls = fake_resource_module(
            {
                1: (32 * 1024 * 1024, 48 * 1024 * 1024),
                2: (2, 3),
                3: (1024, 4096),
                4: (1, 1),
            }
        )
        request = runner._parse_request(encoded_request())

        with (
            patch.object(runner.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": resource_module}),
        ):
            runner._apply_resource_limits(request)

        self.assertEqual(
            calls,
            [
                (resource_module.RLIMIT_AS, (32 * 1024 * 1024, 48 * 1024 * 1024)),
                (resource_module.RLIMIT_CPU, (2, 3)),
                (resource_module.RLIMIT_NPROC, (1, 1)),
                (resource_module.RLIMIT_FSIZE, (1024, 4096)),
            ],
        )

    def test_posix_without_nproc_still_applies_other_limits(self) -> None:
        infinity = -1
        resource_module, calls = fake_resource_module(
            {
                1: (infinity, infinity),
                2: (infinity, infinity),
                3: (infinity, infinity),
            },
            include_nproc=False,
        )
        request = runner._parse_request(encoded_request(max_processes=1))

        with (
            patch.object(runner.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": resource_module}),
        ):
            runner._apply_resource_limits(request)

        self.assertEqual(
            [kind for kind, _value in calls],
            [
                resource_module.RLIMIT_AS,
                resource_module.RLIMIT_CPU,
                resource_module.RLIMIT_FSIZE,
            ],
        )

    def test_setrlimit_failure_is_fail_closed_before_user_code(self) -> None:
        infinity = -1
        resource_module, _calls = fake_resource_module(
            {
                1: (infinity, infinity),
                2: (infinity, infinity),
                3: (infinity, infinity),
                4: (infinity, infinity),
            },
            set_error=OSError("SECRET setrlimit failure"),
        )
        run_code = Mock(side_effect=AssertionError("user code must not execute"))

        with (
            patch.object(runner.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": resource_module}),
            patch.object(runner, "_run", run_code),
        ):
            status, response = self.invoke_main(encoded_request())

        self.assertEqual(status, 0)
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(response["stdout"], "")
        self.assertEqual(response["result"], "")
        self.assertEqual(response["stderr"], "隔离执行请求无效。")
        self.assertNotIn("SECRET", json.dumps(response, ensure_ascii=False))
        run_code.assert_not_called()

    def test_resource_import_failure_is_fail_closed_before_user_code(self) -> None:
        original_import = builtins.__import__
        run_code = Mock(side_effect=AssertionError("user code must not execute"))

        def guarded_import(name: str, *args, **kwargs):
            if name == "resource":
                raise ImportError("SECRET resource import failure")
            return original_import(name, *args, **kwargs)

        with (
            patch.object(runner.os, "name", "posix"),
            patch("builtins.__import__", side_effect=guarded_import),
            patch.object(runner, "_run", run_code),
        ):
            status, response = self.invoke_main(encoded_request())

        self.assertEqual(status, 0)
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(response["stderr"], "隔离执行请求无效。")
        self.assertNotIn("SECRET", json.dumps(response, ensure_ascii=False))
        run_code.assert_not_called()

    def test_getrlimit_failure_is_fail_closed_before_user_code(self) -> None:
        infinity = -1
        resource_module, _calls = fake_resource_module(
            {
                1: (infinity, infinity),
                2: (infinity, infinity),
                3: (infinity, infinity),
                4: (infinity, infinity),
            }
        )
        resource_module.getrlimit = Mock(  # type: ignore[attr-defined]
            side_effect=OSError("SECRET getrlimit failure")
        )
        run_code = Mock(side_effect=AssertionError("user code must not execute"))

        with (
            patch.object(runner.os, "name", "posix"),
            patch.dict(sys.modules, {"resource": resource_module}),
            patch.object(runner, "_run", run_code),
        ):
            status, response = self.invoke_main(encoded_request())

        self.assertEqual(status, 0)
        self.assertEqual(response["exit_code"], 1)
        self.assertEqual(response["stderr"], "隔离执行请求无效。")
        self.assertNotIn("SECRET", json.dumps(response, ensure_ascii=False))
        run_code.assert_not_called()

    def test_windows_resource_path_returns_before_importing_resource(self) -> None:
        request = runner._parse_request(encoded_request())
        imported: list[str] = []
        original_import = builtins.__import__

        def guarded_import(name: str, *args, **kwargs):
            imported.append(name)
            if name == "resource":
                raise AssertionError("Windows runner must not import resource")
            return original_import(name, *args, **kwargs)

        self.assertNotIn("resource", vars(runner))
        with (
            patch.object(runner.os, "name", "nt"),
            patch("builtins.__import__", side_effect=guarded_import),
        ):
            runner._apply_resource_limits(request)

        self.assertNotIn("resource", imported)


if __name__ == "__main__":
    unittest.main()
