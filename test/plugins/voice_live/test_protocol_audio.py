from __future__ import annotations

import math
import struct

import pytest

from plugins.voice_live.audio import (
    float32_bytes_to_pcm16,
    pcm16_to_float32_bytes,
    resample_pcm16_mono,
)
from plugins.voice_live.protocol import pack_audio_frame, unpack_audio_frame


def test_audio_frame_round_trip() -> None:
    packed = pack_audio_frame(7, 16_000, b"\x01\x00\xff\x7f")

    frame = unpack_audio_frame(packed)

    assert frame.sequence == 7
    assert frame.sample_rate == 16_000
    assert frame.pcm16 == b"\x01\x00\xff\x7f"


@pytest.mark.parametrize("payload", [b"", b"VL1", b"BAD!" + b"\x00" * 12])
def test_audio_frame_rejects_invalid_protocol(payload: bytes) -> None:
    with pytest.raises(ValueError):
        unpack_audio_frame(payload)


def test_pcm_conversion_clamps_nonfinite_values() -> None:
    pcm = float32_bytes_to_pcm16(
        struct.pack("<ffff", -2.0, 0.5, 2.0, math.nan)
    )
    restored = pcm16_to_float32_bytes(pcm)

    assert len(pcm) == 8
    assert len(restored) == 16


def test_resample_pcm16_changes_sample_count() -> None:
    source = b"\x00\x00\xff\x7f" * 4

    result = resample_pcm16_mono(source, 8_000, 16_000)

    assert len(result) == len(source) * 2
