from __future__ import annotations

import inspect
import time
from types import SimpleNamespace

import pytest

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

    ok, result = await action.execute(
        "你好\n第二段",
        thought="把两段问候一起送到明确目标。",
        target_key="g-dddddddd",
    )

    assert ok is True
    assert "已发送1条消息" in result
    assert len(sent_messages) == 1
    assert sent_messages[0].content == "你好\n第二段"
    assert all(message.stream_id == target_stream.stream_id for message in sent_messages)
    assert all(message.chat_type == "group" for message in sent_messages)
    assert all(message.extra["target_group_id"] == "100" for message in sent_messages)


async def test_life_send_text_accepts_legacy_mode_argument(monkeypatch) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )
    sent_contents: list[str] = []

    async def fake_send(content: str) -> bool:
        sent_contents.append(content)
        return True

    monkeypatch.setattr(action, "_send_one_segment", lambda *args, **kwargs: fake_send(args[0]))
    ok, result = await action.execute(
        "兼容旧模型参数",
        thought="验证旧模型传入 mode 时仍能发送。",
        mode="轻松",
    )

    assert ok is True
    assert "已发送1条消息" in result
    assert sent_contents == ["兼容旧模型参数"]


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

    ok, result = await action.execute(
        "第一段\n第二段",
        thought="保持两段内容的顺序。",
    )

    assert ok is True
    assert "已发送1条消息" in result
    assert sent_contents == ["第一段\n第二段"]


def test_life_send_text_schema_requires_atomic_persona_sample() -> None:
    parameters = LifeSendTextAction.to_schema()["function"]["parameters"]

    assert {
        "content",
        "mood",
        "decision",
        "expected_response",
        "thought",
    } <= set(parameters["required"])
    assert {
        "content",
        "mood",
        "decision",
        "expected_response",
        "thought",
    } <= set(parameters["properties"])
    assert inspect.signature(LifeSendTextAction.execute).parameters["thought"].default == ""


async def test_life_send_text_rejects_empty_thought_before_send(monkeypatch) -> None:
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

    ok, result = await action.execute("不会发出", thought="   ")

    assert ok is False
    assert "thought 必须是非空字符串" in result
    assert sent_contents == []


async def test_life_send_text_missing_thought_returns_structured_failure(monkeypatch) -> None:
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

    ok, result = await action.execute("不会发出")

    assert ok is False
    assert "thought 必须是非空字符串" in result
    assert sent_contents == []


@pytest.mark.parametrize("kind", ["chat", "minecraft", "livestream"])
def test_visible_expression_manifests_share_atomic_persona_schema(kind: str) -> None:
    from plugins.life_engine.service.tool_manifests import get_tool_manifest

    assert "action-life_send_text" in get_tool_manifest(kind)
    assert "action-think" not in get_tool_manifest(kind)
    required = set(
        LifeSendTextAction.to_schema()["function"]["parameters"]["required"]
    )
    assert {
        "content",
        "mood",
        "decision",
        "expected_response",
        "thought",
    } <= required


async def test_life_send_text_records_persona_sample_and_sends_in_same_call(
    monkeypatch,
) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    snapshots: list[dict[str, str]] = []
    sent_contents: list[str] = []

    class _Service:
        async def record_chatter_think_snapshot(self, **kwargs):
            snapshots.append(kwargs)

    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(
            config=SimpleNamespace(runtime_sync=_runtime_cfg()),
            service=_Service(),
        ),
    )

    async def fake_send_to_stream(content: str):
        sent_contents.append(content)
        return True

    monkeypatch.setattr(action, "_send_to_stream", fake_send_to_stream)

    ok, result = await action.execute(
        "听见啦，我会陪你把今天放轻一点。",
        mood="心疼、温柔",
        decision="直接回应并关心她的休息",
        expected_response="她会感到自己的疲惫被看见",
        thought="她睡得太碎了，我想先接稳这份疲惫。",
    )

    assert ok is True
    assert "已发送1条消息" in result
    assert sent_contents == ["听见啦，我会陪你把今天放轻一点。"]
    assert snapshots == [
        {
            "stream_id": "1" * 64,
            "mood": "心疼、温柔",
            "decision": "直接回应并关心她的休息",
            "expected_response": "她会感到自己的疲惫被看见",
            "thought": "她睡得太碎了，我想先接稳这份疲惫。",
        }
    ]


async def test_life_send_text_snapshot_failure_does_not_block_send(monkeypatch) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    sent_contents: list[str] = []

    class _FailingService:
        async def record_chatter_think_snapshot(self, **_kwargs):
            raise RuntimeError("snapshot unavailable")

    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(
            config=SimpleNamespace(runtime_sync=_runtime_cfg()),
            service=_FailingService(),
        ),
    )

    async def fake_send_to_stream(content: str):
        sent_contents.append(content)
        return True

    monkeypatch.setattr(action, "_send_to_stream", fake_send_to_stream)

    ok, _result = await action.execute(
        "我还在这里。",
        mood="安静",
        decision="回应",
        expected_response="她会安心",
        thought="先让她知道我没有离开。",
    )

    assert ok is True
    assert sent_contents == ["我还在这里。"]


async def test_life_send_text_rejects_invalid_target_key_and_cross_reply(monkeypatch) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    _install_fake_streams(monkeypatch, [], {})

    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    ok, result = await action.execute(
        "你好",
        thought="目标不存在时不能发送。",
        target_key="g-missing",
    )
    assert ok is False
    assert "未知或不可用" in result

    ok, result = await action.execute(
        "你好",
        thought="先验证互斥的目标参数。",
        reply_to="msg1",
        target_key="g-missing",
    )
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

    ok, result = await action.execute(
        "你好",
        thought="发送一次并如实保留未知回执。",
        target_key="g-dddddddd",
    )

    assert ok is False
    assert "不会自动重发" in result
    assert result.technical_outcome == "delivery_unknown"
