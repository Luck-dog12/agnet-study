"""Trusted remote command runner used by the Aliyun transport.

The transport writes this file from the packaged source and passes only a JSON
payload.  It never evaluates a model-provided shell string.
"""

from __future__ import annotations

import json
import os
import posixpath
import signal
import subprocess
import sys
import threading
import time

_SIGKILL = getattr(signal, "SIGKILL", 9)
_SIGTERM = getattr(signal, "SIGTERM", 15)
_MAX_ADDRESS_SPACE_BYTES = 256 * 1024 * 1024
_MAX_CPU_SECONDS = 30
_MAX_PROCESSES = 4
_MAX_FILE_BYTES = 16 * 1024 * 1024


def _tighter_limit(
    resource_module: object,
    resource_kind: int,
    target_soft: int,
    target_hard: int,
) -> None:
    getrlimit = getattr(resource_module, "getrlimit")
    setrlimit = getattr(resource_module, "setrlimit")
    infinity = getattr(resource_module, "RLIM_INFINITY")
    current_soft, current_hard = getrlimit(resource_kind)
    if (
        isinstance(current_soft, bool)
        or not isinstance(current_soft, int)
        or isinstance(current_hard, bool)
        or not isinstance(current_hard, int)
    ):
        raise RuntimeError
    new_soft = target_soft if current_soft == infinity else min(current_soft, target_soft)
    new_hard = target_hard if current_hard == infinity else min(current_hard, target_hard)
    new_soft = min(new_soft, new_hard)
    setrlimit(resource_kind, (new_soft, new_hard))


def _apply_posix_resource_limits() -> None:
    """Apply fixed child-only limits; any missing boundary fails closed."""

    if os.name != "posix":
        raise RuntimeError
    import resource

    limits = (
        (
            getattr(resource, "RLIMIT_AS"),
            _MAX_ADDRESS_SPACE_BYTES,
            _MAX_ADDRESS_SPACE_BYTES,
        ),
        (
            getattr(resource, "RLIMIT_CPU"),
            _MAX_CPU_SECONDS,
            _MAX_CPU_SECONDS + 1,
        ),
        (
            getattr(resource, "RLIMIT_NPROC"),
            _MAX_PROCESSES,
            _MAX_PROCESSES,
        ),
        (
            getattr(resource, "RLIMIT_FSIZE"),
            _MAX_FILE_BYTES,
            _MAX_FILE_BYTES,
        ),
    )
    for resource_kind, target_soft, target_hard in limits:
        _tighter_limit(
            resource,
            resource_kind,
            target_soft,
            target_hard,
        )


def _terminate_process_group(process: subprocess.Popen[bytes], *, force: bool) -> None:
    """Best-effort group termination with a direct-child fallback."""

    if os.name == "posix":
        killpg = getattr(os, "killpg", None)
        if callable(killpg):
            try:
                killpg(process.pid, _SIGKILL if force else _SIGTERM)
            except (OSError, ValueError):
                pass
    try:
        (process.kill if force else process.terminate)()
    except (OSError, ValueError):
        pass


def _bounded_run(argv: list[str], cwd: str, timeout: int, limit: int) -> dict[str, object]:
    if os.name != "posix":
        # This packaged runner is a Linux Sandbox component.  Running without
        # POSIX rlimits would silently remove a required isolation layer.
        raise RuntimeError
    popen_options: dict[str, object] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "start_new_session": True,
        "preexec_fn": _apply_posix_resource_limits,
    }
    process = subprocess.Popen(argv, **popen_options)  # type: ignore[arg-type]
    assert process.stdout is not None and process.stderr is not None
    chunks = {"stdout": bytearray(), "stderr": bytearray()}
    lock = threading.Lock()
    truncated = False
    output_limited = threading.Event()

    def drain(stream: object, key: str) -> None:
        nonlocal truncated
        while True:
            data = stream.read(4096)  # type: ignore[union-attr]
            if not data:
                return
            with lock:
                remaining = limit - len(chunks["stdout"]) - len(chunks["stderr"])
                if remaining <= 0:
                    truncated = True
                    output_limited.set()
                    return
                chunks[key].extend(data[:remaining])
                if len(data) > remaining:
                    truncated = True
                    output_limited.set()

    readers = [
        threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    try:
        timed_out = False
        while process.poll() is None:
            if output_limited.is_set():
                _terminate_process_group(process, force=False)
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                timed_out = True
                _terminate_process_group(process, force=True)
                break
            time.sleep(min(0.02, remaining_time))
        remaining_time = max(0.1, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining_time)
        except subprocess.TimeoutExpired:
            if not output_limited.is_set():
                timed_out = True
            _terminate_process_group(process, force=True)
            process.wait()
        for reader in readers:
            reader.join(timeout=0.5)
        if timed_out:
            return {
                "exit_code": 1,
                "stdout": chunks["stdout"].decode("utf-8", "replace"),
                "stderr": "命令执行超时。",
                "truncated": truncated,
            }
    finally:
        for reader in readers:
            reader.join(timeout=0.5)
        process.stdout.close()
        process.stderr.close()
    return {
        "exit_code": process.returncode,
        "stdout": chunks["stdout"].decode("utf-8", "replace"),
        "stderr": chunks["stderr"].decode("utf-8", "replace"),
        "truncated": truncated,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict) or set(payload) != {
            "command", "paths", "cwd", "timeout", "max_output_bytes"
        }:
            raise ValueError
        command = payload["command"]
        paths = payload["paths"]
        cwd = payload["cwd"]
        timeout = payload["timeout"]
        limit = payload["max_output_bytes"]
        if (
            command not in {"compileall", "unittest", "validate_json"}
            or not isinstance(paths, list)
            or not all(isinstance(path, str) for path in paths)
            or len(paths) > 8
            or not isinstance(cwd, str)
            or not cwd.startswith("/")
            or "\\" in cwd
            or "\x00" in cwd
            or posixpath.normpath(cwd) != cwd
            or any(part in {"", ".", ".."} for part in cwd.split("/")[1:])
            or not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 120
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 128 * 1024
            or any(
                path != cwd
                and not path.startswith(cwd + "/")
                or "\\" in path
                or "\x00" in path
                or posixpath.normpath(path) != path
                or os.path.commonpath((cwd, path)) != cwd
                for path in paths
            )
        ):
            raise ValueError
        if command == "unittest" and len(paths) != 1:
            raise ValueError
        if command in {"compileall", "validate_json"} and not paths:
            raise ValueError
        if command == "compileall":
            argv = [sys.executable, "-m", "compileall", "-q", *paths]
            response = _bounded_run(argv, cwd, timeout, limit)
        elif command == "unittest":
            response = _bounded_run(
                [sys.executable, "-m", "unittest", "discover", "-s", paths[0], "-p", "test*.py", "-v"],
                cwd,
                timeout,
                limit,
            )
        else:
            outputs: list[str] = []
            errors: list[str] = []
            exit_code = 0
            truncated = False
            remaining = limit
            for path in paths:
                if remaining <= 0:
                    truncated = True
                    break
                one = _bounded_run([sys.executable, "-m", "json.tool", path], cwd, timeout, remaining)
                exit_code = exit_code or int(one["exit_code"])
                outputs.append(str(one["stdout"]))
                errors.append(str(one["stderr"]))
                truncated = truncated or bool(one["truncated"])
                remaining -= len(str(one["stdout"]).encode("utf-8"))
                remaining -= len(str(one["stderr"]).encode("utf-8"))
                if one["truncated"]:
                    break
            response = {
                "exit_code": exit_code,
                "stdout": "".join(outputs),
                "stderr": "".join(errors),
                "truncated": truncated,
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException:
        sys.stdout.write(
            json.dumps(
                {"exit_code": 1, "stdout": "", "stderr": "命令请求无效。", "truncated": False},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
