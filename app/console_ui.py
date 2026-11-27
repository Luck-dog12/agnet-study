"""零依赖、TTY 感知的命令行呈现层。"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from threading import RLock
from typing import TextIO
import unicodedata


def command_preview(value: str, *, maximum: int = 120) -> str:
    """Display untrusted command text on one safe, bounded terminal line."""
    replacements = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    rendered: list[str] = []
    size = 0
    for character in value:
        escaped = replacements.get(character)
        if escaped is None:
            category = unicodedata.category(character)
            if category.startswith("C") or category in {"Zl", "Zp"}:
                codepoint = ord(character)
                escaped = (
                    f"\\x{codepoint:02x}" if codepoint < 256 else
                    f"\\u{codepoint:04x}" if codepoint <= 0xFFFF else
                    f"\\U{codepoint:08x}"
                )
            else:
                escaped = character
        if size + len(escaped) > maximum:
            rendered.append("...")
            break
        rendered.append(escaped)
        size += len(escaped)
    return "".join(rendered)


class ConsoleUI:
    def __init__(self, *, plain: bool = False, output: TextIO | None = None, error: TextIO | None = None) -> None:
        self._output = output
        self._error_output = error
        self._lock = RLock()
        self._assistant_line_open = False
        tty = bool(getattr(self.output, "isatty", lambda: False)())
        self.color = tty and not plain and "NO_COLOR" not in os.environ
        self.prompt_marker = self._safe_symbol("›", ">")
        self.assistant_marker = self._safe_symbol("│", "|")

    @property
    def output(self) -> TextIO:
        # prompt_toolkit.patch_stdout replaces sys.stdout while reading input.
        # Resolve it at write time so background replies and queue notices are
        # redrawn above the prompt instead of bypassing that proxy.
        return sys.stdout if self._output is None else self._output

    @property
    def error_output(self) -> TextIO:
        return sys.stderr if self._error_output is None else self._error_output

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
        print("启动时检查模型服务；故障期间输入直接丢弃，恢复后接收新需求。", file=self.output)
        print("/recovery（只读）  /tasks  /task ID  /cancel ID  /interrupt [说明]", file=self.output)
        print("agent --new-session [NAME]  agent --resume NAME  agent --list-sessions", file=self.output)
        print("exit | quit", file=self.output)

    def prompt_text(self) -> str:
        # prompt_toolkit 自己拥有这一行，保持纯文本可避免 ANSI 宽度计算偏差。
        return f"{self.prompt_marker} act "

    def start_assistant(self) -> None:
        with self._lock:
            print(self._style(f"{self.assistant_marker} ", "38;5;173"), end="", flush=True, file=self.output)
            self._assistant_line_open = True

    def stream_text(self, value: str) -> None:
        """Write a prefix and stream fragment atomically with host notices."""
        if not value:
            return
        with self._lock:
            if not self._assistant_line_open:
                print(self._style(f"{self.assistant_marker} ", "38;5;173"), end="", file=self.output)
            print(value, end="", flush=True, file=self.output)
            self._assistant_line_open = not value.endswith("\n")

    def _break_stream_line(self) -> None:
        if self._assistant_line_open:
            print(flush=True, file=self.output)
            self._assistant_line_open = False

    def finish_assistant(self) -> None:
        with self._lock:
            self._break_stream_line()

    @contextmanager
    def host_output(self):
        """Serialize an existing host print block with streaming output."""
        with self._lock:
            self._break_stream_line()
            yield

    def text(self, value: str = "", *, end: str = "\n") -> None:
        with self._lock:
            print(value, end=end, flush=True, file=self.output)
            self._assistant_line_open = not (value + end).endswith("\n")

    def status(self, value: str) -> None:
        with self._lock:
            self._break_stream_line()
            print(self._style(f"status  {value}", "2"), flush=True, file=self.output)

    def warning(self, value: str) -> None:
        with self._lock:
            self._break_stream_line()
            print(self._style(f"warning  {value}", "33"), flush=True, file=self.error_output)

    def error(self, value: str) -> None:
        with self._lock:
            self._break_stream_line()
            print(self._style(f"error  {value}", "31"), flush=True, file=self.error_output)

    def _show_queue(self, items: tuple[tuple[int, str], ...]) -> None:
        if not items:
            print("待执行列表：空", flush=True, file=self.output)
            return
        print(f"待执行列表（{len(items)} 项）：", file=self.output)
        for number, prompt in items:
            print(f"  #{number} {command_preview(prompt)}", file=self.output)
        self.output.flush()

    def queue_received(self, number: int, prompt: str, items: tuple[tuple[int, str], ...]) -> None:
        with self._lock:
            self.status(f"已排队 #{number}：{command_preview(prompt)}")
            self._show_queue(items)

    def queue_started(self, number: int, prompt: str, items: tuple[tuple[int, str], ...]) -> None:
        with self._lock:
            self.status(f"开始执行 #{number}：{command_preview(prompt)}")
            self._show_queue(items)

    def queue_cancelled(self, items: tuple[tuple[int, str], ...], *, reason: str) -> None:
        if not items:
            return
        with self._lock:
            self.warning(f"已取消待执行命令（{reason}）：")
            for number, prompt in items:
                print(f"  #{number} {command_preview(prompt)}", file=self.error_output)
            self.error_output.flush()
            self._show_queue(())

    def queue_status(self, value: str, items: tuple[tuple[int, str], ...]) -> None:
        with self._lock:
            self.status(value)
            self._show_queue(items)
