"""零依赖、TTY 感知的命令行呈现层。"""

from __future__ import annotations

import os
import sys
from typing import TextIO


class ConsoleUI:
    def __init__(self, *, plain: bool = False, output: TextIO | None = None, error: TextIO | None = None) -> None:
        self.output = sys.stdout if output is None else output
        self.error_output = sys.stderr if error is None else error
        tty = bool(getattr(self.output, "isatty", lambda: False)())
        self.color = tty and not plain and "NO_COLOR" not in os.environ
        self.prompt_marker = self._safe_symbol("›", ">")
        self.assistant_marker = self._safe_symbol("│", "|")

    def _safe_symbol(self, preferred: str, fallback: str) -> str:
        encoding = getattr(self.output, "encoding", None) or "utf-8"
        try:
            preferred.encode(encoding)
        except (LookupError, UnicodeError):
            return fallback
        return preferred

    def _style(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def banner(self, session_name: str) -> None:
        title = self._style("Coding Agent", "1;38;5;173")
        print(f"\n{title}  当前会话：{session_name}", file=self.output)
        print("─" * 56 if self.prompt_marker == "›" else "-" * 56, file=self.output)
        print("/plan [需求]  /status  /permissions", file=self.output)
        print("/recovery（只读）  /tasks  /task ID  /cancel ID  /interrupt [说明]", file=self.output)
        print("agent --new-session [NAME]  agent --resume NAME  agent --list-sessions", file=self.output)
        print("exit | quit", file=self.output)

    def prompt_text(self) -> str:
        # prompt_toolkit 自己拥有这一行，保持纯文本可避免 ANSI 宽度计算偏差。
        return f"{self.prompt_marker} act "

    def start_assistant(self) -> None:
        print(self._style(f"{self.assistant_marker} ", "38;5;173"), end="", flush=True, file=self.output)

    def text(self, value: str = "", *, end: str = "\n") -> None:
        print(value, end=end, flush=True, file=self.output)

    def status(self, value: str) -> None:
        print(self._style(f"status  {value}", "2"), file=self.output)

    def warning(self, value: str) -> None:
        print(self._style(f"warning  {value}", "33"), file=self.error_output)

    def error(self, value: str) -> None:
        print(self._style(f"error  {value}", "31"), file=self.error_output)
