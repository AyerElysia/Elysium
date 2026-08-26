from __future__ import annotations

import base64
import io
import math
import struct
import wave

import pytest

from plugins.napcat_adapter.config import NapcatAdapterConfig
from plugins.napcat_adapter.outgoing import voice_projection
from plugins.napcat_adapter.outgoing.voice_projection import (
    QQ_VOICE_TARGET_SAMPLE_RATE,
    project_inline_qq_voice,
)
from src.core.utils.audio_transcode import resolve_ffmpeg


def _sine_wav(*, sample_rate: int = 32_000, duration_seconds: float = 0.08) -> bytes:
    frame_count = round(sample_rate * duration_seconds)
    frames = bytearray()
    for index in range(frame_count):
        value = round(0.25 * 32767 * math.sin(2 * math.pi * 880 * index / sample_rate))
        frames.extend(struct.pack("<h", value))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(frames))
    return output.getvalue()


def _inline(audio_bytes: bytes) -> str:
    return "base64://" + base64.b64encode(audio_bytes).decode("ascii")


def test_qq_voice_projection_defaults_to_enabled() -> None:
    assert NapcatAdapterConfig.FeaturesSection().qq_voice_projection_enabled is True


def test_qq_voice_projection_uses_approved_medium_profile(monkeypatch) -> None:
    source = _sine_wav()
    output = _sine_wav(sample_rate=QQ_VOICE_TARGET_SAMPLE_RATE)
    captured: dict[str, object] = {}

    def _transcode(
        audio_bytes: bytes,
        *,
        output_suffix: str,
        codec_args: list[str],
    ) -> bytes:
        captured.update(
            audio_bytes=audio_bytes,
            output_suffix=output_suffix,
            codec_args=codec_args,
        )
        return output

    monkeypatch.setattr(voice_projection, "transcode_audio_bytes", _transcode)

    projection = project_inline_qq_voice(_inline(source))

    assert projection.applied is True
    assert projection.input_bytes == len(source)
    assert projection.output_bytes == len(output)
    assert base64.b64decode(projection.file_value.removeprefix("base64://")) == output
    assert captured["audio_bytes"] == source
    assert captured["output_suffix"] == ".wav"
    codec_args = captured["codec_args"]
    assert isinstance(codec_args, list)
    filter_chain = codec_args[codec_args.index("-af") + 1]
    assert "equalizer=f=2800:t=q:w=1.0:g=2.5" in filter_chain
    assert "highshelf=f=4500:t=q:w=0.7:g=3.0" in filter_chain
    assert "volume=-2.0dB" in filter_chain
    assert "alimiter=limit=0.89" in filter_chain
    assert "aresample=24000:resampler=soxr:precision=28" in filter_chain
    assert codec_args[codec_args.index("-ar") + 1] == "24000"
    assert codec_args[codec_args.index("-c:a") + 1] == "pcm_s16le"


@pytest.mark.parametrize(
    "file_value",
    [
        "base64://not-valid-base64***",
        _inline(b"#!SILK_V3\x00payload"),
        _inline(b"not a wave container"),
        "https://example.invalid/voice.wav",
    ],
)
def test_qq_voice_projection_preserves_unsupported_inputs(
    monkeypatch,
    file_value: str,
) -> None:
    def _unexpected(*_args, **_kwargs) -> bytes:
        raise AssertionError("unsupported transport input must not be transcoded")

    monkeypatch.setattr(voice_projection, "transcode_audio_bytes", _unexpected)

    projection = project_inline_qq_voice(file_value)

    assert projection.applied is False
    assert projection.file_value == file_value


def test_qq_voice_projection_enforces_input_bound(monkeypatch) -> None:
    source = _sine_wav()

    def _unexpected(*_args, **_kwargs) -> bytes:
        raise AssertionError("oversized input must not be transcoded")

    monkeypatch.setattr(voice_projection, "transcode_audio_bytes", _unexpected)

    projection = project_inline_qq_voice(
        _inline(source),
        max_input_bytes=len(source) - 1,
    )

    assert projection.applied is False


def test_qq_voice_projection_produces_real_24khz_pcm_wav() -> None:
    if resolve_ffmpeg() is None:
        pytest.skip("FFmpeg is unavailable")

    projection = project_inline_qq_voice(_inline(_sine_wav()))
    projected = base64.b64decode(projection.file_value.removeprefix("base64://"))

    assert projection.applied is True
    with wave.open(io.BytesIO(projected), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == QQ_VOICE_TARGET_SAMPLE_RATE
        assert wav_file.getnframes() > 0
