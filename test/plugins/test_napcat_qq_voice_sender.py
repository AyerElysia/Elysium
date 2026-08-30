from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.napcat_adapter.outgoing import sender as sender_module
from plugins.napcat_adapter.outgoing.sender import OutgoingSender
from plugins.napcat_adapter.outgoing.voice_projection import QQVoiceProjection


def _voice_envelope(data: str) -> dict:
    return {
        "message_info": {
            "group_info": {"group_id": "654321"},
            "user_info": {"user_id": "123456"},
        },
        "message_segment": {
            "type": "voice",
            "data": data,
        },
    }


def _config(*, projection_enabled: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        features=SimpleNamespace(
            message_send_timeout_seconds=20.0,
            qq_voice_projection_enabled=projection_enabled,
        )
    )


def _client(message_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        call=AsyncMock(
            return_value={
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": message_id},
            }
        )
    )


async def test_napcat_sender_applies_qq_projection_to_inline_wav(monkeypatch) -> None:
    source_wav = b"RIFF\x04\x00\x00\x00WAVE"
    encoded = base64.b64encode(source_wav).decode("ascii")
    client = _client("projected-voice-1")

    def _project(file_value: str) -> QQVoiceProjection:
        assert file_value == f"base64://{encoded}"
        return QQVoiceProjection(
            file_value="base64://cHJvamVjdGVk",
            applied=True,
            input_bytes=len(source_wav),
            output_bytes=9,
        )

    monkeypatch.setattr(sender_module, "project_inline_qq_voice", _project)
    sender = OutgoingSender(client, lambda: _config(projection_enabled=True))

    await sender.send(_voice_envelope(encoded))

    sent_message = client.call.await_args.args[1]["message"]
    assert sent_message == [
        {"type": "record", "data": {"file": "base64://cHJvamVjdGVk"}}
    ]


async def test_napcat_sender_preserves_voice_when_projection_fails(monkeypatch) -> None:
    source_wav = b"RIFF\x04\x00\x00\x00WAVE"
    encoded = base64.b64encode(source_wav).decode("ascii")
    client = _client("original-voice-1")

    def _fail(_file_value: str) -> QQVoiceProjection:
        raise RuntimeError("synthetic ffmpeg failure")

    monkeypatch.setattr(sender_module, "project_inline_qq_voice", _fail)
    sender = OutgoingSender(client, lambda: _config(projection_enabled=True))

    await sender.send(_voice_envelope(encoded))

    sent_message = client.call.await_args.args[1]["message"]
    assert sent_message == [{"type": "record", "data": {"file": f"base64://{encoded}"}}]


async def test_napcat_sender_skips_projection_when_disabled(monkeypatch) -> None:
    client = _client("disabled-projection-1")

    def _unexpected(_file_value: str) -> QQVoiceProjection:
        raise AssertionError("projection must not run when disabled")

    monkeypatch.setattr(sender_module, "project_inline_qq_voice", _unexpected)
    sender = OutgoingSender(client, lambda: _config(projection_enabled=False))

    await sender.send(_voice_envelope("UklGRg=="))

    sent_message = client.call.await_args.args[1]["message"]
    assert sent_message == [{"type": "record", "data": {"file": "base64://UklGRg=="}}]


async def test_napcat_sender_preserves_source_when_config_is_absent(monkeypatch) -> None:
    client = _client("absent-config-1")

    def _unexpected(_file_value: str) -> QQVoiceProjection:
        raise AssertionError("missing config must keep the source audio unchanged")

    monkeypatch.setattr(sender_module, "project_inline_qq_voice", _unexpected)
    sender = OutgoingSender(client, lambda: None)

    await sender.send(_voice_envelope("UklGRg=="))

    sent_message = client.call.await_args.args[1]["message"]
    assert sent_message == [{"type": "record", "data": {"file": "base64://UklGRg=="}}]


async def test_napcat_sender_does_not_project_voice_url(monkeypatch) -> None:
    client = _client("voice-url-1")

    def _unexpected(_file_value: str) -> QQVoiceProjection:
        raise AssertionError("URL projection must remain transport-neutral")

    monkeypatch.setattr(sender_module, "project_inline_qq_voice", _unexpected)
    sender = OutgoingSender(client, lambda: _config(projection_enabled=True))

    await sender.send(_voice_envelope("https://example.invalid/voice.wav"))

    sent_message = client.call.await_args.args[1]["message"]
    assert sent_message == [
        {
            "type": "record",
            "data": {"file": "https://example.invalid/voice.wav"},
        }
    ]
