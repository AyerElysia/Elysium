"""Unified event bus primitives for life_engine.

The first version is intentionally compatibility-first: existing
``LifeEngineEvent`` callers continue to work, while every published event is
also mirrored into an append-only raw event log for future high-volume
channels.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

from .event_builder import EventType, LifeEngineEvent


RAW_EVENT_LOG_FILE = "life_events.jsonl"


class LifeEventPriority(IntEnum):
    LOW = 10
    NORMAL = 50
    HIGH = 80
    URGENT = 100


class LifeEventChannel(str, Enum):
    CHAT = "chat"
    LIFE = "life"
    TOOL = "tool"
    AGENT = "agent"
    PROACTIVE = "proactive"
    SYSTEM = "system"


@dataclass(slots=True)
class LifeEvent:
    """Channel-agnostic raw event for the unified consciousness pipeline."""

    event_id: str
    sequence: int
    timestamp: str
    source: str
    channel: str
    event_type: str
    content: str
    stream_id: str = ""
    reply_target: dict[str, Any] | None = None
    priority: int = int(LifeEventPriority.NORMAL)
    salience: float = 0.5
    ttl_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _legacy_channel(event: LifeEngineEvent) -> LifeEventChannel:
    content_type = str(event.content_type or "").strip().lower()
    if event.event_type == EventType.SUMMARY:
        return LifeEventChannel.SYSTEM
    if event.event_type in {EventType.TOOL_CALL, EventType.TOOL_RESULT}:
        return LifeEventChannel.TOOL
    if event.event_type == EventType.AGENT_RESULT:
        return LifeEventChannel.AGENT
    if content_type == "proactive_opportunity":
        return LifeEventChannel.PROACTIVE
    if content_type.startswith("autonomy_intent_"):
        return LifeEventChannel.LIFE
    if event.event_type == EventType.HEARTBEAT:
        return LifeEventChannel.LIFE
    if str(event.source or "").strip().lower() == "system":
        return LifeEventChannel.SYSTEM
    return LifeEventChannel.CHAT


def _legacy_priority_and_salience(event: LifeEngineEvent) -> tuple[int, float]:
    content_type = str(event.content_type or "").strip().lower()
    if event.event_type == EventType.TOOL_RESULT and event.tool_success is False:
        return int(LifeEventPriority.URGENT), 1.0
    if content_type in {"direct_message", "dfc_message"}:
        return int(LifeEventPriority.HIGH), 0.9
    if content_type == "proactive_opportunity":
        return int(LifeEventPriority.HIGH), 0.82
    if content_type == "autonomy_intent_due":
        return int(LifeEventPriority.HIGH), 0.86
    if content_type.startswith("autonomy_intent_"):
        return int(LifeEventPriority.NORMAL), 0.74
    if event.event_type == EventType.AGENT_RESULT:
        return int(LifeEventPriority.HIGH), 0.8
    if content_type == "chatter_inner_monologue":
        return int(LifeEventPriority.NORMAL), 0.72
    if event.event_type == EventType.MESSAGE:
        return int(LifeEventPriority.NORMAL), 0.6
    if event.event_type == EventType.TOOL_CALL:
        return int(LifeEventPriority.LOW), 0.25
    if event.event_type == EventType.TOOL_RESULT:
        return int(LifeEventPriority.LOW), 0.35
    return int(LifeEventPriority.LOW), 0.3


def life_event_from_legacy(event: LifeEngineEvent) -> LifeEvent:
    """Convert the existing service event to the new raw event model."""

    priority, salience = _legacy_priority_and_salience(event)
    stream_id = str(event.stream_id or "").strip()
    reply_target = None
    if stream_id:
        reply_target = {
            "stream_id": stream_id,
            "source": event.source,
            "chat_type": event.chat_type,
            "sender": event.sender,
        }

    metadata: dict[str, Any] = {
        "legacy_event_type": event.event_type.value,
        "source_detail": event.source_detail,
        "content_type": event.content_type,
    }
    if event.sender is not None:
        metadata["sender"] = event.sender
    if event.chat_type is not None:
        metadata["chat_type"] = event.chat_type
    if event.heartbeat_index is not None:
        metadata["heartbeat_index"] = event.heartbeat_index
    for field_name in (
        "heartbeat_run_id",
        "call_id",
        "parent_event_id",
        "causation_id",
    ):
        value = getattr(event, field_name, None)
        if value is not None:
            metadata[field_name] = value
    if event.tool_name is not None:
        metadata["tool_name"] = event.tool_name
    if event.tool_args is not None:
        metadata["tool_args"] = event.tool_args
    if event.tool_success is not None:
        metadata["tool_success"] = event.tool_success
    if event.heartbeat_context_consumed:
        metadata["heartbeat_context_consumed"] = True

    return LifeEvent(
        event_id=event.event_id,
        sequence=int(event.sequence or 0),
        timestamp=event.timestamp,
        source=event.source,
        channel=_legacy_channel(event).value,
        event_type=event.content_type or event.event_type.value,
        content=event.content or "",
        stream_id=stream_id,
        reply_target=reply_target,
        priority=priority,
        salience=salience,
        metadata=metadata,
    )


def life_event_to_dict(event: LifeEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp,
        "source": event.source,
        "channel": event.channel,
        "event_type": event.event_type,
        "content": event.content,
        "stream_id": event.stream_id,
        "reply_target": event.reply_target,
        "priority": int(event.priority),
        "salience": float(event.salience),
        "ttl_seconds": event.ttl_seconds,
        "metadata": event.metadata,
    }


def life_event_from_dict(data: dict[str, Any]) -> LifeEvent:
    return LifeEvent(
        event_id=str(data.get("event_id") or ""),
        sequence=int(data.get("sequence") or 0),
        timestamp=str(data.get("timestamp") or ""),
        source=str(data.get("source") or "unknown"),
        channel=str(data.get("channel") or LifeEventChannel.SYSTEM.value),
        event_type=str(data.get("event_type") or "unknown"),
        content=str(data.get("content") or ""),
        stream_id=str(data.get("stream_id") or ""),
        reply_target=data.get("reply_target") if isinstance(data.get("reply_target"), dict) else None,
        priority=int(data.get("priority") or int(LifeEventPriority.NORMAL)),
        salience=float(data.get("salience") or 0.0),
        ttl_seconds=(
            int(data["ttl_seconds"])
            if data.get("ttl_seconds") is not None
            else None
        ),
        metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
    )


class RawEventStore:
    """Append-only JSONL store for raw life events."""

    def __init__(self, workspace_path: str | Path, filename: str = RAW_EVENT_LOG_FILE) -> None:
        self._path = Path(workspace_path).resolve() / filename

    @property
    def path(self) -> Path:
        return self._path

    async def append(self, event: LifeEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(life_event_to_dict(event), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    async def append_many(self, events: list[LifeEvent]) -> None:
        if not events:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(life_event_to_dict(event), ensure_ascii=False) + "\n")

    async def read_tail(self, limit: int = 100) -> list[LifeEvent]:
        if limit <= 0 or not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return self._decode_lines(lines[-limit:])

    async def read_since(self, sequence: int, *, limit: int | None = None) -> list[LifeEvent]:
        if not self._path.exists():
            return []
        result: list[LifeEvent] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            for event in self._decode_lines([line]):
                if event.sequence > sequence:
                    result.append(event)
                    if limit is not None and len(result) >= limit:
                        return result
        return result

    @staticmethod
    def _decode_lines(lines: list[str]) -> list[LifeEvent]:
        events: list[LifeEvent] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                events.append(life_event_from_dict(raw))
        return events


class LifeEventBus:
    """Compatibility event bus that mirrors legacy events to raw storage."""

    def __init__(self, store: RawEventStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    @property
    def store(self) -> RawEventStore:
        return self._store

    async def publish(self, event: LifeEvent) -> LifeEvent:
        async with self._lock:
            await self._store.append(event)
        return event

    async def publish_many(self, events: list[LifeEvent]) -> list[LifeEvent]:
        if not events:
            return []
        async with self._lock:
            await self._store.append_many(events)
        return events

    async def publish_legacy_event(self, event: LifeEngineEvent) -> LifeEvent:
        return await self.publish(life_event_from_legacy(event))

    async def publish_legacy_events(self, events: list[LifeEngineEvent]) -> list[LifeEvent]:
        return await self.publish_many([life_event_from_legacy(event) for event in events])
