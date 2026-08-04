"""Real-MySQL verification of the shared Presence/World domain contract."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from plugins.life_engine.storage.authority import MySQLAuthorityRegistry
from plugins.life_engine.storage.contracts import StorageBackendRuntime
from plugins.life_engine.storage.domain_contracts import PresenceWorldStores
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
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
from test.plugins.life_engine.test_presence_world_storage_contract import (
    _assert_presence_contract,
    _assert_world_contract,
)


def _mysql_config_from_environment() -> MySQLStorageConfig:
    host = os.environ.get("ELYSIUM_TEST_MYSQL_HOST", "")
    database = os.environ.get("ELYSIUM_TEST_MYSQL_DATABASE", "")
    user = os.environ.get("ELYSIUM_TEST_MYSQL_USER", "")
    if not host or not database or not user:
        pytest.skip("isolated MySQL integration database is not configured")
    if os.environ.get("ELYSIUM_TEST_MYSQL_PRESENCE_WORLD_ISOLATED") != "1":
        pytest.skip(
            "Presence/World MySQL contract requires an explicitly isolated database"
        )
    return MySQLStorageConfig(
        host=host,
        port=int(os.environ.get("ELYSIUM_TEST_MYSQL_PORT", "3306")),
        database=database,
        user=user,
        password=os.environ.get("ELYSIUM_TEST_MYSQL_PASSWORD", ""),
        ssl_mode=os.environ.get("ELYSIUM_TEST_MYSQL_SSL_MODE", "disabled"),  # type: ignore[arg-type]
    )


def _generation() -> BackendGeneration:
    return BackendGeneration(
        generation_id="mysql-presence-world-contract-v1",
        backend=BackendKind.MYSQL,
        schema_version=1,
        source_snapshot_sha256="e" * 64,
        root_hashes={"presence-world": "f" * 64},
        frontiers={"presence": 0, "world": 0},
        created_at="2026-08-04T00:00:00+00:00",
        verified_at="2026-08-04T00:01:00+00:00",
        status=GenerationStatus.VERIFIED,
    )


async def _clear_contract_rows(runtime: StorageBackendRuntime) -> None:
    async with runtime.unit_of_work() as uow:
        await uow.session.execute(
            text(
                "DELETE FROM consciousness_presence_outbox "
                "WHERE instance_id IN ('instance:owner', 'instance:claimant')"
            )
        )
        await uow.session.execute(
            text(
                "DELETE FROM consciousness_stream_owners "
                "WHERE instance_id IN ('instance:owner', 'instance:claimant')"
            )
        )
        await uow.session.execute(
            text(
                "DELETE FROM consciousness_presence "
                "WHERE instance_id IN ('instance:owner', 'instance:claimant')"
            )
        )
        await uow.session.execute(
            text(
                "DELETE FROM world_perception_cursors "
                "WHERE instance_id = 'instance:observer'"
            )
        )
        await uow.session.execute(
            text("DELETE FROM world_assertions WHERE assertion_id = 'world-a'")
        )
        await uow.session.execute(
            text("DELETE FROM world_projection_changes WHERE ingest_position = 1")
        )


@pytest.mark.timeout(120)
async def test_mysql_presence_world_adapters_share_domain_contract() -> None:
    """Run the same semantic contract against an isolated real MySQL backend."""

    config = _mysql_config_from_environment()
    engine = create_mysql_storage_engine(config)
    registry = MySQLAuthorityRegistry(
        engine,
        registry_id="life-presence-world-integration",
    )
    runtime: StorageBackendRuntime | None = None
    token = None
    stores: PresenceWorldStores | None = None
    try:
        generation = _generation()
        await registry.register_generation(generation)
        health = await registry.health()
        token = await registry.activate_generation(
            generation.generation_id,
            expected_epoch=int(health.get("authority_epoch") or 0),
            owner_id="presence-world-integration-writer",
            lease_seconds=120,
            confirm_previous_writers_stopped=True,
        )
        runtime = await open_storage_backend(
            StorageFactorySettings(
                enabled=True,
                authoritative_backend=BackendKind.MYSQL,
                backend_generation=generation.generation_id,
                schema_version=1,
                registry_id="life-presence-world-integration",
                authority_provider="mysql",
                authority_epoch=token.authority_epoch,
                authority_owner_id=token.owner_id,
                fencing_token_env="TEST_PRESENCE_WORLD_MYSQL_FENCE",
                mysql=MySQLBackendSettings(
                    host=config.host,
                    port=config.port,
                    database=config.database,
                    user=config.user,
                    password_env="TEST_STORAGE_MYSQL_PASSWORD",
                    ssl_mode=config.ssl_mode,
                ),
            ),
            environment={
                "TEST_PRESENCE_WORLD_MYSQL_FENCE": token.fencing_token,
                "TEST_STORAGE_MYSQL_PASSWORD": config.password,
            },
        )
        stores = await open_presence_world_stores(runtime, initialize_schema=True)
        await _clear_contract_rows(runtime)
        await _assert_presence_contract(stores)
        await _assert_world_contract(stores)
    finally:
        try:
            if runtime is not None:
                if stores is not None:
                    await _clear_contract_rows(runtime)
                await runtime.close()
        finally:
            try:
                if token is not None:
                    await registry.revoke(token)
            finally:
                await engine.dispose()
