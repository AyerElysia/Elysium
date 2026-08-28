"""Legacy opt-in QQ acoustic experiment retained for controlled A/B tests."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from src.core.utils.audio_transcode import transcode_audio_bytes

QQ_VOICE_PROJECTION_ALGORITHM_VERSION = "qq_voice_presence_v1"
QQ_VOICE_TARGET_SAMPLE_RATE = 24_000
QQ_VOICE_MAX_INPUT_BYTES = 30 * 1024 * 1024

_QQ_VOICE_FILTER = (
    "equalizer=f=2800:t=q:w=1.0:g=2.5,"
    "highshelf=f=4500:t=q:w=0.7:g=3.0,"
    "volume=-2.0dB,"
    "alimiter=limit=0.89:attack=5:release=50:level=false,"
    "aresample=24000:resampler=soxr:precision=28"
)


@dataclass(frozen=True, slots=True)
class QQVoiceProjection:
    """A derived QQ transport artifact; the canonical source audio is untouched."""

    file_value: str
    applied: bool
    input_bytes: int = 0
    output_bytes: int = 0


def _is_wave_container(audio_bytes: bytes) -> bool:
    return (
        len(audio_bytes) >= 12
        and audio_bytes[:4] == b"RIFF"
        and audio_bytes[8:12] == b"WAVE"
    )


def _unchanged(file_value: str, *, input_bytes: int = 0) -> QQVoiceProjection:
    return QQVoiceProjection(
        file_value=file_value,
        applied=False,
        input_bytes=input_bytes,
        output_bytes=input_bytes,
    )


def project_inline_qq_voice(
    file_value: str,
    *,
    max_input_bytes: int = QQ_VOICE_MAX_INPUT_BYTES,
) -> QQVoiceProjection:
    """Return the legacy bounded QQ experiment for an inline PCM WAV.

    URLs, malformed Base64, existing Silk payloads and non-WAV inputs are not
    transport-owned PCM and therefore pass through unchanged. Conversion failures
    are raised so the caller can report a content-free warning and send the source.
    Production defaults to source preservation because this EQ/resample chain did
    not pass the human listening gate.
    """

    if not file_value or file_value.startswith(("http://", "https://")):
        return _unchanged(file_value)
    if max_input_bytes <= 0:
        raise ValueError("max_input_bytes must be greater than zero")

    encoded = (
        file_value.removeprefix("base64://")
        if file_value.startswith("base64://")
        else file_value
    )
    max_encoded_chars = ((max_input_bytes + 2) // 3) * 4
    if not encoded or len(encoded) > max_encoded_chars:
        return _unchanged(file_value)

    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _unchanged(file_value)
    if len(audio_bytes) > max_input_bytes:
        return _unchanged(file_value, input_bytes=len(audio_bytes))
    if audio_bytes.startswith(b"#!SILK") or not _is_wave_container(audio_bytes):
        return _unchanged(file_value, input_bytes=len(audio_bytes))

    projected = transcode_audio_bytes(
        audio_bytes,
        output_suffix=".wav",
        codec_args=[
            "-af",
            _QQ_VOICE_FILTER,
            "-ac",
            "1",
            "-ar",
            str(QQ_VOICE_TARGET_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
        ],
    )
    if not _is_wave_container(projected):
        raise RuntimeError("QQ voice projection did not produce a WAV container")

    projected_base64 = base64.b64encode(projected).decode("ascii")
    return QQVoiceProjection(
        file_value=f"base64://{projected_base64}",
        applied=True,
        input_bytes=len(audio_bytes),
        output_bytes=len(projected),
    )
