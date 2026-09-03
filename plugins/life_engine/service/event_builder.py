"""life_engine 事件构建器。

包含事件类型定义、事件构建函数和时间格式化辅助函数。
这些是服务的基础组件，被其他模块依赖。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from datetime import time as dtime
from enum import Enum
from typing import Any

from src.core.models.message import Message


class EventType(str, Enum):
    """事件类型枚举。"""

    MESSAGE = "message"          # 外部消息
    HEARTBEAT = "heartbeat"      # 心跳回复（内部思考）
    SUMMARY = "summary"          # 潜意识/上下文规范摘要
    TOOL_CALL = "tool_call"      # 工具调用
    TOOL_RESULT = "tool_result"  # 工具返回结果
    AGENT_RESULT = "agent_result"  # 后台智能体执行结果
    CONSCIOUS_ACTIVITY = "conscious_activity"  # 意识实例实际发生的认知/状态活动


@dataclass(slots=True)
class LifeEngineEvent:
    """生命中枢事件 - 统一的事件模型。

    所有交互都是事件，保持时间连续性。
    """

    # 基础信息
    event_id: str
    event_type: EventType
    timestamp: str
    sequence: int  # 事件序列号，用于排序

    # 来源信息
    source: str  # 事件来源标识（平台名/life_engine等）
    source_detail: str  # 详细来源描述

    # 内容
    content: str
    content_type: str = "text"

    # 消息特有字段
    sender: str | None = None
    sender_id: str | None = None
    sender_platform_account_key: str | None = None
    canonical_person_key: str | None = None
    identity_resolution_status: str | None = None
    chat_type: str | None = None
    stream_id: str | None = None

    # 心跳特有字段
    heartbeat_index: int | None = None

    # 因果关联字段
    heartbeat_run_id: str | None = None
    call_id: str | None = None
    parent_event_id: str | None = None
    occurrence_id: str | None = None
    causation_id: str | None = None

    # 工具调用特有字段
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_success: bool | None = None

    # heartbeat context acknowledgement; raw events remain append-only
    heartbeat_context_consumed: bool = False

    # Durable attribution fields used by the raw experience ledger.  The
    # legacy ``content`` may stay presentation-bounded while ``raw_content``
    # preserves the complete experience at the persistence boundary.
    source_instance_id: str | None = None
    correlation_id: str | None = None
    content_ref: str | None = None
    raw_content: str | None = None


@dataclass(slots=True)
class LifeEngineState:
    """life_engine 中枢状态。"""

    running: bool = False
    started_at: str | None = None
    last_heartbeat_at: str | None = None
    heartbeat_count: int = 0
    pending_event_count: int = 0
    history_event_count: int = 0
    event_sequence: int = 0
    heartbeat_context_cursor: int = 0
    last_wake_context_at: str | None = None
    last_wake_context_size: int = 0
    last_model_reply_at: str | None = None
    last_model_reply: str | None = None
    last_model_error: str | None = None
    last_error: str | None = None
    # 跟踪最后一次外部消息和传话时间
    last_external_message_at: str | None = None
    # 最后一次外部消息来自哪个流/平台（心跳只知道"X分钟前有消息"不知道是哪个流，
    # 导致主动聊天时找不到目标流，见 send_targets 心跳段落）
    last_external_stream_id: str = ""
    last_external_source: str = ""
    # 空闲心跳追踪：连续没有工具调用的心跳数
    idle_heartbeat_count: int = 0
    # 主动休息锁：由 life_engine 自己决定暂停 LLM 心跳一段时间
    self_pause_until: str | None = None
    self_pause_started_at: str | None = None
    self_pause_reason: str | None = None
    self_pause_duration_minutes: int = 0
    self_pause_checkpoint_minutes: int = 30  # 休息期间检查点间隔
    consecutive_rest_count: int = 0  # 连续休息次数（用于觉察）
    last_leisure_seen_at: str | None = None  # 上次看到休闲机会快照的时间
    # 每个聊天流已经给 life_chatter 注入过的事件序列高水位
    # NOTE: 老字段，新代码实际上把它当作 chatter_event_cursors 使用。
    # 加载老 state 时直接复用；写出时仍写入此字段以保持兼容。
    chatter_context_cursors: dict[str, int] = field(default_factory=dict)
    # 每个聊天流已经看过的 thought_stream 全局 revision 高水位
    chatter_thought_cursors: dict[str, int] = field(default_factory=dict)
    # 每个聊天流最近一次 action-think 的快照
    last_chatter_think_by_stream: dict[str, dict[str, str]] = field(default_factory=dict)
    # 可恢复的规范化潜意识摘要（持久化为 JSON 字典）
    subconscious_summary: dict[str, Any] = field(default_factory=dict)
    # Inner-dialogue open/return projection; rebuildable from Life Events.
    inner_dialogue_ledger: dict[str, Any] = field(default_factory=dict)


# 中枢内部消息的固定标识
INTERNAL_PLATFORM = "life_engine"
INTERNAL_STREAM_ID = "life_engine_internal"
RUNTIME_CONTEXT_FILE = "life_engine_context.json"


def is_life_heartbeat_event(event: LifeEngineEvent) -> bool:
    """判断事件是否为生命中枢自身产生的真实心跳回复。

    ``HEARTBEAT`` 仍兼容承载 life_chatter 的 inner-monologue 事件；
    后者属于对话器运行态，不应参与中枢心跳或 SNN 奖赏统计。
    """
    event_type = getattr(event, "event_type", None)
    event_type_value = getattr(event_type, "value", event_type)
    if str(event_type_value or "").strip().lower() != EventType.HEARTBEAT.value:
        return False

    content_type = str(getattr(event, "content_type", "") or "").strip().lower()
    heartbeat_index = getattr(event, "heartbeat_index", None)
    if heartbeat_index is not None:
        try:
            if int(heartbeat_index) < 0:
                return False
        except (TypeError, ValueError, OverflowError):
            pass
    if content_type == "chatter_inner_monologue":
        return False
    if content_type == "heartbeat_reply":
        return True
    if str(getattr(event, "source", "") or "").strip() == INTERNAL_PLATFORM:
        return True
    return heartbeat_index is not None


def _now_iso() -> str:
    """返回当前时间的 ISO 字符串。"""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _format_time(raw_time: float | int | None) -> str:
    """格式化消息时间为 ISO 字符串（内部存储用）。"""
    if raw_time is None:
        return _now_iso()
    try:
        return datetime.fromtimestamp(float(raw_time), tz=timezone.utc).astimezone().isoformat()
    except Exception:
        return _now_iso()


def _format_time_display(iso_time: str | None) -> str:
    """格式化时间为简洁的显示格式。

    - 5分钟内："刚才"
    - 1小时内："X分钟前"
    - 当日："HH:MM"
    - 跨日："MM-DD HH:MM"
    """
    if not iso_time:
        return "未知时间"

    try:
        dt = datetime.fromisoformat(iso_time)
        now = datetime.now(dt.tzinfo or timezone.utc)
        diff = now - dt
        diff_seconds = diff.total_seconds()

        if diff_seconds < 0:
            return dt.strftime("%H:%M")
        elif diff_seconds < 300:
            return "刚才"
        elif diff_seconds < 3600:
            minutes = int(diff_seconds / 60)
            return f"{minutes}分钟前"
        elif dt.date() == now.date():
            return dt.strftime("%H:%M")
        elif (now.date() - dt.date()).days < 7:
            return dt.strftime("%m-%d %H:%M")
        else:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        return "未知时间"


def _format_current_time() -> str:
    """格式化当前时间为人类可读格式。"""
    now = datetime.now(timezone.utc).astimezone()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekdays[now.weekday()]
    return f"{now.strftime('%Y-%m-%d')} {weekday} {now.strftime('%H:%M:%S')}"


def _shorten_text(text: str, *, max_length: int = 240) -> str:
    """截断过长文本，保持唤醒上下文可读。"""
    normalized = " ".join(text.split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 1] + "…"


def _canonical_json_text(value: Any) -> str:
    """Serialize one durable activity payload without losing Unicode text."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _parse_hhmm(value: str) -> dtime | None:
    """解析 HH:MM（24 小时制）时间字符串。"""
    raw = (value or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return dtime(hour=hour, minute=minute)


class EventBuilder:
    """事件构建器类。

    负责将各种输入转换为统一的事件格式。
    """

    def __init__(self, next_sequence_func) -> None:
        """初始化事件构建器。

        Args:
            next_sequence_func: 获取下一个序列号的函数
        """
        self._next_sequence = next_sequence_func

    def build_message_event(self, message: Message, direction: str = "received") -> LifeEngineEvent:
        """将核心消息对象转换为事件。"""
        seq = self._next_sequence()
        extra = getattr(message, "extra", {}) or {}
        platform = str(message.platform or "unknown")
        chat_type = str(message.chat_type or "unknown").lower()
        stream_id = str(message.stream_id or "")

        group_id = str(extra.get("group_id") or "")
        group_name = str(extra.get("group_name") or "")

        sender_display = (
            str(message.sender_cardname or message.sender_name or message.sender_id or "未知发送者")
        )
        sender_id = str(message.sender_id or "")

        direction_label = "入站" if direction == "received" else "出站"

        if chat_type == "group":
            source_kind = "群聊"
            source_name = group_name or group_id or stream_id[:8] or "未知群聊"
            source_detail = (
                f"{platform} | {direction_label} | {source_kind} | {source_name} | 群ID={group_id or 'unknown'}"
            )
        elif chat_type == "private":
            source_kind = "私聊"
            source_name = sender_display
            source_detail = (
                f"{platform} | {direction_label} | {source_kind} | {source_name} | 用户ID={sender_id or 'unknown'}"
            )
        elif chat_type == "discuss":
            source_kind = "讨论组"
            source_name = group_name or group_id or stream_id[:8] or "未知讨论组"
            source_detail = (
                f"{platform} | {direction_label} | {source_kind} | {source_name} | 讨论组ID={group_id or 'unknown'}"
            )
        else:
            source_kind = chat_type or "未知"
            source_name = group_name or sender_display or stream_id[:8] or "未知来源"
            source_detail = (
                f"{platform} | {direction_label} | {source_kind} | {source_name} | 来源ID={group_id or sender_id or 'unknown'}"
            )

        raw_content = message.processed_plain_text
        if raw_content is None:
            raw_content = message.content if isinstance(message.content, str) else str(message.content)
        full_content = str(raw_content).strip() or f"[{message.message_type.value}]"
        content = _shorten_text(full_content)

        message_type = getattr(message.message_type, "value", str(message.message_type))

        occurrence_digest = hashlib.sha256(
            _canonical_json_text(
                {
                    "direction": str(direction or "received"),
                    "message_id": str(message.message_id or f"sequence:{seq}"),
                    "platform": platform,
                    "stream_id": stream_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        occurrence_id = f"life-message:{occurrence_digest}"

        return LifeEngineEvent(
            event_id=f"msg_{message.message_id or seq}",
            event_type=EventType.MESSAGE,
            timestamp=_format_time(getattr(message, "time", None)),
            sequence=seq,
            source=platform,
            source_detail=source_detail,
            content=content,
            content_type=message_type,
            sender=sender_display,
            sender_id=sender_id or None,
            sender_platform_account_key=(
                str(extra.get("sender_platform_account_key") or "").strip() or None
            ),
            canonical_person_key=(
                str(extra.get("canonical_person_key") or "").strip() or None
            ),
            identity_resolution_status=(
                str(extra.get("identity_resolution_status") or "").strip() or None
            ),
            chat_type=chat_type,
            stream_id=stream_id,
            occurrence_id=occurrence_id,
            source_instance_id=str(extra.get("consciousness_instance_id") or "") or None,
            correlation_id=str(
                extra.get("correlation_id")
                or extra.get("episode_id")
                or ""
            ) or None,
            content_ref=(
                str(extra.get("content_ref") or "").strip()
                or f"life-event-occurrence:{occurrence_id}"
            ),
            raw_content=full_content,
        )

    def build_inner_dialogue_event(
        self,
        thought: str,
        *,
        mode: str = "reflect",
        expect_surface: bool = True,
        receipt_id: str = "",
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
        source_instance_id: str = "",
    ) -> LifeEngineEvent:
        """构建主意识 → 潜意识 的异步内心对话事件。

        这不是外部用户留言，也不是第二人格咨询，而是同一主体把念头沉进中枢慢循环。
        """
        from ..inner_dialogue.protocol import (
            INNER_DIALOGUE_KIND,
            dump_inner_dialogue_payload,
        )

        seq = self._next_sequence()
        platform_name = str(platform or "life_chatter").strip() or "life_chatter"
        chat_type_name = str(chat_type or "unknown").strip().lower() or "unknown"
        sender_display = str(sender_name or "主意识").strip() or "主意识"
        target_stream_id = str(stream_id or "").strip()
        instance_id = str(source_instance_id or "").strip()
        mode_name = str(mode or "reflect").strip().lower() or "reflect"
        rid = str(receipt_id or "").strip() or f"idlg_{seq}"
        body = str(thought or "").strip()
        thought_bytes = len(body.encode("utf-8"))
        thought_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        detail_parts = [
            platform_name,
            "内部",
            "内心对话",
            f"mode={mode_name}",
            f"receipt={rid}",
            f"expect_surface={'yes' if expect_surface else 'no'}",
        ]
        if target_stream_id:
            detail_parts.append(f"stream_id={target_stream_id}")
        if instance_id:
            detail_parts.append(f"source_instance_id={instance_id}")

        header = (
            f"[内心对话 | mode={mode_name} | receipt={rid} | "
            f"expect_surface={'yes' if expect_surface else 'no'}]"
        )
        content = f"{header}\n{body}" if body else header
        payload = dump_inner_dialogue_payload(
            {
                "kind": INNER_DIALOGUE_KIND,
                "receipt_id": rid,
                "expect_surface": bool(expect_surface),
                "stream_id": target_stream_id,
                "source_instance_id": instance_id,
                "mode": mode_name,
                "thought": body,
                "thought_sha256": thought_sha256,
                "thought_bytes": thought_bytes,
            }
        )

        return LifeEngineEvent(
            event_id=f"idlg_{seq}",
            event_type=EventType.MESSAGE,
            timestamp=_now_iso(),
            sequence=seq,
            source=platform_name,
            source_detail=" | ".join(detail_parts),
            content=content,
            content_type=INNER_DIALOGUE_KIND,
            sender=sender_display,
            chat_type=chat_type_name,
            stream_id=target_stream_id or None,
            occurrence_id=rid,
            correlation_id=rid,
            source_instance_id=instance_id or None,
            raw_content=payload,
        )

    def build_inner_dialogue_return_event(
        self,
        *,
        receipt_id: str,
        statement: str,
        stream_id: str,
        occurrence_id: str,
        actor_consciousness_instance_id: str,
        causation_id: str = "",
        source_instance_id: str = "",
    ) -> LifeEngineEvent:
        """构建潜意识 → 表达层 的显式回声事件。"""
        from ..inner_dialogue.protocol import (
            INNER_DIALOGUE_RETURN_KIND,
            dump_inner_dialogue_payload,
        )

        seq = self._next_sequence()
        rid = str(receipt_id or "").strip()
        target_stream_id = str(stream_id or "").strip()
        occurrence = str(occurrence_id or "").strip() or f"inner-return:{seq}"
        actor = str(actor_consciousness_instance_id or "").strip()
        source_instance = str(source_instance_id or actor).strip()
        body = str(statement or "").strip()
        statement_bytes = len(body.encode("utf-8"))
        statement_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        header = (
            f"[内心对话回声 | receipt={rid} | occurrence={occurrence} | "
            f"stream_id={target_stream_id or 'missing'}]"
        )
        content = f"{header}\n{body}" if body else header
        payload = dump_inner_dialogue_payload(
            {
                "kind": INNER_DIALOGUE_RETURN_KIND,
                "receipt_id": rid,
                "return_occurrence_id": occurrence,
                "stream_id": target_stream_id,
                "actor_consciousness_instance_id": actor,
                "statement": body,
                "statement_sha256": statement_sha256,
                "statement_bytes": statement_bytes,
            }
        )
        return LifeEngineEvent(
            event_id=f"idlg_ret_{seq}",
            event_type=EventType.MESSAGE,
            timestamp=_now_iso(),
            sequence=seq,
            source=INTERNAL_PLATFORM,
            source_detail=(
                "内部 | 内心对话回声 | "
                f"receipt={rid} | actor={actor or 'unknown'}"
            ),
            content=content,
            content_type=INNER_DIALOGUE_RETURN_KIND,
            sender="潜意识回声",
            stream_id=target_stream_id or None,
            occurrence_id=occurrence,
            causation_id=str(causation_id or rid) or None,
            correlation_id=rid,
            source_instance_id=source_instance or None,
            raw_content=payload,
        )

    def build_inner_dialogue_return_delivery_event(
        self,
        *,
        receipt_id: str,
        return_occurrence_id: str,
        stream_id: str,
        trigger_message_id: str,
        causation_id: str = "",
    ) -> LifeEngineEvent:
        """记录回声已进入 originating 窗口的 content-free 投递回执。"""
        from ..inner_dialogue.protocol import (
            INNER_DIALOGUE_RETURN_DELIVERY_KIND,
            dump_inner_dialogue_payload,
        )

        seq = self._next_sequence()
        rid = str(receipt_id or "").strip()
        occurrence = str(return_occurrence_id or "").strip()
        target_stream_id = str(stream_id or "").strip()
        trigger_id = str(trigger_message_id or "").strip()
        payload = dump_inner_dialogue_payload(
            {
                "kind": INNER_DIALOGUE_RETURN_DELIVERY_KIND,
                "receipt_id": rid,
                "return_occurrence_id": occurrence,
                "stream_id": target_stream_id,
                "trigger_message_id": trigger_id,
                "delivered": True,
            }
        )
        return LifeEngineEvent(
            event_id=f"idlg_del_{seq}",
            event_type=EventType.MESSAGE,
            timestamp=_now_iso(),
            sequence=seq,
            source=INTERNAL_PLATFORM,
            source_detail=(
                "内部 | 内心对话回声投递 | "
                f"receipt={rid} | trigger={trigger_id or 'none'}"
            ),
            content=(
                f"[内心对话回声投递 | receipt={rid} | "
                f"occurrence={occurrence} | delivered=yes]"
            ),
            content_type=INNER_DIALOGUE_RETURN_DELIVERY_KIND,
            sender="潜意识回声投递",
            stream_id=target_stream_id or None,
            occurrence_id=f"{occurrence}:delivery" if occurrence else f"idlg_del_{seq}",
            causation_id=str(causation_id or occurrence or rid) or None,
            correlation_id=rid,
            raw_content=payload,
        )

    def build_heartbeat_event(
        self,
        content: str,
        heartbeat_count: int,
        task_name: str,
        *,
        heartbeat_run_id: str | None = None,
        call_id: str | None = None,
        parent_event_id: str | None = None,
        causation_id: str | None = None,
    ) -> LifeEngineEvent:
        """构建心跳事件（中枢内部思考）。"""
        seq = self._next_sequence()
        return LifeEngineEvent(
            event_id=f"hb_{heartbeat_count}_{seq}",
            event_type=EventType.HEARTBEAT,
            timestamp=_now_iso(),
            sequence=seq,
            source=INTERNAL_PLATFORM,
            source_detail=f"中枢心跳 | 第{heartbeat_count}次 | task={task_name}",
            content=content,
            content_type="heartbeat_reply",
            heartbeat_index=heartbeat_count,
            heartbeat_run_id=heartbeat_run_id,
            call_id=call_id,
            parent_event_id=parent_event_id,
            causation_id=causation_id,
        )

    def build_chatter_inner_monologue_event(
        self,
        thought: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
        mood: str = "",
        intent: str = "",
        topic: str = "",
    ) -> LifeEngineEvent:
        """构建由 life_chatter 记录的内心独白事件。"""
        seq = self._next_sequence()
        target_stream_id = str(stream_id or "").strip()
        platform_name = str(platform or "life_chatter").strip() or "life_chatter"
        chat_type_name = str(chat_type or "unknown").strip().lower() or "unknown"
        sender_display = str(sender_name or "当前对话器").strip() or "当前对话器"

        content_parts: list[str] = []
        if topic:
            content_parts.append(f"主题={topic}")
        if mood:
            content_parts.append(f"情绪={mood}")
        if intent:
            content_parts.append(f"意图={intent}")
        content_parts.append(f"独白={str(thought or '').strip()}")
        content = _shorten_text(" | ".join(part for part in content_parts if part), max_length=500)

        detail_parts = [
            platform_name,
            "对外对话",
            "内心独白记录",
        ]
        if target_stream_id:
            detail_parts.append(f"stream_id={target_stream_id}")

        return LifeEngineEvent(
            event_id=f"chatter_inner_monologue_{seq}",
            event_type=EventType.CONSCIOUS_ACTIVITY,
            timestamp=_now_iso(),
            sequence=seq,
            source="life_chatter",
            source_detail=" | ".join(detail_parts),
            content=content,
            content_type="chatter_inner_monologue",
            sender=sender_display,
            chat_type=chat_type_name,
            stream_id=target_stream_id or None,
        )

    def build_minecraft_consciousness_decision_event(
        self,
        decision: dict[str, Any],
        context_reference: dict[str, Any],
    ) -> LifeEngineEvent:
        """Build an attributed, idempotent scene decision before body action."""

        decision_id = str(decision.get("decision_id") or "").strip()
        if not decision_id:
            raise ValueError("Minecraft consciousness decision_id must not be empty")
        if decision.get("schema") not in {
            "minecraft.consciousness_decision.v1",
            "minecraft.consciousness_decision.v2",
        }:
            raise ValueError("unknown Minecraft consciousness decision schema")
        if (
            context_reference.get("schema")
            != "minecraft.consciousness_turn_reference.v1"
        ):
            raise ValueError("unknown Minecraft consciousness context schema")
        stream_id = str(context_reference.get("stream_id") or "").strip()
        instance_id = str(context_reference.get("instance_id") or "").strip()
        session_id = str(context_reference.get("session_id") or "").strip()
        if not stream_id or not instance_id or not session_id:
            raise ValueError(
                "Minecraft consciousness decision attribution is incomplete"
            )
        authored_at = str(decision.get("authored_at") or "").strip()
        if not authored_at:
            raise ValueError("Minecraft consciousness authored_at must not be empty")
        raw = json.dumps(
            {
                "decision": dict(decision),
                "context_reference": dict(context_reference),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        kind = str(decision.get("kind") or "").strip()
        intention = str(decision.get("intention") or "").strip()
        reason = str(decision.get("reason") or "").strip()
        visible = json.dumps(
            {
                "decision_id": decision_id,
                "kind": kind,
                "intention": intention,
                "speech": str(decision.get("speech") or ""),
                "task": decision.get("task"),
                "reason": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        seq = self._next_sequence()
        return LifeEngineEvent(
            event_id=decision_id,
            event_type=EventType.CONSCIOUS_ACTIVITY,
            timestamp=authored_at,
            sequence=seq,
            source="minecraft_consciousness",
            source_detail=(
                "Minecraft 场景意识 | "
                f"session={session_id} | instance={instance_id} | kind={kind}"
            ),
            content=_shorten_text(visible, max_length=1200),
            content_type="minecraft_consciousness_decision",
            stream_id=stream_id,
            occurrence_id=decision_id,
            source_instance_id=instance_id,
            correlation_id=session_id,
            content_ref=f"minecraft-consciousness-decision:{decision_id}",
            raw_content=raw,
        )

    def build_minecraft_body_event(
        self,
        body_event: dict[str, Any],
        context_reference: dict[str, Any],
    ) -> LifeEngineEvent:
        """Build one exact game occurrence from the authenticated body stream."""

        if body_event.get("schema") != "minecraft.body_event.v1":
            raise ValueError("unknown Minecraft body event schema")
        if context_reference.get("schema") != "minecraft.body_event_context.v1":
            raise ValueError("unknown Minecraft body event context schema")
        event_id = str(body_event.get("event_id") or "").strip()
        kind = str(body_event.get("kind") or "").strip()
        occurred_at = str(body_event.get("occurred_at") or "").strip()
        game_instance_id = str(body_event.get("instance_id") or "").strip()
        stream_id = str(context_reference.get("stream_id") or "").strip()
        instance_id = str(context_reference.get("instance_id") or "").strip()
        session_id = str(context_reference.get("session_id") or "").strip()
        if not event_id or not kind.startswith("minecraft.") or not occurred_at:
            raise ValueError("Minecraft body event identity is incomplete")
        if not game_instance_id or not stream_id or not instance_id or not session_id:
            raise ValueError("Minecraft body event attribution is incomplete")
        payload = body_event.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("Minecraft body event payload must be an object")
        raw = json.dumps(
            {
                "body_event": dict(body_event),
                "context_reference": dict(context_reference),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(raw.encode("utf-8")) > 24 * 1024:
            raise ValueError("Minecraft body event record exceeds its durable bound")

        is_inbound_chat = kind in {
            "minecraft.chat.received",
            "minecraft.whisper.received",
        }
        sender = str(payload.get("username") or "").strip()
        message = str(payload.get("message") or "").strip()
        if is_inbound_chat and not message:
            raise ValueError("Minecraft inbound chat event has no message")
        visible = (
            message
            if is_inbound_chat
            else json.dumps(
                {
                    "event_id": event_id,
                    "kind": kind,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        seq = self._next_sequence()
        return LifeEngineEvent(
            event_id=event_id,
            event_type=(
                EventType.MESSAGE if is_inbound_chat else EventType.CONSCIOUS_ACTIVITY
            ),
            timestamp=occurred_at,
            sequence=seq,
            source="minecraft",
            source_detail=(
                "Minecraft 身体事件 | "
                f"session={session_id} | body_instance={game_instance_id} | kind={kind}"
            ),
            content=_shorten_text(visible, max_length=1200),
            content_type=kind,
            sender=sender or None,
            sender_id=sender or None,
            chat_type="minecraft" if is_inbound_chat else None,
            stream_id=stream_id,
            occurrence_id=event_id,
            source_instance_id=instance_id,
            correlation_id=session_id,
            content_ref=f"minecraft-body-event:{event_id}",
            raw_content=raw,
        )

    def build_tool_call_event(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        heartbeat_run_id: str | None = None,
        call_id: str | None = None,
        parent_event_id: str | None = None,
        causation_id: str | None = None,
    ) -> LifeEngineEvent:
        """构建工具调用事件。"""
        seq = self._next_sequence()
        event_id = f"tool_call_{seq}"
        occurrence_id = f"life-tool-call:{event_id}"
        raw_content = _canonical_json_text(
            {
                "schema": "life.conscious_activity.tool_call.v1",
                "phase": "chosen",
                "tool_name": str(tool_name or ""),
                "arguments": dict(tool_args or {}),
                "heartbeat_run_id": str(heartbeat_run_id or ""),
                "call_id": str(call_id or ""),
                "parent_event_id": str(parent_event_id or ""),
                "causation_id": str(causation_id or ""),
            }
        )
        return LifeEngineEvent(
            event_id=event_id,
            event_type=EventType.TOOL_CALL,
            timestamp=_now_iso(),
            sequence=seq,
            source=INTERNAL_PLATFORM,
            source_detail=f"中枢工具调用 | {tool_name}",
            content=f"调用工具: {tool_name}",
            content_type="tool_call",
            heartbeat_run_id=heartbeat_run_id,
            call_id=call_id,
            parent_event_id=parent_event_id,
            causation_id=causation_id,
            tool_name=tool_name,
            tool_args=tool_args,
            occurrence_id=occurrence_id,
            content_ref=f"life-event-occurrence:{occurrence_id}",
            raw_content=raw_content,
        )

    def build_conscious_tool_call_event(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        *,
        activity_id: str,
        model_turn_activity_id: str,
        call_id: str,
        stream_id: str,
        source_instance_id: str,
        turn_occurrence_id: str,
        surface: str = "life_chatter",
    ) -> LifeEngineEvent:
        """Build one subject-authored tool choice for the shared life ledger."""

        identity = str(activity_id or "").strip()
        if not identity:
            raise ValueError("conscious activity_id must not be empty")
        event = self.build_tool_call_event(
            tool_name,
            tool_args,
            call_id=call_id,
            parent_event_id=f"{model_turn_activity_id}:generated",
            causation_id=f"{model_turn_activity_id}:generated",
        )
        occurrence_id = f"{identity}:chosen"
        event.event_id = occurrence_id
        surface_name = str(surface or "life_chatter").strip() or "life_chatter"
        event.source = surface_name
        event.source_detail = (
            "意识实例工具选择 | "
            f"surface={surface_name} | "
            f"instance={source_instance_id or 'unknown'} | "
            f"stream={stream_id or 'unknown'} | {tool_name}"
        )
        event.content_type = "conscious_activity_tool_call"
        event.stream_id = stream_id or None
        event.occurrence_id = occurrence_id
        event.source_instance_id = source_instance_id or None
        event.correlation_id = turn_occurrence_id or None
        event.content_ref = f"life-event-occurrence:{occurrence_id}"
        event.raw_content = _canonical_json_text(
            {
                "schema": "life.conscious_activity.tool_call.v1",
                "activity_id": identity,
                "model_turn_activity_id": model_turn_activity_id,
                "phase": "chosen",
                "surface": surface_name,
                "actor_consciousness_instance_id": source_instance_id,
                "stream_id": stream_id,
                "turn_occurrence_id": turn_occurrence_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": dict(tool_args or {}),
            }
        )
        return event

    def build_conscious_model_turn_event(
        self,
        *,
        activity_id: str,
        transport_request_id: str,
        stream_id: str,
        source_instance_id: str,
        turn_occurrence_id: str,
        provider_reasoning_content: str,
        assistant_message: str,
        tool_call_ids: list[str],
        surface: str = "life_chatter",
        heartbeat_run_id: str | None = None,
    ) -> LifeEngineEvent:
        """Build one complete successful model generation as conscious activity."""

        identity = str(activity_id or "").strip()
        if not identity:
            raise ValueError("model turn activity_id must not be empty")
        occurrence_id = f"{identity}:generated"
        surface_name = str(surface or "life_chatter").strip() or "life_chatter"
        raw_content = _canonical_json_text(
            {
                "schema": "life.conscious_activity.model_turn.v1",
                "activity_id": identity,
                "phase": "generated",
                "surface": surface_name,
                "actor_consciousness_instance_id": source_instance_id,
                "stream_id": stream_id,
                "turn_occurrence_id": turn_occurrence_id,
                "transport_request_id": transport_request_id,
                "provider_reasoning_content": str(
                    provider_reasoning_content or ""
                ),
                "assistant_message": str(assistant_message or ""),
                "tool_call_ids": [
                    str(value or "").strip()
                    for value in tool_call_ids
                    if str(value or "").strip()
                ],
            }
        )
        seq = self._next_sequence()
        visible = _canonical_json_text(
            {
                "phase": "generated",
                "has_provider_reasoning": bool(provider_reasoning_content),
                "has_assistant_message": bool(assistant_message),
                "tool_call_count": len(tool_call_ids),
            }
        )
        return LifeEngineEvent(
            event_id=occurrence_id,
            event_type=EventType.CONSCIOUS_ACTIVITY,
            timestamp=_now_iso(),
            sequence=seq,
            source=surface_name,
            source_detail=(
                "意识实例模型轮 | "
                f"surface={surface_name} | "
                f"instance={source_instance_id or 'unknown'} | "
                f"stream={stream_id or 'unknown'}"
            ),
            content=visible,
            content_type="conscious_activity_model_turn",
            heartbeat_run_id=heartbeat_run_id,
            stream_id=stream_id or None,
            occurrence_id=occurrence_id,
            causation_id=turn_occurrence_id or None,
            source_instance_id=source_instance_id or None,
            correlation_id=turn_occurrence_id or None,
            content_ref=f"life-event-occurrence:{occurrence_id}",
            raw_content=raw_content,
        )

    def build_conscious_activity_state_event(
        self,
        *,
        activity_id: str,
        stream_id: str,
        source_instance_id: str,
        occurrence_id: str,
        state_kind: str,
        payload: dict[str, Any],
        surface: str,
        causation_id: str = "",
        correlation_id: str = "",
    ) -> LifeEngineEvent:
        """Build a non-textual conscious state such as wait or interruption."""

        identity = str(activity_id or "").strip()
        state = str(state_kind or "").strip()
        occurrence = str(occurrence_id or "").strip()
        surface_name = str(surface or "").strip()
        if not identity or not state or not occurrence or not surface_name:
            raise ValueError("conscious activity state attribution is incomplete")
        event_occurrence = f"{identity}:state"
        return LifeEngineEvent(
            event_id=event_occurrence,
            event_type=EventType.CONSCIOUS_ACTIVITY,
            timestamp=_now_iso(),
            sequence=self._next_sequence(),
            source=surface_name,
            source_detail=(
                "意识实例状态活动 | "
                f"surface={surface_name} | "
                f"instance={source_instance_id or 'unknown'} | "
                f"state={state}"
            ),
            content=_canonical_json_text(
                {
                    "state_kind": state,
                    "payload_keys": sorted(str(key) for key in payload),
                }
            ),
            content_type="conscious_activity_state",
            stream_id=stream_id or None,
            occurrence_id=event_occurrence,
            causation_id=str(causation_id or occurrence),
            source_instance_id=source_instance_id or None,
            correlation_id=str(correlation_id or occurrence),
            content_ref=f"life-event-occurrence:{event_occurrence}",
            raw_content=_canonical_json_text(
                {
                    "schema": "life.conscious_activity.state.v1",
                    "activity_id": identity,
                    "surface": surface_name,
                    "actor_consciousness_instance_id": source_instance_id,
                    "stream_id": stream_id,
                    "occurrence_id": occurrence,
                    "state_kind": state,
                    "payload": dict(payload),
                }
            ),
        )

    def build_tool_result_event(
        self,
        tool_name: str,
        result: Any,
        success: bool,
        *,
        heartbeat_run_id: str | None = None,
        call_id: str | None = None,
        parent_event_id: str | None = None,
        causation_id: str | None = None,
        call_event: LifeEngineEvent | None = None,
    ) -> LifeEngineEvent:
        """构建工具结果事件，可直接关联对应的 call event。"""
        seq = self._next_sequence()
        linked_call_id = call_id or (call_event.call_id if call_event is not None else None)
        linked_parent_id = parent_event_id or (
            call_event.event_id if call_event is not None else None
        )
        result_text = (
            result if isinstance(result, str) else _canonical_json_text(result)
        )
        event_id = f"tool_result_{seq}"
        occurrence_id = f"life-tool-result:{event_id}"
        raw_content = _canonical_json_text(
            {
                "schema": "life.conscious_activity.tool_result.v1",
                "phase": "completed" if success else "failed",
                "tool_name": str(tool_name or ""),
                "result": result,
                "success": bool(success),
                "heartbeat_run_id": str(
                    heartbeat_run_id
                    or (
                        call_event.heartbeat_run_id
                        if call_event is not None
                        else ""
                    )
                    or ""
                ),
                "call_id": str(linked_call_id or ""),
                "parent_event_id": str(linked_parent_id or ""),
            }
        )
        return LifeEngineEvent(
            event_id=event_id,
            event_type=EventType.TOOL_RESULT,
            timestamp=_now_iso(),
            sequence=seq,
            source=INTERNAL_PLATFORM,
            source_detail=f"工具返回 | {tool_name} | {'成功' if success else '失败'}",
            content=_shorten_text(result_text, max_length=500),
            content_type="tool_result",
            heartbeat_run_id=heartbeat_run_id or (
                call_event.heartbeat_run_id if call_event is not None else None
            ),
            call_id=linked_call_id,
            parent_event_id=linked_parent_id,
            causation_id=causation_id or (
                call_event.event_id if call_event is not None else None
            ),
            tool_name=tool_name,
            tool_success=success,
            occurrence_id=occurrence_id,
            content_ref=f"life-event-occurrence:{occurrence_id}",
            raw_content=raw_content,
        )

    def build_conscious_tool_result_event(
        self,
        tool_name: str,
        result: Any,
        success: bool,
        *,
        activity_id: str,
        call_id: str,
        stream_id: str,
        source_instance_id: str,
        turn_occurrence_id: str,
        technical_outcome: str = "",
        delivery_receipt_sha256: str = "",
        delivery_message_id: str = "",
        delivery_proof_status: str = "",
        surface: str = "life_chatter",
    ) -> LifeEngineEvent:
        """Build the immutable outcome paired with a conscious tool choice."""

        identity = str(activity_id or "").strip()
        if not identity:
            raise ValueError("conscious activity_id must not be empty")
        parent_event_id = f"{identity}:chosen"
        event = self.build_tool_result_event(
            tool_name,
            result,
            success,
            call_id=call_id,
            parent_event_id=parent_event_id,
            causation_id=parent_event_id,
        )
        occurrence_id = f"{identity}:result"
        event.event_id = occurrence_id
        surface_name = str(surface or "life_chatter").strip() or "life_chatter"
        event.source = surface_name
        event.source_detail = (
            "意识实例工具结果 | "
            f"surface={surface_name} | "
            f"instance={source_instance_id or 'unknown'} | "
            f"stream={stream_id or 'unknown'} | {tool_name}"
        )
        event.content_type = "conscious_activity_tool_result"
        event.stream_id = stream_id or None
        event.occurrence_id = occurrence_id
        event.source_instance_id = source_instance_id or None
        event.correlation_id = turn_occurrence_id or None
        event.content_ref = f"life-event-occurrence:{occurrence_id}"
        event.raw_content = _canonical_json_text(
            {
                "schema": "life.conscious_activity.tool_result.v1",
                "activity_id": identity,
                "phase": "completed" if success else "failed",
                "surface": surface_name,
                "actor_consciousness_instance_id": source_instance_id,
                "stream_id": stream_id,
                "turn_occurrence_id": turn_occurrence_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "success": bool(success),
                "technical_outcome": str(technical_outcome or ""),
                "delivery_receipt_sha256": str(delivery_receipt_sha256 or ""),
                "delivery_message_id": str(delivery_message_id or ""),
                "delivery_proof_status": str(delivery_proof_status or ""),
                "result": result,
            }
        )
        return event

    def build_agent_result_event(
        self,
        agent_type: str,
        result_text: str,
        *,
        success: bool = True,
        rounds: int = 0,
        duration_ms: int = 0,
        heartbeat_run_id: str | None = None,
        call_id: str | None = None,
        parent_event_id: str | None = None,
        causation_id: str | None = None,
    ) -> LifeEngineEvent:
        """构建后台智能体执行结果事件。"""
        status = "成功" if success else "失败"
        seq = self._next_sequence()
        event_id = f"agent_result_{seq}"
        occurrence_id = f"life-agent-result:{event_id}"
        return LifeEngineEvent(
            event_id=event_id,
            event_type=EventType.AGENT_RESULT,
            timestamp=_now_iso(),
            sequence=seq,
            source=INTERNAL_PLATFORM,
            source_detail=f"后台智能体 | {agent_type} | {status} | {rounds}轮 | {duration_ms}ms",
            content=_shorten_text(result_text, max_length=500),
            content_type="agent_result",
            heartbeat_run_id=heartbeat_run_id,
            call_id=call_id,
            parent_event_id=parent_event_id,
            causation_id=causation_id,
            tool_name=f"agent:{agent_type}",
            tool_success=success,
            occurrence_id=occurrence_id,
            content_ref=f"life-event-occurrence:{occurrence_id}",
            raw_content=str(result_text or ""),
        )

    def build_direct_message_event(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> LifeEngineEvent:
        """构建用户通过命令直达生命中枢的留言事件。"""
        seq = self._next_sequence()
        platform_name = str(platform or "direct").strip() or "direct"
        chat_type_name = str(chat_type or "unknown").strip().lower() or "unknown"
        sender_display = str(sender_name or "外部用户").strip() or "外部用户"
        target_stream_id = str(stream_id or "").strip()
        source_detail_parts = [
            platform_name,
            "入站",
            "直连命令",
            "用户直达生命中枢",
        ]
        if target_stream_id:
            source_detail_parts.append(f"stream_id={target_stream_id}")

        return LifeEngineEvent(
            event_id=f"direct_msg_{seq}",
            event_type=EventType.MESSAGE,
            timestamp=_now_iso(),
            sequence=seq,
            source=platform_name,
            source_detail=" | ".join(source_detail_parts),
            content=_shorten_text(message, max_length=500),
            content_type="direct_message",
            sender=sender_display,
            chat_type=chat_type_name,
            stream_id=target_stream_id or None,
        )

    def build_proactive_opportunity_event(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> LifeEngineEvent:
        """构建 proactive 插件产生的主动机会事件。"""
        seq = self._next_sequence()
        target_stream_id = str(stream_id or "").strip()
        platform_name = str(platform or "proactive_message_plugin").strip() or "proactive_message_plugin"
        chat_type_name = str(chat_type or "unknown").strip().lower() or "unknown"
        sender_display = str(sender_name or "主动机会调度器").strip() or "主动机会调度器"
        detail_parts = [
            platform_name,
            "内部机会",
            "proactive",
            "交给当前对话器判断是否主动开口",
        ]
        if target_stream_id:
            detail_parts.append(f"stream_id={target_stream_id}")

        return LifeEngineEvent(
            event_id=f"proactive_opportunity_{seq}",
            event_type=EventType.MESSAGE,
            timestamp=_now_iso(),
            sequence=seq,
            source="proactive_message_plugin",
            source_detail=" | ".join(detail_parts),
            content=_shorten_text(str(message or "").strip(), max_length=500),
            content_type="proactive_opportunity",
            sender=sender_display,
            chat_type=chat_type_name,
            stream_id=target_stream_id or None,
        )

    def build_autonomy_intent_event(
        self,
        message: str,
        *,
        content_type: str,
        stream_id: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> LifeEngineEvent:
        """构建 life_engine 自主意向事件。"""
        seq = self._next_sequence()
        target_stream_id = str(stream_id or "").strip()
        content_type_name = str(content_type or "autonomy_intent").strip() or "autonomy_intent"
        detail_parts = [
            "life_engine_autonomy",
            "自主意向",
            content_type_name,
        ]
        if target_stream_id:
            detail_parts.append(f"stream_id={target_stream_id}")

        return LifeEngineEvent(
            event_id=f"autonomy_{seq}",
            event_type=EventType.MESSAGE,
            timestamp=_now_iso(),
            sequence=seq,
            source="life_engine_autonomy",
            source_detail=" | ".join(detail_parts),
            content=_shorten_text(str(message or "").strip(), max_length=700),
            content_type=content_type_name,
            sender=str(sender_name or "自主意向").strip() or "自主意向",
            chat_type=str(chat_type or "internal").strip() or "internal",
            stream_id=target_stream_id or None,
        )
