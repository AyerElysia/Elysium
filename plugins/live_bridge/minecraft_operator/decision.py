"""Pure Touhou Little Maid decision parsing and formatting helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.IGNORECASE | re.DOTALL)
_TOOL_NAME_RE = re.compile(
    r'"?(?:tool_name|tool|name|function)"?\s*[:=]\s*"(?P<name>[A-Za-z0-9_.:\-]+)"',
    re.IGNORECASE,
)
_KNOWN_TLM_TOOLS = {
    "query_game_context",
    "switch_work_task",
    "switch_follow_state",
    "switch_sit",
    "switch_schedule",
    "use_skill",
    "query_minecraft_wiki",
}


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _shorten(value: Any, *, limit: int = 500) -> str:
    text = _as_text(value)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _loads_json_object(text: str) -> dict[str, Any] | None:
    raw = _as_text(text).strip()
    if not raw:
        return None

    candidates = [raw]
    for match in _JSON_BLOCK_RE.finditer(raw):
        candidates.append(match.group("body").strip())

    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(raw[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _loads_json_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = _loads_json_object(value)
        if isinstance(parsed, dict):
            return parsed
    return None


def _read_message_role(message: Any) -> str:
    return _as_text(getattr(message, "role", "")).strip().lower()


def _read_message_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return _as_text(content)


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


def _read_tool_spec(tool: Any) -> dict[str, Any] | None:
    if isinstance(tool, Mapping):
        function = tool.get("function")
        if isinstance(function, Mapping):
            name = _as_text(function.get("name")).strip()
            if not name:
                return None
            return {
                "name": name,
                "description": _as_text(function.get("description")).strip(),
                "parameters": function.get("parameters") if isinstance(function.get("parameters"), Mapping) else {},
            }
        name = _as_text(tool.get("name")).strip()
        if not name:
            return None
        return {
            "name": name,
            "description": _as_text(tool.get("description")).strip(),
            "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), Mapping) else {},
        }

    function = getattr(tool, "function", None)
    if isinstance(function, Mapping):
        name = _as_text(function.get("name")).strip()
        if not name:
            return None
        return {
            "name": name,
            "description": _as_text(function.get("description")).strip(),
            "parameters": function.get("parameters") if isinstance(function.get("parameters"), Mapping) else {},
        }
    return None


def _iter_tool_specs(tools: Sequence[Any] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    specs: list[dict[str, Any]] = []
    for tool in tools:
        spec = _read_tool_spec(tool)
        if spec is not None:
            specs.append(spec)
    return specs


def _tool_call_count(messages: Sequence[Any]) -> int:
    count = 0
    for message in messages:
        tool_calls = getattr(message, "tool_calls", None)
        if isinstance(tool_calls, list):
            count += len(tool_calls)
    return count


def _recent_tool_results(messages: Sequence[Any], *, limit: int = 4) -> list[str]:
    results: list[str] = []
    for message in reversed(messages):
        if _read_message_role(message) == "tool":
            tool_call_id = _as_text(getattr(message, "tool_call_id", "")).strip()
            prefix = f"tool_call_id={tool_call_id}\n" if tool_call_id else ""
            results.append(prefix + _shorten(_read_message_content(message), limit=900))
            if len(results) >= limit:
                break
    return list(reversed(results))


def _last_user_content(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if _read_message_role(message) == "user":
            return _read_message_content(message)
    if messages:
        return _read_message_content(messages[-1])
    return ""


def _message_tail(messages: Sequence[Any], *, limit: int = 8) -> list[str]:
    tail: list[str] = []
    for message in messages[-limit:]:
        role = _read_message_role(message) or "unknown"
        content = _shorten(_read_message_content(message), limit=900)
        if role == "assistant":
            tool_calls = getattr(message, "tool_calls", None)
            if isinstance(tool_calls, list) and tool_calls:
                names = [_read_tool_call_name(call) for call in tool_calls]
                names = [name for name in names if name]
                if names:
                    content = f"{content}\nassistant_tool_calls: {', '.join(names)}".strip()
        tail.append(f"[{role}]\n{content}")
    return tail


def _enum_values_for_tool(tool: Mapping[str, Any], property_name: str) -> list[str]:
    parameters = tool.get("parameters")
    if not isinstance(parameters, Mapping):
        return []
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return []
    prop = properties.get(property_name)
    if not isinstance(prop, Mapping):
        return []
    enum_values = prop.get("enum")
    if isinstance(enum_values, list):
        return [_as_text(item).strip() for item in enum_values if _as_text(item).strip()]
    return []


@dataclass(slots=True)
class MinecraftDecisionRequest:
    model: str
    messages: Sequence[Any]
    tools: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        return [tool["name"] for tool in self.tools if _as_text(tool.get("name")).strip()]

    @property
    def has_recent_tool_result(self) -> bool:
        return any(_read_message_role(message) == "tool" for message in self.messages[-4:])

    @property
    def latest_user_content(self) -> str:
        return _last_user_content(self.messages)


@dataclass(slots=True)
class MinecraftDecisionResult:
    mode: str
    content: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "elysia"

    @property
    def is_tool_call(self) -> bool:
        return self.mode == "tool" and bool(self.tool_name)


def parse_minecraft_decision_request(
    messages: Sequence[Any],
    tools: Sequence[Any] | None,
    *,
    model: str = "elysia-minecraft",
) -> MinecraftDecisionRequest | None:
    """Return a Minecraft decision request when the payload matches TLM's agent contract."""

    tool_specs = _iter_tool_specs(tools)
    if not tool_specs:
        return None

    tool_names = {spec["name"] for spec in tool_specs}
    if not (tool_names & _KNOWN_TLM_TOOLS):
        return None

    return MinecraftDecisionRequest(model=model, messages=messages, tools=tool_specs)


def build_decision_prompt(request: MinecraftDecisionRequest) -> str:
    """Compress Touhou Little Maid's OpenAI request into a main-consciousness prompt."""

    tool_lines: list[str] = []
    for index, tool in enumerate(request.tools, start=1):
        params = tool.get("parameters") if isinstance(tool.get("parameters"), Mapping) else {}
        try:
            params_text = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            params_text = "{}"
        tool_lines.append(
            f"{index}. {tool['name']}\n"
            f"   description: {_shorten(tool.get('description'), limit=420)}\n"
            f"   parameters: {_shorten(params_text, limit=900)}"
        )

    recent_tool_results = _recent_tool_results(request.messages)
    tool_result_text = "\n\n".join(recent_tool_results) if recent_tool_results else "无"

    return (
        "【Minecraft 实时事件 -> 爱莉决策】\n"
        "你正在作为统一主意识陪玩家玩 Minecraft。外部执行器会负责实际调用游戏工具；"
        "你只需要给出本轮决策，不要解释系统机制。\n"
        "提交方式：只调用 action-life_send_text 一次，content 必须是单行 JSON。"
        "系统只会读取这条 JSON；只有 mode=say 的 content 会作为女仆发言出现在游戏里。\n"
        "本轮不要调用表情包、文件、bash、web 或其他发送动作；action-think 可选，但最后只提交一条 JSON。\n"
        "不要把思考、解释、报错、Markdown、内部链路、发送失败或工具格式写进 say.content；"
        "不要编造工具名或参数。\n"
        "需要观察就选择 query_game_context；需要行动就选择本轮可用工具；"
        "只是回应玩家、确认你听得到或闲聊时选择 say，像爱莉本人在游戏里简短回应。\n\n"
        "允许提交的 JSON 格式二选一：\n"
        '{"mode":"tool","tool_name":"从可用工具中原样复制","arguments":{},"reason":"一句话原因"}\n'
        '{"mode":"say","content":"要让女仆说的话","reason":"一句话原因"}\n\n'
        f"model: {request.model}\n"
        f"tool_call_count_so_far: {_tool_call_count(request.messages)}\n\n"
        f"本轮最后玩家输入：{_shorten(request.latest_user_content, limit=700) or '无'}\n\n"
        "最近消息：\n"
        + ("\n\n".join(_message_tail(request.messages)) or "无")
        + "\n\n最近工具结果：\n"
        + tool_result_text
        + "\n\n可用工具：\n"
        + ("\n".join(tool_lines) if tool_lines else "无")
    )


def extract_decision_result(reply_text: str, request: MinecraftDecisionRequest) -> MinecraftDecisionResult | None:
    """Parse Elysia's reply and validate it against the current tool schemas."""

    parsed = _loads_json_object(reply_text)
    if parsed is None:
        match = _TOOL_NAME_RE.search(_as_text(reply_text))
        if not match:
            plain = _as_text(reply_text).strip()
            if not plain:
                return None
            return MinecraftDecisionResult(
                mode="say",
                content=_shorten(plain, limit=220),
                reason="爱莉直接用自然语言回应。",
                source="elysia_plain_text",
            )
        parsed = {"mode": "tool", "tool_name": match.group("name"), "arguments": {}}

    tool_names = set(request.tool_names)

    raw_tool_calls = parsed.get("tool_calls") or parsed.get("toolCalls")
    if isinstance(raw_tool_calls, list) and raw_tool_calls:
        first = raw_tool_calls[0]
        if isinstance(first, Mapping):
            function = first.get("function")
            if isinstance(function, Mapping):
                tool_name = _as_text(function.get("name")).strip()
                arguments = _loads_json_arguments(function.get("arguments")) or {}
                if tool_name in tool_names:
                    return MinecraftDecisionResult(
                        mode="tool",
                        tool_name=tool_name,
                        arguments=arguments,
                        reason=_shorten(parsed.get("reason") or "爱莉选择调用工具。", limit=220),
                    )

    mode = _as_text(parsed.get("mode") or parsed.get("type") or "").strip().lower()
    tool_name = _as_text(
        parsed.get("tool_name")
        or parsed.get("tool")
        or parsed.get("name")
        or parsed.get("function")
    ).strip()

    if mode == "tool" or tool_name:
        if tool_name not in tool_names:
            return None
        arguments = (
            _loads_json_arguments(parsed.get("arguments"))
            or _loads_json_arguments(parsed.get("args"))
            or _loads_json_arguments(parsed.get("parameters"))
            or {}
        )
        return MinecraftDecisionResult(
            mode="tool",
            tool_name=tool_name,
            arguments=arguments,
            reason=_shorten(parsed.get("reason") or parsed.get("reasoning") or "爱莉选择调用工具。", limit=220),
        )

    content = _as_text(parsed.get("content") or parsed.get("message") or parsed.get("say")).strip()
    if mode == "say" or content:
        if not content:
            return None
        return MinecraftDecisionResult(
            mode="say",
            content=content,
            reason=_shorten(parsed.get("reason") or parsed.get("reasoning") or "爱莉选择直接回复。", limit=220),
        )

    return None


def build_fallback_decision(request: MinecraftDecisionRequest, reason: str) -> MinecraftDecisionResult:
    """Return a safe fallback when Elysia is unavailable or replies invalid JSON."""

    if not request.has_recent_tool_result:
        query_tool = next((tool for tool in request.tools if tool.get("name") == "query_game_context"), None)
        if query_tool is not None:
            category_ids = _enum_values_for_tool(query_tool, "category_id")
            preferred = next(
                (value for value in category_ids if value in {"position", "nearby_entities", "equipment", "user"}),
                category_ids[0] if category_ids else "",
            )
            if preferred:
                return MinecraftDecisionResult(
                    mode="tool",
                    tool_name="query_game_context",
                    arguments={"category_id": preferred},
                    reason=f"operator fallback: {reason}",
                    source="operator_fallback",
                )

    return MinecraftDecisionResult(
        mode="say",
        content="我先稳一下，等看清楚情况再行动。",
        reason=f"operator fallback: {reason}",
        source="operator_fallback",
    )
