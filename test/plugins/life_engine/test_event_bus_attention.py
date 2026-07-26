"""Unified life event bus and attention router tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.life_engine.service.attention import AttentionRouter
from plugins.life_engine.service.event_builder import (
    EventType,
    LifeEngineEvent,
    is_life_heartbeat_event,
)
from plugins.life_engine.service.event_bus import (
    RAW_EVENT_LOG_FILE,
    RawEventStore,
    life_event_from_legacy,
)


def _event(
    sequence: int,
    *,
    content: str = "",
    source: str = "qq",
    content_type: str = "text",
    stream_id: str = "stream-a",
    event_type: EventType = EventType.MESSAGE,
    tool_success: bool | None = None,
    heartbeat_run_id: str | None = None,
    call_id: str | None = None,
    parent_event_id: str | None = None,
    causation_id: str | None = None,
) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=f"evt-{sequence}",
        event_type=event_type,
        timestamp="2026-04-25T22:00:00+08:00",
        sequence=sequence,
        source=source,
        source_detail=f"{source} | test",
        content=content or f"event-{sequence}",
        content_type=content_type,
        sender="Ayer",
        chat_type="private",
        stream_id=stream_id,
        heartbeat_run_id=heartbeat_run_id,
        call_id=call_id,
        parent_event_id=parent_event_id,
        causation_id=causation_id,
        tool_name="nucleus_read_file" if event_type == EventType.TOOL_RESULT else None,
        tool_success=tool_success,
    )


async def test_raw_event_store_appends_and_reads_since(tmp_path: Path) -> None:
    store = RawEventStore(tmp_path)
    first = life_event_from_legacy(_event(1, content="first"))
    second = life_event_from_legacy(_event(2, content="second"))

    await store.append(first)
    await store.append(second)

    assert (tmp_path / RAW_EVENT_LOG_FILE).exists()
    assert [event.content for event in await store.read_since(1)] == ["second"]
    assert [event.sequence for event in await store.read_tail(2)] == [1, 2]


def test_attention_router_summarizes_low_salience_flood() -> None:
    events = [
        _event(i, content=f"普通弹幕 {i}", source="live", stream_id="live-1")
        for i in range(1, 101)
    ]
    events.append(
        _event(
            101,
            content="Ayer 直接问你一个重要问题",
            content_type="direct_message",
            stream_id="stream-private",
        )
    )

    window = AttentionRouter(max_events=10, max_chars=1200).select(events)
    rendered = "\n".join(event.content for event in window.events)

    assert window.high_water == 101
    assert window.dropped_count == 91
    assert len(window.events) == 11
    assert window.summary_events[0].event_type == EventType.SUMMARY
    assert window.context_char_count <= 1200
    assert "潜意识已压缩低显著事件" in rendered
    assert "Ayer 直接问你一个重要问题" in rendered


def test_attention_prefers_latest_event_on_equal_salience() -> None:
    window = AttentionRouter(max_events=1, max_chars=500).select(
        [
            _event(1, content="old"),
            _event(2, content="new"),
        ]
    )

    assert [event.sequence for event in window.selected_events] == [2]


def test_attention_strictly_counts_summary_in_budget() -> None:
    window = AttentionRouter(max_events=10, max_chars=120).select(
        [_event(i, content=f"flood-{i}", source="live") for i in range(1, 20)]
    )

    assert window.context_char_count <= 120


def test_raw_metadata_preserves_causality_and_summary_channel() -> None:
    event = _event(
        9,
        event_type=EventType.SUMMARY,
        content="summary",
        content_type="subconscious_summary",
        heartbeat_run_id="run-9",
        call_id="call-9",
        parent_event_id="parent-9",
        causation_id="cause-9",
    )

    raw = life_event_from_legacy(event)

    assert raw.channel == "system"
    assert raw.event_type == "subconscious_summary"
    assert raw.metadata["heartbeat_run_id"] == "run-9"
    assert raw.metadata["call_id"] == "call-9"
    assert raw.metadata["parent_event_id"] == "parent-9"
    assert raw.metadata["causation_id"] == "cause-9"


def test_heartbeat_classifier_excludes_chatter_and_legacy_summary() -> None:
    real_heartbeat = _event(
        1,
        event_type=EventType.HEARTBEAT,
        source="life_engine",
        content_type="heartbeat_reply",
        content="中枢心跳",
    )
    chatter_monologue = _event(
        2,
        event_type=EventType.HEARTBEAT,
        source="life_chatter",
        content_type="chatter_inner_monologue",
        content="对话器独白",
    )
    legacy_summary = _event(
        3,
        event_type=EventType.HEARTBEAT,
        source="system",
        content_type="history_summary",
        content="旧摘要",
    )
    legacy_summary.heartbeat_index = -1

    assert is_life_heartbeat_event(real_heartbeat) is True
    assert is_life_heartbeat_event(chatter_monologue) is False
    assert is_life_heartbeat_event(legacy_summary) is False
