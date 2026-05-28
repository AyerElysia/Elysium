"""life_engine 工具层安全审计 helper。"""

from __future__ import annotations

import re

_DANGEROUS_COMMAND_PATTERN = re.compile(
    r"(^|[\s;&|()])(?P<command>rm|mv)(?=$|[\s;&|()])"
)
_FD_DUPLICATION_PATTERN = re.compile(r"\d+|-")


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _find_unquoted_output_redirection_error(command_text: str) -> str | None:
    """Reject file-writing redirection while allowing common discard patterns."""

    quote: str | None = None
    index = 0
    length = len(command_text)

    while index < length:
        char = command_text[index]
        if char in {"'", '"'} and not _is_escaped(command_text, index):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            index += 1
            continue

        if quote is not None or char != ">" or _is_escaped(command_text, index):
            index += 1
            continue

        if index > 0 and command_text[index - 1] == "<":
            return "禁止使用可能写文件的重定向 '<>'。如需编辑文件，请使用专门的文件工具。"

        target_start = index + 1
        if target_start < length and command_text[target_start] in {">", "|"}:
            target_start += 1

        fd_duplication = False
        if target_start < length and command_text[target_start] == "&":
            fd_duplication = True
            target_start += 1

        while target_start < length and command_text[target_start].isspace():
            target_start += 1

        if target_start >= length:
            return "禁止使用缺少目标的输出重定向。"

        target_end = target_start
        target_quote: str | None = None
        while target_end < length:
            target_char = command_text[target_end]
            if target_char in {"'", '"'} and not _is_escaped(command_text, target_end):
                if target_quote == target_char:
                    target_quote = None
                elif target_quote is None:
                    target_quote = target_char
                target_end += 1
                continue
            if target_quote is None and (
                target_char.isspace() or target_char in {";", "|", "&", "(", ")"}
            ):
                break
            target_end += 1

        target = command_text[target_start:target_end].strip().strip("'\"")
        if target == "/dev/null":
            index = max(target_end, index + 1)
            continue
        if fd_duplication and _FD_DUPLICATION_PATTERN.fullmatch(target):
            index = max(target_end, index + 1)
            continue

        return (
            f"命令审计失败：禁止输出重定向到 '{target or '<empty>'}'。"
            "如需写文件，请使用专门的安全文件工具；丢弃输出可使用 /dev/null。"
        )

    return None


def audit_shell_command(command_text: str) -> str | None:
    """Return an audit error message when a shell command should not run."""

    dangerous = _DANGEROUS_COMMAND_PATTERN.search(command_text)
    if dangerous:
        command = dangerous.group("command")
        return (
            f"命令审计失败：禁止使用危险指令 '{command}'。"
            "如有必要，请使用专门的安全文件工具。"
        )

    return _find_unquoted_output_redirection_error(command_text)
