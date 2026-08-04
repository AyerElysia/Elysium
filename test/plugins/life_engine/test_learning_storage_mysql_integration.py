"""Opt-in real MySQL contract for selectable life-learning storage."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.learning_contracts import (
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
    token = None
    suffix = uuid4().hex
    try:
        generation = _generation()
        await registry.register_generation(generation)
        health = await registry.health()
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health.get("authority_epoch") or 0),
            owner_id="life-learning-integration-writer",
            lease_seconds=180,
            confirm_previous_writers_stopped=True,
        )
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id="life-learning-integration",
                authority_provider="mysql",
                authority_epoch=token.authority_epoch,
                authority_owner_id=token.owner_id,
                fencing_token_env="TEST_LEARNING_MYSQL_FENCE",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_LEARNING_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={
                "TEST_LEARNING_MYSQL_FENCE": token.fencing_token,
                "TEST_LEARNING_MYSQL_PASSWORD": config.password,
            },
        )
        store = (
            await open_learning_stores(
                runtime,
                initialize_schema=True,
            )
        ).store
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
    finally:
        try:
            if runtime is not None:
                await runtime.close()
        finally:
            try:
                if token is not None:
                    await registry.revoke(token)
            finally:
                await engine.dispose()
