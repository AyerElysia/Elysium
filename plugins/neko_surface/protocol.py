"""Versioned wire protocol shared by Elysium and N.E.K.O."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field, replace
import math
from typing import Any, Mapping
from uuid import uuid4

SCHEMA_VERSION = "elysia.surface.v1"
MAX_TEXT_LENGTH = 32_768
MAX_IDENTIFIER_LENGTH = 256
MAX_INPUT_AUDIO_BYTES = 8 * 1024 * 1024
MAX_INPUT_AUDIO_SECONDS = 60.0
INPUT_AUDIO_MIME_TYPES = frozenset({"audio/wav", "audio/mpeg"})
MAX_INPUT_IMAGE_BYTES = 8 * 1024 * 1024
INPUT_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

CLIENT_EVENT_TYPES = frozenset(
    {
        "hello",
        "user.text",
        "user.transcript.final",
        "user.audio",
        "user.screen",
        "user.interaction",
        "playback.started",
        "playback.ended",
        "ack",
        "state",
    }
)
SERVER_EVENT_TYPES = frozenset(
    {
        "ready",
        "assistant.text",
        "assistant.voice",
        "assistant.media",
        "presentation.expression",
        "presentation.motion",
        "turn.end",
        "ack",
        "error",
        "state",
    }
)
ALL_EVENT_TYPES = CLIENT_EVENT_TYPES | SERVER_EVENT_TYPES


def parse_input_audio_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Validate one bounded WAV/MP3 payload and return canonical base64 + MIME."""
    raw_data = payload.get("data")
    if not isinstance(raw_data, str) or not raw_data.strip():
        raise SurfaceProtocolError("missing_audio", "user.audio requires payload.data")

    encoded = raw_data.strip()
    data_url_mime = ""
    if encoded.lower().startswith("data:"):
        header, separator, body = encoded.partition(",")
        if not separator or ";base64" not in header.lower():
            raise SurfaceProtocolError(
                "invalid_audio",
                "user.audio data URL must contain base64 audio",
            )
        data_url_mime = header[5:].split(";", 1)[0].strip().lower()
        encoded = body

    declared_mime = str(payload.get("mime_type") or data_url_mime).split(";", 1)[0].strip().lower()
    mime_aliases = {
        "audio/mp3": "audio/mpeg",
        "audio/x-wav": "audio/wav",
        "audio/wave": "audio/wav",
        "audio/vnd.wave": "audio/wav",
    }
    mime_type = mime_aliases.get(declared_mime, declared_mime)
    canonical_data_url_mime = mime_aliases.get(data_url_mime, data_url_mime)
    if canonical_data_url_mime and mime_type != canonical_data_url_mime:
        raise SurfaceProtocolError(
            "audio_mime_mismatch",
            "user.audio MIME does not match its data URL",
        )
    if mime_type not in INPUT_AUDIO_MIME_TYPES:
        raise SurfaceProtocolError(
            "unsupported_audio",
            "user.audio only accepts WAV or MP3",
        )

    # Base64 expands bytes by 4/3. Reject oversized input before decoding it.
    max_encoded_length = ((MAX_INPUT_AUDIO_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded_length:
        raise SurfaceProtocolError("audio_too_large", "user.audio exceeds the byte limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SurfaceProtocolError("invalid_audio", "user.audio is not valid base64") from exc
    if not decoded:
        raise SurfaceProtocolError("missing_audio", "user.audio contains no audio bytes")
    if len(decoded) > MAX_INPUT_AUDIO_BYTES:
        raise SurfaceProtocolError("audio_too_large", "user.audio exceeds the byte limit")

    duration = payload.get("duration_seconds", payload.get("duration"))
    if duration is not None:
        if isinstance(duration, bool):
            raise SurfaceProtocolError("invalid_audio_duration", "audio duration must be a number")
        try:
            duration_value = float(duration)
        except (TypeError, ValueError) as exc:
            raise SurfaceProtocolError("invalid_audio_duration", "audio duration must be a number") from exc
        if (
            not math.isfinite(duration_value)
            or duration_value <= 0
            or duration_value > MAX_INPUT_AUDIO_SECONDS
        ):
            raise SurfaceProtocolError(
                "invalid_audio_duration",
                f"audio duration must be within {MAX_INPUT_AUDIO_SECONDS:g} seconds",
            )
    return encoded, mime_type


def parse_input_image_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    """Validate one bounded inline screenshot and return canonical base64 + MIME.

    Surface screenshots are deliberately restricted to common raster formats so
    the message converter can materialize an image attachment without fetching
    arbitrary URLs or accepting active SVG content.
    """
    raw_data = payload.get("data")
    if not isinstance(raw_data, str) or not raw_data.strip():
        raise SurfaceProtocolError("missing_image", "user.screen requires payload.data")

    encoded = raw_data.strip()
    data_url_mime = ""
    if encoded.lower().startswith("data:"):
        header, separator, body = encoded.partition(",")
        if not separator or ";base64" not in header.lower():
            raise SurfaceProtocolError(
                "invalid_image",
                "user.screen data URL must contain base64 image",
            )
        data_url_mime = header[5:].split(";", 1)[0].strip().lower()
        encoded = body

    declared_mime = str(payload.get("mime_type") or data_url_mime).split(";", 1)[0].strip().lower()
    mime_aliases = {"image/jpg": "image/jpeg"}
    mime_type = mime_aliases.get(declared_mime, declared_mime)
    canonical_data_url_mime = mime_aliases.get(data_url_mime, data_url_mime)
    if canonical_data_url_mime and mime_type != canonical_data_url_mime:
        raise SurfaceProtocolError(
            "image_mime_mismatch",
            "user.screen MIME does not match its data URL",
        )
    if mime_type not in INPUT_IMAGE_MIME_TYPES:
        raise SurfaceProtocolError(
            "unsupported_image",
            "user.screen only accepts JPEG, PNG, or WebP images",
        )

    # Base64 expands bytes by 4/3. Reject oversized input before decoding it.
    max_encoded_length = ((MAX_INPUT_IMAGE_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded_length:
        raise SurfaceProtocolError("image_too_large", "user.screen exceeds the byte limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SurfaceProtocolError("invalid_image", "user.screen is not valid base64") from exc
    if not decoded:
        raise SurfaceProtocolError("missing_image", "user.screen contains no image bytes")
    if len(decoded) > MAX_INPUT_IMAGE_BYTES:
        raise SurfaceProtocolError("image_too_large", "user.screen exceeds the byte limit")

    # Keep the transport boundary strict: a declared raster MIME must agree
    # with the file signature, otherwise MediaRef would reject it later.
    signature_matches = {
        "image/jpeg": decoded.startswith(b"\xff\xd8\xff"),
        "image/png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": len(decoded) >= 12
        and decoded[:4] == b"RIFF"
        and decoded[8:12] == b"WEBP",
    }
    if not signature_matches[mime_type]:
        raise SurfaceProtocolError(
            "image_mime_mismatch",
            "user.screen MIME does not match the image signature",
        )
    return encoded, mime_type


class SurfaceProtocolError(ValueError):
    """Raised when a surface envelope violates the v1 contract."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _identifier(value: Any, field_name: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise SurfaceProtocolError("missing_field", f"{field_name} is required")
    if len(text) > MAX_IDENTIFIER_LENGTH:
        raise SurfaceProtocolError("field_too_long", f"{field_name} is too long")
    return text


def _sequence(value: Any) -> int:
    if isinstance(value, bool):
        raise SurfaceProtocolError("invalid_sequence", "sequence must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SurfaceProtocolError("invalid_sequence", "sequence must be an integer") from exc
    if parsed < 0:
        raise SurfaceProtocolError("invalid_sequence", "sequence cannot be negative")
    return parsed


def _priority(value: Any) -> int:
    if isinstance(value, bool):
        raise SurfaceProtocolError("invalid_priority", "priority must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SurfaceProtocolError("invalid_priority", "priority must be an integer") from exc
    if parsed < 0 or parsed > 9:
        raise SurfaceProtocolError("invalid_priority", "priority must be between 0 and 9")
    return parsed


@dataclass(frozen=True, slots=True)
class SurfaceEvent:
    """Canonical ``elysia.surface.v1`` event envelope."""

    type: str
    event_id: str
    sequence: int
    session_id: str = ""
    turn_id: str = ""
    surface_id: str = ""
    character: str = ""
    origin: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 5
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        *,
        allowed_types: frozenset[str] | None = None,
    ) -> "SurfaceEvent":
        if not isinstance(raw, Mapping):
            raise SurfaceProtocolError("invalid_envelope", "event must be a JSON object")

        version = _identifier(raw.get("schema_version"), "schema_version", required=True)
        if version != SCHEMA_VERSION:
            raise SurfaceProtocolError(
                "unsupported_schema",
                f"expected {SCHEMA_VERSION}, got {version}",
            )

        event_type = _identifier(raw.get("type"), "type", required=True)
        accepted = allowed_types if allowed_types is not None else ALL_EVENT_TYPES
        if event_type not in accepted:
            raise SurfaceProtocolError("unsupported_event", f"unsupported event type: {event_type}")

        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise SurfaceProtocolError("invalid_payload", "payload must be a JSON object")

        event = cls(
            schema_version=version,
            type=event_type,
            event_id=_identifier(raw.get("event_id"), "event_id", required=True),
            sequence=_sequence(raw.get("sequence")),
            session_id=_identifier(raw.get("session_id"), "session_id"),
            turn_id=_identifier(raw.get("turn_id"), "turn_id"),
            surface_id=_identifier(raw.get("surface_id"), "surface_id"),
            character=_identifier(raw.get("character"), "character"),
            origin=_identifier(raw.get("origin"), "origin", required=True),
            payload=dict(payload),
            priority=_priority(raw.get("priority", 5)),
        )
        event._validate_payload()
        return event

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        sequence: int,
        session_id: str = "",
        turn_id: str = "",
        surface_id: str = "",
        character: str = "",
        origin: str,
        payload: Mapping[str, Any] | None = None,
        priority: int = 5,
        event_id: str | None = None,
    ) -> "SurfaceEvent":
        event = cls(
            type=event_type,
            event_id=event_id or str(uuid4()),
            sequence=_sequence(sequence),
            session_id=_identifier(session_id, "session_id"),
            turn_id=_identifier(turn_id, "turn_id"),
            surface_id=_identifier(surface_id, "surface_id"),
            character=_identifier(character, "character"),
            origin=_identifier(origin, "origin", required=True),
            payload=dict(payload or {}),
            priority=_priority(priority),
        )
        if event_type not in ALL_EVENT_TYPES:
            raise SurfaceProtocolError("unsupported_event", f"unsupported event type: {event_type}")
        event._validate_payload()
        return event

    def with_sequence(self, sequence: int) -> "SurfaceEvent":
        return replace(self, sequence=_sequence(sequence))

    def _validate_payload(self) -> None:
        if self.type in {"user.text", "user.transcript.final", "assistant.text"}:
            text = self.payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise SurfaceProtocolError("missing_text", f"{self.type} requires payload.text")
            if len(text) > MAX_TEXT_LENGTH:
                raise SurfaceProtocolError("text_too_long", "payload.text is too long")
        if self.type == "user.audio":
            parse_input_audio_payload(self.payload)
        if self.type == "user.screen":
            parse_input_image_payload(self.payload)
        if self.type == "hello":
            if not self.surface_id:
                raise SurfaceProtocolError("missing_field", "hello requires surface_id")
            if not self.character:
                raise SurfaceProtocolError("missing_field", "hello requires character")
        if self.type == "ack" and not str(self.payload.get("event_id") or "").strip():
            raise SurfaceProtocolError("missing_field", "ack requires payload.event_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "surface_id": self.surface_id,
            "character": self.character,
            "origin": self.origin,
            "type": self.type,
            "payload": dict(self.payload),
            "priority": self.priority,
        }
