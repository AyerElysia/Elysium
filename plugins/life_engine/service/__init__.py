"""life_engine 心跳服务模块。"""

from __future__ import annotations

from .core import LifeEngineService
from .event_builder import (
    EventType,
    LifeEngineEvent,
    LifeEngineState,
    EventBuilder,
    _now_iso,
    _format_time,
    _format_time_display,
    _format_current_time,
    _shorten_text,
    _parse_hhmm,
)
from .state_manager import (
    StatePersistence,
    event_to_dict,
    event_from_dict,
    compress_history,
    clear_wake_context_reminder,
    get_file_metadata,
)
from .attention import AttentionRouter, AttentionWindow
from .subconscious_context import (
    EventGroup,
    PreparedHeartbeatContext,
    SubconsciousContextManager,
    SubconsciousSummary,
    SummaryEntry,
)
from .event_bus import (
    LifeEvent,
    LifeEventBus,
    LifeEventChannel,
    LifeEventPriority,
    RawEventStore,
    life_event_from_legacy,
)
from .integrations import (
    MemoryIntegration,
    to_jsonable,
)

__all__ = [
    # 核心服务
    "LifeEngineService",
    # 事件模型
    "EventType",
    "LifeEngineEvent",
    "LifeEngineState",
    "EventBuilder",
    "_now_iso",
    "_format_time",
    "_format_time_display",
    "_format_current_time",
    "_shorten_text",
    "_parse_hhmm",
    "LifeEvent",
    "LifeEventBus",
    "LifeEventChannel",
    "LifeEventPriority",
    "RawEventStore",
    "life_event_from_legacy",
    "AttentionRouter",
    "AttentionWindow",
    "SummaryEntry",
    "SubconsciousSummary",
    "EventGroup",
    "PreparedHeartbeatContext",
    "SubconsciousContextManager",
    # 状态管理
    "StatePersistence",
    "event_to_dict",
    "event_from_dict",
    "compress_history",
    "clear_wake_context_reminder",
    "get_file_metadata",
    # 集成管理器
    "MemoryIntegration",
    "to_jsonable",
]
