"""LLM 快捷 API。

提供极简的 chat() / stream() 入口，屏蔽底层 model_set / policy / payload 细节。
高级用户仍可直接使用 LLMRequest。

用法：
    from src.kernel.llm.api import chat, stream

    # 单轮对话
    resp = await chat("你好，介绍一下自己")
    print(resp.text)

    # 指定路由
    resp = await chat("快速回答", model="fast")

    # 带工具
    resp = await chat("查一下天气", tools=[weather_tool_schema])

    # 流式
    async for chunk in stream("写一首诗"):
        print(chunk.delta, end="")

    # 多轮
    resp = await chat([
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"},
    ])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from ..logger import get_logger

logger = get_logger("llm.api", display="LLM-API", enable_event_broadcast=False)


# ─────────────────────────────────────────────
# 响应封装
# ─────────────────────────────────────────────


@dataclass
class ChatResponse:
    """chat() 的返回值。"""

    text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    model: str = ""
    raw: Any = None  # 原始 LLMResponse，高级用途


@dataclass
class StreamChunk:
    """stream() 每次 yield 的片段。"""

    delta: str = ""
    tool_call_delta: dict[str, Any] | None = None
    finished: bool = False
    usage: dict[str, int] | None = None


# ─────────────────────────────────────────────
# 内部：路由解析
# ─────────────────────────────────────────────


def _resolve_model_set(routing_name: str) -> list[dict[str, Any]]:
    """Resolve a task or registered model from authoritative ``models.toml``.

    Automatic fallback to ``elysium.toml`` or legacy ``model.toml`` is
    intentionally forbidden: a production routing error must be visible and
    must not switch the running system to an unreviewed priority chain.
    """

    from ..config.models_loader import get_models_config

    registry = get_models_config()
    resolved_name = (
        "expression" if routing_name in ("default", "main") else routing_name
    )
    if resolved_name in registry.tasks:
        return registry.get_task(resolved_name)

    model = registry.get_model_entry(resolved_name)
    if model is not None:
        return [model]

    raise ValueError(
        f"模型路由 '{routing_name}' 未在权威配置 "
        f"{registry.snapshot.source_path} 中定义 "
        f"(snapshot={registry.snapshot.digest})"
    )


# ─────────────────────────────────────────────
# 公共 API
# ─────────────────────────────────────────────


async def chat(
    messages: str | list[dict[str, Any]],
    *,
    model: str = "default",
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> ChatResponse:
    """单轮/多轮对话（收集全量响应）。

    Args:
        messages: 字符串（单条用户消息）或消息列表
        model: 路由名（对应 config 中 llm.routing 的 key）
        tools: 工具 schema 列表
        temperature: 覆盖温度
        max_tokens: 覆盖最大 token

    Returns:
        ChatResponse 包含 text, tool_calls, usage
    """
    from .payload import ROLE, LLMPayload, Text
    from .payload.tooling import ToolRegistry
    from .request import LLMRequest

    model_set = _resolve_model_set(model)

    # 覆盖参数
    if temperature is not None:
        for entry in model_set:
            entry["temperature"] = temperature
    if max_tokens is not None:
        for entry in model_set:
            entry["max_tokens"] = max_tokens

    # 构建 payloads
    payloads: list[LLMPayload] = []
    if isinstance(messages, str):
        payloads.append(LLMPayload(role=ROLE.USER, content=[Text(text=messages)]))
    else:
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            payloads.append(LLMPayload(role=role, content=[Text(text=content)]))

    # 构建请求
    req = LLMRequest(
        model_set=model_set, payloads=payloads, request_name=f"chat.{model}"
    )

    # 工具
    if tools:
        registry = ToolRegistry()
        for t in tools:
            registry.register_raw(t)
        req._tool_registry = registry

    # 发送
    response = await req.send(auto_append_response=False, stream=False)
    text = await response.collect_text()

    return ChatResponse(
        text=text,
        tool_calls=getattr(response, "tool_calls", []),
        usage=getattr(response, "usage", {}),
        model=model_set[0].get("model_identifier", ""),
        raw=response,
    )


async def stream(
    messages: str | list[dict[str, Any]],
    *,
    model: str = "default",
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    **kwargs: Any,
) -> AsyncGenerator[StreamChunk, None]:
    """流式对话。

    Args:
        messages: 字符串或消息列表
        model: 路由名
        tools: 工具 schema 列表

    Yields:
        StreamChunk，每次包含增量文本
    """
    from .payload import ROLE, LLMPayload, Text
    from .payload.tooling import ToolRegistry
    from .request import LLMRequest

    model_set = _resolve_model_set(model)

    if temperature is not None:
        for entry in model_set:
            entry["temperature"] = temperature
    if max_tokens is not None:
        for entry in model_set:
            entry["max_tokens"] = max_tokens

    payloads: list[LLMPayload] = []
    if isinstance(messages, str):
        payloads.append(LLMPayload(role=ROLE.USER, content=[Text(text=messages)]))
    else:
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            payloads.append(LLMPayload(role=role, content=[Text(text=content)]))

    req = LLMRequest(
        model_set=model_set, payloads=payloads, request_name=f"stream.{model}"
    )

    if tools:
        registry = ToolRegistry()
        for t in tools:
            registry.register_raw(t)
        req._tool_registry = registry

    response = await req.send(auto_append_response=False, stream=True)

    async for chunk in response:
        delta = ""
        if hasattr(chunk, "delta") and chunk.delta:
            delta = chunk.delta
        elif hasattr(chunk, "text"):
            delta = chunk.text or ""

        yield StreamChunk(
            delta=delta,
            finished=getattr(chunk, "finished", False),
        )
