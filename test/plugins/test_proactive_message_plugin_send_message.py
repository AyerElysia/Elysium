"""proactive_message_plugin 消息发送归一化测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path("/root/Elysia/Neo-MoFox/plugins")
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from plugins.proactive_message_plugin.config import ProactiveMessageConfig  # noqa: E402
from plugins.proactive_message_plugin.plugin import ProactiveMessagePlugin  # noqa: E402

class _FakeSender:
    """最小化消息发送器。"""

    def __init__(self) -> None:
        self.messages = []

    async def send_message(self, message) -> bool:
        self.messages.append(message)
        return True


def test_send_message_normalizes_list_content_before_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    """列表内容应拆成多条字符串消息，避免 list 进入数据库层。"""
    fake_sender = _FakeSender()
    monkeypatch.setattr(
        "src.core.transport.message_send.get_message_sender",
        lambda: fake_sender,
    )

    plugin = ProactiveMessagePlugin()
    chat_stream = SimpleNamespace(
        stream_id="sid_001",
        platform="qq",
        chat_type="private",
        bot_id="bot_001",
        bot_nickname="爱莉",
    )

    ok = asyncio.run(
        plugin._send_message(
            chat_stream,
            ["  第一段  ", "", "第二段"],
        )
    )

    assert ok is True
    assert [msg.content for msg in fake_sender.messages] == ["第一段", "第二段"]
    assert [msg.processed_plain_text for msg in fake_sender.messages] == ["第一段", "第二段"]
    assert all(isinstance(msg.content, str) for msg in fake_sender.messages)
    assert all(isinstance(msg.processed_plain_text, str) for msg in fake_sender.messages)


def test_send_message_splits_newlines_before_persisting(monkeypatch: pytest.MonkeyPatch) -> None:
    """主动消息中的换行应被拆成多条消息，而不是原样发送。"""
    fake_sender = _FakeSender()
    monkeypatch.setattr(
        "src.core.transport.message_send.get_message_sender",
        lambda: fake_sender,
    )

    plugin = ProactiveMessagePlugin()
    chat_stream = SimpleNamespace(
        stream_id="sid_001",
        platform="qq",
        chat_type="private",
        bot_id="bot_001",
        bot_nickname="爱莉",
    )

    ok = asyncio.run(
        plugin._send_message(
            chat_stream,
            "第一段\n\n第二段\\n第三段",
        )
    )

    assert ok is True
    assert [msg.content for msg in fake_sender.messages] == ["第一段", "第二段", "第三段"]


def test_proactive_opportunity_prompt_requires_inner_monologue_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主动机会应要求 life_chatter 先记录内心独白，再决定开口或等待。"""
    injected_messages = []

    class _FakeContext:
        def add_unread_message(self, message) -> None:
            injected_messages.append(message)

    class _FakeLoopManager:
        def __init__(self) -> None:
            self._wait_states = {}

    async def fake_record(_chat_stream, _content: str) -> None:
        return None

    monkeypatch.setattr(
        "src.core.transport.distribution.stream_loop_manager.get_stream_loop_manager",
        lambda: _FakeLoopManager(),
    )
    monkeypatch.setattr(
        ProactiveMessagePlugin,
        "_record_proactive_opportunity_event",
        staticmethod(fake_record),
    )

    plugin = ProactiveMessagePlugin()
    plugin.service.record_bot_message("sid_001", "刚刚那句已经说完了")
    chat_stream = SimpleNamespace(
        stream_id="sid_001",
        platform="qq",
        chat_type="private",
        bot_id="bot_001",
        bot_nickname="爱莉",
        context=_FakeContext(),
    )

    asyncio.run(
        plugin._wake_stream_for_proactive_opportunity(
            chat_stream,
            elapsed_minutes=12.0,
            user_name="Ayer",
        )
    )

    assert len(injected_messages) == 1
    prompt = injected_messages[0].processed_plain_text
    assert "action-record_inner_monologue" in prompt
    assert "pass_and_wait" in prompt
    assert "不要假装对方刚刚发了新消息" in prompt


def test_schedule_continue_waiting_ignores_life_external_silence() -> None:
    plugin = ProactiveMessagePlugin()
    plugin.config = ProactiveMessageConfig()
    plugin.service.clear_all()

    state = plugin.service.get_or_create_state("sid_pause")
    state.is_waiting = True
    state.active_check_kind = "silence_wait"
    state.scheduler_task_name = "proactive_check_sid_pause"

    calls: list[tuple[str, float]] = []

    async def fake_start_waiting(*, stream_id: str, wait_minutes: float, callback):
        calls.append((stream_id, wait_minutes))
        return "task"

    plugin.service.start_waiting = fake_start_waiting  # type: ignore[method-assign]

    asyncio.run(plugin._schedule_continue_waiting("sid_pause", 30.0))

    current = plugin.service.get_state("sid_pause")
    assert calls == [("sid_pause", 30.0)]
    assert current is not None
    assert current.is_waiting is True
    assert current.active_check_kind == "silence_wait"
    assert current.scheduler_task_name == "proactive_check_sid_pause"
