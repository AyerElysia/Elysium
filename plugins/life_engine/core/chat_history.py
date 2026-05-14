"""Chat history helpers for unified life_chatter context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.core.components.base.chatter import BaseChatter
from src.core.models.message import Message


@dataclass(slots=True)
class ChatHistoryEntry:
    message: Message
    stream_id: str
    stream_name: str
    platform: str
    chat_type: str
    order: int


def message_flag(message: Message, flag_name: str) -> bool:
    if bool(getattr(message, flag_name, False)):
        return True
    extra = getattr(message, "extra", None)
    if isinstance(extra, dict):
        return bool(extra.get(flag_name, False))
    return False


def is_visible_chat_history_message(message: Message) -> bool:
    return not (
        message_flag(message, "is_inner_monologue")
        or message_flag(message, "is_proactive_opportunity_trigger")
        or message_flag(message, "is_proactive_followup_trigger")
    )


def message_timestamp(message: Message) -> float:
    raw = getattr(message, "time", 0.0)
    if isinstance(raw, datetime):
        return raw.timestamp()
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def stream_label(stream: Any, stream_id: str) -> str:
    name = str(getattr(stream, "stream_name", "") or "").strip()
    if name:
        return name
    return stream_id[:8] if stream_id else "unknown"


def _iter_stream_history_entries(stream: Any, *, start_order: int = 0) -> list[ChatHistoryEntry]:
    stream_id = str(getattr(stream, "stream_id", "") or "").strip()
    context = getattr(stream, "context", None)
    if context is None:
        return []

    messages = list(getattr(context, "history_messages", []) or [])
    entries: list[ChatHistoryEntry] = []
    for index, message in enumerate(messages):
        if not is_visible_chat_history_message(message):
            continue
        msg_stream_id = str(getattr(message, "stream_id", "") or "").strip()
        effective_stream_id = msg_stream_id or stream_id
        entries.append(
            ChatHistoryEntry(
                message=message,
                stream_id=effective_stream_id,
                stream_name=stream_label(stream, effective_stream_id),
                platform=str(
                    getattr(message, "platform", "")
                    or getattr(stream, "platform", "")
                    or ""
                ),
                chat_type=str(
                    getattr(message, "chat_type", "")
                    or getattr(stream, "chat_type", "")
                    or ""
                ),
                order=start_order + index,
            )
        )
    return entries


def _runtime_streams(current_stream: Any, stream_manager: Any | None = None) -> list[Any]:
    streams: list[Any] = []
    current_stream_id = str(getattr(current_stream, "stream_id", "") or "").strip()
    if current_stream is not None:
        streams.append(current_stream)

    manager = stream_manager
    if manager is None:
        try:
            from src.core.managers import get_stream_manager

            manager = get_stream_manager()
        except Exception:
            manager = None

    raw_streams = getattr(manager, "_streams", {}) or {}
    stream_values = raw_streams.values() if isinstance(raw_streams, dict) else raw_streams
    for stream in list(stream_values or []):
        stream_id = str(getattr(stream, "stream_id", "") or "").strip()
        if current_stream_id and stream_id == current_stream_id:
            continue
        streams.append(stream)
    return streams


def collect_chat_history_entries(
    current_stream: Any,
    *,
    max_messages: int | None = 30,
    global_history: bool = False,
    stream_manager: Any | None = None,
) -> list[ChatHistoryEntry]:
    if max_messages is not None and max_messages <= 0:
        return []

    source_streams = (
        _runtime_streams(current_stream, stream_manager)
        if global_history
        else [current_stream]
    )
    entries: list[ChatHistoryEntry] = []
    order_base = 0
    for stream in source_streams:
        stream_entries = _iter_stream_history_entries(stream, start_order=order_base)
        entries.extend(stream_entries)
        order_base += len(stream_entries) + 1

    entries.sort(
        key=lambda entry: (
            message_timestamp(entry.message),
            entry.order,
            entry.stream_id,
        )
    )
    if max_messages is not None:
        entries = entries[-max_messages:]
    return entries


def format_chat_history_entry(
    entry: ChatHistoryEntry,
    *,
    current_stream_id: str = "",
    include_stream_label: bool = False,
) -> str:
    line = BaseChatter.format_message_line(entry.message)
    if not include_stream_label:
        return line

    relation = "当前聊天流" if entry.stream_id == current_stream_id else "其他聊天流"
    meta_parts = [relation, entry.stream_name]
    source = "/".join(part for part in [entry.platform, entry.chat_type] if part)
    if source:
        meta_parts.append(source)
    if entry.stream_id:
        meta_parts.append(entry.stream_id[:8])
    return f"〔{' | '.join(meta_parts)}〕 {line}"


def build_chat_history_text(
    current_stream: Any,
    *,
    max_messages: int | None = 30,
    global_history: bool = False,
    include_stream_label: bool | None = None,
    stream_manager: Any | None = None,
) -> str:
    entries = collect_chat_history_entries(
        current_stream,
        max_messages=max_messages,
        global_history=global_history,
        stream_manager=stream_manager,
    )
    if not entries:
        return ""

    current_stream_id = str(getattr(current_stream, "stream_id", "") or "").strip()
    should_label = global_history if include_stream_label is None else include_stream_label
    return "\n".join(
        format_chat_history_entry(
            entry,
            current_stream_id=current_stream_id,
            include_stream_label=should_label,
        )
        for entry in entries
    )


async def collect_global_chat_history_entries_from_db(
    current_stream: Any,
    *,
    max_messages: int | None = 30,
    stream_manager: Any | None = None,
    exclude_message_ids: set[str] | None = None,
) -> list[ChatHistoryEntry]:
    """从统一消息库构建跨 stream 的最近聊天历史。

    `build_chat_history_text(..., global_history=True)` 只能看到当前进程内已加载
    的 ChatStream。QQ、直播等旧流如果没有在本进程激活，就不会进内存快照。
    这里直接读 `messages` 表，恢复为 Message 后复用同一套格式化逻辑。
    """
    if max_messages is not None and max_messages <= 0:
        return []

    manager = stream_manager
    if manager is None:
        try:
            from src.core.managers import get_stream_manager

            manager = get_stream_manager()
        except Exception:
            manager = None
    if manager is None:
        return []

    try:
        from src.core.models.sql_alchemy import ChatStreams, Messages
        from src.kernel.db import QueryBuilder
    except Exception:
        return []

    wanted = max_messages if max_messages is not None else 30
    wanted = max(1, int(wanted or 1))
    scan_limit = max(wanted * 4, wanted + 20)
    excluded = {str(item) for item in (exclude_message_ids or set()) if str(item)}

    try:
        records = await QueryBuilder(Messages).order_by("-id").limit(scan_limit).all()
    except Exception:
        return []
    if not records:
        return []

    records = list(reversed(records))
    stream_ids = {
        str(getattr(record, "stream_id", "") or "")
        for record in records
        if str(getattr(record, "stream_id", "") or "")
    }
    stream_meta: dict[str, Any] = {}
    if stream_ids:
        try:
            stream_records = await QueryBuilder(ChatStreams).filter(
                stream_id__in=list(stream_ids)
            ).all()
            stream_meta = {str(item.stream_id): item for item in stream_records}
        except Exception:
            stream_meta = {}

    converter = getattr(manager, "_db_message_to_runtime", None)
    if not callable(converter):
        return []

    entries: list[ChatHistoryEntry] = []
    for order, record in enumerate(records):
        message_id = str(getattr(record, "message_id", "") or "")
        if message_id and message_id in excluded:
            continue

        try:
            message = await converter(record)
        except Exception:
            continue
        if not is_visible_chat_history_message(message):
            continue

        stream_id = str(
            getattr(message, "stream_id", "")
            or getattr(record, "stream_id", "")
            or ""
        )
        meta = stream_meta.get(stream_id)
        stream_name = ""
        if meta is not None:
            stream_name = str(getattr(meta, "group_name", "") or "").strip()
        if not stream_name:
            stream_name = stream_id[:8] if stream_id else "unknown"

        entries.append(
            ChatHistoryEntry(
                message=message,
                stream_id=stream_id,
                stream_name=stream_name,
                platform=str(
                    getattr(message, "platform", "")
                    or getattr(record, "platform", "")
                    or getattr(meta, "platform", "")
                    or ""
                ),
                chat_type=str(
                    getattr(message, "chat_type", "")
                    or getattr(meta, "chat_type", "")
                    or ""
                ),
                order=order,
            )
        )

    entries.sort(
        key=lambda entry: (
            message_timestamp(entry.message),
            entry.order,
            entry.stream_id,
        )
    )
    return entries[-wanted:]


async def build_global_chat_history_text_from_db(
    current_stream: Any,
    *,
    max_messages: int | None = 30,
    include_stream_label: bool = True,
    stream_manager: Any | None = None,
    exclude_message_ids: set[str] | None = None,
) -> str:
    entries = await collect_global_chat_history_entries_from_db(
        current_stream,
        max_messages=max_messages,
        stream_manager=stream_manager,
        exclude_message_ids=exclude_message_ids,
    )
    if not entries:
        return ""

    current_stream_id = str(getattr(current_stream, "stream_id", "") or "").strip()
    return "\n".join(
        format_chat_history_entry(
            entry,
            current_stream_id=current_stream_id,
            include_stream_label=include_stream_label,
        )
        for entry in entries
    )
