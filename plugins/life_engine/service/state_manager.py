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

from .event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
    _now_iso,
    _format_time_display,
    RUNTIME_CONTEXT_FILE,
)


logger = get_logger("life_engine", display="life_engine")

# DFC 注入目标标识
_TARGET_REMINDER_BUCKET = "actor"
_TARGET_REMINDER_NAME = "生命中枢唤醒上下文"


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
        "tool_name": event.tool_name,
        "tool_args": event.tool_args,
        "tool_success": event.tool_success,
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
    event_type_raw = str(data.get("event_type") or EventType.MESSAGE.value)
    try:
        event_type = EventType(event_type_raw)
    except ValueError:
        event_type = EventType.MESSAGE

    sequence = int(data.get("sequence") or 0)
    event_id = data.get("event_id")
    if not event_id and next_sequence_func is not None:
        # 仅在 event_id 缺失时使用生成器作为后备
        sequence = next_sequence_func()
        event_id = f"evt_{sequence}"
    elif not event_id:
        event_id = f"evt_{sequence}"

    return LifeEngineEvent(
        event_id=str(event_id),
        event_type=event_type,
        timestamp=str(data.get("timestamp") or _now_iso()),
        sequence=sequence,
        source=str(data.get("source") or "unknown"),
        source_detail=str(data.get("source_detail") or "unknown"),
        content=str(data.get("content") or ""),
        content_type=str(data.get("content_type") or "text"),
        sender=data.get("sender"),
        chat_type=data.get("chat_type"),
        stream_id=data.get("stream_id"),
        heartbeat_index=data.get("heartbeat_index"),
        tool_name=data.get("tool_name"),
        tool_args=data.get("tool_args"),
        tool_success=data.get("tool_success"),
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
        event_type=EventType.HEARTBEAT,  # 用心跳类型表示摘要
        source="system",
        source_detail="上下文压缩系统",
        content=summary,
        heartbeat_index=-1,  # 特殊标记表示这是摘要
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

    负责运行时上下文的保存与恢复，包括事件历史、SNN 状态等。
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

    def _get_lock(self) -> asyncio.Lock:
        """获取锁（懒加载或使用传入的锁）。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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
        snn_network: Any = None,
        inner_state: Any = None,
        dream_scheduler: Any = None,
    ) -> None:
        """持久化当前上下文（待处理事件 + 历史事件 + 各子系统状态）。

        Args:
            state: 当前中枢状态
            pending_events: 待处理事件列表
            event_history: 事件历史列表
            snn_network: SNN 网络（可选）
            inner_state: 调质层引擎（可选）
            dream_scheduler: 做梦调度器（可选）
        """
        async with self._get_lock():
            payload = {
                "version": 1,
                "state": {
                    "heartbeat_count": state.heartbeat_count,
                    "event_sequence": state.event_sequence,
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
                    "chatter_context_cursors": state.chatter_context_cursors or {},
                    "chatter_thought_cursors": state.chatter_thought_cursors or {},
                    "last_chatter_think_by_stream": state.last_chatter_think_by_stream or {},
                },
                "pending_events": [event_to_dict(e) for e in pending_events],
                "event_history": [event_to_dict(e) for e in event_history],
            }
            # SNN 状态持久化
            if snn_network is not None:
                try:
                    payload["snn_state"] = snn_network.serialize()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"SNN 状态序列化失败: {exc}")
            # 调质层状态持久化
            if inner_state is not None:
                try:
                    payload["neuromod_state"] = inner_state.serialize()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"调质层状态序列化失败: {exc}")
            # 做梦系统状态持久化
            if dream_scheduler is not None:
                try:
                    payload["dream_state"] = dream_scheduler.serialize()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"做梦系统状态序列化失败: {exc}")

        path = self._runtime_context_path()
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(path)
        except Exception as exc:  # noqa: BLE001
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
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error(f"life_engine 读取上下文失败: {exc}")
            return [], [], {}

        pending_raw = raw.get("pending_events")
        history_raw = raw.get("event_history")
        state_raw = raw.get("state") or {}

        if not isinstance(pending_raw, list) or not isinstance(history_raw, list):
            logger.warning("life_engine 上下文文件格式无效，跳过恢复")
            return [], [], {}

        pending_events: list[LifeEngineEvent] = []
        history_events: list[LifeEngineEvent] = []
        for item in pending_raw:
            if isinstance(item, dict):
                pending_events.append(event_from_dict(item, next_sequence_func))
        for item in history_raw:
            if isinstance(item, dict):
                history_events.append(event_from_dict(item, next_sequence_func))

        history_limit = self._history_limit_func()
        history_events = history_events[-history_limit:]

        async with self._get_lock():
            state.pending_event_count = len(pending_events)
            state.history_event_count = len(history_events)
            state.heartbeat_count = int(state_raw.get("heartbeat_count") or state.heartbeat_count)
            state.event_sequence = int(state_raw.get("event_sequence") or state.event_sequence)
            state.last_model_reply_at = state_raw.get("last_model_reply_at")
            state.last_model_reply = state_raw.get("last_model_reply")
            state.last_model_error = state_raw.get("last_model_error")
            state.last_wake_context_at = state_raw.get("last_wake_context_at")
            state.last_wake_context_size = int(state_raw.get("last_wake_context_size") or 0)
            state.last_external_message_at = state_raw.get("last_external_message_at")
            state.last_tell_dfc_at = state_raw.get("last_tell_dfc_at")
            state.tell_dfc_count = int(state_raw.get("tell_dfc_count") or 0)
            state.self_pause_until = state_raw.get("self_pause_until")
            state.self_pause_started_at = state_raw.get("self_pause_started_at")
            state.self_pause_reason = state_raw.get("self_pause_reason")
            state.self_pause_duration_minutes = int(
                state_raw.get("self_pause_duration_minutes") or 0
            )
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

            if history_events:
                max_seq = max(event.sequence for event in history_events)
                if pending_events:
                    max_seq = max(max_seq, max(event.sequence for event in pending_events))
                state.event_sequence = max(state.event_sequence, max_seq)

        # 子系统持久化状态
        persisted_state = {
            "snn_state": raw.get("snn_state"),
            "neuromod_state": raw.get("neuromod_state"),
            "dream_state": raw.get("dream_state"),
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
