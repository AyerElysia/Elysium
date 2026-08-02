"""Versioned browser/provider protocol helpers."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from enum import Enum

AUDIO_MAGIC = b"VL1\0"
AUDIO_HEADER = struct.Struct("<4sIII")
AUDIO_FLAG_END_OF_STREAM = 1


class SessionState(str, Enum):
    CREATED = "created"
    CONNECTING = "connecting"
    ACTIVE = "active"
    INTERRUPTING = "interrupting"
    STOPPING = "stopping"
    ENDED = "ended"
    FAILED = "failed"


class ProviderState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(slots=True, frozen=True)
class BrowserAudioFrame:
    sequence: int
    sample_rate: int
    flags: int
    pcm16: bytes


def pack_audio_frame(sequence: int, sample_rate: int, pcm16: bytes, *, flags: int = 0) -> bytes:
    """Pack one self-describing browser audio frame."""
    if sequence < 0 or sequence > 0xFFFFFFFF:
        raise ValueError("audio sequence must fit uint32")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if len(pcm16) % 2:
        raise ValueError("PCM16 payload length must be even")
    return AUDIO_HEADER.pack(AUDIO_MAGIC, sequence, sample_rate, flags) + pcm16


def unpack_audio_frame(data: bytes) -> BrowserAudioFrame:
    """Validate and unpack one browser audio frame."""
    if len(data) < AUDIO_HEADER.size:
        raise ValueError("audio frame is shorter than the protocol header")
    magic, sequence, sample_rate, flags = AUDIO_HEADER.unpack_from(data)
    if magic != AUDIO_MAGIC:
        raise ValueError("unsupported audio frame protocol")
    pcm16 = data[AUDIO_HEADER.size :]
    if len(pcm16) % 2:
        raise ValueError("PCM16 payload length must be even")
    return BrowserAudioFrame(sequence, sample_rate, flags, pcm16)


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000
