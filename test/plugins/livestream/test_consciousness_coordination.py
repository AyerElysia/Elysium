"""Livestream contracts for real presence and retry-safe world perception."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.service.consciousness import ConsciousnessRegistry
from plugins.livestream.config import LivestreamConfig
from plugins.livestream.consciousness import LivestreamConsciousnessManager
from plugins.livestream.pipeline.llm_orchestrator import LLMOrchestrator
from plugins.livestream.platform.base import PlatformEvent


class _Service:
    """Minimal supported LifeEngine integration used by the manager."""

    def __init__(self) -> None:
        self.consciousness_registry = ConsciousnessRegistry()
        self.observations: list[dict[str, Any]] = []
        self.saves = 0
        self.commits: list[Any] = []

    def save_consciousness_registry(self) -> None:
        self.saves += 1

    async def report_world_observation(
        self,
        report: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        self.observations.append({"report": report, **kwargs})
        return {"assertion_id": f"a-{len(self.observations)}"}

    def prepare_perception(self, instance_id: str) -> Any:
        return SimpleNamespace(
            instance_id=instance_id,
            content="other-instance-is-present",
        )

    def commit_perception(self, prepared: Any) -> None:
        self.commits.append(prepared)


def _config() -> LivestreamConfig:
    """Return one deterministic room configuration."""

    config = LivestreamConfig()
    config.platform.room_id = "room-test"
    return config


@pytest.mark.asyncio
async def test_livestream_uses_real_registry_and_observation_events() -> None:
    service = _Service()
    manager = LivestreamConsciousnessManager(_config(), service=service)

    instance = await manager.activate()

    assert instance is service.consciousness_registry.get(manager.instance_id)
    assert instance.is_active
    assert service.observations[-1]["source_instance_id"] == manager.instance_id
    assert service.observations[-1]["subject"] == manager.stream_id
    await manager.suspend(reason="test-ended")
    assert instance.status == "suspended"
    assert "test-ended" in service.observations[-1]["report"]


@pytest.mark.asyncio
async def test_livestream_perception_is_transient_and_committed_after_response() -> None:
    service = _Service()
    manager = LivestreamConsciousnessManager(_config(), service=service)
    await manager.activate()
    orchestrator = LLMOrchestrator(_config(), consciousness=manager)
    orchestrator._client = object()  # The call boundary is replaced below.
    captured: list[list[dict[str, str]]] = []

    async def call_llm(messages: list[dict[str, str]]) -> str:
        captured.append(messages)
        return "自然回应"

    orchestrator._call_llm = call_llm
    event = PlatformEvent(
        kind="danmaku",
        user_name="Ayer",
        content="你好",
        timestamp=1.0,
    )

    response = await orchestrator.generate_response([event])

    assert response == "自然回应"
    assert any(
        "other-instance-is-present" in message["content"]
        for message in captured[0]
    )
    assert all(
        "other-instance-is-present" not in message["content"]
        for message in orchestrator._history
    )
    assert len(service.commits) == 1


@pytest.mark.asyncio
async def test_livestream_requires_environment_credential_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup fails explicitly when the configured secret owner is empty."""

    config = _config()
    monkeypatch.delenv(config.pipeline.llm_api_key_env, raising=False)
    orchestrator = LLMOrchestrator(config)

    with pytest.raises(RuntimeError, match=config.pipeline.llm_api_key_env):
        await orchestrator.start()


@pytest.mark.asyncio
async def test_livestream_failure_keeps_perception_retryable() -> None:
    """A failed model request cannot acknowledge its world cursor window."""

    service = _Service()
    manager = LivestreamConsciousnessManager(_config(), service=service)
    await manager.activate()
    orchestrator = LLMOrchestrator(_config(), consciousness=manager)
    orchestrator._client = object()

    async def fail(_: list[dict[str, str]]) -> str:
        raise RuntimeError("provider unavailable")

    orchestrator._call_llm = fail
    event = PlatformEvent(
        kind="danmaku",
        user_name="Ayer",
        content="你好",
        timestamp=1.0,
    )

    assert await orchestrator.generate_response([event]) == ""
    assert service.commits == []
