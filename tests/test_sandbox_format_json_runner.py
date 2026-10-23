"""Offline tests for the trusted remote format_json data-plane runner."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tool import _sandbox_format_json_runner as runner


def _posix_for_runner(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) >= 3 and value[1:3] == ":/":
        return value[2:]
    return value


class SandboxFormatJsonRunnerTests(unittest.TestCase):
    def test_runner_writes_only_canonical_staging_candidate(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="format-json-runner-", dir=Path(__file__).resolve().parent
        ) as temporary:
            workspace = Path(temporary) / "workspace"
            source = workspace / "input.json"
            staging = workspace / ".agent-internal" / "transactions" / ("a" * 32)
            staging.mkdir(parents=True)
            source.parent.mkdir(parents=True, exist_ok=True)
            before = b'\xef\xbb\xbf{"z":1,"a":{"b":2,"a":1}}\n'
            source.write_bytes(before)
            payload = json.dumps({
                "cwd": _posix_for_runner(workspace),
                "paths": [_posix_for_runner(source)],
                "staging_root": _posix_for_runner(staging),
            })
            stdout = io.StringIO()
            with patch("sys.stdin", io.StringIO(payload)), patch("sys.stdout", stdout):
                self.assertEqual(runner.main(), 0)

            response = json.loads(stdout.getvalue())
            self.assertEqual(set(response), {"files"})
            self.assertEqual(len(response["files"]), 1)
            candidate = staging / "00.json"
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(
                candidate.read_text(encoding="utf-8"),
                '{\n  "a": {\n    "a": 1,\n    "b": 2\n  },\n  "z": 1\n}\n',
            )
            self.assertEqual(response["files"][0]["staged"], _posix_for_runner(candidate))

    def test_runner_reports_fixed_error_for_duplicate_keys_and_bad_payload(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="format-json-runner-error-", dir=Path(__file__).resolve().parent
        ) as temporary:
            workspace = Path(temporary) / "workspace"
            source = workspace / "input.json"
            workspace.mkdir(parents=True)
            source.write_text('{"a":1,"a":2}', encoding="utf-8")
            payloads = (
                json.dumps({
                    "cwd": _posix_for_runner(workspace),
                    "paths": [_posix_for_runner(source)],
                    "staging_root": _posix_for_runner(
                        workspace
                        / ".agent-internal"
                        / "transactions"
                        / ("0" * 32)
                    ),
                }),
                '{"cwd":"/workspace","cwd":"/other","paths":[],"staging_root":"/x"}',
            )
            for index, payload in enumerate(payloads):
                staging = (
                    workspace
                    / ".agent-internal"
                    / "transactions"
                    / (str(index) * 32)
                )
                staging.mkdir(parents=True, exist_ok=True)
                stdout = io.StringIO()
                with (
                    self.subTest(index=index),
                    patch("sys.stdin", io.StringIO(payload)),
                    patch("sys.stdout", stdout),
                ):
                    self.assertEqual(runner.main(), 0)
                    self.assertEqual(
                        json.loads(stdout.getvalue()),
                        {"error": "format_json 候选生成失败。"},
                    )


if __name__ == "__main__":
    unittest.main()
