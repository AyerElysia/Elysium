from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.life_engine.core.chatter import LifeChatter, LifeInspectMediaTool
from plugins.life_engine.core.config import LifeEngineConfig
from src.core.models.message import Message, MessageType
from src.kernel.llm import Audio, Image, LLMPayload, ROLE, Text, ToolResult


def _make_tool(messages: list[Message]) -> LifeInspectMediaTool:
    tool = LifeInspectMediaTool.__new__(LifeInspectMediaTool)
    tool.stream_id = "stream-1"
    tool.chat_stream = SimpleNamespace(
        stream_id="stream-1",
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


def test_filters_audio_media_type() -> None:
    tool = _make_tool([
        _message("m1", media=[{"type": "image", "data": "base64|QUJD"}]),
        _message(
            "m2",
            media=[
                {
                    "type": "voice",
                    "data": {
                        "base64": "base64|UklGRg==",
                        "filename": "sound.mp3",
                        "mime_type": "audio/mpeg",
                    },
                }
            ],
        ),
    ])

    selected = tool._select_media("latest", "audio")

    assert selected is not None
    assert selected.message.message_id == "m2"
    assert selected.kind == "audio"
    assert selected.data_for_native == "base64|UklGRg=="
    assert selected.mime_type == "audio/mpeg"


def test_audio_media_type_uses_nested_mime() -> None:
    tool = _make_tool([
        _message(
            "m1",
            media=[
                {
                    "type": "voice",
                    "data": {
                        "base64": "base64|UklGRg==",
                        "filename": "sound.wav",
                        "mime_type": "audio/wav",
                    },
                }
            ],
        ),
    ])

    selected = tool._select_media("latest", "audio")

    assert selected is not None
    assert selected.mime_type == "audio/wav"


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


@pytest.mark.asyncio
async def test_execute_promotes_media_for_next_native_turn() -> None:
    LifeInspectMediaTool._consume_promoted_media("stream-1")
    tool = _make_tool(
        [_message("m1", media=[{"type": "image", "data": "base64|QUJD"}])]
    )

    success, result = await tool.execute(focus="看图中文字", detail_level="detailed")

    assert success is True
    assert "媒体已提升为原生输入" in result

    content = LifeChatter._consume_promoted_media_content("stream-1")
    assert any(isinstance(part, Text) and "看图中文字" in part.text for part in content)
    assert any(isinstance(part, Image) for part in content)
    assert LifeChatter._consume_promoted_media_content("stream-1") == []


@pytest.mark.asyncio
async def test_execute_promotes_audio_for_next_native_turn() -> None:
    LifeInspectMediaTool._consume_promoted_media("stream-1")
    tool = _make_tool(
        [
            _message(
                "m1",
                media=[
                    {
                        "type": "voice",
                        "data": {
                            "base64": "base64|UklGRg==",
                            "filename": "sound.mp3",
                            "mime_type": "audio/mpeg",
                        },
                    }
                ],
            )
        ]
    )

    success, result = await tool.execute(media_type="audio", focus="理解这段声音的情绪")

    assert success is True
    assert "媒体已提升为原生输入" in result

    content = LifeChatter._consume_promoted_media_content("stream-1")
    assert any(isinstance(part, Text) and "理解这段声音的情绪" in part.text for part in content)
    assert any(isinstance(part, Audio) for part in content)


@pytest.mark.asyncio
async def test_promoted_media_after_tool_result_gets_assistant_bridge() -> None:
    LifeInspectMediaTool._consume_promoted_media("stream-1")
    tool = _make_tool(
        [_message("m1", media=[{"type": "image", "data": "base64|QUJD"}])]
    )
    await tool.execute(focus="确认是否能直接看图")

    response = SimpleNamespace(payloads=[])
    response.add_payload = lambda payload: response.payloads.append(payload)
    response.add_payload(
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="ok", call_id="call-1", name="tool-inspect_media"),
        )
    )

    appended = LifeChatter._append_promoted_media_payload(response, "stream-1")

    assert appended is True
    assert [payload.role for payload in response.payloads] == [
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
        ROLE.USER,
    ]
    assert any(isinstance(part, Image) for part in response.payloads[-1].content)
