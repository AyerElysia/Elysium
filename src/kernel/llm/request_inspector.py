"""LLM 请求体调试检视器。

提供一个基于 FastAPI + SSE 的 Web 界面，实时记录并展示每次
OpenAI 兼容 API 的完整请求体，方便调试 payload 结构。

除原始 JSON 外，本模块还会将请求体转换为结构化渲染模型，
用于以对话式布局展示 role、工具调用、工具结果与 Markdown 文本。

使用方式：
    # 在 HTTP 服务器启动后调用一次
    from src.kernel.llm.request_inspector import get_inspector
    get_inspector().mount(fastapi_app)

    # 在发起请求前调用
    from src.kernel.llm.request_inspector import capture
    capture("chat.completions.create", params)

WebUI 地址：http://<host>:<port>/_inspector/
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .payload.media import redact_media_sources


_OVERVIEW_FIELDS: list[tuple[str, str]] = [
    ("stream", "流式"),
    ("temperature", "temperature"),
    ("max_tokens", "max_tokens"),
    ("top_p", "top_p"),
    ("presence_penalty", "presence_penalty"),
    ("frequency_penalty", "frequency_penalty"),
    ("seed", "seed"),
    ("tool_choice", "tool_choice"),
    ("parallel_tool_calls", "parallel_tool_calls"),
    ("response_format", "response_format"),
]

_ROLE_LABELS: dict[str, str] = {
    "system": "System",
    "user": "User",
    "assistant": "Assistant",
    "tool": "Tool",
    "tool_result": "Tool Result",
}


def _json_dumps(value: Any) -> str:
    """将值安全序列化为 JSON 字符串。"""
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return str(value)


def _format_scalar(value: Any) -> str:
    """将任意标量格式化为便于展示的字符串。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return _json_dumps(value)
    return str(value)


def _build_overview(
    api_name: str,
    model: str,
    params: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, str]]:
    """构造顶层请求摘要。"""
    messages = _get_inspector_messages(params)
    tools = params.get("tools", [])
    overview = [
        {"label": "API", "value": api_name},
        {"label": "提供商", "value": _format_scalar(metadata.get("api_provider", "-"))},
        {"label": "模型", "value": model or "-"},
        {
            "label": "消息数",
            "value": str(len(messages) if isinstance(messages, list) else 0),
        },
        {"label": "工具数", "value": str(len(tools) if isinstance(tools, list) else 0)},
    ]

    estimated_input_tokens = metadata.get("estimated_input_tokens")
    if estimated_input_tokens is not None:
        overview.append(
            {
                "label": "预估输入 Tokens",
                "value": _format_scalar(estimated_input_tokens),
            }
        )

    request_name = metadata.get("request_name")
    if request_name:
        overview.append({"label": "请求名称", "value": _format_scalar(request_name)})

    for key, label in _OVERVIEW_FIELDS:
        if key in params:
            overview.append({"label": label, "value": _format_scalar(params[key])})

    reserved_keys = {"messages", "tools", "model", "system"}
    reserved_keys.update(name for name, _ in _OVERVIEW_FIELDS)
    for key in sorted(key for key in params.keys() if key not in reserved_keys):
        overview.append({"label": key, "value": _format_scalar(params[key])})

    return overview


def _schema_type_text(schema: dict[str, Any]) -> str:
    """读取 JSON Schema 的类型信息。"""
    schema_type = schema.get("type")
    if isinstance(schema_type, str):
        return schema_type
    if isinstance(schema_type, list):
        return " | ".join(str(item) for item in schema_type)
    if "anyOf" in schema:
        return "anyOf"
    if "oneOf" in schema:
        return "oneOf"
    if "allOf" in schema:
        return "allOf"
    return "unknown"


def _build_tools_view(params: dict[str, Any]) -> list[dict[str, Any]]:
    """将 tools schema 转为更适合前端展示的结构。"""
    tools = params.get("tools", [])
    if not isinstance(tools, list):
        return []

    rendered_tools: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, dict):
            rendered_tools.append(
                {
                    "index": index,
                    "name": f"tool_{index}",
                    "kind": "unknown",
                    "description": "",
                    "required": [],
                    "properties": [],
                    "raw_json": _json_dumps(tool),
                }
            )
            continue

        function_obj = tool.get("function")
        if isinstance(function_obj, dict):
            function_schema: dict[str, Any] = function_obj
        else:
            function_schema = tool
        parameters_obj = function_schema.get("parameters")
        if not isinstance(parameters_obj, dict):
            parameters_obj = function_schema.get("input_schema")
        parameters: dict[str, Any] = (
            parameters_obj if isinstance(parameters_obj, dict) else {}
        )
        properties_obj = parameters.get("properties")
        properties: dict[str, Any] = (
            properties_obj if isinstance(properties_obj, dict) else {}
        )
        required_obj = parameters.get("required")
        required: list[Any] = required_obj if isinstance(required_obj, list) else []
        rendered_tools.append(
            {
                "index": index,
                "name": str(function_schema.get("name", f"tool_{index}")),
                "kind": str(tool.get("type", "function")),
                "description": str(function_schema.get("description", "")),
                "required": [str(item) for item in required],
                "properties": [
                    {
                        "name": str(prop_name),
                        "type": (
                            _schema_type_text(prop_schema)
                            if isinstance(prop_schema, dict)
                            else "unknown"
                        ),
                        "description": (
                            str(prop_schema.get("description", ""))
                            if isinstance(prop_schema, dict)
                            else ""
                        ),
                        "required": prop_name in required,
                    }
                    for prop_name, prop_schema in properties.items()
                ],
                "raw_json": _json_dumps(tool),
            }
        )
    return rendered_tools


def _make_block(block_type: str, **kwargs: Any) -> dict[str, Any]:
    """构造统一的消息块结构。"""
    block = {"type": block_type}
    block.update(kwargs)
    return block


def _render_unknown_content(content: Any, label: str = "未知内容") -> dict[str, Any]:
    """为未知结构构造回退块。"""
    return _make_block("unknown", label=label, text=_json_dumps(content))


def _render_content_list(content: list[Any]) -> list[dict[str, Any]]:
    """将 OpenAI content 数组转换为消息块。"""
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append(_make_block("markdown", text=item))
            continue

        if not isinstance(item, dict):
            blocks.append(_render_unknown_content(item))
            continue

        item_type = str(item.get("type", "unknown"))
        if item_type in {"text", "input_text", "output_text"}:
            blocks.append(_make_block("markdown", text=str(item.get("text", ""))))
            continue

        if item_type in {"image_url", "input_image"}:
            image_url_obj = item.get("image_url")
            image_url: dict[str, Any] = (
                image_url_obj if isinstance(image_url_obj, dict) else {}
            )
            url = image_url.get("url") or item.get("url") or ""
            blocks.append(
                _make_block(
                    "media",
                    media_type="image",
                    title="图片内容",
                    text="请求中包含图片输入。",
                    meta=_format_scalar(url)[:120],
                )
            )
            continue

        if item_type == "image":
            source_obj = item.get("source")
            source = source_obj if isinstance(source_obj, dict) else {}
            meta = source.get("media_type") or source.get("type") or "image"
            blocks.append(
                _make_block(
                    "media",
                    media_type="image",
                    title="图片内容",
                    text="请求中包含图片输入。",
                    meta=_format_scalar(meta),
                )
            )
            continue

        if item_type in {"audio", "input_audio", "output_audio"}:
            blocks.append(
                _make_block(
                    "media",
                    media_type="audio",
                    title="音频内容",
                    text="请求中包含音频输入。",
                    meta="音频数据已省略",
                )
            )
            continue

        if item_type in {"video_url", "input_video"}:
            video_url_obj = item.get("video_url")
            video_url: dict[str, Any] = video_url_obj if isinstance(video_url_obj, dict) else {}
            url = video_url.get("url") or item.get("url") or ""
            blocks.append(
                _make_block(
                    "media",
                    media_type="video",
                    title="视频内容",
                    text="请求中包含视频输入。",
                    meta=_format_scalar(url)[:120],
                )
            )
            continue

        if item_type == "refusal":
            blocks.append(
                _make_block(
                    "markdown", text=str(item.get("refusal", "")), label="拒绝说明"
                )
            )
            continue

        if item_type == "thinking":
            blocks.append(
                _make_block(
                    "markdown",
                    text=str(item.get("thinking", "")),
                    label="Reasoning",
                )
            )
            continue

        if item_type == "tool_use":
            blocks.append(
                _make_block(
                    "tool_call",
                    call_id=str(item.get("id", "") or ""),
                    name=str(item.get("name", "unknown_tool")),
                    arguments_text=_json_dumps(item.get("input", {})),
                )
            )
            continue

        if item_type == "tool_result":
            blocks.append(
                _make_block(
                    "tool_result",
                    name=str(item.get("tool_name", "")),
                    call_id=str(item.get("tool_use_id", "") or ""),
                    text=_format_scalar(item.get("content", "")),
                )
            )
            continue

        blocks.append(
            _render_unknown_content(item, label=f"未知 content 类型: {item_type}")
        )
    return blocks


def _get_inspector_messages(params: dict[str, Any]) -> list[Any]:
    """获取用于检视器展示的消息列表，兼容 Anthropic 的 system 字段。"""
    rendered_messages: list[Any] = []

    system = params.get("system")
    if isinstance(system, str):
        rendered_messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        rendered_messages.append({"role": "system", "content": system})

    messages = params.get("messages", [])
    if isinstance(messages, list):
        rendered_messages.extend(messages)

    return rendered_messages


def _build_message_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """将单条 message 转换为前端可渲染的块列表。"""
    blocks: list[dict[str, Any]] = []
    role = str(message.get("role", "unknown"))
    content = message.get("content")

    if role == "tool":
        tool_name = str(message.get("name", ""))
        blocks.append(
            _make_block(
                "tool_result",
                name=tool_name,
                call_id=str(message.get("tool_call_id", "") or ""),
                text=_format_scalar(content if content is not None else ""),
            )
        )
    elif isinstance(content, str):
        blocks.append(_make_block("markdown", text=content))
    elif isinstance(content, list):
        blocks.extend(_render_content_list(content))
    elif content is None:
        blocks.append(_make_block("empty", text="此消息没有文本内容。"))
    else:
        blocks.append(_render_unknown_content(content))

    reasoning_content = message.get("reasoning_content")
    if isinstance(reasoning_content, str) and reasoning_content.strip():
        blocks.append(
            _make_block("markdown", text=reasoning_content, label="Reasoning")
        )

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                blocks.append(_render_unknown_content(tool_call, label="未知工具调用"))
                continue
            function_block_obj = tool_call.get("function")
            function_block: dict[str, Any] = (
                function_block_obj if isinstance(function_block_obj, dict) else {}
            )
            name = str(
                function_block.get("name", tool_call.get("name", "unknown_tool"))
            )
            arguments = function_block.get("arguments", tool_call.get("args", {}))
            arguments_text = (
                arguments if isinstance(arguments, str) else _json_dumps(arguments)
            )
            blocks.append(
                _make_block(
                    "tool_call",
                    call_id=str(tool_call.get("id", "") or ""),
                    name=name,
                    arguments_text=arguments_text,
                )
            )

    if not blocks:
        blocks.append(_make_block("empty", text="此消息没有可展示内容。"))

    return blocks


def _build_messages_view(params: dict[str, Any]) -> list[dict[str, Any]]:
    """将请求中的 messages 转换为卡片化展示模型。"""
    messages = _get_inspector_messages(params)
    if not messages:
        return []

    rendered_messages: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            rendered_messages.append(
                {
                    "index": index,
                    "role": "unknown",
                    "label": "Unknown",
                    "meta": "非标准 message 结构",
                    "blocks": [_render_unknown_content(message)],
                }
            )
            continue

        role = str(message.get("role", "unknown"))
        meta_parts: list[str] = []
        if isinstance(message.get("name"), str) and message["name"]:
            meta_parts.append(f"name: {message['name']}")
        if isinstance(message.get("tool_call_id"), str) and message["tool_call_id"]:
            meta_parts.append(f"tool_call_id: {message['tool_call_id']}")
        if isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
            meta_parts.append(f"tools: {len(message['tool_calls'])}")

        rendered_messages.append(
            {
                "index": index,
                "role": role,
                "label": _ROLE_LABELS.get(role, role.title()),
                "meta": " · ".join(meta_parts),
                "blocks": _build_message_blocks(message),
            }
        )

    return rendered_messages


def _build_response_view(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    """将单轮响应转换为可复用的消息卡片结构。"""
    if not isinstance(response, dict):
        return []

    normalized = dict(response)
    role = normalized.get("role")
    if not isinstance(role, str) or not role:
        normalized["role"] = "assistant"
    return _build_messages_view({"messages": [normalized]})


def build_render_view(
    api_name: str,
    model: str,
    params: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造详情页使用的结构化渲染模型。"""
    effective_metadata = metadata or {}
    return {
        "overview": _build_overview(api_name, model, params, effective_metadata),
        "tools": _build_tools_view(params),
        "messages": _build_messages_view(params),
        "response_messages": _build_response_view(response),
    }


@dataclass
class CapturedRequest:
    """单条捕获记录。"""

    id: int
    ts: float
    api_name: str
    model: str
    params: dict[str, Any]
    metadata: dict[str, Any]
    response: dict[str, Any] | None = None

    def to_summary(self) -> dict[str, Any]:
        """返回列表展示用的摘要。"""
        msg_count = len(self.params.get("messages", []))
        tool_count = len(self.params.get("tools", []))
        request_name = str(self.metadata.get("request_name") or "")
        import_source = str(self.metadata.get("import_source") or "")
        response = self.response if isinstance(self.response, dict) else {}
        reasoning_content = response.get("reasoning_content")
        has_reasoning = isinstance(reasoning_content, str) and bool(reasoning_content.strip())
        return {
            "id": self.id,
            "ts": self.ts,
            "ts_str": time.strftime("%H:%M:%S", time.localtime(self.ts)),
            "api_name": self.api_name,
            "model": self.model,
            "api_provider": str(self.metadata.get("api_provider", "-")),
            "request_name": request_name,
            "import_source": import_source,
            "estimated_input_tokens": self.metadata.get("estimated_input_tokens"),
            "msg_count": msg_count,
            "tool_count": tool_count,
            "has_response": bool(response),
            "has_reasoning": has_reasoning,
        }

    def to_full(self) -> dict[str, Any]:
        """返回完整记录与结构化渲染模型。"""
        summary = self.to_summary()
        summary["params"] = self.params
        summary["metadata"] = self.metadata
        summary["response"] = self.response
        summary["rendered"] = build_render_view(
            self.api_name,
            self.model,
            self.params,
            self.metadata,
            self.response,
        )
        return summary


class RequestInspector:
    """LLM 请求体检视器，保存最近 N 条请求并通过 Web 界面展示。"""

    def __init__(self, max_records: int = 200) -> None:
        self._max_records = max_records
        self._records: deque[CapturedRequest] = deque(maxlen=max_records)
        self._counter: int = 0
        self._subscribers: list[asyncio.Queue[tuple[str, CapturedRequest] | None]] = []
        self._mounted: bool = False

    def capture(
        self,
        api_name: str,
        params: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """捕获一条请求体，存入内存并推送给所有 SSE 订阅者。"""
        self._counter += 1
        try:
            copied = json.loads(json.dumps(params, default=str))
            copied = redact_media_sources(copied)
        except Exception:
            copied = {"_raw": "[unserializable request omitted]"}
        effective_metadata = metadata.copy() if isinstance(metadata, dict) else {}

        model = str(copied.get("model", "—"))
        record = CapturedRequest(
            id=self._counter,
            ts=time.time(),
            api_name=api_name,
            model=model,
            params=copied,
            metadata=effective_metadata,
        )
        self._records.append(record)

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(("new", record))
            except asyncio.QueueFull:
                pass
        return record.id

    def attach_response(self, req_id: int, response: dict[str, Any]) -> bool:
        """为已捕获的请求追加当前轮响应，供前端展示 thinking / tool calls。"""
        if not isinstance(req_id, int) or req_id <= 0:
            return False
        if not isinstance(response, dict):
            return False
        try:
            copied = json.loads(json.dumps(response, default=str))
            copied = redact_media_sources(copied)
        except Exception:
            copied = {"_raw": "[unserializable response omitted]"}

        target: CapturedRequest | None = None
        for record in reversed(self._records):
            if record.id == req_id:
                target = record
                break
        if target is None:
            return False

        target.response = copied
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(("update", target))
            except asyncio.QueueFull:
                pass
        return True

    def import_json(self, payload: Any) -> list[CapturedRequest]:
        """导入外部 JSON，并转换为可渲染的捕获记录。"""
        imported: list[CapturedRequest] = []
        for api_name, params, metadata in _normalize_import_payload(payload):
            next_id = self._counter + 1
            self.capture(api_name, params, metadata)
            record = self._records[-1]
            if record.id != next_id:
                raise RuntimeError("导入记录 ID 不连续")
            imported.append(record)
        return imported

    def mount(self, app: Any, prefix: str = "/_inspector") -> None:
        """将 WebUI 路由挂载到 FastAPI 应用。"""
        if self._mounted:
            return
        self._mounted = True
        router = self._build_router()
        app.include_router(router, prefix=prefix)

    def _build_router(self) -> APIRouter:
        """构建 FastAPI router，包含 WebUI、REST 与 SSE 端点。"""
        router = APIRouter()

        @router.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def webui() -> HTMLResponse:  # type: ignore[return-value]
            return HTMLResponse(_WEBUI_HTML)

        @router.get("/api/requests")
        async def list_requests() -> JSONResponse:
            return JSONResponse([record.to_summary() for record in self._records])

        @router.get("/api/requests/{req_id}")
        async def get_request(req_id: int) -> JSONResponse:
            for record in reversed(self._records):
                if record.id == req_id:
                    return JSONResponse(record.to_full())
            return JSONResponse({"error": "not found"}, status_code=404)

        @router.delete("/api/requests")
        async def clear_requests() -> JSONResponse:
            self._records.clear()
            return JSONResponse({"ok": True})

        @router.post("/api/import")
        async def import_requests(payload: Any = Body(...)) -> JSONResponse:
            try:
                imported = self.import_json(payload)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse(
                {
                    "ok": True,
                    "count": len(imported),
                    "items": [record.to_summary() for record in imported],
                }
            )

        @router.get("/api/stream", include_in_schema=False)
        async def sse_stream() -> StreamingResponse:
            queue: asyncio.Queue[tuple[str, CapturedRequest] | None] = asyncio.Queue(maxsize=50)
            self._subscribers.append(queue)

            async def generate() -> AsyncIterator[str]:
                try:
                    snapshot = [record.to_summary() for record in self._records]
                    yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
                    while True:
                        try:
                            record = await asyncio.wait_for(queue.get(), timeout=25)
                        except asyncio.TimeoutError:
                            yield ": heartbeat\n\n"
                            continue
                        if record is None:
                            break
                        kind, payload = record
                        yield f"event: {kind}\ndata: {json.dumps(payload.to_summary())}\n\n"
                finally:
                    try:
                        self._subscribers.remove(queue)
                    except ValueError:
                        pass

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        return router


_inspector: RequestInspector | None = None


def get_inspector() -> RequestInspector:
    """获取全局 RequestInspector 单例。"""
    global _inspector
    if _inspector is None:
        _inspector = RequestInspector()
    return _inspector


def capture(
    api_name: str,
    params: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> int:
    """捕获一条 OpenAI 请求体。"""
    return get_inspector().capture(api_name, params, metadata)


def attach_response(req_id: int, response: dict[str, Any]) -> bool:
    """为指定记录追加响应。"""
    return get_inspector().attach_response(req_id, response)


def _normalize_import_payload(
    payload: Any,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """将导入 JSON 归一化为 capture 所需的记录列表。"""
    entries = _normalize_import_entries(payload)
    if not entries:
        raise ValueError("导入 JSON 为空，无法渲染")
    return entries


def _normalize_import_entries(
    payload: Any,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """递归解析导入 JSON，支持单条、列表和包装对象。"""
    if isinstance(payload, list):
        entries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for item in payload:
            entries.extend(_normalize_import_entries(item))
        return entries

    if not isinstance(payload, dict):
        raise ValueError(
            "导入 JSON 必须是对象、对象数组，或包含 requests/params 的对象"
        )

    requests_payload = payload.get("requests")
    if requests_payload is not None:
        if not isinstance(requests_payload, list):
            raise ValueError("requests 字段必须是数组")
        entries: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        for item in requests_payload:
            entries.extend(_normalize_import_entries(item))
        return entries

    metadata_raw = payload.get("metadata")
    metadata = metadata_raw.copy() if isinstance(metadata_raw, dict) else {}
    metadata.setdefault("imported", True)

    api_name = str(payload.get("api_name") or payload.get("api") or "imported.request")

    if "params" in payload:
        params = payload.get("params")
        if not isinstance(params, dict):
            raise ValueError("params 字段必须是对象")
        if "source" in payload and "import_source" not in metadata:
            metadata["import_source"] = str(payload.get("source"))
        return [(api_name, params, metadata)]

    if any(key in payload for key in ("messages", "tools", "model", "input")):
        params = payload.copy()
        params.pop("metadata", None)
        return [(api_name, params, metadata)]

    raise ValueError(
        "无法识别导入 JSON 结构；请提供原始请求体、包含 params 的对象，或 requests 数组"
    )


_WEBUI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM 请求检视器</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #f4efe7;
    --bg-strong: #ebe2d5;
    --panel: rgba(255, 252, 246, 0.9);
    --panel-strong: #fffaf3;
    --border: rgba(70, 53, 32, 0.12);
    --shadow: 0 18px 48px rgba(78, 60, 37, 0.08);
    --text: #2f261d;
    --muted: #7a6856;
    --accent: #0e7490;
    --accent-soft: rgba(14, 116, 144, 0.12);
    --success: #1f7a4d;
    --danger: #aa3a2a;
    --system: #5e3ebc;
    --user: #0f766e;
    --assistant: #b45309;
    --tool: #2563eb;
    --code-bg: #221c16;
    --code-text: #f8efe2;
  }
  html, body { height: 100%; }
  body {
    height: 100vh;
    min-height: 100vh;
    background:
      radial-gradient(circle at top left, rgba(14, 116, 144, 0.16), transparent 28%),
      radial-gradient(circle at top right, rgba(180, 83, 9, 0.14), transparent 32%),
      linear-gradient(180deg, var(--bg) 0%, #f8f3eb 100%);
    color: var(--text);
    font-family: 'Segoe UI', 'PingFang SC', 'Noto Sans SC', sans-serif;
    font-size: 14px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  header {
    padding: 18px 22px;
    display: flex;
    gap: 16px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    background: rgba(255, 250, 243, 0.78);
    backdrop-filter: blur(18px);
  }
  .brand { display: flex; align-items: center; gap: 14px; min-width: 0; flex: 1; }
  .brand-mark {
    width: 44px;
    height: 44px;
    border-radius: 14px;
    background: linear-gradient(135deg, rgba(14,116,144,0.95), rgba(180,83,9,0.88));
    color: #fff;
    display: grid;
    place-items: center;
    font-weight: 700;
    letter-spacing: 0.04em;
    box-shadow: 0 10px 24px rgba(14, 116, 144, 0.18);
  }
  .brand-copy h1 { font-size: 19px; font-weight: 700; }
  .brand-copy p { color: var(--muted); margin-top: 4px; font-size: 12px; }
  .status-group { display: flex; align-items: center; gap: 10px; }
  #status-dot {
    width: 10px; height: 10px; border-radius: 50%; background: #baa995;
    box-shadow: 0 0 0 6px rgba(122, 104, 86, 0.08);
  }
  #status-dot.live { background: var(--success); box-shadow: 0 0 0 6px rgba(31, 122, 77, 0.14); }
  .badge {
    min-width: 38px;
    text-align: center;
    padding: 6px 10px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    font-weight: 700;
  }
  .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
  .toolbar-group { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  button {
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.72);
    color: var(--text);
    padding: 9px 14px;
    border-radius: 12px;
    cursor: pointer;
    font-size: 13px;
    transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
  }
  button:hover {
    transform: translateY(-1px);
    border-color: rgba(14, 116, 144, 0.28);
    box-shadow: 0 10px 20px rgba(78, 60, 37, 0.08);
  }
  button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  button.danger { color: var(--danger); }
  input[type=text] {
    width: 220px;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.72);
    color: var(--text);
    border-radius: 12px;
    padding: 10px 14px;
    outline: none;
  }
  input[type=text]:focus { border-color: rgba(14, 116, 144, 0.45); }
  textarea {
    width: 100%;
    min-height: 220px;
    resize: vertical;
    border: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.8);
    color: var(--text);
    border-radius: 16px;
    padding: 14px;
    outline: none;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.6;
  }
  textarea:focus { border-color: rgba(14, 116, 144, 0.45); }
  .main { display: flex; flex: 1; min-height: 0; overflow: hidden; }
  .import-panel {
    display: none;
    padding: 16px 18px;
    border-bottom: 1px solid var(--border);
    background: rgba(255, 250, 243, 0.82);
    backdrop-filter: blur(12px);
    flex-direction: column;
    gap: 12px;
  }
  .import-panel.open { display: flex; }
  .import-row {
    display: flex;
    gap: 10px;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
  }
  .import-hint { color: var(--muted); font-size: 12px; }
  .import-actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .status-text { color: var(--muted); font-size: 12px; min-height: 18px; }
  .status-text.error { color: var(--danger); }
  .status-text.success { color: var(--success); }
  .list-panel {
    width: 360px;
    flex-shrink: 0;
    border-right: 1px solid var(--border);
    background: rgba(248, 242, 233, 0.68);
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .list-scroll { padding: 14px; overflow-y: auto; min-height: 0; }
  .req-item {
    padding: 14px;
    border: 1px solid transparent;
    border-radius: 18px;
    background: rgba(255, 255, 255, 0.72);
    box-shadow: 0 10px 28px rgba(78, 60, 37, 0.04);
    cursor: pointer;
    margin-bottom: 12px;
    transition: transform .15s ease, border-color .15s ease, background .15s ease;
  }
  .req-item:hover { transform: translateY(-1px); border-color: rgba(14, 116, 144, 0.18); }
  .req-item.active {
    border-color: rgba(14, 116, 144, 0.35);
    background: linear-gradient(180deg, rgba(14, 116, 144, 0.08), rgba(255, 255, 255, 0.8));
  }
  .row1 { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
  .api-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    border-radius: 999px;
    background: rgba(14, 116, 144, 0.1);
    color: var(--accent);
    font-size: 11px;
    font-weight: 700;
  }
  .ts { color: var(--muted); font-size: 12px; }
  .model { margin-top: 9px; font-weight: 700; word-break: break-word; }
  .meta { margin-top: 6px; color: var(--muted); font-size: 12px; }
  .meta-row { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
  .mini-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(94, 62, 188, 0.08);
    color: var(--muted);
    font-size: 11px;
    font-weight: 600;
  }
  .detail-panel { flex: 1; min-width: 0; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
  .detail-toolbar {
    padding: 14px 18px;
    display: flex;
    gap: 10px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    background: rgba(255, 250, 243, 0.72);
    backdrop-filter: blur(12px);
    flex-wrap: wrap;
  }
  .detail-title { flex: 1; min-width: 240px; color: var(--muted); }
  .detail-body { flex: 1; min-height: 0; overflow: auto; padding: 22px; }
  .empty-tip {
    padding: 36px;
    border: 1px dashed rgba(122, 104, 86, 0.24);
    border-radius: 22px;
    text-align: center;
    color: var(--muted);
    background: rgba(255, 255, 255, 0.5);
  }
  .section { margin-bottom: 18px; }
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }
  .section-head h2 { font-size: 15px; }
  .section-hint { color: var(--muted); font-size: 12px; }
  .overview-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px;
  }
  .overview-item {
    border: 1px solid var(--border);
    border-radius: 16px;
    background: var(--panel);
    padding: 14px;
    box-shadow: var(--shadow);
  }
  .overview-item .label { color: var(--muted); font-size: 12px; }
  .overview-item .value {
    margin-top: 8px;
    font-weight: 700;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'Segoe UI', 'PingFang SC', sans-serif;
  }
  details.tool-card {
    border: 1px solid var(--border);
    border-radius: 18px;
    background: var(--panel);
    margin-bottom: 12px;
    box-shadow: var(--shadow);
    overflow: hidden;
  }
  details.tool-card > summary {
    list-style: none;
    cursor: pointer;
    padding: 14px 16px;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
  }
  details.tool-card > summary::-webkit-details-marker { display: none; }
  .tool-name { font-weight: 700; }
  .tool-kind { color: var(--muted); font-size: 12px; }
  .tool-body { padding: 0 16px 16px; }
  .tool-desc { color: var(--muted); margin-bottom: 12px; }
  .tool-props {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 10px;
    margin-bottom: 12px;
  }
  .tool-prop {
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 12px;
    background: rgba(255, 255, 255, 0.62);
  }
  .tool-prop .name { font-weight: 700; }
  .tool-prop .meta { margin-top: 6px; }
  .message-stack { display: flex; flex-direction: column; gap: 14px; }
  .message-card {
    border-radius: 22px;
    border: 1px solid var(--border);
    background: var(--panel);
    box-shadow: var(--shadow);
    overflow: hidden;
  }
  .message-head {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    background: rgba(255, 255, 255, 0.5);
    flex-wrap: wrap;
  }
  .message-role {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
  }
  .message-role .dot { width: 10px; height: 10px; border-radius: 50%; }
  .role-system .dot { background: var(--system); }
  .role-user .dot { background: var(--user); }
  .role-assistant .dot { background: var(--assistant); }
  .role-tool .dot { background: var(--tool); }
  .role-unknown .dot { background: var(--muted); }
  .message-meta { color: var(--muted); font-size: 12px; }
  .message-body { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  .block {
    border: 1px solid rgba(70, 53, 32, 0.08);
    border-radius: 16px;
    background: rgba(255, 255, 255, 0.62);
    padding: 14px;
  }
  .block-label { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
  .markdown-body { color: var(--text); line-height: 1.7; word-break: break-word; }
  .markdown-body h1, .markdown-body h2, .markdown-body h3, .markdown-body h4, .markdown-body h5, .markdown-body h6 {
    margin: 14px 0 8px;
    line-height: 1.3;
  }
  .markdown-body p + p { margin-top: 10px; }
  .markdown-body ul, .markdown-body ol { padding-left: 20px; margin: 8px 0; }
  .markdown-body code {
    background: rgba(14, 116, 144, 0.12);
    padding: 2px 6px;
    border-radius: 6px;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12px;
  }
  .markdown-body pre {
    background: var(--code-bg);
    color: var(--code-text);
    padding: 14px;
    border-radius: 14px;
    overflow: auto;
    margin: 10px 0;
  }
  .markdown-body pre code { background: transparent; padding: 0; color: inherit; }
  .tool-call { border-left: 4px solid rgba(14, 116, 144, 0.5); }
  .tool-result { border-left: 4px solid rgba(37, 99, 235, 0.45); }
  .media-block { border-left: 4px solid rgba(180, 83, 9, 0.45); }
  .unknown-block { border-left: 4px solid rgba(170, 58, 42, 0.42); }
  .code-panel {
    background: var(--code-bg);
    color: var(--code-text);
    border-radius: 14px;
    padding: 14px;
    overflow: auto;
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .json-key { color: #8ed7ff; }
  .json-str { color: #9fe3a7; }
  .json-num { color: #f7b56d; }
  .json-bool { color: #f3d070; }
  .json-null { color: #d0bfa8; }
  .new-flash { animation: flash .6s ease; }
  @keyframes flash {
    0%,100% { box-shadow: 0 10px 28px rgba(78, 60, 37, 0.04); }
    50% { box-shadow: 0 18px 32px rgba(14, 116, 144, 0.18); }
  }
  @media (max-width: 980px) {
    header { flex-direction: column; align-items: stretch; }
    .toolbar { justify-content: stretch; }
    .main { flex-direction: column; }
    .list-panel { width: 100%; border-right: 0; border-bottom: 1px solid var(--border); max-height: 42vh; }
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-mark">LLM</div>
    <div class="brand-copy">
      <h1>LLM 请求检视器</h1>
      <p>默认展示结构化对话视图，支持按 request_name 过滤和 Prompt 文本核查。</p>
    </div>
  </div>
  <div class="status-group">
    <span id="status-dot"></span>
    <span class="badge" id="total-badge">0</span>
  </div>
  <div class="toolbar">
    <div class="toolbar-group">
      <input type="text" id="filter-input" placeholder="过滤 request / model / api…">
      <button id="import-toggle-btn">导入 JSON</button>
      <button id="auto-scroll-btn" class="active" title="自动滚动到最新">跟随最新</button>
    </div>
    <div class="toolbar-group">
      <button id="pretty-btn" class="active">渲染视图</button>
      <button id="text-btn">Prompt 文本</button>
      <button id="json-btn">JSON 视图</button>
      <button id="copy-btn">复制 JSON</button>
      <button id="clear-btn" class="danger">清空</button>
    </div>
  </div>
</header>
<div class="main">
  <div class="list-panel">
    <div class="import-panel" id="import-panel">
      <div class="import-row">
        <div>
          <strong>导入外部请求 JSON</strong>
          <div class="import-hint">支持原始请求体、包含 params 的对象、对象数组，或带 requests 数组的导出文件。</div>
        </div>
        <div class="import-actions">
          <input type="file" id="import-file-input" accept=".json,application/json" multiple hidden>
          <button id="import-file-btn">选择文件</button>
          <button id="import-submit-btn">导入文本</button>
        </div>
      </div>
      <textarea id="import-textarea" placeholder='可直接粘贴 JSON，例如：
{
  "api_name": "chat.completions.create",
  "params": {
    "model": "gpt-4.1",
    "messages": [{"role": "user", "content": "hello"}]
  },
  "metadata": {"api_provider": "OpenAI"}
}'></textarea>
      <div class="status-text" id="import-status"></div>
    </div>
    <div class="list-scroll" id="list-scroll">
      <div class="empty-tip" id="empty-tip">等待请求捕获…</div>
    </div>
  </div>
  <div class="detail-panel">
    <div class="detail-toolbar">
      <span class="detail-title" id="detail-title">选择左侧记录查看结构化对话视图</span>
    </div>
    <div class="detail-body" id="detail-body">
      <div class="empty-tip">左侧选择一条请求后，这里会显示摘要、工具定义和消息渲染。</div>
    </div>
  </div>
</div>
<script>
const listScroll = document.getElementById('list-scroll');
const detailBody = document.getElementById('detail-body');
const detailTitle = document.getElementById('detail-title');
const totalBadge = document.getElementById('total-badge');
const filterInput = document.getElementById('filter-input');
const statusDot = document.getElementById('status-dot');
const autoScrollBtn = document.getElementById('auto-scroll-btn');
const prettyBtn = document.getElementById('pretty-btn');
const textBtn = document.getElementById('text-btn');
const jsonBtn = document.getElementById('json-btn');
const importToggleBtn = document.getElementById('import-toggle-btn');
const importPanel = document.getElementById('import-panel');
const importTextarea = document.getElementById('import-textarea');
const importStatus = document.getElementById('import-status');
const importFileInput = document.getElementById('import-file-input');
const importFileBtn = document.getElementById('import-file-btn');
const importSubmitBtn = document.getElementById('import-submit-btn');

let requests = [];
let activeId = null;
let autoScroll = true;
let viewMode = 'pretty';
let fullCache = {};

function connectSSE() {
  const es = new EventSource('/_inspector/api/stream');
  statusDot.classList.remove('live');
  es.addEventListener('snapshot', event => {
    statusDot.classList.add('live');
    requests = JSON.parse(event.data);
    renderList();
  });
  es.addEventListener('new', event => {
    const record = JSON.parse(event.data);
    requests.push(record);
    appendItem(record, true);
    totalBadge.textContent = requests.length;
    if (autoScroll) listScroll.scrollTop = listScroll.scrollHeight;
  });
  es.addEventListener('update', event => {
    const record = JSON.parse(event.data);
    const idx = requests.findIndex(item => item.id === record.id);
    if (idx >= 0) {
      requests[idx] = record;
    } else {
      requests.push(record);
    }
    delete fullCache[record.id];
    renderList();
    if (activeId === record.id) {
      selectItem(record.id);
    }
  });
  es.onerror = () => {
    statusDot.classList.remove('live');
    es.close();
    setTimeout(connectSSE, 3000);
  };
}
connectSSE();

async function refreshRequests() {
  const response = await fetch('/_inspector/api/requests');
  requests = await response.json();
  renderList();
}

function filterText() {
  return filterInput.value.trim().toLowerCase();
}

function matchFilter(record) {
  const f = filterText();
  if (!f) return true;
  return String(record.model || '').toLowerCase().includes(f)
    || String(record.api_name || '').toLowerCase().includes(f)
    || String(record.request_name || '').toLowerCase().includes(f)
    || String(record.import_source || '').toLowerCase().includes(f);
}

function renderList() {
  listScroll.innerHTML = '';
  const filtered = requests.filter(matchFilter);
  if (filtered.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-tip';
    empty.textContent = requests.length ? '无匹配记录' : '等待请求捕获…';
    listScroll.appendChild(empty);
    totalBadge.textContent = requests.length;
    return;
  }
  filtered.forEach(record => appendItem(record, false));
  totalBadge.textContent = requests.length;
  if (autoScroll) listScroll.scrollTop = listScroll.scrollHeight;
}

function appendItem(record, flash) {
  if (!matchFilter(record)) return;
  const item = document.createElement('div');
  item.className = 'req-item' + (record.id === activeId ? ' active' : '') + (flash ? ' new-flash' : '');
  item.dataset.id = record.id;
  const provider = record.api_provider ? escHtml(record.api_provider) : '-';
  const tokens = record.estimated_input_tokens == null ? '-' : escHtml(record.estimated_input_tokens);
  const requestName = record.request_name ? escHtml(record.request_name) : '-';
  const responseChip = record.has_response ? '<span class="mini-chip">Response · captured</span>' : '<span class="mini-chip">Response · pending</span>';
  const reasoningChip = record.has_reasoning ? '<span class="mini-chip">Thinking · yes</span>' : '';
  item.innerHTML = `<div class="row1"><span class="api-chip">${escHtml(record.api_name)}</span><span class="ts">${escHtml(record.ts_str)}</span></div>
    <div class="model">${escHtml(record.model)}</div>
    <div class="meta">${record.msg_count} 条消息 · ${record.tool_count} 个工具</div>
    <div class="meta-row"><span class="mini-chip">Request · ${requestName}</span><span class="mini-chip">Provider · ${provider}</span><span class="mini-chip">Tokens · ${tokens}</span>${responseChip}${reasoningChip}</div>`;
  item.addEventListener('click', () => selectItem(record.id));
  listScroll.appendChild(item);
}

filterInput.addEventListener('input', renderList);

async function selectItem(id) {
  activeId = id;
  document.querySelectorAll('.req-item').forEach(el => el.classList.toggle('active', +el.dataset.id === id));
  if (!fullCache[id]) {
    detailTitle.textContent = '加载中…';
    const res = await fetch(`/_inspector/api/requests/${id}`);
    fullCache[id] = await res.json();
  }
  renderActiveDetail();
}

function renderActiveDetail() {
  if (activeId == null || !fullCache[activeId]) {
    detailBody.innerHTML = '<div class="empty-tip">左侧选择一条请求后，这里会显示详情。</div>';
    detailTitle.textContent = '选择左侧记录查看结构化对话视图';
    return;
  }
  const data = fullCache[activeId];
  const record = requests.find(item => item.id === activeId);
  const requestName = record && record.request_name ? ` · ${record.request_name}` : '';
  detailTitle.textContent = record ? `#${activeId} · ${record.api_name}${requestName} · ${record.ts_str}` : `#${activeId}`;
  if (viewMode === 'text') {
    renderPromptTextDetail(data.params, data.response);
    return;
  }
  if (viewMode === 'json') {
    renderJsonDetail(data);
    return;
  }
  renderPrettyDetail(data.rendered || {});
}

function renderPrettyDetail(rendered) {
  detailBody.innerHTML = '';
  const fragment = document.createDocumentFragment();
  fragment.appendChild(renderOverviewSection(rendered.overview || []));
  fragment.appendChild(renderToolsSection(rendered.tools || []));
  fragment.appendChild(renderMessagesSection(
    rendered.messages || [],
    'Messages 对话流',
    '按 role 拆分板块，渲染 Markdown、工具调用与结果',
    '本次请求没有 messages，或并非 chat 类型请求。',
  ));
  fragment.appendChild(renderMessagesSection(
    rendered.response_messages || [],
    '当前响应',
    '展示本轮模型真实返回的文本、thinking 与工具调用',
    '当前还没有捕获到响应内容。',
  ));
  detailBody.appendChild(fragment);
}

function renderOverviewSection(items) {
  const section = document.createElement('section');
  section.className = 'section';
  section.innerHTML = '<div class="section-head"><h2>请求摘要</h2><span class="section-hint">顶层参数与统计</span></div>';
  const grid = document.createElement('div');
  grid.className = 'overview-grid';
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-tip';
    empty.textContent = '没有可展示的顶层参数。';
    section.appendChild(empty);
    return section;
  }
  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'overview-item';
    card.innerHTML = `<div class="label">${escHtml(item.label || '')}</div><div class="value">${escHtml(item.value || '')}</div>`;
    grid.appendChild(card);
  });
  section.appendChild(grid);
  return section;
}

function renderToolsSection(tools) {
  const section = document.createElement('section');
  section.className = 'section';
  section.innerHTML = '<div class="section-head"><h2>Tools 定义</h2><span class="section-hint">函数签名、参数要求与原始 schema</span></div>';
  if (!tools.length) {
    section.innerHTML += '<div class="empty-tip">本次请求没有携带 tools 定义。</div>';
    return section;
  }
  tools.forEach(tool => {
    const details = document.createElement('details');
    details.className = 'tool-card';
    details.open = tool.index === 0;
    const propertyHtml = (tool.properties || []).map(prop => {
      const requiredLabel = prop.required ? '必填' : '可选';
      return `<div class="tool-prop"><div class="name">${escHtml(prop.name || '')}</div><div class="meta">${escHtml(prop.type || '')} · ${requiredLabel}</div><div class="meta">${escHtml(prop.description || '无描述')}</div></div>`;
    }).join('');
    details.innerHTML = `<summary><div><div class="tool-name">${escHtml(tool.name || '')}</div><div class="tool-kind">${escHtml(tool.kind || '')}</div></div><div class="section-hint">${(tool.required || []).length} 个必填参数</div></summary>
      <div class="tool-body">
        <div class="tool-desc">${escHtml(tool.description || '无描述')}</div>
        ${(tool.properties || []).length ? `<div class="tool-props">${propertyHtml}</div>` : '<div class="empty-tip">没有显式参数定义。</div>'}
        <div class="code-panel">${syntaxHighlight(tool.raw_json || '{}')}</div>
      </div>`;
    section.appendChild(details);
  });
  return section;
}

function renderMessagesSection(messages, title, hint, emptyText) {
  const section = document.createElement('section');
  section.className = 'section';
  section.innerHTML = `<div class="section-head"><h2>${escHtml(title || 'Messages')}</h2><span class="section-hint">${escHtml(hint || '')}</span></div>`;
  if (!messages.length) {
    section.innerHTML += `<div class="empty-tip">${escHtml(emptyText || '暂无内容。')}</div>`;
    return section;
  }
  const stack = document.createElement('div');
  stack.className = 'message-stack';
  messages.forEach(message => stack.appendChild(renderMessageCard(message)));
  section.appendChild(stack);
  return section;
}

function renderMessageCard(message) {
  const card = document.createElement('article');
  const role = normalizeRoleClass(message.role || 'unknown');
  card.className = `message-card role-${role}`;
  const meta = message.meta ? `<span class="message-meta">${escHtml(message.meta)}</span>` : '<span class="message-meta">&nbsp;</span>';
  card.innerHTML = `<div class="message-head"><div class="message-role"><span class="dot"></span><span>${escHtml(message.label || role)}</span></div>${meta}</div>`;
  const body = document.createElement('div');
  body.className = 'message-body';
  (message.blocks || []).forEach(block => body.appendChild(renderBlock(block)));
  card.appendChild(body);
  return card;
}

function renderBlock(block) {
  const container = document.createElement('div');
  const type = block.type || 'unknown';
  container.className = 'block';
  if (block.label) {
    const label = document.createElement('div');
    label.className = 'block-label';
    label.textContent = block.label;
    container.appendChild(label);
  }
  if (type === 'markdown') {
    const body = document.createElement('div');
    body.className = 'markdown-body';
    body.innerHTML = renderMarkdown(block.text || '');
    container.appendChild(body);
    return container;
  }
  if (type === 'tool_call') {
    container.classList.add('tool-call');
    container.innerHTML += `<div class="block-label">工具调用 ${block.call_id ? '· ' + escHtml(block.call_id) : ''}</div><div><strong>${escHtml(block.name || '')}</strong></div><div class="code-panel">${syntaxHighlight(block.arguments_text || '{}')}</div>`;
    return container;
  }
  if (type === 'tool_result') {
    container.classList.add('tool-result');
    container.innerHTML += `<div class="block-label">工具结果${block.call_id ? ' · ' + escHtml(block.call_id) : ''}${block.name ? ' · ' + escHtml(block.name) : ''}</div><div class="markdown-body">${renderMarkdown(block.text || '')}</div>`;
    return container;
  }
  if (type === 'media') {
    container.classList.add('media-block');
    container.innerHTML += `<div><strong>${escHtml(block.title || '媒体内容')}</strong></div><div class="message-meta">${escHtml(block.meta || '')}</div><div class="markdown-body">${renderMarkdown(block.text || '')}</div>`;
    return container;
  }
  if (type === 'empty') {
    container.innerHTML += `<div class="message-meta">${escHtml(block.text || '')}</div>`;
    return container;
  }
  container.classList.add('unknown-block');
  container.innerHTML += `<div class="block-label">${escHtml(block.label || '未知内容')}</div><div class="code-panel">${syntaxHighlight(block.text || '{}')}</div>`;
  return container;
}

function renderJsonDetail(data) {
  detailBody.innerHTML = '';
  const section = document.createElement('section');
  section.className = 'section';
  section.innerHTML = '<div class="section-head"><h2>原始 JSON</h2><span class="section-hint">保留完整调试视图</span></div>';
  const code = document.createElement('div');
  code.className = 'code-panel';
  code.innerHTML = syntaxHighlight(JSON.stringify(data, null, 2));
  section.appendChild(code);
  detailBody.appendChild(section);
}

function renderPromptTextDetail(params, response) {
  detailBody.innerHTML = '';
  const section = document.createElement('section');
  section.className = 'section';
  section.innerHTML = '<div class="section-head"><h2>Prompt / Response 文本</h2><span class="section-hint">按 messages 顺序展开，并附上当前响应</span></div>';
  const code = document.createElement('div');
  code.className = 'code-panel';
  code.textContent = buildPromptText(params, response);
  section.appendChild(code);
  detailBody.appendChild(section);
}

function buildPromptText(params, response) {
  const messages = Array.isArray(params && params.messages) ? params.messages : [];
  const sections = [];
  if (messages.length) {
    sections.push(messages.map((message, index) => {
      if (!message || typeof message !== 'object') {
        return `#${index + 1} [unknown]\n${JSON.stringify(message, null, 2)}`;
      }
      const role = message.role || 'unknown';
      const meta = [];
      if (message.name) meta.push(`name=${message.name}`);
      if (message.tool_call_id) meta.push(`tool_call_id=${message.tool_call_id}`);
      if (Array.isArray(message.tool_calls) && message.tool_calls.length) meta.push(`tool_calls=${message.tool_calls.length}`);
      const header = `#${index + 1} [${role}]${meta.length ? ' ' + meta.join(' ') : ''}`;
      const content = promptContentToText(message.content);
      const toolCalls = Array.isArray(message.tool_calls) && message.tool_calls.length
        ? '\n\n<tool_calls>\n' + JSON.stringify(message.tool_calls, null, 2) + '\n</tool_calls>'
        : '';
      const reasoning = message.reasoning_content ? `\n\n<reasoning>\n${message.reasoning_content}\n</reasoning>` : '';
      return `${header}\n${content}${reasoning}${toolCalls}`;
    }).join('\n\n---\n\n'));
  } else {
    sections.push('本次请求没有 messages。');
  }
  if (response && typeof response === 'object') {
    const responseRole = response.role || 'assistant';
    const responseText = promptContentToText(response.content);
    const responseReasoning = response.reasoning_content ? `\n\n<reasoning>\n${response.reasoning_content}\n</reasoning>` : '';
    const responseToolCalls = Array.isArray(response.tool_calls) && response.tool_calls.length
      ? '\n\n<tool_calls>\n' + JSON.stringify(response.tool_calls, null, 2) + '\n</tool_calls>'
      : '';
    sections.push(`=== RESPONSE ===\n[${responseRole}]\n${responseText}${responseReasoning}${responseToolCalls}`);
  }
  return sections.join('\n\n====================\n\n');
}

function promptContentToText(content) {
  if (content == null) return '';
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(part => {
      if (typeof part === 'string') return part;
      if (!part || typeof part !== 'object') return JSON.stringify(part);
      const type = part.type || 'unknown';
      if (typeof part.text === 'string') return part.text;
      if (type === 'image_url' || type === 'input_image') return `[${type}: ${JSON.stringify(part.image_url || part.url || '')}]`;
      if (type === 'input_audio' || type === 'audio') return `[${type}]`;
      return `[${type}] ${JSON.stringify(part)}`;
    }).join('\n');
  }
  return JSON.stringify(content, null, 2);
}

function normalizeRoleClass(role) {
  return ['system', 'user', 'assistant', 'tool'].includes(role) ? role : 'unknown';
}

autoScrollBtn.addEventListener('click', () => {
  autoScroll = !autoScroll;
  autoScrollBtn.classList.toggle('active', autoScroll);
  autoScrollBtn.textContent = autoScroll ? '跟随最新' : '暂停跟随';
});

prettyBtn.addEventListener('click', () => {
  viewMode = 'pretty';
  prettyBtn.classList.add('active');
  textBtn.classList.remove('active');
  jsonBtn.classList.remove('active');
  renderActiveDetail();
});

textBtn.addEventListener('click', () => {
  viewMode = 'text';
  textBtn.classList.add('active');
  prettyBtn.classList.remove('active');
  jsonBtn.classList.remove('active');
  renderActiveDetail();
});

jsonBtn.addEventListener('click', () => {
  viewMode = 'json';
  jsonBtn.classList.add('active');
  prettyBtn.classList.remove('active');
  textBtn.classList.remove('active');
  renderActiveDetail();
});

document.getElementById('clear-btn').addEventListener('click', async () => {
  if (!confirm('确定清空所有记录？')) return;
  await fetch('/_inspector/api/requests', { method: 'DELETE' });
  requests = [];
  fullCache = {};
  activeId = null;
  renderList();
  renderActiveDetail();
  totalBadge.textContent = '0';
});

document.getElementById('copy-btn').addEventListener('click', async () => {
  if (activeId == null || !fullCache[activeId]) return;
  await navigator.clipboard.writeText(JSON.stringify(fullCache[activeId], null, 2));
});

function setImportStatus(message, kind = '') {
  importStatus.textContent = message;
  importStatus.className = 'status-text' + (kind ? ` ${kind}` : '');
}

async function importJsonPayload(payload) {
  setImportStatus('正在导入…');
  const response = await fetch('/_inspector/api/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) {
    throw new Error(result.error || '导入失败');
  }
  await refreshRequests();
  fullCache = {};
  const latest = (result.items || []).at(-1);
  if (latest) {
    await selectItem(latest.id);
  } else {
    renderActiveDetail();
  }
  const count = Number(result.count || 0);
  setImportStatus(`已导入 ${count} 条记录`, 'success');
}

importToggleBtn.addEventListener('click', () => {
  const isOpen = importPanel.classList.toggle('open');
  importToggleBtn.classList.toggle('active', isOpen);
  if (isOpen) {
    importTextarea.focus();
  }
});

importSubmitBtn.addEventListener('click', async () => {
  const text = importTextarea.value.trim();
  if (!text) {
    setImportStatus('请输入或粘贴 JSON 内容', 'error');
    return;
  }
  try {
    await importJsonPayload(JSON.parse(text));
  } catch (error) {
    setImportStatus(error.message || '导入失败', 'error');
  }
});

importFileBtn.addEventListener('click', () => importFileInput.click());

importFileInput.addEventListener('change', async event => {
  const files = Array.from(event.target.files || []);
  if (!files.length) return;
  try {
    const payloads = [];
    for (const file of files) {
      const text = await file.text();
      payloads.push(JSON.parse(text));
    }
    await importJsonPayload(payloads.length === 1 ? payloads[0] : payloads);
  } catch (error) {
    setImportStatus(error.message || '文件导入失败', 'error');
  } finally {
    importFileInput.value = '';
  }
});

function escHtml(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderInlineMarkdown(text) {
  let rendered = escHtml(text);
  rendered = rendered.replace(/`([^`]+)`/g, '<code>$1</code>');
  rendered = rendered.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  rendered = rendered.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return rendered;
}

function renderParagraph(lines) {
  if (!lines.length) return '';
  return `<p>${lines.map(renderInlineMarkdown).join('<br>')}</p>`;
}

function renderMarkdown(text) {
  const source = String(text || '');
  const codeBlocks = [];
  const placeholderPrefix = '__CODE_BLOCK__';
  const withoutCode = source.replace(/```(\w+)?\n([\s\S]*?)```/g, (_, language = '', code = '') => {
    const index = codeBlocks.length;
    codeBlocks.push(`<pre><code>${escHtml(code.replace(/\n$/, ''))}</code></pre>`);
    return `${placeholderPrefix}${index}__`;
  });
  const lines = withoutCode.split(/\r?\n/);
  const parts = [];
  let paragraph = [];
  let listType = null;
  let listItems = [];

  function flushParagraph() {
    if (paragraph.length) {
      parts.push(renderParagraph(paragraph));
      paragraph = [];
    }
  }

  function flushList() {
    if (listType && listItems.length) {
      parts.push(`<${listType}>${listItems.map(item => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</${listType}>`);
    }
    listType = null;
    listItems = [];
  }

  lines.forEach(line => {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      flushList();
      return;
    }

    const placeholderMatch = trimmed.match(new RegExp(`^${placeholderPrefix}(\\d+)__$`));
    if (placeholderMatch) {
      flushParagraph();
      flushList();
      parts.push(codeBlocks[Number(placeholderMatch[1])] || '');
      return;
    }

    const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = Math.min(heading[1].length, 6);
      parts.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      return;
    }

    const unordered = trimmed.match(/^[-*+]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      if (listType && listType !== 'ul') flushList();
      listType = 'ul';
      listItems.push(unordered[1]);
      return;
    }

    const ordered = trimmed.match(/^\d+\.\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      if (listType && listType !== 'ol') flushList();
      listType = 'ol';
      listItems.push(ordered[1]);
      return;
    }

    flushList();
    paragraph.push(trimmed);
  });

  flushParagraph();
  flushList();

  if (!parts.length) {
    return `<p>${renderInlineMarkdown(source).replace(/\n/g, '<br>')}</p>`;
  }
  return parts.join('');
}

function syntaxHighlight(json) {
  const normalized = typeof json === 'string' ? json : JSON.stringify(json, null, 2);
  return escHtml(normalized).replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, match => {
    let cls = 'json-num';
    if (/^"/.test(match)) cls = /:$/.test(match) ? 'json-key' : 'json-str';
    else if (/true|false/.test(match)) cls = 'json-bool';
    else if (/null/.test(match)) cls = 'json-null';
    return `<span class="${cls}">${match}</span>`;
  });
}
</script>
</body>
</html>"""
