"""延迟加载的交互式终端输入和斜杠命令补全。"""

from __future__ import annotations

import os
import sys
from typing import Callable


SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/plan", "让模型规划，并在必要时进行三选一澄清"),
    ("/status", "显示当前模式和运行配置"),
    ("/permissions", "显示当前权限模式和切换用法"),
    ("/permissions partial-control", "立即降级到敏感写入审批模式"),
    ("/permissions full-access", "经 yes 确认后切换到高风险完全权限"),
    ("/recovery", "只读查看待 Agent 自主核验的中断工具调用"),
    ("/tasks", "列出当前会话的后台任务"),
    ("/task", "查看一个后台任务"),
    ("/cancel", "取消一个后台任务"),
    ("/interrupt", "中断当前模型响应"),
    ("exit", "退出 Agent"),
    ("quit", "退出 Agent"),
)


def filter_command_candidates(text: str) -> tuple[tuple[str, str], ...]:
    """返回稳定顺序的前缀候选，供 UI 与无终端单测共用。"""
    normalized = text.lstrip()
    if not normalized or (
        not normalized.startswith("/")
        and normalized not in {"e", "ex", "exi", "q", "qu", "qui"}
    ):
        return ()
    folded = normalized.casefold()
    return tuple(item for item in SLASH_COMMANDS if item[0].casefold().startswith(folded))


def _is_interactive_terminal() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def create_terminal_reader(*, plain: bool = False) -> Callable[[str, bool], str]:
    """只在真实 TTY 且首次读取时导入 prompt_toolkit；否则使用 input。"""
    session = None
    completer = None
    fallback_only = False

    def read(prompt: str, complete_commands: bool) -> str:
        nonlocal session, completer, fallback_only
        # 主命令读取（包括普通 act 请求运行期间、可能被下一次 idle 循环
        # 接管的抢先读取）启用补全。Plan 澄清使用专属的 False 简单读取；
        # 工具审批仍可接管 act 运行期间已有的主命令读取。
        if (
            not complete_commands
            or plain
            or "NO_COLOR" in os.environ
            or fallback_only
            or not _is_interactive_terminal()
        ):
            return input(prompt)
        if session is None:
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.completion import Completer, Completion
                from prompt_toolkit.patch_stdout import patch_stdout
                from prompt_toolkit.shortcuts import CompleteStyle
            except ImportError:
                fallback_only = True
                return input(prompt)

            class SlashCommandCompleter(Completer):
                def get_completions(self, document, complete_event):
                    text = document.text_before_cursor
                    for command, description in filter_command_candidates(text):
                        yield Completion(
                            command,
                            start_position=-len(text),
                            display=command,
                            display_meta=description,
                        )

            completer = SlashCommandCompleter()
            session = PromptSession(
                complete_while_typing=True,
                complete_style=CompleteStyle.COLUMN,
                reserve_space_for_menu=6,
            )
        active_completer = completer if complete_commands else None
        # 模型流和后台任务仍可能向 stdout 输出；patch_stdout 会在提示符上方
        # 安全重绘，避免破坏补全菜单或把输出混入用户输入。
        from prompt_toolkit.patch_stdout import patch_stdout

        with patch_stdout(raw=True):
            return session.prompt(prompt, completer=active_completer)

    return read
