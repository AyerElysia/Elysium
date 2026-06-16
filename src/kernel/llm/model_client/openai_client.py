"""
OpenAI 模型客户端实现。

实现了 ChatModelClient、EmbeddingModelClient、RerankModelClient 协议，
基于 openai>=2.x SDK，支持异步聊天、流式输出、embedding 与 rerank。
"""

from __future__ import annotations

import base64
import inspect
import json
import threading
from pathlib import Path
from typing import Any, AsyncIterator

from json_repair import repair_json

from src.kernel.logger import get_logger
from src.kernel.llm.payload.tooling import LLMUsable
from src.kernel.llm.tool_call_compat import (
    build_tool_call_compat_prompt,
    parse_tool_call_compat_response,
)

from ..exceptions import LLMConfigurationError, LLMContentFilterError
from ..payload import Audio, Image, LLMPayload, ReasoningText, Text, ToolCall, ToolResult, Video
from ..roles import ROLE
from ..token_counter import count_payload_tokens
from .base import StreamEvent

logger = get_logger("kernel.llm.openai_client", display="OpenAI客户端")


def _log_openai_request_body(
    api_name: str,
    params: dict[str, Any],
    *,
    model_set: dict[str, Any] | None = None,
    payloads: list[LLMPayload] | None = None,
    request_name: str | None = None,
) -> int | None:
    """将 OpenAI 请求体送入请求检视器，便于在 WebUI 中核查 payload 结构。"""
    metadata: dict[str, Any] = {}
    if isinstance(model_set, dict):
        provider = model_set.get("api_provider") or model_set.get("client_type") or model_set.get("base_url")
        if provider is not None:
            metadata["api_provider"] = str(provider)
    if request_name:
        metadata["request_name"] = request_name
    if payloads:
        try:
            metadata["estimated_input_tokens"] = count_payload_tokens(
                payloads,
                model_identifier=str(params.get("model") or "cl100k_base"),
            )
        except Exception:
            pass
    try:
        from src.kernel.llm.request_inspector import capture
        return capture(api_name, params, metadata)
    except Exception:
        return None


def _build_httpx_timeout(timeout: float | None) -> Any:
    """根据总超时时间构造 httpx.Timeout 实例。

    Args:
        timeout: 总超时秒数，非正数或非数值时返回 None。

    Returns:
        httpx.Timeout 实例，或 None（不限超时）。
    """
    import httpx

    if not isinstance(timeout, (int, float)):
        return None

    total = float(timeout)
    if total <= 0:
        return None

    connect_timeout = min(total, 10.0)
    pool_timeout = min(total, 5.0)
    return httpx.Timeout(
        timeout=total,
        connect=connect_timeout,
        read=total,
        write=total,
        pool=pool_timeout,
    )


def _is_data_url(value: str) -> bool:
    """判断字符串是否为 data URL 格式。

    Args:
        value: 待判断字符串。

    Returns:
        是 data URL 则返回 True，否则 False。
    """
    return value.startswith("data:")


def _image_to_data_url(value: str) -> str:
    """将各种图片表示转换为 data URL 字符串。

    支持以下格式：
    - ``base64|<b64>``：已有 base64 内容
    - ``data:...``：已是 data URL，直接返回
    - 文件路径：读取并编码
    - 纯 base64 字符串

    Args:
        value: 图片表示字符串。

    Returns:
        ``data:image/png;base64,...`` 格式的 data URL。

    Raises:
        FileNotFoundError: 无法识别或文件不存在时抛出。
    """
    if value.startswith("base64|"):
        b64 = value.split("|", 1)[1]
        mime = _detect_image_mime_from_base64(b64) or "image/png"
        return f"data:{mime};base64,{b64}"

    if _is_data_url(value):
        return value

    # 尝试作为纯 base64 字符串处理（Image.value 规范化后为纯 base64）
    try:
        base64.b64decode(value, validate=True)
        mime = _detect_image_mime_from_base64(value) or "image/png"
        return f"data:{mime};base64,{value}"
    except Exception:
        pass

    path = Path(value)
    try:
        if path.exists() and path.is_file():
            data = path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            mime = _detect_image_mime_from_bytes(data) or "image/png"
            return f"data:{mime};base64,{b64}"
    except OSError:
        pass

    raise FileNotFoundError(f"Image file not found: {value}")


def _detect_image_mime_from_base64(value: str) -> str | None:
    try:
        cleaned = value.replace("\n", "").replace("\r", "").replace(" ", "")
        raw = base64.b64decode(cleaned, validate=True)
    except Exception:
        return None
    return _detect_image_mime_from_bytes(raw)


def _detect_image_mime_from_bytes(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


_AUDIO_MIME_TO_FORMAT: dict[str, str] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mpeg3": "mp3",
    "audio/x-mpeg-3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/vnd.wave": "wav",
}


def _audio_mime_to_format(mime_type: str) -> str:
    """根据 Audio.mime_type 反推 OpenAI input_audio.format 字段值。

    OpenAI Chat Completions API 仅接受 ``mp3`` / ``wav`` 两种 format 取值。
    未识别的 MIME（如 silk/amr）会保守降级为 ``mp3``，避免 KeyError，
    但调用方应在更上游就拦截不支持的格式（多模态预处理阶段降级为文本占位）。
    """
    if not mime_type:
        return "mp3"
    normalized = mime_type.lower().strip()
    if normalized in _AUDIO_MIME_TO_FORMAT:
        return _AUDIO_MIME_TO_FORMAT[normalized]
    if "wav" in normalized:
        return "wav"
    return "mp3"


def _video_to_data_url(value: str, mime: str = "video/mp4") -> str:
    """将各种视频表示转换为 data URL 字符串。"""
    if value.startswith("base64|"):
        b64 = value.split("|", 1)[1]
        return f"data:{mime};base64,{b64}"

    if _is_data_url(value):
        return value

    try:
        base64.b64decode(value, validate=True)
        return f"data:{mime};base64,{value}"
    except Exception:
        pass

    path = Path(value)
    try:
        if path.exists() and path.is_file():
            data = path.read_bytes()
            b64 = base64.b64encode(data).decode("ascii")
            ext = path.suffix.lower()
            ext_map = {
                ".mp4": "video/mp4",
                ".mov": "video/quicktime",
                ".webm": "video/webm",
                ".mkv": "video/x-matroska",
                ".avi": "video/x-msvideo",
            }
            guessed_mime = ext_map.get(ext, mime)
            return f"data:{guessed_mime};base64,{b64}"
    except OSError:
        pass

    raise FileNotFoundError(f"Video file not found: {value}")


def _messages_contain_native_video(messages: list[dict[str, Any]]) -> bool:
    """检查 messages 中是否包含原生视频内容块。"""
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if _is_native_video_message_item(item):
                return True
    return False


def _messages_contain_native_image(messages: list[dict[str, Any]]) -> bool:
    """检查 messages 中是否包含原生图片内容块。"""
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if _is_native_image_message_item(item):
                return True
    return False


def _messages_contain_life_inspect_media_promotion(messages: list[dict[str, Any]]) -> bool:
    """检查是否是 life_chatter 的按需媒体观察提升请求。"""

    marker = "tool-inspect_media"
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            if marker in content:
                return True
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, str) and marker in item:
                return True
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and marker in text:
                    return True
    return False


def _is_native_video_message_item(item: Any) -> bool:
    """判断单个 content item 是否承载原生视频输入。"""
    if not isinstance(item, dict):
        return False

    item_type = item.get("type")
    if item_type in {"video_url", "input_video"}:
        return True

    if item_type != "image_url":
        return False

    image_url_obj = item.get("image_url")
    url = ""
    if isinstance(image_url_obj, dict):
        url = str(image_url_obj.get("url") or "")
    elif isinstance(item.get("url"), str):
        url = str(item.get("url") or "")

    return url.startswith("data:video/")


def _is_native_image_message_item(item: Any) -> bool:
    """判断单个 content item 是否承载原生图片输入。"""
    if not isinstance(item, dict):
        return False

    if item.get("type") != "image_url":
        return False

    image_url_obj = item.get("image_url")
    url = ""
    if isinstance(image_url_obj, dict):
        url = str(image_url_obj.get("url") or "")
    elif isinstance(item.get("url"), str):
        url = str(item.get("url") or "")

    if url.startswith("data:video/"):
        return False
    return bool(url)


def _strip_native_video_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """移除 messages 中的视频内容块，返回新 messages 和移除数量。"""
    removed = 0
    rebuilt: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            rebuilt.append(msg)
            continue

        cloned = dict(msg)
        content = cloned.get("content")
        if not isinstance(content, list):
            rebuilt.append(cloned)
            continue

        new_content: list[Any] = []
        local_removed = 0
        for item in content:
            if _is_native_video_message_item(item):
                local_removed += 1
                continue
            new_content.append(item)

        if local_removed > 0 and not new_content:
            new_content.append({"type": "text", "text": "[视频原生输入降级为文本摘要]"})

        removed += local_removed
        cloned["content"] = new_content
        rebuilt.append(cloned)

    return rebuilt, removed


def _strip_native_image_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """移除 messages 中的图片内容块，返回新 messages 和移除数量。"""
    removed = 0
    rebuilt: list[dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            rebuilt.append(msg)
            continue

        cloned = dict(msg)
        content = cloned.get("content")
        if not isinstance(content, list):
            rebuilt.append(cloned)
            continue

        new_content: list[Any] = []
        local_removed = 0
        for item in content:
            if _is_native_image_message_item(item):
                local_removed += 1
                continue
            new_content.append(item)

        if local_removed > 0 and not new_content:
            new_content.append({"type": "text", "text": "[图片原生输入已降级为文字描述]"})

        removed += local_removed
        cloned["content"] = new_content
        rebuilt.append(cloned)

    return rebuilt, removed


def _is_native_video_unsupported_error(error: Exception) -> bool:
    """判断异常是否属于“视频输入不被当前端点支持”。"""
    error_name = type(error).__name__.lower()
    error_text = str(error).lower()
    haystack = f"{error_name} {error_text}"

    video_markers = ("video_url", "input_video", "video")
    unsupported_markers = (
        "unsupported",
        "not support",
        "not supported",
        "unknown",
        "unrecognized",
        "invalid",
        "invalid_request",
        "bad request",
        "content type",
        "schema",
    )

    image_format_markers = (
        "invalid image format",
        "only bmp/gif/png/jpeg/webp are supported",
        "param incorrect",
    )

    if any(marker in haystack for marker in image_format_markers):
        return True

    return any(marker in haystack for marker in video_markers) and any(
        marker in haystack for marker in unsupported_markers
    )


def _is_native_image_unsupported_error(error: Exception) -> bool:
    """判断异常是否属于“图片输入不被当前端点支持”。"""
    error_name = type(error).__name__.lower()
    error_text = str(error).lower()
    haystack = f"{error_name} {error_text}"

    image_markers = (
        "image input",
        "image_url",
        "input_image",
        "vision",
        "multimodal",
    )
    unsupported_markers = (
        "no endpoints found that support image input",
        "not support image input",
        "does not support image input",
        "unsupported image input",
        "image input not supported",
        "vision is not supported",
    )

    return any(marker in haystack for marker in image_markers) and any(
        marker in haystack for marker in unsupported_markers
    )


def _coerce_tool_arguments_json(args: Any) -> str:
    """将工具参数规范化为可安全回灌到 function.arguments 的 JSON 文本。"""
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return "{}"
        try:
            normalized = json.loads(stripped)
        except Exception:
            try:
                normalized = repair_json(stripped, return_objects=True)
            except Exception:
                logger.warning("检测到非法 tool_call.arguments，已回退为 JSON 字符串字面量")
                normalized = stripped
        return json.dumps(normalized, ensure_ascii=False)

    try:
        return json.dumps(args if args is not None else {}, ensure_ascii=False)
    except Exception:
        return json.dumps(str(args), ensure_ascii=False)


def _parse_tool_arguments(args_raw: Any) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    """尽量将上游返回的 function.arguments 解析为结构化 JSON。"""
    if args_raw is None:
        return {}
    if not isinstance(args_raw, str):
        return args_raw

    stripped = args_raw.strip()
    if not stripped:
        return {}

    try:
        return json.loads(stripped)
    except Exception:
        try:
            return repair_json(stripped, return_objects=True)
        except Exception:
            return args_raw


def _to_openai_tool(tool: Any) -> dict[str, Any]:
    """将单个 LLMUsable 工具转换为 OpenAI tools 格式。

    自动注入 ``reason`` 必填参数，帮助模型说明选用该工具的原因。

    Args:
        tool: 实现了 ``to_schema()`` 的工具对象。

    Returns:
        符合 OpenAI tools 格式的 dict。
    """
    schema = tool.to_schema()
    # 兼容两类 schema：
    # 1) 已经是 OpenAI tools 格式：{"type":"function","function":{...}}
    # 2) 仅 function schema：{"name":...,"description":...,"parameters":...}
    if schema.get("type") == "function" and "function" in schema:
        result: dict[str, Any] = schema
    else:
        result = {"type": "function", "function": schema}

    func = result.get("function", {})
    params = func.get("parameters", {})
    if isinstance(params, dict):
        _normalize_schema_for_grammar(params)
    props = params.get("properties", {})
    schema_has_reason = isinstance(props, dict) and "reason" in props
    execute_has_reason = _callable_accepts_reason(getattr(tool, "execute", None))
    if not schema_has_reason and not execute_has_reason:
        props["reason"] = {
            "type": "string",
            "description": "说明你选择此动作/工具的原因",
        }
        params["properties"] = props
        required: list[str] = params.get("required", [])
        if "reason" not in required:
            required.append("reason")
        params["required"] = required
        func["parameters"] = params
        result["function"] = func

    return result


def _callable_accepts_reason(callable_obj: Any) -> bool:
    """判断可调用对象是否显式接收 ``reason`` 参数。

    Args:
        callable_obj: 待检查的可调用对象（通常是组件 ``execute``）。

    Returns:
        bool: 若显式声明 ``reason`` 或存在 ``**kwargs`` 则返回 True。
    """
    if not callable(callable_obj):
        return False

    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False

    if "reason" in sig.parameters:
        return True

    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )


def _normalize_schema_for_grammar(schema: Any) -> None:
    """就地归一化 JSON Schema，提升与 grammar 编译器的兼容性。"""
    if isinstance(schema, list):
        for item in schema:
            _normalize_schema_for_grammar(item)
        return

    if not isinstance(schema, dict):
        return

    if schema.get("default") is None:
        schema.pop("default", None)

    if schema.get("type") == "array" and "items" not in schema:
        schema["items"] = {"type": "string"}

    if schema.get("type") == "object" and "properties" not in schema:
        schema.setdefault("additionalProperties", {"type": "string"})

    for key in (
        "properties",
        "items",
        "additionalProperties",
        "anyOf",
        "allOf",
        "oneOf",
    ):
        value = schema.get(key)
        if isinstance(value, dict):
            for child in value.values():
                _normalize_schema_for_grammar(child)
        elif isinstance(value, list):
            for child in value:
                _normalize_schema_for_grammar(child)


def _payloads_to_openai_messages(
    payloads: list[LLMPayload],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """将内部 LLMPayload 列表转换为 OpenAI messages 与 tools 格式。

    Args:
        payloads: 待转换的 payload 列表。

    Returns:
        二元组 (messages, tools)，均为 OpenAI API 所需的 dict 列表。
    """
    messages: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []

    for payload in payloads:
        if payload.role == ROLE.TOOL:
            # TOOL role 不进入 messages；只收集 tools schema
            for item in payload.content:
                tools.append(_to_openai_tool(item))
            continue

        if payload.role == ROLE.TOOL_RESULT:
            tool_payloads: list[tuple[str | None, str]] = []
            fallback_text: str | None = None

            for part in payload.content:
                if isinstance(part, ToolResult):
                    tool_payloads.append((part.call_id, part.to_text()))
                    continue

                if isinstance(part, Text) and fallback_text is None:
                    fallback_text = part.text
                    continue

                call_id_value = getattr(part, "call_id", None)
                call_id = call_id_value if isinstance(call_id_value, str) and call_id_value else None

                to_text = getattr(part, "to_text", None)
                if callable(to_text):
                    try:
                        text_value = to_text()
                        text = text_value if isinstance(text_value, str) else str(text_value)
                    except Exception:
                        text = ""
                    tool_payloads.append((call_id, text))

            if tool_payloads:
                for tool_call_id, content_text in tool_payloads:
                    messages.append(
                        {
                            "role": "tool",
                            "content": content_text,
                            **({"tool_call_id": tool_call_id} if tool_call_id else {}),
                        }
                    )
            else:
                messages.append(
                    {
                        "role": "tool",
                        "content": fallback_text or "",
                    }
                )
            continue

        role = payload.role.value

        if payload.role == ROLE.ASSISTANT:
            tool_calls_list: list[dict[str, Any]] = []
            text_parts: list[str] = []
            reasoning_parts: list[str] = []

            for idx, part in enumerate(payload.content):
                if isinstance(part, ToolCall):
                    args_text = _coerce_tool_arguments_json(part.args)
                    call_id = part.id or f"call_{idx}"
                    tool_calls_list.append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": part.name,
                                "arguments": args_text,
                            },
                        }
                    )
                    continue

                if isinstance(part, ReasoningText):
                    reasoning_parts.append(part.text)
                    continue

                if isinstance(part, Text):
                    text_parts.append(part.text)
                    continue

            reasoning_content = "".join(reasoning_parts)
            if tool_calls_list:
                message: dict[str, Any] = {
                    "role": role,
                    "content": "".join(text_parts),
                    "tool_calls": tool_calls_list,
                }
                if reasoning_content:
                    message["reasoning_content"] = reasoning_content
                messages.append(message)
                continue

            if all(isinstance(part, (Text, ReasoningText)) for part in payload.content):
                message = {"role": role, "content": "".join(text_parts)}
                if reasoning_content:
                    message["reasoning_content"] = reasoning_content
                messages.append(message)
                continue

        # 单纯文本消息走简洁格式
        if len(payload.content) == 1 and isinstance(payload.content[0], Text):
            messages.append({"role": role, "content": payload.content[0].text})
            continue

        # 多模态内容
        parts: list[dict[str, Any]] = []
        reasoning_parts: list[str] = []
        for part in payload.content:
            if isinstance(part, Text):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ReasoningText):
                reasoning_parts.append(part.text)
            elif isinstance(part, Image):
                url = _image_to_data_url(part.value)
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif isinstance(part, Audio):
                parts.append(
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": part.value,
                            "format": _audio_mime_to_format(part.mime_type),
                        },
                    }
                )
            elif isinstance(part, Video):
                data_url = f"data:{part.mime_type};base64,{part.value}"
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
            else:
                parts.append({"type": "text", "text": str(part)})

        message = {"role": role, "content": parts}
        if payload.role == ROLE.ASSISTANT and reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        messages.append(message)

    return messages, tools


def _parse_completion_message(
    msg: Any,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """从 OpenAI 响应消息对象中提取文本内容与工具调用列表。

    Args:
        msg: OpenAI ``ChatCompletionMessage`` 对象。

    Returns:
        三元组 (message_content, tool_calls, reasoning_content)。
        tool_calls 中每个元素形如 ``{"id": ..., "name": ..., "args": ...}``。
    """
    message_content: str = msg.content or ""
    tool_calls: list[dict[str, Any]] = []
    reasoning_content = _extract_reasoning_content(msg)

    if getattr(msg, "tool_calls", None):
        for tc in msg.tool_calls:
            args = _parse_tool_arguments(getattr(tc.function, "arguments", None))
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": args,
                }
            )

    # 兼容旧式 function_call（部分提供商）
    fn_call = getattr(msg, "function_call", None)
    fn_name = getattr(fn_call, "name", None) if fn_call is not None else None
    if not tool_calls and isinstance(fn_name, str) and fn_name:
        fn_args_raw = getattr(fn_call, "arguments", None)
        args = _parse_tool_arguments(fn_args_raw)
        tool_calls.append(
            {
                "id": None,
                "name": fn_name,
                "args": args,
            }
        )

    return message_content, tool_calls, reasoning_content


def _extract_reasoning_content(message: Any) -> str | None:
    """从 provider 响应对象中提取 reasoning_content。"""
    for attr_name in ("reasoning_content", "reasoning"):
        normalized = _normalize_reasoning_value(getattr(message, attr_name, None))
        if normalized:
            return normalized
    return None


def _normalize_reasoning_value(value: Any) -> str | None:
    """将不同 provider 的 reasoning 字段统一归一化为字符串。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            normalized = _normalize_reasoning_value(item)
            if normalized:
                chunks.append(normalized)
        return "".join(chunks) or None

    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text

    content = getattr(value, "content", None)
    if isinstance(content, str):
        return content

    if isinstance(value, dict):
        for key in ("text", "content", "reasoning_content", "reasoning"):
            normalized = _normalize_reasoning_value(value.get(key))
            if normalized:
                return normalized

    return None


def _should_backfill_reasoning_content(messages: list[dict[str, Any]]) -> bool:
    """当上下文中已存在 reasoning_content 时，为缺失的 assistant 历史统一回填。"""
    for message in messages:
        if message.get("role") != "assistant":
            continue
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content.strip():
            return True
    return False


def _usage_get_field(container: Any, key: str) -> Any:
    """从 dict / 对象两种形态中读取 usage 字段。"""
    if isinstance(container, dict):
        return container.get(key)
    return getattr(container, key, None)


def _as_int_or_none(value: Any) -> int | None:
    """将 usage 值规范为 int；无法转换时返回 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
            return parsed
        except Exception:
            return None
    return None


def _extract_usage_payload(resp: Any) -> dict[str, Any] | None:
    """从 provider 响应对象中提取 usage/caching 指标。"""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None

    prompt_tokens = _as_int_or_none(_usage_get_field(usage, "prompt_tokens"))
    completion_tokens = _as_int_or_none(_usage_get_field(usage, "completion_tokens"))
    total_tokens = _as_int_or_none(_usage_get_field(usage, "total_tokens"))

    prompt_details = _usage_get_field(usage, "prompt_tokens_details")
    cache_read_input_tokens = _as_int_or_none(
        _usage_get_field(usage, "cache_read_input_tokens")
    )
    cache_creation_input_tokens = _as_int_or_none(
        _usage_get_field(usage, "cache_creation_input_tokens")
    )

    # 兼容 OpenAI 以及部分兼容网关的 details 结构。
    if cache_read_input_tokens is None and prompt_details is not None:
        cache_read_input_tokens = _as_int_or_none(
            _usage_get_field(prompt_details, "cache_read_input_tokens")
        )
    if cache_creation_input_tokens is None and prompt_details is not None:
        cache_creation_input_tokens = _as_int_or_none(
            _usage_get_field(prompt_details, "cache_creation_input_tokens")
        )

    # OpenAI 官方字段：cached_tokens（等价于 cache_read_input_tokens）。
    if cache_read_input_tokens is None and prompt_details is not None:
        cache_read_input_tokens = _as_int_or_none(
            _usage_get_field(prompt_details, "cached_tokens")
        )

    data: dict[str, Any] = {}
    if prompt_tokens is not None:
        data["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        data["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        data["total_tokens"] = total_tokens
    if cache_read_input_tokens is not None:
        data["cache_read_input_tokens"] = cache_read_input_tokens
    if cache_creation_input_tokens is not None:
        data["cache_creation_input_tokens"] = cache_creation_input_tokens

    return data or None


# _ClientCacheKey: (api_key, base_url, loop_id, timeout, trust_env, force_ipv4)
_ClientCacheKey = tuple[str, str | None, int, float | None, bool, bool]


class OpenAIChatClient:
    """OpenAI 聊天、embedding 与 rerank 客户端。

    依赖 openai>=2.x，纯异步实现。

    配置来源：由上层传入的单个模型配置 dict（见 ``LLMRequest`` 的 model_set 约束）。

    支持的 extra_params 保留键（不传递给 API）：

    - ``trust_env``：是否信任系统代理环境变量，默认 True。
    - ``force_ipv4``：是否强制使用 IPv4 出口，默认 False。
    - ``context_reserve_ratio`` / ``context_reserve_tokens``：由上层策略使用，此处忽略。
    - ``context_compression_trigger_tokens`` / ``context_compression_trigger_ratio``：由上层上下文压缩策略使用，此处忽略。
    - ``force_sync_http``：已废弃，忽略。
    """

    def __init__(self) -> None:
        """初始化客户端，建立内部缓存与锁."""
        self._lock = threading.Lock()
        self._clients: dict[_ClientCacheKey, Any] = {}
        self._platform_info: Any = None
        self._last_usage: dict[str, Any] | None = None

    def _set_last_usage(self, usage: dict[str, Any] | None) -> None:
        """写入最近一次请求 usage（供上层指标采集）。"""
        with self._lock:
            self._last_usage = dict(usage) if isinstance(usage, dict) else None

    def pop_last_usage(self) -> dict[str, Any] | None:
        """弹出并清空最近一次请求 usage。"""
        with self._lock:
            usage = self._last_usage
            self._last_usage = None
            return dict(usage) if isinstance(usage, dict) else None

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _get_loop_key(self) -> int:
        """获取当前事件循环的唯一标识，无循环时返回 0。

        Returns:
            事件循环对象的 id，或 0。
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            return id(loop)
        except RuntimeError:
            return 0

    def _ensure_platform_info(self) -> Any:
        """懒加载并缓存 openai SDK 的平台信息，用于减少重复计算。

        Returns:
            平台信息对象，若获取失败则返回 None。
        """
        with self._lock:
            if self._platform_info is not None:
                return self._platform_info

        try:
            from openai._base_client import get_platform

            platform_info = get_platform()
        except Exception:
            platform_info = None

        with self._lock:
            if self._platform_info is None:
                self._platform_info = platform_info
        return self._platform_info

    def _get_client(
        self,
        *,
        api_key: str,
        base_url: str | None,
        timeout: float | None,
        trust_env: bool,
        force_ipv4: bool,
    ) -> Any:
        """获取或创建异步 AsyncOpenAI 客户端（按循环缓存）。

        Args:
            api_key: OpenAI 兼容 API 密钥。
            base_url: 自定义 base URL，None 表示使用默认。
            timeout: 总超时秒数，None 表示不限。
            trust_env: 是否信任系统代理环境变量。
            force_ipv4: 是否强制使用 IPv4 出口。

        Returns:
            AsyncOpenAI 实例。
        """
        loop_key = self._get_loop_key()
        timeout_key = float(timeout) if isinstance(timeout, (int, float)) else None
        cache_key: _ClientCacheKey = (
            api_key, base_url, loop_key, timeout_key, trust_env, force_ipv4
        )

        with self._lock:
            cached = self._clients.get(cache_key)
            if cached is not None:
                return cached

        from openai import AsyncOpenAI
        import httpx

        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=5,
            keepalive_expiry=10.0,
        )
        timeout_config = _build_httpx_timeout(timeout)
        base_transport = (
            httpx.AsyncHTTPTransport(local_address="0.0.0.0")
            if force_ipv4
            else httpx.AsyncHTTPTransport()
        )
        http_client_kwargs: dict[str, Any] = {
            "transport": base_transport,
            "trust_env": trust_env,
            "limits": limits,
        }
        if timeout_config is not None:
            http_client_kwargs["timeout"] = timeout_config

        http_client = httpx.AsyncClient(**http_client_kwargs)

        kwargs: dict[str, Any] = {"api_key": api_key, "http_client": http_client}
        if base_url:
            kwargs["base_url"] = base_url
        if isinstance(timeout, (int, float)):
            kwargs["timeout"] = float(timeout)
        # 重要：重试策略完全由 policy 控制，provider 侧必须禁用自动重试。
        kwargs["max_retries"] = 0

        client = AsyncOpenAI(**kwargs)
        try:
            platform_info = self._ensure_platform_info()
            if platform_info is not None:
                client._platform = platform_info
        except Exception:
            pass

        with self._lock:
            self._clients[cache_key] = client
        return client

    def _evict_client(
        self,
        *,
        api_key: str,
        base_url: str | None,
        timeout: float | None,
        trust_env: bool,
        force_ipv4: bool,
    ) -> Any | None:
        """从缓存中移除并返回对应的异步客户端（用于连接错误后强制重建）。

        Args:
            api_key: API 密钥。
            base_url: 自定义 base URL。
            timeout: 超时秒数。
            trust_env: 是否信任代理环境变量。
            force_ipv4: 是否强制 IPv4。

        Returns:
            被移除的客户端实例，若不存在则为 None。
        """
        loop_key = self._get_loop_key()
        timeout_key = float(timeout) if isinstance(timeout, (int, float)) else None
        cache_key: _ClientCacheKey = (
            api_key, base_url, loop_key, timeout_key, trust_env, force_ipv4
        )
        with self._lock:
            return self._clients.pop(cache_key, None)

    def _extract_model_params(
        self, model_set: dict[str, Any]
    ) -> tuple[str, str | None, float | None, bool, bool, dict[str, Any]]:
        """从 model_set dict 中解析并校验公共连接参数。

        Args:
            model_set: 单个模型配置字典。

        Returns:
            六元组 ``(api_key, base_url, timeout, trust_env, force_ipv4, extra_params)``。

        Raises:
            ValueError: api_key 为空或 extra_params 非 dict 时抛出。
        """
        api_key = str(model_set.get("api_key") or "")
        if not api_key:
            raise ValueError("model.api_key 不能为空")

        base_url = model_set.get("base_url")
        base_url = str(base_url) if base_url else None
        timeout = model_set.get("timeout")

        extra_params = model_set.get("extra_params")
        if extra_params is None:
            extra_params = {}
        if not isinstance(extra_params, dict):
            raise ValueError("model.extra_params 必须是 dict")

        extra_params = dict(extra_params)
        trust_env_raw = extra_params.pop("trust_env", None)
        trust_env = bool(trust_env_raw) if trust_env_raw is not None else True
        force_ipv4 = bool(extra_params.pop("force_ipv4", False))
        # 以下键由上层策略消费，client 侧不传给 API
        extra_params.pop("context_reserve_ratio", None)
        extra_params.pop("context_reserve_tokens", None)
        extra_params.pop("context_compression_trigger_tokens", None)
        extra_params.pop("context_compression_trigger_ratio", None)
        extra_params.pop("force_sync_http", None)

        timeout_float = float(timeout) if isinstance(timeout, (int, float)) else None
        return api_key, base_url, timeout_float, trust_env, force_ipv4, extra_params

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        model_name: str,
        payloads: list[LLMPayload],
        tools: list[LLMUsable],
        request_name: str,
        model_set: Any,
        stream: bool,
    ) -> tuple[str | None, list[dict[str, Any]] | None, AsyncIterator[StreamEvent] | None, str | None, int | None]:
        """发起一次聊天请求。

        Args:
            model_name: 模型名称（如 ``gpt-4o``）。
            payloads: 消息负载列表。
            tools: 工具定义列表（保持协议兼容，实际通过 payloads 中 ROLE.TOOL 传入）。
            request_name: 请求名称，用于追踪，此处不使用。
            model_set: 单个模型配置 dict。
            stream: 是否开启流式输出。

        Returns:
            五元组 ``(message, tool_calls, stream_iter, reasoning_content, request_record_id)``：

            - 非流时：``(完整文本, 工具调用列表, None, 推理内容, 记录 ID)``
            - 流式时：``(None, None, AsyncIterator[StreamEvent], None, 记录 ID)``

        Raises:
            TypeError: model_set 不是 dict 时抛出。
            ValueError: api_key 为空或 extra_params 非 dict 时抛出。
            LLMContentFilterError: 模型返回空 choices 时抛出。
        """
        del tools  # 通过 payloads 中 ROLE.TOOL 传入，此参数保持协议兼容

        if not isinstance(model_set, dict):
            raise TypeError("OpenAIChatClient 期望 model_set 为单个模型配置 dict")

        self._set_last_usage(None)
        api_key, base_url, timeout, trust_env, force_ipv4, extra_params = (
            self._extract_model_params(model_set)
        )
        # force_sync_http 已废弃，移除后不传给 API
        extra_params.pop("force_sync_http", None)

        client = self._get_client(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            force_ipv4=force_ipv4,
        )
        messages, openai_tools = _payloads_to_openai_messages(payloads)
        has_native_video = _messages_contain_native_video(messages)
        has_native_image = _messages_contain_native_image(messages)
        preserve_native_multimodal_errors = _messages_contain_life_inspect_media_promotion(messages)
        tool_call_compat = bool(model_set.get("tool_call_compat", False))

        if tool_call_compat and openai_tools:
            compat_prompt = build_tool_call_compat_prompt(openai_tools)
            messages = [*messages, {"role": "user", "content": compat_prompt}]

        max_tokens = model_set.get("max_tokens")
        temperature = model_set.get("temperature")

        params: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
        }
        if isinstance(max_tokens, int):
            params["max_tokens"] = max_tokens
        if isinstance(temperature, (int, float)):
            params["temperature"] = float(temperature)

        # 允许每模型注入额外参数（如 top_p/response_format/tool_choice 等）
        params.update(extra_params)

        # 判定是否开启了思考模式（兼容：SiliconFlow/DashScope 等参数名）
        # 思考模式通常与 tool_choice="required" 冲突。
        enable_thinking = extra_params.get("enable_thinking")
        thinking_enabled = bool(enable_thinking) if enable_thinking is not None else False

        if openai_tools and not tool_call_compat:
            params["tools"] = openai_tools
            if "tool_choice" not in params:
                # 默认使用 auto，避免强制 tool 调用带来的兼容性问题。
                # 如需强制调用，请在 model_set.extra_params 显式传入 tool_choice="required"。
                params["tool_choice"] = "auto"
        else:
            params.pop("tool_choice", None)

        # 新增：区分标准参数与非标准参数（统一走 extra_body，避免 openai SDK 因未知参数报错）
        standard_params = {
            "model", "messages", "max_tokens", "temperature", "top_p", "n", "stream",
            "stop", "presence_penalty", "frequency_penalty", "logit_bias", "user",
            "tools", "tool_choice", "response_format", "seed", "parallel_tool_calls",
            "functions", "function_call", "extra_body"
        }
        extra_body: dict[str, Any] = {}
        for key in list(params.keys()):
            if key not in standard_params:
                extra_body[key] = params.pop(key)
        if extra_body:
            existing = params.get("extra_body")
            if isinstance(existing, dict):
                merged = {**existing, **extra_body}
                params["extra_body"] = merged
            else:
                params["extra_body"] = extra_body

        # 若上下文中已存在带 reasoning_content 的 assistant 响应，
        # 或者启用了 thinking 模式（API 要求所有 assistant 消息必须包含 reasoning_content），
        # 则为其余缺失字段的 assistant 历史统一回填，避免 follow-up 400。
        thinking_enabled = bool(extra_params.get("enable_thinking"))
        if thinking_enabled or _should_backfill_reasoning_content(messages):
            for msg in messages:
                if (
                    msg.get("role") == "assistant"
                    and "reasoning_content" not in msg
                ):
                    content = msg.get("content")
                    msg["reasoning_content"] = content if isinstance(content, str) else ""

        request_record_id = _log_openai_request_body(
            "chat.completions.create",
            params,
            model_set=model_set,
            payloads=payloads,
            request_name=request_name,
        )

        if not stream:
            message_content, tool_calls, stream_iter, reasoning_content = await self._create_non_stream(
                client=client,
                params=params,
                tool_call_compat=tool_call_compat,
                openai_tools=openai_tools,
                has_native_video=has_native_video,
                has_native_image=has_native_image,
                preserve_native_multimodal_errors=preserve_native_multimodal_errors,
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                trust_env=trust_env,
                force_ipv4=force_ipv4,
                model_name=model_name,
            )
            return (
                message_content,
                tool_calls,
                stream_iter,
                reasoning_content,
                request_record_id,
            )

        message_content, tool_calls, stream_iter, reasoning_content = await self._create_stream(
            client=client,
            params=params,
            has_native_video=has_native_video,
            has_native_image=has_native_image,
            preserve_native_multimodal_errors=preserve_native_multimodal_errors,
        )
        return (
            message_content,
            tool_calls,
            stream_iter,
            reasoning_content,
            request_record_id,
        )

    async def _create_non_stream(
        self,
        *,
        client: Any,
        params: dict[str, Any],
        tool_call_compat: bool,
        openai_tools: list[dict[str, Any]],
        has_native_video: bool,
        has_native_image: bool,
        preserve_native_multimodal_errors: bool,
        api_key: str,
        base_url: str | None,
        timeout: float | None,
        trust_env: bool,
        force_ipv4: bool,
        model_name: str,
    ) -> tuple[str | None, list[dict[str, Any]] | None, None, str | None]:
        """执行非流式聊天请求并返回解析结果。

        遇到网络/超时异常时会驱逐缓存的客户端，以便下次请求重建连接。

        Args:
            client: AsyncOpenAI 实例。
            params: 请求参数 dict。
            tool_call_compat: 是否使用工具调用兼容模式。
            openai_tools: 已转换的 tools 列表。
            api_key: API 密钥（用于驱逐缓存）。
            base_url: base URL（用于驱逐缓存）。
            timeout: 超时（用于驱逐缓存）。
            trust_env: 代理环境变量开关（用于驱逐缓存）。
            force_ipv4: IPv4 强制标志（用于驱逐缓存）。
            model_name: 模型名称（用于错误信息）。

        Returns:
            四元组 ``(message_content, tool_calls, None, reasoning_content)``。

        Raises:
            LLMContentFilterError: 模型返回空 choices 时抛出。
        """
        try:
            resp = await client.chat.completions.create(**params)
        except Exception as e:
            if (
                has_native_video
                and not preserve_native_multimodal_errors
                and _is_native_video_unsupported_error(e)
            ):
                fallback_params = dict(params)
                original_messages = fallback_params.get("messages")
                if isinstance(original_messages, list):
                    downgraded_messages, removed = _strip_native_video_from_messages(original_messages)
                    if removed > 0:
                        logger.warning(
                            "检测到上游不支持原生视频输入，自动降级为文本摘要模式重试。"
                            f"移除视频块数量: {removed}"
                        )
                        fallback_params["messages"] = downgraded_messages
                        resp = await client.chat.completions.create(**fallback_params)
                    else:
                        raise
                else:
                    raise
            elif (
                has_native_image
                and not preserve_native_multimodal_errors
                and _is_native_image_unsupported_error(e)
            ):
                fallback_params = dict(params)
                original_messages = fallback_params.get("messages")
                if isinstance(original_messages, list):
                    downgraded_messages, removed = _strip_native_image_from_messages(original_messages)
                    if removed > 0:
                        logger.warning(
                            "检测到上游不支持原生图片输入，自动降级为文字描述模式重试。"
                            f"移除图片块数量: {removed}"
                        )
                        fallback_params["messages"] = downgraded_messages
                        resp = await client.chat.completions.create(**fallback_params)
                    else:
                        raise
                else:
                    raise
            else:
                err_name = type(e).__name__.lower()
                err_text = str(e).lower()
                if any(
                    kw in err_name or kw in err_text
                    for kw in ("timeout", "connect", "network", "transport")
                ):
                    stale = self._evict_client(
                        api_key=api_key,
                        base_url=base_url,
                        timeout=timeout,
                        trust_env=trust_env,
                        force_ipv4=force_ipv4,
                    )
                    if stale is not None:
                        try:
                            await stale.close()
                        except Exception:
                            pass
                raise

        self._set_last_usage(_extract_usage_payload(resp))
        if not resp.choices:
            raise LLMContentFilterError(
                f"模型返回空响应（可能触发了安全过滤器）。Response: {resp}",
                filter_type="empty_choices",
                model=model_name,
            )

        msg = resp.choices[0].message
        message_content, tool_calls, reasoning_content = _parse_completion_message(msg)

        if tool_call_compat and openai_tools and not tool_calls:
            parsed_message, parsed_calls = parse_tool_call_compat_response(
                message_content
            )
            return parsed_message, parsed_calls, None, reasoning_content

        return message_content, tool_calls, None, reasoning_content

    async def _create_stream(
        self,
        *,
        client: Any,
        params: dict[str, Any],
        has_native_video: bool,
        has_native_image: bool,
        preserve_native_multimodal_errors: bool,
    ) -> tuple[None, None, AsyncIterator[StreamEvent], None]:
        """执行流式聊天请求并返回事件迭代器。

        Args:
            client: AsyncOpenAI 实例。
            params: 请求参数 dict（不含 ``stream`` 键）。

        Returns:
            四元组 ``(None, None, AsyncIterator[StreamEvent], None)``。
        """
        try:
            stream_resp = await client.chat.completions.create(**params, stream=True)
        except Exception as e:
            if (
                has_native_video
                and not preserve_native_multimodal_errors
                and _is_native_video_unsupported_error(e)
            ):
                fallback_params = dict(params)
                original_messages = fallback_params.get("messages")
                if isinstance(original_messages, list):
                    downgraded_messages, removed = _strip_native_video_from_messages(original_messages)
                    if removed > 0:
                        logger.warning(
                            "流式请求检测到上游不支持原生视频输入，自动降级为文本摘要模式重试。"
                            f"移除视频块数量: {removed}"
                        )
                        fallback_params["messages"] = downgraded_messages
                        stream_resp = await client.chat.completions.create(**fallback_params, stream=True)
                    else:
                        raise
                else:
                    raise
            elif (
                has_native_image
                and not preserve_native_multimodal_errors
                and _is_native_image_unsupported_error(e)
            ):
                fallback_params = dict(params)
                original_messages = fallback_params.get("messages")
                if isinstance(original_messages, list):
                    downgraded_messages, removed = _strip_native_image_from_messages(original_messages)
                    if removed > 0:
                        logger.warning(
                            "流式请求检测到上游不支持原生图片输入，自动降级为文字描述模式重试。"
                            f"移除图片块数量: {removed}"
                        )
                        fallback_params["messages"] = downgraded_messages
                        stream_resp = await client.chat.completions.create(**fallback_params, stream=True)
                    else:
                        raise
                else:
                    raise
            else:
                raise

        async def iter_events() -> AsyncIterator[StreamEvent]:
            """逐块迭代流式响应，产出 StreamEvent。

            OpenAI 标准流格式：首包携带 tool_call id，后续增量包 id 为 None，
            通过 index 归属。使用 ``index_to_id`` 维护映射关系。
            """
            index_to_id: dict[int, str] = {}

            try:
                async for chunk in stream_resp:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    content = getattr(delta, "content", None)
                    if content:
                        yield StreamEvent(text_delta=content)

                    reasoning_content = _extract_reasoning_content(delta)
                    if reasoning_content:
                        yield StreamEvent(reasoning_delta=reasoning_content)

                    tool_calls_delta = getattr(delta, "tool_calls", None)
                    if tool_calls_delta:
                        for tc in tool_calls_delta:
                            fn = getattr(tc, "function", None)
                            tc_id: str | None = getattr(tc, "id", None)
                            tc_index: int | None = getattr(tc, "index", None)

                            if tc_id and tc_index is not None:
                                index_to_id[tc_index] = tc_id

                            effective_id = tc_id or (
                                index_to_id.get(tc_index)
                                if tc_index is not None
                                else None
                            )

                            yield StreamEvent(
                                tool_call_id=effective_id,
                                tool_name=getattr(fn, "name", None) if fn else None,
                                tool_args_delta=(
                                    getattr(fn, "arguments", None) if fn else None
                                ),
                            )

                    function_call_delta = getattr(delta, "function_call", None)
                    if function_call_delta and not tool_calls_delta:
                        yield StreamEvent(
                            tool_call_id="function_call",
                            tool_name=getattr(function_call_delta, "name", None),
                            tool_args_delta=getattr(
                                function_call_delta, "arguments", None
                            ),
                        )
            finally:
                close = getattr(stream_resp, "aclose", None)
                if callable(close):
                    maybe_awaitable = close()
                    if inspect.isawaitable(maybe_awaitable):
                        await maybe_awaitable
                    return

                close_sync = getattr(stream_resp, "close", None)
                if callable(close_sync):
                    maybe_awaitable = close_sync()
                    if inspect.isawaitable(maybe_awaitable):
                        await maybe_awaitable

        return None, None, iter_events(), None

    async def create_embedding(
        self,
        *,
        model_name: str,
        inputs: list[str],
        request_name: str,
        model_set: Any,
    ) -> list[list[float]]:
        """发起 embedding 请求，返回向量列表。

        Args:
            model_name: embedding 模型名称。
            inputs: 待向量化的文本列表，不能为空。
            request_name: 请求名称，用于追踪，此处不使用。
            model_set: 单个模型配置 dict。

        Returns:
            与 ``inputs`` 等长的向量列表，每个向量为 float 列表。

        Raises:
            TypeError: model_set 不是 dict 时抛出。
            ValueError: api_key 为空、inputs 为空或 extra_params 非 dict 时抛出。
        """
        del request_name

        if not isinstance(model_set, dict):
            raise TypeError("OpenAIChatClient 期望 model_set 为单个模型配置 dict")
        if not inputs:
            raise ValueError("inputs 不能为空")

        api_key, base_url, timeout, trust_env, force_ipv4, extra_params = (
            self._extract_model_params(model_set)
        )
        client = self._get_client(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            force_ipv4=force_ipv4,
        )

        params: dict[str, Any] = {
            "model": model_name,
            "input": inputs,
        }
        # 过滤掉 embedding API 不支持的参数
        embedding_safe_params = {
            k: v for k, v in extra_params.items()
            if k not in ("enable_thinking", "thinking", "reasoning_effort")
        }
        params.update(embedding_safe_params)

        _log_openai_request_body("embeddings.create", params)
        resp = await client.embeddings.create(**params)
        data = getattr(resp, "data", None)
        if not data:
            return []

        out: list[list[float]] = []
        for item in data:
            vec = getattr(item, "embedding", None)
            if isinstance(vec, list):
                out.append([float(v) for v in vec])
        return out

    async def create_rerank(
        self,
        *,
        model_name: str,
        query: str,
        documents: list[Any],
        top_n: int | None,
        request_name: str,
        model_set: Any,
    ) -> list[dict[str, Any]]:
        """发起 rerank 请求，返回按相关性降序排列的结果。

        仅支持提供商 SDK 原生 ``rerank`` 接口。
        若 SDK 不存在该接口，抛出 ``LLMConfigurationError`` 交由上级处理。

        Args:
            model_name: rerank 模型名称。
            query: 查询文本，不能为空。
            documents: 待排序的文档列表（str 或 dict），不能为空。
            top_n: 返回结果数量上限，None 表示全部返回。
            request_name: 请求名称，用于追踪，此处不使用。
            model_set: 单个模型配置 dict。

        Returns:
            每个元素为 ``{"index": int, "score": float, "document": Any}``，
            按 score 降序排列。

        Raises:
            TypeError: model_set 不是 dict 时抛出。
            ValueError: query/documents 为空或 extra_params 非 dict 时抛出。
            LLMConfigurationError: 当前 SDK 不支持原生 rerank 接口时抛出。
        """
        del request_name

        if not isinstance(model_set, dict):
            raise TypeError("OpenAIChatClient 期望 model_set 为单个模型配置 dict")
        if not query:
            raise ValueError("query 不能为空")
        if not documents:
            raise ValueError("documents 不能为空")

        api_key, base_url, timeout, trust_env, force_ipv4, extra_params = (
            self._extract_model_params(model_set)
        )
        client = self._get_client(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            force_ipv4=force_ipv4,
        )

        # 尝试调用 SDK 原生 rerank 接口
        rerank_api = getattr(client, "rerank", None)
        rerank_create = (
            getattr(rerank_api, "create", None) if rerank_api is not None else None
        )
        if callable(rerank_create):
            params: dict[str, Any] = {
                "model": model_name,
                "query": query,
                "documents": documents,
            }
            if isinstance(top_n, int) and top_n > 0:
                params["top_n"] = top_n

            _log_openai_request_body("rerank.create", params)
            maybe_resp = rerank_create(**params)
            if inspect.isawaitable(maybe_resp):
                resp = await maybe_resp
            else:
                resp = maybe_resp

            data = (
                getattr(resp, "results", None)
                or getattr(resp, "data", None)
                or []
            )
            out: list[dict[str, Any]] = []
            for rec in data:
                idx = getattr(rec, "index", None)
                score = getattr(rec, "relevance_score", None)
                if score is None:
                    score = getattr(rec, "score", None)
                index = int(idx) if isinstance(idx, int) else 0
                out.append(
                    {
                        "index": index,
                        "score": float(score) if isinstance(score, (int, float)) else 0.0,
                        "document": documents[index] if 0 <= index < len(documents) else None,
                    }
                )
            return out

        raise LLMConfigurationError(
            f"当前 SDK 客户端不支持原生 rerank 接口，模型：{model_name}"
        )

    async def create_transcription(
        self,
        *,
        model_name: str,
        audio_bytes: bytes,
        request_name: str,
        model_set: Any,
    ) -> str:
        """发起语音转文字请求（OpenAI audio/transcriptions 接口）。

        Args:
            model_name: ASR 模型名称。
            audio_bytes: WAV 格式的音频字节数据。
            request_name: 请求名称，用于追踪，此处不使用。
            model_set: 单个模型配置 dict。

        Returns:
            识别出的文字内容。

        Raises:
            TypeError: model_set 不是 dict 时抛出。
            ValueError: api_key 为空或 extra_params 非 dict 时抛出。
        """
        import io

        del request_name

        if not isinstance(model_set, dict):
            raise TypeError("OpenAIChatClient 期望 model_set 为单个模型配置 dict")

        api_key, base_url, timeout, trust_env, force_ipv4, _ = (
            self._extract_model_params(model_set)
        )
        client = self._get_client(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
            force_ipv4=force_ipv4,
        )

        resp = await client.audio.transcriptions.create(
            model=model_name,
            file=("audio.wav", io.BytesIO(audio_bytes), "audio/wav"),
        )
        text = getattr(resp, "text", None)
        return text if isinstance(text, str) else str(resp)
