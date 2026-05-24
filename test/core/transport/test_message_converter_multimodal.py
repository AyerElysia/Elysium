from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.core.models.message import Message, MessageType
from src.core.transport.message_receive.converter import MessageConverter


def _get_segment_types(envelope: dict) -> list[str]:
    segments = envelope.get("message_segment")
    assert isinstance(segments, list)
    return [str(seg.get("type", "")) for seg in segments if isinstance(seg, dict)]


def _get_segments(envelope: dict) -> list[dict]:
    segments = envelope.get("message_segment")
    assert isinstance(segments, list)
    return [seg for seg in segments if isinstance(seg, dict)]


@pytest.mark.asyncio
async def test_message_to_envelope_preserves_text_and_media_from_dict() -> None:
    """文本消息携带的 content.media / extra.media 应保留为可发送的媒体段。"""
    converter = MessageConverter()

    message = Message(
        message_id="msg-100",
        content={
            "text": "请看图",
            "media": [
                {"type": "image", "data": "base64|QUJD", "filename": "photo.png"},
                {
                    "type": "video",
                    "data": {"base64": "base64|RkZGRg==", "filename": "clip.mp4"},
                },
            ],
        },
        processed_plain_text=None,
        message_type=MessageType.TEXT,
        sender_id="user-100",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="stream-100",
        target_user_id="user-100",
        media=[
            {"type": "emoji", "data": "base64|R0hJ", "filename": "face.gif"},
        ],
    )

    envelope = await converter.message_to_envelope(message)

    assert _get_segment_types(envelope) == ["text", "image", "video", "emoji"]
    segments = _get_segments(envelope)
    assert segments[0]["data"] == "请看图"
    assert segments[1]["data"] == "QUJD"
    assert segments[2]["data"] == "RkZGRg=="
    assert segments[3]["data"] == "R0hJ"


@pytest.mark.asyncio
async def test_message_to_envelope_strips_base64_prefix_for_media() -> None:
    """媒体消息中的 base64 前缀应被规范化为发送端可直接消费的原始数据。"""
    converter = MessageConverter()

    message = Message(
        message_id="msg-101",
        content={"data": "base64|iVBORw0KGgoAAA"},
        processed_plain_text=None,
        message_type=MessageType.IMAGE,
        sender_id="user-101",
        sender_name="Bob",
        platform="qq",
        chat_type="private",
        stream_id="stream-101",
        target_user_id="user-101",
    )

    envelope = await converter.message_to_envelope(message)

    segments = _get_segments(envelope)
    assert _get_segment_types(envelope)[0] == "image"
    assert segments[0]["data"] == "iVBORw0KGgoAAA"


@pytest.mark.asyncio
async def test_envelope_to_message_transcribes_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """语音段应走 ASR，并写回 processed_plain_text。"""
    converter = MessageConverter()

    fake_manager = type(
        "FakeMediaManager",
        (),
        {
            "recognize_media": AsyncMock(return_value="图片描述"),
            "recognize_voice": AsyncMock(return_value="这是语音转写"),
            "recognize_video": AsyncMock(return_value=None),
            "should_skip_vlm": lambda self, stream_id, media_type=None: False,
        },
    )()
    monkeypatch.setattr(
        "src.core.managers.media_manager.get_media_manager",
        lambda: fake_manager,
    )

    envelope = {
        "message_info": {
            "message_id": "msg-voice-1",
            "time": 1710000000.0,
            "platform": "qq",
            "user_info": {
                "user_id": "user-voice",
                "user_nickname": "Alice",
            },
            "extra": {},
        },
        "message_segment": [
            {"type": "text", "data": "听一下："},
            {"type": "voice", "data": "base64|QUJD"},
        ],
        "raw_message": {"source": "unit-test"},
    }

    message = await converter.envelope_to_message(envelope)

    assert message.message_type == MessageType.VOICE
    assert message.processed_plain_text is not None
    assert "听一下：" in message.processed_plain_text
    assert "[语音:这是语音转写]" in message.processed_plain_text
    fake_manager.recognize_voice.assert_awaited_once()
    fake_manager.recognize_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_envelope_to_message_adds_audio_file_understanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """音频文件占位符也应写入理解摘要，而不是只保留文件名。"""
    converter = MessageConverter()

    fake_manager = type(
        "FakeMediaManager",
        (),
        {
            "recognize_media": AsyncMock(return_value=None),
            "recognize_voice": AsyncMock(return_value="音乐氛围明亮，像在往星空下奔跑"),
            "recognize_video": AsyncMock(return_value=None),
            "should_skip_vlm": lambda self, stream_id, media_type=None: False,
        },
    )()
    monkeypatch.setattr(
        "src.core.managers.media_manager.get_media_manager",
        lambda: fake_manager,
    )

    envelope = {
        "message_info": {
            "message_id": "msg-audio-file-1",
            "time": 1710000000.0,
            "platform": "qq",
            "user_info": {
                "user_id": "user-audio",
                "user_nickname": "Alice",
            },
            "extra": {},
        },
        "message_segment": [
            {
                "type": "voice",
                "data": {
                    "base64": "base64|QUJD",
                    "filename": "粉色夜灯.mp3",
                    "mime_type": "audio/mpeg",
                },
            },
        ],
        "raw_message": {"source": "unit-test"},
    }

    message = await converter.envelope_to_message(envelope)

    assert message.processed_plain_text is not None
    assert "[语音文件:粉色夜灯.mp3 | 音乐氛围明亮，像在往星空下奔跑]" in message.processed_plain_text
    assert isinstance(message.content, dict)
    assert message.content["media"][0]["type"] == "voice"
    fake_manager.recognize_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_envelope_to_message_skips_registered_image_vlm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream 注册跳过 image VLM 时，图片文本只保留占位，避免和原生 Image 双路并存。"""
    converter = MessageConverter()

    fake_manager = type(
        "FakeMediaManager",
        (),
        {
            "recognize_media": AsyncMock(return_value="不应该注入的图片描述"),
            "recognize_voice": AsyncMock(return_value=None),
            "recognize_video": AsyncMock(return_value=None),
            "should_skip_vlm": lambda self, stream_id, media_type=None: media_type == "image",
        },
    )()

    monkeypatch.setattr(
        "src.core.managers.media_manager.get_media_manager",
        lambda: fake_manager,
    )

    envelope = {
        "message_info": {
            "message_id": "msg-image-native-1",
            "time": 1710000000.0,
            "platform": "qq",
            "user_info": {
                "user_id": "user-image",
                "user_nickname": "Alice",
            },
            "extra": {},
        },
        "message_segment": [
            {"type": "text", "data": "看图："},
            {"type": "image", "data": "base64|QUJD"},
        ],
        "raw_message": {"source": "unit-test"},
    }

    message = await converter.envelope_to_message(envelope)

    assert message.message_type == MessageType.IMAGE
    assert message.processed_plain_text == "看图：[图片]"
    assert isinstance(message.content, dict)
    assert message.content["media"][0]["data"] == "base64|QUJD"
    fake_manager.recognize_media.assert_not_awaited()
