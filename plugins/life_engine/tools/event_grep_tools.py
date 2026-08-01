"""life_engine event ledger grep tools."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..service import LifeEngineService
from ..service.event_builder import EventType

logger = log_api.get_logger("life_engine.event_grep")

_DEFAULT_LIMIT = 12
_MAX_LIMIT = 80
_TEXT_FIELDS = ("content", "sender", "source", "source_detail", "tool_name", "stream_id", "content_type")


def _event_type_value(event: Any) -> str:
    event_type = getattr(event, "event_type", "")
    value = getattr(event_type, "value", event_type)
    return str(value or "").strip().lower()


def _event_to_payload(event: Any) -> dict[str, Any]:
    return {
        "event_id": str(getattr(event, "event_id", "") or ""),
        "event_type": _event_type_value(event),
        "timestamp": str(getattr(event, "timestamp", "") or ""),
        "sequence": int(getattr(event, "sequence", 0) or 0),
        "source": str(getattr(event, "source", "") or ""),
        "source_detail": str(getattr(event, "source_detail", "") or ""),
        "content": str(getattr(event, "content", "") or ""),
        "content_type": str(getattr(event, "content_type", "") or ""),
        "sender": str(getattr(event, "sender", "") or ""),
        "chat_type": str(getattr(event, "chat_type", "") or ""),
        "stream_id": str(getattr(event, "stream_id", "") or ""),
        "heartbeat_index": getattr(event, "heartbeat_index", None),
        "tool_name": str(getattr(event, "tool_name", "") or ""),
        "tool_args": getattr(event, "tool_args", None) or {},
        "tool_success": getattr(event, "tool_success", None),
    }


def _haystack(payload: dict[str, Any], fields: list[str]) -> str:
    parts: list[str] = []
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            parts.append(str(value))
        else:
            parts.append(str(value))
    return "\n".join(parts)


def _compile_pattern(query: str, *, use_regex: bool, case_insensitive: bool) -> re.Pattern[str]:
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(query if use_regex else re.escape(query), flags)


def _normalize_event_types(event_types: list[str] | None) -> set[str]:
    values: set[str] = set()
    for item in event_types or []:
        text = str(item or "").strip().lower()
        if not text:
            continue
        try:
            text = EventType(text).value
        except ValueError:
            pass
        values.add(text)
    return values


def _is_life_internal_payload(payload: dict[str, Any]) -> bool:
    event_type = str(payload.get("event_type") or "").strip().lower()
    if event_type in {"heartbeat", "tool_call", "tool_result"}:
        return True
    source = str(payload.get("source") or "").strip().lower()
    content_type = str(payload.get("content_type") or "").strip().lower()
    stream_id = str(payload.get("stream_id") or "").strip()
    if source == "life_engine":
        return True
    return not stream_id and content_type in {"proactive_opportunity", "dfc_message", "direct_message", "inner_dialogue"}


async def grep_life_events(
    *,
    query: str,
    use_regex: bool = False,
    case_insensitive: bool = True,
    stream_ids: list[str] | None = None,
    event_types: list[str] | None = None,
    fields: list[str] | None = None,
    include_pending: bool = True,
    include_life_internal: bool = False,
    limit: int = _DEFAULT_LIMIT,
    context_before: int = 1,
    context_after: int = 1,
    order: Literal["asc", "desc"] = "desc",
) -> dict[str, Any]:
    """Search the in-memory life event ledger."""
    text = str(query or "").strip()
    if not text:
        raise ValueError("query 不能为空")

    service = LifeEngineService.get_instance()
    if service is None:
        raise RuntimeError("life_engine 服务不可用")

    field_names = [str(field or "").strip() for field in (fields or list(_TEXT_FIELDS))]
    field_names = [field for field in field_names if field]
    if not field_names:
        field_names = list(_TEXT_FIELDS)

    pattern = _compile_pattern(text, use_regex=use_regex, case_insensitive=case_insensitive)
    stream_filter = {str(sid or "").strip() for sid in (stream_ids or []) if str(sid or "").strip()}
    type_filter = _normalize_event_types(event_types)
    resolved_limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    before = max(0, min(int(context_before or 0), 8))
    after = max(0, min(int(context_after or 0), 8))

    async with service._get_lock():
        events = list(getattr(service, "_event_history", []))
        if include_pending:
            events.extend(list(getattr(service, "_pending_events", [])))

    events.sort(key=lambda event: int(getattr(event, "sequence", 0) or 0))
    payloads = [_event_to_payload(event) for event in events]
    scoped_payloads: list[dict[str, Any]] = []
    for payload in payloads:
        if (
            stream_filter
            and str(payload.get("stream_id") or "") not in stream_filter
            and not (include_life_internal and _is_life_internal_payload(payload))
        ):
            continue
        if type_filter and str(payload.get("event_type") or "") not in type_filter:
            continue
        scoped_payloads.append(payload)

    matches: list[dict[str, Any]] = []
    for index, payload in enumerate(scoped_payloads):
        if not pattern.search(_haystack(payload, field_names)):
            continue

        start = max(0, index - before)
        end = min(len(scoped_payloads), index + after + 1)
        matches.append(
            {
                "event": payload,
                "context_before": scoped_payloads[start:index],
                "context_after": scoped_payloads[index + 1:end],
            }
        )

    if order != "asc":
        matches.reverse()

    returned = matches[:resolved_limit]
    return {
        "action": "grep_life_events",
        "query": text,
        "use_regex": bool(use_regex),
        "case_insensitive": bool(case_insensitive),
        "scope": "filtered_streams" if stream_filter else "all_streams",
        "stream_ids": sorted(stream_filter),
        "event_types": sorted(type_filter),
        "fields": field_names,
        "include_pending": bool(include_pending),
        "include_life_internal": bool(include_life_internal),
        "order": order,
        "matches": returned,
        "stats": {
            "total_events": len(payloads),
            "scanned_events": len(scoped_payloads),
            "matched_events": len(matches),
            "returned_matches": len(returned),
            "truncated": len(matches) > len(returned),
        },
    }


class LifeEngineGrepEventsTool(BaseTool):
    """Search life_engine's full event ledger from the internal life mode."""

    tool_name: str = "nucleus_grep_events"
    tool_description: str = (
        "搜索你的完整事件流，包括外部消息、心跳、工具调用和工具结果。\n\n"
        "何时用：回忆“我之前看见/做过/想过什么”；查找某段对话或某次工具调用的上下文；"
        "追溯某件事的来龙去脉。\n"
        "何时不用：查找文件内容用 nucleus_grep_file；查询 TODO 用 nucleus_todo；"
        "这里是事件流检索，不是聊天数据库全文检索。\n"
        "支持正则、按 stream_id 或事件类型过滤，可携带前后相邻事件作为上下文。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        query: Annotated[str, "要搜索的关键词或正则表达式"],
        use_regex: Annotated[bool, "是否按正则表达式匹配 query"] = False,
        case_insensitive: Annotated[bool, "匹配时是否忽略大小写"] = True,
        stream_ids: Annotated[list[str] | None, "限定 stream_id；为空表示全局事件流"] = None,
        event_types: Annotated[list[str] | None, "限定事件类型：message/heartbeat/tool_call/tool_result"] = None,
        fields: Annotated[list[str] | None, "限定搜索字段；为空搜索常用文本字段"] = None,
        include_pending: Annotated[bool, "是否包含尚未进入历史的 pending 事件"] = True,
        include_life_internal: Annotated[bool, "是否在限定 stream 时仍包含 life 内部事件"] = False,
        limit: Annotated[int, "最大返回命中数"] = _DEFAULT_LIMIT,
        context_before: Annotated[int, "每条命中前带几条相邻事件"] = 1,
        context_after: Annotated[int, "每条命中后带几条相邻事件"] = 1,
        order: Annotated[Literal["asc", "desc"], "返回顺序：asc/desc"] = "desc",
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            return True, await grep_life_events(
                query=query,
                use_regex=use_regex,
                case_insensitive=case_insensitive,
                stream_ids=stream_ids,
                event_types=event_types,
                fields=fields,
                include_pending=include_pending,
                include_life_internal=include_life_internal,
                limit=limit,
                context_before=context_before,
                context_after=context_after,
                order=order,
            )
        except re.error as exc:
            return False, f"正则表达式错误: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"搜索 life 事件流失败: {exc}")
            return False, f"搜索事件流失败: {exc}"


EVENT_GREP_TOOLS = [
    LifeEngineGrepEventsTool,
]
