"""Shared Presence/World contracts for selectable domain storage adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from plugins.life_engine.service.event_bus import LifeEvent, LifeEventChannel
from plugins.life_engine.service.presence_store import (
    PresenceRevisionConflict,
    StreamOwnershipConflict,
)
from plugins.life_engine.service.world_projection import (
    WORLD_ASSERTION_ORDER_NEWEST_FIRST,
    WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
    WORLD_OBSERVATION_EVENT,
    PerceptionCursorConflict,
    WorldProjectionConflict,
    WorldProjectionUnavailable,
)
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.domain_contracts import (
    PresenceLeaseConflict,
    PresenceWorldStores,
)
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
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
from test.plugins.life_engine.presence_world_fakes import build_fake_stores


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="presence-world-local-v1",
        backend=BackendKind.LOCAL,
        schema_version=1,
        source_snapshot_sha256="e" * 64,
        root_hashes={"presence-world": "f" * 64},
        frontiers={"presence": 0, "world": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@asynccontextmanager
async def _local_stores(
    tmp_path: Path,
    *,
    initialize_schema: bool = True,
) -> AsyncIterator[tuple[StorageBackendRuntime, PresenceWorldStores]]:
    authority_path = tmp_path / "authority.json"
    registry = FileAuthorityRegistry(authority_path)
    await registry.register_generation(_generation())
    token = await registry.activate_generation(
        _generation().generation_id,
        expected_epoch=0,
        owner_id="presence-world-contract",
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
            fencing_token_env="TEST_PRESENCE_WORLD_FENCE",
            local=LocalBackendSettings(
                database_path=tmp_path / "life.sqlite3",
                authority_state_path=authority_path,
            ),
        ),
        environment={"TEST_PRESENCE_WORLD_FENCE": token.fencing_token},
    )
    stores = await open_presence_world_stores(
        runtime,
        initialize_schema=initialize_schema,
    )
    try:
        yield runtime, stores
    finally:
        await runtime.close()
        await registry.revoke(token)


def _instance(
    instance_id: str,
    stream_id: str,
    *,
    process_epoch: str,
    lease_seconds: int = 30,
) -> dict[str, object]:
    return {
        "instance_id": instance_id,
        "kind": "contract.runtime",
        "display_name": instance_id,
        "status": "active",
        "created_at": "2026-08-04T00:00:00+00:00",
        "last_active_at": "2026-08-04T00:00:00+00:00",
        "suspended_at": "",
        "stream_ids": [stream_id],
        "perception_filter": {},
        "metadata": {"contract": True},
        "session_id": f"session:{instance_id}",
        "process_epoch": process_epoch,
        "lease_expires_at": "",
        "lease_duration_seconds": lease_seconds,
        "revision": 0,
    }


def _observation(
    identity: str,
    *,
    sequence: int,
    value: str,
    predicate: str = "state",
    status: str = "",
    observed_at: str = "2026-08-04T01:00:00+00:00",
) -> LifeEvent:
    return LifeEvent(
        event_id=f"event-{identity}",
        sequence=sequence,
        timestamp=observed_at,
        source="contract.world",
        channel=LifeEventChannel.LIFE.value,
        event_type=WORLD_OBSERVATION_EVENT,
        content=json.dumps(
            {
                "assertion": {
                    "assertion_id": identity,
                    "subject": "subject:contract",
                    "predicate": predicate,
                    "value": value,
                    "status": status,
                    "observed_at": observed_at,
                }
            }
        ),
        stream_id="stream:world",
        occurrence_id=f"occurrence-{identity}",
        recorded_at="2026-08-04T01:00:01+00:00",
        source_instance_id="instance:source",
    )


def _unprojected_event(identity: str, *, sequence: int) -> LifeEvent:
    return LifeEvent(
        event_id=f"event-{identity}",
        sequence=sequence,
        timestamp="2026-08-04T01:00:00+00:00",
        source="contract.unprojected",
        channel=LifeEventChannel.SYSTEM.value,
        event_type="contract.unprojected",
        content="{}",
        occurrence_id=f"occurrence-{identity}",
        recorded_at="2026-08-04T01:00:01+00:00",
    )


async def _assert_presence_contract(stores: PresenceWorldStores) -> None:
    presence = stores.presence
    with pytest.raises(PresenceLeaseConflict, match="database time"):
        await presence.commit(
            _instance(
                "instance:forged",
                "stream:forged",
                process_epoch="epoch:forged",
            ),
            expected_revision=None,
            event_type="consciousness.instance_registered",
        )
    owner = await presence.commit(
        _instance(
            "instance:owner",
            "stream:shared",
            process_epoch="epoch:old",
            lease_seconds=1,
        ),
        expected_revision=None,
        event_type="consciousness.instance_registered",
        refresh_lease=True,
    )
    assert owner.revision == 1
    expirable = await presence.commit(
        _instance(
            "instance:background-expiry",
            "stream:background-expiry",
            process_epoch="epoch:background-expiry",
            lease_seconds=1,
        ),
        expected_revision=None,
        event_type="consciousness.instance_registered",
        refresh_lease=True,
    )
    assert expirable.revision == 1
    with pytest.raises(StreamOwnershipConflict):
        await presence.commit(
            _instance(
                "instance:claimant",
                "stream:shared",
                process_epoch="epoch:new",
            ),
            expected_revision=None,
            event_type="consciousness.instance_registered",
            refresh_lease=True,
        )
    with pytest.raises(StreamOwnershipConflict):
        await presence.takeover_expired(
            _instance(
                "instance:claimant",
                "stream:shared",
                process_epoch="epoch:new",
            ),
            expected_revision=None,
            process_epoch="epoch:new",
            lease_seconds=60,
        )
    await asyncio.sleep(1.05)

    takeover = await presence.takeover_expired(
        _instance(
            "instance:claimant",
            "stream:shared",
            process_epoch="epoch:new",
        ),
        expected_revision=None,
        process_epoch="epoch:new",
        lease_seconds=60,
    )
    assert takeover.claimant.revision == 1
    assert takeover.claimant.instance["status"] == "active"
    assert [item.instance["instance_id"] for item in takeover.displaced] == [
        "instance:owner"
    ]
    assert takeover.displaced[0].instance["status"] == "suspended"
    expired = await presence.expire_leases(limit=10)
    assert [item.instance["instance_id"] for item in expired] == [
        "instance:background-expiry"
    ]
    assert expired[0].instance["status"] == "suspended"
    assert await presence.expire_leases(limit=10) == ()
    with pytest.raises(PresenceRevisionConflict):
        await presence.commit(
            {**takeover.displaced[0].instance, "status": "active"},
            expected_revision=1,
            event_type="consciousness.instance_resumed",
        )

    renewed = await presence.renew_lease(
        "instance:claimant",
        expected_revision=takeover.claimant.revision,
        process_epoch="epoch:new",
        lease_seconds=90,
    )
    lease_until = datetime.fromisoformat(
        str(renewed.instance["lease_expires_at"])
    ).astimezone(UTC)
    database_now = datetime.fromisoformat(renewed.database_now).astimezone(UTC)
    assert (lease_until - database_now).total_seconds() == pytest.approx(90, abs=0.01)
    with pytest.raises(PresenceLeaseConflict):
        await presence.renew_lease(
            "instance:claimant",
            expected_revision=renewed.revision,
            process_epoch="epoch:stale",
            lease_seconds=90,
        )

    pending = await presence.pending_events()
    assert [item["event_type"] for item in pending] == [
        "consciousness.instance_registered",
        "consciousness.instance_registered",
        "consciousness.instance_lease_expired",
        "consciousness.instance_taken_over",
        "consciousness.instance_lease_expired",
        "consciousness.instance_seen",
    ]
    await presence.acknowledge_events([item["outbox_id"] for item in pending])
    await presence.acknowledge_events([item["outbox_id"] for item in pending])
    assert await presence.pending_events() == []
    presence_health = await presence.health_snapshot()
    assert presence_health["instance_count"] == 3
    assert presence_health["active_count"] == 1
    assert presence_health["owned_stream_count"] == 1
    assert presence_health["pending_event_count"] == 0


async def _assert_world_contract(stores: PresenceWorldStores) -> None:
    world = stores.world
    original = _observation("world-a", sequence=1, value="first")
    unprojected = _unprojected_event("opaque-position", sequence=3)
    assert await world.apply_events([original]) == 1
    assert await world.apply_events([original]) == 1
    assert await world.apply_events([unprojected]) == 3
    with pytest.raises(WorldProjectionConflict, match="ingest position"):
        await world.apply_events(
            [replace(original, source_instance_id="instance:conflict")]
        )
    assert [item.value for item in await world.list_assertions()] == ["first"]
    reference_page = await world.list_assertion_references_page(
        include_retracted=True,
        limit=1,
        inline_max_bytes=0,
    )
    assert reference_page.total_items == 1
    assert reference_page.items[0].assertion_id == "world-a"
    assert reference_page.items[0].value_inlined is False
    chunks = []
    offset = 0
    while True:
        chunk = await world.read_assertion_value_chunk(
            "world-a",
            offset_bytes=offset,
            max_bytes=4,
        )
        chunks.append(chunk.content)
        if chunk.complete:
            break
        assert chunk.next_offset_bytes > offset
        offset = chunk.next_offset_bytes
    assert json.loads("".join(chunks)) == "first"
    change_page = await world.change_references_page(
        0,
        through_position=3,
        limit=1,
        inline_max_bytes=0,
    )
    assert change_page.total_items == 1
    assert change_page.items[0].ingest_position == 1
    assert change_page.items[0].payload_inlined is False
    change_chunks = []
    offset = 0
    while True:
        chunk = await world.read_change_payload_chunk(
            1,
            offset_bytes=offset,
            max_bytes=7,
        )
        change_chunks.append(chunk.content)
        if chunk.complete:
            break
        offset = chunk.next_offset_bytes
    assert json.loads("".join(change_chunks))["assertion"]["value"] == "first"

    committed = await world.commit_perception_cursor(
        "instance:observer",
        expected_position=0,
        expected_revision=0,
        through_position=3,
    )
    assert committed == (3, 1)
    assert (
        await world.commit_perception_cursor(
            "instance:observer",
            expected_position=3,
            expected_revision=1,
            through_position=3,
        )
        == committed
    )
    with pytest.raises(PerceptionCursorConflict):
        await world.commit_perception_cursor(
            "instance:observer",
            expected_position=3,
            expected_revision=0,
            through_position=3,
        )

    await world.begin_rebuild()
    assert await world.perception_cursor("instance:observer") == committed
    rebuilding = await world.projector_contract()
    assert rebuilding["as_of_ingest_position"] == 0
    assert rebuilding["rebuild_state"] == "rebuilding"
    await world.apply_events([original, unprojected])
    await world.finish_rebuild(expected_frontier=3)
    world_health = await world.health_snapshot()
    assert world_health["rebuild_state"] == "idle"
    assert world_health["assertion_count"] == 1
    assert world_health["change_count"] == 1
    assert world_health["cursors"][0]["lag"] == 0
    rebuilt = await world.projector_contract()
    assert rebuilt["rebuild_state"] == "idle"
    assert rebuilt["policy"] == "source-preserving-v1"
    assert await world.perception_cursor("instance:observer") == committed
    assert [item.value for item in await world.list_assertions()] == ["first"]

    await world.begin_rebuild()
    await world.fail_rebuild()
    assert (await world.projector_contract())["rebuild_state"] == "failed"
    with pytest.raises(WorldProjectionUnavailable):
        await world.apply_events([original])
    await world.begin_rebuild()
    await world.apply_events([original, unprojected])
    await world.finish_rebuild(expected_frontier=3)
    lifecycle = _observation(
        "world-lifecycle",
        sequence=4,
        value="historical-session",
        predicate="session_state",
        observed_at="2026-08-04T02:00:00+00:00",
    )
    legacy = _observation(
        "world-legacy",
        sequence=5,
        value="historical-import",
        predicate="legacy_snapshot",
        status="legacy_import",
        observed_at="2026-08-04T03:00:00+00:00",
    )
    current = _observation(
        "world-current",
        sequence=6,
        value="present-fact",
        observed_at="2026-08-04T04:00:00+00:00",
    )
    assert await world.apply_events([lifecycle, legacy, current]) == 6
    current_page = await world.list_assertion_references_page(
        delivery_scope=WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
        limit=1,
    )
    assert current_page.result_order == WORLD_ASSERTION_ORDER_NEWEST_FIRST
    assert current_page.items[0].assertion_id == "world-current"
    history_page = await world.list_assertion_references_page(
        include_retracted=True,
        limit=10,
    )
    assert {item.assertion_id for item in history_page.items} == {
        "world-a",
        "world-lifecycle",
        "world-legacy",
        "world-current",
    }
    with pytest.raises(ValueError, match="cannot include retracted"):
        await world.list_assertion_references_page(
            include_retracted=True,
            delivery_scope=WORLD_ASSERTION_SCOPE_CURRENT_SNAPSHOT,
        )


@pytest.mark.asyncio
async def test_local_presence_and_world_share_backend_contract(tmp_path: Path) -> None:
    async with _local_stores(tmp_path) as (runtime, stores):
        await _assert_presence_contract(stores)
        await _assert_world_contract(stores)
        reopened = await open_presence_world_stores(runtime)
        assert [
            item["instance_id"] for item in await reopened.presence.list_instances()
        ] == [
            "instance:background-expiry",
            "instance:claimant",
            "instance:owner",
        ]
        assert await reopened.world.perception_cursor("instance:observer") == (3, 1)


@pytest.mark.asyncio
async def test_fake_presence_and_world_share_backend_contract() -> None:
    stores = build_fake_stores()
    await _assert_presence_contract(stores)
    await _assert_world_contract(stores)


@pytest.mark.asyncio
async def test_domain_factory_does_not_initialize_schema_implicitly(
    tmp_path: Path,
) -> None:
    async with _local_stores(tmp_path, initialize_schema=False) as (runtime, _stores):
        async with runtime.engine.connect() as connection:
            table_count = await connection.scalar(
                text(
                    """SELECT COUNT(*) FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN ('consciousness_presence', 'world_projection_meta')"""
                )
            )
        assert table_count == 0


@pytest.mark.asyncio
async def test_local_concurrent_cas_and_unique_owner(tmp_path: Path) -> None:
    async with _local_stores(tmp_path) as (_runtime, stores):
        presence = stores.presence
        registered = await presence.commit(
            _instance(
                "instance:lease",
                "stream:lease",
                process_epoch="epoch:lease",
            ),
            expected_revision=None,
            event_type="consciousness.instance_registered",
            refresh_lease=True,
        )
        renewals = await asyncio.gather(
            *(
                presence.renew_lease(
                    "instance:lease",
                    expected_revision=registered.revision,
                    process_epoch="epoch:lease",
                    lease_seconds=30,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in renewals) == 1
        assert sum(isinstance(item, PresenceRevisionConflict) for item in renewals) == 1

        duplicate = _instance(
            "instance:duplicate",
            "stream:duplicate",
            process_epoch="epoch:duplicate",
        )
        duplicate["lease_duration_seconds"] = None
        duplicate_starts = await asyncio.gather(
            *(
                presence.commit(
                    duplicate,
                    expected_revision=None,
                    event_type="consciousness.instance_registered",
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert (
            sum(not isinstance(item, BaseException) for item in duplicate_starts) == 1
        )
        assert (
            sum(isinstance(item, PresenceRevisionConflict) for item in duplicate_starts)
            == 1
        )

        claimants = []
        for identity in ("instance:claim-a", "instance:claim-b"):
            candidate = _instance(
                identity,
                "stream:contended",
                process_epoch=f"epoch:{identity}",
            )
            candidate["lease_duration_seconds"] = None
            claimants.append(candidate)
        claims = await asyncio.gather(
            *(
                presence.commit(
                    candidate,
                    expected_revision=None,
                    event_type="consciousness.instance_registered",
                )
                for candidate in claimants
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in claims) == 1
        assert sum(isinstance(item, StreamOwnershipConflict) for item in claims) == 1

        world = stores.world
        await world.apply_events([_observation("concurrent", sequence=1, value="one")])
        cursor_commits = await asyncio.gather(
            *(
                world.commit_perception_cursor(
                    "instance:observer",
                    expected_position=0,
                    expected_revision=0,
                    through_position=1,
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in cursor_commits) == 1
        assert (
            sum(isinstance(item, PerceptionCursorConflict) for item in cursor_commits)
            == 1
        ), [type(item).__name__ for item in cursor_commits]
