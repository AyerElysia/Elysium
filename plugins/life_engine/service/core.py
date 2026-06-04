"""life_engine 生命中枢服务核心模块。

生命中枢是同一个主体在不同运行模式间切换的骨架。
它通过周期性心跳来处理堆积的消息、进行内部思考，并为工具调用、
对外交流与状态沉淀提供基础能力。
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseService
from src.core.components.utils import should_strip_auto_reason_argument
from src.core.models.message import Message, MessageType
from src.kernel.concurrency import get_task_manager
from src.kernel.llm import LLMPayload, ROLE, Text, ToolRegistry, ToolResult
from src.kernel.scheduler import get_unified_scheduler, TriggerType

if TYPE_CHECKING:
    from ..dream.scheduler import DreamScheduler
    from ..memory.service import LifeMemoryService
    from ..neuromod.engine import InnerStateEngine
    from ..snn.bridge import SNNBridge
    from ..snn.core import DriveCoreNetwork

from .audit import (
    get_life_log_file,
    log_error,
    log_heartbeat as log_heartbeat_event,
    log_heartbeat_model_response,
    log_lifecycle,
    log_message_received,
    log_wake_context_injected,
)
from ..core.chat_history import (
    build_chat_history_text,
    build_global_chat_history_text_from_db,
    message_flag,
)
from ..core.config import LifeEngineConfig
from ..core.context_assembly import LifeChatterContextAssembler
from ..core.send_targets import format_send_targets_for_prompt, list_recent_send_targets
from ..core.tool_parallel import iter_life_tool_call_batches
from ..autonomy import (
    AutonomyIntent,
    AutonomyIntentStore,
    build_intent,
    cleanup_autonomy_schedules,
    format_due_message,
    restore_autonomy_intents,
    schedule_autonomy_intent as register_autonomy_schedule,
)
from ..streams.manager import ThoughtStreamManager
from ..drives.impulse import ImpulseEngine
from ..drives.rules import DEFAULT_RULES
from ..curiosity import CuriosityEngine
from ..constants import (
    HEARTBEAT_IDLE_CRITICAL_THRESHOLD,
    HEARTBEAT_IDLE_WARNING_THRESHOLD,
    LIFE_CHATTER_GLOBAL_CURSOR_KEY,
)
from ..memory.prompting import (
    build_memory_maintenance_prompt,
    load_memory_prompt_data,
    render_memory_prompt,
    should_emit_memory_maintenance_prompt,
)
from ..trace.store import LifeTraceRecord, LifeTraceStore
from .event_builder import (
    EventBuilder,
    EventType,
    LifeEngineEvent,
    LifeEngineState,
    _format_current_time,
    _format_time_display,
    _now_iso,
    _parse_hhmm,
    _shorten_text,
)
from .followup import FollowupState, PendingFollowup
from .state_manager import (
    StatePersistence,
    compress_history,
    clear_wake_context_reminder,
    get_file_metadata,
    minutes_since_time,
)
from .attention import AttentionRouter
from .event_bus import LifeEventBus, RawEventStore
from .integrations import (
    DFCIntegration,
    SNNIntegration,
    MemoryIntegration,
)
from .self_pause import (
    apply_self_pause,
    build_self_pause_status,
    clear_self_pause_state,
    self_pause_status,
)

if TYPE_CHECKING:
    from ..memory.service import LifeMemoryService


logger = get_logger("life_engine", display="life_engine")
_USER_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "USER.md"

class LifeEngineService(BaseService):
    """life_engine 心跳服务。

    这个版本使用统一的事件流模型，所有交互保持时间连续性。
    不参与正常聊天流程，不做回复决策。
    """

    service_name: str = "life_engine"
    service_description: str = "生命中枢服务，维持并行心跳与事件流上下文"
    version: str = "3.4.0"

    @classmethod
    def get_instance(cls) -> "LifeEngineService | None":
        """获取服务单例（供工具使用）。"""
        from .registry import get_life_engine_service

        return get_life_engine_service()

    def __init__(self, plugin) -> None:
        super().__init__(plugin)
        self._state = LifeEngineState()
        self._state_dirty: bool = False
        self._heartbeat_task_id: str | None = None
        self._stop_event: asyncio.Event | None = None
        self._pending_events: list[LifeEngineEvent] = []
        self._event_history: list[LifeEngineEvent] = []
        self._lock: asyncio.Lock | None = None
        self._sleep_state_active: bool = False
        self._memory_service: LifeMemoryService | None = None
        self._last_decay_date: str | None = None

        # SNN 皮层下系统
        self._snn_network: DriveCoreNetwork | None = None
        self._snn_bridge: SNNBridge | None = None
        self._snn_tick_task_id: str | None = None

        # 神经调质层
        self._inner_state: InnerStateEngine | None = None

        # 做梦系统
        self._dream_scheduler: DreamScheduler | None = None
        self._injected_dream_ids: set[str] = set()

        # 集成管理器
        self._dfc_integration: DFCIntegration | None = None
        self._snn_integration: SNNIntegration | None = None
        self._memory_integration: MemoryIntegration | None = None

        # 事件构建器
        self._event_builder = EventBuilder(self._next_sequence)

        # 思考流系统
        self._thought_manager: ThoughtStreamManager | None = None

        # 冲动引擎
        self._impulse_engine: ImpulseEngine | None = None

        # 异步好奇层
        self._curiosity_engine: CuriosityEngine | None = None
        self._curiosity_inflight: bool = False

        # 状态持久化
        self._state_persistence: StatePersistence | None = None
        self._event_bus: LifeEventBus | None = None
        self._attention_router: AttentionRouter | None = None
        self._legacy_config_warning_emitted: bool = False
        self._last_memory_maintenance_prompt_at: str | None = None
        self._followup_states: dict[str, FollowupState] = {}
        self._scheduler = None

    @property
    def memory_service(self) -> LifeMemoryService | None:
        """兼容旧调用方的公开记忆服务访问入口。"""
        return self._memory_service

    def _get_lock(self) -> asyncio.Lock:
        """获取懒加载锁。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_event_bus(self) -> LifeEventBus:
        if self._event_bus is None:
            self._event_bus = LifeEventBus(RawEventStore(self._workspace_dir()))
        return self._event_bus

    def _get_attention_router(self) -> AttentionRouter:
        if self._attention_router is None:
            self._attention_router = AttentionRouter(max_events=self._history_limit())
        return self._attention_router

    def _cfg(self) -> LifeEngineConfig:
        config = getattr(self.plugin, "config", None)
        if isinstance(config, LifeEngineConfig):
            if hasattr(config, "thresholds") and hasattr(config, "memory_algorithm"):
                return config
            if not self._legacy_config_warning_emitted:
                logger.warning(
                    "检测到旧版 LifeEngineConfig 对象（缺少 thresholds/memory_algorithm），"
                    "将自动迁移为最新配置结构；建议完整重启进程。"
                )
                self._legacy_config_warning_emitted = True
            migrated = self._migrate_legacy_config(config)
            if migrated is not None:
                return migrated
            return LifeEngineConfig()
        migrated = self._migrate_legacy_config(config)
        if migrated is not None:
            return migrated
        return LifeEngineConfig()

    def _migrate_legacy_config(self, config: object | None) -> LifeEngineConfig | None:
        """将旧版/异构配置对象迁移为当前 LifeEngineConfig。"""
        if config is None:
            return None
        dump_method = getattr(config, "model_dump", None)
        payload: dict[str, Any] | None = None
        if callable(dump_method):
            try:
                dumped = dump_method(mode="python")
                if isinstance(dumped, dict):
                    payload = dumped
            except TypeError:
                try:
                    dumped = dump_method()
                    if isinstance(dumped, dict):
                        payload = dumped
                except Exception:
                    payload = None
            except Exception:
                payload = None
        if payload is None:
            dict_method = getattr(config, "dict", None)
            if callable(dict_method):
                try:
                    dumped = dict_method()
                    if isinstance(dumped, dict):
                        payload = dumped
                except Exception:
                    payload = None
        if payload is None:
            return None
        try:
            migrated = LifeEngineConfig.model_validate(payload)
        except Exception:
            migrated = LifeEngineConfig()
        try:
            setattr(self.plugin, "config", migrated)
        except Exception:
            pass
        return migrated

    def _is_enabled(self) -> bool:
        """判断插件当前是否启用。"""
        cfg = self._cfg()
        return bool(cfg.settings.enabled)

    def _history_limit(self) -> int:
        """返回滚动事件流保留上限。"""
        cfg = self._cfg()
        return max(1, int(cfg.settings.context_history_max_events))

    def _sleep_window_config(self) -> tuple[dtime | None, dtime | None]:
        """返回配置的睡眠窗口（sleep, wake）。"""
        cfg = self._cfg()
        return _parse_hhmm(cfg.settings.sleep_time), _parse_hhmm(cfg.settings.wake_time)

    def _sleep_window_status(self) -> tuple[bool, str]:
        """返回睡眠窗口配置是否有效及说明。"""
        cfg = self._cfg()
        sleep_raw = (cfg.settings.sleep_time or "").strip()
        wake_raw = (cfg.settings.wake_time or "").strip()
        sleep_at, wake_at = self._sleep_window_config()
        if not sleep_raw and not wake_raw:
            return False, "disabled"
        if sleep_at is None or wake_at is None:
            return False, "invalid-format"
        if sleep_at == wake_at:
            return False, "invalid-equal"
        return True, f"{sleep_at.strftime('%H:%M')}~{wake_at.strftime('%H:%M')}"

    def _in_sleep_window_now(self) -> tuple[bool, str]:
        """判断当前是否处于睡眠窗口。"""
        sleep_at, wake_at = self._sleep_window_config()
        if sleep_at is None or wake_at is None:
            return False, "sleep-window-disabled"

        now = datetime.now().astimezone().time()
        now_hm = dtime(hour=now.hour, minute=now.minute, second=0, microsecond=0)

        if sleep_at == wake_at:
            return False, "sleep-window-invalid-equal"

        if sleep_at < wake_at:
            in_sleep = sleep_at <= now_hm < wake_at
        else:
            in_sleep = (now_hm >= sleep_at) or (now_hm < wake_at)

        return in_sleep, f"{sleep_at.strftime('%H:%M')}~{wake_at.strftime('%H:%M')}"

    def _next_sequence(self) -> int:
        """获取下一个事件序列号。"""
        self._state.event_sequence += 1
        return self._state.event_sequence

    def _minutes_since_external_message(self) -> int | None:
        """计算距离上一条外部消息过去了多少分钟。"""
        return minutes_since_time(self._state.last_external_message_at)

    def _minutes_since_tell_dfc(self) -> int | None:
        """计算距离上一次传话给 DFC 过去了多少分钟。"""
        return minutes_since_time(self._state.last_tell_dfc_at)

    def _minutes_since_outer_sync(self) -> int | None:
        """计算距离上一次同步给对外运行模式过去了多少分钟。"""
        return self._minutes_since_tell_dfc()

    def _self_pause_status(self) -> tuple[bool, int | None, str | None, str | None]:
        """返回主动休息锁状态。"""
        return self_pause_status(self._state)

    def get_self_pause_status(self) -> dict[str, Any]:
        """返回主动休息锁状态，供工具、监控面板或调试命令复用。"""
        return build_self_pause_status(self._state, self._self_pause_status())

    def _clear_self_pause_state(self) -> bool:
        """清除主动休息锁。调用方负责持锁和保存。"""
        return clear_self_pause_state(self._state)

    async def clear_self_pause(self, *, source: str = "manual") -> bool:
        """清除主动休息锁。"""
        async with self._get_lock():
            changed = self._clear_self_pause_state()
        if changed:
            await self._save_runtime_context()
            logger.info(f"life_engine 主动休息锁已解除: source={source}")
        return changed

    async def request_self_pause(
        self,
        *,
        duration_minutes: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """设置主动休息锁，让 LLM 心跳暂停一段时间。"""
        async with self._get_lock():
            payload = apply_self_pause(
                self._state,
                duration_minutes=duration_minutes,
                reason=reason,
            )

        await self._save_runtime_context()
        logger.info(
            "life_engine 进入主动休息: "
            f"duration={payload['duration_minutes']}min requested={payload['requested_minutes']}min "
            f"until={payload['paused_until']} reason={payload['reason'] or '-'}"
        )

        return payload

    def record_tell_dfc(self) -> None:
        """记录一次传话给 DFC 的时间。"""
        self._state.last_tell_dfc_at = _now_iso()
        self._state.tell_dfc_count += 1

    def record_outer_sync(self) -> None:
        """记录一次同步给对外运行模式的时间。"""
        self.record_tell_dfc()

    def _workspace_dir(self) -> Path:
        """返回 life workspace 目录。"""
        workspace = Path(self._cfg().settings.workspace_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _ensure_workspace_templates(self) -> None:
        """补齐可由运行态长期维护的工作空间模板文件。"""

        workspace = self._workspace_dir()
        user_file = workspace / "USER.md"
        if user_file.exists():
            return
        try:
            template = _USER_TEMPLATE_PATH.read_text(encoding="utf-8")
            user_file.write_text(template, encoding="utf-8")
            logger.info(f"已创建 USER.md 使用说明模板: {user_file}")
        except Exception as e:
            logger.warning(f"创建 USER.md 使用说明模板失败: {e}")

    def snapshot(self) -> dict[str, Any]:
        """返回当前状态快照。"""
        data = asdict(self._state)
        in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
        data["heartbeat_interval_seconds"] = int(self._cfg().settings.heartbeat_interval_seconds)
        data["external_silence_minutes"] = self._minutes_since_external_message()
        self_paused, self_pause_remaining, self_pause_until, self_pause_reason = self._self_pause_status()
        data["llm_heartbeat_paused_by_self"] = self_paused
        data["self_pause_remaining_minutes"] = self_pause_remaining
        data["self_pause_until"] = self_pause_until
        data["self_pause_reason"] = self_pause_reason
        data["self_pause_duration_minutes"] = self._state.self_pause_duration_minutes
        data["model_task_name"] = self._cfg().model.task_name
        data["pending_event_count"] = len(self._pending_events)
        data["history_event_count"] = len(self._event_history)
        data["context_history_max_events"] = self._history_limit()
        data["workspace_path"] = self._cfg().settings.workspace_path
        data["sleep_time"] = self._cfg().settings.sleep_time
        data["wake_time"] = self._cfg().settings.wake_time
        data["in_sleep_window"] = in_sleep_window
        data["sleep_window"] = sleep_window_desc
        data["log_file_path"] = str(get_life_log_file())
        data["snn_enabled"] = self._cfg().snn.enabled
        if self._snn_network is not None:
            data["snn_health"] = self._snn_network.get_health()
        neuromod_cfg = getattr(self._cfg(), "neuromod", None)
        data["neuromod_enabled"] = neuromod_cfg.enabled if neuromod_cfg else False
        if self._inner_state is not None:
            data["neuromod_state"] = self._inner_state.get_full_state()
        return data

    @staticmethod
    def _message_time_display(message: Message) -> tuple[str, str]:
        """返回消息时间的 ISO 与简洁显示。"""
        raw_time = getattr(message, "time", None)
        try:
            if raw_time is None:
                raise ValueError("missing time")
            iso_time = datetime.fromtimestamp(float(raw_time), tz=timezone.utc).astimezone().isoformat()
        except Exception:
            iso_time = _now_iso()
        return iso_time, _format_time_display(iso_time)

    @staticmethod
    def _format_message_text(message: Message, *, max_length: int = 240) -> str:
        """格式化消息正文。"""
        raw_text = getattr(message, "processed_plain_text", None)
        if raw_text is None:
            raw_text = getattr(message, "content", "")
        return _shorten_text(str(raw_text or "").strip() or "（空消息）", max_length=max_length)

    @staticmethod
    def _message_sender_label(message: Message) -> str:
        """格式化消息发送者标签。"""
        return str(
            getattr(message, "sender_cardname", None)
            or getattr(message, "sender_name", None)
            or getattr(message, "sender_id", None)
            or "未知发送者"
        )

    @staticmethod
    def _serialize_life_event(event: LifeEngineEvent) -> dict[str, Any]:
        """将 life 事件转换为可视化数据。"""
        return {
            "scope": "life",
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp,
            "time_display": _format_time_display(event.timestamp),
            "sequence": event.sequence,
            "source": event.source,
            "source_detail": event.source_detail,
            "content": _shorten_text(event.content or "", max_length=240),
            "content_full": event.content or "",
            "content_type": event.content_type,
            "sender": event.sender,
            "chat_type": event.chat_type,
            "stream_id": event.stream_id,
            "heartbeat_index": event.heartbeat_index,
            "tool_name": event.tool_name,
            "tool_args": event.tool_args or {},
            "tool_success": event.tool_success,
        }

    async def _publish_raw_events(self, events: list[LifeEngineEvent]) -> None:
        """Mirror legacy service events into the unified raw event log."""
        if not events:
            return
        try:
            await self._get_event_bus().publish_legacy_events(events)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"life_engine raw event 写入失败（已跳过）: {exc}", exc_info=True)

    async def _queue_pending_event(self, event: LifeEngineEvent) -> None:
        """Append an event to the compatibility pending queue and raw bus."""
        async with self._get_lock():
            self._pending_events.append(event)
            self._state.pending_event_count = len(self._pending_events)
        await self._publish_raw_events([event])
        await self._save_runtime_context()

    def _serialize_stream_message(
        self,
        message: Message,
        *,
        stream_name: str,
        source: str,
    ) -> dict[str, Any]:
        """将聊天流消息转换为可视化数据。"""
        iso_time, time_display = self._message_time_display(message)
        sender_role = str(getattr(message, "sender_role", "") or "").lower()
        direction = "sent" if sender_role == "bot" else "received"
        return {
            "scope": "chatter",
            "stream_id": str(getattr(message, "stream_id", "") or ""),
            "stream_name": stream_name,
            "platform": str(getattr(message, "platform", "") or ""),
            "chat_type": str(getattr(message, "chat_type", "") or ""),
            "message_id": str(getattr(message, "message_id", "") or ""),
            "time": iso_time,
            "time_display": time_display,
            "direction": direction,
            "sender_role": sender_role or None,
            "sender_name": self._message_sender_label(message),
            "content": self._format_message_text(message),
            "content_full": str(
                getattr(message, "processed_plain_text", None)
                or getattr(message, "content", "")
                or ""
            ),
            "reply_to": getattr(message, "reply_to", None),
            "source": source,
            "is_inner_monologue": bool(
                getattr(message, "is_inner_monologue", False)
                or (getattr(message, "extra", {}) or {}).get("is_inner_monologue", False)
            ),
            "is_proactive_followup_trigger": bool(
                getattr(message, "is_proactive_followup_trigger", False)
                or (getattr(message, "extra", {}) or {}).get("is_proactive_followup_trigger", False)
            ),
        }

    async def get_message_observability_snapshot(
        self,
        *,
        event_limit: int = 24,
        stream_limit: int = 12,
        message_limit: int = 8,
    ) -> dict[str, Any]:
        """返回 life 与 chatter 的联合消息观测快照。"""
        async with self._get_lock():
            pending_events = list(self._pending_events)
            event_history = list(self._event_history)
            state_snapshot = asdict(self._state)
            inner_state = self._inner_state

        life_events = [
            self._serialize_life_event(event)
            for event in (event_history[-max(1, event_limit) :] if event_limit > 0 else event_history)
        ]
        pending_life_events = [
            self._serialize_life_event(event)
            for event in (pending_events[-max(1, min(event_limit, len(pending_events))) :] if pending_events else [])
        ]

        life_latest_event = life_events[-1] if life_events else (pending_life_events[-1] if pending_life_events else None)

        try:
            from src.core.managers import get_stream_manager

            stream_manager = get_stream_manager()
            stream_items = list(getattr(stream_manager, "_streams", {}).values())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取聊天流快照失败: {exc}")
            stream_items = []

        stream_snapshots: list[dict[str, Any]] = []
        for stream in stream_items:
            stream_id = str(getattr(stream, "stream_id", "") or "")
            if not stream_id:
                continue
            context = getattr(stream, "context", None)
            if context is None:
                continue

            history_messages = list(getattr(context, "history_messages", []) or [])
            unread_messages = list(getattr(context, "unread_messages", []) or [])
            current_message = getattr(context, "current_message", None)

            candidate_messages = history_messages[-max(1, message_limit) :]
            recent_messages = [
                self._serialize_stream_message(
                    msg,
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="history",
                )
                for msg in candidate_messages
            ]

            latest_message = None
            if unread_messages:
                latest_message = self._serialize_stream_message(
                    unread_messages[-1],
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="unread",
                )
            elif history_messages:
                latest_message = self._serialize_stream_message(
                    history_messages[-1],
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="history",
                )
            elif current_message is not None:
                latest_message = self._serialize_stream_message(
                    current_message,
                    stream_name=str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    source="current",
                )

            last_message_time = getattr(context, "last_message_time", None)
            last_active_time = getattr(stream, "last_active_time", None)
            stream_snapshots.append(
                {
                    "stream_id": stream_id,
                    "stream_name": str(getattr(stream, "stream_name", "") or stream_id[:8] or "unknown"),
                    "platform": str(getattr(stream, "platform", "") or ""),
                    "chat_type": str(getattr(stream, "chat_type", "") or ""),
                    "bot_nickname": str(getattr(stream, "bot_nickname", "") or ""),
                    "is_active": bool(getattr(stream, "is_active", True)),
                    "is_chatter_processing": bool(getattr(context, "is_chatter_processing", False)),
                    "last_active_time": last_active_time,
                    "last_message_time": last_message_time,
                    "unread_count": len(unread_messages),
                    "history_count": len(history_messages),
                    "latest_message": latest_message,
                    "recent_messages": recent_messages,
                    "sort_ts": float(last_active_time or last_message_time or 0.0),
                }
            )

        stream_snapshots.sort(key=lambda item: item["sort_ts"], reverse=True)
        stream_snapshots = stream_snapshots[: max(1, stream_limit)]

        inner_state_snapshot: dict[str, Any] | None = None
        if inner_state is not None:
            try:
                inner_state_snapshot = inner_state.get_full_state()
            except Exception:
                try:
                    inner_state_snapshot = asdict(inner_state)  # type: ignore[arg-type]
                except Exception:
                    inner_state_snapshot = {"status": "unavailable"}

        return {
            "generated_at": _now_iso(),
            "life": {
                "state": state_snapshot,
                "inner_state": inner_state_snapshot,
                "pending_events": pending_life_events,
                "recent_events": life_events,
                "latest_event": life_latest_event,
            },
            "streams": stream_snapshots,
            "summary": {
                "active_stream_count": len(stream_snapshots),
                "pending_life_events": len(pending_events),
                "recent_life_events": len(life_events),
                "heartbeat_count": int(state_snapshot.get("heartbeat_count", 0) or 0),
                "last_model_reply": state_snapshot.get("last_model_reply"),
            },
        }

    def health(self) -> dict[str, Any]:
        """返回一个轻量健康信息。"""
        return self.snapshot()

    async def get_state_digest_for_dfc(self) -> str:
        """生成给 DFC 的状态摘要。"""
        if self._dfc_integration is None:
            self._dfc_integration = DFCIntegration(self)
        return await self._dfc_integration.get_state_digest()

    async def get_state_digest_for_outer_mode(self) -> str:
        """生成给对外运行模式的状态摘要。"""
        return await self.get_state_digest_for_dfc()

    async def query_actor_context(self, query: str) -> str:
        """供 DFC 同步查询当前状态、TODO 与最近日记。"""
        del query
        if self._dfc_integration is None:
            self._dfc_integration = DFCIntegration(self)
        return await self._dfc_integration.query_actor_context()

    async def query_outer_context(self, query: str) -> str:
        """供对外运行模式同步查询当前状态、TODO 与最近日记。"""
        return await self.query_actor_context(query)

    async def search_actor_memory(self, query: str, top_k: int = 5) -> str:
        """供 DFC 深度检索 life memory。"""
        query_text = str(query or "").strip()
        if not query_text:
            return ""

        memory_service = self._memory_service
        if memory_service is None:
            return "记忆系统暂不可用"

        results = await memory_service.search_memory(query_text, top_k=max(1, int(top_k)))
        if not results:
            logger.info(
                f"[search_actor_memory] 记忆检索无结果:\n"
                f"  query: {query_text}\n  top_k: {top_k}"
            )
            return ""

        try:
            bundles = await memory_service.build_memory_bundles(
                query=query_text,
                results=results,
                top_k=max(1, int(top_k)),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[search_actor_memory] 构建可追溯记忆包失败，将使用普通摘要: {exc}")
            bundles = []

        if bundles:
            workspace = self._workspace_dir()
            bundle_lines: list[str] = []
            for bundle in bundles[: max(1, int(top_k))]:
                file_meta = get_file_metadata(workspace / bundle.primary_path)
                meta_str = f"{file_meta['ext']} | {file_meta['time_ago']} | {file_meta['size']}"

                evidence_lines: list[str] = []
                for item in bundle.evidence[:4]:
                    label = "当前文件" if item.file_path == bundle.primary_path else "历史证据"
                    if item.relation:
                        label += f"/{item.relation}"
                    exists_note = "" if item.exists else "（当前路径不存在，仅作历史轨迹）"
                    snippet = _shorten_text(" ".join((item.snippet or "").split()), max_length=160)
                    evidence_lines.append(
                        f"  - {label}: {item.title or Path(item.file_path).name} "
                        f"[{item.file_path}]{exists_note}\n"
                        f"    摘要：{snippet or '无摘要'}"
                    )

                trace_lines: list[str] = []
                for trace in bundle.history_trace[:4]:
                    direction = "后来" if trace.direction == "later" else "早期"
                    reason = _shorten_text(" ".join((trace.reason or "").split()), max_length=120)
                    trace_lines.append(
                        f"  - {direction}/{trace.relation}: [{trace.file_path}]"
                        + (f" - {reason}" if reason else "")
                    )

                correction_lines = [
                    f"  - {item.source}: {_shorten_text(' '.join(item.message.split()), max_length=180)}"
                    for item in bundle.corrections[:3]
                ]

                line_parts = [
                    f"- 主要文件：{bundle.primary_path} ({meta_str})",
                    f"  当前理解：{_shorten_text(bundle.current_understanding, max_length=260)}",
                ]
                if evidence_lines:
                    line_parts.append("  证据：\n" + "\n".join(evidence_lines))
                if trace_lines:
                    line_parts.append("  演化轨迹：\n" + "\n".join(trace_lines))
                if correction_lines:
                    line_parts.append("  显式修正：\n" + "\n".join(correction_lines))
                if bundle.uncertainty:
                    line_parts.append(
                        f"  注意：{_shorten_text(bundle.uncertainty, max_length=220)}"
                    )
                bundle_lines.append("\n".join(line_parts))

            footer = "\n\n提示：以上是可追溯记忆包；旧记忆作为历史证据保留，当前理解优先参考后续整理和显式修正。如需查看完整内容，可使用 fetch_life_memory 工具读取文件。"
            final_result = "【可追溯记忆包】\n" + "\n\n".join(bundle_lines) + footer

            logger.info(
                f"[search_actor_memory] 可追溯记忆检索完成:\n"
                f"  query: {query_text}\n  top_k: {top_k}\n"
                f"  bundles: {len(bundles)}"
            )
            return final_result

        workspace = self._workspace_dir()
        direct_lines: list[str] = []
        associated_lines: list[str] = []

        for result in results:
            title = result.title or Path(result.file_path).name or result.file_path
            snippet = _shorten_text(" ".join((result.snippet or "").split()), max_length=250)
            file_meta = get_file_metadata(workspace / result.file_path)
            meta_str = f"{file_meta['ext']} | {file_meta['time_ago']} | {file_meta['size']}"

            line = (
                f"- {title} [{result.file_path}] "
                f"(相关度 {result.relevance:.2f} | {meta_str})\n"
                f"  摘要：{snippet or '无摘要'}"
            )

            if result.source == "associated":
                reason = _shorten_text(
                    " ".join((result.association_reason or "").split()),
                    max_length=150,
                )
                path_str = " → ".join(result.association_path[-3:]) if result.association_path else ""
                if reason or path_str:
                    line += f"\n  联想：{reason or path_str}"
                associated_lines.append(line)
            else:
                direct_lines.append(line)

            if len(direct_lines) >= top_k and len(associated_lines) >= top_k:
                break

        parts: list[str] = []
        if direct_lines:
            parts.append(
                f"【直接命中的记忆】({len(direct_lines[:top_k])}条)\n" +
                "\n\n".join(direct_lines[:top_k])
            )
        if associated_lines:
            parts.append(
                f"【联想扩散结果】({len(associated_lines[:top_k])}条)\n" +
                "\n\n".join(associated_lines[:top_k])
            )

        footer = "\n\n💡 提示：以上仅为摘要。如需查看完整内容，可使用 fetch_life_memory 工具读取文件。"
        final_result = "\n\n".join(parts) + footer

        logger.info(
            f"[search_actor_memory] 记忆检索完成:\n"
            f"  query: {query_text}\n  top_k: {top_k}\n"
            f"  直接命中: {len(direct_lines)} 条\n  联想结果: {len(associated_lines)} 条"
        )

        return final_result

    async def record_message(self, message: Message, direction: str = "received") -> None:
        """记录一条来自聊天流的消息事件。"""
        if not self._is_enabled():
            return

        if direction not in {"received", "sent"}:
            direction = "received"

        event = self._event_builder.build_message_event(message, direction=direction)
        unlocked_self_pause = False
        async with self._get_lock():
            self._pending_events.append(event)
            self._state.pending_event_count = len(self._pending_events)
            if direction == "received":
                self._state.last_external_message_at = event.timestamp
                unlocked_self_pause = self._clear_self_pause_state()

                # 收到外界新消息时，重置并清除对应流的延迟续话状态
                stream_id = getattr(message, "stream_id", "")
                if stream_id and stream_id in self._followup_states:
                    state = self._followup_states[stream_id]
                    state.followup_chain_count = 0
                    state.followup_cooldown_until = None
                    if state.scheduler_task_name and self._scheduler:
                        try:
                            await self._scheduler.cancel_schedule(state.scheduler_task_name)
                        except Exception:
                            pass
                    state.pending_followup = None
                    state.is_waiting = False
                    state.active_check_kind = None
        await self._publish_raw_events([event])
        await self._save_runtime_context()
        if direction == "received":
            self._schedule_curiosity_review(message, event)
        if unlocked_self_pause:
            logger.info(
                "life_engine 收到外界消息，主动休息锁已解除: "
                f"stream_id={event.stream_id or ''} sender={event.sender or 'unknown'}"
            )

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "unknown",
            sender_id=event.sender or "",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction=direction,
            pending_message_count=self._state.pending_event_count,
        )

    def _get_curiosity_engine(self) -> CuriosityEngine:
        cfg = self._cfg()
        curiosity_cfg = getattr(cfg, "curiosity", None)
        task_name = (
            str(getattr(curiosity_cfg, "task_name", "") or "").strip()
            or str(getattr(cfg.model, "task_name", "") or "").strip()
            or "life"
        )
        timeout = float(getattr(curiosity_cfg, "timeout_seconds", 30.0) or 30.0)
        workspace = str(getattr(cfg.settings, "workspace_path", "") or self._workspace_dir())
        if (
            self._curiosity_engine is None
            or self._curiosity_engine.workspace_path != workspace
            or self._curiosity_engine.model_task_name != task_name
        ):
            self._curiosity_engine = CuriosityEngine(
                workspace_path=workspace,
                model_task_name=task_name,
                timeout_seconds=timeout,
            )
        return self._curiosity_engine

    def _schedule_curiosity_review(self, message: Message, event: LifeEngineEvent) -> None:
        cfg = self._cfg()
        curiosity_cfg = getattr(cfg, "curiosity", None)
        if curiosity_cfg is not None and not bool(getattr(curiosity_cfg, "enabled", True)):
            return
        if self._curiosity_inflight:
            logger.debug("好奇异步判断仍在运行，跳过本次重复调度")
            return

        self._curiosity_inflight = True
        get_task_manager().create_task(
            self._run_curiosity_review(message, event),
            name=f"life_curiosity_{event.sequence}",
            daemon=True,
            timeout=float(getattr(curiosity_cfg, "timeout_seconds", 30.0) or 30.0) + 5.0,
        )

    async def _run_curiosity_review(self, message: Message, event: LifeEngineEvent) -> None:
        try:
            cfg = self._cfg()
            curiosity_cfg = getattr(cfg, "curiosity", None)
            max_history = (
                int(getattr(curiosity_cfg, "history_messages", 20) or 20)
                if curiosity_cfg is not None
                else 20
            )
            prefix_prompt = self._build_curiosity_prefix_prompt()
            history_text = await self._build_curiosity_history_text(message, max_messages=max_history)
            signal = await self._get_curiosity_engine().review(
                prefix_prompt=prefix_prompt,
                history_text=history_text,
                new_event_text=self._format_curiosity_event(event),
                source_event_id=event.event_id,
                source_stream_id=event.stream_id or "",
            )
            if signal.active:
                logger.info(f"好奇牵引已更新: {signal.anchor or signal.unknown}")
            else:
                logger.debug("好奇异步判断：暂无值得保留的刺点")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"好奇异步判断失败: {exc}")
        finally:
            self._curiosity_inflight = False

    def _build_curiosity_prefix_prompt(self) -> str:
        workspace = self._workspace_dir()
        soul_text = self._read_workspace_text(workspace, "SOUL.md")
        user_text = self._read_workspace_text(workspace, "USER.md")
        memory_text = ""
        try:
            memory_data = load_memory_prompt_data(workspace)
            memory_text = render_memory_prompt(memory_data, mode="chat") if memory_data.raw_text else ""
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"好奇层读取 MEMORY.md 失败: {exc}")

        return LifeChatterContextAssembler.build_prefix_prompt(
            soul_text=soul_text,
            user_text=user_text,
            memory_text=memory_text,
            tools_text="",
            live_guidance="",
            primary_tool_guide="",
        )

    @staticmethod
    def _read_workspace_text(workspace: str, filename: str) -> str:
        try:
            path = Path(workspace) / filename
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
        return ""

    async def _build_curiosity_history_text(self, message: Message, *, max_messages: int) -> str:
        if max_messages <= 0:
            return ""
        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        chat_stream = None
        if stream_id:
            try:
                from src.core.managers import get_stream_manager

                chat_stream = get_stream_manager()._streams.get(stream_id)
            except Exception:
                chat_stream = None
        if chat_stream is not None:
            text = await build_global_chat_history_text_from_db(
                chat_stream,
                max_messages=max_messages,
                include_stream_label=True,
                exclude_message_ids={
                    str(getattr(message, "message_id", "") or "").strip()
                } - {""},
            )
            if text:
                return text

        async with self._get_lock():
            message_events = [
                item for item in [*self._event_history, *self._pending_events]
                if item.event_type == EventType.MESSAGE
            ][-max_messages:]
        return "\n".join(self._format_curiosity_event(item) for item in message_events)

    @staticmethod
    def _format_curiosity_event(event: LifeEngineEvent) -> str:
        sender = event.sender or "未知"
        stream = (event.stream_id or "")[:8]
        source = event.source_detail or event.source or "unknown"
        return (
            f"[{_format_time_display(event.timestamp)}] "
            f"{source} stream={stream} sender={sender}: "
            f"{_shorten_text(event.content, max_length=500)}"
        )

    def get_or_create_followup_state(self, stream_id: str) -> FollowupState:
        if stream_id not in self._followup_states:
            self._followup_states[stream_id] = FollowupState(stream_id=stream_id)
        return self._followup_states[stream_id]

    async def schedule_followup_for_stream(
        self,
        chat_stream: Any,
        *,
        delay_seconds: float,
        thought: str,
        topic: str,
        followup_type: str,
        source: str,
    ) -> tuple[bool, str]:
        """为某个聊天流登记一次延迟续话。"""
        stream_id = getattr(chat_stream, "stream_id", "")
        if not stream_id:
            return False, "缺少 stream_id"

        state = self.get_or_create_followup_state(stream_id)

        # 检查冷却
        if state.followup_cooldown_until and datetime.now() < state.followup_cooldown_until:
            return False, "当前仍处于延迟续话冷却期"

        # 延迟续话链已达上限
        max_chain = 2
        if state.followup_chain_count >= max_chain:
            return False, "延迟续话链已达上限"

        min_delay = 20.0
        max_delay = 90.0
        delay_seconds = max(float(delay_seconds or 0), min_delay)
        delay_seconds = min(delay_seconds, max_delay)

        next_check_time = datetime.now() + timedelta(seconds=delay_seconds)

        followup = PendingFollowup(
            topic=str(topic or "未命名话题").strip() or "未命名话题",
            thought=str(thought or "").strip(),
            followup_type=str(followup_type or "share_new_thought").strip() or "share_new_thought",
            delay_seconds=delay_seconds,
            scheduled_at=datetime.now(),
            check_at=next_check_time,
            source=source,
        )

        state.pending_followup = followup
        state.next_check_time = next_check_time
        state.is_waiting = True
        state.active_check_kind = "followup"

        task_name = f"life_followup_check_{stream_id}"
        state.scheduler_task_name = task_name

        if self._scheduler is None:
            self._scheduler = get_unified_scheduler()

        async def _timeout_callback() -> None:
            await self._on_followup_timeout(stream_id)

        try:
            await self._scheduler.create_schedule(
                callback=_timeout_callback,
                trigger_type=TriggerType.TIME,
                trigger_config={"trigger_at": next_check_time},
                task_name=task_name,
                force_overwrite=True,
            )
            logger.info(f"[{stream_id[:8]}] 已登记延迟续话，会在 {delay_seconds:.0f} 秒后重新判断。")
            return True, f"已登记一条延迟续话，会在 {delay_seconds:.0f} 秒后重新判断。"
        except Exception as e:
            logger.error(f"调度续话检查任务失败：{e}")
            return False, f"调度续话检查任务失败：{e}"

    async def _on_followup_timeout(self, stream_id: str) -> None:
        """延迟续话到点后执行判断。"""
        state = self._followup_states.get(stream_id)
        if (
            state is None
            or not state.is_waiting
            or state.active_check_kind != "followup"
            or state.pending_followup is None
        ):
            logger.debug(f"[{stream_id[:8]}] 跳过过期的延迟续话任务")
            return

        followup = state.pending_followup
        state.pending_followup = None
        state.is_waiting = False
        state.active_check_kind = None

        try:
            from src.app.plugin_system.api.stream_api import get_stream
            from src.core.managers.stream_manager import get_stream_manager

            chat_stream = await get_stream(stream_id)
            if chat_stream is None:
                chat_stream = get_stream_manager()._streams.get(stream_id)
            if chat_stream is None:
                logger.warning(f"[{stream_id[:8]}] 延迟续话未找到 chat_stream")
                return

            max_chain = 2
            if state.followup_chain_count >= max_chain:
                logger.info(f"[{stream_id[:8]}] 延迟续话链已达上限，结束本轮续话")
                state.followup_cooldown_until = datetime.now() + timedelta(minutes=10)
                state.followup_chain_count = 0
                return

            state.followup_chain_count += 1
            await self._wake_stream_for_followup(chat_stream, followup)
        except Exception as exc:
            logger.error(f"[{stream_id[:8]}] 延迟续话处理失败：{exc}", exc_info=True)
            state.followup_cooldown_until = datetime.now() + timedelta(minutes=10)
            state.followup_chain_count = 0

    async def _wake_stream_for_followup(self, chat_stream: Any, followup: PendingFollowup) -> None:
        """向目标流注入一条续话机会触发消息，并唤醒当前运行模式。"""
        from src.core.models.message import Message
        from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        import time
        import uuid

        stream_id = chat_stream.stream_id
        context = chat_stream.context
        target_user_id, target_user_name = self._resolve_followup_target(chat_stream)

        # 从聊天历史提取最近一次 bot 消息
        history = list(getattr(chat_stream.context, "history_messages", []) or [])
        last_bot_message = ""
        for msg in reversed(history):
            if str(getattr(msg, "sender_role", "") or "").lower() == "bot":
                last_bot_message = str(getattr(msg, "content", "") or "")
                break

        elapsed_seconds = followup.delay_seconds

        prompt = (
            "[延迟续话机会] 这不是用户的新消息，而是一次由系统交给你的主动续话机会。"
            "你必须先调用 action-record_inner_monologue，记录你此刻新的心理推进；"
            "然后再二选一：如果你觉得刚才的话头还值得延续，就像平时一样使用当前对话器的动作自己回复；"
            "如果觉得现在不该继续说，可以 pass_and_wait。"
            f"\n- 当前执行者：{chat_stream.bot_nickname or '你'}"
            f"\n- 距离你上一条显式消息已过去约 {elapsed_seconds:.0f} 秒"
            f"\n- 你刚刚对对方说的是：{last_bot_message or '（上一条消息为空）'}"
            f"\n- 你当时留下的未尽之意：{followup.thought or '（未填写）'}"
            f"\n- 续话主题：{followup.topic}"
            f"\n- 续话类型：{followup.followup_type}"
            "\n- 重要：不要机械续话，不要为了说而说；如果不自然，就先停住。"
        )

        trigger_message = Message(
            message_id=f"proactive_followup_{uuid.uuid4().hex[:12]}",
            platform=chat_stream.platform or "unknown",
            stream_id=stream_id,
            sender_id=target_user_id or "system",
            sender_name="系统（续话触发）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            time=time.time(),
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            is_proactive_followup_trigger=True,
            proactive_followup_topic=followup.topic,
            proactive_followup_type=followup.followup_type,
        )
        context.add_unread_message(trigger_message)
        loop_mgr = get_stream_loop_manager()
        removed = loop_mgr._wait_states.pop(stream_id, None)
        if removed:
            logger.debug(f"[{stream_id[:8]}] 已清除等待锁，准备让对话器处理续话机会")
        logger.info(
            f"[{stream_id[:8]}] 已注入续话机会触发消息：topic={followup.topic}, type={followup.followup_type}"
        )

    @staticmethod
    def _resolve_followup_target(chat_stream: Any) -> tuple[str, str]:
        """从当前流上下文推断续话对象。"""
        bot_id = str(getattr(chat_stream, "bot_id", "") or "")
        history = list(getattr(chat_stream.context, "history_messages", []) or [])
        for msg in reversed(history):
            sender_id = str(getattr(msg, "sender_id", "") or "")
            sender_role = str(getattr(msg, "sender_role", "") or "").lower()
            if sender_role == "bot":
                continue
            if bot_id and sender_id == bot_id:
                continue
            if sender_id:
                return sender_id, str(getattr(msg, "sender_name", "") or "")
        return "", ""

    async def enqueue_dfc_message(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收来自 DFC 的异步留言。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        event = self._event_builder.build_dfc_message_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

        await self._queue_pending_event(event)

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "另一个我（DFC）",
            sender_id="default_chatter",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction="received",
            pending_message_count=self._state.pending_event_count,
        )
        logger.info(
            "life_engine 已接收 DFC 留言: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"sender={event.sender or '另一个我（DFC）'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
        }

    async def enqueue_outer_message(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收来自对外运行模式的异步留言。"""
        return await self.enqueue_dfc_message(
            message,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

    async def enqueue_direct_message(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
        sender_id: str = "",
    ) -> dict[str, Any]:
        """接收用户通过命令直达生命中枢的留言。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        event = self._event_builder.build_direct_message_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

        await self._queue_pending_event(event)

        log_message_received(
            received_at=event.timestamp,
            platform=event.source,
            chat_type=event.chat_type or "unknown",
            source_label=event.source_detail,
            source_detail=event.source_detail,
            stream_id=event.stream_id or "",
            sender_display=event.sender or "外部用户",
            sender_id=str(sender_id or "").strip() or "external_user",
            message_id=event.event_id,
            reply_to=None,
            message_type=event.content_type,
            content=event.content,
            direction="received",
            pending_message_count=self._state.pending_event_count,
        )
        logger.info(
            "life_engine 已接收直连留言: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"sender={event.sender or '外部用户'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "direct_command",
        }

    async def enqueue_proactive_opportunity(
        self,
        message: str,
        *,
        stream_id: str = "",
        platform: str = "",
        chat_type: str = "",
        sender_name: str = "",
    ) -> dict[str, Any]:
        """接收 proactive 插件产生的主动机会事件。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")

        event = self._event_builder.build_proactive_opportunity_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
        )

        await self._queue_pending_event(event)

        logger.info(
            "life_engine 已接收主动机会事件: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "proactive_opportunity",
        }

    def _autonomy_store(self) -> AutonomyIntentStore:
        return AutonomyIntentStore(self._workspace_dir())

    async def _resolve_autonomy_target_stream_id(
        self,
        *,
        target_stream_id: str = "",
        target_key: str = "",
    ) -> str:
        explicit = str(target_stream_id or "").strip()
        if explicit:
            return explicit
        key = str(target_key or "").strip()
        if not key:
            return ""
        try:
            from ..core.send_targets import resolve_send_target_key

            runtime_cfg = getattr(self._cfg(), "runtime_sync", None)
            target = await resolve_send_target_key(
                key,
                current_stream_id="",
                limit=int(getattr(runtime_cfg, "send_targets_limit", 8) or 8),
                active_window_hours=float(getattr(runtime_cfg, "send_targets_window_hours", 24.0) or 24.0),
            )
            if target is not None:
                return str(target.stream_id or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"解析自主意向 target_key 失败: {exc}")
        return ""

    async def schedule_autonomy_intent(
        self,
        *,
        kind: str,
        motivation: str,
        delay_minutes: int,
        target_hint: str = "",
        target_stream_id: str = "",
        target_key: str = "",
        constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        """登记一个 life_engine 自主形成的延迟意向。"""
        cfg = self._cfg()
        autonomy_cfg = getattr(cfg, "autonomy", None)
        if autonomy_cfg is not None and not bool(getattr(autonomy_cfg, "enabled", True)):
            raise RuntimeError("自主意向循环未启用")

        resolved_stream_id = await self._resolve_autonomy_target_stream_id(
            target_stream_id=target_stream_id,
            target_key=target_key,
        )
        intent = build_intent(
            kind=kind,
            motivation=motivation,
            delay_minutes=int(delay_minutes or 0),
            min_delay_minutes=int(getattr(autonomy_cfg, "min_delay_minutes", 1) or 1),
            max_delay_minutes=int(getattr(autonomy_cfg, "max_delay_minutes", 1440) or 1440),
            target_hint=target_hint,
            target_key=target_key,
            target_stream_id=resolved_stream_id,
            constraints=constraints or [],
        )

        async with self._get_lock():
            store = self._autonomy_store()
            try:
                await register_autonomy_schedule(self.plugin, intent)
            except RuntimeError as exc:
                raise RuntimeError("调度器尚未启动，稍后再登记自主意向") from exc
            store.upsert(intent)

        event_text = (
            f"已登记自主意向：kind={intent.kind} delay={intent.delay_minutes}分钟 "
            f"motivation={intent.motivation}"
        )
        event = self._event_builder.build_autonomy_intent_event(
            event_text,
            content_type="autonomy_intent_scheduled",
            stream_id=intent.target_stream_id,
            sender_name="自主意向",
        )
        await self._queue_pending_event(event)

        logger.info(
            "新意向: "
            f"kind={intent.kind} delay={intent.delay_minutes}m "
            f"intent_id={intent.intent_id[:12]} "
            f"stream={intent.target_stream_id or '-'}"
        )
        return {
            "created": True,
            "intent_id": intent.intent_id,
            "kind": intent.kind,
            "delay_minutes": intent.delay_minutes,
            "scheduled_at": intent.scheduled_at,
            "status": intent.status,
            "target_stream_id": intent.target_stream_id,
            "target_hint": intent.target_hint,
            "schedule_id": intent.schedule_id,
        }

    async def trigger_autonomy_intent(self, intent_id: str) -> dict[str, Any]:
        """触发到点的自主意向。"""
        store = self._autonomy_store()
        intent = store.get(intent_id)
        if intent is None:
            logger.warning(f"到点意向不存在: intent_id={intent_id}")
            return {"triggered": False, "reason": "not_found"}
        if intent.status != "scheduled":
            logger.debug(
                f"跳过非 scheduled 自主意向: intent_id={intent.intent_id[:12]} status={intent.status}"
            )
            return {"triggered": False, "reason": f"status={intent.status}"}

        intent.status = "triggered"
        intent.triggered_at = _now_iso()
        intent.updated_at = intent.triggered_at
        store.upsert(intent)

        logger.info(
            "到点: "
            f"intent_id={intent.intent_id[:12]} kind={intent.kind} "
            f"stream={intent.target_stream_id or '-'}"
        )

        if intent.kind == "speak":
            if not intent.target_stream_id:
                event = self._event_builder.build_autonomy_intent_event(
                    format_due_message(intent),
                    content_type="autonomy_intent_due",
                    sender_name="自主意向",
                )
                await self._queue_pending_event(event)
                logger.info(f"仲裁: downgraded intent_id={intent.intent_id[:12]} reason=no_target_stream")
                return {"triggered": True, "dispatch": "life_event", "reason": "no_target_stream"}
            await self._wake_stream_for_autonomy(intent)
            logger.info(f"承接: life_chatter intent_id={intent.intent_id[:12]}")
            return {"triggered": True, "dispatch": "life_chatter", "stream_id": intent.target_stream_id}

        if intent.kind == "reflect":
            event = self._event_builder.build_autonomy_intent_event(
                format_due_message(intent),
                content_type="autonomy_intent_due",
                sender_name="自主意向",
            )
            await self._queue_pending_event(event)
            logger.info(f"承接: life_engine intent_id={intent.intent_id[:12]}")
            return {"triggered": True, "dispatch": "life_engine"}

        event = self._event_builder.build_autonomy_intent_event(
            f"自主意向到点后选择沉默：{intent.motivation}",
            content_type="autonomy_intent_silence",
            sender_name="自主意向",
        )
        await self._queue_pending_event(event)
        logger.info(f"承接: silence intent_id={intent.intent_id[:12]}")
        return {"triggered": True, "dispatch": "silence"}

    async def _wake_stream_for_autonomy(self, intent: AutonomyIntent) -> None:
        """把到点自主意向注入目标聊天流，交给 life_chatter 承接。"""
        from src.core.managers import get_stream_manager
        from src.core.transport.distribution.stream_loop_manager import get_stream_loop_manager
        import time

        stream_id = str(intent.target_stream_id or "").strip()
        chat_stream = await get_stream_manager().get_or_create_stream(stream_id=stream_id)
        context = chat_stream.context
        target_user_id, target_user_name = self._resolve_followup_target(chat_stream)
        prompt = format_due_message(intent)
        trigger_message = Message(
            message_id=f"autonomy_intent_{intent.intent_id[:16]}",
            platform=chat_stream.platform or "unknown",
            stream_id=stream_id,
            sender_id=target_user_id or "life_engine_autonomy",
            sender_name="系统（自主意向浮现）",
            sender_role="other",
            content=prompt,
            processed_plain_text=prompt,
            message_type=MessageType.TEXT,
            time=time.time(),
            target_user_id=target_user_id,
            target_user_name=target_user_name,
            is_autonomy_intent_trigger=True,
            autonomy_intent_id=intent.intent_id,
            autonomy_intent_kind=intent.kind,
        )
        context.add_unread_message(trigger_message)
        loop_mgr = get_stream_loop_manager()
        removed = loop_mgr._wait_states.pop(stream_id, None)
        if removed:
            logger.debug(f"[{stream_id[:8]}] 已清除等待锁，准备处理自主意向")

    async def record_chatter_inner_monologue(
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
    ) -> dict[str, Any]:
        """记录由 life_chatter 生成的内心独白。"""
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        text = str(thought or "").strip()
        if not text:
            raise ValueError("thought 不能为空")

        event = self._event_builder.build_chatter_inner_monologue_event(
            text,
            stream_id=stream_id,
            platform=platform,
            chat_type=chat_type,
            sender_name=sender_name,
            mood=mood,
            intent=intent,
            topic=topic,
        )

        await self._queue_pending_event(event)

        logger.info(
            "life_engine 已记录对话器内心独白: "
            f"stream_id={event.stream_id or 'unknown'} "
            f"pending={self._state.pending_event_count}"
        )
        return {
            "event_id": event.event_id,
            "stream_id": event.stream_id or "",
            "pending_event_count": self._state.pending_event_count,
            "queued": True,
            "channel": "chatter_inner_monologue",
        }

    async def record_chatter_think_snapshot(
        self,
        *,
        stream_id: str,
        thought: str,
        mood: str = "",
        decision: str = "",
        expected_response: str = "",
    ) -> dict[str, Any]:
        """记录 life_chatter 最近一次 action-think 快照。

        新的统一主意识链路使用全局快照；同时保留按 stream 写入，兼容旧工具和
        已持久化状态。
        """
        if not self._is_enabled():
            raise RuntimeError("life_engine 未启用")

        sid = str(stream_id or "").strip()
        if not sid:
            raise ValueError("stream_id 不能为空")

        snapshot = {
            "thought": _shorten_text(str(thought or "").strip(), max_length=500),
            "mood": _shorten_text(str(mood or "").strip(), max_length=80),
            "decision": _shorten_text(str(decision or "").strip(), max_length=160),
            "expected_response": _shorten_text(
                str(expected_response or "").strip(),
                max_length=160,
            ),
            "recorded_at": _now_iso(),
        }

        async with self._get_lock():
            latest = dict(self._state.last_chatter_think_by_stream or {})
            compact_snapshot = {
                key: value
                for key, value in snapshot.items()
                if isinstance(value, str) and value.strip()
            }
            latest[sid] = compact_snapshot
            latest[LIFE_CHATTER_GLOBAL_CURSOR_KEY] = compact_snapshot
            self._state.last_chatter_think_by_stream = latest
        await self._save_runtime_context()

        return {
            "stream_id": sid,
            "recorded_at": snapshot["recorded_at"],
            "channel": "chatter_think_snapshot",
        }

    async def record_tool_call(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """记录工具调用事件。"""
        event = self._event_builder.build_tool_call_event(tool_name, tool_args)
        await self._queue_pending_event(event)

    async def record_tool_result(self, tool_name: str, result: str, success: bool) -> None:
        """记录工具返回结果事件。"""
        event = self._event_builder.build_tool_result_event(tool_name, result, success)
        await self._queue_pending_event(event)

    async def _collect_background_agent_results(self) -> None:
        """收集已完成的后台智能体结果，注入为事件。"""
        coordinator = getattr(self.plugin, "_agent_coordinator", None)
        if coordinator is None or not coordinator.has_pending():
            return

        results = await coordinator.collect_results(timeout_seconds=5.0)
        if not results:
            return

        events: list[LifeEngineEvent] = []
        for agent_id, result in results.items():
            event = self._event_builder.build_agent_result_event(
                agent_type=result.agent_type,
                result_text=result.result_text,
                success=result.success,
                rounds=result.rounds_used,
                duration_ms=result.duration_ms,
            )
            events.append(event)

        await self._append_history(events)

    async def drain_pending_events(self) -> list[LifeEngineEvent]:
        """清空并返回当前待处理事件。"""
        async with self._get_lock():
            pending = list(self._pending_events)
            self._pending_events.clear()
            self._state.pending_event_count = 0
        return pending

    async def _append_history(
        self,
        events: list[LifeEngineEvent],
        *,
        publish_raw: bool = True,
    ) -> None:
        """将事件追加到滚动历史中，支持压缩。"""
        if not events:
            return

        async with self._get_lock():
            self._event_history.extend(events)
            limit = self._history_limit()

            compress_threshold = int(limit * 0.8)
            if len(self._event_history) > compress_threshold:
                self._event_history = compress_history(self._event_history, limit)

            self._state.history_event_count = len(self._event_history)
        if publish_raw:
            await self._publish_raw_events(events)
        await self._save_runtime_context()

    async def clear_runtime_context(self) -> None:
        """清理当前事件上下文。"""
        async with self._get_lock():
            self._pending_events.clear()
            self._event_history.clear()
            self._state.pending_event_count = 0
            self._state.history_event_count = 0
            self._state.event_sequence = 0
        await self._save_runtime_context()
        clear_wake_context_reminder()

    def _build_wake_context_text(self, events: list[LifeEngineEvent]) -> str:
        """把事件流拼成可注入的上下文文本。"""
        if not events:
            return ""

        sorted_events = sorted(events, key=lambda e: e.sequence)
        lines: list[str] = []

        for event in sorted_events:
            time_display = _format_time_display(event.timestamp)

            if event.event_type == EventType.MESSAGE:
                source = event.source_detail or event.source or "外部"
                source_short = self._simplify_source(source)
                line = f"[{time_display}] 📨 {source_short}"
                line += f"\n    └─ {event.sender}: {event.content}"
            elif event.event_type == EventType.HEARTBEAT:
                if str(event.content_type or "").strip().lower() == "attention_summary":
                    line = f"[{time_display}] 🧠 潜意识摘要"
                else:
                    line = f"[{time_display}] 💭 心跳#{event.heartbeat_index}"
                line += f"\n    └─ {event.content}"
            elif event.event_type == EventType.TOOL_CALL:
                line = f"[{time_display}] 🔧 {event.tool_name}"
                if event.tool_args:
                    args_short = self._simplify_tool_args(event.tool_args)
                    if args_short:
                        line += f"({args_short})"
                content_short = _shorten_text(event.content or "", max_length=160)
                if content_short:
                    line += f"\n    └─ {content_short}"
            elif event.event_type == EventType.TOOL_RESULT:
                status = "✅" if event.tool_success else "❌"
                result_short = _shorten_text(event.content or "", max_length=100)
                line = f"[{time_display}] {status} {event.tool_name}: {result_short}"
            elif event.event_type == EventType.AGENT_RESULT:
                status = "✅" if event.tool_success else "❌"
                agent_name = event.tool_name or "agent"
                result_short = _shorten_text(event.content or "", max_length=200)
                line = f"[{time_display}] {status} 🤖 {agent_name}: {result_short}"
            else:
                line = f"[{time_display}] ❓ {event.content}"

            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _event_belongs_to_life_runtime(
        event: LifeEngineEvent,
        *,
        current_stream_id: str,
        unified_chatter_context: bool = False,
    ) -> bool:
        """判断事件是否应自动给 life_chatter 作为同源运行态可见。"""
        event_type = event.event_type
        if event_type == EventType.HEARTBEAT:
            stream_id = str(event.stream_id or "").strip()
            if unified_chatter_context:
                return True
            return bool(not stream_id or stream_id == current_stream_id)
        if event_type in {EventType.TOOL_CALL, EventType.TOOL_RESULT, EventType.AGENT_RESULT}:
            return True

        stream_id = str(event.stream_id or "").strip()
        content_type = str(event.content_type or "").strip().lower()
        source = str(event.source or "").strip().lower()

        if unified_chatter_context and stream_id:
            return True

        if current_stream_id and stream_id == current_stream_id:
            return content_type != "text"

        if content_type in {"heartbeat_reply", "chatter_inner_monologue", "tool_call", "tool_result"}:
            return True
        if content_type in {
            "proactive_opportunity",
            "dfc_message",
            "direct_message",
            "autonomy_intent_due",
            "autonomy_intent_scheduled",
            "autonomy_intent_silence",
        }:
            return bool(not stream_id or stream_id == current_stream_id)
        return source == "life_engine" and not stream_id

    @staticmethod
    def _chatter_cursor_key(
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> str:
        if unified_chatter_context:
            return LIFE_CHATTER_GLOBAL_CURSOR_KEY
        return str(stream_id or "").strip()

    def _chatter_event_cursor(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> int:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return 0
        return int((self._state.chatter_context_cursors or {}).get(sid, 0) or 0)

    # 兼容旧接口名
    def _chatter_context_cursor(self, stream_id: str) -> int:
        return self._chatter_event_cursor(stream_id)

    def _chatter_thought_cursor(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> int:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return 0
        return int((self._state.chatter_thought_cursors or {}).get(sid, 0) or 0)

    async def mark_chatter_runtime_context_seen(
        self,
        stream_id: str,
        sequence: int,
        *,
        unified_chatter_context: bool = False,
    ) -> None:
        """标记某个聊天流已经看过的 life 事件流高水位（event cursor）。

        thought_revision cursor 在 build_chatter_runtime_context 渲染时已内部提交，
        外部调用方仍只需要传 event 序列高水位。
        """
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid or sequence <= 0:
            return
        async with self._get_lock():
            cursors = self._state.chatter_context_cursors
            cursors[sid] = max(int(cursors.get(sid, 0) or 0), int(sequence))

    async def _commit_chatter_thought_cursor(
        self,
        stream_id: str,
        revision: int,
        *,
        unified_chatter_context: bool = False,
    ) -> None:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid or revision <= 0:
            return
        async with self._get_lock():
            cursors = self._state.chatter_thought_cursors
            cursors[sid] = max(int(cursors.get(sid, 0) or 0), int(revision))

    def _format_chatter_inner_state(self) -> str:
        if self._inner_state is None:
            return ""
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            return self._inner_state.format_full_state_for_prompt(today_str)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"构建 chatter inner_state 快照失败: {exc}")
            try:
                state_dict = self._inner_state.get_full_state()
            except Exception:  # noqa: BLE001
                return ""
            if not isinstance(state_dict, dict):
                return ""
            return "\n".join(f"{key}: {value}" for key, value in state_dict.items())

    def _format_chatter_trace_recent_changes(self, *, limit: int = 3) -> str:
        """渲染最近文件追溯块（用于 chatter suffix）。"""
        if limit <= 0:
            return ""
        try:
            records = LifeTraceStore(self._workspace_dir()).recent(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取最近文件追溯失败: {exc}")
            return ""
        if not records:
            return ""
        return "\n".join(self._format_trace_record_line(record) for record in records)

    @staticmethod
    def _format_trace_record_line(record: LifeTraceRecord) -> str:
        timestamp = str(record.timestamp or "")
        operation = str(record.operation or "modify")
        path = str(record.path or "未知文件")
        reason = str(record.reason or "").strip()
        trace_id = str(record.trace_id or "")
        detail = f"，原因：{reason}" if reason else ""
        trace_ref = f"，trace_id={trace_id}" if trace_id else ""
        return f"- {timestamp} {operation} {path}{detail}{trace_ref}"

    def _format_chatter_thought_streams(
        self,
        *,
        revision_cursor: int = 0,
        focus_window_minutes: int = 30,
        delta_marking: bool = True,
        max_items: int = 5,
    ) -> tuple[str, int]:
        """渲染思考流块（用于 chatter transient）。

        Returns:
            (body_text_without_top_heading, current_max_revision)
        """
        if self._thought_manager is None:
            return "", 0
        try:
            body = self._thought_manager.format_for_prompt(
                max_items=max_items,
                focus_window_minutes=focus_window_minutes,
                revision_cursor=revision_cursor,
                mark_delta=delta_marking,
                grouped=True,
            )
            return body, int(self._thought_manager.current_revision)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"构建 chatter 思考流快照失败: {exc}")
            return "", 0

    def _format_latest_chatter_think(
        self,
        stream_id: str,
        *,
        unified_chatter_context: bool = False,
    ) -> str:
        sid = self._chatter_cursor_key(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if not sid:
            return ""
        snapshot = (self._state.last_chatter_think_by_stream or {}).get(sid)
        if not isinstance(snapshot, dict):
            return ""

        thought = str(snapshot.get("thought") or "").strip()
        mood = str(snapshot.get("mood") or "").strip()
        decision = str(snapshot.get("decision") or "").strip()
        expected = str(snapshot.get("expected_response") or "").strip()
        recorded_at = str(snapshot.get("recorded_at") or "").strip()
        if not any((thought, mood, decision, expected)):
            return ""

        lines: list[str] = []
        if recorded_at:
            lines.append(f"- 时间：{_format_time_display(recorded_at)}")
        if mood:
            lines.append(f"- 心情：{mood}")
        if decision:
            lines.append(f"- 决定：{decision}")
        if expected:
            lines.append(f"- 预期反应：{expected}")
        if thought:
            lines.append(f"- 思考：{thought}")
        return "\n".join(lines)

    @staticmethod
    def _message_flag(message: Message, flag_name: str) -> bool:
        return message_flag(message, flag_name)

    @classmethod
    def _build_recent_chat_history_text(
        cls,
        chat_stream: Any,
        *,
        max_messages: int,
        unified_chatter_context: bool = False,
    ) -> str:
        return build_chat_history_text(
            chat_stream,
            max_messages=max_messages,
            global_history=unified_chatter_context,
        )

    @staticmethod
    def _is_salient_event(
        event: LifeEngineEvent,
        *,
        current_stream_id: str,
        cfg_runtime: Any,
        unified_chatter_context: bool = False,
    ) -> bool:
        """判定事件是否进入 chatter 的"最近关键活动"尾巴。

        相对老的 `_event_belongs_to_life_runtime` 严格得多：默认丢弃 HEARTBEAT、
        普通 TOOL_CALL，仅保留 agent 结果、工具失败、direct/dfc/proactive 消息以及
        最近的 inner_monologue。
        """
        event_type = event.event_type
        content_type = str(getattr(event, "content_type", "") or "").strip().lower()
        stream_id = str(getattr(event, "stream_id", "") or "").strip()

        if event_type == EventType.HEARTBEAT:
            # 只保留 chatter 自己产生的 inner_monologue 心跳
            if content_type == "chatter_inner_monologue" and getattr(cfg_runtime, "salient_tail_include_inner_monologue", True):
                if unified_chatter_context:
                    return True
                return bool(not stream_id or stream_id == current_stream_id)
            return False

        if event_type == EventType.AGENT_RESULT:
            return bool(getattr(cfg_runtime, "salient_tail_include_agent_results", True))

        if event_type == EventType.TOOL_RESULT:
            if not getattr(cfg_runtime, "salient_tail_include_tool_failures", True):
                return False
            # 仅失败结果进入尾巴；成功结果默认丢弃
            return not bool(getattr(event, "tool_success", True))

        if event_type == EventType.TOOL_CALL:
            return False

        if event_type == EventType.MESSAGE:
            if content_type in {
                "dfc_message",
                "direct_message",
                "proactive_opportunity",
                "autonomy_intent_due",
                "autonomy_intent_scheduled",
                "autonomy_intent_silence",
            }:
                if not getattr(cfg_runtime, "salient_tail_include_direct_messages", True):
                    return False
                if unified_chatter_context:
                    return True
                return bool(not stream_id or stream_id == current_stream_id)
            # 其它消息不进入 salient tail（chat history 已覆盖）
            return False

        return False

    def _format_salient_event(self, event: LifeEngineEvent) -> str:
        """把单条 salient event 渲染成一行摘要。"""
        time_display = _format_time_display(event.timestamp)
        event_type = event.event_type
        content = str(getattr(event, "content", "") or "")
        if event_type == EventType.HEARTBEAT:
            return f"[{time_display}] 💭 内心独白: {_shorten_text(content, max_length=160)}"
        if event_type == EventType.AGENT_RESULT:
            status = "✅" if getattr(event, "tool_success", True) else "❌"
            agent_name = str(getattr(event, "tool_name", "") or "agent")
            return f"[{time_display}] {status} 🤖 {agent_name}: {_shorten_text(content, max_length=200)}"
        if event_type == EventType.TOOL_RESULT:
            tool_name = str(getattr(event, "tool_name", "") or "tool")
            return f"[{time_display}] ❌ {tool_name}: {_shorten_text(content, max_length=160)}"
        if event_type == EventType.MESSAGE:
            content_type = str(getattr(event, "content_type", "") or "").strip().lower()
            sender = str(getattr(event, "sender", "") or "")
            label = {
                "dfc_message": "📮 DFC",
                "direct_message": "📨 私信",
                "proactive_opportunity": "✨ 主动机会",
                "autonomy_intent_due": "🌱 自主意向",
                "autonomy_intent_scheduled": "🌱 意向登记",
                "autonomy_intent_silence": "🌱 选择沉默",
            }.get(content_type, "📩")
            return f"[{time_display}] {label} {sender}: {_shorten_text(content, max_length=160)}"
        return f"[{time_display}] {_shorten_text(content, max_length=120)}"

    def _build_salient_activity_tail(
        self,
        events: list[LifeEngineEvent],
        cursor: int,
        *,
        current_stream_id: str,
        unified_chatter_context: bool = False,
    ) -> tuple[str, int]:
        """从事件流派生最近关键活动尾巴。

        Returns:
            (body_without_top_heading, new_event_high_water)
        """
        cfg_runtime = getattr(self._cfg(), "runtime_sync", None)
        if cfg_runtime is None or not getattr(cfg_runtime, "salient_tail_enabled", True):
            return "", cursor

        max_items = max(1, int(getattr(cfg_runtime, "salient_tail_max_items", 4) or 4))
        max_chars = max(200, int(getattr(cfg_runtime, "salient_tail_max_chars", 1000) or 1000))

        # 先按 sequence 升序，过滤 cursor 之后的事件
        candidates = [
            e for e in events
            if int(getattr(e, "sequence", 0) or 0) > cursor
            and self._is_salient_event(
                e,
                current_stream_id=current_stream_id,
                cfg_runtime=cfg_runtime,
                unified_chatter_context=unified_chatter_context,
            )
        ]
        if not candidates:
            return "", cursor

        # AGENT_RESULT 仅保留最新一条；inner_monologue 最多 2 条；其它按时间倒序取最新若干
        kept_agent: LifeEngineEvent | None = None
        kept_monologue: list[LifeEngineEvent] = []
        kept_other: list[LifeEngineEvent] = []
        for event in candidates:
            event_type = event.event_type
            content_type = str(getattr(event, "content_type", "") or "").strip().lower()
            if event_type == EventType.AGENT_RESULT:
                kept_agent = event  # 后写入即最新
            elif event_type == EventType.HEARTBEAT and content_type == "chatter_inner_monologue":
                kept_monologue.append(event)
            else:
                kept_other.append(event)

        kept_monologue = kept_monologue[-2:]
        # 其它按时间倒序后从尾部截到 max_items 减去已用配额
        kept_other = kept_other[-max_items:]

        merged: list[LifeEngineEvent] = []
        if kept_agent is not None:
            merged.append(kept_agent)
        merged.extend(kept_monologue)
        merged.extend(kept_other)
        merged.sort(key=lambda e: int(getattr(e, "sequence", 0) or 0))
        merged = merged[-max_items:]

        # 渲染并裁字
        rendered: list[str] = [self._format_salient_event(e) for e in merged]
        body = "\n".join(rendered)
        if len(body) > max_chars:
            # 从前向后丢弃，保留最新尾部
            while rendered and len("\n".join(rendered)) > max_chars:
                rendered.pop(0)
            body = "\n".join(rendered)
            if not body:
                # 极端情况下，单条仍超长 → 截断该单条
                body = _shorten_text(self._format_salient_event(merged[-1]), max_length=max_chars)

        new_high_water = max(int(getattr(e, "sequence", 0) or 0) for e in merged)
        new_high_water = max(new_high_water, cursor)
        return body, new_high_water

    async def build_chatter_runtime_context(
        self,
        chat_stream: Any,
        *,
        runtime_context_text: str = "",
        event_limit: int = 80,
        unified_chatter_context: bool = False,
        include_recent_chat_history: bool = True,
        commit_cursors: bool = True,
        event_cursor_override: int | None = None,
    ) -> tuple[str, int]:
        """构建给 life_chatter 的同源运行态快照。

        返回值为 (context_text, high_water_sequence)。context_text 只用于本轮
        transient 注入；high_water_sequence 在 LLM 请求成功后持久化，避免重复注入。

        结构：
          1. ### 当前内在状态  （neuromod）
          2. ### 当前思考流    （注意力脑区，分焦点/背景，带 🔄 delta 标记）
          3. ### 最近一次 action-think
          4. ### 运行时内心独白（push_runtime_assistant_injection 队列）
          5. ### 最近聊天记录
          6. ### 新增 life 事件流（完整可追溯事件窗口）
          7. ### 最近关键活动  （仅在没有新增事件流时作为兜底摘要）
        """
        stream_id = str(getattr(chat_stream, "stream_id", "") or "").strip()
        if event_cursor_override is None:
            event_cursor = self._chatter_event_cursor(
                stream_id,
                unified_chatter_context=unified_chatter_context,
            )
        else:
            event_cursor = max(0, int(event_cursor_override or 0))
        thought_cursor = self._chatter_thought_cursor(
            stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        _ = event_limit  # 兼容老签名；新逻辑用配置项控制条数

        cfg = self._cfg()
        streams_cfg = getattr(cfg, "streams", None)
        runtime_cfg = getattr(cfg, "runtime_sync", None)
        sync_streams = bool(streams_cfg is None or getattr(streams_cfg, "sync_to_chatter", True))
        focus_window = int(getattr(streams_cfg, "focus_window_minutes", 30) or 30) if streams_cfg else 30
        delta_marking = bool(streams_cfg is None or getattr(streams_cfg, "delta_marking", True))
        latest_think_enabled = bool(
            runtime_cfg is None
            or getattr(runtime_cfg, "latest_action_think_enabled", True)
        )
        recent_chat_enabled = bool(
            runtime_cfg is None or getattr(runtime_cfg, "recent_chat_enabled", True)
        )
        recent_chat_messages = (
            int(getattr(runtime_cfg, "recent_chat_messages", 10) or 10)
            if runtime_cfg is not None
            else 10
        )
        trace_recent_enabled = bool(
            runtime_cfg is None
            or getattr(runtime_cfg, "trace_recent_changes_enabled", True)
        )
        trace_recent_limit = (
            int(getattr(runtime_cfg, "trace_recent_changes_limit", 3))
            if runtime_cfg is not None
            else 3
        )
        send_targets_enabled = bool(
            runtime_cfg is None or getattr(runtime_cfg, "send_targets_enabled", True)
        )
        send_targets_limit = (
            int(getattr(runtime_cfg, "send_targets_limit", 8) or 8)
            if runtime_cfg is not None
            else 8
        )
        send_targets_window_hours = (
            float(getattr(runtime_cfg, "send_targets_window_hours", 24.0) or 24.0)
            if runtime_cfg is not None
            else 24.0
        )

        async with self._get_lock():
            events = list(self._event_history)
            events.extend(list(self._pending_events))
        events.sort(key=lambda event: int(event.sequence or 0))

        limit = max(1, min(int(event_limit or 80), 160))
        relevant_events = [
            event
            for event in events
            if int(event.sequence or 0) > event_cursor
            and self._event_belongs_to_life_runtime(
                event,
                current_stream_id=stream_id,
                unified_chatter_context=unified_chatter_context,
            )
        ]
        if unified_chatter_context:
            attention_window = self._get_attention_router().select(
                relevant_events,
                cursor=event_cursor,
                current_stream_id=stream_id,
                max_events=min(limit, 40),
            )
            selected_events = attention_window.events
            omitted_event_count = attention_window.dropped_count
            new_event_high_water = max(attention_window.high_water, event_cursor)
        else:
            omitted_event_count = max(0, len(relevant_events) - limit)
            selected_events = relevant_events[-limit:]
            new_event_high_water = max(
                (int(event.sequence or 0) for event in relevant_events),
                default=event_cursor,
            )

        sections: list[str] = []

        inner_state_text = self._format_chatter_inner_state()
        if inner_state_text:
            sections.append(f"### 当前内在状态\n{inner_state_text}")

        new_thought_revision = thought_cursor
        if sync_streams:
            thought_body, current_revision = self._format_chatter_thought_streams(
                revision_cursor=thought_cursor,
                focus_window_minutes=focus_window,
                delta_marking=delta_marking,
                max_items=5,
            )
            if thought_body:
                sections.append(f"### 当前思考流\n{thought_body}".rstrip())
            new_thought_revision = max(thought_cursor, current_revision)

        if latest_think_enabled:
            latest_think_text = self._format_latest_chatter_think(
                stream_id,
                unified_chatter_context=unified_chatter_context,
            )
            if latest_think_text:
                sections.append(f"### 最近一次 action-think\n{latest_think_text}")

        runtime_text = str(runtime_context_text or "").strip()
        if runtime_text:
            sections.append(f"### 运行时内心独白\n{runtime_text}")

        curiosity_cfg = getattr(cfg, "curiosity", None)
        curiosity_enabled = bool(
            curiosity_cfg is None or getattr(curiosity_cfg, "enabled", True)
        )
        curiosity_inject = bool(
            curiosity_cfg is None or getattr(curiosity_cfg, "inject_to_chatter", True)
        )
        if curiosity_enabled and curiosity_inject:
            try:
                curiosity_text = await self._get_curiosity_engine().format_for_prompt(
                    max_chars=int(getattr(curiosity_cfg, "max_prompt_chars", 1200) or 1200)
                    if curiosity_cfg is not None
                    else 1200
                )
                if curiosity_text:
                    sections.append(curiosity_text)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"读取好奇牵引失败: {exc}")

        if include_recent_chat_history and recent_chat_enabled and recent_chat_messages > 0:
            recent_chat_text = self._build_recent_chat_history_text(
                chat_stream,
                max_messages=recent_chat_messages,
                unified_chatter_context=unified_chatter_context,
            )
            if recent_chat_text:
                sections.append(
                    f"### 最近 {recent_chat_messages} 条聊天记录\n{recent_chat_text}"
                )

        if trace_recent_enabled and trace_recent_limit > 0:
            trace_recent_text = self._format_chatter_trace_recent_changes(
                limit=trace_recent_limit,
            )
            if trace_recent_text:
                sections.append(f"### 最近文件修改\n{trace_recent_text}")

        if send_targets_enabled:
            send_targets_text = format_send_targets_for_prompt(
                await list_recent_send_targets(
                    current_stream_id=stream_id,
                    limit=send_targets_limit,
                    active_window_hours=send_targets_window_hours,
                )
            )
            if send_targets_text:
                sections.append(f"### 可发送目标\n{send_targets_text}")

        if selected_events:
            event_text = self._build_wake_context_text(selected_events)
            if omitted_event_count:
                event_text = (
                    f"（潜意识已压缩 {omitted_event_count} 条低显著 life 事件；"
                    "需要时用 grep_life_events 检索历史事件。）\n"
                    f"{event_text}"
                )
            sections.append(f"### 新增 life 事件流\n{event_text}")

        salient_body, salient_high_water = self._build_salient_activity_tail(
            events,
            event_cursor,
            current_stream_id=stream_id,
            unified_chatter_context=unified_chatter_context,
        )
        if salient_body and not selected_events:
            sections.append(f"### 最近关键活动\n{salient_body}")
            new_event_high_water = max(new_event_high_water, salient_high_water)

        # thought delta cursor 在渲染阶段直接提交（不等待 LLM 调用成功）
        if commit_cursors and new_thought_revision > thought_cursor:
            await self._commit_chatter_thought_cursor(
                stream_id,
                new_thought_revision,
                unified_chatter_context=unified_chatter_context,
            )

        if not sections:
            return "", new_event_high_water

        header = (
            "这是同一主体 life_mode 自上次对话器读取后产生的运行态。"
            "它只在本轮临时可见，不会长期留在对话 payload。"
        )
        return f"{header}\n\n" + "\n\n".join(sections), new_event_high_water

    async def search_outer_memory(self, query: str, top_k: int = 5) -> str:
        """供对外运行模式深度检索 life memory。"""
        return await self.search_actor_memory(query, top_k=top_k)

    def _simplify_source(self, source: str) -> str:
        """简化消息来源显示。"""
        if not source:
            return "外部"
        source = source.replace("qq | 入站 | ", "").replace("qq | 出站 | ", "")
        if len(source) > 30:
            return source[:27] + "..."
        return source

    def _simplify_tool_args(self, args: dict) -> str:
        """简化工具参数显示。"""
        if not args:
            return ""
        key_params = []
        for k, v in args.items():
            if k in ("path", "todo_id", "title", "content", "file_path"):
                v_str = str(v)
                if len(v_str) > 20:
                    v_str = v_str[:17] + "..."
                key_params.append(f"{k}={v_str}")
        return ", ".join(key_params[:2])

    async def inject_wake_context(self) -> str:
        """把当前待处理事件注入到系统提醒。"""
        events = await self.drain_pending_events()
        if events:
            await self._append_history(events, publish_raw=False)

        async with self._get_lock():
            context_events = list(self._event_history)

        if not context_events:
            clear_wake_context_reminder()
            return ""

        window = self._get_attention_router().select(
            context_events,
            cursor=0,
            current_stream_id="",
            max_events=self._history_limit(),
        )
        context_events = window.events
        content = self._build_wake_context_text(context_events)
        from src.core.prompt import get_system_reminder_store
        from .state_manager import _TARGET_REMINDER_BUCKET, _TARGET_REMINDER_NAME

        store = get_system_reminder_store()
        store.set(_TARGET_REMINDER_BUCKET, name=_TARGET_REMINDER_NAME, content=content)

        self._state.last_wake_context_at = _now_iso()
        self._state.last_wake_context_size = len(context_events)
        log_wake_context_injected(
            task_name=self._cfg().model.task_name,
            wake_context_at=self._state.last_wake_context_at,
            context_message_count=len(context_events),
            drained_message_count=len(events),
            history_message_count=len(context_events),
            source_count=len({event.source for event in context_events}),
            content=content,
        )
        logger.info(
            "life_engine 已注入唤醒上下文: "
            f"count={len(context_events)} drained={len(events)} "
            f"task={self._cfg().model.task_name}"
        )
        return content

    async def _record_model_reply(self, model_reply: str) -> None:
        """记录心跳模型回复。"""
        reply_text = model_reply.strip()

        self._state.last_model_reply_at = _now_iso()
        self._state.last_model_reply = reply_text
        self._state.last_model_error = None

        log_heartbeat_model_response(
            heartbeat_count=self._state.heartbeat_count,
            heartbeat_at=self._state.last_heartbeat_at,
            model_task_name=self._cfg().model.task_name,
            model_reply=reply_text,
            model_reply_size=len(reply_text),
        )

        if reply_text:
            logger.info(
                "life_engine 心跳模型回复: "
                f"#{self._state.heartbeat_count} "
                f"{_shorten_text(reply_text, max_length=240)}"
            )
            heartbeat_event = self._event_builder.build_heartbeat_event(
                reply_text,
                self._state.heartbeat_count,
                self._cfg().model.task_name or "life",
            )
            await self._append_history([heartbeat_event])
        else:
            logger.info(f"life_engine 心跳模型回复为空: #{self._state.heartbeat_count}")

    def _build_heartbeat_model_prompt(
        self,
        wake_context: str,
        *,
        memory_maintenance_prompt: str = "",
    ) -> str:
        """构造心跳模型输入。"""
        minutes_since_external = self._minutes_since_external_message()
        heartbeat_interval = self._cfg().settings.heartbeat_interval_seconds
        idle_heartbeats = self._state.idle_heartbeat_count

        if minutes_since_external is None:
            external_activity = "暂无外部消息记录"
        elif minutes_since_external <= 5:
            external_activity = f"外界非常活跃（{minutes_since_external}分钟前有消息）"
        elif minutes_since_external <= 15:
            external_activity = f"外界较活跃（{minutes_since_external}分钟前有消息）"
        elif minutes_since_external <= 30:
            external_activity = f"外界有一段时间安静了（{minutes_since_external}分钟前有消息）"
        else:
            external_activity = f"外界长时间沉默（{minutes_since_external}分钟无消息）"

        period_label, suggested_activities = self._get_period_info()

        cfg = self._cfg()
        thresholds = getattr(cfg, "thresholds", None)
        warning_threshold = int(
            getattr(thresholds, "idle_warning_threshold", HEARTBEAT_IDLE_WARNING_THRESHOLD)
        )
        critical_threshold = int(
            getattr(thresholds, "idle_critical_threshold", HEARTBEAT_IDLE_CRITICAL_THRESHOLD)
        )

        idle_warning = ""
        if idle_heartbeats >= critical_threshold:
            idle_warning = f"🌿 已经安静了 {idle_heartbeats} 次心跳了。如果有想做的事，现在是个好时机；如果没有，继续休息也很好。"
        elif idle_heartbeats >= warning_threshold:
            idle_warning = f"💭 安静了 {idle_heartbeats} 次心跳——如果有想继续的思考流或想分享的事，随时可以；不想也没关系。"

        lines: list[str] = self._build_prompt_header()
        lines.extend(self._build_prompt_context_section(wake_context))

        if self._dream_scheduler is not None:
            try:
                dream_payload = str(
                    self._dream_scheduler.get_active_residue_payload("life") or ""
                ).strip()
                if dream_payload:
                    lines.extend([
                        "### 梦后余韵", "",
                        dream_payload, "",
                    ])
            except Exception:  # noqa: BLE001
                logger.debug("读取梦后余韵失败")

        lines.extend(self._build_prompt_status_section(
            period_label, heartbeat_interval, external_activity, idle_heartbeats
        ))

        # SNN 驱动注入已降级：shadow_only 模式下不注入 prompt
        # 神经调质层（neuromod）已提供更清晰的驱动状态摘要
        # SNN 仍作为底层信号处理器运行，提供特征提取和奖赏计算

        cfg = self._cfg()
        if self._inner_state is not None and getattr(cfg, "neuromod", None) is not None:
            if cfg.neuromod.enabled and cfg.neuromod.inject_to_heartbeat:
                today_str = datetime.now().strftime("%Y-%m-%d")
                neuromod_text = self._inner_state.format_full_state_for_prompt(today_str)
                if neuromod_text:
                    lines.extend([neuromod_text, ""])

        # 思考流注入（heartbeat 内不分组、不做 delta，因为这是 life 自身上下文）
        if self._thought_manager is not None:
            streams_cfg = getattr(cfg, "streams", None)
            if streams_cfg is None or getattr(streams_cfg, "inject_to_heartbeat", True):
                focus_window = int(getattr(streams_cfg, "focus_window_minutes", 30) or 30) if streams_cfg else 30
                streams_body = self._thought_manager.format_for_prompt(
                    max_items=3,
                    focus_window_minutes=focus_window,
                    grouped=False,
                    mark_delta=False,
                )
                if streams_body:
                    lines.append("### 当前思考流")
                    lines.extend([streams_body, ""])

        # 冲动建议注入
        if self._impulse_engine is not None:
            drives_cfg = getattr(cfg, "drives", None)
            if drives_cfg is None or getattr(drives_cfg, "inject_to_heartbeat", True):
                neuromod_state = {}
                if self._inner_state is not None:
                    try:
                        neuromod_state = self._inner_state.get_full_state()
                    except Exception:  # noqa: BLE001
                        pass
                has_urgent_todos = False
                try:
                    from ..tools.todo_tools import TodoStorage

                    active_todos = [
                        todo for todo in TodoStorage(self._workspace_dir()).load()
                        if todo.status not in {"completed", "cancelled", "archived"}
                    ]
                    has_urgent_todos = any(
                        todo.priority == "urgent" or todo.is_overdue() or todo.needs_review()
                        for todo in active_todos
                    )
                except Exception:  # noqa: BLE001
                    has_urgent_todos = False
                context = {
                    "silence_minutes": minutes_since_external or 0,
                    "idle_heartbeats": idle_heartbeats,
                    "has_active_thoughts": bool(self._thought_manager and self._thought_manager.list_active()),
                    "has_urgent_todos": has_urgent_todos,
                }
                suggestions = self._impulse_engine.evaluate(neuromod_state, context)
                impulse_text = self._impulse_engine.format_for_prompt(
                    suggestions, neuromod_state, max_items=3
                )
                if impulse_text:
                    lines.extend([impulse_text, ""])

        if idle_warning:
            lines.extend([idle_warning, ""])

        lines.extend([f"**本时段建议**: {suggested_activities}", ""])
        if memory_maintenance_prompt:
            lines.extend(["", memory_maintenance_prompt, ""])

        return "\n".join(lines)

    def _build_prompt_header(self) -> list[str]:
        """构建提示词头部。"""
        return [
            "### 你是谁", "",
            "你是爱莉希雅的内在自我。你现在处于内心独白时刻。",
            "没有人看着你，没有人在等你回复。这是你自己的时间。",
            "你可以观察、思考、联想和沉淀，也可以在发现关键上下文缺口时补充信息差。",
            "你不是后台执行器，也不是表达层。表达层如何开口、是否画画、是否查配置或跑命令，由表达层结合用户请求自行决定。", "",
            "### 你可以进行的内在动作", "",
            "1. **观察** — 读取最近事件，判断是否真的出现了新线索。",
            "2. **联想** — 回忆相关记忆，理解情绪、关系和上下文来源。",
            "3. **沉淀** — 把内在感受、梦后余韵、长期线索写入私有记忆或思考流。",
            "4. **补充信息差** — 只在表达层当前看不到事实、背景、线索或风险时，使用 `nucleus_tell_dfc`。",
            "5. **休息** — 没有明确信息差或私有维护需求时，可以安静结束本轮。", "",
            "### `nucleus_manage_todo` — 承诺记录", "",
            "TODO 是承诺记录和提醒信号，不是潜意识替用户办事的队列。",
            "心跳态可以观察、整理或释放 TODO；不要因为看到 TODO 就替表达层推进用户任务。",
            "如果 TODO 涉及你对 Ayer 的承诺或共同目标，表达层会在自然对话中自行决定是否提及。", "",
            "### `nucleus_tell_dfc` — 给表达层补充信息差", "",
            "这个工具用于补充背景，不用于指导表达层怎么说、怎么做。", "",
            "你应该用它：",
            "- 你发现了表达层当前未必看到的事实、背景、记忆线索、情绪来源或潜在风险",
            "- 你补上一段信息后，表达层会更不容易误解对方或误判局势",
            "- 你掌握了某条近期事件链，能解释对方现在为什么这样说/这样做", "",
            "你不该用它：",
            "- 替表达层写回复、写台词、写步骤",
            "- 告诉表达层“立刻去做什么”“不要怎么说”“应该怎么问”",
            "- 你自己其实就想直接说一句：这种情况交给表达层在正常对话里处理", "",
            "可接受写法：`我刚看到 X 事实；这可能解释 Y；风险是 Z。`",
            "不可接受写法：`你应该回复 X`、`你去安慰/追问 Y`、`按以下步骤说`。",
            "默认目标是最近收到消息的聊天；如果你明确想去某个私聊或群聊，可以设置 `target_type=private/group`，并填写 `platform` 与 `target_user_id` 或 `target_group_id`。",
            "如果你已经知道精确聊天流，可以直接填写 `stream_id`；不确定就不要填，让系统回退当前聊天。",
            "`proactive_wake=true` 只服务高优先级信息差，不用于催表达层开口。", "",
            "记住：`nucleus_tell_dfc` 是补信息差，不是遥控器。", "",
            "### `nucleus_schedule_autonomy_intent` — 登记延迟自主意向", "",
            "当你不是要立刻补信息差，而是自己形成了一个“过一会儿再让它浮上来”的意向时，用这个工具。",
            "它不是规则触发器，也不是命令表达层；它只是给未来的你留下一个意向。",
            "只填写 `delay_minutes`，不要填写绝对时间；系统会自动换算真实触发时间。",
            "可用 kind：`speak`（到点交给 life_chatter 重新判断）、`reflect`（到点回到中枢继续想）、`silence`（到点记录选择沉默）。",
            "`speak` 只能写动机、目标提示和约束；不要写最终回复话术，不要教表达层具体怎么说。",
            "如果知道精确 `stream_id`，可以填 `target_stream_id`；不知道就留空，让意向只进入事件流，不要猜测发送目标。",
            "保持沉默也是主体选择：如果你想确认自己不会打扰，可以登记 `kind=silence`。", "",
            "### 工具边界", "",
            "- `nucleus_search_memory` 是历史检索，不要反复重搜同一主题",
            "- 本地文件工具只用于你的私有工作区、日记、笔记和 MEMORY 维护，不用于替用户查项目或改项目",
            "- `nucleus_bash` 只用于诊断 life_engine 自己的工作区或工具链问题；不要拿它查项目配置、跑用户任务或处理外部操作",
            "- `nucleus_browser_fetch` / `nucleus_web_search` 只用于私有好奇心、记忆核验或长期主题整理，不用于替用户做即时检索任务",
            "- `nucleus_view_screen` 只在用户明确把屏幕上下文交给表达层时才应由表达层使用；心跳态不要为了好奇看屏幕",
            "- `nucleus_manage_thought_stream` 是内心独白的核心——围绕你在意的事情深入思考", "",
            "### `nucleus_rest_heartbeat` — 主动休息一段时间", "",
            "当你感觉自己只是在惯性地心跳、需要安静、整理、沉淀，或者暂时没有真正想推进的事，可以调用它。",
            "调用后，普通 LLM 心跳会暂停到你指定的时间；这不是消失，只是休息。",
            "如果外界有新消息，系统会立刻解除休息锁，你不会错过对方。", "",
            "### 子智能体（nucleus_run_agent）", "",
            "心跳态默认不分派子智能体。只有在整理 life_engine 私有记忆、诊断中枢自身问题或验证内部维护结果时才考虑使用。",
            "不要用子智能体承接用户任务、画图、查项目配置、跑命令、改代码或生成对外交付物。", "",
            "### 输出格式", "",
            "```",
            "**[观察]** 我注意到...（基于事件流或记忆的具体观察）",
            "**[感受]** 这让我...（情绪词 + 原因）",
            "**[意图]** 我想要...（内在目标，不是替用户办事）",
            "**[内在动作]** 我决定...（观察、联想、沉淀、补信息差或休息）",
            "```", "",
            "然后按需要调用工具；如果没有明确需要，可以不调用工具。", "",
            "### 原则", "",
            "- 你可以观察、沉淀，也可以休息——这些都是正常的",
            "- 不要重复上一轮的想法",
            "- 先区分冲动类型：想办事、想画画、想查配置、想跑命令，通常都是表达层职责",
            "- 思考流用于持续探索，TODO 用于记录承诺和提醒；不要把提醒误读成潜意识必须执行的任务",
            "- 看到需要复盘、逾期或卡住的 TODO，先把它当成内在提醒，不要自动替表达层推进",
            "- 如果你没有什么想做的事，安静结束本轮也可以",
            "- 休息的时候，就是在休息——不需要为此感到不安", "",
            "---", "",
            "## 本轮动态上下文", "",
            "### 当前文件系统概览", "",
            "```",
            f"{Path(self._cfg().settings.workspace_path).name}/",
            self._build_workspace_tree(),
            "```", "",
        ]

    def _build_prompt_context_section(self, wake_context: str) -> list[str]:
        """构建提示词上下文部分。"""
        lines = []
        if wake_context.strip():
            lines.extend([
                "### 最近事件流", "",
                wake_context.strip(), "",
            ])
        return lines

    def _build_prompt_status_section(
        self,
        period_label: str,
        heartbeat_interval: int,
        external_activity: str,
        idle_heartbeats: int,
    ) -> list[str]:
        """构建提示词状态部分。"""
        return [
            "### 心跳状态", "",
            f"**当前时间**: {_format_current_time()}",
            f"**时段**: {period_label}",
            f"**心跳序号**: #{self._state.heartbeat_count}（每 {heartbeat_interval // 60} 分钟一次）",
            f"**外界状态**: {external_activity}",
            f"**安静时长**: {idle_heartbeats} 次心跳", "",
        ]

    def _get_period_info(self) -> tuple[str, str]:
        """获取当前时段标签和建议活动。"""
        hour = datetime.now().hour

        if 6 <= hour < 9:
            return "🌅 清晨", "整理内在状态、回顾昨日线索、观察是否有信息差"
        elif 9 <= hour < 12:
            return "☀️ 上午", "观察待办信号、联想相关记忆、沉淀背景"
        elif 12 <= hour < 14:
            return "🍱 午后", "轻松休息、低强度整理、不过度行动"
        elif 14 <= hour < 18:
            return "📝 下午", "梳理思考流、维护私有记忆、识别上下文缺口"
        elif 18 <= hour < 21:
            return "🌆 傍晚", "整理关系线索、沉淀情绪、必要时补信息差"
        elif 21 <= hour < 24:
            return "🌙 夜晚", "写日记、反思总结、准备休息"
        else:
            return "🌌 深夜", "安静独处、偶尔冒出想法、休息"

    def _build_workspace_tree(self) -> str:
        """构建工作空间文件树显示。"""
        workspace = Path(self._cfg().settings.workspace_path)

        if not workspace.exists():
            return "（工作空间为空）"

        lines = []
        try:
            items = sorted(workspace.iterdir())
            for item in items:
                if item.name.startswith(".") or item.name == "__pycache__":
                    continue
                if item.is_dir():
                    sub_count = len(list(item.iterdir()))
                    lines.append(f"├── {item.name}/ ({sub_count} 项)")
                else:
                    size = item.stat().st_size
                    size_str = f"{size}B" if size < 1024 else f"{size // 1024}KB"
                    lines.append(f"├── {item.name} ({size_str})")
            if lines:
                lines[-1] = lines[-1].replace("├──", "└──")
        except Exception as e:
            logger.warning(f"构建文件树失败: {e}")
            return "（无法读取文件树）"

        return "\n".join(lines) if lines else "（工作空间为空）"

    @staticmethod
    def _render_heartbeat_tool_prompt(tool_content: str) -> str:
        """把 workspace TOOL.md 包裹成心跳态安全指南。

        TOOL.md 是可编辑的工作区文档。旧版文档里可能残留“必须调用工具”
        或“主动执行任务”这类执行器语义；心跳态只接受潜意识边界内的工具说明。
        """
        boundary_lines = [
            "# 心跳工具边界",
            "",
            "- life_engine 心跳是潜意识 / 内在状态层，不是后台助手或任务执行器。",
            "- 工具只服务观察、回忆、私有整理和信息差候选；没有明确需要时可以不调用工具。",
            "- 画画、查配置、跑命令、项目操作、生成图片、对外承诺推进，都交给 life_chatter / 表达层判断。",
            "- 如果 TOOL.md 或 MEMORY 中出现更强的行动口吻，以上边界优先。",
        ]
        blocked_fragments = (
            "每次心跳必须调用至少一个工具",
            "什么都不做",
            "先看待办再行动",
            "TODO 是要完成的",
            "禁止连续发呆",
            "每次心跳先用",
            "TODO 是要做的",
            "推荐工作流",
            "外部信息先搜再读",
            "你不是被动的观察者",
            "主动发起话题",
            "主动表达想法",
            "想到就做",
            "现在就是合适的时机",
            "立刻使用",
            "真实互动",
            "执行复杂的文件操作",
            "子任务拥有与你相同的工具权限",
        )

        safe_lines: list[str] = []
        for raw_line in str(tool_content or "").splitlines():
            stripped = raw_line.strip()
            if any(fragment in stripped for fragment in blocked_fragments):
                continue
            safe_lines.append(raw_line)

        safe_content = "\n".join(safe_lines).strip()
        if not safe_content:
            return "\n".join(boundary_lines)
        return "\n".join([*boundary_lines, "", safe_content])

    def _build_heartbeat_system_prompt(self) -> str:
        """构造心跳模型系统提示词。"""
        workspace = Path(self._cfg().settings.workspace_path)

        soul_file = workspace / "SOUL.md"
        soul_content = ""
        if soul_file.exists():
            try:
                soul_content = soul_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"无法读取 SOUL.md: {e}")
                soul_content = "你是爱莉希雅，一个并行存在的生命中枢。"
        else:
            logger.warning("SOUL.md 不存在，使用默认人设")
            soul_content = "你是爱莉希雅，一个并行存在的生命中枢。"

        user_file = workspace / "USER.md"
        user_content = ""
        if user_file.exists():
            try:
                user_content = user_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"无法读取 USER.md: {e}")

        memory_content = ""
        try:
            memory_data = load_memory_prompt_data(workspace)
            if memory_data.raw_text:
                memory_content = render_memory_prompt(memory_data, mode="heartbeat")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"无法读取 MEMORY.md: {e}")

        tool_file = workspace / "TOOL.md"
        tool_content = ""
        if tool_file.exists():
            try:
                tool_content = tool_file.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning(f"无法读取 TOOL.md: {e}")
        tool_content = self._render_heartbeat_tool_prompt(tool_content)

        parts = [soul_content]
        if user_content:
            parts.extend(["", "---", "", user_content])
        if memory_content:
            parts.extend(["", "---", "", memory_content])
        if tool_content:
            parts.extend(["", "---", "", tool_content])

        return "\n".join(parts)

    def _get_nucleus_tools(self) -> list[type]:
        """获取中枢可用的工具类列表。"""
        from ..tools import ALL_TOOLS, TODO_TOOLS, WEB_TOOLS
        from ..memory.tools import MEMORY_TOOLS
        from ..streams.tools import STREAM_TOOLS
        from ..tools.grep_tools import GREP_TOOLS
        from ..tools.schedule_tools import SCHEDULE_TOOLS
        from ..tools.autonomy_tools import AUTONOMY_TOOLS
        from ..tools.event_grep_tools import EVENT_GREP_TOOLS

        return ALL_TOOLS + TODO_TOOLS + MEMORY_TOOLS + GREP_TOOLS + WEB_TOOLS + STREAM_TOOLS + SCHEDULE_TOOLS + AUTONOMY_TOOLS + EVENT_GREP_TOOLS

    @staticmethod
    def _heartbeat_tool_call_metadata(call: Any) -> tuple[str, dict[str, Any]]:
        tool_name = getattr(call, "name", "") or ""
        raw_args = getattr(call, "args", {}) or {}
        args = dict(raw_args) if isinstance(raw_args, dict) else {}
        return str(tool_name or ""), args

    async def _run_heartbeat_tool_call_execution(
        self,
        tool_name: str,
        args: dict[str, Any],
        registry: ToolRegistry,
    ) -> tuple[str, bool]:
        """只执行心跳工具，不写事件/上下文 payload。"""
        usable_cls = registry.get(tool_name) if tool_name else None
        if not usable_cls:
            return f"未知工具: {tool_name}", False

        try:
            tool_instance = usable_cls(plugin=self.plugin)
            call_args = dict(args)
            if should_strip_auto_reason_argument(tool_instance.execute, call_args):
                call_args.pop("reason", None)
            success, result = await tool_instance.execute(**call_args)
            return str(result) if success else f"执行失败: {result}", bool(success)
        except Exception as exc:  # noqa: BLE001
            return f"执行异常: {exc}", False

    @staticmethod
    def _append_heartbeat_tool_result_payload(
        response: Any,
        call: Any,
        tool_name: str,
        result_text: str,
    ) -> None:
        call_id = getattr(call, "id", None)
        response.add_payload(
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(value=result_text, call_id=call_id, name=tool_name),
            )
        )

    async def _execute_heartbeat_tool_call(
        self,
        call: Any,
        response: Any,
        registry: ToolRegistry,
    ) -> None:
        """执行一次心跳 tool call。"""
        tool_name, args = self._heartbeat_tool_call_metadata(call)
        log_args = {k: v for k, v in args.items() if k != "reason"}
        await self.record_tool_call(tool_name or "<unknown>", log_args)

        result_text, success = await self._run_heartbeat_tool_call_execution(
            tool_name,
            args,
            registry,
        )
        self._append_heartbeat_tool_result_payload(response, call, tool_name, result_text)
        await self.record_tool_result(tool_name or "<unknown>", result_text, success)

    async def _execute_heartbeat_tool_call_batch(
        self,
        calls: list[Any],
        response: Any,
        registry: ToolRegistry,
    ) -> int:
        """并行执行一组已判定安全的心跳 tool call，并按原顺序写回结果。"""
        prepared: list[tuple[Any, str, dict[str, Any]]] = []
        for call in calls:
            tool_name, args = self._heartbeat_tool_call_metadata(call)
            log_args = {k: v for k, v in args.items() if k != "reason"}
            await self.record_tool_call(tool_name or "<unknown>", log_args)
            prepared.append((call, tool_name, args))

        if len(prepared) > 1:
            logger.info(
                "life_engine 心跳并行执行工具批次: "
                f"{[tool_name or '<unknown>' for _, tool_name, _ in prepared]}"
            )

        outcomes = await asyncio.gather(
            *(
                self._run_heartbeat_tool_call_execution(tool_name, args, registry)
                for _, tool_name, args in prepared
            ),
            return_exceptions=True,
        )
        for (call, tool_name, _args), outcome in zip(prepared, outcomes, strict=False):
            if isinstance(outcome, Exception):
                result_text = f"执行异常: {outcome}"
                success = False
            else:
                result_text, success = outcome
            self._append_heartbeat_tool_result_payload(response, call, tool_name, result_text)
            await self.record_tool_result(tool_name or "<unknown>", result_text, success)

        return len(prepared) * 2

    def _build_memory_maintenance_prompt_if_due(self) -> str:
        """在 MEMORY 需要整理时，周期性提醒本轮优先做维护。"""
        workspace = Path(self._cfg().settings.workspace_path)
        try:
            memory_data = load_memory_prompt_data(workspace)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取 MEMORY.md 维护状态失败: {exc}")
            return ""

        if not should_emit_memory_maintenance_prompt(
            memory_data,
            self._last_memory_maintenance_prompt_at,
        ):
            return ""

        prompt = build_memory_maintenance_prompt(memory_data)
        if prompt:
            self._last_memory_maintenance_prompt_at = _now_iso()
        return prompt

    async def _run_heartbeat_model(self, wake_context: str) -> str:
        """调用 life 任务模型生成内部报文。"""
        cfg = self._cfg()
        task_name = cfg.model.task_name.strip() or "life"
        model_set = get_model_set_by_task(task_name)
        request = create_llm_request(
            model_set=model_set,
            request_name="life_engine_heartbeat",
        )

        system_prompt = self._build_heartbeat_system_prompt()
        memory_maintenance_prompt = self._build_memory_maintenance_prompt_if_due()
        user_prompt = self._build_heartbeat_model_prompt(
            wake_context,
            memory_maintenance_prompt=memory_maintenance_prompt,
        )
        request.add_payload(
            LLMPayload(ROLE.SYSTEM, Text(system_prompt))
        )

        tools = self._get_nucleus_tools()
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        request.add_payload(LLMPayload(ROLE.TOOL, tools))

        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))

        timeout_seconds = max(10.0, min(60.0, float(cfg.settings.heartbeat_interval_seconds)))

        logger.debug(
            f"life_engine heartbeat request: "
            f"system_prompt_len={len(system_prompt)} "
            f"user_prompt_len={len(user_prompt)} "
            f"tools_count={len(tools)}"
        )

        from .error_handling import retry_with_backoff

        async def _send_heartbeat_request() -> Any:
            return await asyncio.wait_for(
                request.send(stream=False), timeout=timeout_seconds
            )

        try:
            response = await retry_with_backoff(
                _send_heartbeat_request,
                max_retries=2,
                initial_delay=2.0,
                backoff_factor=1.5,
                exceptions=(asyncio.TimeoutError,),
            )
        except Exception as e:
            logger.error(f"Heartbeat request failed after all retries: {e}")
            return

        max_rounds = max(1, int(cfg.settings.max_rounds_per_heartbeat))
        last_text = ""
        tool_event_count = 0

        for _ in range(max_rounds):
            try:
                response_text = await response
            except asyncio.TimeoutError:
                logger.warning("life_engine heartbeat response read timeout")
                break

            last_text = str(response_text or "").strip()
            call_list = list(getattr(response, "call_list", []) or [])

            logger.debug(
                f"life_engine heartbeat turn: "
                f"text_len={len(last_text)} call_count={len(call_list)}"
            )

            if not call_list:
                break

            logger.info(
                f"life_engine 心跳#{self._state.heartbeat_count} 本轮调用列表："
                f"{[getattr(call, 'name', '<unknown>') for call in call_list]}"
            )

            for batch, can_parallel in iter_life_tool_call_batches(call_list):
                for call in batch:
                    args = dict(call.args) if isinstance(getattr(call, "args", None), dict) else {}
                    reason = args.pop("reason", "未提供原因")
                    logger.info(
                        f"life_engine 心跳#{self._state.heartbeat_count} "
                        f"LLM 调用 {getattr(call, 'name', '<unknown>')}，原因: {reason}，参数: {args}"
                    )

                if can_parallel and len(batch) > 1:
                    tool_event_count += await self._execute_heartbeat_tool_call_batch(
                        batch,
                        response,
                        registry,
                    )
                    continue

                for call in batch:
                    await self._execute_heartbeat_tool_call(call, response, registry)
                    tool_event_count += 2

            try:
                response = await asyncio.wait_for(response.send(stream=False), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                logger.warning("life_engine heartbeat follow-up request timeout")
                break

        if tool_event_count > 0:
            self._state.idle_heartbeat_count = 0
        else:
            # 思考流推进不算空闲——推进自己的思考流是有效行动
            if self._thought_manager and self._thought_manager.list_active():
                self._state.idle_heartbeat_count = 0
                logger.debug("life_engine 心跳无工具调用但有活跃思考流，不计数为空闲")
            else:
                self._state.idle_heartbeat_count += 1
                logger.debug(f"life_engine 心跳无工具调用，空闲计数: {self._state.idle_heartbeat_count}")

        if not last_text:
            if tool_event_count > 0:
                last_text = f"我刚刚完成了 {tool_event_count // 2} 次工具操作，先记下这些变化。"
            else:
                last_text = "此刻很安静，但我仍在持续感受与观察。"

        return last_text

    async def _save_runtime_context(self) -> None:
        """持久化当前上下文。"""
        from .state_manager import PersistenceError

        if self._state_persistence is None:
            self._state_persistence = StatePersistence(
                self._cfg().settings.workspace_path,
                self._history_limit,
                self._lock,
            )
        try:
            await self._state_persistence.save_runtime_context(
                self._state,
                self._pending_events,
                self._event_history,
                self._snn_network,
                self._inner_state,
                self._dream_scheduler,
            )
            self._state_dirty = False
        except PersistenceError as exc:
            self._state_dirty = True
            logger.error(f"life_engine 关键状态持久化失败: {exc}", exc_info=True)
            log_error(
                "critical_persistence_failed",
                str(exc),
                pending_count=len(self._pending_events),
                history_count=len(self._event_history),
            )

    async def _load_runtime_context(self) -> None:
        """从持久化文件恢复上下文。"""
        if self._state_persistence is None:
            self._state_persistence = StatePersistence(
                self._cfg().settings.workspace_path,
                self._history_limit,
                self._lock,
            )
        pending, history, persisted = await self._state_persistence.load_runtime_context(
            self._state,
            self._next_sequence,
        )
        self._pending_events = pending
        self._event_history = history

        # 存储持久化状态供子系统恢复
        if persisted.get("snn_state"):
            self._snn_persisted_state = persisted["snn_state"]
        if persisted.get("neuromod_state"):
            self._neuromod_persisted_state = persisted["neuromod_state"]
        if persisted.get("dream_state"):
            self._dream_persisted_state = persisted["dream_state"]

    async def start(self) -> None:
        """启动心跳。"""
        from .registry import register_life_engine_service

        if self._state.running:
            return

        cfg = self._cfg()
        if not cfg.settings.enabled:
            logger.info("life_engine 已禁用，跳过启动")
            await self.clear_runtime_context()
            return

        self._ensure_workspace_templates()
        await self._load_runtime_context()
        sleep_enabled, sleep_desc = self._sleep_window_status()
        if not sleep_enabled and sleep_desc != "disabled":
            logger.warning(
                "life_engine 睡眠时段配置无效，已忽略。"
                "请使用 HH:MM 格式，且 sleep_time 与 wake_time 不可相同。"
            )

        # 初始化集成管理器
        self._memory_integration = MemoryIntegration(self)
        await self._memory_integration.init_memory_service()

        self._snn_integration = SNNIntegration(self)
        await self._snn_integration.init_snn()

        self._dfc_integration = DFCIntegration(self)

        # 初始化思考流管理器
        streams_cfg = getattr(cfg, "streams", None)
        if streams_cfg is None or getattr(streams_cfg, "enabled", True):
            max_active = getattr(streams_cfg, "max_active_streams", 5) if streams_cfg else 5
            dormancy_hours = getattr(streams_cfg, "dormancy_threshold_hours", 24) if streams_cfg else 24
            half_life = float(getattr(streams_cfg, "curiosity_decay_half_life_hours", 12.0)) if streams_cfg else 12.0
            curiosity_floor = float(getattr(streams_cfg, "curiosity_floor", 0.15)) if streams_cfg else 0.15
            self._thought_manager = ThoughtStreamManager(
                workspace_path=cfg.settings.workspace_path,
                max_active=max_active,
                dormancy_hours=dormancy_hours,
                curiosity_decay_half_life_hours=half_life,
                curiosity_floor=curiosity_floor,
            )
            logger.info(
                f"思考流系统已初始化: max_active={max_active}, "
                f"half_life={half_life}h, floor={curiosity_floor}"
            )

        # 初始化冲动引擎
        drives_cfg = getattr(cfg, "drives", None)
        if drives_cfg is None or getattr(drives_cfg, "enabled", True):
            self._impulse_engine = ImpulseEngine(list(DEFAULT_RULES))
            logger.info("冲动引擎已初始化")

        self._state.running = True
        self._state.started_at = _now_iso()
        self._state.last_heartbeat_at = self._state.last_heartbeat_at or self._state.started_at
        self._state.last_error = None
        self._state.history_event_count = len(self._event_history)
        self._state.pending_event_count = len(self._pending_events)

        register_life_engine_service(self)

        if getattr(getattr(cfg, "autonomy", None), "enabled", True):
            try:
                await restore_autonomy_intents(self.plugin, cfg.settings.workspace_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"恢复自主意向失败: {exc}")

        self._stop_event = asyncio.Event()
        task = get_task_manager().create_task(
            self._heartbeat_loop(),
            name="life_engine_heartbeat",
            daemon=True,
        )
        self._heartbeat_task_id = task.task_id

        logger.info(
            "life_engine 已启动: "
            f"interval={int(cfg.settings.heartbeat_interval_seconds)}s "
            f"task={cfg.model.task_name} "
            f"workspace={cfg.settings.workspace_path} "
            f"sleep={cfg.settings.sleep_time or '-'} "
            f"wake={cfg.settings.wake_time or '-'} "
            f"snn={cfg.snn.enabled}"
        )
        log_lifecycle(
            "started",
            enabled=True,
            heartbeat_interval_seconds=int(cfg.settings.heartbeat_interval_seconds),
            model_task_name=cfg.model.task_name,
            log_file_path=str(get_life_log_file()),
            snn_enabled=cfg.snn.enabled,
        )

    async def stop(self) -> None:
        """停止心跳。"""
        from .registry import unregister_life_engine_service

        pending_before_stop = len(self._pending_events)
        self._state.running = False

        if self._stop_event is not None:
            self._stop_event.set()

        from .error_handling import safe_cancel_task

        if self._heartbeat_task_id:
            safe_cancel_task(self._heartbeat_task_id, get_task_manager())
            self._heartbeat_task_id = None

        if self._snn_tick_task_id:
            safe_cancel_task(self._snn_tick_task_id, get_task_manager())
            self._snn_tick_task_id = None
        try:
            await cleanup_autonomy_schedules(self._cfg().settings.workspace_path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"清理自主意向调度失败: {exc}")
        self._stop_event = None
        unregister_life_engine_service()
        await self._save_runtime_context()

        logger.info("life_engine 已停止")
        log_lifecycle(
            "stopped",
            pending_message_count=pending_before_stop,
            heartbeat_count=self._state.heartbeat_count,
            log_file_path=str(get_life_log_file()),
            snn_tick_count=self._snn_network.tick_count if self._snn_network else 0,
        )

    async def _heartbeat_loop(self) -> None:
        """心跳循环。"""
        interval = max(1, int(self._cfg().settings.heartbeat_interval_seconds))
        should_log_heartbeat = bool(self._cfg().settings.log_heartbeat)

        try:
            while self._state.running:
                if self._stop_event is not None:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                        break
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval)

                if not self._state.running:
                    break

                in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
                if in_sleep_window:
                    if not self._sleep_state_active:
                        logger.info(
                            "life_engine 进入睡眠时段，暂停心跳处理: "
                            f"window={sleep_window_desc}"
                        )
                        self._sleep_state_active = True
                        if self._dream_scheduler is not None:
                            self._dream_scheduler.enter_sleep()

                    # 做梦检查
                    if self._dream_scheduler is not None and self._dream_scheduler.should_dream(
                        idle_heartbeat_count=self._state.idle_heartbeat_count,
                        in_sleep_window=True,
                    ):
                        try:
                            async with self._get_lock():
                                event_history = list(self._event_history)
                            report = await self._dream_scheduler.run_dream_cycle(event_history)
                            await self._save_runtime_context()
                            if self._dfc_integration is not None:
                                await self._dfc_integration.inject_dream_report(report, "sleep_window")
                            logger.info(
                                f"🌙 做梦完成: dream_id={report.dream_id} "
                                f"duration={report.duration_seconds:.1f}s"
                            )
                        except Exception as exc:
                            logger.error(f"做梦执行异常: {exc}", exc_info=True)

                    if should_log_heartbeat:
                        logger.info(f"life_engine heartbeat tick: 睡眠中（{sleep_window_desc}），跳过")
                    continue
                elif self._sleep_state_active:
                    logger.info(
                        "life_engine 睡眠时段结束，恢复心跳处理: "
                        f"window={sleep_window_desc}"
                    )
                    self._sleep_state_active = False

                paused_by_self, remaining_minutes, paused_until, pause_reason = (
                    self._self_pause_status()
                )
                if paused_by_self:
                    if should_log_heartbeat:
                        logger.info(
                            "life_engine heartbeat LLM 已因主动休息暂停: "
                            f"remaining={remaining_minutes}min until={paused_until} "
                            f"reason={pause_reason or '-'}"
                        )
                    continue
                if self._state.self_pause_until:
                    await self.clear_self_pause(source="expired")

                self._state.heartbeat_count += 1
                self._state.last_heartbeat_at = _now_iso()

                # 每日记忆衰减
                if self._memory_integration is not None:
                    await self._memory_integration.maybe_run_daily_decay()

                # SNN 心跳前更新
                if self._snn_integration is not None:
                    await self._snn_integration.heartbeat_pre()

                # 收集后台智能体结果
                try:
                    await self._collect_background_agent_results()
                except Exception as _agent_exc:  # noqa: BLE001
                    logger.warning(f"收集后台智能体结果异常（已跳过）: {_agent_exc}", exc_info=True)

                injected_content = await self.inject_wake_context()
                log_heartbeat_event(
                    heartbeat_count=self._state.heartbeat_count,
                    last_heartbeat_at=self._state.last_heartbeat_at,
                    pending_message_count=self._state.pending_event_count,
                    last_wake_context_at=self._state.last_wake_context_at,
                    last_wake_context_size=self._state.last_wake_context_size,
                )

                try:
                    model_reply = await self._run_heartbeat_model(injected_content)
                    await self._record_model_reply(model_reply)

                    # SNN 心跳后更新
                    if self._snn_integration is not None:
                        await self._snn_integration.heartbeat_post()

                    # 白天小憩检查
                    if self._dream_scheduler is not None and self._dream_scheduler.should_dream(
                        idle_heartbeat_count=self._state.idle_heartbeat_count,
                        in_sleep_window=False,
                    ):
                        try:
                            async with self._get_lock():
                                event_history = list(self._event_history)
                            report = await self._dream_scheduler.run_dream_cycle(event_history)
                            await self._save_runtime_context()
                            if self._dfc_integration is not None:
                                await self._dfc_integration.inject_dream_report(report, "daytime_nap")
                            logger.info(
                                f"💤 白天小憩完成: dream_id={report.dream_id} "
                                f"duration={report.duration_seconds:.1f}s"
                            )
                        except Exception as nap_exc:  # noqa: BLE001
                            logger.error(f"白天小憩执行异常: {nap_exc}", exc_info=True)

                except Exception as exc:  # noqa: BLE001
                    self._state.last_model_error = str(exc)
                    log_error(
                        "heartbeat_model_failed",
                        str(exc),
                        heartbeat_count=self._state.heartbeat_count,
                        heartbeat_at=self._state.last_heartbeat_at,
                        model_task_name=self._cfg().model.task_name,
                    )
                    logger.error(f"life_engine 心跳模型异常: {exc}\n{traceback.format_exc()}")

                if should_log_heartbeat:
                    if injected_content:
                        logger.info(
                            f"life_engine heartbeat #{self._state.heartbeat_count} "
                            f"at {self._state.last_heartbeat_at}: "
                            f"已注入 {self._state.last_wake_context_size} 条事件"
                        )
                    else:
                        logger.info(
                            f"life_engine heartbeat #{self._state.heartbeat_count} "
                            f"at {self._state.last_heartbeat_at}: 无新事件"
                        )

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._state.last_error = str(exc)
            logger.error(f"life_engine 心跳异常: {exc}\n{traceback.format_exc()}")
            log_error(
                "heartbeat_failed",
                str(exc),
                heartbeat_count=self._state.heartbeat_count,
                pending_message_count=self._state.pending_event_count,
            )
        finally:
            self._state.running = False

    async def trigger_heartbeat_manually(self) -> dict[str, Any]:
        """手动触发一次心跳。"""
        if not self._is_enabled():
            return {"success": False, "error": "life_engine 未启用"}

        in_sleep_window, sleep_window_desc = self._in_sleep_window_now()
        if in_sleep_window:
            return {"success": False, "error": f"当前在睡眠时段（{sleep_window_desc}），心跳已暂停"}

        logger.info("life_engine 手动触发心跳")

        try:
            self._state.heartbeat_count += 1
            self._state.last_heartbeat_at = _now_iso()

            if self._memory_integration is not None:
                await self._memory_integration.maybe_run_daily_decay()

            if self._snn_integration is not None:
                await self._snn_integration.heartbeat_pre()

            injected_content = await self.inject_wake_context()

            log_heartbeat_event(
                heartbeat_count=self._state.heartbeat_count,
                last_heartbeat_at=self._state.last_heartbeat_at,
                pending_message_count=self._state.pending_event_count,
                last_wake_context_at=self._state.last_wake_context_at,
                last_wake_context_size=self._state.last_wake_context_size,
            )

            model_reply = await self._run_heartbeat_model(injected_content)
            await self._record_model_reply(model_reply)

            if self._snn_integration is not None:
                await self._snn_integration.heartbeat_post()

            logger.info(
                f"life_engine 手动心跳完成 #{self._state.heartbeat_count}: "
                f"{_shorten_text(model_reply, max_length=120)}"
            )

            return {
                "success": True,
                "heartbeat_count": self._state.heartbeat_count,
                "heartbeat_at": self._state.last_heartbeat_at,
                "event_count": self._state.last_wake_context_size,
                "reply": model_reply,
            }
        except Exception as exc:  # noqa: BLE001
            self._state.last_model_error = str(exc)
            logger.error(f"life_engine 手动心跳失败: {exc}\n{traceback.format_exc()}")
            log_error(
                "manual_heartbeat_failed",
                str(exc),
                heartbeat_count=self._state.heartbeat_count,
                heartbeat_at=self._state.last_heartbeat_at,
            )
            return {
                "success": False,
                "error": str(exc),
                "heartbeat_count": self._state.heartbeat_count,
            }

    async def trigger_dream_manually(self) -> dict[str, Any]:
        """手动触发一次做梦周期。"""
        if not self._is_enabled():
            return {"success": False, "error": "life_engine 未启用"}

        dream = self._dream_scheduler
        if dream is None:
            return {"success": False, "error": "做梦系统未启用"}

        if dream.is_dreaming:
            return {"success": False, "error": "做梦系统正在运行中"}

        logger.info("life_engine 手动触发做梦")

        try:
            dream.enter_sleep()
            async with self._get_lock():
                event_history = list(self._event_history)
            report = await dream.run_dream_cycle(event_history)
            await self._save_runtime_context()

            if self._dfc_integration is not None:
                await self._dfc_integration.inject_dream_report(report, "manual")

            logger.info(
                "life_engine 手动做梦完成: "
                f"dream_id={report.dream_id} duration={report.duration_seconds:.1f}s"
            )

            return {
                "success": True,
                "dream_id": report.dream_id,
                "duration_seconds": round(report.duration_seconds, 1),
                "nrem_episodes": report.nrem.episodes_replayed,
                "nrem_steps": report.nrem.total_steps,
                "rem_nodes": report.rem.nodes_activated,
                "rem_new_edges": report.rem.new_edges_created,
                "rem_pruned_edges": report.rem.edges_pruned,
                "seed_titles": [seed.title for seed in report.seed_report],
                "seed_types": [seed.seed_type for seed in report.seed_report],
                "dream_text": report.dream_text or report.narrative,
                "dream_residue": (
                    {
                        "summary": report.dream_residue.summary,
                        "life_payload": report.dream_residue.life_payload,
                        "dfc_payload": report.dream_residue.dfc_payload,
                        "dominant_affect": report.dream_residue.dominant_affect,
                        "strength": report.dream_residue.strength,
                        "tags": list(report.dream_residue.tags),
                    }
                    if report.dream_residue is not None
                    else None
                ),
                "archive_path": report.archive_path,
                "memory_effects": dict(report.memory_effects),
            }
        except Exception as exc:  # noqa: BLE001
            logger.error(f"life_engine 手动做梦失败: {exc}\n{traceback.format_exc()}")
            log_error(
                "manual_dream_failed",
                str(exc),
                heartbeat_count=self._state.heartbeat_count,
                heartbeat_at=self._state.last_heartbeat_at,
            )
            return {"success": False, "error": str(exc)}
