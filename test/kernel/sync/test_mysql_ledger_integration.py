from __future__ import annotations

import asyncio
import os
import sqlite3
from uuid import uuid4

import pytest

from src.kernel.sync import (
    LocalSyncStore,
    MySQLLedgerConfig,
    RemoteMySQLLedger,
    SyncCoordinator,
    SyncEnvelope,
)


def _config_from_environment() -> MySQLLedgerConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    return MySQLLedgerConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),
    )


@pytest.mark.asyncio
async def test_mysql_accepts_duplicate_quarantines_conflict_and_fetches(
    tmp_path,
) -> None:
    config = _config_from_environment()
    ledger = RemoteMySQLLedger(config)
    suffix = uuid4().hex
    envelope = SyncEnvelope.build(
        event_id=f"sync-contract-{suffix}",
        origin_node_id=f"node-contract-{suffix}",
        origin_sequence=1,
        occurred_at="2026-08-03T00:00:00+08:00",
        recorded_at="2026-08-03T00:00:01+08:00",
        event_type="sync.contract_probe",
        payload={"probe": suffix, "version": 1},
        visibility="shared",
    )
    exercise_conflict = os.environ.get("ELYSIUM_TEST_MYSQL_CONFLICT", "0") == "1"
    try:
        await ledger.initialize()
        health_before = await ledger.health()
        concurrent = await asyncio.gather(*(ledger.publish(envelope) for _ in range(8)))
        accepted = next(item for item in concurrent if item.status == "accepted")
        duplicate = await ledger.publish(envelope)
        conflict = None
        if exercise_conflict:
            conflict = await ledger.publish(
                SyncEnvelope.build(
                    event_id=envelope.event_id,
                    origin_node_id=envelope.origin_node_id,
                    origin_sequence=envelope.origin_sequence,
                    occurred_at=envelope.occurred_at,
                    recorded_at=envelope.recorded_at,
                    event_type=envelope.event_type,
                    payload={"probe": suffix, "version": 2},
                    visibility="shared",
                )
            )
        fetched = await ledger.fetch_after(
            accepted.remote_position - 1,
            limit=10,
            allowed_visibilities={"shared"},
        )
        local_sender = LocalSyncStore(tmp_path / "sender.sqlite3")
        coordinator_event_id = f"sync-coordinator-{suffix}"
        local_sender.enqueue(
            event_id=coordinator_event_id,
            occurred_at=envelope.occurred_at,
            recorded_at=envelope.recorded_at,
            event_type="sync.contract_probe",
            payload={"probe": suffix, "path": "coordinator"},
            visibility="shared",
            export_requested=True,
        )
        sender = SyncCoordinator(local_sender, ledger, base_backoff_seconds=0)
        push_result = await sender.run_once(push=True, pull=False)
        sender_row = local_sender.debug_outbox_row(coordinator_event_id)

        local_receiver = LocalSyncStore(tmp_path / "receiver.sqlite3")
        local_receiver.ensure_schema()
        with sqlite3.connect(local_receiver.database_path) as db:
            db.execute(
                """INSERT INTO sync_cursors (consumer_id, remote_position, updated_at)
                VALUES ('integration.receiver', ?, 'now')""",
                (accepted.remote_position,),
            )
        applied: list[str] = []

        async def apply_remote(item: SyncEnvelope) -> None:
            applied.append(item.event_id)

        receiver = SyncCoordinator(
            local_receiver,
            ledger,
            consumer_id="integration.receiver",
            base_backoff_seconds=0,
            apply_callback=apply_remote,
        )
        pull_result = await receiver.run_once(push=False, pull=True)
        health = await ledger.health()
    finally:
        await ledger.close()

    assert accepted.status == "accepted"
    assert accepted.remote_position > 0
    assert sum(item.status == "accepted" for item in concurrent) == 1
    assert sum(item.status == "duplicate" for item in concurrent) == 7
    assert duplicate.status == "duplicate"
    assert duplicate.remote_position == accepted.remote_position
    if exercise_conflict:
        assert conflict is not None
        assert conflict.status == "conflict"
        assert conflict.existing_hash == envelope.payload_hash
    assert [(position, item.event_id) for position, item in fetched] == [
        (accepted.remote_position, envelope.event_id)
    ]
    assert push_result.pushed == 1
    assert sender_row is not None and sender_row["state"] == "confirmed"
    assert pull_result.pulled == 1
    assert applied == [coordinator_event_id]
    assert health["total"] == health_before["total"] + 2
    assert health["open_conflict_count"] == health_before["open_conflict_count"] + int(
        exercise_conflict
    )
