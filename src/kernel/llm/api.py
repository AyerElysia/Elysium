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

from ..config.unified import get_config
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
    """将路由名解析为 model_set（list[dict]），兼容老 LLMRequest 格式。"""
    cfg = get_config()

    # 1. 从 routing 找到模型名
    model_name = cfg.llm.routing.get(routing_name, routing_name)

    # 2. 从 models 找模型配置
    model_cfg = cfg.llm.models.get(model_name)
    if model_cfg is None:
        # 回退：尝试从老 model_config 获取
        return _legacy_model_set(routing_name)

    # 3. 从 providers 找 provider 配置
    provider_cfg = cfg.llm.providers.get(model_cfg.provider)
    if provider_cfg is None:
        raise ValueError(
            f"模型 '{model_name}' 引用了未配置的 provider '{model_cfg.provider}'"
        )

    # 4. 构建 model_set entry（兼容 LLMRequest 格式）
    api_key = provider_cfg.api_key
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else ""

    entry: dict[str, Any] = {
        "api_provider": model_cfg.provider,
        "base_url": provider_cfg.base_url,
        "model_identifier": model_cfg.model,
        "api_key": api_key,
        "client_type": provider_cfg.client_type,
        "max_tokens": model_cfg.max_tokens,
        "temperature": model_cfg.temperature,
        "top_p": model_cfg.top_p,
        "max_retry": provider_cfg.max_retry,
        "timeout": provider_cfg.timeout,
    }
    entry.update(model_cfg.extra)
    return [entry]


def _legacy_model_set(task_name: str) -> list[dict[str, Any]]:
    """兼容回退：从老 ModelConfig 获取 model_set。"""
    try:
        from src.core.config.model_config import get_model_config

        mc = get_model_config()
        # 尝试直接匹配任务名
        try:
            return mc.get_task(task_name)
        except (ValueError, KeyError):
            pass
        # 回退到 "actor" 作为默认任务
        if task_name in ("default", "main"):
            return mc.get_task("actor")
        raise ValueError(f"任务 '{task_name}' 未找到")
    except Exception as e:
        raise ValueError(
            f"无法解析模型路由 '{task_name}'：统一配置中未定义，老配置也不可用 ({e})"
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
    from .request import LLMRequest
    from .payload import LLMPayload, Text, ROLE
    from .payload.tooling import ToolRegistry

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
    req = LLMRequest(model_set=model_set, payloads=payloads, request_name=f"chat.{model}")

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
    from .request import LLMRequest
    from .payload import LLMPayload, Text, ROLE
    from .payload.tooling import ToolRegistry

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

    req = LLMRequest(model_set=model_set, payloads=payloads, request_name=f"stream.{model}")

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
