from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.chatter import LifeSendTextAction
from src.core.models.stream import ChatStream


def _runtime_cfg() -> object:
    return SimpleNamespace()


async def test_life_send_text_rejects_retired_target_key_without_sending(
    monkeypatch,
) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    async def forbidden_send(_content: str):
        raise AssertionError("retired cross-surface target must fail before send")

    monkeypatch.setattr(action, "_send_to_stream", forbidden_send)

    ok, result = await action.execute(
        "你好\n第二段",
        thought="把两段问候一起送到明确目标。",
        target_key="g-dddddddd",
    )

    assert ok is False
    assert "target_key 已退役" in result


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


async def test_life_send_text_without_target_key_uses_current_surface(monkeypatch) -> None:
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
    assert "target_key" not in parameters["properties"]
    assert "target_key" not in parameters["required"]


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


async def test_life_send_text_rejects_any_legacy_target_key(monkeypatch) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")

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
    assert "target_key 已退役" in result

    ok, result = await action.execute(
        "你好",
        thought="先验证互斥的目标参数。",
        reply_to="msg1",
        target_key="g-missing",
    )
    assert ok is False
    assert "target_key 已退役" in result


async def test_life_send_text_surfaces_delivery_unknown_as_technical_outcome(
    monkeypatch,
) -> None:
    current_stream = ChatStream(stream_id="1" * 64, platform="qq", chat_type="private")
    action = LifeSendTextAction(
        current_stream,
        SimpleNamespace(config=SimpleNamespace(runtime_sync=_runtime_cfg())),
    )

    async def _unknown(*_args: object, **_kwargs: object) -> bool:
        action._last_delivery_status = "unknown"
        return False

    monkeypatch.setattr(action, "_send_one_segment", _unknown)

    ok, result = await action.execute(
        "你好",
        thought="发送一次并如实保留未知回执。",
    )

    assert ok is False
    assert "不会自动重发" in result
    assert result.technical_outcome == "delivery_unknown"
