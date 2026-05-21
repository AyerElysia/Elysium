from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.chatter import LifeInspectMediaTool
from plugins.life_engine.core.config import LifeEngineConfig
from src.core.models.message import Message, MessageType


def _make_tool(messages: list[Message]) -> LifeInspectMediaTool:
    tool = LifeInspectMediaTool.__new__(LifeInspectMediaTool)
    tool.chat_stream = SimpleNamespace(
        context=SimpleNamespace(
            unread_messages=messages,
            current_message=None,
            history_messages=[],
        )
    )
    tool.plugin = SimpleNamespace(config=LifeEngineConfig())
    return tool


def _message(
    message_id: str,
    *,
    media: list[dict] | None = None,
    plain: str | None = None,
) -> Message:
    return Message(
        message_id=message_id,
        content={"media": media or []},
        processed_plain_text=plain,
        message_type=MessageType.IMAGE if media else MessageType.TEXT,
        sender_id="user-1",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="stream-1",
    )


def test_selects_latest_unread_media() -> None:
    tool = _make_tool([
        _message("m1", media=[{"type": "image", "data": "base64|QUJD"}]),
        _message("m2", media=[{"type": "video", "data": {"base64": "base64|REVG"}}]),
    ])

    selected = tool._select_media("latest", "auto")

    assert selected is not None
    assert selected.message.message_id == "m2"
    assert selected.kind == "video"
    assert selected.data_for_native == "base64|REVG"


def test_filters_by_media_type() -> None:
    tool = _make_tool([
        _message("m1", media=[{"type": "image", "data": "base64|QUJD"}]),
        _message("m2", media=[{"type": "video", "data": {"base64": "base64|REVG"}}]),
    ])

    selected = tool._select_media("latest", "image")

    assert selected is not None
    assert selected.message.message_id == "m1"
    assert selected.kind == "image"


def test_model_capability_detection() -> None:
    model = {
        "model_identifier": "omni",
        "extra_params": {"capabilities": ["image", "video"]},
    }

    assert LifeInspectMediaTool._model_supports_native_media(model, "image") is True
    assert LifeInspectMediaTool._model_supports_native_media(model, "video") is True
    assert LifeInspectMediaTool._model_supports_native_media(model, "audio") is False


def test_inspect_media_is_registered_as_tool_schema() -> None:
    schema = LifeInspectMediaTool.to_schema()

    assert schema["function"]["name"] == "tool-inspect_media"


@pytest.mark.asyncio
async def test_execute_returns_plaintext_when_media_data_missing() -> None:
    tool = _make_tool([
        _message(
            "m1",
            media=[{"type": "image"}],
            plain="[图片:一张粉色角色图]",
        )
    ])

    success, result = await tool.execute(focus="看图片内容")

    assert success is True
    assert "原始媒体数据不在当前运行态" in result
    assert "[图片:一张粉色角色图]" in result
