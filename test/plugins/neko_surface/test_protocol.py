from __future__ import annotations

import base64
import io
import wave

import pytest

from plugins.neko_surface.protocol import (
    SCHEMA_VERSION,
    SurfaceEvent,
    SurfaceProtocolError,
)


def _wav_base64() -> str:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(b"\x00\x00" * 1_600)
    return base64.b64encode(output.getvalue()).decode("ascii")


def _jpeg_base64() -> str:
    return base64.b64encode(b"\xff\xd8\xff\xe0surface-jpeg").decode("ascii")


def test_surface_event_round_trip_preserves_contract() -> None:
    event = SurfaceEvent.create(
        "user.text",
        event_id="event-1",
        sequence=4,
        session_id="session-1",
        turn_id="turn-1",
        surface_id="neko-main",
        character="Elysia",
        origin="neko",
        payload={"text": "hello"},
        priority=6,
    )

    parsed = SurfaceEvent.from_dict(event.to_dict())

    assert parsed == event
    assert parsed.schema_version == SCHEMA_VERSION


def test_surface_event_rejects_unknown_schema() -> None:
    with pytest.raises(SurfaceProtocolError) as exc_info:
        SurfaceEvent.from_dict(
            {
                "schema_version": "elysia.surface.v2",
                "event_id": "event-1",
                "sequence": 1,
                "surface_id": "neko-main",
                "character": "Elysia",
                "origin": "neko",
                "type": "hello",
                "payload": {},
                "priority": 5,
            }
        )

    assert exc_info.value.code == "unsupported_schema"


def test_surface_text_event_requires_nonempty_text() -> None:
    with pytest.raises(SurfaceProtocolError) as exc_info:
        SurfaceEvent.create(
            "assistant.text",
            sequence=1,
            origin="neo",
            payload={"text": "  "},
        )

    assert exc_info.value.code == "missing_text"


def test_surface_audio_event_accepts_bounded_wav() -> None:
    event = SurfaceEvent.create(
        "user.audio",
        sequence=2,
        origin="neko",
        payload={
            "data": _wav_base64(),
            "mime_type": "audio/wav",
            "duration_seconds": 0.1,
        },
    )

    assert SurfaceEvent.from_dict(event.to_dict()) == event


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"data": "not-base64", "mime_type": "audio/wav"}, "invalid_audio"),
        ({"data": "YQ==", "mime_type": "audio/webm"}, "unsupported_audio"),
        (
            {
                "data": _wav_base64(),
                "mime_type": "audio/wav",
                "duration_seconds": 61,
            },
            "invalid_audio_duration",
        ),
    ],
)
def test_surface_audio_event_rejects_invalid_payload(payload: dict, code: str) -> None:
    with pytest.raises(SurfaceProtocolError) as exc_info:
        SurfaceEvent.create(
            "user.audio",
            sequence=2,
            origin="neko",
            payload=payload,
        )

    assert exc_info.value.code == code


def test_surface_screen_event_accepts_data_url_and_normalizes_mime() -> None:
    encoded = _jpeg_base64()
    event = SurfaceEvent.create(
        "user.screen",
        sequence=3,
        origin="neko",
        payload={
            "data": f"data:image/jpg;base64,{encoded}",
            "window_title": "Browser",
        },
    )

    parsed = SurfaceEvent.from_dict(event.to_dict())

    assert parsed == event
    assert parsed.payload["data"].startswith("data:image/jpg;base64,")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"data": "not-base64", "mime_type": "image/jpeg"}, "invalid_image"),
        ({"data": _jpeg_base64(), "mime_type": "image/svg+xml"}, "unsupported_image"),
        (
            {
                "data": "data:image/png;base64," + _jpeg_base64(),
                "mime_type": "image/jpeg",
            },
            "image_mime_mismatch",
        ),
        ({"data": base64.b64encode(b"not-an-image").decode(), "mime_type": "image/jpeg"}, "image_mime_mismatch"),
    ],
)
def test_surface_screen_event_rejects_invalid_payload(payload: dict, code: str) -> None:
    with pytest.raises(SurfaceProtocolError) as exc_info:
        SurfaceEvent.create(
            "user.screen",
            sequence=2,
            origin="neko",
            payload=payload,
        )

    assert exc_info.value.code == code


def test_surface_screen_event_rejects_oversized_image(monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.neko_surface import protocol

    monkeypatch.setattr(protocol, "MAX_INPUT_IMAGE_BYTES", 3)
    with pytest.raises(SurfaceProtocolError) as exc_info:
        SurfaceEvent.create(
            "user.screen",
            sequence=2,
            origin="neko",
            payload={
                "data": base64.b64encode(b"\xff\xd8\xff\xe0").decode(),
                "mime_type": "image/jpeg",
            },
        )

    assert exc_info.value.code == "image_too_large"
