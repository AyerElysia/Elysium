from __future__ import annotations

import base64

import pytest

from src.core.models.media import MediaSegmentType
from src.core.models.message import MessageType
from src.core.transport.message_receive.converter import MessageConverter


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42"


def _legacy_base64(data: bytes) -> str:
    return "base64|" + base64.b64encode(data).decode("ascii")


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


async def test_converter_attaches_video_without_running_summary() -> None:
    """视频只保留占位符和验证后的附件，不在 converter 内生成摘要。"""
    converter = MessageConverter()
    legacy_video = {
        "base64": _legacy_base64(_VIDEO_BYTES),
        "filename": "run.mp4",
        "mime_type": "video/mp4",
    }

    message = await converter.envelope_to_message(
        _build_private_envelope([{"type": "video", "data": legacy_video}])
    )

    assert message.message_type is MessageType.VIDEO
    assert message.processed_plain_text == "[视频]"
    assert isinstance(message.content, dict)
    assert message.content["media"] == [
        {"type": "video", "data": legacy_video}
    ]
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.segment_type is MediaSegmentType.VIDEO
    assert attachment.media_ref.data == _VIDEO_BYTES
    assert attachment.media_ref.mime_type == "video/mp4"
    assert attachment.media_ref.source_message_id == "msg-video-1"
    assert attachment.filename == "run.mp4"
    assert "media_errors" not in message.extra
    assert not hasattr(MessageConverter, "_recognize_media_with_manager")
    assert not hasattr(MessageConverter, "_skip_vlm_media_types_for_stream")


async def test_converter_keeps_image_and_video_placeholders_and_attachments() -> None:
    """混合媒体按原顺序附加，文本只含稳定占位符。"""
    converter = MessageConverter()
    message = await converter.envelope_to_message(
        _build_private_envelope(
            [
                {"type": "image", "data": _legacy_base64(_PNG_BYTES)},
                {
                    "type": "video",
                    "data": {
                        "base64": _legacy_base64(_VIDEO_BYTES),
                        "filename": "talk.mp4",
                    },
                },
            ]
        )
    )

    assert message.message_type is MessageType.IMAGE
    assert message.processed_plain_text == "[图片][视频]"
    assert [attachment.segment_type for attachment in message.attachments] == [
        MediaSegmentType.IMAGE,
        MediaSegmentType.VIDEO,
    ]
    assert message.attachments[0].media_ref.data == _PNG_BYTES
    assert message.attachments[1].media_ref.data == _VIDEO_BYTES
    assert message.extra["media"] == message.content["media"]
