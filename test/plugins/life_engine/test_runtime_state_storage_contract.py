"""Contract tests for selected technical runtime state and event storage."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError, PendingRollbackError

from plugins.life_engine.storage.authority import (
    FileAuthorityRegistry,
    StaleAuthorityToken,
)
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageRuntimeClosed,
)
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
from plugins.life_engine.storage.writer_claims import (
    SingletonWriterClaimConflict,
    SingletonWriterClaimLost,
)


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
async def test_runtime_failed_write_preserves_original_dbapi_without_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        original = OperationalError(
            "SELECT CURRENT_TIMESTAMP(6)",
            {},
            RuntimeError("connection invalidated during runtime state write"),
        )
        clear_calls = 0

        async def fail_database_now(_session: object) -> None:
            raise original

        async def forbidden_clear(_runtime: object, _session: object) -> None:
            nonlocal clear_calls
            clear_calls += 1
            raise PendingRollbackError("secondary cleanup masked primary error")

        monkeypatch.setattr(store, "_database_now", fail_database_now)
        monkeypatch.setattr(
            type(runtime),
            "clear_singleton_writer_write",
            forbidden_clear,
        )

        with pytest.raises(OperationalError) as raised:
            await store.put_state(
                namespace="life_chatter",
                state_key="chat_global",
                expected_revision=0,
                schema_version=2,
                payload={"heartbeat_count": 1},
            )

        assert raised.value is original
        assert clear_calls == 0
        assert await store.get_state("life_chatter", "chat_global") is None


@pytest.mark.asyncio
async def test_runtime_cancelled_write_does_not_run_cleanup_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        clear_calls = 0

        async def cancel_database_now(_session: object) -> None:
            raise asyncio.CancelledError

        async def forbidden_clear(_runtime: object, _session: object) -> None:
            nonlocal clear_calls
            clear_calls += 1

        monkeypatch.setattr(store, "_database_now", cancel_database_now)
        monkeypatch.setattr(
            type(runtime),
            "clear_singleton_writer_write",
            forbidden_clear,
        )

        with pytest.raises(asyncio.CancelledError):
            await store.put_state(
                namespace="life_chatter",
                state_key="chat_global",
                expected_revision=0,
                schema_version=2,
                payload={"heartbeat_count": 1},
            )

        assert clear_calls == 0
        assert await store.get_state("life_chatter", "chat_global") is None


@pytest.mark.asyncio
async def test_runtime_unclaimed_success_skips_redundant_tail_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        clear_calls = 0

        async def unexpected_clear(_runtime: object, _session: object) -> None:
            nonlocal clear_calls
            clear_calls += 1

        monkeypatch.setattr(
            type(runtime),
            "clear_singleton_writer_write",
            unexpected_clear,
        )
        record = await store.put_state(
            namespace="life_chatter",
            state_key="chat_global",
            expected_revision=0,
            schema_version=2,
            payload={"heartbeat_count": 1},
        )

        assert record.revision == 1
        assert clear_calls == 0


@pytest.mark.asyncio
async def test_runtime_claimed_success_clears_once_and_clear_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        claim = await runtime.acquire_singleton_writer(
            namespace="life_engine.runtime_context",
            state_key="global",
            owner_instance_id="runtime-cleanup-contract",
            lease_seconds=30,
        )
        original_clear = type(runtime).clear_singleton_writer_write
        clear_calls = 0

        async def fail_clear(_runtime: object, session: object) -> None:
            nonlocal clear_calls
            clear_calls += 1
            raise OperationalError(
                "DELETE runtime_singleton_writer_bindings",
                {},
                RuntimeError("cleanup connection lost"),
            )

        monkeypatch.setattr(
            type(runtime),
            "clear_singleton_writer_write",
            fail_clear,
        )
        with pytest.raises(OperationalError, match="cleanup connection lost"):
            await store.put_state(
                namespace=claim.namespace,
                state_key=claim.state_key,
                expected_revision=0,
                schema_version=2,
                payload={"heartbeat_count": 1},
                writer_claim=claim,
            )
        assert clear_calls == 1
        monkeypatch.setattr(
            type(runtime),
            "clear_singleton_writer_write",
            original_clear,
        )
        assert await store.get_state(claim.namespace, claim.state_key) is None

        async def count_clear(runtime_instance: object, session: object) -> None:
            nonlocal clear_calls
            clear_calls += 1
            await original_clear(runtime_instance, session)  # type: ignore[arg-type]

        monkeypatch.setattr(
            type(runtime),
            "clear_singleton_writer_write",
            count_clear,
        )
        record = await store.put_state(
            namespace=claim.namespace,
            state_key=claim.state_key,
            expected_revision=0,
            schema_version=2,
            payload={"heartbeat_count": 2},
            writer_claim=claim,
        )
        assert record.revision == 1
        assert clear_calls == 2


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


@pytest.mark.asyncio
async def test_singleton_claim_fences_runtime_state_and_identifies_owner(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        claim = await runtime.acquire_singleton_writer(
            namespace="life_engine.runtime_context",
            state_key="global",
            owner_instance_id="host-a:pid-100:boot-a",
            lease_seconds=30,
        )
        assert claim.owner_instance_id == "host-a:pid-100:boot-a"
        assert claim.lease_epoch == 1
        assert "fencing_token" not in repr(claim)

        first = await store.put_state(
            namespace=claim.namespace,
            state_key=claim.state_key,
            expected_revision=0,
            schema_version=2,
            payload={"heartbeat_count": 1},
            writer_claim=claim,
        )
        assert first.revision == 1

        with pytest.raises(SingletonWriterClaimLost, match="ClaimRequired"):
            await store.put_state(
                namespace=claim.namespace,
                state_key=claim.state_key,
                expected_revision=1,
                schema_version=2,
                payload={"heartbeat_count": 2},
            )

        with pytest.raises(
            SingletonWriterClaimConflict,
            match="owner=host-a:pid-100:boot-a:epoch=1",
        ):
            await runtime._singleton_writer_claims.acquire(
                namespace=claim.namespace,
                state_key=claim.state_key,
                owner_instance_id="host-b:pid-200:boot-b",
                lease_seconds=30,
            )

        assert await runtime.release_singleton_writer(claim) is True
        with pytest.raises(SingletonWriterClaimLost, match="ClaimLost"):
            await store.put_state(
                namespace=claim.namespace,
                state_key=claim.state_key,
                expected_revision=1,
                schema_version=2,
                payload={"heartbeat_count": 2},
                writer_claim=claim,
            )

        takeover = await runtime.acquire_singleton_writer(
            namespace=claim.namespace,
            state_key=claim.state_key,
            owner_instance_id="host-b:pid-200:boot-b",
            lease_seconds=30,
        )
        assert takeover.lease_epoch == 2
        second = await store.put_state(
            namespace=takeover.namespace,
            state_key=takeover.state_key,
            expected_revision=1,
            schema_version=2,
            payload={"heartbeat_count": 2},
            writer_claim=takeover,
        )
        assert second.revision == 2


@pytest.mark.asyncio
async def test_runtime_authority_renewal_renews_managed_singleton_claim(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, store):
        claim = await runtime.acquire_singleton_writer(
            namespace="life_engine.runtime_context",
            state_key="global",
            owner_instance_id="host-a:pid-100:boot-a",
            lease_seconds=30,
        )
        await runtime.renew_authority(lease_seconds=300)

        # The consumer can keep its opaque original token snapshot; renewal
        # changes only the database-time lease_until value, not its identity.
        record = await store.put_state(
            namespace=claim.namespace,
            state_key=claim.state_key,
            expected_revision=0,
            schema_version=2,
            payload={"heartbeat_count": 1},
            writer_claim=claim,
        )
        assert record.revision == 1
        health = await runtime._singleton_writer_claims.health_snapshot()
        assert health["known_claim_count"] == 1
        assert health["live_claim_count"] == 1


@pytest.mark.asyncio
async def test_runtime_exposes_trigger_binding_without_claim_store_access(
    tmp_path: Path,
) -> None:
    async with _local_store(tmp_path) as (runtime, _store):
        claim = await runtime.acquire_singleton_writer(
            namespace="life_engine.learning",
            state_key="selected_persistence",
            owner_instance_id="host-a:pid-100:boot-a",
            lease_seconds=30,
        )
        async with runtime.unit_of_work(writer_claim=claim) as uow:
            await runtime.bind_singleton_writer_write(uow.session, claim)
            await runtime.clear_singleton_writer_write(uow.session)

        assert await runtime.release_singleton_writer(claim) is True
        async with runtime.unit_of_work() as uow:
            with pytest.raises(SingletonWriterClaimLost, match="ClaimLost"):
                await runtime.bind_singleton_writer_write(uow.session, claim)
