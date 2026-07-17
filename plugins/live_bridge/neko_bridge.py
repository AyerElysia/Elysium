"""N.E.K.O（Live2D 桌面伴侣）提示词接管桥接辅助模块。

桥接路由复用 ``LifeChatter._build_chat_router_prefix_prompt`` 生成的静态
SOUL/USER/MEMORY 前缀和全局数据库聊天历史，但不会进入完整 LifeChatter
runtime、动态 suffix 或其工具循环。

本模块只提供两类纯函数/小对象，不依赖 kernel 以外的重逻辑：
1. ``NekoToolAdapter`` / ``build_neko_tool_adapters``：把 N.E.K.O 传来的 OpenAI
   格式 tools 包装成 kernel LLM 模块要求的 ``LLMUsable`` 对象。包装边界保留
   schema，随后 kernel/provider 仍可能按兼容性规则规范化；桥接层不执行工具。
2. ``last_user_content`` / ``build_pending_tool_exchange_text``：从 N.E.K.O
   OpenAI messages 里提取当前用户发言，并把本轮尚未落盘的工具调用/结果尾巴
   转成文本摘要，用于直接调用 LLM 时的 user prompt。滚动历史来自全局数据库，
   不依赖 N.E.K.O 传回完整历史。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _shorten(value: Any, *, limit: int = 900) -> str:
    text = _as_text(value)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _read_message_value(message: Any, key: str, default: Any = None) -> Any:
    if isinstance(message, Mapping):
        return message.get(key, default)
    return getattr(message, key, default)


def _read_message_role(message: Any) -> str:
    return _as_text(_read_message_value(message, "role", "")).strip().lower()


def _read_message_content(message: Any) -> str:
    content = _read_message_value(message, "content", "")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return _as_text(content)


def _read_tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, Mapping):
        return _as_text(tool_call.get("id")).strip()
    return _as_text(getattr(tool_call, "id", "")).strip()


def _read_tool_call_name(tool_call: Any) -> str:
    if isinstance(tool_call, Mapping):
        function = tool_call.get("function")
        if isinstance(function, Mapping):
            return _as_text(function.get("name")).strip()
        return _as_text(tool_call.get("name")).strip()
    function = getattr(tool_call, "function", None)
    if isinstance(function, Mapping):
        return _as_text(function.get("name")).strip()
    return _as_text(getattr(function, "name", "") or getattr(tool_call, "name", "")).strip()


def _read_tool_call_arguments(tool_call: Any) -> str:
    if isinstance(tool_call, Mapping):
        function = tool_call.get("function")
        if isinstance(function, Mapping):
            arguments = function.get("arguments", {})
        else:
            arguments = tool_call.get("args", {})
    else:
        function = getattr(tool_call, "function", None)
        if isinstance(function, Mapping):
            arguments = function.get("arguments", {})
        else:
            arguments = getattr(function, "arguments", None)
            if arguments is None:
                arguments = getattr(tool_call, "args", {})
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return _as_text(arguments)


def _exchange_record(**values: str) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def last_user_content(messages: Sequence[Any]) -> str:
    """取出最后一条 user 消息内容；找不到就退回最后一条消息。"""
    for message in reversed(messages):
        if _read_message_role(message) == "user":
            return _read_message_content(message)
    if messages:
        return _read_message_content(messages[-1])
    return ""


def build_pending_tool_exchange_text(messages: Sequence[Any], *, limit: int = 6) -> str:
    """把尚未落盘的工具调用/结果尾巴转成多步续接文本。

    N.E.K.O 侧的多步工具调用（模型选择工具 -> N.E.K.O 本地执行 -> 把结果回传
    -> 模型继续决策）发生在同一次用户请求的 messages 数组尾部，不会经过全局
    聊天历史落盘。这里只摘要最后一个 user 消息之后的 assistant tool_calls / tool
    结果，并显式保留 call id、name、arguments 和 result 的关联。
    """

    tail: list[Any] = []
    for message in reversed(messages):
        if _read_message_role(message) == "user":
            break
        tail.append(message)
    tail.reverse()
    if not tail:
        return ""

    lines: list[str] = []
    call_names: dict[str, str] = {}
    for message in tail[-limit:]:
        role = _read_message_role(message) or "unknown"
        if role == "assistant":
            tool_calls = _read_message_value(message, "tool_calls", None) or []
            for call in tool_calls:
                call_id = _read_tool_call_id(call)
                name = _read_tool_call_name(call)
                arguments = _read_tool_call_arguments(call)
                if call_id and name:
                    call_names[call_id] = name
                lines.append(
                    "[assistant 工具调用] "
                    + _exchange_record(id=call_id, name=name, arguments=arguments)
                )
            content = _shorten(_read_message_content(message))
            if content:
                lines.append(f"[assistant]: {content}")
        elif role == "tool":
            tool_call_id = _as_text(
                _read_message_value(message, "tool_call_id", "")
            ).strip()
            name = _as_text(_read_message_value(message, "name", "")).strip()
            if not name:
                name = call_names.get(tool_call_id, "")
            lines.append(
                "[工具结果] "
                + _exchange_record(
                    tool_call_id=tool_call_id,
                    name=name,
                    result=_read_message_content(message),
                )
            )
        else:
            content = _shorten(_read_message_content(message))
            if content:
                lines.append(f"[{role}]: {content}")

    return "\n".join(lines)


class NekoToolAdapter:
    """把 N.E.K.O 的 OpenAI 格式 tool dict 包装为 ``LLMUsable``。

    ``to_schema()`` 在本适配层直接返回保存的 schema，不主动修改。接受
    ``**kwargs`` 的 ``execute`` 可避免 kernel 因缺少通用参数入口而注入必填
    ``reason``；桥接层不会调用该方法。schema 交给 kernel/provider 后仍可能因
    客户端兼容策略被规范化。
    """

    __slots__ = ("_raw_tool",)

    def __init__(self, raw_tool: dict[str, Any]) -> None:
        self._raw_tool = raw_tool

    def to_schema(self) -> dict[str, Any]:
        return self._raw_tool

    def execute(self, **kwargs: Any) -> None:  # pragma: no cover - 由 N.E.K.O 前端自行执行，不会被调用
        raise NotImplementedError("NEKO 工具由 N.E.K.O 前端自行执行，不会在 Neo-MoFox 内被调用")


def build_neko_tool_adapters(tools: Sequence[dict[str, Any]] | None) -> list[NekoToolAdapter]:
    """把 N.E.K.O 传来的 tools 列表包装成可注入 ``ROLE.TOOL`` payload 的对象列表。"""
    if not tools:
        return []
    adapters: list[NekoToolAdapter] = []
    for tool in tools:
        if isinstance(tool, Mapping):
            adapters.append(NekoToolAdapter(dict(tool)))
    return adapters
