from __future__ import annotations

import base64

from plugins.napcat_adapter.events.message import MessageEventHandler


async def test_mp3_file_message_is_promoted_to_voice_segment(tmp_path) -> None:
    audio_path = tmp_path / "pink-light.mp3"
    audio_path.write_bytes(b"fake mp3 bytes")
    handler = MessageEventHandler(client=None, get_config=lambda: None)  # type: ignore[arg-type]

    segment = {
        "type": "file",
        "data": {
            "file": "pink-light.mp3",
            "file_size": audio_path.stat().st_size,
            "file_id": "file-1",
            "file_path": str(audio_path),
        },
    }

    result = await handler._handle_file(segment, {"group_id": "100"})

    assert result is not None
    assert result["type"] == "voice"
    assert result["data"]["filename"] == "pink-light.mp3"
    assert base64.b64decode(result["data"]["base64"]) == b"fake mp3 bytes"


def test_audio_file_name_detection() -> None:
    assert MessageEventHandler._is_audio_file("粉色夜灯.mp3") is True
    assert MessageEventHandler._is_audio_file("voice.wav") is True
    assert MessageEventHandler._is_audio_file("movie.mp4") is False

