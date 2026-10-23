"""Trusted, data-plane JSON candidate generator.

The host supplies only absolute source paths below one fixed workspace and a
random transaction directory below ``.agent-internal/transactions``.  This
runner never writes the active paths: normalized bytes are written only to the
transaction directory and are subsequently read and removed by the transport.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import stat
import sys

MAX_FILE_BYTES = 1024 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError


def _safe_absolute(path: object) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or posixpath.normpath(path) != path
        or any(part in {"", ".", ".."} for part in path.split("/")[1:])
    ):
        raise ValueError
    return path


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read(), object_pairs_hook=_unique_object)
        if not isinstance(payload, dict) or set(payload) != {"cwd", "paths", "staging_root"}:
            raise ValueError
        cwd = _safe_absolute(payload["cwd"])
        staging_root = _safe_absolute(payload["staging_root"])
        paths = payload["paths"]
        prefix = cwd + "/"
        staging_prefix = cwd + "/.agent-internal/transactions/"
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 8
            or len(set(paths)) != len(paths)
            or staging_root != staging_prefix + posixpath.basename(staging_root)
            or len(posixpath.basename(staging_root)) != 32
            or any(character not in "0123456789abcdef" for character in posixpath.basename(staging_root))
        ):
            raise ValueError
        checked: list[str] = []
        for raw in paths:
            path = _safe_absolute(raw)
            if (
                not path.startswith(prefix)
                or path.startswith(prefix + ".agent-internal/")
                or not path.casefold().endswith(".json")
            ):
                raise ValueError
            checked.append(path)
        if checked != sorted(checked):
            raise ValueError
        root_info = os.lstat(staging_root)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise ValueError

        results: list[dict[str, object]] = []
        for index, path in enumerate(checked):
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise ValueError
            if not 0 <= info.st_size <= MAX_FILE_BYTES:
                raise ValueError
            with open(path, "rb") as stream:
                before = stream.read(MAX_FILE_BYTES + 1)
            after_info = os.lstat(path)
            if len(before) != info.st_size or len(before) > MAX_FILE_BYTES:
                raise ValueError
            if (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns) != (
                after_info.st_dev, after_info.st_ino, after_info.st_mode,
                after_info.st_nlink, after_info.st_size, after_info.st_mtime_ns,
            ):
                raise ValueError
            text = before.decode("utf-8-sig")
            value = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            normalized = (json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            ) + "\n").encode("utf-8")
            if len(normalized) > MAX_FILE_BYTES:
                raise ValueError
            staged = staging_root + "/" + f"{index:02d}.json"
            descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(normalized)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            staged_info = os.lstat(staged)
            if not stat.S_ISREG(staged_info.st_mode) or stat.S_ISLNK(staged_info.st_mode) or staged_info.st_nlink != 1:
                raise ValueError
            results.append({
                "after_sha256": hashlib.sha256(normalized).hexdigest(),
                "before_sha256": hashlib.sha256(before).hexdigest(),
                "path": path,
                "size": len(normalized),
                "staged": staged,
            })
        sys.stdout.write(json.dumps({"files": results}, ensure_ascii=False, separators=(",", ":")))
        return 0
    except BaseException:
        sys.stdout.write(json.dumps({"error": "format_json 候选生成失败。"}, ensure_ascii=False, separators=(",", ":")))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
