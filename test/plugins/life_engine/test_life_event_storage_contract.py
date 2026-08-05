"""Shared semantic contract for the selectable authoritative event ledger."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.service.event_bus import (
    LifeEvent,
    LifeEventChannel,
    RawEventGapError,
    RawEventStore,
)
from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.event_contracts import (
    LifeEventConsumerConflict,
    LifeEventOccurrenceConflict,
    LifeEventSnapshotImportPort,
    LifeEventSnapshotSourcePort,
    LifeEventStorePort,
)
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.event_schema import ensure_life_event_schema
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.migration.copy_authority import (
    CopyAuthorityToken,
    MySQLCopyAuthorityRegistry,
)
from plugins.life_engine.storage.migration.event_copy import (
    copy_life_events_from_sqlite,
)
from plugins.life_engine.storage.migration.event_export import (
    export_life_events_to_sqlite,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from scripts.migrate_life_events import _snapshot_event_source


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="life-event-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"life-event": "2" * 64},
        frontiers={"life-event": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


def test_life_event_migration_resolves_only_manifest_declared_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sqlite" / "life_engine_workspace" / "life_events.sqlite3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"exact-ledger-bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "sqlite": [
            {
                "source_relative": "life_engine_workspace/life_events.sqlite3",
                "backup_relative": "sqlite/life_engine_workspace/life_events.sqlite3",
                "backup_sha256": digest,
            }
        ]
    }

    assert _snapshot_event_source(tmp_path.resolve(), manifest) == source.resolve()
    manifest["sqlite"][0]["backup_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="hash mismatch"):
        _snapshot_event_source(tmp_path.resolve(), manifest)


async def test_active_life_event_writer_cannot_relax_database_immutability(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, _, _, _):
        with pytest.raises(RuntimeError, match="only for candidate copy"):
            await ensure_life_event_schema(
                runtime,
                require_database_immutability=False,
            )


@asynccontextmanager
async def _local_store(
    tmp_path: Path,
) -> AsyncIterator[
    tuple[StorageBackendRuntime, LifeEventStorePort, FileAuthorityRegistry, object]
]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        _generation().generation_id,
        expected_epoch=0,
        owner_id="life-event-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=_generation().generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_LIFE_EVENT_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_LIFE_EVENT_FENCE": token.fencing_token},
    )
    store = await open_life_event_store(runtime, initialize_schema=True)
    try:
        yield runtime, store, registry, token
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


def _event(
    identity: str,
    *,
    content: str = "evidence",
    sync_export: bool = False,
    visibility: str = "private",
) -> LifeEvent:
    return LifeEvent(
        event_id=f"event:{identity}",
        sequence=17,
        timestamp="2026-08-04T01:02:03.123456+00:00",
        source="contract.life-event",
        channel=LifeEventChannel.LIFE.value,
        event_type="contract.observation",
        content=content,
        stream_id="stream:contract",
        metadata={
            "sync_export": sync_export,
            "visibility": visibility,
            "contract": True,
        },
        occurrence_id=f"occurrence:{identity}",
        source_instance_id="instance:contract",
    )


class _CopyRegistryStub:
    def __init__(self) -> None:
        self.progress: list[int] = []
        self.conflicts: list[dict[str, Any]] = []

    async def set_progress(
        self,
        token: object,
        *,
        copied_records: int,
    ) -> None:
        del token
        self.progress.append(int(copied_records))

    async def record_conflict(self, token: object, **kwargs: Any) -> None:
        del token
        self.conflicts.append(dict(kwargs))


async def test_life_event_local_contract_preserves_identity_and_atomicity(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        first = await store.append(_event("a", sync_export=True, visibility="shared"))
        replay = await store.append(_event("a", sync_export=True, visibility="shared"))
        assert first == replay
        assert first.sequence > 0
        assert first.source_sequence == 17
        assert first.recorded_at

        with pytest.raises(LifeEventOccurrenceConflict):
            await store.append(_event("a", content="different"))

        with pytest.raises(LifeEventOccurrenceConflict):
            await store.append_many(
                [_event("rolled-back"), _event("a", content="different")]
            )
        assert [item.occurrence_id for item in await store.read_since(0)] == [
            "occurrence:a"
        ]
        health = await store.health_snapshot()
        assert health["total"] == 1
        assert health["export_outbox"] == {"pending": 1}


async def test_life_event_positions_are_tokens_and_gaps_are_explicit(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store, _, _):
        first = await store.append(_event("first"))
        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    "UPDATE sqlite_sequence SET seq = 20 WHERE name = 'raw_life_events'"
                )
            )
        later = await store.append(_event("later"))
        assert later.sequence == 21
        assert [item.sequence for item in await store.read_since(first.sequence)] == [
            21
        ]

        async with runtime.unit_of_work() as uow:
            await uow.session.execute(
                text(
                    """INSERT INTO raw_event_ledger_meta (
                        meta_key, meta_value, updated_at
                    ) VALUES ('history_floor_position', '20', :updated_at)"""
                ),
                {"updated_at": "2026-08-04T02:00:00+00:00"},
            )
        with pytest.raises(RawEventGapError) as captured:
            await store.read_since(19)
        assert captured.value.earliest_available == 21


async def test_life_event_consumer_cursor_is_bounded_revision_cas(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store, _, _):
        event = await store.append(_event("cursor"))
        initial = await store.consumer_cursor("consumer:contract")
        assert (initial.position, initial.revision) == (0, 0)
        committed = await store.commit_consumer_cursor(
            "consumer:contract",
            expected_position=0,
            expected_revision=0,
            through_position=event.sequence,
            metadata={"accepted": True},
        )
        assert (committed.position, committed.revision) == (event.sequence, 1)
        no_op = await store.commit_consumer_cursor(
            "consumer:contract",
            expected_position=event.sequence,
            expected_revision=1,
            through_position=event.sequence,
            metadata={"ignored": True},
        )
        assert no_op == committed
        with pytest.raises(LifeEventConsumerConflict):
            await store.commit_consumer_cursor(
                "consumer:contract",
                expected_position=0,
                expected_revision=0,
                through_position=event.sequence,
            )
        with pytest.raises(LifeEventConsumerConflict, match="exceeds"):
            await store.commit_consumer_cursor(
                "consumer:new",
                expected_position=0,
                expected_revision=0,
                through_position=event.sequence + 1,
            )
        assert (
            await store.commit_consumer_offset(
                "consumer:contract",
                event.sequence,
                metadata={"legacy": True},
            )
            == event.sequence
        )


async def test_life_event_concurrent_cursor_and_database_immutability(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store, _, _):
        event = await store.append(_event("race"))

        async def advance(label: str) -> str:
            try:
                await store.commit_consumer_cursor(
                    "consumer:race",
                    expected_position=0,
                    expected_revision=0,
                    through_position=event.sequence,
                    metadata={"winner": label},
                )
            except LifeEventConsumerConflict:
                return "conflict"
            return "committed"

        outcomes = await asyncio.gather(advance("a"), advance("b"))
        assert sorted(outcomes) == ["committed", "conflict"]

        assert runtime.engine is not None
        with pytest.raises(DBAPIError, match="RawLifeEventImmutable"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE raw_life_events SET source_event_id = 'forged' "
                        "WHERE occurrence_id = 'occurrence:race'"
                    )
                )
        with pytest.raises(DBAPIError, match="RawLifeEventImmutable"):
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM raw_life_events "
                        "WHERE occurrence_id = 'occurrence:race'"
                    )
                )


async def test_life_event_restart_and_stale_writer_fencing(tmp_path: Path) -> None:
    async with _local_store(tmp_path) as (_, store, registry, token):
        persisted = await store.append(_event("restart"))
        settings = StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=_generation().generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_LIFE_EVENT_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=tmp_path / "authority.json",
            ),
        )
        second_runtime = await open_storage_backend(
            settings,
            environment={"TEST_LIFE_EVENT_FENCE": token.fencing_token},
        )
        second_store = await open_life_event_store(second_runtime)
        try:
            assert await second_store.read_tail(1) == [persisted]
        finally:
            await second_runtime.close()

        await registry.revoke(token)
        with pytest.raises(RuntimeError):
            await store.append(replace(_event("stale"), sequence=18))


async def test_life_event_snapshot_copy_is_idempotent_and_root_verified(
    tmp_path: Path,
) -> None:
    source = RawEventStore(tmp_path / "source")
    source_events = await source.append_many(
        [_event("copy-a"), _event("copy-b"), _event("copy-c")]
    )
    await source.commit_consumer_offset(
        "consumer:copy",
        source_events[-1].sequence,
        metadata={"source": True},
    )
    registry = _CopyRegistryStub()
    token = cast(CopyAuthorityToken, object())
    async with _local_store(tmp_path / "target") as (runtime, target, _, _):
        assert isinstance(target, LifeEventSnapshotImportPort)
        with pytest.raises(RuntimeError, match="candidate-copy"):
            await copy_life_events_from_sqlite(
                source.database_path,
                target,
                copy_registry=cast(MySQLCopyAuthorityRegistry, registry),
                token=token,
                batch_size=2,
            )

        async def candidate_fence(_: object) -> None:
            return None

        async def candidate_validate() -> None:
            return None

        candidate_runtime = StorageBackendRuntime(
            enabled=True,
            backend=runtime.backend,
            backend_identity=runtime.backend_identity,
            generation=None,
            authority_registry=None,
            authority_token=None,
            engine=runtime.engine,
            session_factory=runtime.session_factory,
            _write_fence=candidate_fence,
            _writer_validator=candidate_validate,
            writer_role=StorageWriterRole.CANDIDATE_COPY,
        )
        target = await open_life_event_store(candidate_runtime)
        assert isinstance(target, LifeEventSnapshotImportPort)
        first = await copy_life_events_from_sqlite(
            source.database_path,
            target,
            copy_registry=cast(MySQLCopyAuthorityRegistry, registry),
            token=token,
            batch_size=2,
        )
        second = await copy_life_events_from_sqlite(
            source.database_path,
            target,
            copy_registry=cast(MySQLCopyAuthorityRegistry, registry),
            token=token,
            batch_size=2,
        )
        cursor = await target.consumer_cursor("consumer:copy")
        assert isinstance(target, LifeEventSnapshotSourcePort)
        export_directory = tmp_path / "reverse-export"
        export_report = await export_life_events_to_sqlite(
            target,
            export_directory,
            batch_size=2,
        )
        with pytest.raises(FileExistsError):
            await export_life_events_to_sqlite(target, export_directory)
        async with runtime.unit_of_work() as uow:
            copied_payloads = (
                (
                    await uow.session.execute(
                        text(
                            "SELECT payload_json, payload_hash FROM raw_life_events "
                            "ORDER BY ingest_position"
                        )
                    )
                )
                .mappings()
                .all()
            )
        with sqlite3.connect(source.database_path) as source_db:
            source_payloads = source_db.execute(
                "SELECT payload_json, payload_hash FROM raw_life_events "
                "ORDER BY ingest_position"
            ).fetchall()
        with sqlite3.connect(export_report.database_path) as export_db:
            exported_payloads = export_db.execute(
                "SELECT payload_json, payload_hash FROM raw_life_events "
                "ORDER BY ingest_position"
            ).fetchall()
    assert first == second
    assert first.verified is True
    assert first.copied_count == 3
    assert first.source_root_sha256 == first.target_root_sha256
    assert (cursor.position, cursor.revision) == (source_events[-1].sequence, 1)
    assert registry.conflicts == []
    assert registry.progress[-1] == 3
    assert [tuple(row.values()) for row in copied_payloads] == [
        tuple(row) for row in source_payloads
    ]
    assert exported_payloads == source_payloads
    assert export_report.event_count == 3
    assert export_report.root_sha256 == first.source_root_sha256
    assert not (export_directory / "EXPORT_INCOMPLETE").exists()
