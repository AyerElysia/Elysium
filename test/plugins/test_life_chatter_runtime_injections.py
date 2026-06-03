"""life_chatter runtime assistant injection tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.chatter import (
    LifeChatter,
    consume_runtime_assistant_injections,
    push_runtime_assistant_injection,
)
from src.core.models.message import Message, MessageType
from src.kernel.llm import LLMPayload, ROLE, Text


class _FakeResponse:
    def __init__(self, payloads: list[LLMPayload] | None = None) -> None:
        self.payloads = payloads or []

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)


def _payload_text(payload: Any) -> str:
    content = getattr(payload, "content", [])
    if isinstance(content, list) and content:
        return "\n".join(str(getattr(part, "text", part)) for part in content)
    return str(content)


def _message(
    index: int,
    content: str,
    *,
    stream_id: str = "stream_history",
    platform: str = "qq",
    chat_type: str = "private",
) -> Message:
    return Message(
        message_id=f"m{index}",
        time=1_700_000_000 + index,
        content=content,
        processed_plain_text=content,
        message_type=MessageType.TEXT,
        sender_id=f"u{index}",
        sender_name=f"user{index}",
        sender_role="user",
        platform=platform,
        chat_type=chat_type,
        stream_id=stream_id,
    )


def test_life_chatter_runtime_queue_is_per_stream() -> None:
    push_runtime_assistant_injection("stream_a", "[内心独白] A")
    push_runtime_assistant_injection("stream_b", "[内心独白] B")

    assert consume_runtime_assistant_injections("stream_a") == ["[内心独白] A"]
    assert consume_runtime_assistant_injections("stream_a") == []
    assert consume_runtime_assistant_injections("stream_b") == ["[内心独白] B"]


def test_life_chatter_injects_runtime_context_after_existing_user_payload() -> None:
    stream = SimpleNamespace(stream_id="stream_with_user")
    response = _FakeResponse([LLMPayload(ROLE.USER, Text("previous user"))])
    chatter = LifeChatter.__new__(LifeChatter)

    push_runtime_assistant_injection(stream.stream_id, "[内心独白] 等一等再说")

    runtime_context = chatter._format_runtime_context_text(
        chatter._consume_runtime_assistant_context(stream)
    )
    dynamic_context, high_water = asyncio.run(
        chatter._build_dynamic_context_text(
            stream,
            service=None,
            runtime_context_text=runtime_context,
        )
    )

    assert high_water == 0
    assert "[内心独白] 等一等再说" in dynamic_context

    chatter._append_transient_context(response, dynamic_context)
    assert response.payloads[-1].role == ROLE.USER
    assert "<transient_life_context>" in _payload_text(response.payloads[-1])
    assert "[内心独白] 等一等再说" in _payload_text(response.payloads[-1])


def test_life_chatter_keeps_runtime_context_for_first_user_prompt() -> None:
    stream = SimpleNamespace(stream_id="stream_without_user")
    response = _FakeResponse([LLMPayload(ROLE.SYSTEM, Text("sys"))])
    chatter = LifeChatter.__new__(LifeChatter)

    push_runtime_assistant_injection(stream.stream_id, "[内心独白] 先记下来")

    runtime_context = chatter._format_runtime_context_text(
        chatter._consume_runtime_assistant_context(stream)
    )
    dynamic_context, high_water = asyncio.run(
        chatter._build_dynamic_context_text(
            stream,
            service=None,
            runtime_context_text=runtime_context,
        )
    )

    prompt = chatter._build_chat_user_prompt(
        SimpleNamespace(stream_id=stream.stream_id, stream_name="test"),
        unread_lines="用户: hi",
    )

    assert high_water == 0
    assert "[内心独白] 先记下来" in dynamic_context
    assert "<life_runtime_context>" in dynamic_context
    assert "[内心独白] 先记下来" not in prompt

    chatter._append_transient_context(response, dynamic_context)
    assert _payload_text(response.payloads[-1]) == "sys"
    assert consume_runtime_assistant_injections(stream.stream_id) == []


def test_life_chatter_history_text_can_keep_short_tail_after_first_merge() -> None:
    stream = SimpleNamespace(
        context=SimpleNamespace(
            history_messages=[
                _message(1, "第一条旧消息"),
                _message(2, "第二条旧消息"),
                _message(3, "刚刚真正讨论的重点"),
                _message(4, "上一句追问"),
            ]
        )
    )

    full_history = LifeChatter._build_history_text(stream, max_messages=None)
    tail_history = LifeChatter._build_history_text(stream, max_messages=2)

    assert "第一条旧消息" in full_history
    assert "刚刚真正讨论的重点" in tail_history
    assert "上一句追问" in tail_history
    assert "第二条旧消息" not in tail_history


def test_life_chatter_global_history_merges_streams_with_source_labels() -> None:
    current_stream = SimpleNamespace(
        stream_id="stream-a",
        stream_name="A 私聊",
        platform="qq",
        chat_type="private",
        context=SimpleNamespace(
            history_messages=[
                _message(1, "A_OLDER", stream_id="stream-a"),
                _message(4, "A_NEWER", stream_id="stream-a"),
            ]
        ),
    )
    other_stream = SimpleNamespace(
        stream_id="stream-b",
        stream_name="B 直播间",
        platform="live",
        chat_type="group",
        context=SimpleNamespace(
            history_messages=[
                _message(2, "B_MIDDLE", stream_id="stream-b", platform="live", chat_type="group"),
            ]
        ),
    )
    manager = SimpleNamespace(_streams={"stream-b": other_stream})

    history = LifeChatter._build_history_text(
        current_stream,
        max_messages=3,
        global_history=True,
        stream_manager=manager,
    )

    assert "〔当前聊天流 | A 私聊 | qq/private | stream-a〕" in history
    assert "〔其他聊天流 | B 直播间 | live/group | stream-b〕" in history
    assert history.index("A_OLDER") < history.index("B_MIDDLE") < history.index("A_NEWER")


def test_life_chatter_initial_history_limit_reads_config() -> None:
    config = LifeEngineConfig()
    config.chatter.initial_history_messages = 4
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)

    assert chatter._get_initial_history_message_limit() == 4


def test_life_chatter_initial_history_limit_supports_legacy_field() -> None:
    config = LifeEngineConfig()
    config.chatter.initial_history_messages = 30
    config.chatter.recent_history_tail_messages = 6
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)

    assert chatter._get_initial_history_message_limit() == 6


def test_life_chatter_router_history_limit_is_fixed_to_10() -> None:
    config = LifeEngineConfig()
    config.chatter.initial_history_messages = 80
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)

    assert chatter._get_router_history_message_limit() == 10

# ---- 新增：runtime context 事件流 + thought delta cursor 去重 ---------------


import pytest  # noqa: E402

from plugins.life_engine.service.core import LifeEngineService  # noqa: E402
from plugins.life_engine.service.event_builder import (  # noqa: E402
    EventType,
    LifeEngineEvent,
)


def _make_event(seq: int, **kwargs) -> LifeEngineEvent:
    base = dict(
        event_id=f"e{seq}",
        event_type=EventType.HEARTBEAT,
        timestamp="2026-04-25T22:00:00+08:00",
        sequence=seq,
        source="life_engine",
        source_detail="hb",
        content=f"content-{seq}",
    )
    base.update(kwargs)
    return LifeEngineEvent(**base)


@pytest.mark.asyncio
async def test_build_chatter_runtime_includes_full_new_event_stream() -> None:
    """新增 life 事件流应完整进入 runtime context，而不是只剩 salient tail。"""
    service = LifeEngineService(SimpleNamespace(config=None))
    chat = SimpleNamespace(stream_id="stream-x")
    service._event_history = [
        _make_event(1, content="HB_NOISE", heartbeat_index=1),
        _make_event(
            2,
            event_type=EventType.AGENT_RESULT,
            content="AGENT_DONE",
            tool_name="planner",
            tool_success=True,
            source_detail="agent",
        ),
        _make_event(
            3,
            event_type=EventType.TOOL_CALL,
            content="tool_args_blob",
            tool_name="search",
            source_detail="tool",
        ),
    ]
    text, hw = await service.build_chatter_runtime_context(chat)
    assert "### 新增 life 事件流" in text
    assert "HB_NOISE" in text
    assert "tool_args_blob" in text
    assert "AGENT_DONE" in text
    assert hw == 3


@pytest.mark.asyncio
async def test_build_chatter_runtime_thought_delta_cursor_dedup() -> None:
    """同一 stream 第二次 build 不应再在 thought 块带 🔄 delta 标记。"""
    service = LifeEngineService(SimpleNamespace(config=None))
    chat = SimpleNamespace(stream_id="stream-d")
    service._thought_manager = SimpleNamespace(
        format_for_prompt=lambda **kw: (
            "🔄 (刚推进) idea-1" if kw.get("revision_cursor", 0) < 5 else "idea-1"
        ),
        current_revision=5,
    )
    service._event_history = []

    first, _ = await service.build_chatter_runtime_context(chat)
    second, _ = await service.build_chatter_runtime_context(chat)

    assert "🔄" in first
    assert "🔄" not in second


@pytest.mark.asyncio
async def test_build_chatter_runtime_includes_latest_think_and_recent_chat() -> None:
    service = LifeEngineService(SimpleNamespace(config=LifeEngineConfig()))
    chat = SimpleNamespace(
        stream_id="stream-think",
        context=SimpleNamespace(
            history_messages=[
                _message(1, "旧消息 1"),
                _message(2, "旧消息 2"),
                _message(3, "旧消息 3"),
            ]
        ),
    )

    await service.record_chatter_think_snapshot(
        stream_id="stream-think",
        thought="她白天喊我的名字，其实是在确认我还在。",
        mood="小心地在意",
        decision="先回应她的不安，再接住话题",
        expected_response="她会放松一点",
    )

    text, _ = await service.build_chatter_runtime_context(chat)

    assert "### 最近一次 action-think" in text
    assert "她白天喊我的名字" in text
    assert "### 最近 10 条聊天记录" in text
    assert "旧消息 1" in text
    assert "旧消息 3" in text


@pytest.mark.asyncio
async def test_build_chatter_runtime_unified_recent_chat_merges_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LifeEngineService(SimpleNamespace(config=LifeEngineConfig()))
    current_stream = SimpleNamespace(
        stream_id="stream-a",
        stream_name="A 私聊",
        platform="qq",
        chat_type="private",
        context=SimpleNamespace(
            history_messages=[
                _message(1, "A_HISTORY", stream_id="stream-a"),
            ]
        ),
    )
    other_stream = SimpleNamespace(
        stream_id="stream-b",
        stream_name="B 直播间",
        platform="live",
        chat_type="group",
        context=SimpleNamespace(
            history_messages=[
                _message(2, "B_HISTORY", stream_id="stream-b", platform="live", chat_type="group"),
            ]
        ),
    )
    manager = SimpleNamespace(_streams={"stream-a": current_stream, "stream-b": other_stream})
    monkeypatch.setattr("src.core.managers.get_stream_manager", lambda: manager)

    text, _ = await service.build_chatter_runtime_context(
        current_stream,
        unified_chatter_context=True,
    )
    without_recent, _ = await service.build_chatter_runtime_context(
        current_stream,
        unified_chatter_context=True,
        include_recent_chat_history=False,
    )

    assert "### 最近 10 条聊天记录" in text
    assert "〔当前聊天流 | A 私聊 | qq/private | stream-a〕" in text
    assert "〔其他聊天流 | B 直播间 | live/group | stream-b〕" in text
    assert "A_HISTORY" in text
    assert "B_HISTORY" in text
    assert "### 最近 10 条聊天记录" not in without_recent
