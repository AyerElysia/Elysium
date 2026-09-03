"""Terminal panels for conscious and subconscious model activity.

These formatters are console-only.  They must not be routed through
``logger.info``: that path writes SQLite audit rows, and reasoning or tool
bodies do not belong there.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.panel import Panel

RECEIPT_BODY_MAX_CHARS = 2048
HEARTBEAT_PANEL_SINKS = frozenset({"stdout", "file", "both"})
DEFAULT_HEARTBEAT_PANEL_PATH = "logs/heartbeat.console"
HEARTBEAT_PANEL_FILE_MAX_BYTES = 8 * 1024 * 1024
_FAILED_RESULT_PREFIXES = ("执行失败: ", "执行异常: ")

_file_lock = threading.Lock()
_file_handle: TextIO | None = None
_file_console: Console | None = None
_file_path: Path | None = None
_file_warned = False


def resolve_heartbeat_panel_sink(value: Any) -> str:
    """Normalize a sink name; unknown values fall back to stdout."""

    sink = str(value or "stdout").strip().lower()
    if sink in HEARTBEAT_PANEL_SINKS:
        return sink
    return "stdout"


def resolve_heartbeat_panel_path(value: Any) -> Path:
    """Resolve the heartbeat panel file against the process working directory."""

    raw = str(value or "").strip() or DEFAULT_HEARTBEAT_PANEL_PATH
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def close_heartbeat_panel_file() -> None:
    """Close the dedicated heartbeat console. Tests use this to avoid leaked FDs."""

    global _file_warned
    with _file_lock:
        _drop_heartbeat_panel_console()
        _file_warned = False


def _drop_heartbeat_panel_console() -> None:
    global _file_handle, _file_console, _file_path
    if _file_handle is not None:
        try:
            _file_handle.close()
        except OSError:
            pass
    _file_handle = None
    _file_console = None
    _file_path = None


def _rotate_heartbeat_panel_file(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= HEARTBEAT_PANEL_FILE_MAX_BYTES:
            backup = path.with_name(f"{path.name}.1")
            if backup.exists():
                backup.unlink()
            path.replace(backup)
    except OSError:
        return


def _ensure_heartbeat_panel_console(path: Path) -> Console | None:
    global _file_handle, _file_console, _file_path
    if (
        _file_console is not None
        and _file_path == path
        and _file_handle is not None
        and not _file_handle.closed
    ):
        try:
            if _file_handle.tell() >= HEARTBEAT_PANEL_FILE_MAX_BYTES:
                _drop_heartbeat_panel_console()
            else:
                return _file_console
        except OSError:
            _drop_heartbeat_panel_console()
    if _file_handle is not None:
        _drop_heartbeat_panel_console()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_heartbeat_panel_file(path)
        handle = path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        return None
    _file_handle = handle
    _file_console = Console(
        file=handle,
        force_terminal=True,
        highlight=False,
        width=100,
        legacy_windows=False,
    )
    _file_path = path
    return _file_console


def _print_heartbeat_panel_file(
    path: Path,
    body: str,
    *,
    title: str,
    border_style: str,
) -> bool:
    """Write one panel to the heartbeat file. Returns False if the file is unusable."""

    with _file_lock:
        console = _ensure_heartbeat_panel_console(path)
        if console is None or _file_handle is None:
            return False
        console.print(
            Panel(
                body,
                title=title,
                border_style=border_style,
            )
        )
        try:
            _file_handle.flush()
        except OSError:
            return False
        return True


def format_decision_tool_args(args: Any) -> str:
    """Format one tool-call argument map, omitting internal ``reason``."""

    if not isinstance(args, dict):
        return ""
    display_items: list[str] = []
    for key, value in args.items():
        if key == "reason":
            continue
        display_items.append(f"{key}: {value}")
    return ", ".join(display_items)


def format_decision_panel(
    *,
    thought: str = "",
    monologue: str = "",
    call_list: Any = None,
    header_lines: tuple[str, ...] | list[str] = (),
) -> str:
    """Render one model turn the same way Chatter's decision panel does."""

    thought_text = str(thought or "").strip() or "（无）"
    monologue_text = str(monologue or "").strip() or "（无）"
    tool_lines: list[str] = []
    for call in call_list or []:
        call_name = str(getattr(call, "name", "") or "<unknown>")
        formatted_args = format_decision_tool_args(getattr(call, "args", None))
        if formatted_args:
            tool_lines.append(f"    {call_name} ({formatted_args})")
        else:
            tool_lines.append(f"    {call_name}")
    tools_text = "\n".join(tool_lines) if tool_lines else "    （无）"
    sections = [str(line).strip() for line in header_lines if str(line).strip()]
    sections.extend(
        [
            f"思考：{thought_text}",
            f"独白：{monologue_text}",
            f"调用工具：\n{tools_text}",
        ]
    )
    return "\n\n".join(sections)


def _truncate_receipt_body(text: str) -> str:
    body = str(text or "")
    if len(body) <= RECEIPT_BODY_MAX_CHARS:
        return body
    omitted = len(body) - RECEIPT_BODY_MAX_CHARS
    return f"{body[:RECEIPT_BODY_MAX_CHARS]}\n… truncated {omitted} chars"


def _parse_failed_tool_payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    remainder = value
    for prefix in _FAILED_RESULT_PREFIXES:
        if value.startswith(prefix):
            remainder = value[len(prefix) :]
            break
    else:
        return value
    try:
        return ast.literal_eval(remainder)
    except (SyntaxError, ValueError):
        return value


def _receipt_failed(raw: Any, parsed: Any) -> bool:
    if isinstance(raw, str) and raw.startswith(_FAILED_RESULT_PREFIXES):
        return True
    if isinstance(parsed, dict):
        if parsed.get("authority_committed") is False:
            return True
        if parsed.get("error"):
            return True
        if parsed.get("mutated") is False and parsed.get("error_message"):
            return True
    return False


def _highlight_receipt_dict(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("error", "error_message", "authority_committed"):
        if key in payload:
            lines.append(f"  {key}: {payload[key]}")
    leftover = {
        key: value
        for key, value in payload.items()
        if key not in {"error", "error_message", "authority_committed"}
    }
    if leftover:
        try:
            leftover_text = json.dumps(leftover, ensure_ascii=False)
        except (TypeError, ValueError):
            leftover_text = str(leftover)
        lines.append(f"  {_truncate_receipt_body(leftover_text)}")
    if lines:
        return "\n".join(lines)
    try:
        return f"  {_truncate_receipt_body(json.dumps(payload, ensure_ascii=False))}"
    except (TypeError, ValueError):
        return f"  {_truncate_receipt_body(str(payload))}"


def format_tool_receipt_panel(results: list[Any] | tuple[Any, ...]) -> str:
    """Render one heartbeat tool round for the operator console."""

    if not results:
        return "本轮无工具回执。"
    blocks: list[str] = []
    for result in results:
        name = str(getattr(result, "name", "") or "<unknown>")
        raw = getattr(result, "value", result)
        parsed = _parse_failed_tool_payload(raw)
        status = "失败" if _receipt_failed(raw, parsed) else "成功"
        lines = [f"- {name}: {status}"]
        if isinstance(parsed, dict):
            highlighted = _highlight_receipt_dict(parsed)
            if highlighted:
                lines.append(highlighted)
        else:
            lines.append(f"  {_truncate_receipt_body(str(parsed))}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def format_skip_panel(
    *,
    reason: str = "",
    remaining: Any = None,
    until: Any = None,
) -> str:
    """Render one heartbeat tick that did not call the model."""

    lines = [f"原因：{str(reason or '').strip() or '（未说明）'}"]
    remaining_text = str(remaining if remaining is not None else "").strip()
    if remaining_text:
        lines.append(f"剩余：{remaining_text}")
    until_text = str(until if until is not None else "").strip()
    if until_text:
        lines.append(f"直到：{until_text}")
    return "\n".join(lines)


def format_stall_panel(
    *,
    heartbeat_count: int,
    reason: str = "",
    stall_kind: str = "",
    stage: str = "",
    model_turns: int | None = None,
    tools: list[str] | tuple[str, ...] | None = None,
    consecutive_no_progress: int | None = None,
    consecutive_protocol_failures: int | None = None,
    consecutive_same_failure: int | None = None,
) -> str:
    """Render why a heartbeat tool loop ended, without thought or receipt bodies."""

    lines = [
        f"心跳序号：#{heartbeat_count}",
        f"原因：{str(reason or '').strip() or '（未说明）'}",
    ]
    kind_text = str(stall_kind or "").strip()
    if kind_text:
        lines.append(f"类型：{kind_text}")
    stage_text = str(stage or "").strip()
    if stage_text:
        lines.append(f"阶段：{stage_text}")
    if model_turns is not None:
        lines.append(f"模型轮数：{model_turns}")
    tool_text = ", ".join(str(item).strip() for item in (tools or []) if str(item).strip())
    lines.append(f"末轮工具：{tool_text or '（无）'}")
    counters = []
    if consecutive_no_progress is not None:
        counters.append(f"无进展={consecutive_no_progress}")
    if consecutive_protocol_failures is not None:
        counters.append(f"协议失败={consecutive_protocol_failures}")
    if consecutive_same_failure is not None:
        counters.append(f"同失败={consecutive_same_failure}")
    if counters:
        lines.append("计数：" + " ".join(counters))
    return "\n".join(lines)


def print_activity_panel(
    logger: Any,
    body: str,
    *,
    title: str,
    border_style: str,
    sink: str = "stdout",
    path: str | Path | None = None,
) -> None:
    """Print a Rich panel; missing ``print_panel`` is a silent no-op.

    ``sink`` is ``stdout`` (main terminal), ``file`` (dedicated heartbeat
    stream), or ``both``. Chatter should leave it at the default.
    """

    global _file_warned
    resolved_sink = resolve_heartbeat_panel_sink(sink)
    if resolved_sink in {"stdout", "both"}:
        print_panel = getattr(logger, "print_panel", None)
        if callable(print_panel):
            print_panel(body, title=title, border_style=border_style)
    if resolved_sink in {"file", "both"}:
        target = resolve_heartbeat_panel_path(path)
        wrote = _print_heartbeat_panel_file(
            target,
            body,
            title=title,
            border_style=border_style,
        )
        if not wrote and resolved_sink == "file":
            warning = getattr(logger, "warning", None)
            if callable(warning) and not _file_warned:
                _file_warned = True
                warning(f"心跳面板文件无法写入，本轮未显示。 path={target}")
