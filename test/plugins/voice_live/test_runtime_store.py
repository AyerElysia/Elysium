from __future__ import annotations

import asyncio

from plugins.voice_live.runtime_store import VoiceEpisodeStore


async def test_episode_store_is_durable_and_recovers_sequence(tmp_path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_instance", "episode_1")
    first = await store.append_async("transcript.final", {"role": "user", "text": "hi"})
    await store.checkpoint_async("active", provider="fake")
    second = await store.append_async("provider.state", {"state": "listening"})

    recovered = VoiceEpisodeStore(tmp_path, "voice_instance", "episode_1")
    third = recovered.append("transcript.final", {"role": "assistant", "text": "hello"})

    assert (first.sequence, second.sequence, third.sequence) == (1, 2, 3)
    assert recovered.load_checkpoint()["provider"] == "fake"
    assert [item["text"] for item in recovered.transcript()] == ["hi", "hello"]


async def test_episode_store_serializes_concurrent_appends(tmp_path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_instance", "episode_2")

    records = await asyncio.gather(
        *(store.append_async("event", {"index": index}) for index in range(25))
    )

    assert sorted(record.sequence for record in records) == list(range(1, 26))
    assert len(store.read_all()) == 25


def test_episode_store_ignores_truncated_final_line(tmp_path) -> None:
    store = VoiceEpisodeStore(tmp_path, "voice_instance", "episode_3")
    store.append("event", {})
    with store.events_path.open("a", encoding="utf-8") as handle:
        handle.write('{"sequence":')

    recovered = VoiceEpisodeStore(tmp_path, "voice_instance", "episode_3")

    assert recovered.append("event", {}).sequence == 2
