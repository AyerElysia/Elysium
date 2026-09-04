"""KOOK sender must not upload every voice clip as voice.mp3."""

from __future__ import annotations

import base64
import struct
from typing import Any

import pytest

from plugins.kook_adapter.sender import KookSender
from src.core.utils.audio_transcode import resolve_ffmpeg


def _pcm_wav(payload: bytes, *, rate: int = 8000) -> bytes:
    n = len(payload)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + n)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", n)
        + payload
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, int, str | None]] = []

    async def resolve_media_bytes(self, data: str) -> bytes:
        return base64.b64decode(data)

    async def upload_asset(
        self,
        file_data: bytes,
        filename: str,
        content_type: str | None = None,
    ) -> str:
        self.uploads.append((filename, len(file_data), content_type))
        return f"https://example.invalid/{filename}"

    async def send_direct_message(self, **kwargs: Any) -> None:
        if kwargs.get("msg_type") == 8:
            raise RuntimeError("KOOK API 错误: code=40000 message=不允许发送此消息类型")

    async def send_channel_message(self, **kwargs: Any) -> None:
        if kwargs.get("msg_type") == 8:
            raise RuntimeError("KOOK API 错误: code=40000 message=不允许发送此消息类型")


@pytest.mark.skipif(resolve_ffmpeg() is None, reason="ffmpeg 不可用")
@pytest.mark.asyncio
async def test_voice_fallback_uploads_unique_mp3_not_voice_mp3() -> None:
    client = _RecordingClient()
    sender = KookSender(client, lambda: None)  # type: ignore[arg-type]
    first = base64.b64encode(_pcm_wav(b"\x00\x10" * 800)).decode("ascii")
    second = base64.b64encode(_pcm_wav(b"\x30\x00" * 800)).decode("ascii")

    await sender._send_media_seg("PERSON", "", "user-1", {"type": "voice", "data": first}, None)
    await sender._send_media_seg("PERSON", "", "user-1", {"type": "voice", "data": second}, None)

    names = [name for name, _size, _type in client.uploads]
    assert names
    assert "voice.mp3" not in names
    assert names[0] != names[1]
    assert all(name.startswith("elysia-voice-") and name.endswith(".mp3") for name in names)
    assert all(content_type == "audio/mpeg" for _name, _size, content_type in client.uploads)
