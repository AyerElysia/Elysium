"""Architecture contracts for transactional consciousness presence."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.service.consciousness import (
    ConsciousnessInstance,
    ConsciousnessRegistry,
    PresenceMigrationError,
)
from plugins.life_engine.service.event_builder import EventBuilder
from plugins.life_engine.service.event_bus import RawEventStore, life_event_from_legacy
from plugins.life_engine.service.presence_store import (
    PresenceRevisionConflict,
    SQLitePresenceStore,
    StreamOwnershipConflict,
)
from plugins.life_engine.service.state_manager import event_from_dict, event_to_dict


def _instance(
    instance_id: str,
    stream_id: str,
    *,
    at: str = "2026-01-01T00:00:00+00:00",
) -> ConsciousnessInstance:
    return ConsciousnessInstance(
        instance_id=instance_id,
        kind="test.runtime",
        display_name=instance_id,
        stream_ids=[stream_id],
        created_at=at,
        last_active_at=at,
        session_id=f"session:{instance_id}",
        lease_duration_seconds=60,
    )


def test_stream_claim_is_atomic_and_released_on_suspend(tmp_path: Path) -> None:
    store = SQLitePresenceStore(tmp_path / "presence.sqlite3")
    registry = ConsciousnessRegistry(store=store, process_epoch="process:a")
    registry.register(_instance("instance:a", "stream:shared"))

    with pytest.raises(StreamOwnershipConflict):
        registry.register(_instance("instance:b", "stream:shared"))

    reopened = ConsciousnessRegistry(store=store, process_epoch="process:b")
    assert reopened.get("instance:a") is not None
    assert reopened.get("instance:b") is None

    assert reopened.suspend(
        "instance:a",
        timestamp="2026-01-01T00:00:01+00:00",
        reason="contract_test",
    )
    reopened.register(_instance("instance:b", "stream:shared"))
    assert reopened.get_for_stream("stream:shared").instance_id == "instance:b"


def test_stale_presence_revision_cannot_overwrite_newer_activity(
    tmp_path: Path,
) -> None:
    store = SQLitePresenceStore(tmp_path / "presence.sqlite3")
    first = ConsciousnessRegistry(store=store, process_epoch="process:first")
    first.register(_instance("instance:a", "stream:a"))
    stale = ConsciousnessRegistry(store=store, process_epoch="process:stale")

    first.touch(
        "instance:a",
        timestamp="2026-01-01T00:00:10+00:00",
        reason="newer_activity",
    )
    with pytest.raises(PresenceRevisionConflict):
        stale.touch(
            "instance:a",
            timestamp="2026-01-01T00:00:05+00:00",
            reason="stale_activity",
        )


def test_expired_lease_suspends_ghost_and_releases_stream(tmp_path: Path) -> None:
    store = SQLitePresenceStore(tmp_path / "presence.sqlite3")
    registry = ConsciousnessRegistry(store=store, process_epoch="process:a")
    instance = _instance("instance:ghost", "stream:ghost")
    instance.lease_duration_seconds = 1
    registry.register(instance)

    expired = registry.reconcile_expired(
        timestamp="2026-01-01T00:00:02+00:00"
    )

    assert expired == ["instance:ghost"]
    assert registry.get("instance:ghost").status == "suspended"
    replacement = _instance("instance:replacement", "stream:ghost")
    registry.register(replacement)
    assert registry.get_for_stream("stream:ghost") is replacement


@pytest.mark.asyncio
async def test_lifecycle_outbox_is_attributed_and_idempotent(
    tmp_path: Path,
) -> None:
    store = SQLitePresenceStore(tmp_path / "presence.sqlite3")
    registry = ConsciousnessRegistry(store=store, process_epoch="process:a")
    registry.register(_instance("instance:voice", "stream:voice"))
    ledger = RawEventStore(tmp_path / "ledger")

    published = registry.flush_lifecycle_events(ledger.append_sync)
    assert published == 2  # chat_global bootstrap + explicit instance
    assert registry.flush_lifecycle_events(ledger.append_sync) == 0

    events = await ledger.read_tail(limit=10)
    voice_events = [
        event
        for event in events
        if event.source_instance_id == "instance:voice"
    ]
    assert len(voice_events) == 1
    event = voice_events[0]
    assert event.event_type == "consciousness.instance_registered"
    assert event.correlation_id == "session:instance:voice"
    assert event.metadata["presence_revision"] == 1
    assert event.recorded_at


def test_message_ledger_preserves_full_content_and_instance_attribution() -> None:
    full_content = "x" * 400
    message = SimpleNamespace(
        platform="voice_live",
        chat_type="private",
        stream_id="voice:one",
        extra={
            "consciousness_instance_id": "instance:voice",
            "episode_id": "episode:one",
            "sender_platform_account_key": "voice_live:owner:1",
            "canonical_person_key": "ayer",
            "identity_resolution_status": "resolved",
        },
        sender_cardname="",
        sender_name="owner",
        sender_id="owner:1",
        processed_plain_text=full_content,
        content=full_content,
        message_type=SimpleNamespace(value="text"),
        message_id="message:one",
        time=None,
    )

    legacy = EventBuilder(lambda: 1).build_message_event(message)
    raw = life_event_from_legacy(legacy)

    assert len(legacy.content) == 240
    assert raw.content == full_content
    assert raw.source_instance_id == "instance:voice"
    assert raw.correlation_id == "episode:one"
    assert raw.metadata["sender_id"] == "owner:1"
    assert raw.metadata["sender_platform_account_key"] == "voice_live:owner:1"
    assert raw.metadata["canonical_person_key"] == "ayer"
    assert raw.metadata["identity_resolution_status"] == "resolved"

    restored = event_from_dict(event_to_dict(legacy))
    assert restored.raw_content == full_content
    assert restored.source_instance_id == "instance:voice"
    assert restored.correlation_id == "episode:one"
    assert restored.sender_id == "owner:1"
    assert restored.sender_platform_account_key == "voice_live:owner:1"
    assert restored.canonical_person_key == "ayer"
    assert restored.identity_resolution_status == "resolved"


def test_corrupt_legacy_registry_is_preserved_and_blocks_empty_reset(
    tmp_path: Path,
) -> None:
    """A damaged legacy snapshot must be repaired, never replaced silently."""

    legacy_path = tmp_path / "consciousness_registry.json"
    legacy_payload = "{not valid json"
    legacy_path.write_text(legacy_payload, encoding="utf-8")

    with pytest.raises(PresenceMigrationError):
        ConsciousnessRegistry.load(legacy_path)

    assert legacy_path.read_text(encoding="utf-8") == legacy_payload
    store = SQLitePresenceStore(tmp_path / "consciousness_presence.sqlite3")
    assert store.list_instances() == []


def test_legacy_stream_conflict_imports_without_overwriting_owner(
    tmp_path: Path,
) -> None:
    """Legacy ambiguity remains visible as a suspended migration record."""

    legacy_path = tmp_path / "consciousness_registry.json"
    first = _instance("instance:first", "stream:shared").to_dict()
    second = _instance("instance:second", "stream:shared").to_dict()
    first["lease_duration_seconds"] = None
    first["lease_expires_at"] = ""
    second["lease_duration_seconds"] = None
    second["lease_expires_at"] = ""
    legacy_path.write_text(
        json.dumps(
            {"schema_version": 1, "instances": {
                "instance:first": first,
                "instance:second": second,
            }},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry = ConsciousnessRegistry.load(legacy_path)

    assert registry.get("instance:first").status == "active"
    migrated = registry.get("instance:second")
    assert migrated.status == "suspended"
    assert "legacy_import_conflict" in migrated.metadata
    assert registry.get_for_stream("stream:shared").instance_id == "instance:first"
