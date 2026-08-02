"""Contract tests for event-sourced world projection and perception delivery."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_bus import (
    LifeEvent,
    LifeEventChannel,
    RawEventStore,
)
from plugins.life_engine.service.perception_gateway import PerceptionGateway
from plugins.life_engine.service.tool_manifests import get_tool_manifest
from plugins.life_engine.service.world_projection import (
    PerceptionCursorConflict,
    WORLD_OBSERVATION_EVENT,
    WorldProjectionStore,
)
from plugins.life_engine.service.world_state import (
    RelationshipState,
    WorldState,
    WorldStateMigrationError,
)


@dataclass
class _Plugin:
    """Minimal plugin wrapper for an isolated LifeEngine service."""

    config: object


def _service(tmp_path: Path) -> LifeEngineService:
    """Create one service whose complete durable runtime lives under tmp_path."""

    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(_Plugin(config))


def _observation(
    identity: str,
    *,
    source_instance_id: str,
    value: object,
    retracts: str = "",
) -> LifeEvent:
    """Build one immutable observation event for store-level tests."""

    assertion = {
        "assertion_id": identity,
        "subject": "person:ayer",
        "predicate": "current_state",
        "value": value,
        "domain": "relationship",
        "status": "observed",
        "source_instance_id": "untrusted-content-spoof",
        "observed_at": "2026-08-02T12:00:00+08:00",
        "valid_from": "2026-08-02T12:00:00+08:00",
        "retracts_assertion_id": retracts,
    }
    return LifeEvent(
        event_id=f"event-{identity}",
        sequence=0,
        timestamp="2026-08-02T12:00:00+08:00",
        source="test.world",
        channel=LifeEventChannel.LIFE.value,
        event_type=WORLD_OBSERVATION_EVENT,
        content=json.dumps({"assertion": assertion}, ensure_ascii=False),
        stream_id="stream-test",
        occurrence_id=f"occurrence-{identity}",
        source_instance_id=source_instance_id,
    )


def test_projection_retains_contradictions_provenance_and_explicit_retraction(
    tmp_path: Path,
) -> None:
    """Conflicting testimony coexists and only explicit evidence retracts it."""

    ledger = RawEventStore(tmp_path)
    ledger.append_sync(
        _observation("a", source_instance_id="chat_global", value="calm")
    )
    ledger.append_sync(
        _observation("b", source_instance_id="voice_1", value="worried")
    )
    ledger.append_sync(
        _observation(
            "c",
            source_instance_id="chat_global",
            value="I no longer hold assertion a",
            retracts="a",
        )
    )
    projection = WorldProjectionStore(tmp_path / "projection.sqlite3")

    frontier = projection.catch_up(ledger)
    assertions = projection.list_assertions()

    assert frontier == 3
    assert [item.assertion_id for item in assertions] == ["a", "b", "c"]
    assert [item.value for item in assertions[:2]] == ["calm", "worried"]
    assert assertions[0].retracted_by_assertion_id == "c"
    assert assertions[1].retracted_at == ""
    assert assertions[0].source_instance_id == "chat_global"
    assert assertions[1].source_instance_id == "voice_1"
    assert assertions[0].source_event_id == "event-a"


def test_projection_rebuild_is_equivalent_and_idempotent(tmp_path: Path) -> None:
    """Deleting derived state and replaying produces the same canonical view."""

    ledger = RawEventStore(tmp_path)
    first = _observation(
        "stable",
        source_instance_id="chat_global",
        value={"complete": "content"},
    )
    ledger.append_sync(first)
    ledger.append_sync(first)
    original = WorldProjectionStore(tmp_path / "original.sqlite3")
    original.catch_up(ledger)
    expected = original.canonical_snapshot()

    rebuilt = WorldProjectionStore(tmp_path / "rebuilt.sqlite3")
    rebuilt.rebuild(ledger)

    assert rebuilt.canonical_snapshot() == expected
    assert len(rebuilt.list_assertions()) == 1


def test_gateway_cursor_is_commit_after_delivery_and_survives_restart(
    tmp_path: Path,
) -> None:
    """Prepare is retryable; commit is CAS-protected and durable per instance."""

    ledger = RawEventStore(tmp_path)
    registry = ConsciousnessRegistry()
    voice = registry.register(
        ConsciousnessInstance(
            instance_id="voice_1",
            kind="voice_live",
            stream_ids=["voice-stream"],
        )
    )
    registry.flush_lifecycle_events(ledger.append_sync)
    ledger.append_sync(
        _observation("world-a", source_instance_id="chat_global", value="known")
    )
    projection_path = tmp_path / "projection.sqlite3"
    gateway = PerceptionGateway(
        registry,
        ledger,
        WorldProjectionStore(projection_path),
    )

    prepared = gateway.prepare(voice.instance_id)

    assert "chat_global" in prepared.content
    assert "voice_1" in prepared.content
    assert "world-a" in prepared.content
    assert gateway.projection.perception_cursor("voice_1") == (0, 0)
    committed_position, committed_revision = gateway.commit(prepared)
    assert committed_position == prepared.through_position
    assert committed_revision == 1
    with pytest.raises(PerceptionCursorConflict):
        gateway.commit(prepared)

    restarted = PerceptionGateway(
        registry,
        ledger,
        WorldProjectionStore(projection_path),
    )
    after_restart = restarted.prepare("voice_1")
    assert after_restart.from_position == prepared.through_position
    assert after_restart.change_positions == ()

    ledger.append_sync(
        _observation("world-b", source_instance_id="chat_global", value="new")
    )
    delta = restarted.prepare("voice_1")
    assert delta.from_position == prepared.through_position
    assert delta.change_positions
    assert "world-b" in delta.content


@pytest.mark.asyncio
async def test_service_end_to_end_syncs_instances_and_preserves_full_content(
    tmp_path: Path,
) -> None:
    """Presence, report, projection, delivery, and restart form one closed loop."""

    service = _service(tmp_path)
    voice = service.consciousness_registry.register(
        ConsciousnessInstance(
            instance_id="voice_episode",
            kind="voice_live",
            stream_ids=["voice-stream"],
            session_id="episode",
        )
    )
    service.save_consciousness_registry()
    full_report = "完整观察-" + ("没有被代码截断。" * 200)
    receipt = await service.report_world_observation(
        full_report,
        source_instance_id=voice.instance_id,
        subject="voice-stream",
        predicate="session_state",
        domain="voice_live",
        stream_id="voice-stream",
    )

    chat_view = service.prepare_perception("chat_global")
    voice_view = service.prepare_perception(voice.instance_id)

    assert voice.instance_id in chat_view.content
    assert "chat_global" in voice_view.content
    assert full_report in chat_view.content
    assertion = service.world_projection.list_assertions()[-1]
    assert assertion.value == full_report
    assert assertion.source_instance_id == voice.instance_id
    assert assertion.assertion_id == receipt["assertion_id"]
    service.commit_perception(voice_view)

    restarted = _service(tmp_path)
    restarted_view = restarted.prepare_perception(voice.instance_id)
    assert restarted_view.from_position == voice_view.through_position
    assert full_report in restarted_view.content


@pytest.mark.asyncio
async def test_chatter_world_cursor_commits_only_after_model_success_hook(
    tmp_path: Path,
) -> None:
    """Prompt assembly alone does not consume a chatter instance's world delta."""

    service = _service(tmp_path)
    stream = SimpleNamespace(stream_id="chat-stream")

    context, high_water = await service.build_chatter_runtime_context(
        stream,
        unified_chatter_context=True,
        include_recent_chat_history=False,
    )
    prepared = service._pending_chatter_perceptions[
        service._chatter_cursor_key(
            "chat-stream",
            unified_chatter_context=True,
        )
    ]

    assert "chat_global" in context
    assert service.world_projection.perception_cursor("chat_global") == (0, 0)
    await service.mark_chatter_runtime_context_seen(
        "chat-stream",
        high_water,
        unified_chatter_context=True,
    )
    assert service.world_projection.perception_cursor("chat_global")[0] == (
        prepared.through_position
    )


def test_legacy_world_snapshot_is_imported_once_and_source_is_preserved(
    tmp_path: Path,
) -> None:
    """Legacy JSON becomes ledger evidence exactly once and remains on disk."""

    legacy_path = tmp_path / "runtime" / "world_state.json"
    legacy = WorldState(
        relationships={
            "ayer": RelationshipState(
                entity_id="ayer",
                display_name="Ayer",
                status_summary="legacy-state",
            )
        },
        revision=7,
    )
    legacy.save(legacy_path)
    source_before = legacy_path.read_text(encoding="utf-8")

    first = _service(tmp_path)
    first_assertions = first.world_projection.list_assertions()
    first_event_count = first._get_event_bus().store.health_snapshot()["total"]
    second = _service(tmp_path)
    second_assertions = second.world_projection.list_assertions()
    second_event_count = second._get_event_bus().store.health_snapshot()["total"]

    assert len(first_assertions) == 1
    assert first_assertions[0].value["status_summary"] == "legacy-state"
    assert [item.to_dict() for item in second_assertions] == [
        item.to_dict() for item in first_assertions
    ]
    assert second_event_count == first_event_count
    assert legacy_path.read_text(encoding="utf-8") == source_before


def test_corrupt_legacy_world_snapshot_blocks_lossy_reset(tmp_path: Path) -> None:
    """Corrupt migration input fails explicitly instead of becoming empty truth."""

    legacy_path = tmp_path / "runtime" / "world_state.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(WorldStateMigrationError):
        _service(tmp_path)
    assert legacy_path.read_text(encoding="utf-8") == "{not-json"


def test_unknown_consciousness_kind_has_no_chat_capability_fallback() -> None:
    """A new runtime kind must declare its powers instead of inheriting chat."""

    with pytest.raises(KeyError):
        get_tool_manifest("undeclared-consciousness-kind")
