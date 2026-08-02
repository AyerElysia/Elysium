"""life_engine 状态管理与持久化模块。

包含事件序列化、历史压缩、上下文持久化等状态管理功能。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.app.plugin_system.api.log_api import get_logger

from ..storage_utils import atomic_write_text
from .event_builder import (
    RUNTIME_CONTEXT_FILE,
    EventType,
    LifeEngineEvent,
    LifeEngineState,
    _format_time_display,
    _now_iso,
)

logger = get_logger("life_engine", display="life_engine")

# DFC 注入目标标识
_TARGET_REMINDER_BUCKET = "actor"
_TARGET_REMINDER_NAME = "生命中枢唤醒上下文"
_SUMMARY_CONTENT_TYPES = {
    "attention_summary",
    "history_summary",
    "subconscious_summary",
}
_CONTEXT_COMPRESSION_MARKERS = (
    "context compression system",
    "context_compression_system",
    "上下文压缩系统",
    "上下文压缩",
)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return default
        return normalized in {"1", "true", "yes", "on"}
    return bool(value)


def _is_legacy_summary_data(data: dict[str, Any]) -> bool:
    """识别 v1 用 HEARTBEAT 表示的摘要事件。"""
    if str(data.get("event_type") or "").strip().lower() != EventType.HEARTBEAT.value:
        return False
    if _safe_int(data.get("heartbeat_index"), 0) == -1:
        return True
    content_type = str(data.get("content_type") or "").strip().lower()
    if content_type in _SUMMARY_CONTENT_TYPES:
        return True
    source_detail = str(data.get("source_detail") or "").strip().lower()
    return any(marker in source_detail for marker in _CONTEXT_COMPRESSION_MARKERS)


def _sort_events(events: list[LifeEngineEvent]) -> list[LifeEngineEvent]:
    return sorted(
        events,
        key=lambda event: (_safe_int(event.sequence), str(event.event_id or "")),
    )


def _coerce_summary(value: Any) -> Any:
    from .subconscious_context import SubconsciousSummary, SummaryEntry

    if isinstance(value, SubconsciousSummary):
        return SubconsciousSummary.from_dict(value.to_dict())
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return SubconsciousSummary()
        try:
            value = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return SubconsciousSummary(entries=[SummaryEntry(kind="fact", text=text)])
    if not isinstance(value, dict):
        return SubconsciousSummary()

    canonical_keys = {
        "schema_version",
        "covered_from_sequence",
        "covered_through_sequence",
        "entries",
        "stats",
    }
    if not canonical_keys.intersection(value):
        entry = _summary_entry_from_dict(value)
        if entry is None:
            text = _summary_body_text(value)
            entry = SummaryEntry(kind="fact", text=text) if text else None
        return SubconsciousSummary(entries=[entry] if entry is not None else [])

    raw_entries = value.get("entries")
    entry_values = raw_entries if isinstance(raw_entries, list) else [raw_entries]
    entries = [
        entry
        for raw_entry in entry_values
        if raw_entry is not None
        for entry in [_summary_entry_from_dict(raw_entry)]
        if entry is not None
    ]
    if raw_entries and not entries:
        entries.append(SummaryEntry(kind="fact", text=_summary_body_text(raw_entries)))
    for key in ("summary", "text", "content", "body", "description", "fact"):
        if value.get(key) is None:
            continue
        text = _summary_body_text(value.get(key))
        if text:
            entries.append(SummaryEntry(kind="fact", text=text))
        break

    stats: dict[str, int] = {}
    raw_stats = value.get("stats")
    if isinstance(raw_stats, dict):
        for key, raw_value in raw_stats.items():
            try:
                stats[str(key)] = int(raw_value or 0)
            except (TypeError, ValueError, OverflowError):
                continue
    return SubconsciousSummary(
        schema_version=max(1, _safe_int(value.get("schema_version"), 1)),
        covered_from_sequence=max(
            0,
            _safe_int(value.get("covered_from_sequence")),
        ),
        covered_through_sequence=max(
            0,
            _safe_int(value.get("covered_through_sequence")),
        ),
        entries=entries,
        stats=stats,
    )


def _summary_to_dict(value: Any) -> dict[str, Any]:
    return _coerce_summary(value).to_dict()


def _summary_body_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value).strip()


def _summary_entry_from_dict(data: Any) -> Any:
    from .subconscious_context import SummaryEntry

    if isinstance(data, SummaryEntry):
        data = data.to_dict()
    if isinstance(data, str):
        text = data.strip()
        return SummaryEntry(kind="fact", text=text) if text else None
    if not isinstance(data, dict):
        text = str(data or "").strip()
        return SummaryEntry(kind="fact", text=text) if text else None

    raw_text = next(
        (
            data.get(key)
            for key in ("text", "content", "summary", "body", "description", "fact")
            if data.get(key) is not None
        ),
        "",
    )
    text = _summary_body_text(raw_text)
    if not text:
        text = _summary_body_text(data)
    if not text:
        return None

    raw_event_ids = data.get("event_ids")
    if raw_event_ids is None:
        raw_event_ids = data.get("event_id")
    if isinstance(raw_event_ids, (list, tuple, set)):
        event_ids = [str(value) for value in raw_event_ids if value]
    elif raw_event_ids:
        event_ids = [str(raw_event_ids)]
    else:
        event_ids = []

    raw_sequences = data.get("sequences")
    if raw_sequences is None:
        raw_sequences = data.get("sequence")
    if isinstance(raw_sequences, (list, tuple, set)):
        sequence_values = raw_sequences
    elif raw_sequences is None:
        sequence_values = []
    else:
        sequence_values = [raw_sequences]
    sequences: list[int] = []
    for value in sequence_values:
        try:
            sequences.append(int(value))
        except (TypeError, ValueError, OverflowError):
            continue

    return SummaryEntry(
        kind=str(data.get("kind") or data.get("event_type") or data.get("type") or "fact"),
        text=text,
        event_ids=list(dict.fromkeys(event_ids)),
        sequences=sorted(set(sequences)),
        source=str(data.get("source") or ""),
        tool_name=(
            str(data["tool_name"])
            if data.get("tool_name") is not None
            else None
        ),
    )


def _summary_from_event(event: LifeEngineEvent, body: Any = None) -> Any:
    """把结构化或旧文本摘要转换为可合并的摘要种子。"""
    from .subconscious_context import SubconsciousSummary, SummaryEntry

    raw_body = event.content if body is None else body
    decoded = raw_body
    if isinstance(raw_body, str) and raw_body.strip():
        try:
            decoded = json.loads(raw_body)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = None

    if isinstance(decoded, dict):
        summary = _coerce_summary(decoded)
    elif isinstance(decoded, list):
        entries = [
            entry
            for raw_entry in decoded
            for entry in [_summary_entry_from_dict(raw_entry)]
            if entry is not None
        ]
        summary = SubconsciousSummary(entries=entries)
    else:
        decoded_text = decoded.strip() if isinstance(decoded, str) else ""
        text = decoded_text or _summary_body_text(raw_body)
        summary = SubconsciousSummary(
            entries=[SummaryEntry(kind="fact", text=text)] if text else []
        )

    sequence = _safe_int(event.sequence)
    for entry in summary.entries:
        if not entry.event_ids and event.event_id:
            entry.event_ids = [str(event.event_id)]
        if not entry.sequences and event.sequence is not None:
            entry.sequences = [sequence]
        if not entry.source:
            entry.source = str(event.source or "")

    entry_sequences = [
        value
        for entry in summary.entries
        for value in entry.sequences
        if value > 0
    ]
    if summary.covered_from_sequence <= 0:
        summary.covered_from_sequence = (
            min(entry_sequences) if entry_sequences else max(0, sequence)
        )
    summary.covered_through_sequence = max(
        summary.covered_through_sequence,
        max(entry_sequences, default=0),
        max(0, sequence),
    )
    return summary


def _deduplicate_summary(summary: Any) -> Any:
    from .subconscious_context import SubconsciousSummary, SummaryEntry

    entries_by_content: dict[str, SummaryEntry] = {}
    for raw_entry in summary.entries:
        entry = _summary_entry_from_dict(raw_entry.to_dict())
        if entry is None:
            continue
        normalized = " ".join(str(entry.text or "").split()).casefold()
        if not normalized:
            continue
        existing = entries_by_content.get(normalized)
        if existing is None:
            entries_by_content[normalized] = entry
            continue
        existing.event_ids = list(
            dict.fromkeys([*existing.event_ids, *entry.event_ids])
        )
        existing.sequences = sorted(set([*existing.sequences, *entry.sequences]))
        if not existing.source:
            existing.source = entry.source
        if not existing.tool_name:
            existing.tool_name = entry.tool_name

    entries = sorted(
        entries_by_content.values(),
        key=lambda entry: (
            entry.sequences[0] if entry.sequences else 0,
            entry.kind,
            entry.text,
        ),
    )
    stats: dict[str, int] = {}
    for key, value in (summary.stats or {}).items():
        stats[str(key)] = max(0, _safe_int(value))
    return SubconsciousSummary(
        schema_version=1,
        covered_from_sequence=max(0, _safe_int(summary.covered_from_sequence)),
        covered_through_sequence=max(0, _safe_int(summary.covered_through_sequence)),
        entries=entries,
        stats=stats,
    )


def _merge_summaries(left: Any, right: Any) -> Any:
    from .subconscious_context import SubconsciousSummary

    left = _coerce_summary(left)
    right = _coerce_summary(right)
    covered_from = [
        value
        for value in (
            _safe_int(left.covered_from_sequence),
            _safe_int(right.covered_from_sequence),
        )
        if value > 0
    ]
    stats = dict(left.stats or {})
    for key, value in (right.stats or {}).items():
        stats[str(key)] = max(stats.get(str(key), 0), _safe_int(value))
    return _deduplicate_summary(
        SubconsciousSummary(
            schema_version=1,
            covered_from_sequence=min(covered_from) if covered_from else 0,
            covered_through_sequence=max(
                _safe_int(left.covered_through_sequence),
                _safe_int(right.covered_through_sequence),
            ),
            entries=[*left.entries, *right.entries],
            stats=stats,
        )
    )


def event_to_dict(event: LifeEngineEvent) -> dict[str, Any]:
    """将事件序列化为可落盘字典。

    Args:
        event: 要序列化的事件对象

    Returns:
        可 JSON 序列化的字典
    """
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "timestamp": event.timestamp,
        "sequence": event.sequence,
        "source": event.source,
        "source_detail": event.source_detail,
        "content": event.content,
        "content_type": event.content_type,
        "sender": event.sender,
        "chat_type": event.chat_type,
        "stream_id": event.stream_id,
        "heartbeat_index": event.heartbeat_index,
        "heartbeat_run_id": event.heartbeat_run_id,
        "call_id": event.call_id,
        "parent_event_id": event.parent_event_id,
        "causation_id": event.causation_id,
        "tool_name": event.tool_name,
        "tool_args": event.tool_args,
        "tool_success": event.tool_success,
        "heartbeat_context_consumed": event.heartbeat_context_consumed,
        "source_instance_id": event.source_instance_id,
        "correlation_id": event.correlation_id,
        "content_ref": event.content_ref,
        "raw_content": event.raw_content,
    }


def event_from_dict(
    data: dict[str, Any],
    next_sequence_func: Any = None,
) -> LifeEngineEvent:
    """从字典反序列化事件。

    Args:
        data: 序列化的事件字典
        next_sequence_func: 获取下一个序列号的函数（可选）

    Returns:
        反序列化的事件对象
    """
    event_type_raw = str(data.get("event_type") or EventType.MESSAGE.value).strip().lower()
    try:
        event_type = EventType(event_type_raw)
    except ValueError:
        event_type = EventType.MESSAGE
    if _is_legacy_summary_data(data):
        event_type = EventType.SUMMARY

    sequence = _safe_int(data.get("sequence"))
    event_id = data.get("event_id")
    if not event_id and next_sequence_func is not None:
        # 仅在 event_id 缺失时使用生成器作为后备
        sequence = next_sequence_func()
        event_id = f"evt_{sequence}"
    elif not event_id:
        event_id = f"evt_{sequence}"

    raw_content = data.get("content")
    if isinstance(raw_content, (dict, list)):
        content = json.dumps(raw_content, ensure_ascii=False, sort_keys=True)
    else:
        content = str(raw_content or "")

    return LifeEngineEvent(
        event_id=str(event_id),
        event_type=event_type,
        timestamp=str(data.get("timestamp") or _now_iso()),
        sequence=sequence,
        source=str(data.get("source") or "unknown"),
        source_detail=str(data.get("source_detail") or "unknown"),
        content=content,
        content_type=str(data.get("content_type") or "text"),
        sender=data.get("sender"),
        chat_type=data.get("chat_type"),
        stream_id=data.get("stream_id"),
        heartbeat_index=data.get("heartbeat_index"),
        heartbeat_run_id=data.get("heartbeat_run_id"),
        call_id=data.get("call_id"),
        parent_event_id=data.get("parent_event_id"),
        causation_id=data.get("causation_id"),
        tool_name=data.get("tool_name"),
        tool_args=data.get("tool_args"),
        tool_success=data.get("tool_success"),
        heartbeat_context_consumed=_safe_bool(
            data.get("heartbeat_context_consumed"),
        ),
        source_instance_id=data.get("source_instance_id"),
        correlation_id=data.get("correlation_id"),
        content_ref=data.get("content_ref"),
        raw_content=data.get("raw_content"),
    )


def generate_event_summary(events: list[LifeEngineEvent]) -> str:
    """生成事件摘要。

    Args:
        events: 要摘要的事件列表

    Returns:
        格式化的摘要文本
    """
    if not events:
        return "（无历史事件）"

    # 统计各类事件
    msg_count = 0
    heartbeat_count = 0
    tool_count = 0
    senders: set[str] = set()
    topics: list[str] = []

    for event in events:
        if event.event_type == EventType.MESSAGE:
            msg_count += 1
            if event.sender:
                senders.add(event.sender)
            # 提取关键词作为话题
            if event.content and len(event.content) > 10:
                topics.append(event.content[:30])
        elif event.event_type == EventType.HEARTBEAT:
            heartbeat_count += 1
        elif event.event_type in (EventType.TOOL_CALL, EventType.TOOL_RESULT):
            tool_count += 1

    # 时间范围
    start_time = _format_time_display(events[0].timestamp) if events else "未知"
    end_time = _format_time_display(events[-1].timestamp) if events else "未知"

    # 构建摘要
    parts = [f"📋 **历史摘要** ({start_time} ~ {end_time})"]

    stats = []
    if msg_count > 0:
        sender_str = "、".join(list(senders)[:3])
        if len(senders) > 3:
            sender_str += f" 等{len(senders)}人"
        stats.append(f"{msg_count}条消息（来自 {sender_str}）")
    if heartbeat_count > 0:
        stats.append(f"{heartbeat_count}次心跳")
    if tool_count > 0:
        stats.append(f"{tool_count}次工具调用")

    if stats:
        parts.append("- " + "，".join(stats))

    # 添加话题提示（最多3个）
    if topics:
        topic_hints = topics[:3]
        parts.append(f"- 话题涉及: {' / '.join(topic_hints)}...")

    return "\n".join(parts)


def compress_history(
    events: list[LifeEngineEvent],
    target_count: int,
) -> list[LifeEngineEvent]:
    """压缩事件历史，保留最近事件，其余总结为摘要。

    压缩策略（参考 Claude Code）：
    1. 保留最近 60% 的事件完整
    2. 将较早的 40% 压缩为一条摘要事件

    Args:
        events: 要压缩的事件列表
        target_count: 目标事件数量

    Returns:
        压缩后的事件列表
    """
    if len(events) <= target_count:
        return events

    # 计算保留数量
    keep_count = int(target_count * 0.6)
    compress_count = len(events) - keep_count

    if compress_count <= 0:
        return events[-target_count:]

    # 分割事件
    old_events = events[:compress_count]
    recent_events = events[compress_count:]

    # 生成摘要
    summary = generate_event_summary(old_events)

    # 创建摘要事件
    summary_event = LifeEngineEvent(
        event_id=f"summary_{uuid4().hex[:12]}",
        sequence=old_events[-1].sequence if old_events else 0,
        timestamp=old_events[-1].timestamp if old_events else _now_iso(),
        event_type=EventType.SUMMARY,
        source="system",
        source_detail="上下文压缩系统",
        content=summary,
        content_type="history_summary",
    )

    # 返回：摘要 + 最近事件
    result = [summary_event] + recent_events

    logger.info(
        f"life_engine 上下文压缩: {len(events)} → {len(result)} "
        f"(压缩了 {compress_count} 条旧事件)"
    )

    return result


class PersistenceError(RuntimeError):
    """运行时上下文持久化失败时抛出的异常。"""


class StatePersistence:
    """状态持久化管理器。

    负责运行时上下文的保存与恢复，包括事件历史。
    """

    def __init__(
        self,
        workspace_path: str,
        history_limit_func: Any,
        lock: asyncio.Lock | None = None,
    ) -> None:
        """初始化状态持久化管理器。

        Args:
            workspace_path: 工作空间路径
            history_limit_func: 获取历史上限的函数
            lock: 异步锁（可选，用于线程安全）
        """
        self._workspace_path = workspace_path
        self._history_limit_func = history_limit_func
        self._lock = lock
        self._write_lock: asyncio.Lock | None = None

    def _get_lock(self) -> asyncio.Lock:
        """获取锁（懒加载或使用传入的锁）。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_write_lock(self) -> asyncio.Lock:
        """Return the lock that serializes file replacement order."""

        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    def _runtime_context_path(self) -> Path:
        """返回运行时上下文持久化文件路径。"""
        workspace = Path(self._workspace_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace / RUNTIME_CONTEXT_FILE

    async def save_runtime_context(
        self,
        state: LifeEngineState,
        pending_events: list[LifeEngineEvent],
        event_history: list[LifeEngineEvent],
    ) -> None:
        """持久化当前上下文（待处理事件 + 历史事件）。

        Args:
            state: 当前中枢状态
            pending_events: 待处理事件列表
            event_history: 事件历史列表
        """
        async with self._get_lock():
            payload = {
                "version": 2,
                "state": {
                    "heartbeat_count": state.heartbeat_count,
                    "event_sequence": state.event_sequence,
                    "heartbeat_context_cursor": _safe_int(
                        getattr(state, "heartbeat_context_cursor", 0)
                    ),
                    "subconscious_summary": _summary_to_dict(
                        getattr(state, "subconscious_summary", None)
                    ),
                    "last_model_reply_at": state.last_model_reply_at,
                    "last_model_reply": state.last_model_reply,
                    "last_model_error": state.last_model_error,
                    "last_wake_context_at": state.last_wake_context_at,
                    "last_wake_context_size": state.last_wake_context_size,
                    "last_external_message_at": state.last_external_message_at,
                    "last_tell_dfc_at": state.last_tell_dfc_at,
                    "tell_dfc_count": state.tell_dfc_count,
                    "self_pause_until": state.self_pause_until,
                    "self_pause_started_at": state.self_pause_started_at,
                    "self_pause_reason": state.self_pause_reason,
                    "self_pause_duration_minutes": state.self_pause_duration_minutes,
                    "self_pause_checkpoint_minutes": state.self_pause_checkpoint_minutes,
                    "consecutive_rest_count": state.consecutive_rest_count,
                    "last_leisure_seen_at": state.last_leisure_seen_at,
                    "chatter_context_cursors": state.chatter_context_cursors or {},
                    "chatter_thought_cursors": state.chatter_thought_cursors or {},
                    "last_chatter_think_by_stream": state.last_chatter_think_by_stream or {},
                },
                "pending_events": [event_to_dict(e) for e in pending_events],
                "event_history": [event_to_dict(e) for e in event_history],
            }

        path = self._runtime_context_path()
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            async with self._get_write_lock():
                await asyncio.to_thread(atomic_write_text, path, serialized)
        except Exception as exc:
            logger.error(f"life_engine 持久化上下文失败: {exc}")
            raise PersistenceError(f"Failed to persist runtime context: {exc}") from exc

    async def load_runtime_context(
        self,
        state: LifeEngineState,
        next_sequence_func: Any,
    ) -> tuple[list[LifeEngineEvent], list[LifeEngineEvent], dict[str, Any]]:
        """从持久化文件恢复上下文。

        Args:
            state: 要恢复的状态对象
            next_sequence_func: 获取下一个序列号的函数

        Returns:
            元组：(待处理事件列表, 事件历史列表, 持久化的子系统状态字典)
        """
        path = self._runtime_context_path()
        if not path.exists():
            return [], [], {}

        try:
            raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"life_engine 读取上下文失败: {exc}")
            return [], [], {}

        if not isinstance(raw, dict):
            logger.warning("life_engine 上下文文件格式无效，跳过恢复")
            return [], [], {}

        pending_raw = raw.get("pending_events", [])
        history_raw = raw.get("event_history", [])
        raw_state_value = raw.get("state")
        state_raw = raw_state_value if isinstance(raw_state_value, dict) else {}

        if not isinstance(pending_raw, list) or not isinstance(history_raw, list):
            logger.warning("life_engine 上下文文件格式无效，跳过恢复")
            return [], [], {}

        persisted_summary = _coerce_summary(
            state_raw.get("subconscious_summary", raw.get("subconscious_summary"))
        )
        pending_events: list[LifeEngineEvent] = []
        pending_summary_inputs: list[tuple[LifeEngineEvent, dict[str, Any]]] = []
        parsed_history: list[tuple[LifeEngineEvent, dict[str, Any], bool]] = []
        history_events: list[LifeEngineEvent] = []
        for item in pending_raw:
            if isinstance(item, dict):
                event = event_from_dict(item, next_sequence_func)
                if event.event_type == EventType.SUMMARY:
                    pending_summary_inputs.append((event, item))
                else:
                    pending_events.append(event)
        for item in history_raw:
            if not isinstance(item, dict):
                continue
            legacy_summary = _is_legacy_summary_data(item)
            parsed_history.append(
                (event_from_dict(item, next_sequence_func), item, legacy_summary)
            )

        pending_events = _sort_events(pending_events)
        pending_summary_inputs.sort(
            key=lambda item: (
                _safe_int(item[0].sequence),
                str(item[0].event_id or ""),
            )
        )
        for event, item in pending_summary_inputs:
            persisted_summary = _merge_summaries(
                persisted_summary,
                _summary_from_event(event, item.get("content")),
            )
        parsed_history.sort(
            key=lambda item: (
                _safe_int(item[0].sequence),
                str(item[0].event_id or ""),
            )
        )
        parsed_history_events = [event for event, _, _ in parsed_history]
        for event, item, legacy_summary in parsed_history:
            if legacy_summary or event.event_type == EventType.SUMMARY:
                persisted_summary = _merge_summaries(
                    persisted_summary,
                    _summary_from_event(event, item.get("content")),
                )
            if not legacy_summary and event.event_type != EventType.SUMMARY:
                history_events.append(event)
        loaded_events = [*pending_events, *parsed_history_events, *[event for event, _ in pending_summary_inputs]]
        loaded_max_sequence = max(
            (_safe_int(event.sequence) for event in loaded_events),
            default=0,
        )
        history_limit = max(0, _safe_int(self._history_limit_func()))
        history_events = history_events[-history_limit:] if history_limit else []
        summary_dict = _deduplicate_summary(persisted_summary).to_dict()

        async with self._get_lock():
            state.pending_event_count = len(pending_events)
            state.history_event_count = len(history_events)
            state.heartbeat_count = _safe_int(
                state_raw.get("heartbeat_count"),
                state.heartbeat_count,
            )
            state.event_sequence = max(
                _safe_int(state.event_sequence),
                _safe_int(state_raw.get("event_sequence")),
                loaded_max_sequence,
            )
            if hasattr(state, "heartbeat_context_cursor"):
                state.heartbeat_context_cursor = _safe_int(
                    state_raw.get("heartbeat_context_cursor")
                )
            if hasattr(state, "subconscious_summary"):
                state.subconscious_summary = summary_dict
            state.last_model_reply_at = state_raw.get("last_model_reply_at")
            state.last_model_reply = state_raw.get("last_model_reply")
            state.last_model_error = state_raw.get("last_model_error")
            state.last_wake_context_at = state_raw.get("last_wake_context_at")
            state.last_wake_context_size = _safe_int(state_raw.get("last_wake_context_size"))
            state.last_external_message_at = state_raw.get("last_external_message_at")
            state.last_tell_dfc_at = state_raw.get("last_tell_dfc_at")
            state.tell_dfc_count = _safe_int(state_raw.get("tell_dfc_count"))
            state.self_pause_until = state_raw.get("self_pause_until")
            state.self_pause_started_at = state_raw.get("self_pause_started_at")
            state.self_pause_reason = state_raw.get("self_pause_reason")
            state.self_pause_duration_minutes = _safe_int(
                state_raw.get("self_pause_duration_minutes")
            )
            state.self_pause_checkpoint_minutes = _safe_int(
                state_raw.get("self_pause_checkpoint_minutes", 30)
            )
            state.consecutive_rest_count = _safe_int(
                state_raw.get("consecutive_rest_count", 0)
            )
            state.last_leisure_seen_at = state_raw.get("last_leisure_seen_at")
            raw_cursors = state_raw.get("chatter_context_cursors")
            if isinstance(raw_cursors, dict):
                cursors: dict[str, int] = {}
                for key, value in raw_cursors.items():
                    sid = str(key).strip()
                    if not sid:
                        continue
                    try:
                        cursors[sid] = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                state.chatter_context_cursors = cursors

            raw_thought_cursors = state_raw.get("chatter_thought_cursors")
            if isinstance(raw_thought_cursors, dict):
                t_cursors: dict[str, int] = {}
                for key, value in raw_thought_cursors.items():
                    sid = str(key).strip()
                    if not sid:
                        continue
                    try:
                        t_cursors[sid] = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                state.chatter_thought_cursors = t_cursors

            raw_last_thinks = state_raw.get("last_chatter_think_by_stream")
            if isinstance(raw_last_thinks, dict):
                snapshots: dict[str, dict[str, str]] = {}
                for key, value in raw_last_thinks.items():
                    sid = str(key).strip()
                    if not sid or not isinstance(value, dict):
                        continue
                    snapshot: dict[str, str] = {}
                    for field in (
                        "thought",
                        "mood",
                        "decision",
                        "expected_response",
                        "recorded_at",
                    ):
                        raw_field = value.get(field)
                        text = str(raw_field or "").strip()
                        if text:
                            snapshot[field] = text
                    if snapshot:
                        snapshots[sid] = snapshot
                state.last_chatter_think_by_stream = snapshots

        # 持久化状态
        persisted_state = {
            "subconscious_summary": summary_dict,
        }

        logger.info(
            "life_engine 上下文恢复完成: "
            f"history={len(history_events)} pending={len(pending_events)} "
            f"heartbeat_count={state.heartbeat_count}"
        )

        return pending_events, history_events, persisted_state


def clear_wake_context_reminder() -> None:
    """清除系统提醒中的中枢上下文。"""
    from src.core.prompt import get_system_reminder_store

    get_system_reminder_store().delete(_TARGET_REMINDER_BUCKET, _TARGET_REMINDER_NAME)


def minutes_since_time(iso_time: str | None) -> int | None:
    """计算距离给定 ISO 时间过去了多少分钟。

    Args:
        iso_time: ISO 格式的时间字符串

    Returns:
        分钟数，如果时间为空或解析失败则返回 None
    """
    if not iso_time:
        return None
    try:
        last_time = datetime.fromisoformat(iso_time)
        now = datetime.now().astimezone()
        delta = now - last_time
        return int(delta.total_seconds() / 60)
    except Exception:
        return None


def get_file_metadata(file_path: Path) -> dict[str, str]:
    """获取文件元数据。

    Args:
        file_path: 文件路径

    Returns:
        包含 ext、time_ago、size 的字典
    """
    try:
        if not file_path.exists():
            return {"ext": "?", "time_ago": "未知", "size": "0B"}

        stat = file_path.stat()

        # 文件扩展名
        ext = file_path.suffix or "(无扩展名)"

        # 相对时间
        now = time.time()
        days_ago = int((now - stat.st_mtime) / 86400)
        if days_ago == 0:
            time_ago = "今天"
        elif days_ago == 1:
            time_ago = "昨天"
        elif days_ago < 7:
            time_ago = f"{days_ago}天前"
        elif days_ago < 30:
            time_ago = f"{days_ago // 7}周前"
        else:
            time_ago = f"{days_ago // 30}月前"

        # 文件大小
        size = stat.st_size
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                size_str = f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
                break
            size /= 1024
        else:
            size_str = f"{size:.1f}TB"

        return {"ext": ext, "time_ago": time_ago, "size": size_str}
    except Exception as e:
        logger.debug(f"获取文件元数据失败 {file_path}: {e}")
        return {"ext": "?", "time_ago": "未知", "size": "0B"}
