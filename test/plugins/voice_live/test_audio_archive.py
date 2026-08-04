from __future__ import annotations

import hashlib
import json
import stat
import wave

from plugins.voice_live.audio_archive import AudioTrackSpec, VoiceAudioArchive
from plugins.voice_live.runtime_store import VoiceEpisodeStore


async def test_audio_archive_writes_standard_tracks_and_manifest(tmp_path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_archive", "episode")
    archive = VoiceAudioArchive(store, fsync_interval_seconds=0.05)
    user_pcm = b"\x01\x00" * 320
    assistant_pcm = b"\x02\x00" * 480

    await archive.start(
        [
            AudioTrackSpec("user_input", 16000, "user", "provider_input"),
            AudioTrackSpec(
                "assistant_source",
                24000,
                "assistant",
                "provider_output_before_voice_conversion",
            ),
        ],
        metadata={"provider": "fake", "model": "voice-model"},
    )
    assert archive.append("user_input", user_pcm, 16000) is True
    assert archive.append("assistant_source", assistant_pcm, 24000) is True
    archive.update_metadata(subject_context_revision="subject-v1")
    cursors = archive.cursor_snapshot()
    assert cursors["user_input"]["samples_enqueued"] == 320
    assert cursors["assistant_source"]["samples_enqueued"] == 480

    manifest = await archive.close(reason="test_complete")

    assert manifest["state"] == "closed"
    assert manifest["reason"] == "test_complete"
    assert manifest["metadata"]["subject_context_revision"] == "subject-v1"
    assert (
        manifest["tracks"]["user_input"]["sha256_pcm"]
        == hashlib.sha256(user_pcm).hexdigest()
    )
    assert (
        manifest["tracks"]["assistant_source"]["sha256_pcm"]
        == hashlib.sha256(assistant_pcm).hexdigest()
    )

    for name, sample_rate, expected in (
        ("user_input", 16000, user_pcm),
        ("assistant_source", 24000, assistant_pcm),
    ):
        path = store.directory / "audio" / f"{name}.wav"
        with wave.open(str(path), "rb") as recording:
            assert recording.getnchannels() == 1
            assert recording.getsampwidth() == 2
            assert recording.getframerate() == sample_rate
            assert recording.readframes(recording.getnframes()) == expected
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    persisted = json.loads(
        (store.directory / "audio" / "manifest.json").read_text(encoding="utf-8")
    )
    assert persisted == manifest
    assert stat.S_IMODE((store.directory / "audio").stat().st_mode) == 0o700


async def test_audio_archive_resume_appends_without_losing_prior_samples(
    tmp_path,
) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_resume", "episode")
    spec = AudioTrackSpec("user_input", 16000, "user", "provider_input")
    first_pcm = b"\x03\x00" * 160
    second_pcm = b"\x04\x00" * 160

    first = VoiceAudioArchive(store)
    await first.start([spec], metadata={"provider": "fake"})
    assert first.append("user_input", first_pcm, 16000) is True
    first_manifest = await first.close(reason="transport_lost")

    resumed = VoiceAudioArchive(store)
    await resumed.start([spec], metadata={"resume": True})
    assert resumed.cursor_snapshot()["user_input"]["samples_enqueued"] == 160
    assert resumed.append("user_input", second_pcm, 16000) is True
    final_manifest = await resumed.close(reason="normal")

    assert final_manifest["started_at"] == first_manifest["started_at"]
    assert final_manifest["metadata"] == {"provider": "fake", "resume": True}
    assert final_manifest["tracks"]["user_input"]["samples"] == 320
    assert (
        final_manifest["tracks"]["user_input"]["sha256_pcm"]
        == hashlib.sha256(first_pcm + second_pcm).hexdigest()
    )
    with wave.open(
        str(store.directory / "audio" / "user_input.wav"), "rb"
    ) as recording:
        assert recording.readframes(recording.getnframes()) == first_pcm + second_pcm


async def test_audio_archive_repairs_stale_header_and_partial_sample(tmp_path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_crash", "episode")
    spec = AudioTrackSpec("user_input", 16000, "user", "provider_input")
    pcm = b"\x05\x00" * 80
    archive = VoiceAudioArchive(store)
    await archive.start([spec])
    assert archive.append("user_input", pcm, 16000) is True
    await archive.close(reason="before_crash")

    path = store.directory / "audio" / "user_input.wav"
    with path.open("r+b") as handle:
        handle.seek(4)
        handle.write((36).to_bytes(4, "little"))
        handle.seek(40)
        handle.write((0).to_bytes(4, "little"))
        handle.seek(0, 2)
        handle.write(b"\xff")

    recovered = VoiceAudioArchive(store)
    await recovered.start([spec])
    manifest = await recovered.close(reason="recovered")

    assert manifest["tracks"]["user_input"]["pcm_bytes"] == len(pcm)
    with wave.open(str(path), "rb") as recording:
        assert recording.getnframes() == 80
        assert recording.readframes(recording.getnframes()) == pcm
