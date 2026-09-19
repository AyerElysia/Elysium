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


class _CaptureClient:
    def __init__(self) -> None:
        self.direct: list[dict[str, Any]] = []

    async def send_direct_message(self, **kwargs: Any) -> dict[str, Any]:
        self.direct.append(kwargs)
        return {}


@pytest.mark.asyncio
async def test_private_send_rejects_empty_target() -> None:
    client = _CaptureClient()
    sender = KookSender(client, lambda: None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="私信目标为空"):
        await sender._send_text("PERSON", "", "", "hi", 9, None)
    assert client.direct == []


@pytest.mark.asyncio
async def test_private_send_drops_internal_quote_ids() -> None:
    from plugins.kook_adapter.config import KookAdapterConfig
    from plugins.kook_adapter.sender import _kook_native_quote_id

    assert _kook_native_quote_id("msg_03070545-d524-45fc-9ed3-501d6bb1a660") is None
    assert _kook_native_quote_id("action_life_send_text_abc") is None
    assert _kook_native_quote_id("5b50cfb5-bf8e-4ab0-9d2a-80e3156410ed") == (
        "5b50cfb5-bf8e-4ab0-9d2a-80e3156410ed"
    )

    config = KookAdapterConfig()
    config.features.reply_with_quote = True
    config.features.use_kmarkdown = True
    client = _CaptureClient()
    sender = KookSender(client, lambda: config)
    envelope = {
        "kook_channel_type": "PERSON",
        "message_info": {"user_info": {"user_id": "1370110560"}},
        "reply_to_message_id": "msg_03070545-d524-45fc-9ed3-501d6bb1a660",
        "message_segment": [{"type": "text", "data": "hi"}],
    }
    await sender.send(envelope)
    assert client.direct == [
        {"target_id": "1370110560", "content": "hi", "msg_type": 9, "quote": None}
    ]
