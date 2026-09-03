"""KOOK 出站媒体命名：按内容寻址，后缀跟字节魔数走。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from src.core.utils.audio_transcode import transcode_audio_bytes


@dataclass(frozen=True, slots=True)
class NamedKookAsset:
    """One upload-ready KOOK asset with a collision-resistant filename."""

    body: bytes
    filename: str
    content_type: str
    digest: str
    suffix: str


_VOICE_MP3_ARGS = ["-c:a", "libmp3lame", "-b:a", "128k", "-ac", "1"]


def sniff_audio_suffix_and_type(payload: bytes) -> tuple[str, str]:
    """Return ``(suffix, content_type)`` from audio magic bytes."""

    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WAVE":
        return ".wav", "audio/wav"
    if payload.startswith(b"ID3") or payload[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return ".mp3", "audio/mpeg"
    if payload.startswith(b"OggS"):
        return ".ogg", "audio/ogg"
    if payload.startswith(b"fLaC"):
        return ".flac", "audio/flac"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        brand = payload[8:12]
        if brand.startswith(b"M4A") or brand in {b"mp41", b"mp42", b"isom", b"iso2"}:
            return ".m4a", "audio/mp4"
    return ".bin", "application/octet-stream"


def sniff_image_suffix_and_type(payload: bytes) -> tuple[str, str]:
    """Return ``(suffix, content_type)`` from image magic bytes."""

    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if payload.startswith(b"GIF87a") or payload.startswith(b"GIF89a"):
        return ".gif", "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".bin", "application/octet-stream"


def content_addressed_filename(
    payload: bytes, *, kind: str, suffix: str
) -> tuple[str, str]:
    """Return ``(filename, sha256 hex)`` for one payload."""

    digest = hashlib.sha256(payload).hexdigest()
    return f"elysia-{kind}-{digest[:16]}{suffix}", digest


def name_kook_asset(
    payload: bytes, *, kind: str, suffix: str, content_type: str
) -> NamedKookAsset:
    """Bind payload bytes to a content-addressed KOOK filename."""

    filename, digest = content_addressed_filename(payload, kind=kind, suffix=suffix)
    return NamedKookAsset(
        body=payload,
        filename=filename,
        content_type=content_type,
        digest=digest,
        suffix=suffix,
    )


def prepare_kook_image_asset(payload: bytes) -> NamedKookAsset:
    """Name an outbound image from its real bytes instead of ``image.png``."""

    suffix, content_type = sniff_image_suffix_and_type(payload)
    return name_kook_asset(
        payload, kind="image", suffix=suffix, content_type=content_type
    )


def prepare_kook_voice_asset(payload: bytes) -> NamedKookAsset:
    """Prepare voice bytes for KOOK: real format, unique name, MP3 when possible.

    TTS currently emits WAV while the adapter historically uploaded every clip
    as ``voice.mp3``. KOOK then rejected native audio (type 8) and fell back to
    a file named ``voice.mp3``. Clients cache that filename, so later clips
    replay the first upload even though Elysium synthesized different audio.
    """

    suffix, content_type = sniff_audio_suffix_and_type(payload)
    if suffix == ".mp3":
        return name_kook_asset(
            payload, kind="voice", suffix=suffix, content_type=content_type
        )
    try:
        mp3_body = transcode_audio_bytes(
            payload,
            output_suffix=".mp3",
            codec_args=list(_VOICE_MP3_ARGS),
        )
    except Exception:
        return name_kook_asset(
            payload, kind="voice", suffix=suffix, content_type=content_type
        )
    return name_kook_asset(
        mp3_body, kind="voice", suffix=".mp3", content_type="audio/mpeg"
    )
