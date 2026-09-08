"""Minecraft adapters preserve the shared ledger's durable replay contracts."""
from __future__ import annotations
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.minecraft.config import MinecraftConfig
from plugins.minecraft.service import MinecraftService


def _DummyPlugin(config):
    return SimpleNamespace(config=config)


def _config(tmp_path: Path) -> LifeEngineConfig:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return config


def _adapter(life: LifeEngineService) -> MinecraftService:
    service = MinecraftService(SimpleNamespace(config=MinecraftConfig()))
    service._life = life
    return service


def _body_occurrence(index: int = 1) -> tuple[dict[str, Any], dict[str, Any]]:
    return ({
        "schema": "minecraft.body_event.v1",
        "event_id": f"mc-body-event-{index}",
        "sequence": index,
        "instance_id": "bot-session-1",
        "kind": "minecraft.chat.received",
        "occurred_at": "2026-09-06T10:00:00+00:00",
        "payload": {"username": "Player", "message": "爱莉，跟我走吧"},
    }, {
        "schema": "minecraft.body_event_context.v1",
        "session_id": "session-1",
        "stream_id": "game.minecraft.session-1",
        "instance_id": "minecraft-session-1",
        "body_name": "bot",
    })


@pytest.mark.parametrize("failure_phase", ["append", "checkpoint"])
async def test_minecraft_body_event_retries_exact_occurrence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str,
) -> None:
    service = LifeEngineService(_DummyPlugin(_config(tmp_path)))
    adapter = _adapter(service)
    original_publish = service._publish_raw_events
    published: list[Any] = []
    checkpoints = 0

    async def publish(events: list[Any]) -> None:
        published.append(events[0])
        if failure_phase == "append" and len(published) == 1:
            raise OSError("injected MC append failure")
        await original_publish(events)

    async def save(*_: Any, **__: Any) -> None:
        nonlocal checkpoints
        checkpoints += 1
        if failure_phase == "checkpoint" and checkpoints == 1:
            raise OSError("injected MC checkpoint failure")

    monkeypatch.setattr(service, "_publish_raw_events", publish)
    monkeypatch.setattr(service, "_save_runtime_context", save)
    body, context = _body_occurrence()
    with pytest.raises(OSError, match="injected MC"):
        await adapter.record_minecraft_body_event(body, context)
    if failure_phase == "append":
        assert not service._pending_events
    results = await asyncio.gather(*(
        adapter.record_minecraft_body_event(body, context) for _ in range(8)
    ))
    assert all(event is results[0] for event in results)
    assert published == [results[0], results[0]]
    assert len(service._pending_events) == 1
    stored = [
        item for item in await service._get_event_bus().store.read_tail(10)
        if item.source == "minecraft"
    ]
    assert len(stored) == 1
    assert stored[0].source_instance_id == context["instance_id"]
    assert results[0].content == body["payload"]["message"]


async def test_minecraft_body_event_replay_survives_restart_and_cache_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.storage.event_contracts import LifeEventOccurrenceConflict

    async def save(*_: Any, **__: Any) -> None:
        return None

    service = LifeEngineService(_DummyPlugin(_config(tmp_path)))
    monkeypatch.setattr(service, "_save_runtime_context", save)
    adapter = _adapter(service)
    body, context = _body_occurrence()
    original = await adapter.record_minecraft_body_event(body, context)
    for index in range(2, 259):
        await adapter.record_minecraft_body_event(*_body_occurrence(index))
    assert len(service._external_event_cache) == 256
    assert len(service._external_recorded_event_ids) == 256
    replay = await adapter.record_minecraft_body_event(body, context)
    assert replay.sequence == original.sequence

    restarted = LifeEngineService(_DummyPlugin(_config(tmp_path)))
    monkeypatch.setattr(restarted, "_save_runtime_context", save)
    restarted_adapter = _adapter(restarted)
    replay = await restarted_adapter.record_minecraft_body_event(body, context)
    assert replay.sequence == original.sequence
    assert replay.raw_content == original.raw_content
    stored = await restarted._get_event_bus().store.read_tail(300)
    assert len([item for item in stored if item.source == "minecraft"]) == 258
    changed = {**body, "payload": {"username": "Player", "message": "不同内容"}}
    with pytest.raises(LifeEventOccurrenceConflict):
        await restarted_adapter.record_minecraft_body_event(changed, context)
    restarted._external_event_cache.clear()
    restarted._external_recorded_event_ids.clear()
    with pytest.raises(LifeEventOccurrenceConflict):
        await restarted_adapter.record_minecraft_body_event(changed, context)


async def test_minecraft_decision_record_retry_is_exact_and_not_duplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw-write ambiguity retries the same event before body execution."""

    service = LifeEngineService(_DummyPlugin(_config(tmp_path)))
    adapter = _adapter(service)
    published: list[Any] = []
    saves = 0

    async def publish(events: list[Any]) -> None:
        published.append(events[0])
        if len(published) == 1:
            raise OSError("temporary event backend failure")

    async def save(*_: Any, **__: Any) -> None:
        nonlocal saves
        saves += 1

    monkeypatch.setattr(service, "_publish_raw_events", publish)
    monkeypatch.setattr(service, "_save_runtime_context", save)
    decision = {
        "schema": "minecraft.consciousness_decision.v1",
        "decision_id": "minecraft_decision_" + "a" * 64,
        "kind": "pursue",
        "turn_index": 1,
        "authored_at": "2026-08-30T12:00:00+00:00",
        "intention": "去看看山后面",
        "reason": "我想知道那里有什么",
        "reconsider_after_seconds": None,
    }
    context = {
        "schema": "minecraft.consciousness_turn_reference.v1",
        "session_id": "session-1",
        "stream_id": "game.minecraft.session-1",
        "instance_id": "minecraft-session-1",
        "body_name": "agent",
        "turn_index": 1,
        "wake_reasons": ["session_started"],
        "subject": {"projection_sha256": "b" * 64},
        "perception": {"observation": {"observation_id": "observation-1"}},
        "recent_outcome_decision_ids": [],
    }

    with pytest.raises(OSError, match="temporary event backend failure"):
        await adapter.record_minecraft_consciousness_decision(decision, context)
    event = await adapter.record_minecraft_consciousness_decision(decision, context)
    replayed = await adapter.record_minecraft_consciousness_decision(decision, context)

    assert published == [event, event]
    assert replayed is event
    assert saves == 1
    assert [item.event_id for item in service._pending_events] == [event.event_id]
    assert event.source_instance_id == "minecraft-session-1"
    assert event.occurrence_id == decision["decision_id"]
    assert "去看看山后面" in str(event.raw_content)

