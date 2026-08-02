"""Dependency-free mono PCM conversion used at provider boundaries."""

from __future__ import annotations

import array
import base64
import math
import struct


def pcm16_to_float32_bytes(pcm16: bytes) -> bytes:
    if len(pcm16) % 2:
        raise ValueError("PCM16 payload length must be even")
    samples = array.array("h")
    samples.frombytes(pcm16)
    return b"".join(struct.pack("<f", max(-1.0, min(1.0, value / 32768.0))) for value in samples)


def float32_bytes_to_pcm16(raw: bytes) -> bytes:
    if len(raw) % 4:
        raise ValueError("float32 PCM payload length must be divisible by four")
    result = array.array("h")
    for (value,) in struct.iter_unpack("<f", raw):
        if not math.isfinite(value):
            value = 0.0
        result.append(max(-32768, min(32767, round(value * 32767.0))))
    return result.tobytes()


def resample_pcm16_mono(pcm16: bytes, source_rate: int, target_rate: int) -> bytes:
    """Linearly resample a mono PCM16 block while preserving block boundaries."""
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    if source_rate == target_rate or not pcm16:
        return pcm16
    source = array.array("h")
    source.frombytes(pcm16)
    if len(source) == 1:
        return array.array("h", [source[0]]).tobytes()
    target_length = max(1, round(len(source) * target_rate / source_rate))
    scale = (len(source) - 1) / max(1, target_length - 1)
    target = array.array("h")
    for index in range(target_length):
        position = index * scale
        left = int(position)
        right = min(left + 1, len(source) - 1)
        fraction = position - left
        target.append(round(source[left] * (1.0 - fraction) + source[right] * fraction))
    return target.tobytes()


def float32_b64_to_pcm16(value: str) -> bytes:
    return float32_bytes_to_pcm16(base64.b64decode(value))


def pcm16_to_float32_b64(value: bytes) -> str:
    return base64.b64encode(pcm16_to_float32_bytes(value)).decode("ascii")
