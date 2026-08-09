"""Opt-in real MySQL contract for selectable life-learning storage."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.learning_contracts import (
    LEARNING_WRITER_CLAIM_NAMESPACE,
    LEARNING_WRITER_CLAIM_STATE_KEY,
    LearningEventDraft,
    LearningOccurrenceConflict,
    LearningProjectionWrite,
)
from plugins.life_engine.storage.learning_factory import open_learning_stores
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from plugins.life_engine.storage.writer_claims import SingletonWriterClaimLost
from src.kernel.storage.engine import MySQLStorageConfig, create_mysql_storage_engine


def _mysql_config() -> MySQLStorageConfig:
    if os.environ.get("ELYSIUM_TEST_MYSQL_LEARNING_ISOLATED") != "1":
        pytest.skip("isolated Learning MySQL contract is not explicitly enabled")
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get(  # type: ignore[arg-type]
            "ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"
        ),
    )


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="mysql-life-learning-contract-v1",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="3" * 64,
        root_hashes={"life-learning": "4" * 64},
        frontiers={"life-learning": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


@pytest.mark.timeout(180)
async def test_mysql_learning_event_projection_contract() -> None:
    config = _mysql_config()
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(engine, registry_id="life-learning-integration")
    runtime: StorageBackendRuntime | None = None
    suffix = uuid4().hex
    try:
        generation = _generation()
        await registry.register_generation(generation)
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id="life-learning-integration",
                authority_provider="mysql",
                authority_owner_id="life-learning-integration-writer",
                authority_lease_seconds=180,
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_LEARNING_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={"TEST_LEARNING_MYSQL_PASSWORD": config.password},
        )
        await open_learning_stores(runtime, initialize_schema=True)
        preclaim_store = (await open_learning_stores(runtime)).store
        preclaim_projection = LearningProjectionWrite(
            projection_name=f"mysql_learning_preclaim_{suffix}",
            expected_revision=0,
            expected_source_frontier=0,
            schema_version=1,
            projector_version="mysql-contract-v1",
            rebuild_state="ready",
            payload={"phase": "before-claim"},
        )
        with pytest.raises(
            SingletonWriterClaimLost,
            match="LearningSingletonWriterClaimRequired",
        ):
            await preclaim_store.commit(events=[], projections=[preclaim_projection])
        assert (
            await preclaim_store.get_projection(preclaim_projection.projection_name)
            is None
        )

        claim = await runtime.acquire_singleton_writer(
            namespace=LEARNING_WRITER_CLAIM_NAMESPACE,
            state_key=LEARNING_WRITER_CLAIM_STATE_KEY,
            owner_instance_id=f"life-learning-integration:{suffix}",
            lease_seconds=180,
        )
        store = (await open_learning_stores(runtime, writer_claim=claim)).store
        event = LearningEventDraft(
            occurrence_id=f"mysql-learning:{suffix}",
            event_kind="contract.observation",
            occurred_at="2026-08-04T06:00:00.123456+00:00",
            source="mysql.contract",
            actor_consciousness_instance_id=f"instance:{suffix}",
            subject_revision="c" * 64,
            provenance={"source_occurrence_id": f"source:{suffix}"},
            payload={"evidence": "UTF-8 花朵"},
        )
        write = LearningProjectionWrite(
            projection_name=f"mysql_learning_contract_{suffix}",
            expected_revision=0,
            expected_source_frontier=0,
            schema_version=1,
            projector_version="mysql-contract-v1",
            rebuild_state="ready",
            payload={"occurrence_id": event.occurrence_id},
        )
        first = await store.commit(events=[event], projections=[write])
        replay = await store.commit(events=[event], projections=[write])
        assert replay == first
        assert (await store.event_by_occurrence(event.occurrence_id)) == first.events[0]
        assert (await store.get_projection(write.projection_name)) == first.projections[
            0
        ]

        with pytest.raises(LearningOccurrenceConflict):
            await store.commit(
                events=[replace(event, payload={"evidence": "different"})],
                projections=[],
            )
        storage_health = await store.health_snapshot()
        assert storage_health["status"] == "healthy"
        assert "UTF-8 花朵" not in str(storage_health)

        unclaimed = (await open_learning_stores(runtime)).store
        blocked = replace(
            event,
            occurrence_id=f"mysql-learning-unclaimed:{suffix}",
        )
        unclaimed_event_commit = await unclaimed.commit(
            events=[blocked],
            projections=[],
        )
        assert unclaimed_event_commit.events[0].occurrence_id == blocked.occurrence_id
        unclaimed_projection = replace(
            write,
            projection_name=f"mysql-learning-unclaimed-projection:{suffix}",
            payload={"occurrence_id": blocked.occurrence_id},
        )
        with pytest.raises(
            SingletonWriterClaimLost,
            match="LearningSingletonWriterClaimRequired",
        ):
            await unclaimed.commit(events=[], projections=[unclaimed_projection])
        assert await store.event_by_occurrence(blocked.occurrence_id) is not None
        assert await store.get_projection(unclaimed_projection.projection_name) is None

        for statement in (
            text(
                """UPDATE learning_projections
                SET rebuild_state = 'blocked_raw_update'
                WHERE projection_name = :projection_name"""
            ),
            text(
                """DELETE FROM learning_projections
                WHERE projection_name = :projection_name"""
            ),
        ):
            with pytest.raises(
                DBAPIError,
                match="LearningSingletonWriterClaimRequired",
            ):
                async with runtime.unit_of_work() as uow:
                    await uow.session.execute(
                        statement,
                        {"projection_name": write.projection_name},
                    )
        assert (await store.get_projection(write.projection_name)) == first.projections[
            0
        ]

        assert await runtime.release_singleton_writer(claim) is True
        stale = replace(
            event,
            occurrence_id=f"mysql-learning-stale:{suffix}",
        )
        with pytest.raises(SingletonWriterClaimLost):
            await store.commit(events=[stale], projections=[])
        assert await store.event_by_occurrence(stale.occurrence_id) is None
    finally:
        try:
            if runtime is not None:
                await runtime.revoke_authority()
                await runtime.close()
        finally:
            await engine.dispose()
