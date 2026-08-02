from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from plugins.voice_live.audio import (
    float32_bytes_to_pcm16,
    pcm16_to_float32_bytes,
    resample_pcm16_mono,
)
from plugins.voice_live.protocol import pack_audio_frame, unpack_audio_frame
from plugins.voice_live.runtime_store import VoiceEpisodeStore


def test_audio_frame_round_trip_and_validation() -> None:
    pcm = b"\x00\x00\xff\x7f\x00\x80"
    packed = pack_audio_frame(41, 16000, pcm, flags=1)
    frame = unpack_audio_frame(packed)
    assert (frame.sequence, frame.sample_rate, frame.flags, frame.pcm16) == (
        41,
        16000,
        1,
        pcm,
    )
    with pytest.raises(ValueError):
        unpack_audio_frame(b"legacy raw pcm")
    with pytest.raises(ValueError):
        pack_audio_frame(1, 16000, b"\x00")


def test_pcm_conversion_and_resampling() -> None:
    source = b"\x00\x80\x00\x00\xff\x7f"
    restored = float32_bytes_to_pcm16(pcm16_to_float32_bytes(source))
    assert len(restored) == len(source)
    upsampled = resample_pcm16_mono(source, 16000, 24000)
    assert len(upsampled) == round(3 * 24000 / 16000) * 2
    assert resample_pcm16_mono(source, 16000, 16000) == source


def test_store_recovers_sequence_and_ignores_torn_tail(tmp_path: Path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_live_a", "a")
    assert store.append("session.started", {"value": 1}).sequence == 1
    store.checkpoint("active")
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"sequence":')

    recovered = VoiceEpisodeStore(tmp_path, "voice_live_a", "a")
    assert recovered.append("session.recovered", {}).sequence == 2
    records = recovered.read_all()
    assert [record.event for record in records] == [
        "session.started",
        "session.recovered",
    ]
    assert recovered.load_checkpoint()["state"] == "active"


@pytest.mark.asyncio
async def test_store_async_appends_are_serialized(tmp_path: Path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_live_parallel", "parallel")
    await asyncio.gather(
        *(store.append_async("event", {"index": index}) for index in range(24))
    )
    records = store.read_all()
    assert len(records) == 24
    assert sorted(record.sequence for record in records) == list(range(1, 25))
    for line in store.events_path.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["schema_version"] == 1
