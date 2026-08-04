from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.consciousness import VoiceLiveConsciousnessManager
from plugins.voice_live.runtime_store import VoiceEpisodeStore


class _ReadOnlyRegistrySnapshot:
    """Selected-backend view: consumers may inspect but cannot mutate it."""

    def __init__(self, registry: ConsciousnessRegistry) -> None:
        self._registry = registry

    def get(self, instance_id: str) -> Any:
        return self._registry.get(instance_id)

    def get_by_kind(self, kind: str) -> list[Any]:
        return self._registry.get_by_kind(kind)


class FakeLifeService:
    def __init__(self) -> None:
        self._registry = ConsciousnessRegistry()
        self.consciousness_registry = _ReadOnlyRegistrySnapshot(self._registry)
        self.registry_saves = 0
        self.lifecycle_calls: list[str] = []
        self.observations: list[dict[str, Any]] = []
        self.prepared = object()
        self.committed: list[Any] = []

    async def register_consciousness_instance(self, instance: Any) -> Any:
        self.lifecycle_calls.append("register")
        result = self._registry.register(instance)
        self.registry_saves += 1
        return result

    async def touch_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> None:
        self.lifecycle_calls.append("touch")
        self._registry.touch(instance_id, **kwargs)
        self.registry_saves += 1

    async def resume_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> bool:
        self.lifecycle_calls.append("resume")
        changed = self._registry.resume(instance_id, **kwargs)
        self.registry_saves += int(changed)
        return changed

    async def suspend_consciousness_instance(
        self,
        instance_id: str,
        **kwargs: Any,
    ) -> bool:
        self.lifecycle_calls.append("suspend")
        changed = self._registry.suspend(instance_id, **kwargs)
        self.registry_saves += int(changed)
        return changed

    async def prepare_perception(self, instance_id: str) -> Any:
        self.lifecycle_calls.append(f"prepare:{instance_id}")
        return self.prepared

    async def commit_perception(self, prepared: Any) -> tuple[int, int]:
        self.lifecycle_calls.append("commit")
        self.committed.append(prepared)
        return (1, 1)

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
    assert service.lifecycle_calls[:4] == ["register", "touch", "suspend", "resume"]

    prepared = await recovered.prepare_perception()
    await recovered.commit_perception(prepared)
    assert prepared is service.prepared
    assert service.committed == [service.prepared]


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
