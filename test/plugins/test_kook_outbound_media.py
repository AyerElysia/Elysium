"""KOOK outbound media must be named by content, not a reused voice.mp3."""

from __future__ import annotations

import struct

import pytest

from plugins.kook_adapter.outbound_media import (
    name_kook_asset,
    prepare_kook_image_asset,
    prepare_kook_voice_asset,
    sniff_audio_suffix_and_type,
    sniff_image_suffix_and_type,
)
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


def test_sniff_rejects_wav_disguised_as_mp3() -> None:
    wav = _pcm_wav(b"\x00\x00" * 32)
    suffix, content_type = sniff_audio_suffix_and_type(wav)
    assert suffix == ".wav"
    assert content_type == "audio/wav"


def test_sniff_png_magic() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    suffix, content_type = sniff_image_suffix_and_type(png)
    assert suffix == ".png"
    assert content_type == "image/png"


def test_content_addressed_names_differ_for_different_audio() -> None:
    first = name_kook_asset(
        _pcm_wav(b"\x00\x00" * 16),
        kind="voice",
        suffix=".wav",
        content_type="audio/wav",
    )
    second = name_kook_asset(
        _pcm_wav(b"\x01\x00" * 16),
        kind="voice",
        suffix=".wav",
        content_type="audio/wav",
    )
    assert first.filename != second.filename
    assert first.filename.startswith("elysia-voice-")
    assert first.filename.endswith(".wav")
    assert "voice.mp3" not in {first.filename, second.filename}


def test_prepare_image_does_not_reuse_image_png() -> None:
    named = prepare_kook_image_asset(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    assert named.filename != "image.png"
    assert named.filename.endswith(".png")
    assert named.content_type == "image/png"


@pytest.mark.skipif(resolve_ffmpeg() is None, reason="ffmpeg 不可用")
def test_prepare_voice_transcodes_wav_to_unique_mp3() -> None:
    wav = _pcm_wav(b"\x00\x10" * 800, rate=8000)
    named = prepare_kook_voice_asset(wav)
    assert named.suffix == ".mp3"
    assert named.filename != "voice.mp3"
    assert named.filename.startswith("elysia-voice-")
    assert named.content_type == "audio/mpeg"
    assert named.body != wav
    assert named.body[:3] == b"ID3" or named.body[:2] in {
        b"\xff\xfb",
        b"\xff\xf3",
        b"\xff\xf2",
    }


@pytest.mark.skipif(resolve_ffmpeg() is None, reason="ffmpeg 不可用")
def test_prepare_voice_different_wavs_become_different_mp3_names() -> None:
    first = prepare_kook_voice_asset(_pcm_wav(b"\x00\x20" * 800))
    second = prepare_kook_voice_asset(_pcm_wav(b"\x7f\x00" * 800))
    assert first.filename != second.filename
    assert first.digest != second.digest
