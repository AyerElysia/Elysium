"""Contract tests for selected technical runtime state and event storage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from plugins.life_engine.storage.authority import FileAuthorityRegistry, StaleAuthorityToken
from plugins.life_engine.storage.contracts import StorageBackendRuntime, StorageRuntimeClosed
from plugins.life_engine.storage.factory import (
    LocalBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.runtime_contracts import (
    RuntimeEventConflict,
    RuntimeStateConflict,
    RuntimeStateStorePort,
)
from plugins.life_engine.storage.runtime_factory import open_runtime_state_store


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="runtime-state-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="1" * 64,
        root_hashes={"runtime_state": "2" * 64},
        frontiers={"runtime_state": 0},
        created_at="2026-08-06T00:00:00+00:00",
        verified_at="2026-08-06T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_store(
    tmp_path: Path,
) -> AsyncIterator[tuple[StorageBackendRuntime, RuntimeStateStorePort]]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    generation = _generation()
    await registry.register_generation(generation)
    token = await registry.activate_generation(
        generation.generation_id,
        expected_epoch=0,
        owner_id="runtime-state-contract",
        lease_seconds=300,
        confirm_previous_writers_stopped=True,
    )
    runtime = await open_storage_backend(
        StorageFactorySettings(
            enabled=True,
            authoritative_backend=BackendKind.LOCAL,
            backend_generation=generation.generation_id,
            schema_version=1,
            authority_epoch=token.authority_epoch,
            authority_owner_id=token.owner_id,
            fencing_token_env="TEST_RUNTIME_STATE_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_RUNTIME_STATE_FENCE": token.fencing_token},
    )
    store = await open_runtime_state_store(runtime, initialize_schema=True)
    try:
        yield runtime, store
    finally:
        await runtime.close()
        try:
            await registry.revoke(token)
        except StaleAuthorityToken:
            pass


@pytest.mark.asyncio
async def test_runtime_state_uses_exact_revision_cas(tmp_path: Path) -> None:
    async with _local_store(tmp_path) as (_, store):
        assert await store.get_state("life_chatter", "chat_global") is None

        first = await store.put_state(
            namespace="life_chatter",
            state_key="chat_global",
            expected_revision=0,
            schema_version=2,
            payload={"payloads": [{"role": "user", "content": ["hello"]}]},
        )
        assert first.revision == 1
        assert (await store.get_state("life_chatter", "chat_global")) == first

        second = await store.put_state(
            namespace="life_chatter",
            state_key="chat_global",
            expected_revision=1,
            schema_version=2,
            payload={"payloads": [{"role": "assistant", "content": ["hi"]}]},
        )
        assert second.revision == 2

        with pytest.raises(RuntimeStateConflict, match="expected=1:actual=2"):
            await store.put_state(
                namespace="life_chatter",
                state_key="chat_global",
                expected_revision=1,
                schema_version=2,
                payload={"payloads": []},
            )


@pytest.mark.asyncio
async def test_runtime_events_are_idempotent_and_conflict_on_different_bytes(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (_, store):
        first = await store.append_event(
            namespace="autonomy.lifecycle",
            occurrence_id="autonomy:occurrence:1",
            event_kind="intent.scheduled",
            payload={"intent_id": "intent-1", "status": "scheduled"},
            occurred_at="2026-08-06T01:02:03.123456+00:00",
        )
        replay = await store.append_event(
            namespace="autonomy.lifecycle",
            occurrence_id="autonomy:occurrence:1",
            event_kind="intent.scheduled",
            payload={"intent_id": "intent-1", "status": "scheduled"},
            occurred_at="2026-08-06T01:02:03.123456+00:00",
        )
        assert replay == first
        assert await store.read_events("autonomy.lifecycle") == [first]

        with pytest.raises(RuntimeEventConflict, match="OccurrenceConflict"):
            await store.append_event(
                namespace="autonomy.lifecycle",
                occurrence_id="autonomy:occurrence:1",
                event_kind="intent.scheduled",
                payload={"intent_id": "intent-1", "status": "cancelled"},
                occurred_at="2026-08-06T01:02:03.123456+00:00",
            )


@pytest.mark.asyncio
async def test_runtime_store_fails_after_owned_runtime_closes(tmp_path: Path) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        await runtime.close()
        with pytest.raises(StorageRuntimeClosed):
            await store.get_state("life_chatter", "chat_global")
