from __future__ import annotations

import time
from types import SimpleNamespace

from plugins.life_engine.core.chatter import LifeSendTextAction
from plugins.life_engine.core.send_targets import (
    format_send_targets_for_prompt,
    list_recent_send_targets,
)
from src.core.models.stream import ChatStream


class _FakeStreamManager:
    def __init__(self, streams: list[object], info_by_stream: dict[str, dict]) -> None:
        self._streams = {str(stream.stream_id): stream for stream in streams}
        self._info_by_stream = info_by_stream

    async def get_stream_info(self, stream_id: str) -> dict | None:
        return self._info_by_stream.get(stream_id)


class _FakePersonCrud:
    async def get_by(self, person_id: str):
        if person_id == "person_ayer":
            return SimpleNamespace(user_id="2665253325", nickname="AyerElysia")
        return None


class _FakeUserQueryHelper:
    person_crud = _FakePersonCrud()


def _runtime_cfg() -> object:
    return SimpleNamespace(
        send_targets_limit=8,
        send_targets_window_hours=24.0,
    )


def _install_fake_streams(monkeypatch, streams: list[object], info_by_stream: dict[str, dict]) -> None:
    fake_manager = _FakeStreamManager(streams, info_by_stream)
    monkeypatch.setattr("src.core.managers.stream_manager.get_stream_manager", lambda: fake_manager)
    monkeypatch.setattr("src.core.utils.user_query_helper.get_user_query_helper", lambda: _FakeUserQueryHelper())


async def test_send_target_prompt_lists_recent_current_group_and_private(monkeypatch) -> None:
    now = time.time()
    group_stream = SimpleNamespace(
        stream_id="a" * 64,
        platform="qq",
        chat_type="group",
        stream_name="始源之地",
        last_active_time=now,
    )
    private_stream = SimpleNamespace(
        stream_id="b" * 64,
        platform="qq",
        chat_type="private",
        stream_name="AyerElysia",
        last_active_time=now - 10,
    )
    old_stream = SimpleNamespace(
        stream_id="c" * 64,
        platform="qq",
        chat_type="group",
        stream_name="旧群",
        last_active_time=now - 48 * 3600,
    )
    _install_fake_streams(
        monkeypatch,
        [old_stream, private_stream, group_stream],
        {
            group_stream.stream_id: {
                "stream_id": group_stream.stream_id,
                "platform": "qq",
                "chat_type": "group",
                "group_id": "100",
                "group_name": "始源之地",
                "last_active_time": group_stream.last_active_time,
            },
            private_stream.stream_id: {
                "stream_id": private_stream.stream_id,
                "platform": "qq",
                "chat_type": "private",
                "person_id": "person_ayer",
                "last_active_time": private_stream.last_active_time,
            },
            old_stream.stream_id: {
                "stream_id": old_stream.stream_id,
                "platform": "qq",
                "chat_type": "group",
                "group_id": "200",
                "group_name": "旧群",
                "last_active_time": old_stream.last_active_time,
            },
        },
    )

    targets = await list_recent_send_targets(
        current_stream_id=group_stream.stream_id,
        limit=8,
        active_window_hours=24.0,
    )
    prompt = format_send_targets_for_prompt(targets)

    assert "target_key=g-aaaaaaaa" in prompt
    assert "target_key=p-bbbbbbbb" in prompt
    assert "始源之地 | 当前聊天" in prompt
    assert "AyerElysia" in prompt
    assert "旧群" not in prompt


async def test_life_send_text_can_send_to_target_key(monkeypatch) -> None:
    now = time.time()
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    target_stream = SimpleNamespace(
        stream_id="d" * 64,
        platform="qq",
        chat_type="group",
        stream_name="始源之地",
        last_active_time=now,
    )
    _install_fake_streams(
        monkeypatch,
        [target_stream],
        {
            target_stream.stream_id: {
                "stream_id": target_stream.stream_id,
                "platform": "qq",
                "chat_type": "group",
                "group_id": "100",
                "group_name": "始源之地",
                "last_active_time": now,
            }
        },
    )

    class _AdapterManager:
        async def get_bot_info_by_platform(self, platform: str):
            return {"bot_id": "bot", "bot_name": "爱莉"}

    monkeypatch.setattr("src.core.managers.adapter_manager.get_adapter_manager", lambda: _AdapterManager())

    sent_messages = []

    class _Sender:
        async def send_message(self, message):
            sent_messages.append(message)
            return True

    monkeypatch.setattr("src.core.transport.message_send.get_message_sender", lambda: _Sender())

    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    ok, result = await action.execute("你好\n第二段", target_key="g-dddddddd")

    assert ok is True
    assert "已发送2条消息" in result
    assert len(sent_messages) == 2
    assert all(message.stream_id == target_stream.stream_id for message in sent_messages)
    assert all(message.chat_type == "group" for message in sent_messages)
    assert all(message.extra["target_group_id"] == "100" for message in sent_messages)


async def test_life_send_text_without_target_key_uses_legacy_stream_send(monkeypatch) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    sent_contents: list[str] = []

    async def fake_send_to_stream(content: str):
        sent_contents.append(content)
        return True

    monkeypatch.setattr(action, "_send_to_stream", fake_send_to_stream)

    ok, result = await action.execute("第一段\n第二段")

    assert ok is True
    assert "已发送2条消息" in result
    assert sent_contents == ["第一段", "第二段"]


async def test_life_send_text_rejects_invalid_target_key_and_cross_reply(monkeypatch) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    _install_fake_streams(monkeypatch, [], {})

    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    ok, result = await action.execute("你好", target_key="g-missing")
    assert ok is False
    assert "未知或不可用" in result

    ok, result = await action.execute("你好", reply_to="msg1", target_key="g-missing")
    assert ok is False
    assert "不能同时使用 reply_to" in result


async def test_life_send_text_surfaces_delivery_unknown_as_technical_outcome(
    monkeypatch,
) -> None:
    now = time.time()
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    target_stream = SimpleNamespace(
        stream_id="d" * 64,
        platform="qq",
        chat_type="group",
        stream_name="始源之地",
        last_active_time=now,
    )
    _install_fake_streams(
        monkeypatch,
        [target_stream],
        {
            target_stream.stream_id: {
                "stream_id": target_stream.stream_id,
                "platform": "qq",
                "chat_type": "group",
                "group_id": "100",
                "group_name": "始源之地",
                "last_active_time": now,
            }
        },
    )

    class _AdapterManager:
        async def get_bot_info_by_platform(self, _platform: str):
            return {"bot_id": "bot", "bot_name": "爱莉"}

    class _UnknownSender:
        async def send_message(self, message):
            message.extra["delivery_status"] = "unknown"
            return False

    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: _AdapterManager(),
    )
    monkeypatch.setattr(
        "src.core.transport.message_send.get_message_sender",
        lambda: _UnknownSender(),
    )
    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    ok, result = await action.execute("你好", target_key="g-dddddddd")

    assert ok is False
    assert "不会自动重发" in result
    assert result.technical_outcome == "delivery_unknown"
