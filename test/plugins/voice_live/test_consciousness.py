from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.consciousness import VoiceLiveConsciousnessManager
from plugins.voice_live.runtime_store import VoiceEpisodeStore


class FakeLifeService:
    def __init__(self) -> None:
        self.consciousness_registry = ConsciousnessRegistry()
        self.registry_saves = 0
        self.observations: list[dict[str, Any]] = []

    def save_consciousness_registry(self) -> None:
        self.registry_saves += 1

    async def report_world_observation(
        self,
        report: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        self.observations.append({"report": report, **kwargs})
        return {"assertion_id": f"assertion-{len(self.observations)}"}


def make_config(tmp_path: Path) -> VoiceLiveConfig:
    config = VoiceLiveConfig()
    config.observability.trace_root = str(tmp_path)
    config.session.require_life_engine = True
    return config


@pytest.mark.asyncio
async def test_lifecycle_is_idempotent_resumable_and_persisted(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = FakeLifeService()
    store = VoiceEpisodeStore(tmp_path, "voice_live_episode", "episode")
    manager = VoiceLiveConsciousnessManager(
        config, "episode", store, service=service
    )

    first = await manager.activate("minicpm_omni")
    second = await manager.activate("minicpm_omni")
    assert first is second
    assert service.consciousness_registry.get("voice_live_episode") is first
    assert service.observations[-1]["subject"] == "voice_live_episode"
    assert service.observations[-1]["source_instance_id"] == first.instance_id
    assert first.metadata["tool_manifest"]

    await manager.suspend(reason="abnormal_exit")
    assert first.status == "suspended"
    assert "abnormal_exit" in service.observations[-1]["report"]

    recovered = VoiceLiveConsciousnessManager(
        config, "episode", store, service=service
    )
    resumed = await recovered.activate("minicpm_omni")
    assert resumed is first
    assert resumed.status == "active"
    assert service.registry_saves >= 4
    assert len(service.observations) >= 4


@pytest.mark.asyncio
async def test_episodes_do_not_share_transcripts_or_instance_ids(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    service = FakeLifeService()
    store_a = VoiceEpisodeStore(tmp_path, "voice_live_a", "a")
    store_b = VoiceEpisodeStore(tmp_path, "voice_live_b", "b")
    a = VoiceLiveConsciousnessManager(config, "a", store_a, service=service)
    b = VoiceLiveConsciousnessManager(config, "b", store_b, service=service)
    await a.activate("minicpm_omni")
    await b.activate("minicpm_omni")
    await store_a.append_async("transcript.final", {"role": "user", "text": "only-a"})
    await store_b.append_async("transcript.final", {"role": "user", "text": "only-b"})

    assert a.instance_id != b.instance_id
    assert store_a.transcript() == [{"role": "user", "text": "only-a"}]
    assert store_b.transcript() == [{"role": "user", "text": "only-b"}]
    assert len(service.consciousness_registry.get_by_kind("voice_live")) == 2
