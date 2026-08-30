"""心跳唤醒上下文的表达层投递事实标注。"""

from __future__ import annotations

from types import SimpleNamespace

from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from src.core.models.message import Message


def _message_event(message_id: str, content: str) -> LifeEngineEvent:
    return LifeEngineEvent(
        event_id=message_id,
        event_type=EventType.MESSAGE,
        timestamp="2026-08-29T00:53:10+08:00",
        sequence=1,
        source="qq",
        source_detail="qq | 入站 | 群聊",
        content=content,
        sender="T9033",
        stream_id="group-stream",
    )


def test_note_flags_messages_still_in_expression_unread_queue() -> None:
    streams = {
        "group-stream": SimpleNamespace(
            context=SimpleNamespace(
                unread_messages=[
                    Message(message_id="msg-mention-1", content="x"),
                ]
            )
        )
    }
    events = [
        _message_event(
            "msg-mention-1",
            "@<爱莉希雅:3427056465> 晚安啦～♪好爱莉[表情：爱心]",
        ),
        _message_event("msg-answered", "已被表达层处理的消息"),
    ]

    note = LifeEngineService._expression_unseen_note(events, streams.get)

    assert "尚未经过表达层处理" in note
    assert "T9033" in note
    assert "晚安啦～♪好爱莉" in note
    assert "是否回应、如何回应由她决定" in note
    assert "已被表达层处理的消息" not in note


def test_note_empty_when_no_message_waits_in_unread_queue() -> None:
    streams = {
        "group-stream": SimpleNamespace(
            context=SimpleNamespace(unread_messages=[])
        )
    }
    events = [_message_event("msg-answered", "已被表达层处理的消息")]

    note = LifeEngineService._expression_unseen_note(events, streams.get)

    assert note == ""


def test_note_skips_non_message_events_and_missing_streams() -> None:
    heartbeat_event = LifeEngineEvent(
        event_id="hb-1",
        event_type=EventType.HEARTBEAT,
        timestamp="2026-08-29T00:53:10+08:00",
        sequence=2,
        source="life_engine",
        source_detail="心跳",
        content="心跳事件",
    )
    events = [
        heartbeat_event,
        _message_event("msg-offline-stream", "流已不在内存的消息"),
    ]

    note = LifeEngineService._expression_unseen_note(events, {}.get)

    assert note == ""


def test_note_truncates_long_content_snippet() -> None:
    streams = {
        "group-stream": SimpleNamespace(
            context=SimpleNamespace(
                unread_messages=[
                    Message(message_id="msg-long", content="x"),
                ]
            )
        )
    }
    long_content = "很长的消息内容" * 30

    note = LifeEngineService._expression_unseen_note(
        [_message_event("msg-long", long_content)],
        streams.get,
    )

    assert "…" in note
    assert long_content not in note
