from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.core.models.message import MessageType
from src.core.transport.message_receive.converter import MessageConverter


def _build_private_envelope(segments: list[dict]) -> dict:
    return {
        "message_info": {
            "message_id": "msg-video-1",
            "time": 1710000000.0,
            "platform": "qq",
            "user_info": {
                "user_id": "user_001",
                "user_nickname": "Alice",
            },
            "extra": {},
        },
        "message_segment": segments,
        "raw_message": {"source": "unit-test"},
    }


@pytest.mark.asyncio
async def test_converter_recognizes_video_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """视频段应走非原生摘要链路，并写入 [视频:xxx] 占位文本。"""
    converter = MessageConverter()

    fake_manager = SimpleNamespace(
        recognize_media=AsyncMock(return_value=None),
        recognize_video=AsyncMock(return_value="画面中有人在室外慢跑，背景是街道和建筑。"),
    )
    monkeypatch.setattr(
        "src.core.managers.media_manager.get_media_manager",
        lambda: fake_manager,
    )

    envelope = _build_private_envelope(
        [{"type": "video", "data": {"base64": "base64|ZmFrZV92aWRlbw==", "filename": "run.mp4"}}]
    )
    message = await converter.envelope_to_message(envelope)

    assert message.message_type == MessageType.VIDEO
    assert message.processed_plain_text is not None
    assert "[视频:画面中有人在室外慢跑，背景是街道和建筑。]" in message.processed_plain_text
    fake_manager.recognize_video.assert_awaited_once()
    fake_manager.recognize_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_converter_skip_vlm_still_handles_video(monkeypatch: pytest.MonkeyPatch) -> None:
    """注册 skip_vlm 时应跳过 image/emoji 识别，但 video 仍应摘要。"""
    converter = MessageConverter()

    fake_manager = SimpleNamespace(
        recognize_media=AsyncMock(return_value="这是一张图片"),
        recognize_video=AsyncMock(return_value="视频大致在讲一次对话场景。"),
    )
    monkeypatch.setattr(
        "src.core.managers.media_manager.get_media_manager",
        lambda: fake_manager,
    )
    monkeypatch.setattr(
        MessageConverter,
        "_skip_vlm_media_types_for_stream",
        staticmethod(lambda _stream_id, _media_types: {"image", "emoji"}),
    )

    envelope = _build_private_envelope(
        [
            {"type": "image", "data": "base64|ZmFrZV9pbWFnZQ=="},
            {"type": "video", "data": {"base64": "base64|ZmFrZV92aWRlbw==", "filename": "talk.mp4"}},
        ]
    )
    message = await converter.envelope_to_message(envelope)

    assert message.message_type == MessageType.IMAGE
    assert message.processed_plain_text is not None
    assert "[图片]" in message.processed_plain_text
    assert "[视频:视频大致在讲一次对话场景。]" in message.processed_plain_text
    fake_manager.recognize_media.assert_not_awaited()
    fake_manager.recognize_video.assert_awaited_once()
