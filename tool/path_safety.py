"""跨 Python 版本的 Windows 保留路径判定。"""

from __future__ import annotations

import ntpath


_WINDOWS_DEVICES = frozenset(
    {
        "con", "prn", "aux", "nul", "clock$",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)


def is_windows_reserved(path: str) -> bool:
    """在 Python 3.11/3.12 也保留 ``ntpath.isreserved`` 的安全语义。"""
    native = getattr(ntpath, "isreserved", None)
    if callable(native):
        return bool(native(path))
    if not isinstance(path, str) or not path:
        return False
    for part in path.replace("/", "\\").split("\\"):
        if not part:
            continue
        if part.endswith((" ", ".")) or any(ord(character) < 32 for character in part):
            return True
        if any(character in part for character in ':*?<>|"'):
            return True
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if stem in _WINDOWS_DEVICES:
            return True
    return False
