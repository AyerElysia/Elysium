"""life_engine 命令执行工具。

提供一个 workspace-scoped 的 Bash 工具。
表达层可在用户请求需要终端时使用；life_engine 心跳态只用于诊断自身工作区或工具链。

注意：
- 这是 best-effort shell，不是 OS 级强隔离沙箱
- 工作目录限制在 workspace 内
- 不做命令白名单过滤，命令本身保留 shell 的完整表达力
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.api import log_api
from src.core.components import BaseTool

from ._utils import _get_workspace

logger = log_api.get_logger("life_engine.exec_tools")

_DEFAULT_TIMEOUT_SECONDS = 30
_DEFAULT_MAX_OUTPUT_CHARS = 12_000
_MAX_TIMEOUT_SECONDS = 600
_MAX_OUTPUT_CHARS = 50_000
_SENSITIVE_ENV_TOKENS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "COOKIE",
    "SESSION",
    "AUTH",
    "SSH_",
)

_DANGEROUS_COMMAND_PATTERN = re.compile(
    r"(^|[\s;&|()])(?P<command>rm|mv)(?=$|[\s;&|()])"
)
_FD_DUPLICATION_PATTERN = re.compile(r"\d+|-")


def _resolve_cwd(plugin: Any, cwd: str) -> tuple[bool, Path | str]:
    """把 cwd 解析到 workspace 内的实际目录。"""
    workspace = _get_workspace(plugin)
    raw = str(cwd or "").strip()
    if not raw or raw in {".", "./"}:
        return True, workspace

    candidate = Path(raw)
    try:
        resolved = candidate if candidate.is_absolute() else (workspace / candidate)
        resolved = resolved.resolve()
    except Exception as exc:  # noqa: BLE001
        return False, f"cwd 解析失败: {exc}"

    try:
        resolved.relative_to(workspace)
    except ValueError:
        return False, f"cwd 超出工作空间范围。工作空间: {workspace}"

    if not resolved.exists():
        return False, f"cwd 不存在: {resolved}"
    if not resolved.is_dir():
        return False, f"cwd 不是目录: {resolved}"

    return True, resolved


def _build_shell_env(workspace: Path, cwd: Path) -> dict[str, str]:
    """构建尽量干净的 shell 环境。"""
    tmp_root = workspace / ".shell"
    tmp_dir = tmp_root / "tmp"
    xdg_home = tmp_root / "xdg"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    xdg_home.mkdir(parents=True, exist_ok=True)

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(token in upper for token in _SENSITIVE_ENV_TOKENS):
            continue
        env[key] = value

    env["HOME"] = str(workspace)
    env["PWD"] = str(cwd)
    env["TMPDIR"] = str(tmp_dir)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    env["XDG_CACHE_HOME"] = str(xdg_home / "cache")
    env["XDG_CONFIG_HOME"] = str(xdg_home / "config")
    env["XDG_DATA_HOME"] = str(xdg_home / "data")
    env["SHELL"] = "/bin/bash"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", env["LANG"])
    env.setdefault(
        "PATH",
        os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
    )
    return env


def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


def _decode_output(data: bytes | None, limit: int) -> tuple[str, bool]:
    text = (data or b"").decode("utf-8", errors="replace")
    return _truncate_text(text, limit)


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


def _audit_command(command_text: str) -> str | None:
    """Return an audit error message when a command should not run."""

    dangerous = _DANGEROUS_COMMAND_PATTERN.search(command_text)
    if dangerous:
        command = dangerous.group("command")
        return (
            f"命令审计失败：禁止使用危险指令 '{command}'。"
            "如有必要，请使用专门的安全文件工具。"
        )

    return _find_unquoted_output_redirection_error(command_text)


def _stop_process(process: Any) -> None:
    """尽力终止子进程。"""
    if getattr(process, "returncode", None) is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"terminate 失败: {exc}")

    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass

    if getattr(process, "returncode", None) is None:
        try:
            process.kill()
        except ProcessLookupError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"kill 失败: {exc}")


class LifeEngineBashTool(BaseTool):
    """在 workspace 内执行 bash 命令。"""

    tool_name: str = "nucleus_bash"
    tool_description: str = (
        "在 workspace 内执行 bash 命令。这个工具同时暴露给表达层和 life_engine 心跳态，"
        "两种运行态的边界不同。"
        "\n\n"
        "**life_engine 心跳态边界（重要）：**\n"
        "- 你是潜意识 / 内在状态层，不是后台终端助手。\n"
        "- 只在诊断 life_engine 自己的 workspace、日志、工具链异常时使用。\n"
        "- 不要用它查用户项目配置、跑用户任务、生成图片、改代码或处理外部系统。\n"
        "- 如果只是因为用户提到 API、画图、项目或配置而想运行命令，应该停下，把这交给 life_chatter / 表达层。\n"
        "\n"
        "**life_chatter / 表达层用法：**\n"
        "- 只有用户请求或当前对话处理确实需要终端时使用。\n"
        "- 适合查看 workspace 内文件、日志和产物，或做轻量批处理。\n"
        "- 优先用最少轮次拿结果，能一条命令解决就别拆成多次调用。\n"
        "\n"
        "**何时不用：**\n"
        "- 只是想读写单个文件 → 用 file 工具\n"
        "- 只是想搜记忆或网页 → 用 memory / web 工具\n"
        "- 没必要跑命令就能回答的问题\n"
        "\n"
        "**边界：**\n"
        "- 只保证工作目录限制在 workspace 内\n"
        "- 这是 best-effort shell，不是 OS 级强隔离沙箱"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        command: Annotated[str, "要执行的 bash 命令"],
        cwd: Annotated[str, "执行目录（相对 workspace，默认 workspace 根目录）"] = "",
        timeout_seconds: Annotated[int, "超时时间（秒）"] = _DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: Annotated[int, "stdout/stderr 单独截断长度"] = _DEFAULT_MAX_OUTPUT_CHARS,
    ) -> tuple[bool, str | dict]:
        """执行 bash 命令并返回结构化结果。"""
        command_text = str(command or "").strip()
        if not command_text:
            return False, {"error": "command 不能为空"}

        audit_error = _audit_command(command_text)
        if audit_error:
            return False, {"error": audit_error, "command": command_text}

        try:
            timeout_seconds = max(1, min(_MAX_TIMEOUT_SECONDS, int(timeout_seconds)))
        except Exception:
            timeout_seconds = _DEFAULT_TIMEOUT_SECONDS

        try:
            max_output_chars = max(1, min(_MAX_OUTPUT_CHARS, int(max_output_chars)))
        except Exception:
            max_output_chars = _DEFAULT_MAX_OUTPUT_CHARS

        ok, resolved_cwd = _resolve_cwd(self.plugin, cwd)
        if not ok:
            return False, {"error": str(resolved_cwd), "command": command_text}

        workspace = _get_workspace(self.plugin)
        env = _build_shell_env(workspace, resolved_cwd)

        logger.info(
            f"[nucleus_bash] cwd={resolved_cwd} timeout={timeout_seconds}s "
            f"cmd={command_text[:240]}"
        )

        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                command_text,
                cwd=str(resolved_cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            return False, {"error": "未找到 bash 可执行文件", "command": command_text}
        except Exception as exc:  # noqa: BLE001
            return False, {"error": f"启动 bash 失败: {exc}", "command": command_text}

        timed_out = False
        stdout: bytes | None = None
        stderr: bytes | None = None

        communicate_task = asyncio.create_task(process.communicate())

        try:
            stdout, stderr = await asyncio.wait_for(
                communicate_task,
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            communicate_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await communicate_task
            _stop_process(process)
            try:
                stdout, stderr = await process.communicate()
            except Exception as exc:  # noqa: BLE001
                stdout, stderr = b"", str(exc).encode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            _stop_process(process)
            return False, {"error": f"执行失败: {exc}", "command": command_text}

        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout_text, stdout_truncated = _decode_output(stdout, max_output_chars)
        stderr_text, stderr_truncated = _decode_output(stderr, max_output_chars)
        exit_code = process.returncode if process.returncode is not None else -1

        payload: dict[str, Any] = {
            "command": command_text,
            "cwd": str(resolved_cwd),
            "workspace": str(workspace),
            "exit_code": exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "duration_ms": duration_ms,
            "timed_out": timed_out,
        }

        if timed_out:
            payload["error"] = f"命令执行超时（{timeout_seconds}秒）"
            return False, payload

        if exit_code != 0:
            payload["error"] = f"命令以退出码 {exit_code} 结束"
            return False, payload

        return True, payload


EXEC_TOOLS = [
    LifeEngineBashTool,
]

__all__ = [
    "EXEC_TOOLS",
    "LifeEngineBashTool",
]
