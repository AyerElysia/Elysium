"""Opt-in real-MySQL contract for the authoritative Life Event ledger."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.event_contracts import LifeEventOccurrenceConflict
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.factory import (
    MySQLBackendSettings,
    StorageFactorySettings,
    open_storage_backend,
)
from plugins.life_engine.storage.models import (
    BackendGeneration,
    BackendKind,
    GenerationStatus,
)
from src.kernel.storage.engine import (
    MySQLStorageConfig,
    create_mysql_storage_engine,
)
from test.plugins.life_engine.test_life_event_storage_contract import _event


def _config() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    if os.environ.get("ELYSIUM_TEST_MYSQL_LIFE_EVENT_ISOLATED") != "1":
        pytest.skip("Life Event MySQL contract requires an isolated database")
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
    )


@pytest.mark.timeout(120)
async def test_mysql_life_event_adapter_contract() -> None:
    """Exercise immutable append and cursor CAS in a disposable schema."""

    config = _config()
    identity = uuid4().hex
    generation = BackendGeneration(
        generation_id=f"mysql-life-event-{identity}",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="3" * 64,
        root_hashes={"life-event": "4" * 64},
        frontiers={"life-event": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )
    registry_id = f"life-event-integration-{identity}"
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(engine, registry_id=registry_id)
    runtime = None
    try:
        await registry.register_generation(generation)
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id=registry_id,
                authority_provider="mysql",
                authority_owner_id=f"writer-{identity}",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_LIFE_EVENT_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={"TEST_LIFE_EVENT_MYSQL_PASSWORD": config.password},
        )
        store = await open_life_event_store(runtime, initialize_schema=True)
        event = _event(identity, sync_export=True, visibility="shared")
        persisted = await store.append(event)
        assert await store.append(event) == persisted
        with pytest.raises(LifeEventOccurrenceConflict):
            await store.append(replace(event, content="conflicting evidence"))
        cursor = await store.commit_consumer_cursor(
            f"consumer:{identity}",
            expected_position=0,
            expected_revision=0,
            through_position=persisted.sequence,
        )
        assert (cursor.position, cursor.revision) == (persisted.sequence, 1)
        assert (await store.health_snapshot())["latest_position"] >= persisted.sequence
    finally:
        if runtime is not None:
            await runtime.revoke_authority()
            await runtime.close()
        await engine.dispose()
