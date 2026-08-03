from __future__ import annotations

import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.event_bus import (
    LifeEvent,
    RawEventStore,
    life_event_to_dict,
)
from src.kernel.sync import LocalSyncStore, SyncCoordinator, SyncEnvelope
from src.kernel.sync.models import PublishResult


def _envelope(
    event_id: str,
    *,
    node_id: str = "node_remote",
    sequence: int = 1,
    payload: dict[str, Any] | None = None,
) -> SyncEnvelope:
    return SyncEnvelope.build(
        event_id=event_id,
        origin_node_id=node_id,
        origin_sequence=sequence,
        occurred_at="2026-08-03T00:00:00+08:00",
        recorded_at="2026-08-03T00:00:01+08:00",
        event_type="sync.test",
        payload=payload or {"event_id": event_id},
        visibility="shared",
    )


def _enqueue(local: LocalSyncStore, event_id: str) -> None:
    local.enqueue(
        event_id=event_id,
        occurred_at="2026-08-03T00:00:00+08:00",
        recorded_at="2026-08-03T00:00:01+08:00",
        event_type="sync.test",
        payload={"event_id": event_id},
        visibility="shared",
        export_requested=True,
    )


class MemoryRemote:
    def __init__(self) -> None:
        self.available = True
        self.initialized = 0
        self.events: dict[str, tuple[int, SyncEnvelope]] = {}
        self.by_origin: dict[tuple[str, int], str] = {}
        self.remote_rows: list[tuple[int, SyncEnvelope]] = []

    async def initialize(self) -> None:
        self.initialized += 1
        if not self.available:
            raise ConnectionError("offline")

    async def publish(self, envelope: SyncEnvelope) -> PublishResult:
        if not self.available:
            raise ConnectionError("offline")
        existing = self.events.get(envelope.event_id)
        if existing is not None:
            position, current = existing
            if current.payload_hash == envelope.payload_hash and (
                current.origin_node_id,
                current.origin_sequence,
            ) == (envelope.origin_node_id, envelope.origin_sequence):
                return PublishResult(status="duplicate", remote_position=position)
            return PublishResult(
                status="conflict",
                remote_position=position,
                conflict_reason="event id collision",
                existing_hash=current.payload_hash,
            )
        origin = (envelope.origin_node_id, envelope.origin_sequence)
        if origin in self.by_origin:
            current = self.events[self.by_origin[origin]][1]
            return PublishResult(
                status="conflict",
                conflict_reason="origin sequence collision",
                existing_hash=current.payload_hash,
            )
        position = len(self.events) + 1
        self.events[envelope.event_id] = (position, envelope)
        self.by_origin[origin] = envelope.event_id
        return PublishResult(status="accepted", remote_position=position)

    async def fetch_after(
        self,
        remote_position: int,
        *,
        limit: int,
        allowed_visibilities: set[str],
    ) -> list[tuple[int, SyncEnvelope]]:
        if not self.available:
            raise ConnectionError("offline")
        return [
            row
            for row in self.remote_rows
            if row[0] > remote_position and row[1].visibility in allowed_visibilities
        ][:limit]

    async def close(self) -> None:
        return None


def test_life_event_and_outbox_are_one_transaction(tmp_path, monkeypatch) -> None:
    store = RawEventStore(tmp_path)
    event = LifeEvent(
        event_id="source-event-1",
        occurrence_id="occurrence-1",
        sequence=0,
        timestamp="2026-08-03T00:00:00+08:00",
        source="test",
        channel="life",
        event_type="sync.test",
        content="hello",
        metadata={"visibility": "shared", "sync_export": True},
    )
    persisted = store.append_sync(event)
    local = LocalSyncStore(store.database_path)
    row = local.debug_outbox_row("occurrence-1")
    assert persisted.sequence == 1
    assert row is not None
    assert row["state"] == "pending"
    assert row["origin_sequence"] == 1

    from plugins.life_engine.service import event_bus as event_bus_module

    def fail_enqueue(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(event_bus_module, "enqueue_in_transaction", fail_enqueue)
    with pytest.raises(RuntimeError, match="injected outbox failure"):
        store.append_sync(
            replace(event, event_id="source-event-2", occurrence_id="occurrence-2")
        )
    with sqlite3.connect(store.database_path) as db:
        raw_count = db.execute(
            "SELECT COUNT(*) FROM raw_life_events WHERE occurrence_id = 'occurrence-2'"
        ).fetchone()[0]
        outbox_count = db.execute(
            "SELECT COUNT(*) FROM sync_outbox WHERE event_id = 'occurrence-2'"
        ).fetchone()[0]
    assert raw_count == 0
    assert outbox_count == 0


def test_private_event_is_held_and_cannot_be_released(tmp_path) -> None:
    local = LocalSyncStore(tmp_path / "events.sqlite3")
    state = local.enqueue(
        event_id="private-1",
        occurred_at="now",
        recorded_at="now",
        event_type="memory.private",
        payload={"secret": True},
        visibility="private",
        export_requested=True,
    )
    assert state == "held"
    with pytest.raises(PermissionError, match="SyncOutboxPrivate"):
        local.release_held("private-1")
    row = local.debug_outbox_row("private-1")
    assert row is not None
    assert row["origin_sequence"] is None
    assert local.health_snapshot()["outbox_backlog"] == 0

    local_only = local.enqueue(
        event_id="private-local-only",
        occurred_at="now",
        recorded_at="now",
        event_type="memory.private",
        payload={"secret": "not duplicated"},
        visibility="private",
        export_requested=False,
    )
    assert local_only == "local_only"
    assert local.debug_outbox_row("private-local-only") is None


@pytest.mark.asyncio
async def test_offline_retry_reconnect_and_restart(tmp_path) -> None:
    local = LocalSyncStore(tmp_path / "events.sqlite3")
    _enqueue(local, "event-1")
    remote = MemoryRemote()
    remote.available = False
    first = SyncCoordinator(local, remote, base_backoff_seconds=0)
    with pytest.raises(ConnectionError, match="offline"):
        await first.run_once()
    row = local.debug_outbox_row("event-1")
    assert row is not None
    assert row["state"] == "pending"

    remote.available = True
    restarted = SyncCoordinator(
        LocalSyncStore(local.database_path),
        remote,
        base_backoff_seconds=0,
    )
    result = await restarted.run_once()
    assert result.pushed == 1
    row = local.debug_outbox_row("event-1")
    assert row is not None
    assert row["state"] == "confirmed"
    assert row["remote_position"] == 1


@pytest.mark.asyncio
async def test_remote_commit_before_local_ack_replays_as_duplicate(
    tmp_path, monkeypatch
) -> None:
    local = LocalSyncStore(tmp_path / "events.sqlite3")
    _enqueue(local, "event-1")
    remote = MemoryRemote()
    coordinator = SyncCoordinator(
        local, remote, lease_seconds=1, base_backoff_seconds=0
    )
    original_confirm = local.confirm

    def crash_before_ack(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("crash before local ack")

    monkeypatch.setattr(local, "confirm", crash_before_ack)
    with pytest.raises(RuntimeError, match="crash before local ack"):
        await coordinator.run_once()
    assert len(remote.events) == 1
    row = local.debug_outbox_row("event-1")
    assert row is not None and row["state"] == "inflight"

    with sqlite3.connect(local.database_path) as db:
        db.execute("UPDATE sync_outbox SET lease_until = 0 WHERE event_id = 'event-1'")
    monkeypatch.setattr(local, "confirm", original_confirm)
    restarted = SyncCoordinator(local, remote, lease_seconds=1, base_backoff_seconds=0)
    result = await restarted.run_once()
    assert result.duplicates == 1
    assert len(remote.events) == 1
    assert local.debug_outbox_row("event-1")["state"] == "confirmed"


@pytest.mark.asyncio
async def test_conflict_stops_ordered_delivery(tmp_path) -> None:
    local = LocalSyncStore(tmp_path / "events.sqlite3")
    _enqueue(local, "event-1")
    _enqueue(local, "event-2")
    first = local.debug_outbox_row("event-1")
    assert first is not None
    remote = MemoryRemote()
    conflicting = SyncEnvelope.build(
        event_id="event-1",
        origin_node_id=str(first["origin_node_id"]),
        origin_sequence=int(first["origin_sequence"]),
        occurred_at="other",
        recorded_at="other",
        event_type="sync.test",
        payload={"different": True},
        visibility="shared",
    )
    remote.events["event-1"] = (1, conflicting)
    remote.by_origin[(conflicting.origin_node_id, conflicting.origin_sequence)] = (
        "event-1"
    )
    coordinator = SyncCoordinator(local, remote, base_backoff_seconds=0)
    result = await coordinator.run_once()
    assert result.conflicts == 1
    assert local.debug_outbox_row("event-1")["state"] == "conflict"
    assert local.debug_outbox_row("event-2")["state"] == "pending"
    assert local.health_snapshot()["open_conflict_count"] == 1


@pytest.mark.asyncio
async def test_inbox_cursor_advances_only_after_successful_application(
    tmp_path,
) -> None:
    local = LocalSyncStore(tmp_path / "events.sqlite3")
    remote = MemoryRemote()
    remote.remote_rows = [(5, _envelope("remote-1"))]

    async def fail_apply(envelope: SyncEnvelope) -> None:
        raise RuntimeError(f"cannot apply {envelope.event_id}")

    first = SyncCoordinator(local, remote, apply_callback=fail_apply)
    result = await first.run_once(push=False, pull=True)
    assert result.failed == 1
    assert local.cursor("life_engine.shared_sync") == 0

    applied: list[str] = []

    async def apply(envelope: SyncEnvelope) -> None:
        applied.append(envelope.event_id)

    restarted = SyncCoordinator(local, remote, apply_callback=apply)
    result = await restarted.run_once(push=False, pull=True)
    assert result.pulled == 1
    assert applied == ["remote-1"]
    assert local.cursor("life_engine.shared_sync") == 5


def test_inbox_duplicate_and_conflict_are_durable(tmp_path) -> None:
    local = LocalSyncStore(tmp_path / "events.sqlite3")
    envelope = _envelope("remote-1")
    assert local.stage_inbox(1, envelope) == "staged"
    assert local.stage_inbox(1, envelope) == "duplicate"
    conflicting = _envelope("remote-1", payload={"different": True})
    assert local.stage_inbox(1, conflicting) == "conflict"
    assert local.health_snapshot()["open_conflict_count"] == 1


def test_shared_sync_config_is_off_and_secretless_by_default() -> None:
    config = LifeEngineConfig()
    assert config.shared_sync.enabled is False
    assert config.shared_sync.pull_enabled is False
    assert config.shared_sync.remote_password_env == "ELYSIUM_SYNC_MYSQL_PASSWORD"
    assert "password" not in config.shared_sync.model_dump()


def test_life_service_health_exposes_disabled_sync_without_connecting(tmp_path) -> None:
    from plugins.life_engine.service.core import LifeEngineService

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(SimpleNamespace(config=config))
    health = service.health()
    assert health["shared_sync"] == {
        "component": "offline_sync",
        "status": "disabled",
        "running": False,
        "outbox_backlog": 0,
        "degraded_reason": "",
        "enabled": False,
    }


@pytest.mark.asyncio
async def test_life_bridge_import_is_idempotent_and_never_echoes(
    tmp_path,
    monkeypatch,
) -> None:
    from plugins.life_engine.service import shared_sync as shared_sync_module

    raw_store = RawEventStore(tmp_path)
    remote = MemoryRemote()
    monkeypatch.setenv("SYNC_TEST_PASSWORD", "not-published")
    monkeypatch.setattr(
        shared_sync_module,
        "RemoteMySQLLedger",
        lambda config: remote,
    )
    section = SimpleNamespace(
        remote_host="db.invalid",
        remote_port=3306,
        remote_database="elysium",
        remote_user="elysia",
        remote_password_env="SYNC_TEST_PASSWORD",
        mysql_ssl_mode="disabled",
        mysql_ssl_ca="",
        mysql_ssl_cert="",
        mysql_ssl_key="",
        connect_timeout_seconds=1,
        allowed_visibilities=["shared"],
        consumer_id="test.consumer",
        batch_size=10,
        lease_seconds=5,
        base_backoff_seconds=0,
        max_backoff_seconds=1,
        poll_interval_seconds=0.1,
        push_enabled=True,
        pull_enabled=True,
    )
    bridge = shared_sync_module.SharedSyncBridge(section, raw_store)
    event = LifeEvent(
        event_id="remote-source-1",
        occurrence_id="remote-occurrence-1",
        sequence=1,
        timestamp="2026-08-03T00:00:00+08:00",
        source="remote",
        channel="life",
        event_type="sync.test",
        content="shared",
        metadata={"visibility": "shared", "sync_export": True},
    )
    envelope = SyncEnvelope.build(
        event_id=event.occurrence_id,
        origin_node_id="node-other",
        origin_sequence=1,
        occurred_at=event.timestamp,
        recorded_at="2026-08-03T00:00:01+08:00",
        event_type=event.event_type,
        payload=life_event_to_dict(event),
        visibility="shared",
    )
    await bridge._apply_remote_event(envelope)
    await bridge._apply_remote_event(envelope)
    events = await raw_store.read_tail(10)
    assert len(events) == 1
    assert events[0].metadata["sync_export"] is False
    assert events[0].metadata["sync_import_origin_node_id"] == "node-other"
    row = LocalSyncStore(raw_store.database_path).debug_outbox_row(event.occurrence_id)
    assert row is None
    health_text = str(bridge.health_snapshot())
    assert "not-published" not in health_text
    await bridge.close()
