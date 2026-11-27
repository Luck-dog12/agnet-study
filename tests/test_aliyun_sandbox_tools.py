"""Fake-transport tests for the stage 4 remote tool-group boundary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
from pathlib import Path
import shlex
import signal
import threading
import unittest
from unittest import mock
import sys
import time
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Sequence

from pydantic import SecretStr

from app import aliyun_sandbox_tools as sandbox_tools_module
from app.aliyun_sandbox_tools import (
    AliyunSandboxToolGroup,
    RemoteSandboxApprovalRequired,
    RemoteSandboxClosedError,
    RemoteSandboxPolicyDenied,
    RemoteSandboxToolError,
)
from app.execution_backend import (
    AliyunSandboxExecutionBackend as _AliyunSandboxExecutionBackend,
    ExecutionLease,
    WorkspaceDescription,
)
from tests.sandbox_cleanup_fakes import empty_recovery_provider


def AliyunSandboxExecutionBackend(*args, **kwargs):
    kwargs.setdefault("automatic_maintenance", False)
    kwargs.setdefault("recovery_provider", empty_recovery_provider())
    if "sandbox_factory" in kwargs and "persistence_sandbox_factory" not in kwargs:
        class PersistenceSandbox:
            sandbox_id = "tools-persistence-sandbox"

            def kill(self):
                return True

        kwargs["persistence_sandbox_factory"] = (
            lambda _settings, _metadata: PersistenceSandbox()
        )
    if "transport_factory" in kwargs and "persistence_transport_factory" not in kwargs:
        original = kwargs["transport_factory"]
        cached = {}

        def execution_transport(candidate):
            cached["value"] = original(candidate)
            return cached["value"]

        kwargs["transport_factory"] = execution_transport
        kwargs["persistence_transport_factory"] = lambda _candidate: cached["value"]
    return _AliyunSandboxExecutionBackend(*args, **kwargs)
from app.sandbox_config import SandboxSettings
from app.sandbox_transport import (
    E2BSandboxTransport,
    SandboxCommandResult,
    SandboxEditResult,
    SandboxFileEntry,
    SandboxGrepMatch,
    SandboxGrepResult,
    SandboxListResult,
    SandboxReadResult,
    SandboxWriteResult,
)
from app.schemas import (
    BashShellArguments,
    CodeInterpreterArguments,
    EditorFileArguments,
    GrepArguments,
    ListFilesArguments,
    ReadFileArguments,
    WriteFileArguments,
)
from app.workspace_seed import WorkspaceSeedEntry, WorkspaceSeedSnapshot
from app.workspace_versions import MAX_VERSION_ENTRIES, WorkspaceVersionCoordinator
from tool import _sandbox_command_runner as command_runner
from tool._sandbox_command_runner import _bounded_run
from tool.policy import PolicyLoadError, load_tool_policy


def enabled_settings() -> SandboxSettings:
    return SandboxSettings(
        enabled=True,
        api_key=SecretStr("api-key-must-not-leak"),
        api_url="https://api.cn-hangzhou.e2b.fc.aliyuncs.com",
        domain="cn-hangzhou.e2b.fc.aliyuncs.com",
        template="code-interpreter-v1",
        timeout_seconds=300,
        oss_bucket="agent-probe-bucket",
        oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        oss_bucket_path="/projects/test/runs/backend-tools",
        oss_mount_dir="/mnt/oss",
        oss_role_arn="acs:ram::1234567890123456:role/role-must-not-leak",
        oss_read_only=False,
    )


class FakeSandbox:
    def __init__(self) -> None:
        self.sandbox_id = "sandbox-secret-id"
        self.kill_calls = 0

    def kill(self) -> bool:
        self.kill_calls += 1
        return True


class _FakeRunnerProcess:
    def __init__(self, stdout: bytes = b"") -> None:
        self.pid = 4321
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO()
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -int(getattr(signal, "SIGTERM", 15))

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -int(getattr(signal, "SIGKILL", 9))

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise command_runner.subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def _fake_command_resource_module(
    initial: dict[int, tuple[object, object]],
    *,
    include_nproc: bool = True,
    set_error: BaseException | None = None,
) -> tuple[object, list[tuple[int, tuple[int, int]]]]:
    calls: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(kind: int) -> tuple[object, object]:
        return initial[kind]

    def setrlimit(kind: int, value: tuple[int, int]) -> None:
        calls.append((kind, value))
        if set_error is not None:
            raise set_error

    values: dict[str, object] = {
        "RLIM_INFINITY": -1,
        "RLIMIT_AS": 1,
        "RLIMIT_CPU": 2,
        "RLIMIT_FSIZE": 3,
        "getrlimit": getrlimit,
        "setrlimit": setrlimit,
    }
    if include_nproc:
        values["RLIMIT_NPROC"] = 4
    return SimpleNamespace(**values), calls


class FakeTransport:
    def __init__(self, workspace_root: str) -> None:
        self.workspace_root = workspace_root
        self.files: dict[str, bytes] = {
            f"{workspace_root}/README.md": b"hello\n",
            f"{workspace_root}/src/main.py": b"hello from python\n",
        }
        self.calls: list[tuple[str, object]] = []
        self.forced_grep_path: str | None = None
        self.next_command = SandboxCommandResult(0, "ok\n", "")
        self.next_code = SandboxCommandResult(0, "2\n", "", result="2")
        self.next_list: SandboxListResult | None = None
        self.next_grep: SandboxGrepResult | None = None
        self.raise_error: Exception | None = None

    def _fail(self) -> None:
        if self.raise_error is not None:
            raise self.raise_error

    def read_file(self, path: str, *, offset: int, max_bytes: int, timeout_seconds: int):
        self._fail()
        self.calls.append(("read", (path, offset, max_bytes, timeout_seconds)))
        data = self.files[path]
        return SandboxReadResult(data[offset : offset + max_bytes], len(data))

    def write_file(self, path: str, data: bytes, *, overwrite: bool, timeout_seconds: int):
        self._fail()
        self.calls.append(("write", (path, overwrite, timeout_seconds)))
        if not overwrite and path in self.files:
            raise OSError("already exists")
        self.files[path] = data
        return SandboxWriteResult(len(data))

    def edit_file(
        self,
        path: str,
        *,
        operation: str,
        old_text: str,
        new_text: str | None,
        expected_replacements: int,
        position: str | None,
        timeout_seconds: int,
    ):
        self._fail()
        self.calls.append(("edit", (path, operation, timeout_seconds)))
        original = self.files[path]
        text = original.decode("utf-8")
        if text.count(old_text) != expected_replacements:
            raise OSError("wrong replacement count")
        replacement = text.replace(old_text, new_text or "")
        updated = replacement.encode("utf-8")
        self.files[path] = updated
        return SandboxEditResult(
            encoding="utf-8",
            replacements=expected_replacements,
            bytes_before=len(original),
            bytes_after=len(updated),
            sha256_before=hashlib.sha256(original).hexdigest(),
            sha256_after=hashlib.sha256(updated).hexdigest(),
        )

    def list_files(
        self,
        pattern: str,
        *,
        workspace_root: str,
        max_results: int,
        timeout_seconds: int,
        path_predicate,
    ):
        self._fail()
        if self.next_list is not None:
            return self.next_list
        self.assert_workspace_root = workspace_root
        self.calls.append(("list", (pattern, max_results, timeout_seconds)))
        entries = tuple(
            SandboxFileEntry(path=path, size=len(data))
            for path, data in sorted(self.files.items())
            if path_predicate(path[len(self.workspace_root) + 1 :], False)
        )
        return SandboxListResult(entries=entries[:max_results], truncated=len(entries) > max_results)

    def grep(
        self,
        pattern: str,
        *,
        workspace_root: str,
        file_pattern: str,
        case_sensitive: bool,
        max_results: int,
        timeout_seconds: int,
        path_predicate,
    ):
        self._fail()
        if self.next_grep is not None:
            return self.next_grep
        if workspace_root != self.workspace_root:
            raise OSError("workspace mismatch")
        self.calls.append(("grep", (file_pattern, timeout_seconds)))
        matches: list[SandboxGrepMatch] = []
        for path, data in sorted(self.files.items()):
            relative = path[len(self.workspace_root) + 1 :]
            if (
                not path_predicate(relative, False)
                or not PurePosixPath(relative).full_match(file_pattern)
            ):
                continue
            text = data.decode("utf-8")
            for line_number, line in enumerate(text.splitlines(), 1):
                haystack = line if case_sensitive else line.casefold()
                needle = pattern if case_sensitive else pattern.casefold()
                column = haystack.find(needle)
                if column >= 0:
                    matches.append(
                        SandboxGrepMatch(
                            self.forced_grep_path or path,
                            line_number,
                            column + 1,
                            line,
                        )
                    )
        return SandboxGrepResult(
            matches=tuple(matches[:max_results]),
            files_searched=sum(
                1
                for path in self.files
                if path_predicate(path[len(self.workspace_root) + 1 :], False)
            ),
            truncated=len(matches) > max_results,
        )

    def run(
        self,
        command: str,
        *,
        paths: Sequence[str],
        code: str | None,
        cwd: str,
        timeout_seconds: int,
        max_output_bytes: int,
        max_output_characters: int | None = None,
        max_memory_mb: int | None = None,
        max_processes: int | None = None,
    ):
        self._fail()
        self.calls.append(
            (
                "run",
                (
                    command,
                    tuple(paths),
                    code,
                    cwd,
                    timeout_seconds,
                    max_output_bytes,
                    max_output_characters,
                    max_memory_mb,
                    max_processes,
                ),
            )
        )
        return self.next_code if command == "python" else self.next_command


class SdkEntry:
    def __init__(self, path: str, kind: str, size: int = 0, target: str | None = None) -> None:
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.type = kind
        self.size = size
        self.symlink_target = target


class FakeProviderFileNotFound(Exception):
    """Provider-style missing-path error, deliberately not FileNotFoundError."""


class FakeSdkFilesystem:
    def __init__(self, root: str) -> None:
        self.root = root
        self.dirs: set[str] = {"/workspace", root}
        self.data: dict[str, bytes] = {f"{root}/README.md": b"hello\n"}
        self.symlink = False
        self.symlink_parent = False
        self.workspace_symlink = False
        self.remove_error = False
        self.remove_result: object = None
        self.remove_retains_file = False
        self.calls: list[tuple[str, object]] = []
        self.write_payloads: list[tuple[str, bytes]] = []
        self.entry_overrides: dict[str, SdkEntry] = {}
        self.exists_overrides: dict[str, object] = {}
        self.exists_errors: dict[str, Exception] = {}
        self.get_info_errors: dict[str, Exception] = {}

    def _entries(self, directory: str, depth: int):
        entries: dict[str, SdkEntry] = {}
        for path in sorted(self.dirs):
            if path.startswith(directory + "/"):
                relative = path[len(directory) + 1 :]
                parts = relative.split("/")
                if depth != 1 or len(parts) == 1:
                    entries[path] = SdkEntry(path, "directory")
                elif parts:
                    child = directory + "/" + parts[0]
                    entries[child] = SdkEntry(child, "directory")
        for path, data in self.data.items():
            relative = path[len(directory) + 1 :] if path.startswith(directory + "/") else ""
            if not relative:
                continue
            parts = relative.split("/")
            if depth == 1 and len(parts) > 1:
                child = directory + "/" + parts[0]
                entries[child] = SdkEntry(child, "directory")
            else:
                entries[path] = SdkEntry(path, "file", len(data))
        if self.symlink and directory == self.root:
            entries[directory + "/link"] = SdkEntry(
                directory + "/link", "symlink", target="/mnt/oss"
            )
        if self.symlink_parent and directory == self.root:
            entries[directory + "/linkdir"] = SdkEntry(
                directory + "/linkdir", "symlink", target="/mnt/oss"
            )
        for path, entry in self.entry_overrides.items():
            if posixpath.dirname(path) == directory:
                entries[path] = entry
        return list(entries.values())

    def list(
        self,
        path: str,
        *,
        depth: int,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("list", (path, depth, request_timeout, user)))
        return self._entries(path, depth)

    def read(
        self,
        path: str,
        *,
        format: str,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("read", (path, format, request_timeout, user)))
        return self.data[path]

    def write(
        self,
        path: str,
        data: bytes,
        *,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("write", (path, request_timeout, user)))
        payload = bytes(data)
        self.write_payloads.append((path, payload))
        self.data[path] = payload
        parent = path.rsplit("/", 1)[0]
        self.dirs.add(parent)

    def make_dir(
        self,
        path: str,
        *,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("make_dir", (path, request_timeout, user)))
        if self.workspace_symlink and path == "/workspace":
            return False
        current = ""
        for part in path.split("/"):
            if not part:
                current = "/"
                continue
            current = current.rstrip("/") + "/" + part
            self.dirs.add(current)
        return True

    def get_info(
        self,
        path: str,
        *,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("get_info", (path, request_timeout, user)))
        if path in self.get_info_errors:
            raise self.get_info_errors[path]
        if path in self.entry_overrides:
            return self.entry_overrides[path]
        if self.workspace_symlink and path == "/workspace":
            return SdkEntry(path, "symlink", target="/mnt/oss")
        if self.symlink_parent and path == self.root + "/linkdir":
            return SdkEntry(path, "symlink", target="/mnt/oss")
        if self.symlink and path == self.root + "/link":
            return SdkEntry(path, "symlink", target="/mnt/oss")
        if path in self.dirs:
            return SdkEntry(path, "directory")
        if path in self.data:
            return SdkEntry(path, "file", len(self.data[path]))
        raise FileNotFoundError(path)

    def exists(
        self,
        path: str,
        *,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("exists", (path, request_timeout, user)))
        if path in self.exists_errors:
            raise self.exists_errors[path]
        if path in self.exists_overrides:
            return self.exists_overrides[path]
        return (
            path in self.dirs
            or path in self.data
            or path in self.entry_overrides
        )

    def remove(
        self,
        path: str,
        *,
        user: str | None = None,
        request_timeout: int,
    ):
        self.calls.append(("remove", (path, request_timeout, user)))
        if self.remove_error:
            raise OSError("cleanup secret.invalid")
        if not self.remove_retains_file:
            self.data.pop(path, None)
            self.dirs.discard(path)
        return self.remove_result


class FakeSdkCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.runner_output = '{"exit_code":0,"stdout":"","result":"2","stderr":"","truncated":false}'
        self.command_runner_output = '{"exit_code":0,"stdout":"ok","stderr":"","truncated":false}'
        self.runtime_resolved_python = "/usr/bin/python3.11"
        self.runtime_executable_sha256 = "b" * 64
        self.runtime_metadata_digest = "c" * 64
        self.runtime_probe_executable = "/opt/agnet-study/runtime/venv/bin/python"
        self.fail_runtime_inspection = False
        self.persistence_root_result = (0, "persistence-root-ok\n", "")
        self.persistence_runtime_result = (0, "persistence-runtime-denied\n", "")
        self.persistence_cleanup_result = (0, "", "")

    class Handle:
        def __init__(self, result: object) -> None:
            self.result = result
            self.kill_calls = 0

        def wait(self):
            return self.result

        def kill(self) -> bool:
            self.kill_calls += 1
            return True

    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        request_timeout: int,
        background: bool = False,
        user: str | None = None,
    ):
        self.calls.append((command, (cwd, timeout, request_timeout, user)))
        if "persistence-root-ok" in command:
            exit_code, stdout, stderr = self.persistence_root_result
            return type(
                "Result",
                (),
                {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            )()
        if "persistence-runtime-denied" in command:
            exit_code, stdout, stderr = self.persistence_runtime_result
            return type(
                "Result",
                (),
                {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            )()
        if "persistence-probe-cleanup" in command:
            exit_code, stdout, stderr = self.persistence_cleanup_result
            return type(
                "Result",
                (),
                {"stdout": stdout, "stderr": stderr, "exit_code": exit_code},
            )()
        if "json.dumps" in command and "sys.executable" in command:
            executable = (
                self.runtime_probe_executable
                if "/opt/agnet-study/runtime/venv/bin/python" in command
                else "/usr/bin/python3"
            )
            result = type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {"executable": executable, "version": [3, 11, 9]},
                        separators=(",", ":"),
                    )
                    + "\n",
                    "stderr": "",
                    "exit_code": 0,
                },
            )()
            return self.Handle(result) if background else result
        if "metadata_digest" in command and "resolved_python_path" in command:
            if self.fail_runtime_inspection:
                result = type(
                    "Result",
                    (),
                    {"stdout": "", "stderr": "inspection failed", "exit_code": 1},
                )()
                return self.Handle(result) if background else result
            result = type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        {
                            "resolved_python_path": self.runtime_resolved_python,
                            "executable_sha256": self.runtime_executable_sha256,
                            "metadata_digest": self.runtime_metadata_digest,
                        },
                        separators=(",", ":"),
                    )
                    + "\n",
                    "stderr": "",
                    "exit_code": 0,
                },
            )()
            return self.Handle(result) if background else result
        if "code_runner.py" in command:
            result = type("Result", (), {"stdout": self.runner_output, "stderr": "", "exit_code": 0})()
        elif "command_runner.py" in command:
            result = type("Result", (), {"stdout": self.command_runner_output, "stderr": "", "exit_code": 0})()
        else:
            result = type("Result", (), {"stdout": "", "stderr": "", "exit_code": 0})()
        return self.Handle(result) if background else result


class FakeSdkSandbox:
    def __init__(self, root: str) -> None:
        self.files = FakeSdkFilesystem(root)
        self.commands = FakeSdkCommands()


class AliyunSandboxToolGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = FakeSandbox()
        self.transport: FakeTransport | None = None
        self.workspace_root = "/workspace/agnet-study/" + "a" * 32
        self.authorization = object()

        def transport_factory(sandbox: object) -> FakeTransport:
            self.assertIs(sandbox, self.sandbox)
            self.transport = FakeTransport(self.workspace_root)
            return self.transport

        self.backend = AliyunSandboxExecutionBackend(
            "a" * 32,
            enabled_settings(),
            sandbox_factory=lambda settings, metadata: self.sandbox,
            transport_factory=transport_factory,
        )
        def consume_authorization(
            tool_name: str,
            arguments: object,
            authorization: object | None,
            authorization_kind: str,
        ) -> None:
            del tool_name, arguments, authorization_kind
            if authorization is not self.authorization:
                raise RemoteSandboxApprovalRequired("test authorization rejected")

        self.group = AliyunSandboxToolGroup(
            self.backend,
            authorization_consumer=consume_authorization,
        )

    def test_all_file_and_search_operations_share_one_workspace(self):
        read = self.group.read_file(ReadFileArguments(path="README.md", max_bytes=100))
        write = self.group.write_file(
            WriteFileArguments(path="new.txt", content="created", mode="create"),
            authorization=self.authorization,
        )
        edit = self.group.editor_file(
            EditorFileArguments(
                path="README.md",
                old_text="hello",
                new_text="world",
                expected_replacements=1,
            ),
            authorization=self.authorization,
        )
        listed = self.group.list_files(ListFilesArguments(pattern="**/*", max_results=10))
        searched = self.group.grep(GrepArguments(pattern="hello", file_pattern="**/*"))

        self.assertEqual(read.content, "hello\n")
        self.assertEqual(write.operation, "created")
        self.assertEqual(edit.replacements, 1)
        self.assertEqual(listed.count, 3)
        self.assertEqual(searched.count, 1)
        assert self.transport is not None
        for name, payload in self.transport.calls:
            if name in {"read", "write", "edit"}:
                self.assertTrue(payload[0].startswith(self.workspace_root + "/"))  # type: ignore[index]
        self.assertEqual(self.backend.ensure_ready().workspace_root, self.workspace_root)

    def test_command_and_code_use_same_cwd_and_require_explicit_control_plane_approval(self):
        with self.assertRaises(RemoteSandboxApprovalRequired):
            self.group.command(BashShellArguments(command="compileall", paths=["src"]))
        with self.assertRaises(RemoteSandboxApprovalRequired):
            self.group.code_interpreter(CodeInterpreterArguments(code="1+1"))

        command = self.group.command(
            BashShellArguments(command="compileall", paths=["src"], timeout_seconds=7),
            authorization=self.authorization,
        )
        code = self.group.code_interpreter(
            CodeInterpreterArguments(code="1+1", timeout_seconds=3),
            authorization=self.authorization,
        )
        self.assertEqual(command.exit_code, 0)
        self.assertEqual(code.result, "2")
        assert self.transport is not None
        runs = [payload for name, payload in self.transport.calls if name == "run"]
        self.assertEqual(runs[0][3], self.workspace_root)  # type: ignore[index]
        self.assertEqual(runs[1][3], self.workspace_root)  # type: ignore[index]
        self.assertEqual(runs[0][4], 7)  # type: ignore[index]
        self.assertEqual(runs[1][4], 3)  # type: ignore[index]
        self.assertIsNone(runs[0][6])  # type: ignore[index]
        self.assertEqual(runs[1][6], 16_384)  # type: ignore[index]

    def test_mutating_file_operations_require_explicit_control_plane_approval(self):
        write = WriteFileArguments(path="new.txt", content="x", mode="create")
        edit = EditorFileArguments(
            path="README.md",
            old_text="hello",
            new_text="world",
            expected_replacements=1,
        )
        with self.assertRaises(RemoteSandboxApprovalRequired):
            self.group.write_file(write)
        with self.assertRaises(RemoteSandboxApprovalRequired):
            self.group.editor_file(edit)
        self.group.write_file(write, authorization=self.authorization)
        self.group.editor_file(edit, authorization=self.authorization)

    def test_paths_escape_and_destructive_commands_fail_before_transport(self):
        for path in ("/etc/passwd", "../outside", "a/../../x", "a\\b", "a\x00b"):
            with self.subTest(path=path):
                with self.assertRaises(RemoteSandboxToolError):
                    self.group.read_file(ReadFileArguments(path=path))
        with self.assertRaises(RemoteSandboxToolError):
            self.group.list_files(ListFilesArguments(pattern="../*"))
        with self.assertRaises(RemoteSandboxToolError):
            self.group.grep(GrepArguments(pattern="x", file_pattern="/etc/*"))
        with self.assertRaises(RemoteSandboxApprovalRequired):
            self.group.command(
                BashShellArguments(command="remove_file", paths=["x"]),
                authorization=self.authorization,
            )
        assert self.transport is None

    def test_sealed_paths_fail_before_remote_transport_for_every_workspace_primitive(self):
        operations = (
            lambda: self.group.read_file(ReadFileArguments(path=".envrc")),
            lambda: self.group.write_file(
                WriteFileArguments(path="credentials", content="x", mode="create"),
                authorization=self.authorization,
            ),
            lambda: self.group.editor_file(
                EditorFileArguments(
                    path="token",
                    old_text="x",
                    new_text="y",
                    expected_replacements=1,
                ),
                authorization=self.authorization,
            ),
            lambda: self.group.list_files(ListFilesArguments(pattern=".env*")),
            lambda: self.group.grep(
                GrepArguments(pattern="x", file_pattern="**/*.key")
            ),
            lambda: self.group.command(
                BashShellArguments(command="compileall", paths=["private/server.key"]),
                authorization=self.authorization,
            ),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaises(RemoteSandboxPolicyDenied):
                    operation()
        self.assertIsNone(self.transport)

    def test_output_bounds_and_transport_timeout_are_enforced(self):
        assert self.transport is None
        self.group.ensure_ready()
        assert self.transport is not None
        self.transport.next_command = SandboxCommandResult(0, "x" * 40_000, "")
        output = self.group.command(
            BashShellArguments(command="compileall", paths=["src"]),
            authorization=self.authorization,
        )
        self.assertTrue(output.truncated)
        self.assertLessEqual(len(output.stdout), 32_768)
        self.transport.next_code = SandboxCommandResult(0, "x" * 20_000, "", result="y" * 20_000)
        code = self.group.code_interpreter(
            CodeInterpreterArguments(code="1+1"),
            authorization=self.authorization,
        )
        self.assertTrue(code.truncated)
        self.assertLessEqual(len(code.stdout), 16_384)

    def test_code_uses_latest_policy_output_limit_for_transport_and_result(self):
        policy = load_tool_policy()
        low_output_policy = policy.code_interpreter.model_copy(
            update={
                "max_output_characters": 1024,
                "max_memory_mb": 64,
                "max_processes": 1,
            }
        )
        self.group.ensure_ready()
        assert self.transport is not None
        self.transport.next_code = SandboxCommandResult(
            0,
            "x" * 2048,
            "",
            result="y" * 2048,
        )

        with mock.patch.object(
            sandbox_tools_module,
            "validate_code_interpreter_policy",
            return_value=low_output_policy,
        ) as validate_policy:
            result = self.group.code_interpreter(
                CodeInterpreterArguments(code="1+1"),
                authorization=self.authorization,
            )

        validate_policy.assert_called_once()
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), 1024)
        self.assertEqual(len(result.result), 1024)
        run_payload = [
            payload for name, payload in self.transport.calls if name == "run"
        ][-1]
        self.assertEqual(run_payload[6], 1024)  # type: ignore[index]
        self.assertEqual(run_payload[7], 64)  # type: ignore[index]
        self.assertEqual(run_payload[8], 1)  # type: ignore[index]

    def test_transport_paths_outside_lease_are_rejected(self):
        self.group.ensure_ready()
        assert self.transport is not None
        self.transport.next_list = SandboxListResult(
            entries=(SandboxFileEntry("/workspace/other/escape.txt", 1),),
            truncated=False,
        )
        with self.assertRaises(RemoteSandboxToolError):
            self.group.list_files(ListFilesArguments(pattern="**/*"))

        self.transport.next_list = None
        self.transport.files = {self.workspace_root + "/escape.txt": b"needle"}
        self.transport.forced_grep_path = "/workspace/other/escape.txt"
        with self.assertRaises(RemoteSandboxToolError):
            self.group.grep(GrepArguments(pattern="needle", file_pattern="**/*"))

    def test_malicious_transport_cannot_return_policy_protected_paths(self):
        self.group.ensure_ready()
        assert self.transport is not None
        protected = self.workspace_root + "/data/pending/candidate.bin"
        self.transport.next_list = SandboxListResult(
            entries=(SandboxFileEntry(protected, 6),),
            truncated=False,
        )
        with self.assertRaises(RemoteSandboxToolError):
            self.group.list_files(ListFilesArguments(pattern="**/*"))

        self.transport.next_grep = SandboxGrepResult(
            matches=(SandboxGrepMatch(protected, 1, 1, "secret"),),
            files_searched=1,
            truncated=False,
        )
        with self.assertRaises(RemoteSandboxToolError):
            self.group.grep(GrepArguments(pattern="secret", file_pattern="**/*"))

    def test_policy_load_failure_stops_scan_before_transport_creation(self):
        with mock.patch.object(
            sandbox_tools_module,
            "load_tool_policy",
            side_effect=PolicyLoadError("SECRET broken policy"),
        ):
            for operation in (
                lambda: self.group.list_files(ListFilesArguments(pattern="**/*")),
                lambda: self.group.grep(
                    GrepArguments(pattern="needle", file_pattern="**/*")
                ),
            ):
                with self.subTest(operation=operation), self.assertRaises(
                    RemoteSandboxToolError
                ) as caught:
                    operation()
                self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(self.transport)

    def test_policy_predicate_does_not_prune_allowed_descendant_directories(self):
        policy = load_tool_policy()
        narrow = policy.model_copy(
            update={
                "file_access": policy.file_access.model_copy(
                    update={"allowed_path_patterns": ["src/nested/*.py"]}
                )
            }
        )
        with mock.patch.object(
            sandbox_tools_module, "load_tool_policy", return_value=narrow
        ):
            predicate = sandbox_tools_module._workspace_path_predicate()

        self.assertTrue(predicate("src", True))
        self.assertTrue(predicate("src/nested", True))
        self.assertTrue(predicate("src/nested/main.py", False))
        self.assertFalse(predicate("src/other.txt", False))
        self.assertFalse(predicate("skills", True))
        self.assertFalse(predicate(".agent", True))
        self.assertFalse(predicate("src/__pycache__", True))

    def test_grep_result_order_and_duplicates_are_rejected(self):
        self.group.ensure_ready()
        assert self.transport is not None
        first = SandboxGrepMatch(self.workspace_root + "/README.md", 2, 1, "needle")
        second = SandboxGrepMatch(self.workspace_root + "/README.md", 1, 1, "needle")
        self.transport.next_grep = SandboxGrepResult((first, second), 1, False)
        with self.assertRaises(RemoteSandboxToolError):
            self.group.grep(GrepArguments(pattern="needle", file_pattern="**/*"))
        self.transport.next_grep = SandboxGrepResult((first, first), 1, False)
        with self.assertRaises(RemoteSandboxToolError):
            self.group.grep(GrepArguments(pattern="needle", file_pattern="**/*"))

    def test_transport_errors_are_sanitized_and_close_fails_closed(self):
        self.group.ensure_ready()
        assert self.transport is not None
        self.transport.raise_error = OSError("https://user:secret@provider.invalid/request")
        with self.assertRaises(RemoteSandboxToolError) as caught:
            self.group.read_file(ReadFileArguments(path="README.md"))
        self.assertNotIn("provider.invalid", str(caught.exception))

        self.group.close()
        with self.assertRaises(RemoteSandboxClosedError):
            self.group.read_file(ReadFileArguments(path="README.md"))
        with self.assertRaises(RemoteSandboxClosedError):
            self.group.command(
                BashShellArguments(command="compileall", paths=["src"]),
                authorization=self.authorization,
            )
        self.assertEqual(self.sandbox.kill_calls, 1)

    def test_code_policy_rejects_import_before_transport_call(self):
        for code in ("import os", "open('x')", "__import__('os').system('id')"):
            with self.subTest(code=code), self.assertRaises(RemoteSandboxToolError):
                self.group.code_interpreter(
                    CodeInterpreterArguments(code=code),
                    authorization=self.authorization,
                )
        self.assertIsNone(self.transport)

    def test_sdk_transport_maps_files_commands_and_binds_workspace(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        read = transport.read_file(
            self.workspace_root + "/README.md",
            offset=0,
            max_bytes=64,
            timeout_seconds=4,
        )
        self.assertEqual(read.data, b"hello\n")
        created = transport.write_file(
            self.workspace_root + "/new.txt",
            b"new",
            overwrite=False,
            timeout_seconds=4,
        )
        self.assertEqual(created.bytes_written, 3)
        with self.assertRaises(FileExistsError):
            transport.write_file(
                self.workspace_root + "/new.txt",
                b"newer",
                overwrite=False,
                timeout_seconds=4,
            )
        listed = transport.list_files(
            "**/*",
            workspace_root=self.workspace_root,
            max_results=10,
            timeout_seconds=4,
            path_predicate=lambda _path, _is_directory: True,
        )
        self.assertEqual([entry.path for entry in listed.entries], sorted(entry.path for entry in listed.entries))
        list_calls = [payload for name, payload in sdk.files.calls if name == "list"]
        self.assertTrue(list_calls)
        self.assertTrue(all(payload[1] == 1 for payload in list_calls))
        result = transport.run(
            "compileall",
            paths=(self.workspace_root + "/README.md",),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=128,
        )
        self.assertEqual(result.exit_code, 0)
        runner_call = next(
            call for call in sdk.commands.calls if "command_runner.py" in call[0]
        )
        self.assertEqual(runner_call[1][0], self.workspace_root)
        self.assertEqual(runner_call[1][3], "user")

    def test_sdk_transport_bootstraps_missing_trusted_venv_as_root_without_clear(self):
        sdk = FakeSdkSandbox(self.workspace_root)

        transport = E2BSandboxTransport(
            sdk,
            self.workspace_root,
            runtime_venv_dir="/opt/agnet-study/runtime/venv",
            runtime_user="agentuser",
        )

        commands = sdk.commands.calls
        create = next(call for call in commands if " -m venv " in call[0])
        freeze = next(call for call in commands if "os.chown(root,0,0)" in call[0])
        runtime_probe = next(
            call
            for call in commands
            if "sys.executable" in call[0]
            and "/opt/agnet-study/runtime/venv/bin/python" in call[0]
        )
        self.assertEqual(create[1][3], "root")
        self.assertEqual(freeze[1][3], "root")
        self.assertEqual(runtime_probe[1][3], "agentuser")
        self.assertNotIn("--clear", create[0])
        self.assertEqual(
            transport.runtime_identity.python_path,
            "/opt/agnet-study/runtime/venv/bin/python",
        )
        self.assertEqual(transport.runtime_identity.runtime_user, "agentuser")
        transport.run(
            "compileall",
            paths=(self.workspace_root + "/README.md",),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=128,
        )
        runner = next(
            call for call in sdk.commands.calls if "command_runner.py" in call[0]
        )
        self.assertEqual(runner[1][3], "agentuser")
        self.assertNotEqual(runner[1][3], "root")

    def test_runtime_missing_path_uses_exists_not_provider_exception_type(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        runtime_root = "/opt/agnet-study/runtime/venv"
        sdk.files.exists_overrides[runtime_root] = False
        sdk.files.get_info_errors[runtime_root] = FakeProviderFileNotFound(
            "provider-secret-must-not-escape"
        )

        transport = E2BSandboxTransport(
            sdk,
            self.workspace_root,
            runtime_venv_dir=runtime_root,
            runtime_user="agentuser",
        )

        self.assertEqual(transport.runtime_identity.runtime_user, "agentuser")
        self.assertIn(
            ("exists", (runtime_root, 30, None)),
            sdk.files.calls,
        )
        self.assertNotIn(
            ("get_info", (runtime_root, 30, None)),
            sdk.files.calls,
        )
        self.assertTrue(
            any(" -I -m venv " in command for command, _ in sdk.commands.calls)
        )

    def test_runtime_exists_requires_strict_bool_and_safe_success(self):
        runtime_root = "/opt/agnet-study/runtime/venv"
        for invalid in (None, 0, 1, "false"):
            with self.subTest(invalid=invalid):
                sdk = FakeSdkSandbox(self.workspace_root)
                sdk.files.exists_overrides[runtime_root] = invalid
                with self.assertRaises(ValueError) as caught:
                    E2BSandboxTransport(sdk, self.workspace_root)
                self.assertNotIn(repr(invalid), str(caught.exception))
                self.assertFalse(
                    any(
                        " -I -m venv " in command
                        for command, _ in sdk.commands.calls
                    )
                )

        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.exists_errors[runtime_root] = RuntimeError(
            "provider-secret-must-not-escape"
        )
        with self.assertRaises(ValueError) as caught:
            E2BSandboxTransport(sdk, self.workspace_root)
        self.assertNotIn("provider-secret-must-not-escape", str(caught.exception))
        self.assertFalse(
            any(" -I -m venv " in command for command, _ in sdk.commands.calls)
        )

    def test_runtime_exists_true_get_info_failure_is_toctou_not_create(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        runtime_root = "/opt/agnet-study/runtime/venv"
        sdk.files.exists_overrides[runtime_root] = True
        sdk.files.get_info_errors[runtime_root] = FakeProviderFileNotFound(
            "provider-secret-must-not-escape"
        )

        with self.assertRaises(ValueError) as caught:
            E2BSandboxTransport(sdk, self.workspace_root)

        self.assertNotIn("provider-secret-must-not-escape", str(caught.exception))
        self.assertIn(("get_info", (runtime_root, 30, None)), sdk.files.calls)
        self.assertFalse(
            any(" -I -m venv " in command for command, _ in sdk.commands.calls)
        )

    def test_existing_untrusted_runtime_fails_closed_without_repair_or_clear(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.add("/opt/agnet-study/runtime/venv")
        sdk.commands.fail_runtime_inspection = True

        with self.assertRaises(ValueError):
            E2BSandboxTransport(sdk, self.workspace_root)

        commands = [call[0] for call in sdk.commands.calls]
        self.assertFalse(any(" -m venv " in command for command in commands))
        self.assertFalse(any("os.chown(root,0,0)" in command for command in commands))
        self.assertFalse(any("--clear" in command for command in commands))

    def test_preinstalled_trusted_runtime_is_verified_without_recreation(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.add("/opt/agnet-study/runtime/venv")

        transport = E2BSandboxTransport(
            sdk,
            self.workspace_root,
            runtime_user="agentuser",
        )

        commands = sdk.commands.calls
        self.assertFalse(any(" -m venv " in command for command, _ in commands))
        self.assertFalse(
            any("os.chown(root,0,0)" in command for command, _ in commands)
        )
        runtime_probe = next(
            call
            for call in commands
            if "sys.executable" in call[0]
            and "/opt/agnet-study/runtime/venv/bin/python" in call[0]
        )
        inspection = next(
            call for call in commands if "metadata_digest" in call[0]
        )
        self.assertEqual(runtime_probe[1][3], "agentuser")
        self.assertEqual(inspection[1][3], "root")
        self.assertEqual(transport.runtime_identity.runtime_user, "agentuser")

    def test_runtime_inspection_requires_root_owner_and_no_group_world_write(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        E2BSandboxTransport(sdk, self.workspace_root)

        inspection_command = next(
            command
            for command, _payload in sdk.commands.calls
            if "metadata_digest" in command
        )
        self.assertIn("rs.st_uid!=0 or (rs.st_mode&0o022)", inspection_command)
        self.assertIn("s.st_uid!=0", inspection_command)
        self.assertIn("s.st_mode&0o022", inspection_command)
        self.assertIn("stat.S_ISREG(ps.st_mode)", inspection_command)
        self.assertIn("ps.st_uid!=0 or (ps.st_mode&0o022)", inspection_command)

    def test_runtime_inspection_allows_only_bounded_root_owned_symlinks(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        E2BSandboxTransport(sdk, self.workspace_root)

        inspection_command = next(
            command
            for command, _payload in sdk.commands.calls
            if "metadata_digest" in command
        )
        self.assertIn("if stat.S_ISLNK(s.st_mode)", inspection_command)
        self.assertIn(
            "os.path.commonpath((root,target))==root",
            inspection_command,
        )
        self.assertIn("if not inside and target!=rp", inspection_command)
        self.assertIn("ts=os.stat(target)", inspection_command)
        self.assertIn("ts.st_uid!=0 or (ts.st_mode&0o022)", inspection_command)
        self.assertIn("if not inside and not stat.S_ISREG(ts.st_mode)", inspection_command)
        self.assertNotIn(
            "s.st_uid==0 and not(s.st_mode&0o022)",
            inspection_command,
        )
        command_parts = shlex.split(inspection_command, posix=True)
        inspection_source = command_parts[command_parts.index("-c") + 1]
        compile(inspection_source, "<runtime-inspection>", "exec")

    def test_runtime_inspection_rejects_symlink_root_and_special_entries(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        E2BSandboxTransport(sdk, self.workspace_root)

        inspection_command = next(
            command
            for command, _payload in sdk.commands.calls
            if "metadata_digest" in command
        )
        self.assertIn("not stat.S_ISDIR(rs.st_mode)", inspection_command)
        self.assertIn(
            "not stat.S_ISREG(s.st_mode) and not stat.S_ISDIR(s.st_mode)",
            inspection_command,
        )

    def test_runtime_identity_digest_covers_external_interpreter_metadata(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        E2BSandboxTransport(sdk, self.workspace_root)

        inspection_command = next(
            command
            for command, _payload in sdk.commands.calls
            if "metadata_digest" in command
        )
        self.assertIn("@resolved_python", inspection_command)
        self.assertIn("ps.st_dev,ps.st_ino,ps.st_mode", inspection_command)

    def test_version_listing_limit_crosses_real_e2b_transport_boundary(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.data.clear()
        sdk.files.dirs.add("/mnt/oss")
        transport = E2BSandboxTransport(
            sdk,
            self.workspace_root,
            persistence_root="/mnt/oss",
        )
        body = b"seed\n"
        snapshot = WorkspaceSeedSnapshot((WorkspaceSeedEntry(
            "README.md",
            len(body),
            hashlib.sha256(body).hexdigest(),
            body,
        ),))
        session_id = self.workspace_root.rsplit("/", 1)[-1]
        lease = ExecutionLease(
            WorkspaceDescription(
                "aliyun-sandbox",
                session_id,
                self.workspace_root,
                "/mnt/oss",
                True,
            ),
            sandbox_id="sandbox-test",
        )

        with mock.patch.object(
            transport,
            "workspace_version_list_files",
            wraps=transport.workspace_version_list_files,
        ) as version_list:
            WorkspaceVersionCoordinator(snapshot)(transport, lease)

        self.assertEqual(
            MAX_VERSION_ENTRIES,
            E2BSandboxTransport._MAX_PERSISTENCE_RESULTS,
        )
        version_list.assert_called_once()
        self.assertEqual(
            version_list.call_args.kwargs["max_results"],
            E2BSandboxTransport._MAX_PERSISTENCE_RESULTS,
        )

    def test_runtime_rejects_interpreter_that_claims_a_different_path(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.commands.runtime_probe_executable = "/opt/agnet-study/runtime/fake-python"

        with self.assertRaises(ValueError):
            E2BSandboxTransport(sdk, self.workspace_root)

        self.assertFalse(
            any("code_runner.py" in command for command, _ in sdk.commands.calls)
        )

    def test_runtime_same_path_hash_metadata_and_realpath_tampering_is_rejected(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        mutations = (
            ("runtime_metadata_digest", "d" * 64),
            ("runtime_executable_sha256", "e" * 64),
            ("runtime_resolved_python", "/usr/bin/python3.12"),
        )
        for attribute, value in mutations:
            with self.subTest(attribute=attribute):
                setattr(sdk.commands, attribute, value)
                with self.assertRaises(ValueError):
                    transport.run(
                        "compileall",
                        paths=(self.workspace_root + "/README.md",),
                        code=None,
                        cwd=self.workspace_root,
                        timeout_seconds=4,
                        max_output_bytes=128,
                    )
                setattr(
                    sdk.commands,
                    attribute,
                    {
                        "runtime_metadata_digest": "c" * 64,
                        "runtime_executable_sha256": "b" * 64,
                        "runtime_resolved_python": "/usr/bin/python3.11",
                    }[attribute],
                )

        self.assertFalse(
            any("command_runner.py" in command for command, _payload in sdk.commands.calls)
        )

    def test_runtime_root_rejects_active_workspace_and_oss_descendants(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.update({"/mnt", "/mnt/oss"})
        for runtime_root, persistence_root in (
            (self.workspace_root + "/.venv", None),
            ("/mnt/oss/runtime/venv", "/mnt/oss"),
        ):
            with self.subTest(runtime_root=runtime_root), self.assertRaises(ValueError):
                E2BSandboxTransport(
                    sdk,
                    self.workspace_root,
                    persistence_root,
                    runtime_venv_dir=runtime_root,
                )

    def test_persistence_root_is_hardened_and_probed_before_runtime_ready(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.update({"/mnt", "/mnt/oss"})

        transport = E2BSandboxTransport(
            sdk,
            self.workspace_root,
            "/mnt/oss",
            runtime_user="agentuser",
        )

        commands = sdk.commands.calls
        root_probe_index = next(
            index
            for index, call in enumerate(commands)
            if "persistence-root-ok" in call[0]
        )
        runtime_probe_index = next(
            index
            for index, call in enumerate(commands)
            if "persistence-runtime-denied" in call[0]
        )
        cleanup_index = next(
            index
            for index, call in enumerate(commands)
            if "persistence-probe-cleanup" in call[0]
        )
        venv_index = next(
            index for index, call in enumerate(commands) if " -m venv " in call[0]
        )
        self.assertLess(root_probe_index, runtime_probe_index)
        self.assertLess(runtime_probe_index, cleanup_index)
        self.assertLess(cleanup_index, venv_index)

        root_probe = commands[root_probe_index]
        runtime_probe = commands[runtime_probe_index]
        cleanup = commands[cleanup_index]
        self.assertEqual(root_probe[1][3], "root")
        self.assertEqual(runtime_probe[1][3], "agentuser")
        self.assertEqual(cleanup[1][3], "root")
        self.assertIn("os.chown(root,0,0)", root_probe[0])
        self.assertIn("os.chmod(root,0o700)", root_probe[0])
        self.assertIn("stat.S_IMODE", root_probe[0])
        self.assertIn("os.listdir(fd)", root_probe[0])
        self.assertIn("os.listdir(root)", runtime_probe[0])
        self.assertIn("os.O_RDONLY|os.O_NOFOLLOW", runtime_probe[0])
        self.assertIn("os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW", runtime_probe[0])

        persistence_calls = [
            payload
            for name, payload in sdk.files.calls
            if name in {"get_info", "list"} and payload[0].startswith("/mnt/oss")
        ]
        self.assertTrue(persistence_calls)
        self.assertTrue(all(payload[-1] == "root" for payload in persistence_calls))
        self.assertEqual(transport.runtime_identity.runtime_user, "agentuser")

    def test_persistence_isolation_failure_is_fixed_and_prevents_runtime_ready(self):
        failures = (
            ("root", (1, "", "SECRET chmod/stat failure"), False),
            ("runtime", (1, "", "SECRET access unexpectedly allowed"), True),
            ("cleanup", (1, "", "SECRET cleanup failure"), True),
        )
        for stage, result, cleanup_expected in failures:
            with self.subTest(stage=stage):
                sdk = FakeSdkSandbox(self.workspace_root)
                sdk.files.dirs.update({"/mnt", "/mnt/oss"})
                if stage == "root":
                    sdk.commands.persistence_root_result = result
                elif stage == "runtime":
                    sdk.commands.persistence_runtime_result = result
                else:
                    sdk.commands.persistence_cleanup_result = result

                with self.assertRaisesRegex(
                    ValueError,
                    "^persistence 访问隔离失败。$",
                ) as caught:
                    E2BSandboxTransport(
                        sdk,
                        self.workspace_root,
                        "/mnt/oss",
                        runtime_user="agentuser",
                    )

                self.assertNotIn("SECRET", str(caught.exception))
                self.assertNotIn("/mnt/oss", str(caught.exception))
                self.assertFalse(
                    any(" -m venv " in command for command, _ in sdk.commands.calls)
                )
                cleanup_called = any(
                    "persistence-probe-cleanup" in command
                    for command, _ in sdk.commands.calls
                )
                self.assertEqual(cleanup_called, cleanup_expected)

    def test_unbound_transport_does_not_run_persistence_access_probes(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        E2BSandboxTransport(sdk, self.workspace_root)
        self.assertFalse(
            any(
                marker in command
                for command, _ in sdk.commands.calls
                for marker in (
                    "persistence-root-ok",
                    "persistence-runtime-denied",
                    "persistence-probe-cleanup",
                )
            )
        )

    def test_sdk_transport_rejects_symlink_and_workspace_mismatch(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.symlink = True
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        with self.assertRaises(ValueError):
            transport.read_file(
                self.workspace_root + "/link",
                offset=0,
                max_bytes=10,
                timeout_seconds=4,
            )

    def test_sdk_transport_persistence_is_bound_bounded_and_create_only(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.update({"/mnt", "/mnt/oss"})
        transport = E2BSandboxTransport(sdk, self.workspace_root, "/mnt/oss")
        checkpoint_root = (
            "/mnt/oss/.agnet-study/checkpoints/v1/"
            + "a" * 32
            + "/"
            + "b" * 64
        )
        path = checkpoint_root + "/files/pkg/a.txt"
        written = transport.persistence_write_file(
            path,
            b"body",
            overwrite=False,
            timeout_seconds=4,
        )
        self.assertEqual(written.bytes_written, 4)
        read = transport.persistence_read_file(
            path,
            offset=0,
            max_bytes=4,
            timeout_seconds=4,
        )
        self.assertEqual((read.data, read.total_bytes), (b"body", 4))
        listed = transport.persistence_list_files(
            checkpoint_root,
            max_results=100,
            max_depth=64,
            timeout_seconds=4,
        )
        self.assertFalse(listed.truncated)
        self.assertIn(path, [entry.path for entry in listed.entries])
        self.assertTrue(any(entry.is_directory for entry in listed.entries))
        with self.assertRaises(FileExistsError):
            transport.persistence_write_file(
                path,
                b"replacement",
                overwrite=False,
                timeout_seconds=4,
            )
        with self.assertRaises(ValueError):
            transport.persistence_write_file(
                path,
                b"replacement",
                overwrite=True,
                timeout_seconds=4,
            )
        persistence_calls = [
            payload
            for name, payload in sdk.files.calls
            if name in {"get_info", "list", "read", "write", "make_dir"}
            and payload[0].startswith("/mnt/oss")
        ]
        self.assertTrue(persistence_calls)
        self.assertTrue(all(payload[-1] == "root" for payload in persistence_calls))

    def test_sdk_transport_persistence_fails_closed_on_unbound_escape_and_types(self):
        checkpoint_root = (
            "/mnt/oss/.agnet-study/checkpoints/v1/"
            + "a" * 32
            + "/"
            + "b" * 64
        )
        unbound = E2BSandboxTransport(FakeSdkSandbox(self.workspace_root), self.workspace_root)
        with self.assertRaises(ValueError):
            unbound.persistence_list_files(
                checkpoint_root,
                max_results=10,
                max_depth=64,
                timeout_seconds=4,
            )

        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.update({"/mnt", "/mnt/oss"})
        transport = E2BSandboxTransport(sdk, self.workspace_root, "/mnt/oss")
        invalid_paths = (
            "/mnt/oss/escape.txt",
            checkpoint_root + "/../escape.txt",
            checkpoint_root + "\\escape.txt",
            "/mnt/oss/.agnet-study/checkpoints/v1/" + "A" * 32 + "/" + "b" * 64 + "/x",
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                transport.persistence_write_file(
                    path,
                    b"x",
                    overwrite=False,
                    timeout_seconds=4,
                )

        namespace = "/mnt/oss/.agnet-study"
        sdk.files.entry_overrides[namespace] = SdkEntry(
            namespace,
            "symlink",
            target="/workspace",
        )
        with self.assertRaises(ValueError):
            transport.persistence_write_file(
                checkpoint_root + "/files/a.txt",
                b"x",
                overwrite=False,
                timeout_seconds=4,
            )

        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.update({"/mnt", "/mnt/oss"})
        sdk.files.entry_overrides["/mnt/oss"] = SdkEntry(
            "/mnt/oss",
            "symlink",
            target="/workspace",
        )
        with self.assertRaises(ValueError):
            E2BSandboxTransport(sdk, self.workspace_root, "/mnt/oss")

        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.update({"/mnt", "/mnt/oss"})
        transport = E2BSandboxTransport(sdk, self.workspace_root, "/mnt/oss")
        special = checkpoint_root + "/fifo"
        sdk.files.dirs.update(
            {
                checkpoint_root,
                posixpath.dirname(checkpoint_root),
                posixpath.dirname(posixpath.dirname(checkpoint_root)),
                "/mnt/oss/.agnet-study",
                "/mnt/oss/.agnet-study/checkpoints",
                "/mnt/oss/.agnet-study/checkpoints/v1",
                "/mnt/oss/.agnet-study/checkpoints/v1/" + "a" * 32,
            }
        )
        sdk.files.entry_overrides[special] = SdkEntry(special, "fifo")
        with self.assertRaises(ValueError):
            transport.persistence_list_files(
                checkpoint_root,
                max_results=10,
                max_depth=64,
                timeout_seconds=4,
            )
        with self.assertRaises(ValueError):
            transport.list_files(
                "**/*",
                workspace_root="/workspace/other",
                max_results=10,
                timeout_seconds=4,
                path_predicate=lambda _path, _is_directory: True,
            )

        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.symlink_parent = True
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        with self.assertRaises(ValueError):
            transport.write_file(
                self.workspace_root + "/linkdir/escape.txt",
                b"x",
                overwrite=False,
                timeout_seconds=4,
            )

    def test_sdk_transport_creates_workspace_and_nested_parent_safely(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        transport.write_file(
            self.workspace_root + "/nested/deep.txt",
            b"x",
            overwrite=False,
            timeout_seconds=4,
        )
        self.assertIn(self.workspace_root + "/nested", sdk.files.dirs)
        with self.assertRaises(FileExistsError):
            transport.write_file(
                self.workspace_root + "/missing.txt",
                b"x",
                overwrite=True,
                timeout_seconds=4,
            )
        self.assertTrue(any(name == "make_dir" for name, _ in sdk.files.calls))

    def test_sdk_transport_creates_and_then_verifies_missing_workspace_root(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.dirs.clear()
        sdk.files.data.clear()
        transport = E2BSandboxTransport(sdk, self.workspace_root)

        transport._ensure_workspace(4)

        self.assertIn("/workspace", sdk.files.dirs)
        self.assertIn(self.workspace_root, sdk.files.dirs)
        root_calls = [
            (name, payload[0])
            for name, payload in sdk.files.calls
            if name in {"make_dir", "get_info"} and payload[0] == "/workspace"
        ]
        self.assertEqual(root_calls[:2], [("make_dir", "/workspace"), ("get_info", "/workspace")])

    def test_sdk_transport_rejects_workspace_root_symlink_after_make_dir(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.workspace_symlink = True

        with self.assertRaises(ValueError):
            E2BSandboxTransport(sdk, self.workspace_root)

        root_calls = [
            (name, payload[0])
            for name, payload in sdk.files.calls
            if name in {"make_dir", "get_info"} and payload[0] == "/workspace"
        ]
        self.assertEqual(root_calls, [("make_dir", "/workspace"), ("get_info", "/workspace")])

    def test_sdk_transport_rejects_oversized_file_before_download(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        oversized = self.workspace_root + "/large.txt"
        sdk.files.data[oversized] = b"x" * (8 * 1024 * 1024 + 1)
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        with self.assertRaises(ValueError):
            transport.read_file(oversized, offset=0, max_bytes=10, timeout_seconds=4)
        self.assertFalse(any(name == "read" and call[0] == oversized for name, call in sdk.files.calls))

    def test_sdk_transport_rejects_oversized_edit_before_write(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        path = self.workspace_root + "/editable.txt"
        sdk.files.data[path] = b"a" * (8 * 1024 * 1024 - 2) + b"x"
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        with self.assertRaises(ValueError):
            transport.edit_file(
                path,
                operation="replace_text",
                old_text="x",
                new_text="b" * (1024 * 1024),
                expected_replacements=1,
                position=None,
                timeout_seconds=4,
            )
        self.assertFalse(any(name == "write" and call[0] == path for name, call in sdk.files.calls))

    def test_sdk_transport_uses_restricted_runner_and_cleans_generated_files(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        result = transport.run(
            "python",
            paths=(),
            code="1+1",
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=1024,
            max_output_characters=1024,
            max_memory_mb=64,
            max_processes=1,
        )
        self.assertEqual(result.result, "2")
        command = sdk.commands.calls[-1][0]
        self.assertIn("code_runner.py", command)
        self.assertNotIn("1+1", command)
        self.assertNotIn(self.workspace_root + "/.agent-internal/code_runner.py", sdk.files.data)
        runner_payloads = [
            payload
            for path, payload in sdk.files.write_payloads
            if path.endswith("/payload.json")
        ]
        self.assertEqual(len(runner_payloads), 1)
        self.assertEqual(
            json.loads(runner_payloads[0]),
            {
                "code": "1+1",
                "max_output_characters": 1024,
                "max_memory_mb": 64,
                "max_processes": 1,
                "timeout_seconds": 4,
            },
        )

    def test_sdk_transport_preserves_code_runner_truncation_at_host_boundary(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.commands.runner_output = json.dumps(
            {
                "exit_code": 0,
                "stdout": "x" * 1024,
                "result": "",
                "stderr": "",
                "truncated": True,
            },
            separators=(",", ":"),
        )
        transport = E2BSandboxTransport(sdk, self.workspace_root)

        result = transport.run(
            "python",
            paths=(),
            code="print('bounded')",
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=1024,
            max_output_characters=1024,
            max_memory_mb=64,
            max_processes=1,
        )

        self.assertEqual(result.stdout, "x" * 1024)
        self.assertTrue(result.truncated)

    def test_sdk_transport_preserves_command_runner_truncation_at_host_boundary(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.commands.command_runner_output = json.dumps(
            {
                "exit_code": 0,
                "stdout": "y" * 1024,
                "stderr": "",
                "truncated": True,
            },
            separators=(",", ":"),
        )
        transport = E2BSandboxTransport(sdk, self.workspace_root)

        result = transport.run(
            "compileall",
            paths=(self.workspace_root + "/README.md",),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=1024,
        )

        self.assertEqual(result.stdout, "y" * 1024)
        self.assertTrue(result.truncated)

    def test_sdk_transport_infers_truncation_for_oversized_runner_output(self):
        cases = (
            ("python", "runner_output", "x"),
            ("compileall", "command_runner_output", "y"),
        )
        for command, output_attribute, character in cases:
            with self.subTest(command=command):
                sdk = FakeSdkSandbox(self.workspace_root)
                payload = {
                    "exit_code": 0,
                    "stdout": character * 1025,
                    "stderr": "",
                    "truncated": False,
                }
                if command == "python":
                    payload["result"] = ""
                setattr(
                    sdk.commands,
                    output_attribute,
                    json.dumps(payload, separators=(",", ":")),
                )
                transport = E2BSandboxTransport(sdk, self.workspace_root)

                run_kwargs = {
                    "paths": () if command == "python" else (self.workspace_root + "/README.md",),
                    "code": "print('bounded')" if command == "python" else None,
                    "cwd": self.workspace_root,
                    "timeout_seconds": 4,
                    "max_output_bytes": 1024,
                }
                if command == "python":
                    run_kwargs.update(
                        max_output_characters=1024,
                        max_memory_mb=64,
                        max_processes=1,
                    )
                result = transport.run(command, **run_kwargs)

                self.assertEqual(result.stdout, character * 1024)
                self.assertTrue(result.truncated)

    def test_sdk_transport_rejects_non_boolean_runner_truncation_flags(self):
        invalid_values = (1, "true", None, {}, [])
        for invalid_value in invalid_values:
            with self.subTest(path="code", value_type=type(invalid_value).__name__):
                sdk = FakeSdkSandbox(self.workspace_root)
                sdk.commands.runner_output = json.dumps(
                    {
                        "exit_code": 0,
                        "stdout": "",
                        "result": "",
                        "stderr": "",
                        "truncated": invalid_value,
                    },
                    separators=(",", ":"),
                )
                transport = E2BSandboxTransport(sdk, self.workspace_root)

                result = transport.run(
                    "python",
                    paths=(),
                    code="1+1",
                    cwd=self.workspace_root,
                    timeout_seconds=4,
                    max_output_bytes=1024,
                    max_output_characters=1024,
                    max_memory_mb=64,
                    max_processes=1,
                )

                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.result, "")
                self.assertEqual(result.stderr, "远端受限 Python 返回无效结果。")
                self.assertFalse(result.truncated)

            with self.subTest(path="command", value_type=type(invalid_value).__name__):
                sdk = FakeSdkSandbox(self.workspace_root)
                sdk.commands.command_runner_output = json.dumps(
                    {
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                        "truncated": invalid_value,
                    },
                    separators=(",", ":"),
                )
                transport = E2BSandboxTransport(sdk, self.workspace_root)

                with self.assertRaisesRegex(
                    ValueError,
                    "^远端结构化命令返回无效结果。$",
                ):
                    transport.run(
                        "compileall",
                        paths=(self.workspace_root + "/README.md",),
                        code=None,
                        cwd=self.workspace_root,
                        timeout_seconds=4,
                        max_output_bytes=1024,
                    )

        with self.assertRaisesRegex(ValueError, "^远端命令截断标记类型无效。$"):
            E2BSandboxTransport._bounded_command_result(
                0,
                "",
                "",
                1024,
                source_truncated=1,
            )

    def test_sdk_runner_provider_deadline_adds_only_bounded_grace(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        transport = E2BSandboxTransport(sdk, self.workspace_root)

        transport.run(
            "unittest",
            paths=(self.workspace_root,),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=120,
            max_output_bytes=1024,
        )
        structured_call = next(
            call
            for call in sdk.commands.calls
            if "command_runner.py" in call[0]
        )
        self.assertEqual(
            structured_call[1],
            (self.workspace_root, 125, 125, "user"),
        )
        structured_payload = next(
            json.loads(payload)
            for path, payload in sdk.files.write_payloads
            if path.endswith("/payload.json")
            and json.loads(payload).get("command") == "unittest"
        )
        self.assertEqual(structured_payload["timeout"], 120)

        transport.run(
            "python",
            paths=(),
            code="1+1",
            cwd=self.workspace_root,
            timeout_seconds=10,
            max_output_bytes=1024,
            max_output_characters=1024,
            max_memory_mb=64,
            max_processes=1,
        )
        code_call = next(
            call
            for call in sdk.commands.calls
            if "code_runner.py" in call[0]
        )
        self.assertEqual(
            code_call[1],
            (self.workspace_root, 15, 15, "user"),
        )
        code_payload = next(
            json.loads(payload)
            for path, payload in sdk.files.write_payloads
            if path.endswith("/payload.json")
            and "timeout_seconds" in json.loads(payload)
        )
        self.assertEqual(code_payload["timeout_seconds"], 10)

        runner_calls_before = len([
            call
            for call in sdk.commands.calls
            if "command_runner.py" in call[0]
        ])
        for invalid_timeout in (0, 121, True, 1.5):
            with self.subTest(timeout=invalid_timeout), self.assertRaises(ValueError):
                transport.run(
                    "unittest",
                    paths=(self.workspace_root,),
                    code=None,
                    cwd=self.workspace_root,
                    timeout_seconds=invalid_timeout,  # type: ignore[arg-type]
                    max_output_bytes=1024,
                )
        runner_calls_after = len([
            call
            for call in sdk.commands.calls
            if "command_runner.py" in call[0]
        ])
        self.assertEqual(runner_calls_after, runner_calls_before)

    def test_sdk_transport_runner_cleanup_failure_is_safe_error(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.remove_error = True
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        with self.assertRaises(ValueError) as caught:
            transport.run(
                "python",
                paths=(),
                code="1+1",
                cwd=self.workspace_root,
                timeout_seconds=4,
                max_output_bytes=1024,
                max_output_characters=1024,
                max_memory_mb=64,
                max_processes=1,
            )
        self.assertNotIn("secret.invalid", str(caught.exception))

    def test_sdk_format_json_candidate_protocol_and_exact_cleanup(self):
        source_path = self.workspace_root + "/input.json"
        before = b'{"z":1,"a":2}\n'
        after = b'{\n  "a": 2,\n  "z": 1\n}\n'
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.data[source_path] = before
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        observed_staging: list[tuple[str, str]] = []

        class Handle:
            def __init__(self, output: str) -> None:
                self.output = output

            def wait(self):
                return SandboxCommandResult(0, self.output, "")

        def start_runner(**kwargs):
            payload = json.loads(kwargs["payload"])
            staged = payload["staging_root"] + "/00.json"
            sdk.files.data[staged] = after
            observed_staging.append((payload["staging_root"], staged))
            item = {
                "after_sha256": hashlib.sha256(after).hexdigest(),
                "before_sha256": hashlib.sha256(before).hexdigest(),
                "path": source_path,
                "size": len(after),
                "staged": staged,
            }
            return Handle(json.dumps({"files": [item]}, separators=(",", ":")))

        with mock.patch.object(transport, "_start_runner", side_effect=start_runner):
            result = transport.format_json_candidates(
                (source_path,), timeout_seconds=7
            )

        self.assertEqual(len(result.files), 1)
        self.assertEqual(result.files[0].path, source_path)
        self.assertEqual(result.files[0].after, after)
        staging_root, staged = observed_staging[0]
        self.assertNotIn(staged, sdk.files.data)
        self.assertNotIn(staging_root, sdk.files.dirs)
        self.assertEqual(sdk.files.data[source_path], before)

    def test_sdk_format_json_rejects_malformed_runner_bindings_and_cleans_staging(self):
        source_path = self.workspace_root + "/input.json"
        before = b'{"z":1,"a":2}\n'
        after = b'{\n  "a": 2,\n  "z": 1\n}\n'

        class Handle:
            def __init__(self, output: str) -> None:
                self.output = output

            def wait(self):
                return SandboxCommandResult(0, self.output, "")

        def run_case(case: str) -> tuple[FakeSdkSandbox, str, str]:
            sdk = FakeSdkSandbox(self.workspace_root)
            sdk.files.data[source_path] = before
            transport = E2BSandboxTransport(sdk, self.workspace_root)
            staging: list[tuple[str, str]] = []

            def start_runner(**kwargs):
                payload = json.loads(kwargs["payload"])
                staging_root = payload["staging_root"]
                staged = staging_root + "/00.json"
                sdk.files.data[staged] = after
                staging.append((staging_root, staged))
                item = {
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                    "before_sha256": hashlib.sha256(before).hexdigest(),
                    "path": source_path,
                    "size": len(after),
                    "staged": staged,
                }
                if case == "error":
                    output = '{"error":"format_json 候选生成失败。"}'
                elif case == "duplicate_key":
                    output = '{"files":[],"files":[]}'
                else:
                    if case == "path":
                        item["path"] = self.workspace_root + "/other.json"
                    elif case == "digest":
                        item["after_sha256"] = "0" * 64
                    elif case == "size":
                        item["size"] = len(after) + 1
                    elif case == "staged":
                        item["staged"] = staging_root + "/wrong.json"
                    output = json.dumps({"files": [item]}, separators=(",", ":"))
                return Handle(output)

            with (
                mock.patch.object(
                    transport, "_start_runner", side_effect=start_runner
                ),
                self.assertRaisesRegex(
                    ValueError, "format_json 候选生成失败。"
                ),
            ):
                transport.format_json_candidates(
                    (source_path,), timeout_seconds=7
                )
            return sdk, staging[0][0], staging[0][1]

        for case in ("error", "duplicate_key", "path", "digest", "size", "staged"):
            with self.subTest(case=case):
                sdk, staging_root, staged = run_case(case)
                self.assertNotIn(staged, sdk.files.data)
                self.assertNotIn(staging_root, sdk.files.dirs)
                self.assertEqual(sdk.files.data[source_path], before)

    def test_sdk_format_json_cleanup_failure_is_safe_and_keeps_active_untouched(self):
        source_path = self.workspace_root + "/input.json"
        before = b'{"z":1,"a":2}\n'
        after = b'{\n  "a": 2,\n  "z": 1\n}\n'
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.data[source_path] = before
        sdk.files.remove_error = True
        transport = E2BSandboxTransport(sdk, self.workspace_root)

        class Handle:
            def wait(self):
                return SandboxCommandResult(0, json.dumps({
                    "files": [{
                        "after_sha256": hashlib.sha256(after).hexdigest(),
                        "before_sha256": hashlib.sha256(before).hexdigest(),
                        "path": source_path,
                        "size": len(after),
                        "staged": staging[0] + "/00.json",
                    }],
                }, separators=(",", ":")), "")

        staging: list[str] = []

        def start_runner(**kwargs):
            payload = json.loads(kwargs["payload"])
            staging.append(payload["staging_root"])
            sdk.files.data[staging[0] + "/00.json"] = after
            return Handle()

        with (
            mock.patch.object(transport, "_start_runner", side_effect=start_runner),
            self.assertRaisesRegex(
                ValueError, "format_json 候选生成失败。"
            ) as caught,
        ):
            transport.format_json_candidates((source_path,), timeout_seconds=7)
        self.assertNotIn("secret.invalid", str(caught.exception))
        self.assertEqual(sdk.files.data[source_path], before)

    def test_sdk_transport_rollback_delete_requires_exact_regular_file_and_absence(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        path = self.workspace_root + "/new.txt"
        body = b"created"
        sdk.files.data[path] = body
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        with self.assertRaises(ValueError):
            transport.rollback_delete_file(
                path, expected_sha256="0" * 64, timeout_seconds=4
            )
        self.assertIn(path, sdk.files.data)

        sdk.files.entry_overrides[path] = SdkEntry(path, "symlink", target="/mnt/oss")
        with self.assertRaises(ValueError):
            transport.rollback_delete_file(
                path,
                expected_sha256=hashlib.sha256(body).hexdigest(),
                timeout_seconds=4,
            )
        self.assertIn(path, sdk.files.data)

        sdk.files.entry_overrides.pop(path)
        transport.rollback_delete_file(
            path,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            timeout_seconds=4,
        )
        self.assertNotIn(path, sdk.files.data)
        with self.assertRaises(ValueError):
            transport.rollback_delete_file(
                path,
                expected_sha256=hashlib.sha256(body).hexdigest(),
                timeout_seconds=4,
            )

    def test_sdk_transport_version_object_rollback_delete_is_exact_and_sanitized(self):
        body = b"transaction object"
        digest = hashlib.sha256(body).hexdigest()
        root = "/mnt/oss/.agnet-study/checkpoints/v2/" + "a" * 32
        object_path = root + "/objects/" + digest
        generation_path = root + "/generations/" + "0" * 16 + "-" + digest + ".json"

        def build():
            sdk = FakeSdkSandbox(self.workspace_root)
            sdk.files.dirs.update({"/mnt", "/mnt/oss"})
            transport = E2BSandboxTransport(
                sdk,
                self.workspace_root,
                "/mnt/oss",
            )
            transport.workspace_version_write_file(
                object_path,
                body,
                overwrite=False,
                timeout_seconds=4,
            )
            return sdk, transport

        invalid_calls = (
            (generation_path, len(body), digest),
            (object_path, len(body), "0" * 64),
            (object_path, len(body) + 1, digest),
            (object_path, True, digest),
        )
        for path, expected_size, expected_digest in invalid_calls:
            with self.subTest(path=path, size=expected_size, digest=expected_digest):
                sdk, transport = build()
                with self.assertRaisesRegex(ValueError, "version object rollback 清理失败。"):
                    transport.rollback_delete_workspace_version_object(
                        path,
                        expected_size=expected_size,
                        expected_sha256=expected_digest,
                        timeout_seconds=4,
                    )
                self.assertIn(object_path, sdk.files.data)

        sdk, transport = build()
        sdk.files.entry_overrides[object_path] = SdkEntry(
            object_path,
            "symlink",
            len(body),
            target="/mnt/oss",
        )
        with self.assertRaisesRegex(ValueError, "version object rollback 清理失败。"):
            transport.rollback_delete_workspace_version_object(
                object_path,
                expected_size=len(body),
                expected_sha256=digest,
                timeout_seconds=4,
            )
        self.assertIn(object_path, sdk.files.data)

        sdk, transport = build()
        sdk.files.data[object_path] = b"x" * len(body)
        with self.assertRaisesRegex(ValueError, "version object rollback 清理失败。") as caught:
            transport.rollback_delete_workspace_version_object(
                object_path,
                expected_size=len(body),
                expected_sha256=digest,
                timeout_seconds=4,
            )
        self.assertNotIn("secret.invalid", str(caught.exception))
        self.assertIn(object_path, sdk.files.data)

        for failure in ("false", "retained", "exception"):
            with self.subTest(failure=failure):
                sdk, transport = build()
                if failure == "false":
                    sdk.files.remove_result = False
                elif failure == "retained":
                    sdk.files.remove_retains_file = True
                else:
                    sdk.files.remove_error = True
                with self.assertRaisesRegex(ValueError, "version object rollback 清理失败。") as caught:
                    transport.rollback_delete_workspace_version_object(
                        object_path,
                        expected_size=len(body),
                        expected_sha256=digest,
                        timeout_seconds=4,
                    )
                self.assertNotIn("secret.invalid", str(caught.exception))

        sdk, transport = build()
        transport.rollback_delete_workspace_version_object(
            object_path,
            expected_size=len(body),
            expected_sha256=digest,
            timeout_seconds=4,
        )
        self.assertNotIn(object_path, sdk.files.data)

    def test_sdk_transport_version_object_gc_delete_is_exact_and_sanitized(self):
        body = b"stable orphan"
        digest = hashlib.sha256(body).hexdigest()
        root = "/mnt/oss/.agnet-study/checkpoints/v2/" + "a" * 32
        object_path = root + "/objects/" + digest
        generation_path = (
            root + "/generations/" + "0" * 16 + "-" + digest + ".json"
        )

        def build():
            sdk = FakeSdkSandbox(self.workspace_root)
            sdk.files.dirs.update({"/mnt", "/mnt/oss"})
            transport = E2BSandboxTransport(
                sdk,
                self.workspace_root,
                "/mnt/oss",
            )
            transport.workspace_version_write_file(
                object_path,
                body,
                overwrite=False,
                timeout_seconds=4,
            )
            return sdk, transport

        for path, expected_size, expected_digest in (
            (generation_path, len(body), digest),
            (object_path, len(body), "0" * 64),
            (object_path, len(body) + 1, digest),
            (object_path, True, digest),
        ):
            with self.subTest(path=path, size=expected_size, digest=expected_digest):
                sdk, transport = build()
                with self.assertRaisesRegex(
                    ValueError,
                    "version object GC 清理失败。",
                ) as caught:
                    transport.gc_delete_workspace_version_object(
                        path,
                        expected_size=expected_size,
                        expected_sha256=expected_digest,
                        timeout_seconds=4,
                    )
                self.assertNotIn("secret.invalid", str(caught.exception))
                self.assertIn(object_path, sdk.files.data)

        sdk, transport = build()
        sdk.files.entry_overrides[object_path] = SdkEntry(
            object_path,
            "symlink",
            len(body),
            target="/mnt/oss",
        )
        with self.assertRaisesRegex(ValueError, "version object GC 清理失败。"):
            transport.gc_delete_workspace_version_object(
                object_path,
                expected_size=len(body),
                expected_sha256=digest,
                timeout_seconds=4,
            )
        self.assertIn(object_path, sdk.files.data)

        sdk, transport = build()
        sdk.files.data[object_path] = b"x" * len(body)
        with self.assertRaisesRegex(ValueError, "version object GC 清理失败。"):
            transport.gc_delete_workspace_version_object(
                object_path,
                expected_size=len(body),
                expected_sha256=digest,
                timeout_seconds=4,
            )
        self.assertIn(object_path, sdk.files.data)

        for failure in ("false", "retained", "exception"):
            with self.subTest(failure=failure):
                sdk, transport = build()
                if failure == "false":
                    sdk.files.remove_result = False
                elif failure == "retained":
                    sdk.files.remove_retains_file = True
                else:
                    sdk.files.remove_error = True
                with self.assertRaisesRegex(
                    ValueError,
                    "version object GC 清理失败。",
                ) as caught:
                    transport.gc_delete_workspace_version_object(
                        object_path,
                        expected_size=len(body),
                        expected_sha256=digest,
                        timeout_seconds=4,
                    )
                self.assertNotIn("secret.invalid", str(caught.exception))

        sdk, transport = build()
        transport.gc_delete_workspace_version_object(
            object_path,
            expected_size=len(body),
            expected_sha256=digest,
            timeout_seconds=4,
        )
        self.assertNotIn(object_path, sdk.files.data)

    def test_sdk_transport_command_output_is_bounded(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.commands.command_runner_output = (
            '{"exit_code":0,"stdout":"' + "x" * 4096 + '","stderr":"","truncated":false}'
        )
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        result = transport.run(
            "compileall",
            paths=(self.workspace_root + "/README.md",),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=128,
        )
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 128)
        self.assertTrue(result.truncated)

    def test_sdk_transport_grep_matches_all_occurrences_and_skips_unsafe_files(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.data[self.workspace_root + "/matches.txt"] = b"Needle needle\n"
        sdk.files.data[self.workspace_root + "/nul.txt"] = b"needle\x00"
        sdk.files.data[self.workspace_root + "/binary.txt"] = b"\xffneedle"
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        result = transport.grep(
            "needle",
            workspace_root=self.workspace_root,
            file_pattern="**/*",
            case_sensitive=False,
            max_results=10,
            timeout_seconds=4,
            path_predicate=lambda _path, _is_directory: True,
        )
        self.assertEqual(result.files_searched, 2)  # README + matches; unsafe files skipped
        self.assertEqual([match.column for match in result.matches], [1, 8])
        self.assertTrue(all(len(match.text) <= 500 for match in result.matches))

    def test_sdk_transport_applies_host_policy_before_sensitive_file_reads(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sensitive = {
            ".env": b"secret\n",
            ".envrc": b"secret\n",
            "private/server.key": b"secret\n",
            "credentials/cache.json": b"secret\n",
            "token": b"secret\n",
            ".agent/tool_policy.json": b"secret\n",
            "skills/example/SKILL.md": b"secret\n",
            "data/pending/candidate.bin": b"secret\n",
        }
        for relative, data in sensitive.items():
            sdk.files.data[self.workspace_root + "/" + relative] = data
        sdk.files.data[self.workspace_root + "/src/main.py"] = b"secret\n"
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        predicate = sandbox_tools_module._workspace_path_predicate()
        sdk.files.calls.clear()

        listed = transport.list_files(
            "**/*",
            workspace_root=self.workspace_root,
            max_results=100,
            timeout_seconds=4,
            path_predicate=predicate,
        )
        listed_relative = [
            item.path[len(self.workspace_root) + 1 :] for item in listed.entries
        ]
        self.assertEqual(listed_relative, ["README.md", "src/main.py"])

        searched = transport.grep(
            "secret",
            workspace_root=self.workspace_root,
            file_pattern="**/*",
            case_sensitive=True,
            max_results=100,
            timeout_seconds=4,
            path_predicate=predicate,
        )
        self.assertEqual(
            [item.path[len(self.workspace_root) + 1 :] for item in searched.matches],
            ["src/main.py"],
        )
        read_paths = [
            payload[0]
            for name, payload in sdk.files.calls
            if name == "read"
        ]
        self.assertNotIn(self.workspace_root + "/.env", read_paths)
        self.assertFalse(
            any(
                path.startswith(self.workspace_root + "/.agent/")
                or path.startswith(self.workspace_root + "/skills/")
                or path.startswith(self.workspace_root + "/data/pending/")
                for path in read_paths
            )
        )
        traversed_directories = [
            payload[0]
            for name, payload in sdk.files.calls
            if name == "list"
        ]
        self.assertIn(self.workspace_root + "/src", traversed_directories)
        self.assertIn(self.workspace_root + "/data", traversed_directories)
        self.assertNotIn(self.workspace_root + "/.agent", traversed_directories)
        self.assertNotIn(self.workspace_root + "/skills", traversed_directories)
        self.assertNotIn(self.workspace_root + "/data/pending", traversed_directories)

    def test_command_runner_has_wallclock_and_incremental_output_bounds(self):
        if os.name != "posix":
            with self.assertRaises(RuntimeError):
                _bounded_run(
                    [sys.executable, "-c", "print('must not run')"],
                    "/workspace",
                    5,
                    128,
                )
            return
        cwd = Path.cwd().resolve().as_posix()
        bounded = _bounded_run(
            [sys.executable, "-c", "print('x' * 10000)"], cwd, 5, 128
        )
        self.assertLessEqual(
            len(str(bounded["stdout"]).encode()) + len(str(bounded["stderr"]).encode()),
            128,
        )
        self.assertTrue(bounded["truncated"])
        started = time.monotonic()
        timeout_result = _bounded_run(
            [sys.executable, "-c", "import time; time.sleep(2)"], cwd, 1, 128
        )
        self.assertEqual(timeout_result["stderr"], "命令执行超时。")
        self.assertLess(time.monotonic() - started, 2)

    def test_command_runner_starts_posix_session_and_kills_group_on_timeout(self):
        process = _FakeRunnerProcess()
        killpg = mock.Mock(side_effect=lambda pid, sig: setattr(process, "returncode", -sig))
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.object(command_runner.os, "killpg", killpg, create=True),
            mock.patch.object(command_runner.subprocess, "Popen", return_value=process) as popen,
        ):
            result = command_runner._bounded_run(["python", "child.py"], "/workspace", 0, 128)

        self.assertEqual(result["stderr"], "命令执行超时。")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertIs(
            popen.call_args.kwargs["preexec_fn"],
            command_runner._apply_posix_resource_limits,
        )
        killpg.assert_called_with(process.pid, command_runner._SIGKILL)
        self.assertEqual(process.kill_calls, 1)

    def test_command_runner_posix_resource_limits_are_fixed_and_complete(self):
        self.assertEqual(
            command_runner._MAX_ADDRESS_SPACE_BYTES,
            256 * 1024 * 1024,
        )
        self.assertEqual(command_runner._MAX_CPU_SECONDS, 30)
        self.assertEqual(command_runner._MAX_PROCESSES, 4)
        self.assertEqual(command_runner._MAX_FILE_BYTES, 16 * 1024 * 1024)
        infinity = -1
        resource_module, calls = _fake_command_resource_module({
            1: (infinity, infinity),
            2: (infinity, infinity),
            3: (infinity, infinity),
            4: (infinity, infinity),
        })
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.dict(sys.modules, {"resource": resource_module}),
        ):
            command_runner._apply_posix_resource_limits()

        self.assertEqual(
            calls,
            [
                (
                    resource_module.RLIMIT_AS,
                    (
                        command_runner._MAX_ADDRESS_SPACE_BYTES,
                        command_runner._MAX_ADDRESS_SPACE_BYTES,
                    ),
                ),
                (
                    resource_module.RLIMIT_CPU,
                    (
                        command_runner._MAX_CPU_SECONDS,
                        command_runner._MAX_CPU_SECONDS + 1,
                    ),
                ),
                (
                    resource_module.RLIMIT_NPROC,
                    (
                        command_runner._MAX_PROCESSES,
                        command_runner._MAX_PROCESSES,
                    ),
                ),
                (
                    resource_module.RLIMIT_FSIZE,
                    (
                        command_runner._MAX_FILE_BYTES,
                        command_runner._MAX_FILE_BYTES,
                    ),
                ),
            ],
        )

    def test_command_runner_resource_limits_never_raise_inherited_limits(self):
        resource_module, calls = _fake_command_resource_module({
            1: (128 * 1024 * 1024, 256 * 1024 * 1024),
            2: (5, 10),
            3: (1024, 4096),
            4: (2, 4),
        })
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.dict(sys.modules, {"resource": resource_module}),
        ):
            command_runner._apply_posix_resource_limits()

        self.assertEqual(
            calls,
            [
                (resource_module.RLIMIT_AS, (128 * 1024 * 1024, 256 * 1024 * 1024)),
                (resource_module.RLIMIT_CPU, (5, 10)),
                (resource_module.RLIMIT_NPROC, (2, 4)),
                (resource_module.RLIMIT_FSIZE, (1024, 4096)),
            ],
        )

    def test_command_runner_invalid_inherited_limit_fails_before_set(self):
        resource_module, calls = _fake_command_resource_module({
            1: (True, -1),
            2: (-1, -1),
            3: (-1, -1),
            4: (-1, -1),
        })
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.dict(sys.modules, {"resource": resource_module}),
            self.assertRaises(RuntimeError),
        ):
            command_runner._apply_posix_resource_limits()
        self.assertFalse(calls)

    def test_command_runner_missing_process_limit_and_non_posix_fail_closed(self):
        resource_module, calls = _fake_command_resource_module(
            {1: (-1, -1), 2: (-1, -1), 3: (-1, -1)},
            include_nproc=False,
        )
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.dict(sys.modules, {"resource": resource_module}),
            self.assertRaises(AttributeError),
        ):
            command_runner._apply_posix_resource_limits()
        self.assertFalse(calls)

        popen = mock.Mock(side_effect=AssertionError("child must not start"))
        with (
            mock.patch.object(command_runner.os, "name", "nt"),
            mock.patch.object(command_runner.subprocess, "Popen", popen),
            self.assertRaises(RuntimeError),
        ):
            command_runner._bounded_run(
                ["python", "child.py"], "/workspace", 5, 128
            )
        popen.assert_not_called()

    def test_command_runner_setrlimit_failure_is_sanitized_before_exec(self):
        resource_module, _calls = _fake_command_resource_module(
            {1: (-1, -1), 2: (-1, -1), 3: (-1, -1), 4: (-1, -1)},
            set_error=OSError("SECRET setrlimit failure"),
        )
        child_exec_reached = False

        def popen(_argv, **options):
            nonlocal child_exec_reached
            options["preexec_fn"]()
            child_exec_reached = True
            raise AssertionError("child exec must not be reached")

        payload = {
            "command": "unittest",
            "paths": ["/workspace/tests"],
            "cwd": "/workspace",
            "timeout": 5,
            "max_output_bytes": 1024,
        }
        output = io.StringIO()
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.dict(sys.modules, {"resource": resource_module}),
            mock.patch.object(command_runner.subprocess, "Popen", side_effect=popen),
            mock.patch.object(command_runner.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(command_runner.sys, "stdout", output),
        ):
            self.assertEqual(command_runner.main(), 0)

        response = json.loads(output.getvalue())
        self.assertEqual(
            response,
            {
                "exit_code": 1,
                "stdout": "",
                "stderr": "命令请求无效。",
                "truncated": False,
            },
        )
        self.assertNotIn("SECRET", output.getvalue())
        self.assertFalse(child_exec_reached)

    def test_command_runner_payload_cannot_override_resource_limits(self):
        payload = {
            "command": "unittest",
            "paths": ["/workspace/tests"],
            "cwd": "/workspace",
            "timeout": 5,
            "max_output_bytes": 1024,
            "max_memory_mb": 4096,
        }
        output = io.StringIO()
        bounded_run = mock.Mock(side_effect=AssertionError("must not run"))
        with (
            mock.patch.object(command_runner, "_bounded_run", bounded_run),
            mock.patch.object(command_runner.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(command_runner.sys, "stdout", output),
        ):
            self.assertEqual(command_runner.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["stderr"], "命令请求无效。")
        bounded_run.assert_not_called()

    def test_command_runner_kills_group_when_output_limit_is_reached(self):
        process = _FakeRunnerProcess(b"x" * 4096)
        killpg = mock.Mock(side_effect=lambda pid, sig: setattr(process, "returncode", -sig))
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.object(command_runner.os, "killpg", killpg, create=True),
            mock.patch.object(command_runner.subprocess, "Popen", return_value=process),
        ):
            result = command_runner._bounded_run(["python", "child.py"], "/workspace", 5, 8)

        self.assertTrue(result["truncated"])
        killpg.assert_called_with(process.pid, command_runner._SIGTERM)
        self.assertEqual(process.terminate_calls, 1)

    def test_command_runner_killpg_error_still_kills_direct_child(self):
        process = _FakeRunnerProcess()
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.object(command_runner.os, "killpg", side_effect=OSError("gone"), create=True) as killpg,
            mock.patch.object(command_runner.subprocess, "Popen", return_value=process),
        ):
            command_runner._bounded_run(["python", "child.py"], "/workspace", 0, 128)

        killpg.assert_called_with(process.pid, command_runner._SIGKILL)
        self.assertEqual(process.kill_calls, 1)

    def test_command_runner_without_killpg_still_terminates_direct_child(self):
        process = _FakeRunnerProcess()
        with (
            mock.patch.object(command_runner.os, "name", "posix"),
            mock.patch.object(command_runner.os, "killpg", None, create=True),
        ):
            command_runner._terminate_process_group(process, force=False)

        self.assertEqual(process.terminate_calls, 1)

    def test_sdk_transport_command_shapes_match_local_tool_semantics(self):
        sdk = FakeSdkSandbox(self.workspace_root)
        sdk.files.data[self.workspace_root + "/config.json"] = b'{"ok": true}'
        transport = E2BSandboxTransport(sdk, self.workspace_root)
        transport.run(
            "unittest",
            paths=(self.workspace_root,),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=1024,
        )
        transport.run(
            "validate_json",
            paths=(self.workspace_root + "/config.json",),
            code=None,
            cwd=self.workspace_root,
            timeout_seconds=4,
            max_output_bytes=1024,
        )
        commands = [
            call[0]
            for call in sdk.commands.calls
            if "command_runner.py" in call[0]
        ]
        self.assertIn("command_runner.py", commands[0])
        self.assertIn("-I -S -X utf8", commands[0])
        self.assertIn(".agent-internal", commands[0])
        self.assertIn("command_runner.py", commands[1])
        self.assertNotIn("config.json", commands[1])

    def test_no_raw_sandbox_or_credentials_in_group_or_lease_repr(self):
        lease = self.group.ensure_ready()
        self.assertNotIn("api-key-must-not-leak", repr(lease))
        self.assertNotIn("role-must-not-leak", repr(lease))
        self.assertNotIn("api-key-must-not-leak", repr(self.group))
        self.assertNotIn("role-must-not-leak", repr(self.group))
        self.assertNotIn("sandbox", self.group.__dict__)


if __name__ == "__main__":
    unittest.main()
