from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.napcat_adapter.src.handlers.to_core.message_handler import MessageHandler


async def test_mp3_file_message_is_promoted_to_voice_segment(tmp_path) -> None:
    audio_path = tmp_path / "pink-light.mp3"
    audio_path.write_bytes(b"fake mp3 bytes")
    handler = MessageHandler(SimpleNamespace(plugin=None))

    segment = {
        "type": "file",
        "data": {
            "file": "pink-light.mp3",
            "file_size": audio_path.stat().st_size,
            "file_id": "file-1",
            "file_path": str(audio_path),
        },
    }

    result = await handler._handle_file_message(segment, {"group_id": "100"})

    assert result is not None
    assert result["type"] == "voice"
    assert result["data"]["filename"] == "pink-light.mp3"
    assert result["data"]["mime_type"] == "audio/mpeg"
    assert result["data"]["base64"]


def test_audio_file_name_detection() -> None:
    assert MessageHandler._is_audio_file_name("粉色夜灯.mp3") is True
    assert MessageHandler._is_audio_file_name("voice.wav") is True
    assert MessageHandler._is_audio_file_name("movie.mp4") is False

